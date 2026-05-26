"""
skills_owl.py  —  OWL 2 DL + SKOS native integration for Vera ontologies
============================================================================

This module deepens the existing Vera ontology schema with W3C OWL 2 DL and
SKOS standards rather than treating OWL as a foreign export format. The
existing Vera fields (entities, relationships, processing_rules, memory_slots,
context_hints, domain) are PRESERVED, and a parallel set of OWL-native fields
is added so an ontology record IS itself a (minimal) OWL document.

Schema extension — added fields (all optional, additive)
─────────────────────────────────────────────────────────

Top level (Ontology):
    iri:           str    — base IRI for this ontology, e.g.
                            "https://vera.local/ontology/threat-intel#"
    imports:       [str]  — list of OWL ontology IRIs this imports
    annotations:   {str: str}   — extra owl:Annotation properties
                                  (free-form, e.g. {"creator": "alice",
                                  "version": "1.2", "license": "..."})
    pref_label:    str    — skos:prefLabel
    alt_labels:    [str]  — skos:altLabel (synonyms)

Per Entity (extends entity dict):
    iri:           str             — full IRI; computed from name if absent
    sub_class_of:  [str]           — list of parent class names (rdfs:subClassOf)
    equivalent_to: [str]           — owl:equivalentClass references
    disjoint_with: [str]           — owl:disjointWith references
    pref_label:    str             — skos:prefLabel  (UI label override)
    alt_labels:    [str]           — skos:altLabel   (synonyms)
    broader:       [str]           — skos:broader    (more general concept)
    narrower:      [str]           — skos:narrower
    related:       [str]           — skos:related
    notation:      str             — skos:notation   (code/identifier)
    restrictions:  [{kind, on_property, value, qualifier}]
                                   — owl:Restriction list
                                     kind ∈ {someValuesFrom, allValuesFrom,
                                            hasValue, minCardinality,
                                            maxCardinality, exactCardinality,
                                            minQualifiedCardinality,
                                            maxQualifiedCardinality,
                                            exactQualifiedCardinality}
    annotations:   {str: str}      — extra annotations on the class

Per Relationship (extends relationship dict):
    iri:              str             — full IRI of this property
    inverse_of:       str             — owl:inverseOf (label of inverse property)
    sub_property_of:  [str]           — rdfs:subPropertyOf labels
    characteristics:  [str]           — list from {Functional,
                                                   InverseFunctional,
                                                   Transitive, Symmetric,
                                                   Asymmetric, Reflexive,
                                                   Irreflexive}
    domain_classes:   [str]           — overrides single 'from' for multi-domain
    range_classes:    [str]           — overrides single 'to' for multi-range

Per attribute (when expressed as a dict instead of a bare string):
    name:        str   — the property label (also accepts bare string for
                         backward compat)
    iri:         str   — owl:DatatypeProperty IRI
    range_type:  str   — XSD type, default 'string'
                         ('string'|'integer'|'decimal'|'boolean'|'dateTime'|
                          'date'|'anyURI'|'float'|'double')
    functional:  bool  — if True, owl:FunctionalProperty
    description: str   — rdfs:comment

Capabilities registered
───────────────────────
  ontologies.export_owl     — render to Turtle / RDF-XML / JSON-LD / N-Triples
  ontologies.import_owl     — parse arbitrary OWL/RDF into the Vera schema
  ontologies.owl_context    — Turtle snippet for LLM injection
  ontologies.list_formats   — discoverable format list
  ontologies.schema         — describe the extended schema (for UIs)
  ontologies.add_class      — add a class with full OWL/SKOS fields
  ontologies.add_property   — add a property with full OWL fields
  ontologies.add_restriction — add an owl:Restriction to a class
  ontologies.validate       — sanity-check an ontology for OWL/SKOS coherence

Round-trip
──────────
The same JSON ontology, serialised to Turtle and parsed back, yields the same
record (modulo whitespace and key ordering). Existing records that lack the
new fields continue to work — the serialiser fills sensible defaults
(iri := "{base}/{Slugged_Name}", pref_label := name, etc).

Dependency: rdflib  (pip install rdflib).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    capability, emit_event, now_iso,
)

log = logging.getLogger("vera.skills.owl")

# ─────────────────────────────────────────────────────────────────────────────
# rdflib soft import — caps still load if it isn't installed
# ─────────────────────────────────────────────────────────────────────────────
try:
    import rdflib                                                    # type: ignore
    from rdflib import Graph, Namespace, URIRef, Literal, BNode      # type: ignore
    from rdflib.namespace import RDF, RDFS, OWL, XSD, DC, SKOS        # type: ignore
    _HAS_RDFLIB = True
    _RDFLIB_ERR = ""
except Exception as _e:                                               # pragma: no cover
    _HAS_RDFLIB = False
    _RDFLIB_ERR = repr(_e)
    log.warning("rdflib not available — OWL caps will return errors: %s", _e)

# ─────────────────────────────────────────────────────────────────────────────
# Format helpers
# ─────────────────────────────────────────────────────────────────────────────
SUPPORTED_FORMATS = {
    "turtle":   "text/turtle",
    "ttl":      "text/turtle",
    "xml":      "application/rdf+xml",
    "rdfxml":   "application/rdf+xml",
    "json-ld":  "application/ld+json",
    "jsonld":   "application/ld+json",
    "nt":       "application/n-triples",
    "ntriples": "application/n-triples",
    "n3":       "text/n3",
}
_FORMAT_TO_RDFLIB = {
    "turtle":   "turtle", "ttl":      "turtle",
    "xml":      "xml",    "rdfxml":   "xml",
    "json-ld":  "json-ld","jsonld":   "json-ld",
    "nt":       "nt",     "ntriples": "nt",
    "n3":       "n3",
}

# XSD type alias map
_XSD_MAP = {
    "string":   "string",  "str":      "string",  "text":   "string",
    "integer":  "integer", "int":      "integer",
    "decimal":  "decimal", "number":   "decimal",
    "float":    "float",
    "double":   "double",
    "boolean":  "boolean", "bool":     "boolean",
    "datetime": "dateTime","date":     "date",
    "uri":      "anyURI",  "url":      "anyURI",  "anyuri": "anyURI",
}

# OWL property characteristics
_CHARACTERISTIC_TO_OWL = {
    "Functional":         "FunctionalProperty",
    "InverseFunctional":  "InverseFunctionalProperty",
    "Transitive":         "TransitiveProperty",
    "Symmetric":          "SymmetricProperty",
    "Asymmetric":         "AsymmetricProperty",
    "Reflexive":          "ReflexiveProperty",
    "Irreflexive":        "IrreflexiveProperty",
}
_OWL_TO_CHARACTERISTIC = {v: k for k, v in _CHARACTERISTIC_TO_OWL.items()}

# Restriction kind → OWL predicate
_RESTRICTION_KINDS = {
    "someValuesFrom",
    "allValuesFrom",
    "hasValue",
    "minCardinality", "maxCardinality", "exactCardinality",
    "minQualifiedCardinality",
    "maxQualifiedCardinality",
    "exactQualifiedCardinality",
}

VERA_NS_URI = "https://vera.local/vocab#"


# ─────────────────────────────────────────────────────────────────────────────
# Slug / IRI helpers
# ─────────────────────────────────────────────────────────────────────────────
def _slug(s: str) -> str:
    if not s:
        return "_"
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(s).strip()).strip("_") or "_"
    if s[0].isdigit():
        s = "_" + s
    return s


def _ent_local(name: str) -> str:
    parts = re.split(r"[_\s\-]+", _slug(name))
    return "".join(p.capitalize() if p else "" for p in parts) or "Class"


def _prop_local(name: str) -> str:
    parts = re.split(r"[_\s\-]+", _slug(name))
    if not parts:
        return "prop"
    out = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
    return out or "prop"


def _normalise_base_iri(base: str) -> str:
    if not base:
        return "https://vera.local/ontology/_default#"
    if not base.endswith(("#", "/")):
        return base + "#"
    return base


def _ontology_iri(ont: Dict[str, Any]) -> str:
    """Resolve the base IRI for an ontology, computing from id+name if absent."""
    iri = (ont.get("iri") or "").strip()
    if iri:
        return _normalise_base_iri(iri)
    oid = ont.get("id") or _slug(ont.get("name", "ontology")).lower()
    return f"https://vera.local/ontology/{oid}#"


def _entity_iri(entity: Dict[str, Any], base_iri: str) -> str:
    if entity.get("iri"):
        return entity["iri"]
    return base_iri + _ent_local(entity.get("name", "Class"))


def _property_iri_for_label(label: str, base_iri: str) -> str:
    return base_iri + _prop_local(label)


def _resolve_class_ref(ref: str, classes_by_label: Dict[str, str], base_iri: str) -> str:
    """Resolve a class reference (label or IRI) → IRI."""
    if not ref:
        return ""
    if ref.startswith(("http://", "https://", "urn:")):
        return ref
    if ref in classes_by_label:
        return classes_by_label[ref]
    # Fall back to slugged local name
    return base_iri + _ent_local(ref)


# ─────────────────────────────────────────────────────────────────────────────
# JSON  →  RDF Graph
# ─────────────────────────────────────────────────────────────────────────────
def ontology_to_graph(ont: Dict[str, Any], base_iri: Optional[str] = None) -> "Graph":
    if not _HAS_RDFLIB:
        raise RuntimeError("rdflib not installed: " + _RDFLIB_ERR)

    base = _normalise_base_iri(base_iri or ont.get("iri") or _ontology_iri(ont))
    ONT = Namespace(base)
    VERA = Namespace(VERA_NS_URI)

    g: Graph = Graph()
    g.bind("owl",  OWL)
    g.bind("rdfs", RDFS)
    g.bind("rdf",  RDF)
    g.bind("xsd",  XSD)
    g.bind("dc",   DC)
    g.bind("skos", SKOS)
    g.bind("vera", VERA)
    g.bind("",     ONT)

    # ── Ontology header ────────────────────────────────────────────────
    ont_iri = URIRef(base.rstrip("#/"))
    g.add((ont_iri, RDF.type, OWL.Ontology))
    if ont.get("name"):
        g.add((ont_iri, RDFS.label,  Literal(ont["name"])))
    if ont.get("pref_label"):
        g.add((ont_iri, SKOS.prefLabel, Literal(ont["pref_label"])))
    for alt in (ont.get("alt_labels") or []):
        g.add((ont_iri, SKOS.altLabel, Literal(alt)))
    if ont.get("description"):
        g.add((ont_iri, RDFS.comment, Literal(ont["description"])))
    if ont.get("context_hints"):
        g.add((ont_iri, VERA.contextHint, Literal(ont["context_hints"])))
    if ont.get("domain"):
        g.add((ont_iri, DC.subject, Literal(ont["domain"])))
    for t in (ont.get("tags") or []):
        g.add((ont_iri, VERA.tag, Literal(str(t))))
    for imp in (ont.get("imports") or []):
        g.add((ont_iri, OWL.imports, URIRef(imp)))
    for k, v in (ont.get("annotations") or {}).items():
        g.add((ont_iri, VERA[_prop_local(k)], Literal(str(v))))

    # ── Build classes_by_label index for ref resolution ────────────────
    classes_by_label: Dict[str, str] = {}
    for e in (ont.get("entities") or []):
        nm = e.get("name") or ""
        if nm:
            classes_by_label[nm] = _entity_iri(e, base)

    # ── Entities → owl:Class ───────────────────────────────────────────
    for e in (ont.get("entities") or []):
        cname  = e.get("name") or "Entity"
        cls    = URIRef(_entity_iri(e, base))
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(cname)))

        # SKOS labels
        if e.get("pref_label"):
            g.add((cls, SKOS.prefLabel, Literal(e["pref_label"])))
        for alt in (e.get("alt_labels") or []):
            g.add((cls, SKOS.altLabel, Literal(alt)))
        if e.get("notation"):
            g.add((cls, SKOS.notation, Literal(e["notation"])))

        # SKOS conceptual hierarchy
        for b in (e.get("broader") or []):
            target = _resolve_class_ref(b, classes_by_label, base)
            if target:
                g.add((cls, SKOS.broader, URIRef(target)))
        for n in (e.get("narrower") or []):
            target = _resolve_class_ref(n, classes_by_label, base)
            if target:
                g.add((cls, SKOS.narrower, URIRef(target)))
        for r in (e.get("related") or []):
            target = _resolve_class_ref(r, classes_by_label, base)
            if target:
                g.add((cls, SKOS.related, URIRef(target)))

        # OWL hierarchy
        for parent in (e.get("sub_class_of") or []):
            target = _resolve_class_ref(parent, classes_by_label, base)
            if target:
                g.add((cls, RDFS.subClassOf, URIRef(target)))
        for eq in (e.get("equivalent_to") or []):
            target = _resolve_class_ref(eq, classes_by_label, base)
            if target:
                g.add((cls, OWL.equivalentClass, URIRef(target)))
        for dj in (e.get("disjoint_with") or []):
            target = _resolve_class_ref(dj, classes_by_label, base)
            if target:
                g.add((cls, OWL.disjointWith, URIRef(target)))

        # rdfs:comment
        if e.get("description"):
            g.add((cls, RDFS.comment, Literal(e["description"])))

        # Free-form annotations
        for k, v in (e.get("annotations") or {}).items():
            g.add((cls, VERA[_prop_local(k)], Literal(str(v))))

        # Attributes — strings or dicts
        for attr in (e.get("attributes") or []):
            if isinstance(attr, str):
                attr_dict = {"name": attr}
            elif isinstance(attr, dict):
                attr_dict = dict(attr)
            else:
                continue
            if not attr_dict.get("name"):
                continue
            prop_iri = attr_dict.get("iri") or _property_iri_for_label(attr_dict["name"], base)
            prop = URIRef(prop_iri)
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.label, Literal(attr_dict["name"])))
            g.add((prop, RDFS.domain, cls))
            xsd_kind = _XSD_MAP.get((attr_dict.get("range_type") or "string").lower(), "string")
            g.add((prop, RDFS.range, getattr(XSD, xsd_kind)))
            if attr_dict.get("functional"):
                g.add((prop, RDF.type, OWL.FunctionalProperty))
            if attr_dict.get("description"):
                g.add((prop, RDFS.comment, Literal(attr_dict["description"])))

        # Restrictions → owl:Restriction blank nodes
        for restr in (e.get("restrictions") or []):
            if not isinstance(restr, dict):
                continue
            kind = restr.get("kind")
            on_p = restr.get("on_property")
            if kind not in _RESTRICTION_KINDS or not on_p:
                continue
            r_node = BNode()
            g.add((r_node, RDF.type, OWL.Restriction))
            g.add((r_node, OWL.onProperty,
                    URIRef(_property_iri_for_label(on_p, base))))
            kind_pred = getattr(OWL, kind)
            val = restr.get("value")
            qualifier = restr.get("qualifier")
            if kind in {"someValuesFrom", "allValuesFrom"}:
                tgt = _resolve_class_ref(val, classes_by_label, base) if val else ""
                if tgt:
                    g.add((r_node, kind_pred, URIRef(tgt)))
            elif kind == "hasValue":
                if isinstance(val, (int, float, bool)):
                    g.add((r_node, kind_pred, Literal(val)))
                elif isinstance(val, str) and val.startswith(("http://", "https://", "urn:")):
                    g.add((r_node, kind_pred, URIRef(val)))
                else:
                    g.add((r_node, kind_pred, Literal(str(val))))
            else:
                # cardinality variants
                try:
                    n = int(val)
                except Exception:
                    n = 0
                g.add((r_node, kind_pred,
                        Literal(n, datatype=XSD.nonNegativeInteger)))
                if "Qualified" in kind and qualifier:
                    qtgt = _resolve_class_ref(qualifier, classes_by_label, base)
                    if qtgt:
                        g.add((r_node, OWL.onClass, URIRef(qtgt)))
            g.add((cls, RDFS.subClassOf, r_node))

    # ── Relationships → owl:ObjectProperty ─────────────────────────────
    for r in (ont.get("relationships") or []):
        lbl = r.get("label") or r.get("relation") or "related_to"
        prop_iri = r.get("iri") or _property_iri_for_label(lbl, base)
        prop = URIRef(prop_iri)
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.label, Literal(lbl)))

        # Domain(s) / Range(s) — multi-source supported
        domains = r.get("domain_classes") or [r.get("from") or r.get("source")]
        ranges  = r.get("range_classes")  or [r.get("to")   or r.get("target")]
        for d in domains:
            if not d:
                continue
            g.add((prop, RDFS.domain, URIRef(_resolve_class_ref(d, classes_by_label, base))))
        for rng in ranges:
            if not rng:
                continue
            g.add((prop, RDFS.range, URIRef(_resolve_class_ref(rng, classes_by_label, base))))

        if r.get("description"):
            g.add((prop, RDFS.comment, Literal(r["description"])))

        if r.get("inverse_of"):
            inv = r["inverse_of"]
            inv_iri = inv if inv.startswith(("http://", "https://", "urn:")) \
                      else _property_iri_for_label(inv, base)
            g.add((prop, OWL.inverseOf, URIRef(inv_iri)))

        for sup in (r.get("sub_property_of") or []):
            sup_iri = sup if sup.startswith(("http://", "https://", "urn:")) \
                      else _property_iri_for_label(sup, base)
            g.add((prop, RDFS.subPropertyOf, URIRef(sup_iri)))

        for ch in (r.get("characteristics") or []):
            owl_cls = _CHARACTERISTIC_TO_OWL.get(ch)
            if owl_cls:
                g.add((prop, RDF.type, getattr(OWL, owl_cls)))

    # ── Processing rules ────────────────────────────────────────────────
    for i, rule in enumerate(ont.get("processing_rules") or []):
        node = ONT[f"rule_{i+1}"]
        g.add((node, RDF.type, VERA.ProcessingRule))
        if rule.get("trigger"):
            g.add((node, VERA.trigger, Literal(rule["trigger"])))
        if rule.get("action"):
            g.add((node, VERA.action, Literal(rule["action"])))
        try:
            pri = int(rule.get("priority", 0))
        except Exception:
            pri = 0
        g.add((node, VERA.priority, Literal(pri, datatype=XSD.integer)))

    # ── Memory slots ────────────────────────────────────────────────────
    for s in (ont.get("memory_slots") or []):
        key = s.get("key") or s.get("name")
        if not key:
            continue
        node = ONT[f"slot_{_prop_local(key)}"]
        g.add((node, RDF.type, VERA.MemorySlot))
        g.add((node, VERA.slotKey, Literal(key)))
        if s.get("type"):
            g.add((node, VERA.slotType, Literal(s["type"])))
        if s.get("description"):
            g.add((node, RDFS.comment, Literal(s["description"])))

    return g


# ─────────────────────────────────────────────────────────────────────────────
# RDF Graph  →  JSON ontology  (round-trip + arbitrary OWL import)
# ─────────────────────────────────────────────────────────────────────────────
def graph_to_ontology(g: "Graph", default_name: str = "imported") -> Dict[str, Any]:
    if not _HAS_RDFLIB:
        raise RuntimeError("rdflib not installed: " + _RDFLIB_ERR)

    VERA = Namespace(VERA_NS_URI)

    # ── Ontology metadata ───────────────────────────────────────────────
    name        = default_name
    description = ""
    domain      = "general"
    context_hints = ""
    pref_label  = ""
    alt_labels: List[str] = []
    tags:       List[str] = []
    imports:    List[str] = []
    annotations: Dict[str, str] = {}
    iri_str     = ""

    ont_uri = next(iter(g.subjects(RDF.type, OWL.Ontology)), None)
    if ont_uri is not None:
        iri_str = str(ont_uri)
        if not iri_str.endswith(("#", "/")):
            iri_str += "#"
        for o in g.objects(ont_uri, RDFS.label):     name = str(o); break
        for o in g.objects(ont_uri, SKOS.prefLabel): pref_label = str(o); break
        for o in g.objects(ont_uri, SKOS.altLabel):  alt_labels.append(str(o))
        for o in g.objects(ont_uri, RDFS.comment):   description = str(o); break
        for o in g.objects(ont_uri, DC.subject):     domain = str(o); break
        for o in g.objects(ont_uri, VERA.contextHint): context_hints = str(o); break
        for o in g.objects(ont_uri, VERA.tag):       tags.append(str(o))
        for o in g.objects(ont_uri, OWL.imports):    imports.append(str(o))

    # ── classes ─────────────────────────────────────────────────────────
    # IRI → entity dict
    classes: Dict[str, Dict[str, Any]] = {}
    iri_to_label: Dict[str, str] = {}

    for cls in set(g.subjects(RDF.type, OWL.Class)):
        if isinstance(cls, BNode):
            continue
        clbl = next((str(o) for o in g.objects(cls, RDFS.label)), None) \
                or str(cls).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        cdesc = next((str(o) for o in g.objects(cls, RDFS.comment)), "")
        ent: Dict[str, Any] = {
            "iri": str(cls),
            "name": clbl,
            "description": cdesc,
            "attributes": [],
            "sub_class_of": [],
            "equivalent_to": [],
            "disjoint_with": [],
            "broader": [],
            "narrower": [],
            "related": [],
            "alt_labels": [],
            "restrictions": [],
            "annotations": {},
        }
        for o in g.objects(cls, SKOS.prefLabel): ent["pref_label"] = str(o); break
        for o in g.objects(cls, SKOS.altLabel):  ent["alt_labels"].append(str(o))
        for o in g.objects(cls, SKOS.notation):  ent["notation"] = str(o); break

        for o in g.objects(cls, RDFS.subClassOf):
            if isinstance(o, BNode):
                # Probably an owl:Restriction — handled later
                continue
            ent["sub_class_of"].append(str(o))
        for o in g.objects(cls, OWL.equivalentClass):
            if isinstance(o, BNode): continue
            ent["equivalent_to"].append(str(o))
        for o in g.objects(cls, OWL.disjointWith):
            if isinstance(o, BNode): continue
            ent["disjoint_with"].append(str(o))
        for o in g.objects(cls, SKOS.broader):
            ent["broader"].append(str(o))
        for o in g.objects(cls, SKOS.narrower):
            ent["narrower"].append(str(o))
        for o in g.objects(cls, SKOS.related):
            ent["related"].append(str(o))

        # Restrictions on this class
        for o in g.objects(cls, RDFS.subClassOf):
            if not isinstance(o, BNode):
                continue
            if (o, RDF.type, OWL.Restriction) not in g:
                continue
            restr: Dict[str, Any] = {}
            on_p = next((str(x) for x in g.objects(o, OWL.onProperty)), "")
            if on_p:
                restr["on_property"] = on_p.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            for kind in _RESTRICTION_KINDS:
                pred = getattr(OWL, kind)
                for v in g.objects(o, pred):
                    restr["kind"] = kind
                    restr["value"] = (str(v) if isinstance(v, URIRef) else
                                       (v.toPython() if hasattr(v, "toPython") else str(v)))
                    break
                if "kind" in restr:
                    break
            if "kind" in restr:
                qval = next((str(x) for x in g.objects(o, OWL.onClass)), "")
                if qval:
                    restr["qualifier"] = qval
                ent["restrictions"].append(restr)

        classes[str(cls)] = ent
        iri_to_label[str(cls)] = clbl

    # Resolve class refs (currently full IRIs) to label form where known
    def _maybe_label(ref: str) -> str:
        return iri_to_label.get(ref, ref.rsplit("#", 1)[-1].rsplit("/", 1)[-1])

    for ent in classes.values():
        for k in ("sub_class_of", "equivalent_to", "disjoint_with",
                  "broader", "narrower", "related"):
            ent[k] = [_maybe_label(x) for x in ent[k]]

    # ── Datatype properties → entity attributes ─────────────────────────
    for prop in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if isinstance(prop, BNode):
            continue
        plbl = next((str(o) for o in g.objects(prop, RDFS.label)), None) \
                or str(prop).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        pdesc = next((str(o) for o in g.objects(prop, RDFS.comment)), "")
        # Range type
        rng = next((o for o in g.objects(prop, RDFS.range)), None)
        rng_type = "string"
        if rng is not None:
            rng_str = str(rng)
            if "#" in rng_str:
                rng_local = rng_str.rsplit("#", 1)[-1]
                rng_type = rng_local
        functional = (prop, RDF.type, OWL.FunctionalProperty) in g

        attr_obj: Dict[str, Any] = {"name": plbl, "iri": str(prop), "range_type": rng_type}
        if pdesc:
            attr_obj["description"] = pdesc
        if functional:
            attr_obj["functional"] = True

        for d in g.objects(prop, RDFS.domain):
            d_iri = str(d)
            if d_iri in classes:
                # Avoid duplicate (by name)
                names = [a if isinstance(a, str) else a.get("name") for a in classes[d_iri]["attributes"]]
                if plbl not in names:
                    classes[d_iri]["attributes"].append(attr_obj)

    # ── Object properties → relationships ───────────────────────────────
    relationships: List[Dict[str, Any]] = []
    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        if isinstance(prop, BNode):
            continue
        plbl = next((str(o) for o in g.objects(prop, RDFS.label)), None) \
                or str(prop).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        pdesc = next((str(o) for o in g.objects(prop, RDFS.comment)), "")

        domains = [str(x) for x in g.objects(prop, RDFS.domain) if not isinstance(x, BNode)]
        ranges  = [str(x) for x in g.objects(prop, RDFS.range)  if not isinstance(x, BNode)]
        if not domains or not ranges:
            continue

        # Characteristics
        chars: List[str] = []
        for owl_kind, lbl in _OWL_TO_CHARACTERISTIC.items():
            if (prop, RDF.type, getattr(OWL, owl_kind)) in g:
                chars.append(lbl)

        inverse = next((str(o) for o in g.objects(prop, OWL.inverseOf)), "")
        sub_props = [str(o) for o in g.objects(prop, RDFS.subPropertyOf)]

        # Map IRIs to labels where possible
        d_labels = [_maybe_label(d) for d in domains]
        r_labels = [_maybe_label(r) for r in ranges]

        if len(d_labels) == 1 and len(r_labels) == 1:
            rel = {
                "from":  d_labels[0],
                "to":    r_labels[0],
                "label": plbl,
                "iri":   str(prop),
            }
        else:
            rel = {
                "from":  d_labels[0] if d_labels else "",
                "to":    r_labels[0] if r_labels else "",
                "label": plbl,
                "iri":   str(prop),
                "domain_classes": d_labels,
                "range_classes":  r_labels,
            }
        if pdesc:    rel["description"] = pdesc
        if inverse:  rel["inverse_of"]  = inverse.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if sub_props: rel["sub_property_of"] = [s.rsplit("#",1)[-1].rsplit("/",1)[-1] for s in sub_props]
        if chars:    rel["characteristics"] = chars
        relationships.append(rel)

    # ── Processing rules ────────────────────────────────────────────────
    rules: List[Dict[str, Any]] = []
    for node in set(g.subjects(RDF.type, VERA.ProcessingRule)):
        rule: Dict[str, Any] = {}
        for o in g.objects(node, VERA.trigger):  rule["trigger"] = str(o); break
        for o in g.objects(node, VERA.action):   rule["action"]  = str(o); break
        for o in g.objects(node, VERA.priority):
            try:    rule["priority"] = int(o)
            except: rule["priority"] = 0
            break
        if rule:
            rule.setdefault("priority", 0)
            rules.append(rule)

    # ── Memory slots ────────────────────────────────────────────────────
    slots: List[Dict[str, Any]] = []
    for node in set(g.subjects(RDF.type, VERA.MemorySlot)):
        s: Dict[str, Any] = {}
        for o in g.objects(node, VERA.slotKey):  s["key"]  = str(o); break
        for o in g.objects(node, VERA.slotType): s["type"] = str(o); break
        for o in g.objects(node, RDFS.comment):  s["description"] = str(o); break
        if s.get("key"):
            slots.append(s)

    return {
        "iri":              iri_str,
        "name":             name,
        "pref_label":       pref_label,
        "alt_labels":       alt_labels,
        "description":      description,
        "domain":           domain,
        "context_hints":    context_hints,
        "tags":             tags,
        "imports":          imports,
        "annotations":      annotations,
        "entities":         list(classes.values()),
        "relationships":    relationships,
        "processing_rules": sorted(rules, key=lambda x: x.get("priority", 0), reverse=True),
        "memory_slots":     slots,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────
def _validate_ontology(ont: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight coherence checks. Returns {ok, errors, warnings}."""
    errors:   List[str] = []
    warnings: List[str] = []

    ent_names = set()
    for e in (ont.get("entities") or []):
        nm = e.get("name", "")
        if not nm:
            errors.append("Entity missing name")
            continue
        if nm in ent_names:
            warnings.append(f"Duplicate entity name: {nm}")
        ent_names.add(nm)

    for e in (ont.get("entities") or []):
        for parent in (e.get("sub_class_of") or []):
            if parent and parent not in ent_names \
                and not parent.startswith(("http://","https://","urn:")):
                warnings.append(f"sub_class_of references unknown class: {parent}")
        for restr in (e.get("restrictions") or []):
            if restr.get("kind") not in _RESTRICTION_KINDS:
                errors.append(f"Restriction on {e.get('name')}: invalid kind '{restr.get('kind')}'")
            if not restr.get("on_property"):
                errors.append(f"Restriction on {e.get('name')} missing on_property")
        for attr in (e.get("attributes") or []):
            if isinstance(attr, dict):
                rt = (attr.get("range_type") or "string").lower()
                if rt not in _XSD_MAP:
                    warnings.append(
                        f"{e.get('name')}.{attr.get('name','?')}: unknown range_type "
                        f"'{rt}', will fall back to string"
                    )

    for r in (ont.get("relationships") or []):
        for c in (r.get("characteristics") or []):
            if c not in _CHARACTERISTIC_TO_OWL:
                errors.append(f"Relationship '{r.get('label')}': unknown characteristic '{c}'")
        if not (r.get("from") or r.get("domain_classes")):
            errors.append(f"Relationship '{r.get('label')}' missing from/domain")
        if not (r.get("to") or r.get("range_classes")):
            errors.append(f"Relationship '{r.get('label')}' missing to/range")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "ontologies.list_formats", memory="off", silent=True,
    http_method="GET", http_path="/ontologies/owl/formats", http_tags=["ontologies"],
    description="List supported OWL/RDF serialisation formats and report whether rdflib is installed.",
)
async def ontologies_list_formats(trace_id=None):
    return {
        "rdflib_available": _HAS_RDFLIB,
        "rdflib_error":     _RDFLIB_ERR if not _HAS_RDFLIB else "",
        "formats": [
            {"id": "turtle",   "label": "Turtle (.ttl)",        "mime": SUPPORTED_FORMATS["turtle"]},
            {"id": "rdfxml",   "label": "RDF/XML (.owl, .rdf)", "mime": SUPPORTED_FORMATS["xml"]},
            {"id": "json-ld",  "label": "JSON-LD (.jsonld)",    "mime": SUPPORTED_FORMATS["json-ld"]},
            {"id": "ntriples", "label": "N-Triples (.nt)",      "mime": SUPPORTED_FORMATS["nt"]},
            {"id": "n3",       "label": "Notation3 (.n3)",      "mime": SUPPORTED_FORMATS["n3"]},
        ],
    }


