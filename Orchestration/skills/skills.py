"""
vera_skills.py  —  Skills & Ontologies capability module
=========================================================
Skills     : named prompt templates that augment LLM / agent behaviour.
             They inject system-prompt fragments, few-shot examples,
             chain-of-thought scaffolds, or persona definitions.
Ontologies : structured knowledge schemas that tell the agent HOW to
             process data — entity types, relationship rules, context
             hierarchies, memory slot definitions, tagging taxonomies.

Both are stored as JSON in-memory (with optional Redis persistence)
and exposed as:
  • @capability  →  MCP-callable, REST-mountable, observable
  • register_ui  →  built-in harness panel (Skills Editor / Ontology Browser)

Usage:
    import vera_skills        # after importing vera_orchestrator + vera_capabilities

Persistence:
    If Redis is available, skills and ontologies are persisted under:
        vera:skills:<id>
        vera:ontologies:<id>
    On startup they are reloaded automatically.
"""

import asyncio
import sys
import sqlite3
from pathlib import Path
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,           # noqa
    capability, emit_event, now_iso, ollama_generate, register_ui, schedule,
)

def _redis():
    """Lazy getter — always returns the live Redis connection, never a stale None."""
    return _orch.REDIS

log = logging.getLogger("vera.skills")

# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY STORES
# ─────────────────────────────────────────────────────────────────────────────

SKILLS: dict[str, dict]     = {}   # id → skill record
ONTOLOGIES: dict[str, dict] = {}   # id → ontology record

# ── Skill record schema ───────────────────────────────────────────────────────
# {
#   "id": str,
#   "name": str,                       # human label
#   "description": str,
#   "type": "system_prompt"            # how the skill is injected
#          | "few_shot"
#          | "chain_of_thought"
#          | "persona"
#          | "tool_hint"
#          | "custom",
#   "content": str,                    # the prompt fragment / template
#   "variables": ["{{var1}}", ...],    # detected template variables
#   "tags": [str],
#   "enabled": bool,
#   "created": iso_str,
#   "updated": iso_str,
# }

# ── Ontology record schema ────────────────────────────────────────────────────
# Vera native fields are kept verbatim. OWL 2 DL + SKOS standard fields are
# added below as additive optional extensions; they round-trip through
# skills_owl.ontology_to_graph / graph_to_ontology.
# {
#   "id": str,
#   "name": str,
#   "description": str,
#   "domain": str,                     # e.g. "medical", "legal", "code", "general"
#   "context_hints": str,              # free-form text for LLM context
#   "tags": [str],
#   "enabled": bool,
#   "created": iso_str,
#   "updated": iso_str,
#
#   # ── OWL/SKOS ontology-level extensions (all optional) ─────────────────
#   "iri": str,                        # base IRI, "https://vera.local/ontology/<id>#"
#   "pref_label": str,                 # skos:prefLabel
#   "alt_labels": [str],               # skos:altLabel synonyms
#   "imports": [str],                  # owl:imports IRIs
#   "annotations": {str: str},         # free-form owl:Annotation properties
#
#   # ── Entities (owl:Class) ─────────────────────────────────────────────
#   # An attribute can be a bare string OR a dict
#   #     {name, iri?, range_type ('string'|'integer'|'decimal'|'boolean'|
#   #                              'dateTime'|'date'|'anyURI'|'float'|'double'),
#   #      functional?: bool, description?: str}
#   "entities": [{
#       "name": str, "description": str, "attributes": [str|dict],
#       # OWL ↓
#       "iri": str, "sub_class_of": [str], "equivalent_to": [str],
#       "disjoint_with": [str],
#       "restrictions": [{
#           "kind": "someValuesFrom"|"allValuesFrom"|"hasValue"|
#                   "minCardinality"|"maxCardinality"|"exactCardinality"|
#                   "minQualifiedCardinality"|"maxQualifiedCardinality"|
#                   "exactQualifiedCardinality",
#           "on_property": str, "value": str, "qualifier": str
#       }],
#       # SKOS ↓
#       "pref_label": str, "alt_labels": [str], "notation": str,
#       "broader": [str], "narrower": [str], "related": [str],
#       "annotations": {str: str},
#   }],
#
#   # ── Relationships (owl:ObjectProperty) ───────────────────────────────
#   "relationships": [{
#       "from": str, "to": str, "label": str, "description": str,
#       # OWL ↓
#       "iri": str, "inverse_of": str, "sub_property_of": [str],
#       "characteristics": [  # subset of:
#           "Functional", "InverseFunctional", "Transitive",
#           "Symmetric", "Asymmetric", "Reflexive", "Irreflexive"],
#       "domain_classes": [str],   # multi-domain (overrides single 'from')
#       "range_classes":  [str],   # multi-range  (overrides single 'to')
#   }],
#
#   "processing_rules": [{"trigger":str, "action":str, "priority":int}],
#   "memory_slots":     [{"key":str, "type":str, "description":str}],
# }

# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE — SQLite (primary, always works) + Redis (secondary cache)
# ─────────────────────────────────────────────────────────────────────────────

_SQLITE_PATH = Path(__file__).parent / "vera_skills.db"

