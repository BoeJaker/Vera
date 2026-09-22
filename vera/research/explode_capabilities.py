# -*- coding: utf-8 -*-
"""
Explode — a record, a slice of one, a few records, or a pasted passage, as a
STRUCTURED graph (shared-planning/nlp-graphs-code/EXPLODE.md §4, §7.1).

Loom stitches relations INTO the fabric; this reads one thing and hands back a
diagram, persisting nothing. The output is the Explode contract that
<vera-structgraph> (vera/ui/structgraph_element.js) draws:

    { kind: "prose", source: {record_id, ranges, label, text_hash, text},
      layout: {direction, mode}, layers: [...], groups: [paragraphs],
      cards: [entities, each with the SPAN of its first mention],
      edges: [relations, each with a RESOLUTION], assessments: [...] }

Every analysis is a LAYER: one tool, one pass, cards and/or edges and/or
assessments, each carrying `layer` and `by` (the tool that produced it), so two
engines disagreeing over one span show as two layers over the same text, never
as one merged guess. The layers registered here are the tools the estate
already has — the fabric's canonical NER (GLiNER / spaCy / heuristic, whichever
is active), its sentence-scoped typed relations, same-sentence co-occurrence,
the node tier's NER, language id and sentiment. Another tool is one
`register_layer(...)` call, and needs no renderer change.

Capabilities
    nlp.explode.prose    text | record_id | record_ids, ranges, mode, layers → the contract
    nlp.explode.layers   the registered layers and their defaults
    code.explode         text + lang | path / paths (repo files, one hop of imports) | record_id → the contract
                         (the extractor itself is vera/research/code_explode_core.py — pure, node-testable)

Rules the contract keeps (EXPLODE.md §3): no card without a span; every edge
carries its resolution ("exact" for a cued, typed relation; "heuristic" for a
weak or co-occurrence one); a fragment is marked partial.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("vera.explode")

try:
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability
    _CAP_AVAILABLE = True
except ImportError:  # pragma: no cover - tests import the pure helpers only
    CAPABILITY_REGISTRY = {}
    _CAP_AVAILABLE = False

    def capability(*_a, **_k):
        def deco(fn):
            return fn
        return deco


# ── the engines this module leans on (imported lazily: the fabric module is heavy) ───────────────
def _wa():
    import Vera.vera.fabric.fabric_web_acquisition as wa
    return wa


def _assess():
    """The scorer registry (assess_capabilities.py, loaded after this file): the loader's copy in the app, the
    package's in tests."""
    import sys
    mod = sys.modules.get("assess_capabilities")
    if mod is not None:
        return mod
    try:
        from Vera.vera.research import assess_capabilities as mod  # noqa: F811
    except ImportError:
        from vera.research import assess_capabilities as mod       # noqa: F811
    return mod


def _code_core():
    """The pure code extractor (vera/research/code_explode_core.py). This file is a `_module_files` entry point
    loaded flat, so the import is absolute — the app's package first, the test tree's second."""
    try:
        from Vera.vera.research import code_explode_core as core
    except ImportError:
        from vera.research import code_explode_core as core
    return core


def _sqlite_conn():
    from Vera.vera.fabric.data_fabric import _sqlite_conn as sc
    return sc()


async def _call_cap(name: str, **kw):
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return None
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return None
    kw.setdefault("trace_id", None)
    return await fn(**kw)


# ── text structure: paragraphs and sentences with offsets ─────────────────────────────────────────
_PARA_RX = re.compile(r"\n[ \t]*\n+")


def paragraphs_of(text: str, base: int = 0) -> List[Dict]:
    """Paragraph spans [{start, end, text}] in the TEXT's coordinates (+ base), blank-line separated;
    a text without blank lines is one paragraph. Leading / trailing whitespace of a paragraph is
    trimmed from its span so a caption never starts on a newline."""
    out: List[Dict] = []
    pos = 0
    for m in list(_PARA_RX.finditer(text)) + [None]:
        end = m.start() if m else len(text)
        seg = text[pos:end]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        if seg.strip():
            out.append({"start": base + pos + lead, "end": base + end - trail, "text": seg.strip()})
        pos = m.end() if m else end
    return out


def sentences_of(text: str, base: int = 0) -> List[Dict]:
    """Sentence spans via the fabric's own splitter (offsets preserved); a plain split when the
    fabric module is not importable (tests)."""
    try:
        spans = _wa()._split_sentences(text)
        return [{"start": base + s, "end": base + e, "text": seg} for s, e, seg in spans]
    except Exception:
        out, pos = [], 0
        for m in list(re.finditer(r"[.!?]+(?:\s+|$)|\n{2,}", text)) + [None]:
            end = m.end() if m else len(text)
            if text[pos:end].strip():
                out.append({"start": base + pos, "end": base + end, "text": text[pos:end]})
            pos = end
        return out


def _para_index(paras: List[Dict], pos: int) -> int:
    for i, p in enumerate(paras):
        if p["start"] <= pos < p["end"]:
            return i
    # a position in the gap between paragraphs belongs to the one before it
    for i in range(len(paras) - 1, -1, -1):
        if pos >= paras[i]["start"]:
            return i
    return 0


def _sent_index(sents: List[Dict], pos: int) -> int:
    for i, s in enumerate(sents):
        if s["start"] <= pos < s["end"]:
            return i
    return -1


def _eid(etype: str, norm: str) -> str:
    return "e:%s:%s" % (etype, hashlib.sha1((norm or "").encode()).hexdigest()[:10])


_KIND_ALIAS = {"organisation": "org", "organization": "org", "company": "org", "government agency": "org", "place": "location",
               "gpe": "location", "city": "location", "country": "location", "building": "location", "landmark": "location",
               "geographic feature": "location", "year": "date", "time": "date", "money": "amount", "currency": "amount",
               "creative work": "work", "book": "work", "film": "work", "song": "work", "game": "work", "work_of_art": "work",
               "software": "product", "programming language": "product", "device": "product", "technology": "product",
               "vehicle": "product", "job title": "role", "named_entity": "entity"}


def _kind(etype: str) -> str:
    t = (etype or "entity").lower()
    return _KIND_ALIAS.get(t, t)


# ── the layer registry ────────────────────────────────────────────────────────────────────────────
# A layer: {id, label, kind ('entity' | 'relation' | 'assessment'), by, where, default_on, fn}
# fn(ctx) is awaited with ctx = {text, base, paragraphs, sentences, cards, edges, ent_list, ...} and
# returns {cards: [...], edges: [...], assessments: [...], by?, where?} — anything missing is empty.
LAYERS: Dict[str, Dict] = {}
_ORDER: List[str] = []


def register_layer(id: str, label: str, kind: str, fn: Callable[[Dict], Awaitable[Dict]],
                   by: str = "", where: str = "", default_on: bool = True, needs: Optional[List[str]] = None):
    """Add (or replace) a layer. Layers run in registration order; one that `needs` another's
    output (`needs=["ner"]`) runs after it and is skipped, with a note, when it is off."""
    if id not in LAYERS:
        _ORDER.append(id)
    LAYERS[id] = {"id": id, "label": label, "kind": kind, "fn": fn, "by": by, "where": where,
                  "default_on": bool(default_on), "needs": list(needs or [])}
    return LAYERS[id]


def layer_list() -> List[Dict]:
    return [{k: v for k, v in LAYERS[i].items() if k != "fn"} for i in _ORDER]