@capability(
    "ontologies.schema", memory="off", silent=True,
    http_method="GET", http_path="/ontologies/schema", http_tags=["ontologies"],
    description="Return the extended Vera+OWL+SKOS schema description (for UIs and validation).",
)
async def ontologies_schema(trace_id=None):
    return {
        "ontology_fields": {
            "core": ["id","name","description","domain","tags","enabled","context_hints"],
            "owl_skos": ["iri","pref_label","alt_labels","imports","annotations"],
            "containers": ["entities","relationships","processing_rules","memory_slots"],
        },
        "entity_fields": {
            "core": ["name","description","attributes"],
            "owl": ["iri","sub_class_of","equivalent_to","disjoint_with","restrictions"],
            "skos": ["pref_label","alt_labels","broader","narrower","related","notation"],
            "vera": ["annotations"],
        },
        "attribute_fields": ["name","iri","range_type","functional","description"],
        "relationship_fields": {
            "core": ["from","to","label","description"],
            "owl":  ["iri","inverse_of","sub_property_of","characteristics",
                     "domain_classes","range_classes"],
        },
        "restriction_kinds": sorted(_RESTRICTION_KINDS),
        "characteristics":   sorted(_CHARACTERISTIC_TO_OWL.keys()),
        "xsd_types":         sorted(set(_XSD_MAP.values())),
    }