def _sqlite_init():
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY, name TEXT, data TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ontologies (
            id TEXT PRIMARY KEY, name TEXT, data TEXT, updated_at TEXT
        );
    """)
    conn.commit()
    return conn

# Eager init at module load — tables exist before any request
try:
    _db_init = _sqlite_init(); _db_init.close()
    log.debug("skills: SQLite ready at %s", _SQLITE_PATH)
except Exception as _e:
    log.warning("skills: SQLite init failed: %s", _e)


def _sqlite_save(table: str, record: dict):
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (id, name, data, updated_at) VALUES (?,?,?,?)",
            (record["id"], record.get("name", ""),
             json.dumps(record), record.get("updated", now_iso()))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("skills sqlite_save %s: %s", table, e)


def _sqlite_delete(table: str, item_id: str):
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("skills sqlite_delete %s: %s", table, e)


def _sqlite_load_all(table: str) -> list:
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        rows = conn.execute(f"SELECT data FROM {table}").fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]
    except Exception as e:
        log.warning("skills sqlite_load_all %s: %s", table, e)
        return []


async def _redis_save(prefix: str, record: dict):
    r = _redis()
    if r:
        try:
            await r.set(f"vera:{prefix}:{record['id']}", json.dumps(record))
        except Exception as e:
            log.debug("Redis save %s/%s: %s", prefix, record['id'], e)


async def _redis_delete(prefix: str, item_id: str):
    r = _redis()
    if r:
        try:
            await r.delete(f"vera:{prefix}:{item_id}")
        except Exception as e:
            log.debug("Redis delete %s/%s: %s", prefix, item_id, e)


async def _save_async(prefix: str, record: dict):
    """Write to SQLite synchronously (via executor) + Redis cache."""
    loop = asyncio.get_event_loop()
    table = "skills" if prefix == "skills" else "ontologies"
    await loop.run_in_executor(None, _sqlite_save, table, record)
    await _redis_save(prefix, record)


async def _delete_async(prefix: str, item_id: str):
    loop = asyncio.get_event_loop()
    table = "skills" if prefix == "skills" else "ontologies"
    await loop.run_in_executor(None, _sqlite_delete, table, item_id)
    await _redis_delete(prefix, item_id)


# ── Graph + Fabric helpers ────────────────────────────────────────────────────

def _get_session_id() -> str:
    """Get session_id from syslog trigger chain."""
    try:
        sl = sys.modules.get("syslog")
        if sl:
            return sl.get_trigger_chain().get("session_id", "")
    except Exception:
        pass
    return ""


async def _record_to_graph(item: dict, category: str, cap_name: str,
                            tags: list, session_id: str):
    """Store a skill/ontology as a MemoryRecord linked to the session node."""
    if not session_id:
        return
    try:
        hooks   = sys.modules.get("memory_hooks")
        mem_mod = sys.modules.get("memory")
        if not hooks or not mem_mod:
            return
        MEMORY, MemoryRecord = mem_mod.MEMORY, mem_mod.MemoryRecord
        sess_root = await hooks.get_or_create_session(session_id)
        node_id   = str(uuid.uuid4())
        rec = MemoryRecord(
            id=node_id, session_id=session_id,
            record_type="fact", source_type="tool",
            category=category, tags=tags,
            text=f"{category}: {item.get('name','')} — {item.get('description','')[:120]}",
            full_text=json.dumps(item)[:2000],
            importance=0.7, capability=cap_name,
            metadata={"item_id": item["id"], "item_name": item.get("name", "")},
        )
        await MEMORY.store(rec)
        await hooks._link_nodes(sess_root, node_id, "SESSION_CONTENT",
                                {"cap": cap_name, "item_id": item["id"]},
                                session_id=session_id)
        log.debug("skills: graph node %s for %s/%s", node_id[:8], category, item["id"])
    except Exception as e:
        log.debug("skills _record_to_graph: %s", e)


async def _record_to_fabric(item: dict, dataset_id: str, tags: list):
    """Store a skill/ontology in the data fabric (primary persistent store)."""
    try:
        fabric = sys.modules.get("data_fabric")
        if not fabric:
            return
        text = "\n".join(filter(None, [
            item.get("name", ""),
            item.get("description", ""),
            item.get("content", ""),
            item.get("context_hints", ""),
        ]))
        await fabric.ingest_dataset(
            dataset_id=dataset_id,
            data=[{"text": text[:3000], **{k: v for k, v in item.items()
                                           if not isinstance(v, (list, dict)) or k in ("tags",)}}],
            source="skills", tags=tags,
        )
    except Exception as e:
        log.debug("skills _record_to_fabric: %s", e)


async def _load_from_fabric(dataset_id: str, store: dict):
    """Pull all records from a fabric dataset into the in-memory store."""
    try:
        fabric = sys.modules.get("data_fabric")
        if not fabric:
            return 0
        results = await fabric.query_dataset(
            dataset_id=dataset_id,
            query={"limit": 2000, "include_data": True},
        )
        count = 0
        for r in (results or []):
            data = r.get("data") or {}
            item_id = data.get("id")
            if item_id and item_id not in store:
                store[item_id] = data
                count += 1
        return count
    except Exception as e:
        log.debug("_load_from_fabric %s: %s", dataset_id, e)
        return 0


async def _startup_load():
    """Reload skills and ontologies. Order: fabric (primary) → SQLite → Redis."""
    loop = asyncio.get_event_loop()

    # 1. Try fabric first (most up-to-date across restarts)
    fabric_skills = await _load_from_fabric("skills", SKILLS)
    fabric_onts   = await _load_from_fabric("ontologies", ONTOLOGIES)

    # 2. SQLite fallback (always works offline)
    try:
        skill_recs = await loop.run_in_executor(None, _sqlite_load_all, "skills")
        ont_recs   = await loop.run_in_executor(None, _sqlite_load_all, "ontologies")
        added_s, added_o = 0, 0
        for rec in skill_recs:
            if rec["id"] not in SKILLS:
                SKILLS[rec["id"]] = rec; added_s += 1
        for rec in ont_recs:
            if rec["id"] not in ONTOLOGIES:
                ONTOLOGIES[rec["id"]] = rec; added_o += 1
        if added_s or added_o:
            log.debug("skills: SQLite added %d skills, %d ontologies", added_s, added_o)
    except Exception as e:
        log.warning("skills: SQLite startup load: %s", e)

    # 3. Redis cache merge
    r = _redis()
    if r:
        try:
            for k in (await r.keys("vera:skills:*") or []):
                v = await r.get(k)
                if v:
                    rec = json.loads(v)
                    if rec["id"] not in SKILLS:
                        SKILLS[rec["id"]] = rec
                        await loop.run_in_executor(None, _sqlite_save, "skills", rec)
            for k in (await r.keys("vera:ontologies:*") or []):
                v = await r.get(k)
                if v:
                    rec = json.loads(v)
                    if rec["id"] not in ONTOLOGIES:
                        ONTOLOGIES[rec["id"]] = rec
                        await loop.run_in_executor(None, _sqlite_save, "ontologies", rec)
        except Exception as e:
            log.debug("skills: Redis startup merge: %s", e)

    log.info("skills: loaded %d skills, %d ontologies (fabric:%d/%d)",
             len(SKILLS), len(ONTOLOGIES), fabric_skills, fabric_onts)

def _extract_variables(content: str) -> list[str]:
    """Detect {{variable}} placeholders in a skill template."""
    return list(dict.fromkeys(re.findall(r'\{\{(\w+)\}\}', content)))

def _render_skill(skill: dict, variables: dict) -> str:
    """Substitute {{var}} placeholders with provided values."""
    content = skill["content"]
    for k, v in variables.items():
        content = content.replace(f"{{{{{k}}}}}", str(v))
    return content

# ─────────────────────────────────────────────────────────────────────────────
# ██  SKILLS CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability("skills.list", memory="off",
            http_method="GET", http_path="/skills", http_tags=["skills"],
            description="List all registered skills. Filter by tag or type.")
async def skills_list(tag: str = "", type: str = "", enabled_only: bool = False, trace_id=None):
    items = list(SKILLS.values())
    if tag:          items = [s for s in items if tag in s.get("tags", [])]
    if type:         items = [s for s in items if s.get("type") == type]
    if enabled_only: items = [s for s in items if s.get("enabled", True)]
    return {"skills": items, "count": len(items)}


@capability("skills.get", memory="off",
            http_method="GET", http_path="/skills/get", http_tags=["skills"],
            description="Get a single skill by id.")
async def skills_get(id: str, trace_id=None):
    skill = SKILLS.get(id)
    if not skill:
        return {"error": f"Skill not found: {id}"}
    return skill


@capability("skills.create", memory="off",
            http_method="POST", http_path="/skills", http_tags=["skills"],
            description="Create a new skill. Returns the created record with generated id.")
async def skills_create(
    name:        str,
    content:     str,
    description: str  = "",
    type:        str  = "system_prompt",
    tags:        str  = "",          # comma-separated
    enabled:     bool = True,
    session_id:  str  = "",   # caller's session for graph linking
    trace_id=None,
):
    sid = str(uuid.uuid4())[:8]
    now = now_iso()
    skill = {
        "id":          sid,
        "name":        name,
        "description": description,
        "type":        type,
        "content":     content,
        "variables":   _extract_variables(content),
        "tags":        [t.strip() for t in tags.split(",") if t.strip()],
        "enabled":     enabled,
        "created":     now,
        "updated":     now,
    }
    SKILLS[sid] = skill
    await _save_async("skills", skill)
    await emit_event({"type": "skills.created", "id": sid, "name": name})
    # Record to graph + data fabric (fire-and-forget)
    _sid = session_id or _get_session_id()
    _tag_list = ["skill", type, name] + [t.strip() for t in tags.split(",") if t.strip()]
    asyncio.create_task(_record_to_graph(skill, "skill", "skills.create", _tag_list, _sid))
    asyncio.create_task(_record_to_fabric(skill, "skills", _tag_list))
    return skill


@capability("skills.update", memory="off",
            http_method="POST", http_path="/skills/update", http_tags=["skills"],
            description="Update an existing skill by id. Only provided fields are changed.")
async def skills_update(
    id:          str,
    name:        str  = "",
    content:     str  = "",
    description: str  = "",
    type:        str  = "",
    tags:        str  = "",
    enabled:     bool = None,
    trace_id=None,
):
    skill = SKILLS.get(id)
    if not skill:
        return {"error": f"Skill not found: {id}"}
    if name:        skill["name"]        = name
    if content:     skill["content"]     = content; skill["variables"] = _extract_variables(content)
    if description: skill["description"] = description
    if type:        skill["type"]        = type
    if tags:        skill["tags"]        = [t.strip() for t in tags.split(",") if t.strip()]
    if enabled is not None: skill["enabled"] = enabled
    skill["updated"] = now_iso()
    await _save_async("skills", skill)
    await emit_event({"type": "skills.updated", "id": id})
    return skill


@capability("skills.delete", memory="off",
            http_method="POST", http_path="/skills/delete", http_tags=["skills"],
            description="Delete a skill by id.")
async def skills_delete(id: str, trace_id=None):
    if id not in SKILLS:
        return {"error": f"Skill not found: {id}"}
    SKILLS.pop(id)
    await _delete_async("skills", id)
    await emit_event({"type": "skills.deleted", "id": id})
    return {"deleted": id}


@capability("skills.apply", memory="auto",
            http_method="POST", http_path="/skills/apply", http_tags=["skills"],
            description="Apply one or more skills to augment an LLM call. "
                        "Injects skill content into the system prompt, renders templates, "
                        "and returns the LLM response.")
async def skills_apply(
    prompt:      str,
    skill_ids:   str  = "",     # comma-separated skill ids; empty = all enabled
    variables:   str  = "{}",   # JSON dict of template variables
    model:       str  = "",
    instance_id: str  = "",
    prefer_gpu:  bool = False,
    trace_id=None,
):
    # Resolve which skills to apply
    if skill_ids.strip():
        ids     = [s.strip() for s in skill_ids.split(",") if s.strip()]
        skills  = [SKILLS[i] for i in ids if i in SKILLS]
    else:
        skills = [s for s in SKILLS.values() if s.get("enabled", True)]

    # Parse template variables
    try:
        vars_dict = json.loads(variables) if variables.strip() else {}
    except Exception:
        vars_dict = {}

    # Build augmented system prompt by stacking skill content
    system_parts = []
    for skill in skills:
        rendered = _render_skill(skill, vars_dict)
        stype    = skill.get("type", "system_prompt")
        if stype == "persona":
            system_parts.insert(0, rendered)   # persona goes first
        elif stype == "chain_of_thought":
            system_parts.append(f"## Reasoning approach\n{rendered}")
        elif stype == "few_shot":
            system_parts.append(f"## Examples\n{rendered}")
        elif stype == "tool_hint":
            system_parts.append(f"## Available tools / capabilities\n{rendered}")
        else:
            system_parts.append(rendered)

    system = "\n\n".join(system_parts)

    text = await ollama_generate(
        prompt,
        system    = system,
        model     = model or None,
        instance_id = instance_id or None,
        prefer_gpu  = prefer_gpu,
    )

    return {
        "text":           text,
        "skills_applied": [s["name"] for s in skills],
        "skill_count":    len(skills),
        "system_preview": system[:300] + ("…" if len(system) > 300 else ""),
        "model":          model or "default",
    }


@capability("skills.compose", memory="on",
            http_method="POST", http_path="/skills/compose", http_tags=["skills"],
            description="Use the LLM to compose a new skill from a plain-English description.")
async def skills_compose(
    description: str,
    type:        str  = "system_prompt",
    name:        str  = "",
    save:        bool = True,
    trace_id=None,
):
    """Ask the LLM to draft a skill prompt from a natural-language description."""
    type_hints = {
        "system_prompt":    "a clear system prompt that shapes overall assistant behaviour",
        "few_shot":         "2-4 input/output example pairs that demonstrate the desired behaviour",
        "chain_of_thought": "step-by-step reasoning instructions the LLM should follow",
        "persona":          "a concise persona / role definition for the assistant",
        "tool_hint":        "a list of available tools/capabilities with brief descriptions",
    }
    hint = type_hints.get(type, "a prompt fragment")
    system = (
        f"You are a prompt engineering expert. Write {hint} based on the user's description. "
        "Use {{variable_name}} placeholders for any values that should be parameterised. "
        "Respond with ONLY the prompt text — no preamble, no explanation, no markdown fences."
    )
    content = await ollama_generate(
        f"Write a skill prompt for: {description}",
        system    = system,
        prefer_gpu = True,
    )
    content = content.strip()

    result: dict = {
        "content":   content,
        "type":      type,
        "variables": _extract_variables(content),
        "preview":   content[:400],
    }

    if save and content:
        skill_name = name or description[:40].strip()
        saved = await skills_create(
            name=skill_name, content=content, description=description,
            type=type, trace_id=trace_id,
        )
        result["saved"] = saved

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ██  ONTOLOGIES CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability("ontologies.list", memory="off",
            http_method="GET", http_path="/ontologies", http_tags=["ontologies"],
            description="List all registered ontologies.")
async def ontologies_list(domain: str = "", tag: str = "", trace_id=None):
    items = list(ONTOLOGIES.values())
    if domain: items = [o for o in items if o.get("domain") == domain]
    if tag:    items = [o for o in items if tag in o.get("tags", [])]
    return {"ontologies": items, "count": len(items)}


@capability("ontologies.get", memory="off",
            http_method="GET", http_path="/ontologies/get", http_tags=["ontologies"],
            description="Get a single ontology by id.")
async def ontologies_get(id: str, trace_id=None):
    ont = ONTOLOGIES.get(id)
    if not ont:
        return {"error": f"Ontology not found: {id}"}
    return ont


@capability("ontologies.create", memory="off",
            http_method="POST", http_path="/ontologies", http_tags=["ontologies"],
            description="Create a new ontology definition.")
async def ontologies_create(
    name:              str,
    description:       str  = "",
    domain:            str  = "general",
    context_hints:     str  = "",
    entities:          str  = "[]",      # JSON array
    relationships:     str  = "[]",
    processing_rules:  str  = "[]",
    memory_slots:      str  = "[]",
    tags:              str  = "",
    enabled:           bool = True,
    session_id:        str  = "",   # caller's session for graph linking
    # ── OWL+SKOS extensions (all optional) ───────────────────────────────
    iri:               str  = "",
    pref_label:        str  = "",
    alt_labels:        str  = "",      # csv
    imports:           str  = "",      # csv of IRIs
    annotations:       str  = "{}",    # JSON object
    trace_id=None,
):
    oid = str(uuid.uuid4())[:8]
    now = now_iso()

    def _parse(s, default):
        try:
            return json.loads(s) if isinstance(s, str) else s
        except Exception:
            return default

    # Default IRI if none supplied — keeps records globally addressable.
    iri_resolved = iri.strip() or f"https://vera.local/ontology/{oid}#"

    ont = {
        "id":               oid,
        "name":             name,
        "description":      description,
        "domain":           domain,
        "context_hints":    context_hints,
        "entities":         _parse(entities, []),
        "relationships":    _parse(relationships, []),
        "processing_rules": _parse(processing_rules, []),
        "memory_slots":     _parse(memory_slots, []),
        "tags":             [t.strip() for t in tags.split(",") if t.strip()],
        "enabled":          enabled,
        "created":          now,
        "updated":          now,
        # ── OWL/SKOS native fields ────────────────────────────────────
        "iri":              iri_resolved,
        "pref_label":       pref_label,
        "alt_labels":       [t.strip() for t in alt_labels.split(",") if t.strip()],
        "imports":          [t.strip() for t in imports.split(",") if t.strip()],
        "annotations":      _parse(annotations, {}) or {},
    }
    ONTOLOGIES[oid] = ont
    await _save_async("ontologies", ont)
    await emit_event({"type": "ontologies.created", "id": oid, "name": name})
    # Record to graph + data fabric (fire-and-forget)
    _sid = session_id or _get_session_id()
    _tag_list = ["ontology", domain, name] + [t.strip() for t in tags.split(",") if t.strip()]
    asyncio.create_task(_record_to_graph(ont, "ontology", "ontologies.create", _tag_list, _sid))
    asyncio.create_task(_record_to_fabric(ont, "ontologies", _tag_list))
    return ont


@capability("ontologies.update", memory="off",
            http_method="POST", http_path="/ontologies/update", http_tags=["ontologies"],
            description="Update fields of an existing ontology.")
async def ontologies_update(
    id:             str,
    name:           str = "",
    description:    str = "",
    domain:         str = "",
    context_hints:  str = "",
    entities:       str = "",
    relationships:  str = "",
    processing_rules: str = "",
    memory_slots:   str = "",
    tags:           str = "",
    enabled:        bool = None,
    # ── OWL+SKOS extensions ─────────────────────────────────────────────
    iri:            str = "",
    pref_label:     str = "",
    alt_labels:     str = "",      # csv; pass empty to leave unchanged
    imports:        str = "",
    annotations:    str = "",      # JSON object; "" leaves unchanged
    trace_id=None,
):
    ont = ONTOLOGIES.get(id)
    if not ont:
        return {"error": f"Ontology not found: {id}"}

    def _parse(s, default):
        if not s: return default
        try:   return json.loads(s) if isinstance(s, str) else s
        except: return default

    if name:           ont["name"]             = name
    if description:    ont["description"]       = description
    if domain:         ont["domain"]            = domain
    if context_hints:  ont["context_hints"]     = context_hints
    if entities:       ont["entities"]          = _parse(entities, ont["entities"])
    if relationships:  ont["relationships"]     = _parse(relationships, ont["relationships"])
    if processing_rules: ont["processing_rules"]= _parse(processing_rules, ont["processing_rules"])
    if memory_slots:   ont["memory_slots"]      = _parse(memory_slots, ont["memory_slots"])
    if tags:           ont["tags"]              = [t.strip() for t in tags.split(",") if t.strip()]
    if enabled is not None: ont["enabled"]      = enabled
    # ── OWL/SKOS field updates ─────────────────────────────────────────
    if iri:         ont["iri"]         = iri
    if pref_label:  ont["pref_label"]  = pref_label
    if alt_labels:  ont["alt_labels"]  = [t.strip() for t in alt_labels.split(",") if t.strip()]
    if imports:     ont["imports"]     = [t.strip() for t in imports.split(",") if t.strip()]
    if annotations: ont["annotations"] = _parse(annotations, ont.get("annotations", {})) or {}
    ont["updated"] = now_iso()

    await _save_async("ontologies", ont)
    await emit_event({"type": "ontologies.updated", "id": id})
    return ont


@capability("ontologies.delete", memory="off",
            http_method="POST", http_path="/ontologies/delete", http_tags=["ontologies"],
            description="Delete an ontology by id.")
async def ontologies_delete(id: str, trace_id=None):
    if id not in ONTOLOGIES:
        return {"error": f"Ontology not found: {id}"}
    ONTOLOGIES.pop(id)
    await _delete_async("ontologies", id)
    await emit_event({"type": "ontologies.deleted", "id": id})
    return {"deleted": id}


@capability("ontologies.apply", memory="auto",
            http_method="POST", http_path="/ontologies/apply", http_tags=["ontologies"],
            description="Apply an ontology to a piece of text or data. "
                        "The ontology's entity types, processing rules, and context hints "
                        "are injected as a structured system prompt and the LLM processes "
                        "the input accordingly.")
async def ontologies_apply(
    text:          str,
    ontology_id:   str  = "",       # specific ontology; empty = all enabled
    task:          str  = "extract", # extract | classify | tag | summarize | relate
    model:         str  = "",
    prefer_gpu:    bool = True,
    trace_id=None,
):
    # Resolve ontologies to apply
    if ontology_id:
        onts = [ONTOLOGIES[ontology_id]] if ontology_id in ONTOLOGIES else []
    else:
        onts = [o for o in ONTOLOGIES.values() if o.get("enabled", True)]

    if not onts:
        return {"error": "No enabled ontologies found", "result": None}

    # Build a rich system prompt from the combined ontology definitions
    sections = []
    for ont in onts:
        parts = [f"## Ontology: {ont['name']} (domain: {ont['domain']})"]
        if ont.get("context_hints"):
            parts.append(f"Context: {ont['context_hints']}")
        if ont.get("entities"):
            ents = "\n".join(
                f"  - {e['name']}: {e.get('description','')} "
                f"(attrs: {', '.join(e.get('attributes',[]))})"
                for e in ont["entities"]
            )
            parts.append(f"Entities:\n{ents}")
        if ont.get("relationships"):
            rels = "\n".join(
                f"  - {r['from']} --[{r['label']}]--> {r['to']}: {r.get('description','')}"
                for r in ont["relationships"]
            )
            parts.append(f"Relationships:\n{rels}")
        if ont.get("processing_rules"):
            rules = "\n".join(
                f"  [{r.get('priority',0)}] {r['trigger']} → {r['action']}"
                for r in sorted(ont["processing_rules"], key=lambda x: x.get("priority", 0), reverse=True)
            )
            parts.append(f"Processing rules:\n{rules}")
        if ont.get("memory_slots"):
            slots = ", ".join(f"{s['key']} ({s['type']})" for s in ont["memory_slots"])
            parts.append(f"Memory slots to populate: {slots}")
        sections.append("\n".join(parts))

    task_prompts = {
        "extract":   "Extract all entities, relationships, and populate memory slots defined in the ontology. Return JSON.",
        "classify":  "Classify the input according to the ontology's entity and domain definitions. Return JSON.",
        "tag":       "Tag the input using the ontology's entity types and relationship labels. Return JSON with a 'tags' array.",
        "summarize": "Summarise the input using the ontology's context and domain as a guide. Return plain text.",
        "relate":    "Identify relationships between concepts in the input using the ontology's relationship definitions. Return JSON.",
    }
    task_instr = task_prompts.get(task, task_prompts["extract"])

    system = (
        "You process data according to structured ontology definitions.\n\n"
        + "\n\n".join(sections)
        + f"\n\nTask: {task_instr}"
    )

    raw = await ollama_generate(
        text,
        system     = system,
        json_mode  = task != "summarize",
        model      = model or None,
        prefer_gpu = prefer_gpu,
    )

    result: dict = {"raw": raw}
    if task != "summarize":
        try:
            result["structured"] = json.loads(raw)
        except Exception:
            result["structured"] = None
            result["parse_error"] = True

    return {
        "result":            result,
        "task":              task,
        "ontologies_applied": [o["name"] for o in onts],
        "ontology_count":    len(onts),
    }


@capability("ontologies.infer", memory="on",
            http_method="POST", http_path="/ontologies/infer", http_tags=["ontologies"],
            description="Ask the LLM to infer an ontology structure from example data or a description.")
async def ontologies_infer(
    description:  str,
    example_data: str  = "",
    domain:       str  = "general",
    name:         str  = "",
    save:         bool = True,
    trace_id=None,
):
    """LLM drafts a full ontology definition from a plain-English description."""
    system = (
        "You are a knowledge engineering expert. Given a description of a domain, "
        "produce a structured ontology definition as valid JSON with these keys:\n"
        '{"entities":[{"name":"...","description":"...","attributes":["..."]}],'
        '"relationships":[{"from":"...","to":"...","label":"...","description":"..."}],'
        '"processing_rules":[{"trigger":"...","action":"...","priority":1}],'
        '"memory_slots":[{"key":"...","type":"string|number|boolean|list","description":"..."}],'
        '"context_hints":"..."}\n'
        "Return ONLY valid JSON. No preamble."
    )
    prompt = f"Domain: {domain}\nDescription: {description}"
    if example_data:
        prompt += f"\n\nExample data:\n{example_data[:1000]}"

    raw = await ollama_generate(prompt, system=system, json_mode=True, prefer_gpu=True)

    try:
        structure = json.loads(raw)
    except Exception:
        return {"error": "LLM returned invalid JSON", "raw": raw}

    result: dict = {"structure": structure, "domain": domain}

    if save:
        ont_name = name or description[:40].strip()
        saved = await ontologies_create(
            name          = ont_name,
            description   = description,
            domain        = domain,
            context_hints = structure.get("context_hints", ""),
            entities      = json.dumps(structure.get("entities", [])),
            relationships = json.dumps(structure.get("relationships", [])),
            processing_rules = json.dumps(structure.get("processing_rules", [])),
            memory_slots  = json.dumps(structure.get("memory_slots", [])),
            trace_id      = trace_id,
        )
        result["saved"] = saved

    return result


@capability("skills.active_context", memory="off",
            http_method="GET", http_path="/skills/context", http_tags=["skills"],
            description="Return the combined system prompt that the requested skills and ontologies "
                        "would inject. With no filters, returns every enabled skill and ontology. "
                        "When `skill_ids` / `ontology_ids` (comma-sep) are provided, ONLY those items "
                        "are rendered — used by the agent panel to preview exactly what a given "
                        "agent will receive, not the full library. "
                        "Set `format=json` to also receive the structured ontology payload that the "
                        "context block was rendered from, suitable for an LLM that prefers JSON.")
async def skills_active_context(
    skill_ids:    str = "",
    ontology_ids: str = "",
    format:       str = "text",   # "text" | "json"
    trace_id=None,
):
    # ── Parse filter sets ────────────────────────────────────────────────
    sk_filter = {s.strip() for s in skill_ids.split(",")    if s.strip()}
    on_filter = {o.strip() for o in ontology_ids.split(",") if o.strip()}

    def _skill_match(sid: str, s: dict) -> bool:
        if sk_filter:
            return sid in sk_filter            # explicit list — strict allowlist
        return s.get("enabled", True)          # no filter — every enabled skill

    def _ont_match(oid: str, o: dict) -> bool:
        if on_filter:
            return oid in on_filter
        return o.get("enabled", True)

    # ── Skills: emit content verbatim, tagged with name + type ───────────
    skill_parts = []
    for sid, s in SKILLS.items():
        if not _skill_match(sid, s):
            continue
        skill_parts.append(f"[SKILL: {s['name']} ({s.get('type','custom')})]\n{s.get('content','')}")

    # ── Ontologies: emit a rich structured block.
    # The earlier version of this endpoint reduced an ontology to
    # "Entities: name1, name2, ..." which threw away every description,
    # attribute, relationship, and rule the user had carefully defined.
    # The agent's actual injection path (context._resolve_ontologies_block)
    # emits the full structure, so this preview now matches what the agent
    # actually receives — otherwise the preview misled users into thinking
    # ontologies were a glorified word-list.
    ont_text_parts:    list[str] = []
    ont_struct_parts:  list[dict] = []
    for oid, o in ONTOLOGIES.items():
        if not _ont_match(oid, o):
            continue
        text_block, struct = _render_ontology_block(o)
        if text_block:
            ont_text_parts.append(text_block)
        ont_struct_parts.append(struct)

    out = {
        "skill_count":       len(skill_parts),
        "ontology_count":    len(ont_text_parts),
        "skill_context":     "\n\n".join(skill_parts),
        "ontology_context":  "\n\n".join(ont_text_parts),
        "combined":          "\n\n".join(skill_parts + ont_text_parts),
        "char_count":        sum(len(p) for p in skill_parts + ont_text_parts),
        "filtered":          bool(sk_filter or on_filter),
        "skill_ids_used":    sorted(sk_filter) if sk_filter else None,
        "ontology_ids_used": sorted(on_filter) if on_filter else None,
    }
    if format == "json":
        out["ontology_structures"] = ont_struct_parts
        out["combined_json"] = {
            "skills": [
                {"id": sid, "name": s.get("name"), "type": s.get("type"),
                 "content": s.get("content","")}
                for sid, s in SKILLS.items() if _skill_match(sid, s)
            ],
            "ontologies": ont_struct_parts,
        }
    return out


def _render_ontology_block(o: dict) -> tuple[str, dict]:
    """
    Render a single ontology to a Markdown-flavoured text block AND
    a structured dict that mirrors the same data.

    OWL/SKOS extensions are surfaced compactly when present:
      Entities:
        - EntityName [pref:Preferred Label] (alt: a, b) ⊆ Parent ≡ Eq ≢ Disj
            description (attrs: …)
            ↑ broader: …  ↓ narrower: …  ↔ related: …
            ⊆some on_property → target
      Relationships:
        - From --[LABEL inverse=X props=Functional,Transitive]--> To
    """
    name   = o.get("name", "?")
    domain = o.get("domain", "general")
    pref   = o.get("pref_label") or ""
    alts   = o.get("alt_labels") or []
    iri    = o.get("iri") or ""
    parts  = [f"## Ontology: {name}{' [pref:'+pref+']' if pref else ''} (domain: {domain})"]
    if iri:    parts.append(f"IRI: {iri}")
    if alts:   parts.append(f"Aka: {', '.join(alts)}")

    if o.get("description"):
        parts.append(f"Description: {o['description']}")
    if o.get("context_hints"):
        parts.append(f"Context: {o['context_hints']}")

    imports = o.get("imports") or []
    if imports:
        parts.append("Imports: " + ", ".join(imports[:6])
                      + ("…" if len(imports) > 6 else ""))

    ents = o.get("entities") or []
    if ents:
        parts.append("Entities:")
        for e in ents:
            ename = e.get("name", "?")
            edesc = e.get("description", "")
            attrs = e.get("attributes") or []
            line_bits = [f"  - {ename}"]
            if e.get("pref_label"):
                line_bits.append(f"[pref:{e['pref_label']}]")
            if e.get("alt_labels"):
                line_bits.append(f"(alt: {', '.join(e['alt_labels'])})")
            if e.get("sub_class_of"):
                line_bits.append("⊆ " + ", ".join(e['sub_class_of']))
            if e.get("equivalent_to"):
                line_bits.append("≡ " + ", ".join(e['equivalent_to']))
            if e.get("disjoint_with"):
                line_bits.append("≢ " + ", ".join(e['disjoint_with']))
            line = " ".join(line_bits)
            if edesc:
                line += f": {edesc}"
            if attrs:
                # Attributes can now be strings OR dicts
                attr_strs = []
                for a in attrs:
                    if isinstance(a, dict):
                        n = a.get("name", "?")
                        rt = a.get("range_type") or "string"
                        attr_strs.append(f"{n}:{rt}" + ("!" if a.get("functional") else ""))
                    else:
                        attr_strs.append(str(a))
                line += f" (attrs: {', '.join(attr_strs)})"
            parts.append(line)

            # SKOS conceptual ties
            for tie, sym in (("broader","↑"),("narrower","↓"),("related","↔")):
                vals = e.get(tie) or []
                if vals:
                    parts.append(f"      {sym} {tie}: {', '.join(vals)}")

            # OWL restrictions
            for restr in (e.get("restrictions") or []):
                kind  = restr.get("kind", "?")
                onp   = restr.get("on_property", "?")
                val   = restr.get("value", "?")
                qual  = restr.get("qualifier", "")
                rs    = f"      ⊆ {kind} on {onp} → {val}"
                if qual:
                    rs += f" :: {qual}"
                parts.append(rs)

    rels = o.get("relationships") or []
    if rels:
        parts.append("Relationships:")
        for r in rels:
            frm = r.get("from") or r.get("source") or "?"
            to  = r.get("to")   or r.get("target") or "?"
            lbl = r.get("label") or r.get("relation") or r.get("type") or "REL"
            extras = []
            if r.get("inverse_of"):
                extras.append(f"inverse={r['inverse_of']}")
            if r.get("characteristics"):
                extras.append("props=" + ",".join(r["characteristics"]))
            if r.get("sub_property_of"):
                extras.append("⊆" + ",".join(r["sub_property_of"]))
            tag = (" " + " ".join(extras)) if extras else ""
            parts.append(f"  - {frm} --[{lbl}{tag}]--> {to}")

    rules = o.get("processing_rules") or []
    if rules:
        parts.append("Processing rules:")
        for r in sorted(rules, key=lambda x: x.get("priority", 0), reverse=True):
            trig = r.get("trigger", "")
            act  = r.get("action",  "")
            pri  = r.get("priority", 0)
            parts.append(f"  - [{pri}] {trig} -> {act}")

    slots = o.get("memory_slots") or []
    if slots:
        parts.append("Memory slots:")
        for s in slots:
            sk = s.get("key") or s.get("name") or "?"
            st = s.get("type", "")
            sd = s.get("description", "")
            parts.append(f"  - {sk}{f' ({st})' if st else ''}{f': {sd}' if sd else ''}")

    text = "\n".join(parts)
    struct = {
        "id":               o.get("id"),
        "name":             name,
        "domain":           domain,
        "description":      o.get("description", ""),
        "context_hints":    o.get("context_hints", ""),
        "iri":              iri,
        "pref_label":       pref,
        "alt_labels":       alts,
        "imports":          imports,
        "annotations":      o.get("annotations", {}),
        "entities":         ents,
        "relationships":    rels,
        "processing_rules": rules,
        "memory_slots":     slots,
    }
    return text, struct


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP PERSISTENCE RELOAD
# ─────────────────────────────────────────────────────────────────────────────

schedule(_startup_load, interval=999999, name="skills_startup_load")

# Trigger immediately at import time (Redis may not be ready yet at import,
# so also scheduled above for the first run after lifespan brings Redis up)
try:
    loop = _asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(_startup_load())
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────────────
# UI PANELS
# ─────────────────────────────────────────────────────────────────────────────

register_ui(
    "skills-editor",
    "Skills",
    "🧠",
    """