# ── the built-in layers ───────────────────────────────────────────────────────────────────────────
def _entity_cards(ent_list: List[Dict], ctx: Dict, layer: str, by: str) -> List[Dict]:
    """Entity dicts {name, type, normalised, position, mention_count, confidence} → cards, one per
    normalised entity, standing in the paragraph of its FIRST mention with that mention's span."""
    cards, seen = [], set()
    for e in ent_list:
        name = (e.get("name") or "").strip()
        norm = e.get("normalised") or name.lower()
        if not name or not norm:
            continue
        etype = _kind(e.get("type"))
        cid = _eid(etype, norm) + (":" + layer.split(".")[-1] if layer != "ner" else "")
        if cid in seen:
            continue
        seen.add(cid)
        pos = int(e.get("position", 0) or 0) + ctx["base"]
        n = int(e.get("mention_count", 1) or 1)
        pi = _para_index(ctx["paragraphs"], pos)
        # a "name" that runs across a paragraph break is the fallback engine's capitalised-phrase detector
        # reading past a blank line ("Summary\n\nSeptember"): not an entity
        if ctx["paragraphs"] and pos + len(name) > ctx["paragraphs"][pi]["end"]:
            continue
        card = {"id": cid, "group": ctx["paragraphs"][pi]["id"] if ctx["paragraphs"] else None,
                "layer": layer, "kind": etype, "title": name,
                "subtitle": "%s · %d mention%s" % (etype.upper(), n, "" if n == 1 else "s"),
                "span": {"path": ctx.get("path", ""), "start": pos, "end": pos + len(name)},
                "fields": [], "badges": [], "by": by}
        conf = e.get("confidence")
        if conf is not None:
            try:
                card["score"] = round(float(conf), 3)
            except Exception:
                pass
        if e.get("context"):
            card["fields"].append({"k": "context", "v": str(e["context"])[:80]})
        cards.append(card)
    return cards


async def _layer_ner_fabric(ctx: Dict) -> Dict:
    """The fabric's canonical NER — GLiNER, spaCy or the heuristic engine, whichever is active —
    run off the loop on the shared NER thread. Its raw list is kept on the ctx for the relation
    layers, which need positions."""
    wa = _wa()
    fn = getattr(wa, "_extract_entities_from_text", None)
    if fn is None:
        return {"error": "no canonical extractor"}
    st = {}
    try:
        st = wa._ner_backend() or {}
    except Exception:
        pass
    by = {"gliner": "GLiNER · " + str(__import__("os").getenv("FABRIC_GLINER_MODEL", "urchade/gliner_medium-v2.1")),
          "spacy": "spaCy", "heuristic": "heuristic patterns"}.get(st.get("kind", ""), st.get("kind", "fabric NER"))
    ents = await wa.ner_offload(fn, ctx["text"], ctx.get("content_type", "text")) or []
    ctx["ent_list"] = ents
    return {"cards": _entity_cards(ents, ctx, "ner", by), "by": by, "where": "host"}


def _relation_cards(ctx: Dict) -> List[Dict]:
    """The cards a relation stands between: the fabric's entities, plus the node tier's for the names the fabric
    did not find (a node card that MATCHES a fabric card is that entity already — the fabric card stands for it)."""
    matched = {e["from"] for e in ctx["edges"] if e.get("kind") == "MATCHES"}
    return [c for c in ctx["cards"] if c.get("layer") == "ner" or (str(c.get("layer", "")).startswith("ner.") and c["id"] not in matched)]


async def _layer_rel_typed(ctx: Dict) -> Dict:
    """Sentence-scoped typed relations between adjacent entities (a cue between them → typed and
    exact; a DATED / RELATED_TO without a cue → heuristic), over every engine's entities."""
    ents = ctx.get("ent_list") or []
    if not ents:
        return {"edges": []}
    fn = getattr(_wa(), "_extract_relationships_from_entities", None)
    if fn is None:
        return {"error": "no relation extractor"}
    rels = await asyncio.get_running_loop().run_in_executor(None, fn, ents, ctx["text"]) or []
    by_norm: Dict[str, str] = {}
    for c in _relation_cards(ctx):
        by_norm.setdefault(c["_norm"], c["id"])
    edges = []
    for r in rels:
        a = by_norm.get(((r.get("from_name") or "").lower()))
        b = by_norm.get(((r.get("to_name") or "").lower()))
        if not a or not b or a == b:
            continue
        rel = str(r.get("rel") or "RELATED_TO")
        cued = bool(r.get("cue")) and rel not in ("RELATED_TO", "DATED", "CO_OCCURS", "MENTIONED_WITH")
        # a cue between ADJACENT entities can pair the wrong two ("on Tuesday in Bristol" → Tuesday located in
        # Bristol): a date or an amount is never the thing located, founded or led — such a run is a guess
        if cued and _kind(r.get("from_type")) in ("date", "amount") and rel in ("LOCATED_IN", "FOUNDED", "LEADS", "WORKS_FOR", "BASED_IN"):
            cued = False
        edges.append({"from": a, "to": b, "layer": "rel.typed", "kind": "RELATES",
                      "label": rel.lower().replace("_", " "),
                      "resolution": "exact" if cued else "heuristic",
                      "score": r.get("score"), "cue": r.get("cue", ""), "by": "sentence cues"})
    return {"edges": edges, "by": "sentence-scoped cues", "where": "host"}


async def _layer_rel_cooccur(ctx: Dict) -> Dict:
    """Two entities first mentioned in ONE sentence, not already related by the typed layer: a
    CO_OCCURS run, heuristic by definition. Keeps the diagram readable when the cue engine finds
    little, without claiming more than it knows."""
    cards = _relation_cards(ctx)
    have = {(e["from"], e["to"]) for e in ctx["edges"]} | {(e["to"], e["from"]) for e in ctx["edges"]}
    by_sent: Dict[int, List[Dict]] = {}
    for c in cards:
        si = _sent_index(ctx["sentences"], c["span"]["start"])
        if si >= 0:
            by_sent.setdefault(si, []).append(c)
    edges = []
    for si, cs in by_sent.items():
        cs = sorted(cs, key=lambda c: c["span"]["start"])
        for i in range(len(cs) - 1):
            a, b = cs[i], cs[i + 1]
            if (a["id"], b["id"]) in have:
                continue
            edges.append({"from": a["id"], "to": b["id"], "layer": "rel.cooccur", "kind": "CO_OCCURS",
                          "resolution": "heuristic", "label": "in one sentence", "by": "same sentence"})
            if len(edges) >= 120:
                break
    return {"edges": edges, "by": "same sentence", "where": "host"}


_NODE_NER_DROP = {"CARDINAL", "ORDINAL", "PERCENT", "QUANTITY"}   # what the fabric's spaCy path drops too
_NODE_NER_KIND = {"PERSON": "person", "PER": "person", "ORG": "org", "GPE": "location", "LOC": "location",
                  "FAC": "location", "DATE": "date", "TIME": "date", "EVENT": "event", "MONEY": "amount",
                  "PRODUCT": "product", "WORK_OF_ART": "work", "LAW": "law", "NORP": "group", "LANGUAGE": "language"}