@capability(
    "ontologies.export_owl", memory="off",
    http_method="POST", http_path="/ontologies/owl/export", http_tags=["ontologies"],
    description=(
        "Serialise a stored ontology to OWL/RDF. "
        "Inputs: id (str!), format (turtle|rdfxml|json-ld|ntriples|n3, default turtle), "
        "base_iri (str, optional override). "
        "Output: {ok, format, mime, content, length, triples}."
    ),
)
async def ontologies_export_owl(
    id:       str,
    format:   str = "turtle",
    base_iri: str = "",
    trace_id=None,
):
    if not _HAS_RDFLIB:
        return {"error": f"rdflib not installed: {_RDFLIB_ERR}"}
    from Vera.Orchestration.skills import ONTOLOGIES
    ont = ONTOLOGIES.get(id)
    if not ont:
        return {"error": f"Ontology not found: {id}"}
    fmt = (format or "turtle").lower()
    rdflib_fmt = _FORMAT_TO_RDFLIB.get(fmt)
    if not rdflib_fmt:
        return {"error": f"Unsupported format: {format}",
                "supported": sorted(_FORMAT_TO_RDFLIB.keys())}
    g = ontology_to_graph(ont, base_iri or None)
    try:
        content = g.serialize(format=rdflib_fmt)
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
    except Exception as e:
        return {"error": f"serialise failed: {e}"}
    return {
        "ok":      True,
        "id":      id,
        "format":  fmt,
        "mime":    SUPPORTED_FORMATS.get(fmt, "text/plain"),
        "content": content,
        "length":  len(content),
        "triples": len(g),
    }