<div style="display:flex;flex-direction:column;gap:12px;height:100%">

  <!-- Top toolbar -->
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <select id="skillTypeFilter" onchange="skillsLoad()" style="width:150px">
      <option value="">All types</option>
      <option value="system_prompt">System Prompt</option>
      <option value="persona">Persona</option>
      <option value="few_shot">Few-Shot</option>
      <option value="chain_of_thought">Chain of Thought</option>
      <option value="tool_hint">Tool Hint</option>
      <option value="custom">Custom</option>
    </select>
    <input id="skillTagFilter" placeholder="Filter by tag…" style="width:130px" oninput="skillsLoad()">
    <button class="btn primary sm" onclick="skillsShowCreate()">+ New Skill</button>
    <button class="btn sm teal" onclick="skillsCompose()">🪄 Compose with LLM</button>
    <button class="btn sm" onclick="skillsLoad()">↻</button>
    <span id="skillCount" style="color:var(--dim2);font-size:10px;margin-left:4px"></span>
  </div>

  <div class="g2" style="flex:1;min-height:0;gap:10px">

    <!-- LEFT: skill list -->
    <div style="overflow-y:auto;display:flex;flex-direction:column;gap:6px" id="skillList">
      <span style="color:var(--dim);font-size:11px">Loading…</span>
    </div>

    <!-- RIGHT: editor / detail -->
    <div style="display:flex;flex-direction:column;gap:8px">
      <div id="skillEditor" style="display:none;flex-direction:column;gap:7px">
        <div style="font-size:10px;color:var(--dim2);text-transform:uppercase;letter-spacing:.8px" id="skillEditorTitle">New Skill</div>
        <input id="seId"      type="hidden">
        <div class="row"><label>Name</label><input id="seName" placeholder="My Skill" style="flex:1"></div>
        <div class="row"><label>Type</label>
          <select id="seType" style="flex:1">
            <option value="system_prompt">System Prompt</option>
            <option value="persona">Persona</option>
            <option value="few_shot">Few-Shot Examples</option>
            <option value="chain_of_thought">Chain of Thought</option>
            <option value="tool_hint">Tool Hint</option>
            <option value="custom">Custom</option>
          </select>
        </div>
        <div class="row"><label>Description</label><input id="seDesc" placeholder="What this skill does…" style="flex:1"></div>
        <div class="row"><label>Tags</label><input id="seTags" placeholder="comma, separated" style="flex:1"></div>
        <div>
          <div style="font-size:9.5px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.8px">
            Content <span style="color:var(--dim2)" id="seVarList"></span>
          </div>
          <textarea id="seContent" style="min-height:140px;font-size:11px" placeholder="Skill prompt content. Use {{variable}} for template slots." oninput="skillsDetectVars()"></textarea>
        </div>
        <div class="row"><label>Enabled</label><input type="checkbox" id="seEnabled" checked style="margin-left:4px"></div>
        <div style="display:flex;gap:7px;flex-wrap:wrap">
          <button class="btn primary" onclick="skillsSave()">💾 Save</button>
          <button class="btn" onclick="skillsCancel()">Cancel</button>
          <button class="btn danger sm" onclick="skillsDelete()" id="seDeleteBtn" style="display:none">Delete</button>
        </div>
        <div class="status-bar" id="seStatus"></div>
      </div>

      <!-- Apply panel -->
      <div id="skillApplyPanel" style="display:none;flex-direction:column;gap:7px">
        <div style="font-size:10px;color:var(--dim2);text-transform:uppercase;letter-spacing:.8px">Apply Skills to Prompt</div>
        <textarea id="saPrompt" style="min-height:80px;font-size:11px" placeholder="Your prompt here…"></textarea>
        <div id="saVarInputs"></div>
        <div class="row"><label>Skill IDs</label><input id="saIds" placeholder="comma-separated ids (empty=all enabled)" style="flex:1"></div>
        <button class="btn primary" onclick="skillsApply()">▶ Apply + Generate</button>
        <div class="status-bar" id="saStatus"></div>
        <div class="result" id="saResult" style="min-height:60px"></div>
      </div>

      <!-- Compose panel -->
      <div id="skillComposePanel" style="display:none;flex-direction:column;gap:7px">
        <div style="font-size:10px;color:var(--dim2);text-transform:uppercase;letter-spacing:.8px">Compose Skill with LLM</div>
        <textarea id="scDesc" style="min-height:72px;font-size:11px" placeholder="Describe what you want the skill to do…"></textarea>
        <div class="row"><label>Type</label>
          <select id="scType" style="flex:1">
            <option value="system_prompt">System Prompt</option>
            <option value="persona">Persona</option>
            <option value="few_shot">Few-Shot</option>
            <option value="chain_of_thought">Chain of Thought</option>
          </select>
        </div>
        <div class="row"><label>Name</label><input id="scName" placeholder="auto-generated if empty" style="flex:1"></div>
        <div class="row"><label>Save?</label><input type="checkbox" id="scSave" checked style="margin-left:4px"></div>
        <button class="btn primary" onclick="skillsDoCompose()">🪄 Compose</button>
        <div class="status-bar" id="scStatus"></div>
        <div class="result" id="scResult" style="min-height:60px"></div>
      </div>
    </div>
  </div>