async def _node_ner(ctx: Dict, task: str, layer: str, suffix: str, label: str, **kw) -> Dict:
    """One of the node tier's NER engines (nlp.ner task=…) over the same text, as cards on its own layer,
    each MATCHED to the fabric entity whose span it overlaps — so where the engines disagree, the disagreement
    is visible; an exact-span match at high confidence retypes a generic fabric entity."""
    res = await _call_cap("nlp.ner", text=ctx["text"], task=task, **kw)
    if not res or res.get("error"):
        return {"error": (res or {}).get("error", "nlp.ner unavailable"), "cards": [], "edges": []}
    fabric = [c for c in ctx["cards"] if c.get("layer") == "ner"]
    cards, edges, seen = [], [], {}
    for e in res.get("entities") or []:
        word = (e.get("word") or "").strip()
        if not word or e.get("start") is None:
            continue
        label = str(e.get("entity") or "").upper()
        if label in _NODE_NER_DROP:            # a bare number or a percentage is not a thing the diagram is about
            continue
        # markdown punctuation the model kept at either end ("**Claude Fable 5.1") is not part of the name: the
        # span moves with the trim so it still points at the name in the text
        lead = len(word) - len(word.lstrip("*#_`>[("))
        word = word.strip("*#_`>[](),.;:")
        if not word:
            continue
        etype = _NODE_NER_KIND.get(label, _kind(label or "entity"))
        norm = word.lower()
        key = (etype, norm)
        start = int(e["start"]) + lead + ctx["base"]
        if key in seen:
            seen[key]["_n"] += 1
            continue
        pi = _para_index(ctx["paragraphs"], start)
        card = {"id": _eid(etype, norm) + ":" + suffix, "group": ctx["paragraphs"][pi]["id"] if ctx["paragraphs"] else None,
                "layer": layer, "kind": etype, "title": word, "subtitle": etype.upper() + " · " + label,
                "span": {"path": ctx.get("path", ""), "start": start, "end": start + len(word)},
                "fields": [], "badges": [], "score": round(float(e.get("score") or 0), 3), "by": res.get("model", "nlp.ner"), "_n": 1}
        seen[key] = card
        cards.append(card)
        for f in fabric:
            fs = f["span"]
            if fs["start"] < card["span"]["end"] and card["span"]["start"] < fs["end"]:
                same_span = fs["start"] == card["span"]["start"] and fs["end"] == card["span"]["end"]
                # the fabric's fallback engines type a name as a bare "entity"; a node model that read the very same
                # span at high confidence knows what it is — the card takes that type, and says who typed it (the
                # correction the LLM pass makes at ingest, made here by the node tier for the price of one call)
                if same_span and f["kind"] in ("entity", "named_entity") and etype not in ("entity",) and card["score"] >= 0.9:
                    f["kind"] = etype
                    f["subtitle"] = re.sub(r"^[A-Z_]+ ·", etype.upper() + " ·", f["subtitle"])
                    f["by"] = (f.get("by") or "") + " · typed by " + str(res.get("model", "node NER"))
                    f["badges"] = list(f.get("badges") or []) + ["retyped"]
                    for ent in ctx.get("ent_list") or []:
                        if int(ent.get("position", 0) or 0) + ctx["base"] == fs["start"] and (ent.get("name") or "") == f["title"]:
                            ent["type"] = etype
                    edges.append({"from": card["id"], "to": f["id"], "layer": layer, "kind": "MATCHES", "resolution": "exact",
                                  "label": "the same span · typed " + etype + " here", "by": "span match"})
                else:
                    edges.append({"from": card["id"], "to": f["id"], "layer": layer, "kind": "MATCHES",
                                  "resolution": "heuristic" if f["kind"] != etype else "exact",
                                  "label": "the same span" + ("" if f["kind"] == etype else " · typed " + f["kind"] + " there"), "by": "span overlap"})
                break
    for c in cards:
        n = c.pop("_n", 1)
        c["subtitle"] = "%s · %d mention%s · %s" % (c["kind"].upper(), n, "" if n == 1 else "s", label)
    # the node's entities join the list the relation layers read — positions in the TEXT's coordinates, like the
    # fabric's — for the names the fabric did not find at all (a date, an amount, a weekday), so relations to them exist
    have = {(int(e.get("position", 0) or 0), (e.get("name") or "").lower()) for e in ctx.get("ent_list") or []}
    for c in cards:
        pos = c["span"]["start"] - ctx["base"]
        if (pos, c["title"].lower()) in have or any(abs(p - pos) < 2 and nm == c["title"].lower() for p, nm in have):
            continue
        ctx.setdefault("ent_list", []).append({"name": c["title"], "type": c["kind"], "normalised": c["title"].lower(), "position": pos,
                                               "mention_count": 1, "confidence": c["score"], "_layer": layer})
    return {"cards": cards, "edges": edges, "by": res.get("model", "nlp.ner"), "where": res.get("node") or res.get("served_by") or "node tier"}




async def _layer_ner_node(ctx: Dict) -> Dict:
    return await _node_ner(ctx, "ner", "ner.node", "node", "node NER")


async def _layer_ner_multi(ctx: Dict) -> Dict:
    return await _node_ner(ctx, "ner_multi", "ner.multi", "multi", "multilingual NER")


async def _layer_ner_gliner_node(ctx: Dict) -> Dict:
    """The fabric's own zero-shot engine, run on a node over the fabric's label set (the host's GLiNER is the
    same model; on a host without it — a mirror, a sandbox — this is the only GLiNER there is)."""
    return await _node_ner(ctx, "gliner", "ner.gliner", "gliner", "GLiNER · node", labels=list(_wa()._GLINER_LABELS_DEFAULT) if _has_wa() else None)


async def _layer_ner_spacy_node(ctx: Dict) -> Dict:
    return await _node_ner(ctx, "spacy", "ner.spacy", "spacy", "spaCy · node")


def _has_wa() -> bool:
    try:
        _wa(); return True
    except Exception:
        return False


_GENRES = ["news report", "opinion or commentary", "advertisement or marketing", "technical documentation",
           "academic or research writing", "fiction or narrative", "instructions or how-to", "conversation or chat log"]


async def _layer_genre(ctx: Dict) -> Dict:
    """What kind of text this is — zero-shot on the node tier over a fixed set of genres; a verdict, not a card.
    The labels are in the receipt so a caller can read what was asked."""
    res = await _call_cap("nlp.zeroshot", text=ctx["text"][:1500], labels=_GENRES, multi_label=False)
    if not res or res.get("error"):
        return {"error": (res or {}).get("error", "nlp.zeroshot unavailable")}
    top = (res.get("labels") or [{}])[0]
    return {"assessments": [{"key": "genre", "label": "genre · " + str(top.get("label") or "?"), "score": round(float(top.get("score") or 0), 3),
                             "confidence": round(float(top.get("score") or 0), 3), "by": "nlp.zeroshot · " + str(res.get("model") or ""), "on": "source",
                             "evidence": [], "detail": {l.get("label"): round(float(l.get("score") or 0), 3) for l in (res.get("labels") or [])[:4]}}],
            "where": res.get("node") or "node tier"}


_QUESTIONS = [("who", "Who is this about?"), ("what", "What happened?"), ("when", "When did it happen?"), ("where", "Where did it happen?")]


async def _layer_claims(ctx: Dict) -> Dict:
    """Extractive QA on the node tier: who / what / when / where, per paragraph — each answer a CLAIM card standing at
    its answer span, with the question it answers as a field. Off by default: four node calls per paragraph."""
    cards = []
    for p in ctx["paragraphs"][:12]:
        for key, q in _QUESTIONS:
            res = await _call_cap("nlp.qa", question=q, context=p["text"][:2000])
            if not res or res.get("error"):
                return {"error": (res or {}).get("error", "nlp.qa unavailable"), "cards": cards}
            ans = (res.get("answer") or "").strip()
            if not ans or res.get("start") is None or float(res.get("score") or 0) < 0.15:
                continue
            start = p["start"] + int(res["start"])
            cards.append({"id": "claim:%s:%s" % (p["id"], key), "group": p["id"], "layer": "qa.claims", "kind": "claim", "title": ans[:80],
                          "subtitle": "CLAIM · " + key, "span": {"path": ctx.get("path", ""), "start": start, "end": start + len(ans)},
                          "fields": [{"k": "question", "v": q}], "badges": [], "score": round(float(res.get("score") or 0), 3), "by": res.get("model", "nlp.qa")})
    return {"cards": cards, "by": "nlp.qa", "where": "node tier"}


async def _layer_paragraphs(ctx: Dict) -> Dict:
    """Every paragraph as a card of its own — the narrative's units — so similarity between paragraphs has ends to
    run between. Off by default (the plates already show the paragraphs)."""
    cards = []
    for p in ctx["paragraphs"]:
        head = re.sub(r"\s+", " ", p["text"])[:60]
        cards.append({"id": "para:" + p["id"], "group": p["id"], "layer": "paragraphs", "kind": "paragraph", "title": head + ("…" if len(p["text"]) > 60 else ""),
                      "subtitle": "paragraph · %d words" % len(p["text"].split()), "span": {"path": ctx.get("path", ""), "start": p["start"], "end": p["end"]},
                      "fields": [], "badges": [], "by": "paragraphs_of"})
    return {"cards": cards, "by": "paragraphs_of", "where": "host"}