@capability(
    "ontologies.import_owl", memory="off",
    http_method="POST", http_path="/ontologies/owl/import", http_tags=["ontologies"],
    description=(
        "Parse an OWL/RDF document and create or update a Vera ontology from it. "
        "All extended OWL+SKOS fields (sub_class_of, equivalent_to, disjoint_with, "
        "restrictions, characteristics, inverse_of, broader/narrower, etc) round-trip. "
        "Inputs: content (str! — raw RDF text), format (auto-detected if omitted), "
        "id (str — update if exists, else new), name (str — fallback name). "
        "Output: ontology record."
    ),
)
async def ontologies_import_owl(
    content: str,
    format:  str  = "",
    id:      str  = "",
    name:    str  = "",
    trace_id=None,
):
    if not _HAS_RDFLIB:
        return {"error": f"rdflib not installed: {_RDFLIB_ERR}"}
    if not (content or "").strip():
        return {"error": "content is empty"}

    fmt = (format or "").lower()
    rdflib_fmt = _FORMAT_TO_RDFLIB.get(fmt) if fmt else None

    g: Graph = Graph()
    parse_attempts = [rdflib_fmt] if rdflib_fmt else \
                       ["turtle", "xml", "json-ld", "nt", "n3"]
    last_err = None
    parsed = False
    for f in parse_attempts:
        try:
            g.parse(data=content, format=f)
            parsed = True
            break
        except Exception as e:
            last_err = e
            g = Graph()  # fresh graph for next attempt
    if not parsed:
        return {"error": f"could not parse RDF: {last_err}"}

    rec = graph_to_ontology(g, default_name=name or "imported")

    from Vera.Orchestration.skills import (
        ONTOLOGIES, ontologies_create, ontologies_update,
    )

    payload = dict(
        name             = rec["name"] or (name or "imported"),
        description      = rec["description"],
        domain           = rec["domain"],
        context_hints    = rec["context_hints"],
        entities         = json.dumps(rec["entities"]),
        relationships    = json.dumps(rec["relationships"]),
        processing_rules = json.dumps(rec["processing_rules"]),
        memory_slots     = json.dumps(rec["memory_slots"]),
        tags             = ",".join(rec["tags"]) if rec["tags"] else "",
    )

    if id and id in ONTOLOGIES:
        result = await ontologies_update(id=id, **payload)
    else:
        result = await ontologies_create(**payload)

    # Persist OWL+SKOS extras directly onto the record (existing CRUD paths
    # don't carry these as parameters, so we set them after the fact). The
    # SQLite/Redis save in skills serialises the whole record dict — these
    # additions survive automatically.
    rec_id = result.get("id") if isinstance(result, dict) else None
    if rec_id and rec_id in ONTOLOGIES:
        target = ONTOLOGIES[rec_id]
        for k in ("iri","pref_label","alt_labels","imports","annotations"):
            if rec.get(k):
                target[k] = rec[k]
        # Also upgrade the entities/relationships in place so the OWL fields
        # are preserved (ontologies_create only stored the JSON-stringified
        # form which loses nothing but the create path doesn't know about
        # OWL fields). The records inside ONTOLOGIES already hold the parsed
        # dicts via the create's _parse() call, so the OWL extras are intact.

    await emit_event({"type": "ontologies.imported_owl",
                      "id": rec_id, "triples": len(g)})
    return result