</div>
""",
    r"""
(function(){
  window.selectedSkillId = null;
  var selectedSkillId = null; // alias for closure use

  window.skillsLoad = async function() {
    const type = document.getElementById('skillTypeFilter').value;
    const tag  = document.getElementById('skillTagFilter').value;
    const url  = window._veraBase + '/skills' + (type||tag ? '?type='+type+'&tag='+tag : '');
    const res  = await fetch(url).then(r=>r.json()).catch(()=>null);
    const list = document.getElementById('skillList');
    const cnt  = document.getElementById('skillCount');
    if (!res || res.error) { list.innerHTML='<span style="color:var(--err)">Failed to load</span>'; return; }
    const skills = res.skills || [];
    cnt.textContent = skills.length + ' skills';
    if (!skills.length) { list.innerHTML='<span style="color:var(--dim);font-size:11px">No skills yet — create one!</span>'; return; }
    list.innerHTML = skills.map(s=>`
      <div onclick="skillsSelect('${s.id}')" id="sk_${s.id}"
           style="padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:5px;cursor:pointer;transition:border-color .15s"
           onmouseover="this.style.borderColor='var(--acc)'" onmouseout="this.style.borderColor=selectedSkillId==='${s.id}'?'var(--acc)':'var(--border)'">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:3px">
          <span style="font-weight:600;font-size:12px;font-family:var(--mono)">${s.name}</span>
          <span style="font-size:9px;padding:1px 5px;background:rgba(79,142,247,.1);border:1px solid rgba(79,142,247,.25);border-radius:2px;color:var(--acc)">${s.type}</span>
          ${s.enabled?'':'<span style="font-size:9px;color:var(--dim)">disabled</span>'}
        </div>
        <div style="font-size:10.5px;color:var(--dim2);margin-bottom:4px">${s.description||'—'}</div>
        ${s.content?`<div style="font-size:9.5px;color:var(--dim2);font-family:monospace;background:rgba(0,0,0,.2);padding:3px 6px;border-radius:3px;margin-bottom:3px;white-space:pre-wrap;max-height:54px;overflow:hidden">${s.content.slice(0,120)}${s.content.length>120?'…':''}</div>`:''}
        ${s.variables?.length?`<div style="font-size:9.5px;color:var(--acc3)">${s.variables.map(v=>`{{${v}}}`).join(' ')}</div>`:''}
        ${s.tags?.length?`<div style="margin-top:3px">${s.tags.map(t=>`<span style="display:inline-block;font-size:8.5px;padding:1px 5px;background:rgba(255,255,255,.04);border-radius:10px;color:var(--dim2);margin:1px">${t}</span>`).join('')}</div>`:''}
        <div style="display:flex;gap:4px;margin-top:5px">
          <button class="btn sm teal" style="font-size:9px;padding:2px 7px" onclick="event.stopPropagation();skillsAssignAgent('${s.id}','${s.name}')">+ Agent</button>
          <button class="btn sm" style="font-size:9px;padding:2px 7px" onclick="event.stopPropagation();skillsCopy('${s.id}')">📋</button>
        </div>
      </div>`).join('');
  };

  window.skillsSelect = function(id) {
    selectedSkillId = id; window.selectedSkillId = id;
    // Highlight
    document.querySelectorAll('[id^="sk_"]').forEach(el=>{
      el.style.borderColor = el.id === 'sk_'+id ? 'var(--acc)' : 'var(--border)';
    });
    // Load into editor
    fetch(window._veraBase+'/skills/get?id='+id).then(r=>r.json()).then(s=>{
      if (s.error) return;
      document.getElementById('skillEditorTitle').textContent = 'Edit Skill';
      document.getElementById('seId').value      = s.id;
      document.getElementById('seName').value    = s.name;
      document.getElementById('seType').value    = s.type;
      document.getElementById('seDesc').value    = s.description||'';
      document.getElementById('seTags').value    = (s.tags||[]).join(', ');
      document.getElementById('seContent').value = s.content;
      document.getElementById('seEnabled').checked = s.enabled!==false;
      document.getElementById('seDeleteBtn').style.display = 'inline-flex';
      skillsDetectVars();
      showPanel('editor');
    });
  };

  window.skillsShowCreate = function() {
    selectedSkillId=null; window.selectedSkillId=null;
    ['seId','seName','seDesc','seTags','seContent'].forEach(id=>document.getElementById(id).value='');
    document.getElementById('seType').value='system_prompt';
    document.getElementById('seEnabled').checked=true;
    document.getElementById('seDeleteBtn').style.display='none';
    document.getElementById('skillEditorTitle').textContent='New Skill';
    document.getElementById('seVarList').textContent='';
    showPanel('editor');
  };

  window.skillsSave = async function() {
    const id = document.getElementById('seId').value;
    const body = {
      name:    document.getElementById('seName').value,
      content: document.getElementById('seContent').value,
      description: document.getElementById('seDesc').value,
      type:    document.getElementById('seType').value,
      tags:    document.getElementById('seTags').value,
      enabled: document.getElementById('seEnabled').checked,
    };
    const url = id ? '/skills/update' : '/skills';
    if (id) body.id = id;
    // Pass session_id on create so the skill gets linked to the graph session
    if (!id && window._chatSessionId) body.session_id = window._chatSessionId;
    const st = document.getElementById('seStatus');
    st.textContent = '⟳ Saving…'; st.className='status-bar';
    const res = await fetch(window._veraBase+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(e=>({error:e.message}));
    if (res.error) { st.textContent='✗ '+res.error; st.className='status-bar err'; return; }
    st.textContent='✓ Saved'; st.className='status-bar ok';
    await skillsLoad();
  };

  window.skillsCancel = function() { showPanel('none'); selectedSkillId=null; window.selectedSkillId=null; };

  window.skillsDelete = async function() {
    const id = document.getElementById('seId').value;
    if (!id || !confirm('Delete this skill?')) return;
    await fetch(window._veraBase+'/skills/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    showPanel('none'); await skillsLoad();
  };

  window.skillsDetectVars = function() {
    const content = document.getElementById('seContent').value;
    const vars = [...new Set(content.match(/\{\{(\w+)\}\}/g)||[])];
    document.getElementById('seVarList').textContent = vars.length ? '— vars: '+vars.join(', ') : '';
  };

  window.skillsApplyPanel = function() { showPanel('apply'); };

  window.skillsApply = async function() {
    const prompt = document.getElementById('saPrompt').value.trim();
    const ids    = document.getElementById('saIds').value.trim();
    const st     = document.getElementById('saStatus');
    const resEl  = document.getElementById('saResult');
    if(!prompt){ st.textContent='✗ Enter a prompt'; st.className='status-bar err'; return; }
    st.textContent='⟳ Applying…'; st.className='status-bar';
    resEl.textContent='';

    // Get the assembled system prompt from the server (skills stacking, no LLM)
    // by calling skills/active_context which returns the combined system prompt
    const ctxRes = await fetch(window._veraBase+'/skills/context'+(ids?`?skill_ids=${encodeURIComponent(ids)}`:''))
      .then(r=>r.json()).catch(()=>null);

    const system = ctxRes?.combined || ctxRes?.system_prompt || '';
    const skillCount = (ctxRes?.skill_count||0) + (ctxRes?.ontology_count||0);

    // Stream the generation with the assembled system prompt
    const text = await streamInto(resEl, prompt, {system, prefer_gpu: true});
    st.textContent = text.startsWith('✗')
      ? text
      : `✓ Applied ${skillCount} skill${skillCount!==1?'s':''} — ${text.split(/\s+/).length} words generated`;
    st.className = text.startsWith('✗') ? 'status-bar err' : 'status-bar ok';
  };

  window.skillsCompose = function() { showPanel('compose'); };

  // Assign this skill to an agent via the agent API
  window.skillsAssignAgent = async function(skillId, skillName) {
    // Load agent list into a small popover dropdown
    const agents = await fetch(window._veraBase+'/agents/list').then(r=>r.json()).catch(()=>null);
    const list = agents?.agents||[];
    if(!list.length){ alert('No agents found — create an agent first.'); return; }

    // Show inline agent picker near the button
    const existingPicker = document.getElementById('skill-agent-picker');
    if(existingPicker) existingPicker.remove();

    const picker = document.createElement('div');
    picker.id = 'skill-agent-picker';
    picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9999;background:var(--bg1);border:1px solid var(--acc);border-radius:8px;padding:12px;min-width:260px;box-shadow:0 8px 32px rgba(0,0,0,.4)';
    picker.innerHTML = `<div style="font-size:11px;font-weight:600;margin-bottom:8px;color:var(--acc)">Assign skill <em>"${skillName}"</em> to agent</div>
      <div style="display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto">
        ${list.map(a=>`<label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:4px 6px;border-radius:4px;background:var(--bg2);font-size:10.5px">
          <input type="checkbox" id="sap_${a.id}" ${(a.skill_ids||[]).includes(skillId)?'checked':''}>
          ${a.avatar||'🤖'} ${a.label||a.name}
        </label>`).join('')}
      </div>
      <div style="display:flex;gap:6px;margin-top:10px">
        <button class="btn primary sm" style="flex:1" onclick="skillsSaveAgentAssign('${skillId}')">Save</button>
        <button class="btn sm" style="flex:1" onclick="document.getElementById('skill-agent-picker')?.remove()">Cancel</button>
      </div>`;
    document.body.appendChild(picker);
  };

  window.skillsSaveAgentAssign = async function(skillId) {
    const picker = document.getElementById('skill-agent-picker');
    const agents = await fetch(window._veraBase+'/agents/list').then(r=>r.json()).catch(()=>null);
    const list = agents?.agents||[];
    for(const a of list){
      const cb = document.getElementById('sap_'+a.id);
      if(!cb) continue;
      const currentIds = [...(a.skill_ids||[])];
      const hasIt = currentIds.includes(skillId);
      if(cb.checked && !hasIt) currentIds.push(skillId);
      else if(!cb.checked && hasIt) currentIds.splice(currentIds.indexOf(skillId),1);
      else continue;
      await fetch(window._veraBase+'/agents/create',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...a, agent_id:a.id, domain_caps:(a.domain_caps||[]).join(','), skill_ids:currentIds.join(','), ontology_ids:(a.ontology_ids||[]).join(',')})
      }).catch(()=>null);
    }
    picker?.remove();
    const st = document.getElementById('seStatus');
    if(st){ st.textContent='✓ Agent assignments saved'; st.className='status-bar ok'; }
  };

  window.skillsDoCompose = async function() {
    const desc = document.getElementById('scDesc').value.trim();
    if (!desc) return;
    const st   = document.getElementById('scStatus');
    const type = document.getElementById('scType').value;
    const name = document.getElementById('scName').value;
    const save = document.getElementById('scSave').checked;
    st.textContent='⟳ Composing…'; st.className='status-bar';

    const resEl = document.getElementById('scResult');
    const typeHints = {
      system_prompt: 'Write a clear system prompt that shapes overall assistant behaviour.',
      persona:       'Write a concise persona / role definition for an AI assistant.',
      few_shot:      'Write 2-4 input/output example pairs demonstrating the desired behaviour.',
      chain_of_thought: 'Write step-by-step reasoning instructions the LLM should follow.',
    };
    const prompt = `Write a skill of type "${type}" for the following purpose:\n\n${desc}\n\n${typeHints[type]||''}\n\nReturn ONLY the skill content, no explanations.`;

    const content = await (window.streamChatInto
      ? window.streamChatInto(resEl, prompt, 'assistant')
      : (async()=>{ resEl.textContent='⧗ Generating…'; const r=await fetch(window._veraBase+'/skills/compose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({description:desc,type,name,save})}).then(x=>x.json()).catch(e=>({error:e.message})); resEl.textContent=r.error||r.content||''; return r.content||''; })());

    if (!content || content.startsWith('✗')) {
      st.textContent = '✗ Failed'; st.className = 'status-bar err'; return;
    }

    // Save if requested
    if (save) {
      const body = {description: desc, type, name: name||desc.slice(0,40), content, save: true};
      const res = await fetch(window._veraBase+'/skills/compose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(e=>({error:e.message}));
      st.textContent = res.error ? '✗ '+res.error : '✓ Composed & saved';
      st.className   = res.error ? 'status-bar err' : 'status-bar ok';
      if (!res.error) await skillsLoad();
    } else {
      st.textContent = '✓ Composed (not saved)'; st.className = 'status-bar ok';
    }
  };

  function showPanel(name) {
    document.getElementById('skillEditor').style.display      = name==='editor'  ? 'flex' : 'none';
    document.getElementById('skillApplyPanel').style.display  = name==='apply'   ? 'flex' : 'none';
    document.getElementById('skillComposePanel').style.display= name==='compose' ? 'flex' : 'none';
  }

  // Initial load
  skillsLoad();
})();
""",
    ui_caps=['skill.create', 'skill.list', 'skill.get', 'skill.update', 'skill.delete',
             'skill.apply', 'skill.compose'],
    mode="inject",   # UI served via standalone /ui/panels/skills-panel iframe; mode=inject here for ui_caps tracking only
    tab_order=60,
)


register_ui(
    "ontologies-browser",
    "Ontologies",
    "🗂",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input id="ontDomainFilter" placeholder="Filter by domain…" style="width:150px" oninput="ontsLoad()">
    <button class="btn primary sm" onclick="ontShowCreate()">+ New Ontology</button>
    <button class="btn sm teal" onclick="ontInfer()">🧬 Infer from Data</button>
    <button class="btn sm amber" onclick="ontApplyPanel()">⚙ Apply</button>
    <button class="btn sm" onclick="ontsLoad()">↻</button>
    <span id="ontCount" style="color:var(--dim2);font-size:10px"></span>
  </div>

  <div class="g2" style="gap:10px">
    <!-- List -->
    <div style="overflow-y:auto;max-height:440px;display:flex;flex-direction:column;gap:6px" id="ontList">
      <span style="color:var(--dim);font-size:11px">Loading…</span>
    </div>
    <!-- Editor / viewer -->
    <div style="display:flex;flex-direction:column;gap:7px">

      <div id="ontEditor" style="display:none;flex-direction:column;gap:7px">
        <div style="font-size:10px;color:var(--dim2);text-transform:uppercase;letter-spacing:.8px" id="ontEditorTitle">New Ontology</div>
        <input id="oeId" type="hidden">
        <div class="row"><label>Name</label><input id="oeName" style="flex:1"></div>
        <div class="row"><label>Domain</label><input id="oeDomain" placeholder="general" style="flex:1"></div>
        <div class="row"><label>Description</label><input id="oeDesc" style="flex:1"></div>
        <div>
          <div style="font-size:9.5px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.8px">Context Hints (free-form, for LLM)</div>
          <textarea id="oeHints" style="min-height:60px;font-size:11px" placeholder="Tell the LLM how to interpret data in this domain…"></textarea>
        </div>
        <div>
          <div style="font-size:9.5px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.8px">Entities (JSON array)</div>
          <textarea id="oeEntities" style="min-height:70px;font-size:10px;font-family:var(--mono)" placeholder='[{"name":"Person","description":"A human individual","attributes":["name","age"]}]'></textarea>
        </div>
        <div>
          <div style="font-size:9.5px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.8px">Relationships (JSON array)</div>
          <textarea id="oeRels" style="min-height:60px;font-size:10px;font-family:var(--mono)" placeholder='[{"from":"Person","to":"Organisation","label":"WORKS_AT","description":"Employment"}]'></textarea>
        </div>
        <div>
          <div style="font-size:9.5px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.8px">Processing Rules (JSON array)</div>
          <textarea id="oeRules" style="min-height:60px;font-size:10px;font-family:var(--mono)" placeholder='[{"trigger":"date mentioned","action":"extract ISO date","priority":1}]'></textarea>
        </div>
        <div>
          <div style="font-size:9.5px;color:var(--dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.8px">Memory Slots (JSON array)</div>
          <textarea id="oeSlots" style="min-height:50px;font-size:10px;font-family:var(--mono)" placeholder='[{"key":"speaker","type":"string","description":"Current speaker identity"}]'></textarea>
        </div>
        <div class="row"><label>Tags</label><input id="oeTags" placeholder="comma, separated" style="flex:1"></div>
        <div class="row"><label>Enabled</label><input type="checkbox" id="oeEnabled" checked style="margin-left:4px"></div>
        <div style="display:flex;gap:7px;flex-wrap:wrap">
          <button class="btn primary" onclick="ontSave()">💾 Save</button>
          <button class="btn" onclick="ontCancel()">Cancel</button>
          <button class="btn danger sm" onclick="ontDelete()" id="oeDeleteBtn" style="display:none">Delete</button>
        </div>
        <div class="status-bar" id="oeStatus"></div>
      </div>

      <div id="ontApply" style="display:none;flex-direction:column;gap:7px">
        <div style="font-size:10px;color:var(--dim2);text-transform:uppercase;letter-spacing:.8px">Apply Ontology to Data</div>
        <textarea id="oaText" style="min-height:90px;font-size:11px" placeholder="Paste data / text to process…"></textarea>
        <div class="row"><label>Ontology ID</label><input id="oaId" placeholder="empty=all enabled" style="flex:1"></div>
        <div class="row"><label>Task</label>
          <select id="oaTask" style="flex:1">
            <option value="extract">Extract entities</option>
            <option value="classify">Classify</option>
            <option value="tag">Tag</option>
            <option value="relate">Find relationships</option>
            <option value="summarize">Summarize</option>
          </select>
        </div>
        <button class="btn primary" onclick="ontApplyRun()">▶ Apply</button>
        <div class="status-bar" id="oaStatus"></div>
        <div class="result" id="oaResult" style="min-height:60px"></div>
      </div>

      <div id="ontInferPanel" style="display:none;flex-direction:column;gap:7px">
        <div style="font-size:10px;color:var(--dim2);text-transform:uppercase;letter-spacing:.8px">Infer Ontology from Data</div>
        <textarea id="oiDesc" style="min-height:60px;font-size:11px" placeholder="Describe the domain…"></textarea>
        <textarea id="oiData" style="min-height:70px;font-size:11px" placeholder="Optional: paste example data…"></textarea>
        <div class="row"><label>Domain</label><input id="oiDomain" value="general" style="flex:1"></div>
        <div class="row"><label>Name</label><input id="oiName" placeholder="auto-generated" style="flex:1"></div>
        <div class="row"><label>Save?</label><input type="checkbox" id="oiSave" checked style="margin-left:4px"></div>
        <button class="btn primary" onclick="ontDoInfer()">🧬 Infer</button>
        <div class="status-bar" id="oiStatus"></div>
        <div class="result" id="oiResult" style="min-height:60px"></div>
      </div>
    </div>
  </div>
</div>
""",
    r"""
(function(){
  let selectedOntId = null;

  window.ontsLoad = async function() {
    const domain = document.getElementById('ontDomainFilter').value;
    const url = window._veraBase + '/ontologies' + (domain ? '?domain='+domain : '');
    const res = await fetch(url).then(r=>r.json()).catch(()=>null);
    const list = document.getElementById('ontList');
    const cnt  = document.getElementById('ontCount');
    if (!res) { list.innerHTML='<span style="color:var(--err)">Failed</span>'; return; }
    const items = res.ontologies || [];
    cnt.textContent = items.length + ' ontologies';
    if (!items.length) { list.innerHTML='<span style="color:var(--dim);font-size:11px">No ontologies yet</span>'; return; }
    list.innerHTML = items.map(o=>`
      <div onclick="ontSelect('${o.id}')" id="ont_${o.id}"
           style="padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:5px;cursor:pointer;transition:border-color .15s"
           onmouseover="this.style.borderColor='var(--acc2)'" onmouseout="this.style.borderColor='var(--border)'">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:3px">
          <span style="font-weight:600;font-size:12px;font-family:var(--mono)">${o.name}</span>
          <span style="font-size:9px;padding:1px 5px;background:rgba(0,212,170,.1);border:1px solid rgba(0,212,170,.25);border-radius:2px;color:var(--acc2)">${o.domain}</span>
          ${o.enabled?'':'<span style="font-size:9px;color:var(--dim)">disabled</span>'}
        </div>
        <div style="font-size:10.5px;color:var(--dim2);margin-bottom:3px">${o.description||'—'}</div>
        <div style="font-size:9.5px;color:var(--dim2)">
          ${o.entities?.length||0} entities · ${o.relationships?.length||0} rels · ${o.processing_rules?.length||0} rules
        </div>
        <div style="display:flex;gap:4px;margin-top:5px">
          <button class="btn sm teal" style="font-size:9px;padding:2px 7px"
            onclick="event.stopPropagation();ontAssignAgent('${o.id}','${o.name}')">+ Agent</button>
          <button class="btn sm" style="font-size:9px;padding:2px 7px"
            onclick="event.stopPropagation();ontCopyHints('${o.id}')">📋 Hints</button>
        </div>
      </div>`).join('');
  };

  window.ontSelect = function(id) {
    selectedOntId = id;
    fetch(window._veraBase+'/ontologies/get?id='+id).then(r=>r.json()).then(o=>{
      if (o.error) return;
      document.getElementById('ontEditorTitle').textContent = 'Edit Ontology';
      document.getElementById('oeId').value      = o.id;
      document.getElementById('oeName').value    = o.name;
      document.getElementById('oeDomain').value  = o.domain||'';
      document.getElementById('oeDesc').value    = o.description||'';
      document.getElementById('oeHints').value   = o.context_hints||'';
      document.getElementById('oeEntities').value= JSON.stringify(o.entities||[],null,2);
      document.getElementById('oeRels').value    = JSON.stringify(o.relationships||[],null,2);
      document.getElementById('oeRules').value   = JSON.stringify(o.processing_rules||[],null,2);
      document.getElementById('oeSlots').value   = JSON.stringify(o.memory_slots||[],null,2);
      document.getElementById('oeTags').value    = (o.tags||[]).join(', ');
      document.getElementById('oeEnabled').checked = o.enabled!==false;
      document.getElementById('oeDeleteBtn').style.display='inline-flex';
      ontShowPanel('editor');
    });
  };

  window.ontShowCreate = function() {
    selectedOntId=null;
    ['oeId','oeName','oeDomain','oeDesc','oeHints','oeTags'].forEach(id=>document.getElementById(id).value='');
    ['oeEntities','oeRels','oeRules','oeSlots'].forEach(id=>document.getElementById(id).value='[]');
    document.getElementById('oeEnabled').checked=true;
    document.getElementById('oeDeleteBtn').style.display='none';
    document.getElementById('ontEditorTitle').textContent='New Ontology';
    ontShowPanel('editor');
  };

  window.ontSave = async function() {
    const id = document.getElementById('oeId').value;
    const body = {
      name:         document.getElementById('oeName').value,
      description:  document.getElementById('oeDesc').value,
      domain:       document.getElementById('oeDomain').value||'general',
      context_hints:document.getElementById('oeHints').value,
      entities:     document.getElementById('oeEntities').value,
      relationships:document.getElementById('oeRels').value,
      processing_rules: document.getElementById('oeRules').value,
      memory_slots: document.getElementById('oeSlots').value,
      tags:         document.getElementById('oeTags').value,
      enabled:      document.getElementById('oeEnabled').checked,
    };
    if (id) body.id = id;
    // Pass session_id so new ontologies get linked to the current graph session
    if (!id && window._chatSessionId) body.session_id = window._chatSessionId;
    const url = id ? '/ontologies/update' : '/ontologies';
    const st  = document.getElementById('oeStatus');
    st.textContent='⟳ Saving…'; st.className='status-bar';
    const res = await fetch(window._veraBase+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(e=>({error:e.message}));
    if (res.error) { st.textContent='✗ '+res.error; st.className='status-bar err'; return; }
    st.textContent='✓ Saved'; st.className='status-bar ok';
    await ontsLoad();
  };

  window.ontCancel = function() { ontShowPanel('none'); };

  window.ontCopyHints = function(id) {
    fetch(window._veraBase+'/ontologies/get?id='+id).then(r=>r.json()).then(o=>{
      if(o.error) return;
      const txt = o.context_hints||o.description||o.name||'';
      navigator.clipboard?.writeText(txt).then(()=>{}).catch(()=>{});
    });
  };

  // Apply ontology context hints to the currently-open agent's system prompt
  window.ontAssignAgent = async function(ontId, ontName) {
    const agents = await fetch(window._veraBase+'/agents/list').then(r=>r.json()).catch(()=>null);
    const list = agents?.agents||[];
    if(!list.length){ alert('No agents found — create an agent first.'); return; }

    const existingPicker = document.getElementById('ont-agent-picker');
    if(existingPicker) existingPicker.remove();

    const picker = document.createElement('div');
    picker.id = 'ont-agent-picker';
    picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:9999;background:var(--bg1);border:1px solid var(--acc2);border-radius:8px;padding:12px;min-width:260px;box-shadow:0 8px 32px rgba(0,0,0,.4)';
    picker.innerHTML = `<div style="font-size:11px;font-weight:600;margin-bottom:8px;color:var(--acc2)">Assign ontology <em>"${ontName}"</em> to agent</div>
      <div style="display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto">
        ${list.map(a=>`<label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:4px 6px;border-radius:4px;background:var(--bg2);font-size:10.5px">
          <input type="checkbox" id="oap_${a.id}" ${(a.ontology_ids||[]).includes(ontId)?'checked':''}>
          ${a.avatar||'🤖'} ${a.label||a.name}
        </label>`).join('')}
      </div>
      <div style="display:flex;gap:6px;margin-top:10px">
        <button class="btn primary sm" style="flex:1" onclick="ontSaveAgentAssign('${ontId}')">Save</button>
        <button class="btn sm" style="flex:1" onclick="document.getElementById('ont-agent-picker')?.remove()">Cancel</button>
      </div>`;
    document.body.appendChild(picker);
  };

  window.ontSaveAgentAssign = async function(ontId) {
    const picker = document.getElementById('ont-agent-picker');
    const agents = await fetch(window._veraBase+'/agents/list').then(r=>r.json()).catch(()=>null);
    const list = agents?.agents||[];
    for(const a of list){
      const cb = document.getElementById('oap_'+a.id);
      if(!cb) continue;
      const currentIds = [...(a.ontology_ids||[])];
      const hasIt = currentIds.includes(ontId);
      if(cb.checked && !hasIt) currentIds.push(ontId);
      else if(!cb.checked && hasIt) currentIds.splice(currentIds.indexOf(ontId),1);
      else continue;
      await fetch(window._veraBase+'/agents/create',{
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...a, agent_id:a.id, domain_caps:(a.domain_caps||[]).join(','), skill_ids:(a.skill_ids||[]).join(','), ontology_ids:currentIds.join(',')})
      }).catch(()=>null);
    }
    picker?.remove();
    const st = document.getElementById('oeStatus');
    if(st){ st.textContent='✓ Agent assignments saved'; st.className='status-bar ok'; }
  };
  window.ontDelete = async function() {
    const id = document.getElementById('oeId').value;
    if (!id || !confirm('Delete this ontology?')) return;
    await fetch(window._veraBase+'/ontologies/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    ontShowPanel('none'); await ontsLoad();
  };

  window.ontApplyPanel = function() { ontShowPanel('apply'); };
  window.ontApplyRun = async function() {
    const text  = document.getElementById('oaText').value;
    const id    = document.getElementById('oaId').value;
    const task  = document.getElementById('oaTask').value;
    const st    = document.getElementById('oaStatus');
    st.textContent='⟳ Applying…'; st.className='status-bar';
    document.getElementById('oaResult').textContent='';
    const body = {text, task};
    if (id) body.ontology_id = id;
    const res = await fetch(window._veraBase+'/ontologies/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(e=>({error:e.message}));
    document.getElementById('oaResult').textContent = JSON.stringify(res,null,2);
    st.textContent = res.error ? '✗ '+res.error : `✓ Applied ${res.ontology_count||0} ontologies`;
    st.className   = res.error ? 'status-bar err' : 'status-bar ok';
  };

  window.ontInfer = function() { ontShowPanel('infer'); };
  window.ontDoInfer = async function() {
    const desc = document.getElementById('oiDesc').value.trim();
    if (!desc) return;
    const st   = document.getElementById('oiStatus');
    st.textContent='⟳ Inferring…'; st.className='status-bar';
    document.getElementById('oiResult').textContent='';
    const body = {
      description:  desc,
      example_data: document.getElementById('oiData').value,
      domain:       document.getElementById('oiDomain').value||'general',
      name:         document.getElementById('oiName').value,
      save:         document.getElementById('oiSave').checked,
    };
    const res = await fetch(window._veraBase+'/ontologies/infer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(e=>({error:e.message}));
    document.getElementById('oiResult').textContent = JSON.stringify(res,null,2);
    st.textContent = res.error ? '✗ '+res.error : '✓ Inferred'+(res.saved?' & saved':'');
    st.className   = res.error ? 'status-bar err' : 'status-bar ok';
    if (!res.error && res.saved) await ontsLoad();
  };

  function ontShowPanel(name) {
    document.getElementById('ontEditor').style.display    = name==='editor' ? 'flex' : 'none';
    document.getElementById('ontApply').style.display     = name==='apply'  ? 'flex' : 'none';
    document.getElementById('ontInferPanel').style.display= name==='infer'  ? 'flex' : 'none';
  }

  ontsLoad();
})();
""",
    ui_caps=['ontology.create', 'ontology.list', 'ontology.get', 'ontology.delete',
             'ontology.infer', 'ontology.apply'],
    mode="inject",   # UI served via standalone /ui/panels/ontologies-panel iframe; mode=inject here for ui_caps tracking only
    tab_order=70,
)