async def _layer_sim_embed(ctx: Dict) -> Dict:
    """Which paragraphs say the same thing: sentence embeddings from the node tier (CPU, never GPU-routed), cosine
    similarity, a MATCHES run between the paragraph cards above a threshold. Needs the paragraphs layer."""
    paras = [c for c in ctx["cards"] if c.get("layer") == "paragraphs"]
    if len(paras) < 2:
        return {"edges": []}
    res = await _call_cap("nlp.embed", texts=[ctx["text"][c["span"]["start"] - ctx["base"]:c["span"]["end"] - ctx["base"]][:1500] for c in paras])
    if not res or res.get("error"):
        return {"error": (res or {}).get("error", "nlp.embed unavailable")}
    vecs = res.get("embeddings") or []
    edges = []
    for i in range(len(paras)):
        for j in range(i + 1, len(paras)):
            if i >= len(vecs) or j >= len(vecs):
                continue
            sim = sum(a * b for a, b in zip(vecs[i], vecs[j]))
            if sim >= 0.6:
                edges.append({"from": paras[i]["id"], "to": paras[j]["id"], "layer": "sim.embed", "kind": "MATCHES", "resolution": "exact" if sim >= 0.8 else "heuristic",
                              "label": "similar · %.2f" % sim, "score": round(sim, 3), "by": res.get("model", "nlp.embed")})
    return {"edges": edges, "by": res.get("model", "nlp.embed"), "where": res.get("node") or "node tier"}


async def _layer_langid(ctx: Dict) -> Dict:
    res = await _call_cap("nlp.langid", text=ctx["text"][:2000])
    if not res or res.get("error"):
        return {"error": (res or {}).get("error", "nlp.langid unavailable")}
    top = (res.get("langs") or [{}])[0]
    return {"assessments": [{"key": "lang", "label": "language · " + str(res.get("lang") or top.get("lang") or "?"),
                             "score": round(float(top.get("score") or 0), 3), "confidence": round(float(top.get("score") or 0), 3),
                             "by": "nlp.langid", "on": "source"}], "where": res.get("node") or "node tier"}


async def _layer_sentiment(ctx: Dict) -> Dict:
    """Sentiment per paragraph (nlp.classify), rolled up to the source: the score is the share of
    positive, the evidence the paragraphs' own readings."""
    paras = ctx["paragraphs"]
    if not paras:
        return {}
    outs = []
    for p in paras[:24]:
        res = await _call_cap("nlp.classify", text=p["text"][:1500])
        if not res or res.get("error"):
            return {"error": (res or {}).get("error", "nlp.classify unavailable")}
        labels = {str(l.get("label", "")).upper(): float(l.get("score") or 0) for l in (res.get("labels") or [])}
        pos = labels.get("POSITIVE", labels.get("LABEL_1", 0.0))
        outs.append({"span": {"path": ctx.get("path", ""), "start": p["start"], "end": p["end"]}, "note": "%s %.2f" % (res.get("top", ""), pos), "score": pos})
    mean = sum(o["score"] for o in outs) / len(outs)
    return {"assessments": [{"key": "sentiment", "label": "positive", "score": round(mean, 3), "confidence": 0.7,
                             "by": "nlp.classify · sst-2", "on": "source", "evidence": [{"span": o["span"], "note": o["note"]} for o in outs]}],
            "where": "node tier"}


register_layer("ner", "entities · fabric NER", "entity", _layer_ner_fabric, by="GLiNER / spaCy / heuristic", where="host")
register_layer("ner.node", "entities · node NER", "entity", _layer_ner_node, by="nlp.ner", where="node tier", needs=["ner"])
register_layer("ner.gliner", "entities · GLiNER (node)", "entity", _layer_ner_gliner_node, by="nlp.ner gliner", where="node tier", default_on=False, needs=["ner"])
register_layer("ner.spacy", "entities · spaCy (node)", "entity", _layer_ner_spacy_node, by="nlp.ner spacy", where="node tier", default_on=False, needs=["ner"])
register_layer("ner.multi", "entities · multilingual NER", "entity", _layer_ner_multi, by="nlp.ner ner_multi", where="node tier", default_on=False, needs=["ner"])
register_layer("rel.typed", "relations · typed", "relation", _layer_rel_typed, by="sentence cues", where="host", needs=["ner"])
register_layer("rel.cooccur", "co-occurrence", "relation", _layer_rel_cooccur, by="same sentence", where="host", needs=["ner"])
register_layer("langid", "language", "assessment", _layer_langid, by="nlp.langid", where="node tier", default_on=False)
register_layer("cls.sentiment", "sentiment", "assessment", _layer_sentiment, by="nlp.classify", where="node tier", default_on=False)
register_layer("cls.genre", "genre", "assessment", _layer_genre, by="nlp.zeroshot", where="node tier", default_on=False)
register_layer("qa.claims", "claims · who / what / when / where", "entity", _layer_claims, by="nlp.qa", where="node tier", default_on=False)
register_layer("paragraphs", "paragraphs as cards", "entity", _layer_paragraphs, by="paragraphs_of", where="host", default_on=False)
register_layer("sim.embed", "similar paragraphs", "relation", _layer_sim_embed, by="nlp.embed", where="node tier", default_on=False, needs=["paragraphs"])


# ── assembling the contract ───────────────────────────────────────────────────────────────────────
def _read_record(record_id: str) -> Optional[Dict]:
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT id, dataset_id, text, source_id, tags FROM fabric_records WHERE id=? LIMIT 1", (record_id,)).fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row:
        return None
    keys = ["id", "dataset_id", "text", "source_id", "tags"]
    rec = dict(zip(keys, row)) if not hasattr(row, "keys") else {k: row[k] for k in keys}
    return rec


def _ranges_of(ranges, n: int) -> List[List[int]]:
    out = []
    for r in ranges or []:
        try:
            s, e = int(r[0]), int(r[1])
        except Exception:
            continue
        s, e = max(0, min(n, s)), max(0, min(n, e))
        if e > s:
            out.append([s, e])
    return out


async def explode_text(text: str, *, path: str = "", base: int = 0, layers: Optional[List[str]] = None,
                       content_type: str = "text", group_prefix: str = "", para_offset: int = 0) -> Dict:
    """Run the chosen layers over ONE text (a record, a slice, a passage) and return its groups,
    cards, edges, assessments and the layers' receipts. `base` is where this text starts in its
    record, so every span is in the record's coordinates."""
    paras = paragraphs_of(text, base)
    for i, p in enumerate(paras):
        p["id"] = "%sp%d" % (group_prefix, para_offset + i + 1)
        p["label"] = "¶ %d · %s" % (para_offset + i + 1, p["text"][:48].replace("\n", " ") + ("…" if len(p["text"]) > 48 else ""))
    ctx: Dict[str, Any] = {"text": text, "base": base, "path": path, "content_type": content_type,
                           "paragraphs": paras, "sentences": sentences_of(text, base),
                           "cards": [], "edges": [], "assessments": [], "ent_list": []}
    wanted = [l for l in _ORDER if (layers is None and LAYERS[l]["default_on"]) or (layers is not None and l in layers)]
    receipts = []
    for lid in _ORDER:
        L = LAYERS[lid]
        rec = {"id": lid, "label": L["label"], "kind": L["kind"], "by": L["by"], "where": L["where"], "on": lid in wanted, "count": 0, "ms": 0}
        if lid not in wanted:
            receipts.append(rec)
            continue
        missing = [n for n in L["needs"] if n not in wanted]
        if missing:
            rec.update(on=False, error="needs " + ", ".join(missing))
            receipts.append(rec)
            continue
        t0 = time.monotonic()
        try:
            out = await L["fn"](ctx) or {}
        except Exception as e:  # a layer's failure is its own receipt, never the diagram's
            log.warning("explode layer %s: %s", lid, e)
            out = {"error": str(e)[:200]}
        rec["ms"] = int((time.monotonic() - t0) * 1000)
        if out.get("by"):
            rec["by"] = out["by"]
        if out.get("where"):
            rec["where"] = out["where"]
        if out.get("error"):
            rec["error"] = str(out["error"])[:200]
        cards = out.get("cards") or []
        for c in cards:
            c["_norm"] = (c.get("title") or "").lower()
        ctx["cards"].extend(cards)
        ctx["edges"].extend(out.get("edges") or [])
        ctx["assessments"].extend(out.get("assessments") or [])
        rec["count"] = len(cards) + len(out.get("edges") or []) + len(out.get("assessments") or [])
        receipts.append(rec)
    for c in ctx["cards"]:
        c.pop("_norm", None)
    groups = [{"id": p["id"], "label": p["label"], "kind": "paragraph", "parent": None,
               "span": {"path": path, "start": p["start"], "end": p["end"]}} for p in paras]
    return {"groups": groups, "cards": ctx["cards"], "edges": ctx["edges"], "assessments": ctx["assessments"], "layers": receipts,
            "paragraphs": len(paras), "sentences": len(ctx["sentences"])}