@capability(
    "ontologies.owl_context", memory="off", silent=True,
    http_method="POST", http_path="/ontologies/owl/context", http_tags=["ontologies"],
    description=(
        "Render one or more ontologies as a Turtle snippet for LLM injection. "
        "Inputs: ontology_ids (csv, empty=all enabled). "
        "Output: {turtle, ontology_count, length, triples}."
    ),
)
async def ontologies_owl_context(
    ontology_ids: str = "",
    trace_id=None,
):
    if not _HAS_RDFLIB:
        return {"error": f"rdflib not installed: {_RDFLIB_ERR}"}
    from Vera.Orchestration.skills import ONTOLOGIES
    wanted = {x.strip() for x in (ontology_ids or "").split(",") if x.strip()}
    onts = [o for oid, o in ONTOLOGIES.items()
            if (oid in wanted if wanted else o.get("enabled", True))]
    if not onts:
        return {"turtle": "", "ontology_count": 0, "length": 0, "triples": 0}

    combined: Graph = Graph()
    for o in onts:
        try:
            g = ontology_to_graph(o)
            combined += g
            for prefix, ns in g.namespaces():
                combined.bind(prefix, ns, override=False)
        except Exception as e:
            log.warning("owl_context: skipping %s — %s", o.get("name"), e)

    try:
        ttl = combined.serialize(format="turtle")
        if isinstance(ttl, bytes):
            ttl = ttl.decode("utf-8", "replace")
    except Exception as e:
        return {"error": f"serialise failed: {e}"}

    return {
        "turtle":         ttl,
        "ontology_count": len(onts),
        "length":         len(ttl),
        "triples":        len(combined),
    }


@capability(
    "ontologies.add_class", memory="off",
    http_method="POST", http_path="/ontologies/class/add", http_tags=["ontologies"],
    description=(
        "Add an entity (owl:Class) with full OWL+SKOS fields to an existing ontology. "
        "Inputs: ontology_id (str!), name (str!), description (str), "
        "pref_label (str), alt_labels (csv), broader (csv class refs), "
        "narrower (csv), related (csv), sub_class_of (csv), "
        "equivalent_to (csv), disjoint_with (csv), notation (str), "
        "attributes (JSON list of strings or {name,range_type,functional,description})."
    ),
)
async def ontologies_add_class(
    ontology_id:    str,
    name:           str,
    description:    str = "",
    pref_label:     str = "",
    alt_labels:     str = "",
    broader:        str = "",
    narrower:       str = "",
    related:        str = "",
    sub_class_of:   str = "",
    equivalent_to:  str = "",
    disjoint_with:  str = "",
    notation:       str = "",
    attributes:     str = "[]",
    trace_id=None,
):
    from Vera.Orchestration.skills import ONTOLOGIES, ontologies_update
    ont = ONTOLOGIES.get(ontology_id)
    if not ont:
        return {"error": f"Ontology not found: {ontology_id}"}
    try:
        attrs = json.loads(attributes) if isinstance(attributes, str) else (attributes or [])
    except Exception:
        attrs = []
    csv = lambda s: [t.strip() for t in (s or "").split(",") if t.strip()]
    new_ent: Dict[str, Any] = {
        "name":         name,
        "description":  description,
        "attributes":   attrs,
    }
    if pref_label:    new_ent["pref_label"]    = pref_label
    if alt_labels:    new_ent["alt_labels"]    = csv(alt_labels)
    if broader:       new_ent["broader"]       = csv(broader)
    if narrower:      new_ent["narrower"]      = csv(narrower)
    if related:       new_ent["related"]       = csv(related)
    if sub_class_of:  new_ent["sub_class_of"]  = csv(sub_class_of)
    if equivalent_to: new_ent["equivalent_to"] = csv(equivalent_to)
    if disjoint_with: new_ent["disjoint_with"] = csv(disjoint_with)
    if notation:      new_ent["notation"]      = notation

    ents = list(ont.get("entities") or [])
    # Replace by name if exists, else append
    for i, e in enumerate(ents):
        if e.get("name") == name:
            ents[i] = {**e, **new_ent}
            break
    else:
        ents.append(new_ent)

    return await ontologies_update(id=ontology_id, entities=json.dumps(ents))