def _assess_arg(assess) -> Optional[List[str]]:
    """assess=False → None (do not run); True → [] (the default scorers); a list / comma string → those keys."""
    if assess is None or assess is False or assess == "" or assess == "false":
        return None
    if assess is True or assess == "true" or assess == "default":
        return []
    if isinstance(assess, str):
        return [s.strip() for s in assess.split(",") if s.strip()]
    return list(assess)


async def explode_prose(text: str = "", record_id: str = "", record_ids: Optional[List[str]] = None,
                        ranges: Optional[List] = None, mode: str = "", layers: Optional[List[str]] = None,
                        content_type: str = "text", include_text: bool = True, max_chars: int = 60000, assess=None) -> Dict:
    """The contract for a passage, a record, a slice of a record, or several records (as lanes). `assess` runs the
    prose scorers (True: the defaults; a list: those) and appends their assessments and receipts."""
    if isinstance(layers, str):
        layers = [s.strip() for s in layers.split(",") if s.strip()] or None
    ids = [r for r in (record_ids or []) if r] or ([record_id] if record_id else [])
    loop = asyncio.get_running_loop()
    lanes: List[Dict] = []          # {id, label, text, base, ranges}
    if ids:
        for rid in ids:
            rec = await loop.run_in_executor(None, _read_record, rid)
            if not rec:
                return {"error": "record %s not found - if this is a graph node or a path, ask "
                                 "explode.target what it is and it will say how it explodes" % rid}
            t = (rec.get("text") or "")
            if len(t) > max_chars:
                t = t[:max_chars]
            lanes.append({"id": rid, "label": (t.strip().split("\n", 1)[0][:60] or rid), "text": t, "dataset_id": rec.get("dataset_id", ""), "source_id": rec.get("source_id", "")})
    elif text:
        lanes.append({"id": "pasted", "label": "pasted passage", "text": text[:max_chars]})
    else:
        return {"error": "text, record_id or record_ids required"}

    rng = _ranges_of(ranges, len(lanes[0]["text"])) if len(lanes) == 1 else []
    partial = bool(rng) or (len(lanes) == 1 and lanes[0]["id"] == "pasted")
    if not mode:
        mode = "position" if (rng or lanes[0]["id"] == "pasted") else "type"
    if mode not in ("position", "type"):
        mode = "position"

    groups: List[Dict] = []
    cards: List[Dict] = []
    edges: List[Dict] = []
    assessments: List[Dict] = []
    receipts: Dict[str, Dict] = {}
    paras_total = 0
    multi = len(lanes) > 1

    async def run(lane: Dict, seg_text: str, base: int, prefix: str, parent: Optional[str]):
        nonlocal paras_total
        out = await explode_text(seg_text, path=lane["id"], base=base, layers=layers, content_type=content_type,
                                 group_prefix=prefix, para_offset=paras_total)
        paras_total += out["paragraphs"]
        for g in out["groups"]:
            g["parent"] = parent
        groups.extend(out["groups"]); cards.extend(out["cards"]); edges.extend(out["edges"]); assessments.extend(out["assessments"])
        for r in out["layers"]:          # one receipt per layer across every lane and range: counts and time add up
            acc = receipts.get(r["id"])
            if acc is None:
                receipts[r["id"]] = dict(r)
                continue
            acc["count"] = acc.get("count", 0) + r.get("count", 0)
            acc["ms"] = acc.get("ms", 0) + r.get("ms", 0)
            if r.get("error") and not acc.get("error"):
                acc["error"] = r["error"]

    for li, lane in enumerate(lanes):
        parent = None
        if multi:
            parent = "rec:" + lane["id"]
            groups.append({"id": parent, "label": lane["label"], "kind": "record", "parent": None,
                           "span": {"path": lane["id"], "start": 0, "end": len(lane["text"])}})
        prefix = ("r%d." % (li + 1)) if multi else ""
        if rng:
            for ri, (s, e) in enumerate(rng):
                gid = "range%d" % (ri + 1)
                if len(rng) > 1:
                    groups.append({"id": gid, "label": "%d–%d" % (s, e), "kind": "range", "parent": parent,
                                   "span": {"path": lane["id"], "start": s, "end": e}})
                await run(lane, lane["text"][s:e], s, prefix + ("g%d." % (ri + 1) if len(rng) > 1 else ""), gid if len(rng) > 1 else parent)
        else:
            await run(lane, lane["text"], 0, prefix, parent)

    layer_rows = [receipts[i] for i in _ORDER if i in receipts]
    src = {"record_id": ids[0] if len(ids) == 1 else "", "record_ids": ids if multi else [],
           "ranges": rng, "partial": partial, "label": lanes[0]["label"] if not multi else "%d records" % len(lanes),
           "text_hash": hashlib.sha1("".join(l["text"] for l in lanes).encode("utf-8", "replace")).hexdigest()[:16],
           "chars": sum(len(l["text"]) for l in lanes)}
    if ids and not multi:
        src["dataset_id"] = lanes[0].get("dataset_id", ""); src["source_id"] = lanes[0].get("source_id", "")
    if include_text:
        src["text"] = lanes[0]["text"] if not multi else {l["id"]: l["text"] for l in lanes}
    want = _assess_arg(assess)
    if want is not None:
        # the scorers read the (first) lane's text — the slice when there is one — and see the layers' own
        # assessments (a language id) before composing trust
        one = rng[0] if len(rng) == 1 else None
        seg = lanes[0]["text"][one[0]:one[1]] if one else lanes[0]["text"]
        base = one[0] if one else 0
        paras = [{"start": p["span"]["start"] - base, "end": p["span"]["end"] - base} for p in groups if p["kind"] == "paragraph"]
        A = _assess()
        n_prior = len(assessments)
        res = await A.assess_prose_text(seg, paras, lanes[0]["id"], want or None, prior=assessments)
        for a in res["assessments"][n_prior:]:
            for ev in a.get("evidence") or []:   # the scorers' spans, back into the record's coordinates
                if base and ev.get("span") and ev["span"].get("path") == lanes[0]["id"]:
                    ev["span"]["start"] += base; ev["span"]["end"] += base
        assessments = res["assessments"]
        layer_rows += res["receipts"]
    return {"ok": True, "kind": "prose", "source": src, "layout": {"direction": "LR", "mode": mode},
            "layers": layer_rows, "groups": groups, "cards": cards, "edges": edges, "assessments": assessments,
            "counts": {"groups": len(groups), "cards": len(cards), "edges": len(edges), "assessments": len(assessments), "paragraphs": paras_total}}