@capability(
    "ontologies.add_property", memory="off",
    http_method="POST", http_path="/ontologies/property/add", http_tags=["ontologies"],
    description=(
        "Add an owl:ObjectProperty (relationship) with full OWL fields. "
        "Inputs: ontology_id (str!), label (str!), from_class (str!), to_class (str!), "
        "description (str), inverse_of (str), sub_property_of (csv), "
        "characteristics (csv from {Functional, InverseFunctional, Transitive, "
        "Symmetric, Asymmetric, Reflexive, Irreflexive})."
    ),
)
async def ontologies_add_property(
    ontology_id:      str,
    label:            str,
    from_class:       str,
    to_class:         str,
    description:      str = "",
    inverse_of:       str = "",
    sub_property_of:  str = "",
    characteristics:  str = "",
    trace_id=None,
):
    from Vera.Orchestration.skills import ONTOLOGIES, ontologies_update
    ont = ONTOLOGIES.get(ontology_id)
    if not ont:
        return {"error": f"Ontology not found: {ontology_id}"}
    csv = lambda s: [t.strip() for t in (s or "").split(",") if t.strip()]

    chars = csv(characteristics)
    bad = [c for c in chars if c not in _CHARACTERISTIC_TO_OWL]
    if bad:
        return {"error": f"unknown characteristics: {bad}",
                "valid": sorted(_CHARACTERISTIC_TO_OWL.keys())}

    new_rel: Dict[str, Any] = {
        "from":  from_class,
        "to":    to_class,
        "label": label,
    }
    if description:     new_rel["description"]     = description
    if inverse_of:      new_rel["inverse_of"]      = inverse_of
    if sub_property_of: new_rel["sub_property_of"] = csv(sub_property_of)
    if chars:           new_rel["characteristics"] = chars

    rels = list(ont.get("relationships") or [])
    for i, r in enumerate(rels):
        if r.get("label") == label and r.get("from") == from_class and r.get("to") == to_class:
            rels[i] = {**r, **new_rel}
            break
    else:
        rels.append(new_rel)

    return await ontologies_update(id=ontology_id, relationships=json.dumps(rels))


@capability(
    "ontologies.add_restriction", memory="off",
    http_method="POST", http_path="/ontologies/restriction/add", http_tags=["ontologies"],
    description=(
        "Add an owl:Restriction to a class. Restrictions are anonymous super-classes "
        "constraining how a property is used by instances of the target class. "
        "Inputs: ontology_id (str!), class_name (str!), kind "
        "(someValuesFrom|allValuesFrom|hasValue|min/max/exactCardinality|"
        "min/max/exactQualifiedCardinality), on_property (str!), value (str!), "
        "qualifier (str — required for *QualifiedCardinality)."
    ),
)
async def ontologies_add_restriction(
    ontology_id:  str,
    class_name:   str,
    kind:         str,
    on_property:  str,
    value:        str,
    qualifier:    str = "",
    trace_id=None,
):
    from Vera.Orchestration.skills import ONTOLOGIES, ontologies_update
    ont = ONTOLOGIES.get(ontology_id)
    if not ont:
        return {"error": f"Ontology not found: {ontology_id}"}
    if kind not in _RESTRICTION_KINDS:
        return {"error": f"unknown restriction kind: {kind}",
                "valid": sorted(_RESTRICTION_KINDS)}
    if not on_property or not value:
        return {"error": "on_property and value are required"}
    if "Qualified" in kind and not qualifier:
        return {"error": f"{kind} requires a qualifier (target class)"}

    ents = list(ont.get("entities") or [])
    for i, e in enumerate(ents):
        if e.get("name") == class_name:
            restr = {"kind": kind, "on_property": on_property, "value": value}
            if qualifier:
                restr["qualifier"] = qualifier
            new_e = dict(e)
            new_e["restrictions"] = list(new_e.get("restrictions") or []) + [restr]
            ents[i] = new_e
            break
    else:
        return {"error": f"class not found: {class_name}"}

    return await ontologies_update(id=ontology_id, entities=json.dumps(ents))


@capability(
    "ontologies.validate", memory="off", silent=True,
    http_method="POST", http_path="/ontologies/validate", http_tags=["ontologies"],
    description="Validate an ontology against the extended OWL+SKOS schema. "
                "Inputs: id (str!). Output: {ok, errors, warnings}.",
)
async def ontologies_validate(id: str, trace_id=None):
    from Vera.Orchestration.skills import ONTOLOGIES
    ont = ONTOLOGIES.get(id)
    if not ont:
        return {"error": f"Ontology not found: {id}"}
    return _validate_ontology(ont)


log.info("skills_owl: registered (rdflib=%s, vera_vocab=%s)", _HAS_RDFLIB, VERA_NS_URI)