# ── code: files of the repo, a snippet, a record — the pure extractor over what was read ───────────
_CODE_EXT = (".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".html", ".htm")


def _repo_root() -> str:
    import os
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _read_repo_files(paths: List[str], max_files: int, max_bytes: int) -> Dict:
    """Read repo-relative files (a directory lists its code files, non-recursively) — never outside the repo."""
    import os
    root = _repo_root()
    out, skipped, total = [], [], 0
    for rel in paths:
        rel = str(rel).replace("\\", "/").lstrip("/")
        full = os.path.abspath(os.path.join(root, rel))
        if not (full == root or full.startswith(root + os.sep)):
            skipped.append({"path": rel, "why": "outside the repo"})
            continue
        if os.path.isdir(full):
            for name in sorted(os.listdir(full)):
                if name.lower().endswith(_CODE_EXT) and os.path.isfile(os.path.join(full, name)):
                    paths.append(rel.rstrip("/") + "/" + name)
            continue
        if not os.path.isfile(full):
            skipped.append({"path": rel, "why": "not a file"})
            continue
        if len(out) >= max_files:
            skipped.append({"path": rel, "why": "max_files"})
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(max_bytes + 1)
        except Exception as e:
            skipped.append({"path": rel, "why": str(e)[:80]})
            continue
        if len(text) > max_bytes:
            skipped.append({"path": rel, "why": "truncated at %d chars" % max_bytes}); text = text[:max_bytes]
        total += len(text)
        out.append({"path": rel, "text": text})
    return {"sources": out, "skipped": skipped, "chars": total}


async def _external_lint(root: str, paths: List[str]) -> Dict[str, List[Dict]]:
    """ruff / eslint output when the host actually has them — mapped to the same finding shape. The image carries
    neither today (checked 2026-09-22), so this returns {} and the in-process rules stand alone; the moment a
    linter is installed its findings lead, marked `external linter`."""
    import json as _json
    import os as _os
    import shutil as _shutil
    try:
        from Vera.vera.execution.spawn_core import run_argv
    except ImportError:
        try:
            from vera.execution.spawn_core import run_argv
        except ImportError:
            return {}
    out: Dict[str, List[Dict]] = {}
    py = [p for p in paths if p.endswith((".py", ".pyi"))]
    if py and _shutil.which("ruff"):
        r = await run_argv(["ruff", "check", "--output-format", "json", "--"] + py, timeout=60, cwd=root)
        try:
            for f in _json.loads(r.get("stdout") or "[]"):
                rel = _os.path.relpath(f.get("filename", ""), root)
                out.setdefault(rel, []).append({"line": (f.get("location") or {}).get("row", 1), "col": (f.get("location") or {}).get("column", 0),
                                                "code": f.get("code") or "RUFF", "note": f.get("message", ""), "sev": "warn", "by": "ruff"})
        except Exception:
            pass
    js = [p for p in paths if p.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"))]
    if js and _shutil.which("eslint"):
        r = await run_argv(["eslint", "-f", "json", "--"] + js, timeout=60, cwd=root)
        try:
            for f in _json.loads(r.get("stdout") or "[]"):
                rel = _os.path.relpath(f.get("filePath", ""), root)
                for m in f.get("messages") or []:
                    out.setdefault(rel, []).append({"line": m.get("line", 1), "col": m.get("column", 0), "code": m.get("ruleId") or "ESLINT",
                                                    "note": m.get("message", ""), "sev": "error" if m.get("severity") == 2 else "warn", "by": "eslint"})
        except Exception:
            pass
    return out


async def explode_code(text: str = "", lang: str = "", path: str = "", paths: Optional[List[str]] = None, record_id: str = "",
                       depth: int = 1, max_files: int = 40, max_bytes: int = 2000000, prefer_tree_sitter: bool = False, assess=None,
                       lint: bool = True, card_lines: int = 40, max_line: int = 0) -> Dict:
    # max_bytes: capability_orchestration.py alone is past 400k chars; a truncated file is a syntax error and every
    # symbol in it falls to a stub (seen live 2026-09-21) — 2M keeps the repo's biggest modules whole, and ast reads
    # them in well under a second
    """The contract for a snippet (text + lang), repo files (path / paths — a directory lists its code files;
    depth=1 pulls in the repo files they import, once), or a fabric record holding code."""
    import os
    core = _code_core()
    loop = asyncio.get_running_loop()
    sources: List[Dict] = []
    skipped: List[Dict] = []
    label = ""
    if text:
        sources.append({"path": path or ("snippet." + (lang or "txt")), "text": text[:max_bytes], "lang": lang})
        label = path or "pasted " + (lang or "code")
    elif record_id:
        rec = await loop.run_in_executor(None, _read_record, record_id)
        if not rec:
            return {"error": "record %s not found" % record_id}
        sources.append({"path": record_id, "text": (rec.get("text") or "")[:max_bytes], "lang": lang})
        label = "record " + record_id
    else:
        want = [p for p in ([path] if path else []) + list(paths or []) if p]
        if not want:
            return {"error": "text, path, paths or record_id required"}
        read = await loop.run_in_executor(None, _read_repo_files, list(want), max_files, max_bytes)
        sources, skipped = read["sources"], read["skipped"]
        if not sources:
            return {"error": "nothing readable in " + ", ".join(want), "skipped": skipped}
        label = want[0] if len(want) == 1 else "%d paths" % len(want)
        # one hop: the repo files the seeds import join the parsed set, so calls across them resolve exact
        if depth and depth > 0 and len(sources) < max_files:
            seen = {s["path"] for s in sources}
            extra: List[str] = []
            for s in sources:
                lang_s = core.detect_lang(s["path"], s["text"], s.get("lang") or "")
                if lang_s == "python":
                    p = core.parse_python_ast(s["path"], s["text"])
                elif lang_s in ("javascript", "typescript"):
                    p = core.parse_js_patterns(s["path"], s["text"])
                elif lang_s == "html":
                    p = core.parse_html(s["path"], s["text"])
                else:
                    continue
                for cand in core.import_targets(p["imports"], s["path"]):
                    if cand not in seen and cand not in extra and os.path.isfile(os.path.join(_repo_root(), cand)):
                        extra.append(cand); seen.add(cand)
            if extra:
                more = await loop.run_in_executor(None, _read_repo_files, extra[: max(0, max_files - len(sources))], max_files, max_bytes)
                sources += more["sources"]; skipped += more["skipped"]
    doc = await loop.run_in_executor(None, lambda: core.explode_sources(sources, prefer_tree_sitter=prefer_tree_sitter, label=label,
                                                                        card_lines=int(card_lines or 0)))
    if lint:
        ext = await _external_lint(_repo_root(), [s["path"] for s in sources]) if (path or paths) else {}
        found = await loop.run_in_executor(None, lambda: core.lint_sources(sources, ext, int(max_line or 0)))
        doc = await loop.run_in_executor(None, lambda: core.attach_lint(doc, found))
    doc["source"]["skipped"] = skipped
    doc["source"]["text"] = {s["path"]: s["text"] for s in sources} if len(sources) > 1 else sources[0]["text"]
    want = _assess_arg(assess)
    if want is not None:
        A = _assess()
        res = await A.assess_code_doc(doc, {s["path"]: s["text"] for s in sources}, want or None, _repo_root() if not text and not record_id else "")
        doc["assessments"] = res["assessments"]
        doc["layers"] = list(doc["layers"]) + res["receipts"]
        doc["counts"]["assessments"] = len(doc["assessments"])
    return doc


# ── capabilities ──────────────────────────────────────────────────────────────────────────────────
# ── explode.target: what IS this thing, and how would it explode? ─────────────────────────────────────────────
# "if i try to explode a record by clicking it i get the error: explode failed: record
# topic_memgraph_repo_helm_charts not found" (owner, 2026-09-22). It was never a record. Checked live against the
# graph: it is a **Dataset** node that CONTAINS four FabricRecords -- so the honest answer is not an error at all,
# it is "four records, as lanes". A graph node, a repo file, a canvas block and a pasted passage are all
# explodable, each in its own way, and only the server can tell which; so ONE resolver answers the question and
# every surface -- the panel, the chat, the canvas -- asks it instead of guessing that an id is a record id.
#
# What the estate actually holds (counted on the live graph, 2026-09-22):
#   FabricRecord 608k   a record            -> its text
#   Dataset        5k   CONTAINS records    -> its records, as lanes          (590k such edges)
#   Entity       267k   MENTIONED_IN records-> the records that mention it     (1.4M such edges)
#   Memory       343k   human_text/summary  -> that text
#   Session/Response    text + topic        -> that text
#   CodeFile       5k   filepath + language -> code.explode on that file
#   CodeFunction   4k   inside a CodeFile   -> its file's code (the function is a card in it)
# A node with none of those is not an error either: it is told what it is and what it would take.

_TEXT_PROPS = ("text", "human_text", "content", "body", "summary", "docstring", "topic")
_MIN_TEXT = 40          # shorter than this is a LABEL, not a passage


def _prop_text(props: Dict) -> tuple:
    """The longest text-bearing property of a node, and which one it was. Graph properties are untyped -- a
    Memory's `human_text` comes back as a boolean on some rows -- so every value is checked, never assumed."""
    best, key = "", ""
    for k in _TEXT_PROPS:
        v = props.get(k)
        if isinstance(v, str) and len(v.strip()) > len(best):
            best, key = v.strip(), k
    return best, key


async def _aux_rows(cypher: str, **params) -> List[Dict]:
    """Read-only Cypher through the fabric's own guarded capability (never a driver of our own)."""
    out = await _call_cap("fabric.aux_graph.query", cypher=cypher, params=params or {})
    if not isinstance(out, dict) or out.get("error"):
        return []
    return out.get("rows") or []


def _lang_of(path: str) -> str:
    from vera.research import code_explode_core as _C
    try:
        return _C.detect_lang(path)
    except Exception:
        return ""


def _repo_has(rel: str) -> bool:
    import os
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return False
    root = _repo_root()
    full = os.path.abspath(os.path.join(root, rel))
    return (full == root or full.startswith(root + os.sep)) and os.path.isfile(full)


# How the answer READS is the feature: it is the sentence a reader gets instead of a false error, so it is built
# to be read. Neo4j hands labels general-first (["Entity", "File", "CodeFile"], ["Entity", "Response"]), so the
# LAST one is the informative one -- "a Response node", not "a Entity node".
_REL_SAYS = {"CONTAINS": "it contains", "MENTIONED_IN": "it is mentioned in", "HAS_RECORD": "it holds",
             "DERIVED_FROM": "it derives from", "REFERENCES": "it references", "LINKS_TO": "it links to"}


def _kind_of(ls: List[str]) -> str:
    return (ls[-1] if ls else "node")


def _an(word: str) -> str:
    return ("an " if str(word)[:1].upper() in "AEIOU" else "a ") + str(word)


def _hit(what: str, cap: str, args: Dict, label: str, why: str, seen: Optional[Dict] = None) -> Dict:
    return {"ok": True, "what": what, "cap": cap, "args": args, "label": label, "why": why, "seen": seen or {}}


def _miss(label: str, why: str, seen: Optional[Dict] = None) -> Dict:
    """Not explodable -- and SAYS SO in a sentence about the thing itself. Never 'record X not found' for
    something that was never a record."""
    return {"ok": False, "what": "nothing", "cap": "", "args": {}, "label": label, "why": why, "seen": seen or {}}


async def resolve_target(id: str = "", text: str = "", lang: str = "", path: str = "",
                         canvas_id: str = "", key: str = "", max_records: int = 8) -> Dict:
    """Resolve anything a reader can click into the explode call that suits it."""
    id = (id or "").strip()
    max_records = max(1, min(24, int(max_records or 8)))

    # ── what the caller already holds: a passage, a snippet, a path ───────────────────────────────────────────
    if text:
        if lang or (path and str(path).lower().endswith(_CODE_EXT)):
            return _hit("code", "code.explode", {"text": text, "lang": lang, "path": path},
                        "%s snippet - %d chars" % (lang or "code", len(text)), "the caller passed code")
        return _hit("text", "nlp.explode.prose", {"text": text},
                    "passage - %d chars" % len(text), "the caller passed a passage")
    if path:
        rel = str(path).replace("\\", "/").lstrip("/")
        if not _repo_has(rel):
            return _miss(rel, "no file at '%s' in the repo - a path is repo-relative (vera/research/...)" % rel)
        return _hit("code", "code.explode", {"path": rel, "depth": 1}, rel,
                    "a repo file, exploded with one hop of the files it imports")

    # ── a canvas block ───────────────────────────────────────────────────────────────────────────────────────
    if canvas_id:
        doc = await _call_cap("canvas.get", id=canvas_id)
        if not isinstance(doc, dict) or doc.get("error"):
            return _miss(canvas_id, "no canvas '%s'" % canvas_id)
        blocks = doc.get("blocks") or []
        b = next((x for x in blocks if key and (x.get("key") == key or x.get("id") == key)), None)
        if key and not b:
            return _miss(key, "the canvas has no item '%s' - its items are: %s"
                         % (key, ", ".join(str(x.get("key") or x.get("id")) for x in blocks[:12]) or "none"))
        if not b:
            b = next((x for x in blocks if x.get("type") in ("code", "markdown", "note")), None)
        if not b:
            return _miss(canvas_id, "nothing on this canvas holds code or prose")
        c = b.get("content") or {}
        nm = str(b.get("key") or b.get("id") or "")
        if b.get("type") == "code" and isinstance(c.get("code"), str):
            return _hit("code", "code.explode",
                        {"text": c["code"], "lang": c.get("lang") or "", "path": c.get("filename") or ""},
                        "canvas code - %s" % (c.get("filename") or nm), "a code item on the canvas")
        body = c.get("md") if isinstance(c.get("md"), str) else c.get("text")
        if isinstance(body, str) and body.strip():
            return _hit("text", "nlp.explode.prose", {"text": body}, "canvas text - %s" % nm,
                        "a %s item on the canvas" % b.get("type"))
        return _miss(nm, "the canvas item '%s' is a %s - it holds no code or prose" % (nm, b.get("type")))

    if not id:
        return _miss("", "nothing to explode: pass an id (a record, a graph node, a canvas item), "
                         "a repo path, or the text itself")

    # ── a fabric record ──────────────────────────────────────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    rec = await loop.run_in_executor(None, _read_record, id)
    if rec:
        t = (rec.get("text") or "").strip()
        head = (t.split("\n", 1)[0][:60] or id) if t else id
        return _hit("record", "nlp.explode.prose", {"record_id": id}, "record %s - %s" % (id, head),
                    "a fabric record in dataset '%s'" % (rec.get("dataset_id") or "?"),
                    {"type": "FabricRecord", "chars": len(t)})

    # ── a graph node ─────────────────────────────────────────────────────────────────────────────────────────
    rows = await _aux_rows(
        "MATCH (n {id:$id}) OPTIONAL MATCH (f:CodeFile)-[]->(n) "
        "RETURN properties(n) AS props, labels(n) AS ls, f.filepath AS parent_path, "
        "f.language AS parent_lang LIMIT 1", id=id)
    if rows:
        r = rows[0] or {}
        props = r.get("props") or {}
        ls = [str(x) for x in (r.get("ls") or [])]
        kind = _kind_of(ls)
        seen = {"type": kind, "labels": ls, "id": id}

        if "FabricRecord" in ls:
            # The graph is shared across the estate; the fabric's record STORE is per instance. A record this
            # instance holds was resolved above, so reaching here means the graph knows the record and the local
            # store does not hold its text -- a fact about WHERE to explode it, not a missing record. (Seen live
            # on the design mirror, 2026-09-22: its store has 41k records, the graph 608k.)
            title = props.get("title") if isinstance(props.get("title"), str) else ""
            return {"ok": False, "what": "record", "cap": "nlp.explode.prose", "args": {"record_id": id},
                    "label": "record %s%s" % (id[:12], (" - " + title[:50]) if title else ""),
                    "why": "a fabric record of dataset '%s'. The graph knows it, but this instance's record store "
                           "does not hold its text - the graph is shared across the estate, the records are per "
                           "instance - so explode it where that fabric lives."
                           % (props.get("dataset_id") or "?"),
                    "seen": seen}

        if "CodeFile" in ls:
            fp = props.get("filepath") or props.get("path") or props.get("filename")
            if isinstance(fp, str) and _repo_has(fp):
                return _hit("code", "code.explode", {"path": fp, "depth": 1}, fp,
                            "a code file of the project graph", seen)
            if isinstance(fp, str) and fp:
                return _miss(fp, "the graph has this file as '%s', which is not in this checkout - "
                                 "explode it by path from a checkout that has it" % fp, seen)

        if "CodeFunction" in ls or "Method" in ls or "Function" in ls:
            fp, fname = r.get("parent_path"), props.get("function_name") or props.get("name") or id
            if isinstance(fp, str) and _repo_has(fp):
                return _hit("code", "code.explode", {"path": fp, "depth": 0},
                            "%s - %s" % (fp, fname),
                            "a function of %s: the file is exploded and '%s' is a card in it" % (fp, fname), seen)

        body, which = _prop_text(props)
        if len(body) >= _MIN_TEXT:
            return _hit("text", "nlp.explode.prose", {"text": body},
                        "%s %s - %d chars" % (kind, id, len(body)),
                        "%s node: its own `%s` is the passage" % (_an(kind), which), seen)

        rids = props.get("record_ids")
        if isinstance(rids, (list, tuple)) and rids:
            got = [str(x) for x in rids][:max_records]
            return _hit("records", "nlp.explode.prose", {"record_ids": got},
                        "%s %s - %d records" % (kind, id, len(got)),
                        "%s node: the records it names, as lanes" % _an(kind), seen)

        recs = await _aux_rows(
            "MATCH ({id:$id})-[r]->(m:FabricRecord) RETURN m.id AS rid, type(r) AS rel LIMIT $k",
            id=id, k=max_records)
        if recs:
            got = [str(x.get("rid")) for x in recs if x.get("rid")]
            rel = str((recs[0] or {}).get("rel") or "")
            if got:
                says = _REL_SAYS.get(rel, "it links to" + (" by %s" % rel if rel else ""))
                return _hit("records", "nlp.explode.prose", {"record_ids": got},
                            "%s %s - %d records" % (kind, id, len(got)),
                            "%s node holds no text of its own; the %d records %s are the evidence, and explode "
                            "as lanes" % (_an(kind), len(got), says), seen)

        have = ", ".join(sorted(k for k, v in props.items() if v not in (None, "", [], {}))) or "nothing"
        short = _prop_text(props)[0]
        return _miss("%s %s" % (kind, id),
                     "%s node. It carries %s - %s and no records linked to it, so there is nothing here to "
                     "explode. Open a record that mentions it, or explode the passage it came from."
                     % (_an(kind), have,
                        ("its text is %d characters, too short to have structure" % len(short)) if short
                        else "no text"), seen)

    # ── a repo path given as an id ───────────────────────────────────────────────────────────────────────────
    if ("/" in id or id.lower().endswith(_CODE_EXT)) and _repo_has(id):
        return _hit("code", "code.explode", {"path": id.replace("\\", "/").lstrip("/"), "depth": 1}, id,
                    "a repo file")

    return _miss(id, "nothing with the id '%s': no fabric record, no graph node, no repo file. If this is text, "
                     "pass it as text; if it is a canvas item, pass canvas_id and key." % id)


if _CAP_AVAILABLE:
    @capability(
        "code.explode",
        http_method="POST", http_path="/code/explode", http_tags=["code", "graph"],
        memory="off",
        description=("Explode code into a STRUCTURED graph — the contract <vera-structgraph> draws: files and classes as "
                     "groups, modules / classes / functions / methods (or a page's elements and CSS rules) as cards with "
                     "their spans, CALLS / IMPORTS / INHERITS / MATCHES edges each with a resolution (exact | heuristic | "
                     "external — an unresolved target is a grey stub, never an invented edge). Engines: Python via ast, "
                     "tree-sitter when installed (parses broken and partial code), tolerant patterns for JS / CSS / HTML; "
                     "every card says which. One of: text + lang (a snippet), path / paths (repo-relative files or a "
                     "directory; depth=1 pulls in the repo files they import), record_id (a fabric record holding code). "
                     "assess (bool | list) runs the code scorers — complexity, smells, clones, tests, provenance, health. "
                     "Each card carries its own SOURCE (card_lines=40, 0 for none) and, with lint=true (the default), "
                     "the findings on its lines — ruff / eslint when this host has them, a set of ast-level rules "
                     "otherwise, each finding naming the tool that produced it. max_line turns the line-length rule on. "
                     "Nothing is persisted. Output: {ok, kind: code|page, source:{paths, partial, errors, engines, "
                     "tree_sitter, text}, layout, layers, groups, cards, edges, assessments:[syntax, resolved], counts}."),
    )
    async def cap_code_explode(text: str = "", lang: str = "", path: str = "", paths: Optional[List[str]] = None, record_id: str = "",
                               depth: int = 1, max_files: int = 40, prefer_tree_sitter: bool = False, assess=None,
                               lint: bool = True, card_lines: int = 40, max_line: int = 0, trace_id=None) -> Dict:
        if isinstance(paths, str):
            paths = [p.strip() for p in paths.split(",") if p.strip()]
        return await explode_code(text=text, lang=lang, path=path, paths=paths, record_id=record_id, depth=int(depth or 0),
                                  max_files=max(1, min(200, int(max_files or 40))), prefer_tree_sitter=bool(prefer_tree_sitter), assess=assess,
                                  lint=bool(lint), card_lines=int(card_lines or 0), max_line=int(max_line or 0))

    @capability(
        "explode.target",
        http_method="POST", http_path="/explode/target", http_tags=["graph", "nlp", "code"],
        memory="off",
        description=("What IS this thing, and how would it explode? Resolves anything a reader can click into the "
                     "explode call that suits it, so no surface has to guess that an id is a record id: a fabric "
                     "record -> its text; a Dataset node -> the records it CONTAINS, as lanes; an Entity -> the "
                     "records that MENTION it (the evidence); a Memory / Session / Response -> its own text; a "
                     "CodeFile -> code.explode on its path; a CodeFunction -> its file (the function is a card in "
                     "it); a canvas item (canvas_id + key) -> its code or its prose; a repo path -> the file; text "
                     "-> itself. A thing with no text and no records is NOT an error: the answer says what the "
                     "node is and what it would take. Input: id (record id, graph node id or repo path), or text "
                     "(+ lang), or path, or canvas_id + key; max_records. Output: {ok, what: record | records | "
                     "text | code | nothing, cap (the capability to call), args (ready to post to it), label, "
                     "why, seen}."),
    )
    async def cap_explode_target(id: str = "", text: str = "", lang: str = "", path: str = "",
                                 canvas_id: str = "", key: str = "", max_records: int = 8,
                                 trace_id=None) -> Dict:
        return await resolve_target(id=id, text=text, lang=lang, path=path, canvas_id=canvas_id,
                                    key=key, max_records=int(max_records or 8))

    @capability(
        "nlp.explode.prose",
        http_method="POST", http_path="/nlp/explode/prose", http_tags=["nlp", "fabric", "graph"],
        memory="off",
        description=("Explode prose into a STRUCTURED graph — the contract <vera-structgraph> draws: paragraphs as "
                     "groups, entities as cards (each with the span of its first mention), relations as edges (each "
                     "with its resolution), assessments. Nothing is persisted. One of: text (a pasted passage), "
                     "record_id (a fabric record; ranges=[[start,end],...] slices it), record_ids (several records, "
                     "as lanes). mode: 'position' (paragraphs down, entity types across — the default for a passage "
                     "or a slice) | 'type' (a band per entity type — the default for a whole record). layers: list "
                     "of layer ids to run (nlp.explode.layers lists them; default = the layers on by default). "
                     "include_text (bool=True) carries the source text back for click-to-source. assess (bool | list) runs the "
                     "prose scorers (assess.scorers) and appends their verdicts and receipts. "
                     "Output: {ok, kind, source:{record_id, ranges, partial, label, text_hash, text}, layout, "
                     "layers:[{id, label, by, where, on, count, ms, error?}], groups, cards, edges, assessments, counts}."),
    )
    async def cap_explode_prose(text: str = "", record_id: str = "", record_ids: Optional[List[str]] = None,
                                ranges: Optional[List] = None, mode: str = "", layers: Optional[List[str]] = None,
                                content_type: str = "text", include_text: bool = True, assess=None, trace_id=None) -> Dict:
        return await explode_prose(text=text, record_id=record_id, record_ids=record_ids, ranges=ranges, mode=mode,
                                   layers=layers, content_type=content_type, include_text=bool(include_text), assess=assess)

    @capability(
        "nlp.explode.layers",
        http_method="GET", http_path="/nlp/explode/layers", http_tags=["nlp", "graph"],
        memory="off",
        description=("The analysis layers Explode can run over prose — each one tool, one pass — with what produced "
                     "it, where it runs and whether it is on by default. Output: {layers:[{id, label, kind, by, "
                     "where, default_on, needs}]}."),
    )
    async def cap_explode_layers(trace_id=None) -> Dict:
        return {"ok": True, "layers": layer_list()}

    log.info("explode: %d prose layers registered (%s)", len(_ORDER), ", ".join(_ORDER))
