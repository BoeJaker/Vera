"""
cap_ontology.py  —  Capability ↔ Capability Ontology System
============================================================
Stores and serves *pairwise relations between capabilities*. The mental
model is a square table of caps × caps; each cell describes how the row
cap interacts with the column cap.

Relations are directed:  (from_cap, to_cap) → relation record.
A cell (X, Y) describes how X relates to Y.  The reverse (Y, X) is a
separate edge, optionally inferred when `direction="bidirectional"`.

This sits ON TOP of the existing skills/ontologies system in skills.py:
- `ontologies.*` defines high-level domain ontologies (entities,
  relationships, processing rules) used by `ontologies.apply`.
- This module defines the *capability mesh* — how caps connect to one
  another in practice, what feeds what, what's an alternative to what.
  It's the system map used by the planner.

Persistence: SQLite (always available) + optional Redis cache + emit
events for live UI updates.

Auto-generation: pairwise relations can be inferred by the LLM from
each cap's name, description, and JSON schema.  Generation is batched
to keep prompts small and parallelism reasonable.

Planner integration: `cap_ontology.context_for` returns a snippet
suitable for injection into a planner system-prompt.  When an agent
has a restricted `domain_caps` allowlist, this function returns
relations *between an allowed cap and a hidden cap* — described by
the relation only — so the planner knows that an adjacent capability
exists without being able to call it directly.

Capabilities registered:
  cap_ontology.list           — list all relations (with filters)
  cap_ontology.get            — single relation by (from, to)
  cap_ontology.set            — upsert one relation
  cap_ontology.bulk_set       — upsert many at once
  cap_ontology.delete         — remove a relation
  cap_ontology.delete_all     — wipe everything (with confirmation)
  cap_ontology.matrix         — full grid as a sparse object
  cap_ontology.neighbours     — relations touching a cap
  cap_ontology.context_for    — planner-injection snippet
  cap_ontology.auto_pair      — LLM: infer one (from, to) relation
  cap_ontology.auto_group     — LLM: infer all pairs for a group
  cap_ontology.auto_grid      — LLM: infer the full grid (long task)
  cap_ontology.suggest        — quick LLM suggestion for editor UI
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    CAPABILITY_REGISTRY,
    capability,
    emit_event,
    now_iso,
    ollama_generate,
    register_ui,
)

log = logging.getLogger("vera.cap_ontology")

# ─────────────────────────────────────────────────────────────────────────────
# SQLITE STORE
# ─────────────────────────────────────────────────────────────────────────────
_SQLITE_PATH = Path(__file__).parent / "vera_cap_ontology.db"


def _sqlite_init():
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cap_relations (
            from_cap     TEXT NOT NULL,
            to_cap       TEXT NOT NULL,
            relation     TEXT NOT NULL DEFAULT '',
            description  TEXT NOT NULL DEFAULT '',
            direction    TEXT NOT NULL DEFAULT 'forward',
            strength     REAL NOT NULL DEFAULT 0.5,
            auto         INTEGER NOT NULL DEFAULT 0,
            tags         TEXT NOT NULL DEFAULT '[]',
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (from_cap, to_cap)
        );
        CREATE INDEX IF NOT EXISTS idx_cap_rel_from ON cap_relations(from_cap);
        CREATE INDEX IF NOT EXISTS idx_cap_rel_to   ON cap_relations(to_cap);
        CREATE INDEX IF NOT EXISTS idx_cap_rel_strn ON cap_relations(strength);

        -- Per-capability description overrides (separate table — one entry per cap).
        -- Lets the user replace a cap's auto-generated decorator description with a
        -- curated one without modifying source code. Read by _cap_card via
        -- _description_override_for() so it flows into the auto-pair prompts too.
        CREATE TABLE IF NOT EXISTS cap_description_overrides (
            cap_name    TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL
        );
    """)
    # Idempotent migrations for the new fields
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cap_relations)").fetchall()}
    if "wire" not in cols:
        conn.execute("ALTER TABLE cap_relations ADD COLUMN wire TEXT NOT NULL DEFAULT ''")
    if "confidence" not in cols:
        conn.execute("ALTER TABLE cap_relations ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5")
    conn.commit()
    return conn


try:
    _c = _sqlite_init(); _c.close()
    log.info("cap_ontology: SQLite ready at %s", _SQLITE_PATH)
except Exception as e:
    log.warning("cap_ontology: SQLite init failed: %s", e)


# Read all rows of cap_relations including new columns. We use named indices
# rather than positional unpacking so future migrations don't break this.
_REL_COLS = (
    "from_cap, to_cap, relation, description, direction, strength, "
    "auto, tags, updated_at, wire, confidence"
)

def _row_to_dict(r) -> dict:
    return {
        "from":        r[0],
        "to":          r[1],
        "relation":    r[2],
        "description": r[3],
        "direction":   r[4],
        "strength":    float(r[5]) if r[5] is not None else 0.5,
        "auto":        bool(r[6]),
        "tags":        json.loads(r[7]) if r[7] else [],
        "updated_at":  r[8],
        "wire":        r[9] if len(r) > 9 and r[9] is not None else "",
        "confidence":  float(r[10]) if len(r) > 10 and r[10] is not None else float(r[5] or 0.5),
    }


def _conn():
    return sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)


def _db_get(from_cap: str, to_cap: str) -> Optional[dict]:
    conn = _conn()
    try:
        r = conn.execute(
            f"SELECT {_REL_COLS} FROM cap_relations WHERE from_cap=? AND to_cap=?",
            (from_cap, to_cap),
        ).fetchone()
        return _row_to_dict(r) if r else None
    finally:
        conn.close()


def _db_upsert(rec: dict) -> dict:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cap_relations "
            "(from_cap,to_cap,relation,description,direction,strength,auto,tags,updated_at,wire,confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec["from"], rec["to"],
                rec.get("relation", ""), rec.get("description", ""),
                rec.get("direction", "forward"),
                float(rec.get("strength", 0.5)),
                1 if rec.get("auto") else 0,
                json.dumps(rec.get("tags", [])),
                rec.get("updated_at", now_iso()),
                rec.get("wire", "") or "",
                float(rec.get("confidence", rec.get("strength", 0.5))),
            ),
        )
        conn.commit()
        return _db_get(rec["from"], rec["to"])
    finally:
        conn.close()


def _db_delete(from_cap: str, to_cap: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM cap_relations WHERE from_cap=? AND to_cap=?",
            (from_cap, to_cap),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _db_all(from_cap: str = "", to_cap: str = "",
            min_strength: float = 0.0, only_auto: bool = False,
            only_manual: bool = False) -> List[dict]:
    conn = _conn()
    try:
        q = f"SELECT {_REL_COLS} FROM cap_relations WHERE 1=1"
        args: list = []
        if from_cap:
            q += " AND from_cap=?"; args.append(from_cap)
        if to_cap:
            q += " AND to_cap=?"; args.append(to_cap)
        if min_strength > 0:
            q += " AND strength>=?"; args.append(float(min_strength))
        if only_auto:
            q += " AND auto=1"
        if only_manual:
            q += " AND auto=0"
        rows = conn.execute(q, args).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _db_count() -> int:
    conn = _conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM cap_relations").fetchone()[0]
    finally:
        conn.close()


def _db_wipe() -> int:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM cap_relations")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── Description overrides ────────────────────────────────────────────────────
# Cache so _cap_card() doesn't hit SQLite on every call during a grid pass.
_DESC_OVERRIDES: Dict[str, str] = {}
_DESC_LOADED = False


def _load_desc_overrides() -> None:
    global _DESC_LOADED
    conn = _conn()
    try:
        rows = conn.execute("SELECT cap_name, description FROM cap_description_overrides").fetchall()
        _DESC_OVERRIDES.clear()
        for r in rows:
            if r[1]:
                _DESC_OVERRIDES[r[0]] = r[1]
        _DESC_LOADED = True
    except Exception as e:
        log.debug("desc overrides load: %s", e)
    finally:
        conn.close()


def _description_override_for(name: str) -> Optional[str]:
    if not _DESC_LOADED:
        _load_desc_overrides()
    return _DESC_OVERRIDES.get(name)


def _save_desc_override(name: str, description: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cap_description_overrides (cap_name, description, updated_at) "
            "VALUES (?, ?, ?)",
            (name, description, now_iso()),
        )
        conn.commit()
        _DESC_OVERRIDES[name] = description
    finally:
        conn.close()


def _delete_desc_override(name: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM cap_description_overrides WHERE cap_name=?", (name,)
        )
        conn.commit()
        if name in _DESC_OVERRIDES:
            _DESC_OVERRIDES.pop(name, None)
        return cur.rowcount > 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# CAP REGISTRY ACCESS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _all_cap_names() -> List[str]:
    return sorted(CAPABILITY_REGISTRY.keys())


# ── Cap introspection helpers ──────────────────────────────────────────────────

def _format_param(name: str, spec: dict, required: bool) -> str:
    """Render one parameter as 'name: type [= default] (description)' for the prompt."""
    typ = spec.get("type") or "any"
    desc = (spec.get("description") or "").strip()
    if isinstance(typ, list):
        typ = "|".join(t for t in typ if t)
    line = f"{name}: {typ}"
    if not required and "default" in spec:
        line += f" = {json.dumps(spec.get('default'))}"
    line += "  (required)" if required else ""
    if desc:
        line += f"  — {desc[:140]}"
    return line


def _cap_card(name: str, *, with_source: bool = False, source_lines: int = 60) -> str:
    """
    Build a rich, structured card describing a capability for the LLM.

    Includes:
      - canonical name (so the LLM never has to refer to "X" or "Y")
      - one-line description
      - input parameters with types + descriptions + required flag
      - declared output shape (if present in schema)
      - return-type hint from the function signature (if available)
      - tags, mode, http info
      - optional truncated source (signature + first few lines of body)
    """
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return f"NAME: {name}\n(unknown capability — not registered)"

    description = (cap.get("description") or "").strip()
    # Allow per-cap description overrides — these are curated by the user via
    # the Cap Ontology UI and let the user replace a poor decorator description
    # without touching source. Used by the auto-pair prompt and by anything
    # else that calls _cap_card.
    override = _description_override_for(name)
    if override:
        description = override
    schema = cap.get("schema") or {}
    props  = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    tags = cap.get("tags") or []
    mode = cap.get("mode") or "local"
    http_method = cap.get("http_method") or ""
    http_path   = cap.get("http_path") or ""

    lines: List[str] = []
    lines.append(f"NAME: {name}")
    if description:
        lines.append(f"DESCRIPTION: {description[:600]}")
    if mode and mode != "local":
        lines.append(f"MODE: {mode}")
    if http_method and http_path:
        lines.append(f"HTTP: {http_method} {http_path}")
    if tags:
        lines.append(f"TAGS: {', '.join(str(t) for t in tags[:10])}")

    # Inputs
    in_lines = []
    for pname, pspec in props.items():
        if pname == "trace_id":
            continue
        if not isinstance(pspec, dict):
            continue
        in_lines.append("  - " + _format_param(pname, pspec, pname in required))
    if in_lines:
        lines.append("INPUTS:")
        lines.extend(in_lines)
    else:
        lines.append("INPUTS: (none)")

    # Output shape — explicit `output` schema overrides
    output = schema.get("output") or schema.get("returns") or cap.get("output")
    if output:
        try:
            lines.append("OUTPUT SHAPE: " + json.dumps(output, default=str)[:500])
        except Exception:
            pass

    # Return-annotation hint from the function
    func = cap.get("func")
    if func:
        try:
            import inspect
            sig = inspect.signature(func)
            ret = sig.return_annotation
            if ret is not inspect.Parameter.empty and ret is not None:
                lines.append(f"RETURNS: {getattr(ret, '__name__', str(ret))}")
        except Exception:
            pass

    # Source code (truncated) — most informative for the "could" question
    if with_source and func:
        try:
            import inspect
            src = inspect.getsource(func)
            src_lines = src.splitlines()
            # Trim to roughly the signature + first N body lines
            head = src_lines[: max(8, source_lines)]
            body = "\n".join(head)
            if len(src_lines) > len(head):
                body += f"\n# … ({len(src_lines) - len(head)} more lines elided)"
            lines.append("SOURCE (truncated):\n" + body)
        except Exception:
            pass

    return "\n".join(lines)


def _cap_brief(name: str) -> str:
    """Compact one-liner — kept for legacy use, e.g. context_for snippets."""
    cap = CAPABILITY_REGISTRY.get(name, {})
    raw_desc = (cap.get("description") or "").strip().split("\n")[0][:120]
    override = _description_override_for(name)
    desc = override.split("\n")[0][:120] if override else raw_desc
    props = (cap.get("schema") or {}).get("properties") or {}
    req = set((cap.get("schema") or {}).get("required") or [])
    sig = ", ".join(
        f"{p}:{(v.get('type') or 'str')}{'!' if p in req else ''}"
        for p, v in props.items() if p != "trace_id"
    )
    return f"{name}({sig}) — {desc}"


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — CRUD
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "cap_ontology.list", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/list",
    http_tags=["cap_ontology"],
    description="List capability-to-capability relations. Filters: from, to, min_strength, only_auto, only_manual.",
)
async def co_list(
    from_cap: str = "",
    to_cap: str = "",
    min_strength: float = 0.0,
    only_auto: bool = False,
    only_manual: bool = False,
    trace_id=None,
):
    items = _db_all(from_cap, to_cap, float(min_strength or 0),
                    bool(only_auto), bool(only_manual))
    return {"relations": items, "count": len(items)}


@capability(
    "cap_ontology.get", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/get",
    http_tags=["cap_ontology"],
    description="Get a single relation. Inputs: from_cap, to_cap.",
)
async def co_get(from_cap: str, to_cap: str, trace_id=None):
    r = _db_get(from_cap, to_cap)
    if not r:
        return {"error": "not found"}
    return r


@capability(
    "cap_ontology.set", memory="off",
    http_method="POST", http_path="/cap_ontology/set",
    http_tags=["cap_ontology"],
    description=(
        "Upsert one capability relation. "
        "Inputs: from_cap, to_cap, relation, description, "
        "direction (forward|backward|bidirectional), strength (0..1), "
        "tags (csv), auto (bool), wire (str — 'A.field -> B.param'), "
        "confidence (0..1)."
    ),
)
async def co_set(
    from_cap:    str,
    to_cap:      str,
    relation:    str   = "",
    description: str   = "",
    direction:   str   = "forward",
    strength:    float = 0.5,
    tags:        str   = "",
    auto:        bool  = False,
    wire:        str   = "",
    confidence:  float = 0.0,
    trace_id=None,
):
    if not from_cap or not to_cap:
        return {"error": "from_cap and to_cap required"}
    if from_cap == to_cap:
        return {"error": "self-loops are not allowed"}
    direction = direction if direction in ("forward", "backward", "bidirectional") else "forward"
    s = max(0.0, min(1.0, float(strength or 0.5)))
    c = float(confidence or 0)
    if c <= 0:
        c = s   # default confidence to strength when not specified
    rec = {
        "from": from_cap, "to": to_cap,
        "relation": relation.strip(), "description": description.strip(),
        "direction": direction,
        "strength": s,
        "confidence": max(0.0, min(1.0, c)),
        "wire": (wire or "").strip()[:200],
        "auto": bool(auto),
        "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
        "updated_at": now_iso(),
    }
    saved = _db_upsert(rec)
    # If bidirectional, also write the reverse with mirrored direction
    if direction == "bidirectional":
        rev = dict(rec); rev["from"], rev["to"] = rec["to"], rec["from"]
        _db_upsert(rev)
    await emit_event({"type": "cap_ontology.set",
                      "from": from_cap, "to": to_cap,
                      "relation": rec["relation"]})
    return saved


@capability(
    "cap_ontology.bulk_set", memory="off",
    http_method="POST", http_path="/cap_ontology/bulk_set",
    http_tags=["cap_ontology"],
    description="Upsert many relations in one call. Input: relations (JSON array of {from,to,relation,description,direction,strength,tags,auto}).",
)
async def co_bulk_set(relations: str, trace_id=None):
    try:
        items = json.loads(relations) if isinstance(relations, str) else relations
    except Exception as e:
        return {"error": f"invalid JSON: {e}"}
    if not isinstance(items, list):
        return {"error": "relations must be a list"}
    saved = 0
    skipped = 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1; continue
        f, t = it.get("from"), it.get("to")
        if not f or not t or f == t:
            skipped += 1; continue
        rec = {
            "from": f, "to": t,
            "relation": (it.get("relation") or "").strip(),
            "description": (it.get("description") or "").strip(),
            "direction": it.get("direction", "forward") if it.get("direction") in ("forward", "backward", "bidirectional") else "forward",
            "strength": max(0.0, min(1.0, float(it.get("strength", 0.5) or 0.5))),
            "auto": bool(it.get("auto", False)),
            "tags": it.get("tags", []) if isinstance(it.get("tags"), list) else [],
            "updated_at": now_iso(),
        }
        _db_upsert(rec)
        saved += 1
    await emit_event({"type": "cap_ontology.bulk_set", "count": saved})
    return {"saved": saved, "skipped": skipped, "total": _db_count()}


@capability(
    "cap_ontology.delete", memory="off",
    http_method="POST", http_path="/cap_ontology/delete",
    http_tags=["cap_ontology"],
    description="Delete a relation. Inputs: from_cap, to_cap.",
)
async def co_delete(from_cap: str, to_cap: str, trace_id=None):
    ok = _db_delete(from_cap, to_cap)
    await emit_event({"type": "cap_ontology.deleted",
                      "from": from_cap, "to": to_cap})
    return {"deleted": ok}


@capability(
    "cap_ontology.delete_all", memory="off",
    http_method="POST", http_path="/cap_ontology/delete_all",
    http_tags=["cap_ontology"],
    description="Wipe ALL relations. Requires confirm='YES_DELETE_ALL'.",
)
async def co_delete_all(confirm: str = "", trace_id=None):
    if confirm != "YES_DELETE_ALL":
        return {"error": "confirm='YES_DELETE_ALL' required"}
    n = _db_wipe()
    await emit_event({"type": "cap_ontology.wiped", "deleted": n})
    return {"deleted": n}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — VIEWS
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "cap_ontology.matrix", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/matrix",
    http_tags=["cap_ontology"],
    description=(
        "Return a sparse matrix of all relations. Output: "
        "{caps: [names], cells: [{from,to,relation,description,strength,direction,auto}]}. "
        "Optional filters: group (cap-prefix), search (substring), only_connected (bool — caps with at least one edge)."
    ),
)
async def co_matrix(
    group: str = "",
    search: str = "",
    only_connected: bool = False,
    trace_id=None,
):
    all_caps = _all_cap_names()
    if group:
        all_caps = [c for c in all_caps if c.split(".")[0] == group]
    if search:
        s = search.lower()
        all_caps = [c for c in all_caps if s in c.lower()]
    rels = _db_all()
    # If only_connected, restrict to caps that appear in at least one relation
    if only_connected:
        connected = set()
        for r in rels:
            connected.add(r["from"]); connected.add(r["to"])
        all_caps = [c for c in all_caps if c in connected]

    # Filter rels to caps in scope
    cap_set = set(all_caps)
    cells = [r for r in rels if r["from"] in cap_set and r["to"] in cap_set]
    return {"caps": all_caps, "cells": cells, "total_caps": len(all_caps),
            "total_cells": len(cells)}


@capability(
    "cap_ontology.neighbours", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/neighbours",
    http_tags=["cap_ontology"],
    description="Get all relations touching a cap (in or out). Inputs: cap (str), direction (in|out|both, default both).",
)
async def co_neighbours(cap: str, direction: str = "both", trace_id=None):
    out_rels: List[dict] = []
    in_rels:  List[dict] = []
    if direction in ("out", "both"):
        out_rels = _db_all(from_cap=cap)
    if direction in ("in", "both"):
        in_rels  = _db_all(to_cap=cap)
    return {"cap": cap, "outgoing": out_rels, "incoming": in_rels,
            "count": len(out_rels) + len(in_rels)}


@capability(
    "cap_ontology.context_for", memory="off", silent=True,
    http_method="POST", http_path="/cap_ontology/context_for",
    http_tags=["cap_ontology"],
    description=(
        "Build a planner-injection snippet for an agent. "
        "Inputs: available_caps (CSV or JSON list of cap names the agent CAN call), "
        "include_hidden (bool, default true — include relations where one side is "
        "outside the available set, but only describe the hidden cap by relation, NOT name+schema). "
        "Output: {snippet, allowed_count, hidden_referenced_count}."
    ),
)
async def co_context_for(
    available_caps: str,
    include_hidden: bool = True,
    max_relations:  int  = 80,
    trace_id=None,
):
    # Parse the allowlist
    if isinstance(available_caps, str):
        try:
            allowed = json.loads(available_caps)
            if not isinstance(allowed, list):
                raise ValueError
        except Exception:
            allowed = [c.strip() for c in available_caps.split(",") if c.strip()]
    else:
        allowed = list(available_caps or [])
    # Wildcard: agent has all caps
    if "*" in allowed:
        allowed_set = set(_all_cap_names())
        wildcard = True
    else:
        allowed_set = set(allowed)
        wildcard = False

    rels = _db_all()
    visible_pair = []   # both ends visible
    edge_to_hidden = []  # one end is hidden — describe by relation only
    for r in rels:
        f_in = r["from"] in allowed_set
        t_in = r["to"]   in allowed_set
        if f_in and t_in:
            visible_pair.append(r)
        elif (f_in or t_in) and include_hidden and not wildcard:
            edge_to_hidden.append(r)

    # Sort by strength desc
    visible_pair.sort(key=lambda x: -x["strength"])
    edge_to_hidden.sort(key=lambda x: -x["strength"])
    visible_pair = visible_pair[:max_relations]
    edge_to_hidden = edge_to_hidden[:max_relations]

    # Build snippet
    lines = []
    if visible_pair:
        lines.append("## Capability relations (caps you can call)")
        for r in visible_pair:
            arrow = "↔" if r["direction"] == "bidirectional" else "→"
            lab = r["relation"] or "related"
            d = (r["description"] or "")[:140]
            lines.append(f"- {r['from']} {arrow}[{lab}]{arrow} {r['to']}" + (f" — {d}" if d else ""))
    if edge_to_hidden:
        lines.append("\n## Adjacent capabilities (visible only by relation; you cannot call them directly)")
        for r in edge_to_hidden:
            # Identify the hidden side
            if r["from"] in allowed_set:
                visible, hidden, dir_label = r["from"], r["to"], "feeds_into"
            else:
                visible, hidden, dir_label = r["to"], r["from"], "depends_on"
            lab = r["relation"] or dir_label
            d = (r["description"] or "")[:140]
            lines.append(f"- {visible} → [{lab}] → <hidden capability>" + (f" — {d}" if d else ""))
    snippet = "\n".join(lines).strip()
    return {
        "snippet": snippet,
        "allowed_count": len(visible_pair),
        "hidden_referenced_count": len(edge_to_hidden),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — DESCRIPTION OVERRIDES
# Curated descriptions that replace the decorator-supplied one. Used both in
# the UI (cap_card returns them) and as inputs to the auto-pair LLM prompt.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "cap_ontology.descriptions", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/descriptions",
    http_tags=["cap_ontology"],
    description="List all description overrides as {cap_name: description}.",
)
async def co_descriptions_list(trace_id=None):
    if not _DESC_LOADED:
        _load_desc_overrides()
    return {"overrides": dict(_DESC_OVERRIDES), "count": len(_DESC_OVERRIDES)}


@capability(
    "cap_ontology.description_get", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/description_get",
    http_tags=["cap_ontology"],
    description=(
        "Get the effective description for a cap. "
        "Returns {name, description, original, override, has_override}."
    ),
)
async def co_description_get(cap_name: str, trace_id=None):
    if not _DESC_LOADED:
        _load_desc_overrides()
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"unknown cap: {cap_name}"}
    original = (cap.get("description") or "").strip()
    override = _DESC_OVERRIDES.get(cap_name)
    return {
        "name":         cap_name,
        "original":     original,
        "override":     override or "",
        "has_override": bool(override),
        "description":  override or original,
    }


@capability(
    "cap_ontology.description_set", memory="off",
    http_method="POST", http_path="/cap_ontology/description_set",
    http_tags=["cap_ontology"],
    description=(
        "Set or replace the description override for a cap. "
        "Inputs: cap_name (str), description (str). "
        "Pass an empty description to remove the override and fall back to the decorator's text."
    ),
)
async def co_description_set(cap_name: str, description: str = "", trace_id=None):
    if cap_name not in CAPABILITY_REGISTRY:
        return {"error": f"unknown cap: {cap_name}"}
    description = (description or "").strip()
    if not description:
        # Empty = clear override
        _delete_desc_override(cap_name)
        await emit_event({"type": "cap_ontology.description_cleared", "cap": cap_name})
        return {"cap_name": cap_name, "cleared": True}
    _save_desc_override(cap_name, description[:2000])
    await emit_event({"type": "cap_ontology.description_set", "cap": cap_name})
    return {"cap_name": cap_name, "description": description, "saved": True}


@capability(
    "cap_ontology.description_delete", memory="off",
    http_method="POST", http_path="/cap_ontology/description_delete",
    http_tags=["cap_ontology"],
    description="Remove the description override for a cap (revert to decorator text).",
)
async def co_description_delete(cap_name: str, trace_id=None):
    ok = _delete_desc_override(cap_name)
    await emit_event({"type": "cap_ontology.description_cleared", "cap": cap_name})
    return {"cap_name": cap_name, "deleted": ok}


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES — LLM AUTO GENERATION
# ─────────────────────────────────────────────────────────────────────────────

# Re-defined: the prompt now drives the model toward COMPOSABILITY analysis
# (could the output of A be used as input to B?), not premise-induced narrative
# about how the caps "do" interact in the running system.  This avoids the
# previous failure mode where the model fabricated procedural-sounding
# descriptions like "deleting credentials is required before saving them".
_AUTO_SYSTEM = (
    "You analyse whether two capabilities (A and B) COULD be composed together "
    "in a workflow. You reason from concrete signatures: their declared inputs, "
    "their declared/observable outputs, and their source code — not from naming "
    "intuitions or imagined procedural workflows.\n\n"

    "PRIMARY QUESTION:\n"
    "  Could the OUTPUT of A be plausibly fed as INPUT to B (or wrap/augment a "
    "  call to B)? If yes, describe specifically WHICH output field of A could "
    "  be wired to WHICH input parameter of B, and under what circumstances "
    "  that composition would be useful.\n\n"

    "REASONING ORDER (do all of this internally before producing JSON):\n"
    "  1. List A's likely output fields (from OUTPUT SHAPE, RETURNS, or what "
    "     the source code returns).\n"
    "  2. List B's input parameters (from INPUTS).\n"
    "  3. Look for type-compatible / semantically-compatible matches.\n"
    "  4. If no match exists, the answer is NO RELATION (strength=0, "
    '     relation:"").\n\n'

    "RELATION LABEL — pick ONE that fits the composition shape:\n"
    "  • feeds_into       — A's output naturally becomes B's input\n"
    "  • alternative_to   — A and B do similar things; one might replace the other\n"
    "  • complements      — A and B are commonly run together but neither feeds the other\n"
    "  • prerequisite_of  — B requires that A has been run first to set up state\n"
    "  • post_processes   — B refines/summarises/transforms A's output\n"
    "  • validates        — B checks A's output\n"
    "  • observes         — B watches A's progress/events without altering it\n"
    "  • configures       — A's output configures behaviour of B\n"
    "  • indexes          — A indexes/registers what B produces (or vice versa)\n"
    "  • triggers         — A's invocation reasonably causes B to run\n"
    "  • ''   (empty)     — NO MEANINGFUL RELATION (use this whenever in doubt)\n\n"

    "DESCRIPTION RULES — VIOLATIONS ARE REJECTED:\n"
    "  - Use the literal capability NAMES from the cards. NEVER write 'Capability X', "
    '    "X", "Y", "the from cap", or other placeholders.\n'
    "  - Frame as POSSIBILITY: 'X.y could supply <field> to Z.w when …'. "
    "    Do NOT write 'X must be called before Y' unless the source clearly proves it.\n"
    "  - Cite the specific field/parameter that bridges them when relevant.\n"
    "  - Be honest about uncertainty: if the link is weak or speculative, say so "
    "    and use a low strength (≤ 0.4).\n"
    "  - One sentence. No headings. No bullet points.\n\n"

    "STRENGTH GUIDANCE:\n"
    "  0.85-1.00  — output of A clearly types-into a required input of B; obvious composition\n"
    "  0.55-0.84  — plausible composition, requires shaping the data\n"
    "  0.30-0.54  — same domain, weak composition; mostly useful as context\n"
    "  0.00-0.29  — speculative, return relation:'' instead\n\n"

    "STRONG BIAS TO 'NO RELATION'. Most cap pairs in a system are unrelated. "
    "If A and B are in different domains and you cannot point to a specific "
    'field-to-parameter wire, return strength:0 and relation:"".\n\n'

    "Return ONLY valid JSON in this shape — no prose around it:\n"
    "{\n"
    '  "relation":     "<label or empty>",\n'
    '  "description":  "<single sentence using the actual cap names>",\n'
    '  "direction":    "forward|backward|bidirectional",\n'
    '  "strength":     0.0-1.0,\n'
    '  "wire":         "<optional: A.output_field -> B.input_param>",\n'
    '  "confidence":   0.0-1.0\n'
    "}"
)


_BAD_DESCRIPTION_PATTERNS = [
    r"\bcapability x\b", r"\bcapability y\b",
    r"\bcap x\b", r"\bcap y\b",
    r"\(x\)", r"\(y\)",
    r"\bthe from cap\b", r"\bthe to cap\b",
    r"\bfrom side\b", r"\bto side\b",
]
_BAD_RE = re.compile("|".join(_BAD_DESCRIPTION_PATTERNS), re.IGNORECASE)


def _scrub_description(text: str, from_cap: str, to_cap: str) -> str:
    """
    Replace placeholder-y references in a description with the real cap names,
    and collapse leftover X/Y. Intended as a safety net — the prompt forbids
    these, but small models still emit them.
    """
    if not text:
        return text
    s = text
    # Replace explicit placeholders with real names
    replacements = [
        (r"\bcapability x\b",   from_cap),
        (r"\bcapability y\b",   to_cap),
        (r"\bcap x\b",          from_cap),
        (r"\bcap y\b",          to_cap),
        (r"\bx\b(?=\s)",        from_cap),
        (r"\by\b(?=\s)",        to_cap),
        (r"\bthe from cap\b",   from_cap),
        (r"\bthe to cap\b",     to_cap),
        (r"\bfrom side\b",      from_cap),
        (r"\bto side\b",        to_cap),
        (r"\(x\)",              f"({from_cap})"),
        (r"\(y\)",              f"({to_cap})"),
    ]
    for pat, repl in replacements:
        s = re.sub(pat, repl, s, flags=re.IGNORECASE)
    return s.strip()


def _description_looks_bad(text: str) -> bool:
    """Detect placeholder-laden descriptions that should be rejected."""
    if not text:
        return False
    return bool(_BAD_RE.search(text))


async def _auto_pair(from_cap: str, to_cap: str, prefer_gpu: bool = True,
                     with_source: bool = True) -> dict:
    if from_cap == to_cap:
        return {"relation": "", "description": "", "direction": "forward", "strength": 0.0}

    a_card = _cap_card(from_cap, with_source=with_source)
    b_card = _cap_card(to_cap,   with_source=with_source)

    prompt = (
        f"=== Capability A (source side) ===\n{a_card}\n\n"
        f"=== Capability B (target side) ===\n{b_card}\n\n"
        f"Task: Decide whether {from_cap} and {to_cap} COULD be composed in a "
        f"useful workflow. If yes, describe specifically how — naming both caps "
        f"by their literal names ({from_cap} and {to_cap}) and the field/param "
        f"that bridges them. If no, return strength:0 and relation:''.\n\n"
        "Respond with the JSON object only."
    )

    try:
        raw = await asyncio.wait_for(
            ollama_generate(prompt, system=_AUTO_SYSTEM, json_mode=True,
                            prefer_gpu=prefer_gpu),
            timeout=90,
        )
    except asyncio.TimeoutError:
        return {"relation": "", "description": "(LLM timeout)",
                "direction": "forward", "strength": 0.0,
                "confidence": 0.0, "wire": ""}

    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?|\n?```$", "", raw)

    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not a dict")
    except Exception as e:
        log.debug("auto_pair parse error %s→%s: %s — raw=%r",
                  from_cap, to_cap, e, raw[:240])
        return {"relation": "", "description": "(unparseable LLM output)",
                "direction": "forward", "strength": 0.0,
                "confidence": 0.0, "wire": ""}

    relation    = str(d.get("relation", ""))[:60].strip()
    description = str(d.get("description", ""))[:400].strip()
    direction   = d.get("direction", "forward")
    if direction not in ("forward", "backward", "bidirectional"):
        direction = "forward"
    strength   = max(0.0, min(1.0, float(d.get("strength", 0.0) or 0.0)))
    confidence = max(0.0, min(1.0, float(d.get("confidence", strength) or 0.0)))
    wire       = str(d.get("wire", ""))[:200].strip()

    # Scrub leftover placeholder-speak
    description = _scrub_description(description, from_cap, to_cap)

    # If the description still mentions placeholder forms, downgrade aggressively —
    # the LLM didn't engage with the real names.
    if _description_looks_bad(description):
        log.debug("auto_pair %s→%s: description still has placeholders, downgrading",
                  from_cap, to_cap)
        strength = min(strength, 0.25)
        confidence = min(confidence, 0.3)
        relation = ""  # force "no relation"

    # If the model returned a relation but the description does not contain
    # either cap name, that's also a strong signal of fabrication. Most caps
    # have at least one segment that should appear in any honest description.
    if relation and description:
        a_tokens = [t for t in re.split(r"[._\W]+", from_cap) if len(t) >= 3]
        b_tokens = [t for t in re.split(r"[._\W]+", to_cap)   if len(t) >= 3]
        d_lower  = description.lower()
        a_match  = any(tok.lower() in d_lower for tok in a_tokens)
        b_match  = any(tok.lower() in d_lower for tok in b_tokens)
        if not (a_match and b_match):
            # Penalty — neither name actually referenced
            strength   = min(strength, 0.4)
            confidence = min(confidence, 0.4)

    # Final 'no-relation' clamp: sub-threshold strength is forced empty so
    # the caller treats it as "do not save".
    if strength < 0.30 or not relation:
        return {"relation": "", "description": description,
                "direction": "forward", "strength": 0.0,
                "confidence": confidence, "wire": wire}

    return {
        "relation":    relation,
        "description": description,
        "direction":   direction,
        "strength":    strength,
        "confidence":  confidence,
        "wire":        wire,
    }


@capability(
    "cap_ontology.auto_pair", memory="off",
    http_method="POST", http_path="/cap_ontology/auto_pair",
    http_tags=["cap_ontology"],
    description="LLM-infer one (from→to) relation. Inputs: from_cap, to_cap, save (bool default true), prefer_gpu (bool default true).",
)
async def co_auto_pair(from_cap: str, to_cap: str, save: bool = True,
                       prefer_gpu: bool = True, trace_id=None):
    if from_cap == to_cap:
        return {"error": "self-loops not allowed"}
    if from_cap not in CAPABILITY_REGISTRY:
        return {"error": f"unknown from_cap: {from_cap}"}
    if to_cap not in CAPABILITY_REGISTRY:
        return {"error": f"unknown to_cap: {to_cap}"}
    inferred = await _auto_pair(from_cap, to_cap, prefer_gpu=prefer_gpu)
    # Save only when the LLM committed to a real relation. _auto_pair clears
    # `relation` and zeroes strength when it cannot defend a composition.
    will_save = bool(save and inferred.get("strength", 0) > 0 and inferred.get("relation"))
    if will_save:
        rec = {
            "from": from_cap, "to": to_cap,
            "relation":    inferred["relation"],
            "description": inferred["description"],
            "direction":   inferred["direction"],
            "strength":    inferred["strength"],
            "confidence":  inferred.get("confidence", inferred["strength"]),
            "wire":        inferred.get("wire", ""),
            "auto": True, "tags": ["llm-generated"],
            "updated_at": now_iso(),
        }
        _db_upsert(rec)
        if inferred["direction"] == "bidirectional":
            rev = dict(rec); rev["from"], rev["to"] = to_cap, from_cap
            _db_upsert(rev)
        await emit_event({"type": "cap_ontology.set",
                          "from": from_cap, "to": to_cap,
                          "auto": True, "relation": inferred["relation"]})
    return {**inferred, "from": from_cap, "to": to_cap, "saved": will_save}


@capability(
    "cap_ontology.suggest", memory="off",
    http_method="POST", http_path="/cap_ontology/suggest",
    http_tags=["cap_ontology"],
    description="Quick LLM suggestion for the editor UI: returns relation+description+direction+strength WITHOUT saving. Inputs: from_cap, to_cap.",
)
async def co_suggest(from_cap: str, to_cap: str, trace_id=None):
    if from_cap == to_cap:
        return {"error": "self-loops not allowed"}
    if from_cap not in CAPABILITY_REGISTRY:
        return {"error": f"unknown from_cap: {from_cap}"}
    if to_cap not in CAPABILITY_REGISTRY:
        return {"error": f"unknown to_cap: {to_cap}"}
    inferred = await _auto_pair(from_cap, to_cap, prefer_gpu=True)
    return {**inferred, "from": from_cap, "to": to_cap}


# ── Background batch jobs ────────────────────────────────────────────────────
_AUTO_JOBS: Dict[str, dict] = {}   # job_id → status dict


def _new_job(kind: str, total: int) -> str:
    jid = f"co_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    _AUTO_JOBS[jid] = {
        "id": jid, "kind": kind, "total": total, "done": 0,
        "saved": 0, "skipped": 0, "errors": 0,
        "started_at": now_iso(), "finished_at": "",
        "current": "", "status": "running",
    }
    return jid


async def _run_pairs(job_id: str, pairs: List[Tuple[str, str]],
                     min_strength: float, prefer_gpu: bool,
                     concurrency: int = 2):
    job = _AUTO_JOBS[job_id]
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(f: str, t: str):
        async with sem:
            job["current"] = f"{f} → {t}"
            try:
                inf = await _auto_pair(f, t, prefer_gpu=prefer_gpu)
            except Exception as e:
                job["errors"] += 1; log.warning("auto_pair %s→%s: %s", f, t, e)
                inf = None
            if inf and inf["strength"] >= min_strength and inf["relation"]:
                rec = {
                    "from": f, "to": t,
                    "relation":    inf["relation"],
                    "description": inf["description"],
                    "direction":   inf["direction"],
                    "strength":    inf["strength"],
                    "confidence":  inf.get("confidence", inf["strength"]),
                    "wire":        inf.get("wire", ""),
                    "auto": True, "tags": ["llm-generated"],
                    "updated_at": now_iso(),
                }
                _db_upsert(rec)
                if inf["direction"] == "bidirectional":
                    rev = dict(rec); rev["from"], rev["to"] = t, f
                    _db_upsert(rev)
                job["saved"] += 1
            else:
                job["skipped"] += 1
            job["done"] += 1
            if job["done"] % 5 == 0 or job["done"] == job["total"]:
                await emit_event({"type": "cap_ontology.job_progress",
                                  "job_id": job_id,
                                  "done": job["done"], "total": job["total"],
                                  "saved": job["saved"], "skipped": job["skipped"]})

    try:
        await asyncio.gather(*[_one(f, t) for f, t in pairs], return_exceptions=True)
        job["status"] = "complete"
    except Exception as e:
        job["status"] = "error"; job["errors"] += 1
        log.warning("auto job %s failed: %s", job_id, e)
    finally:
        job["finished_at"] = now_iso()
        await emit_event({"type": "cap_ontology.job_done",
                          "job_id": job_id, "saved": job["saved"],
                          "skipped": job["skipped"], "errors": job["errors"]})


@capability(
    "cap_ontology.auto_group", memory="off",
    http_method="POST", http_path="/cap_ontology/auto_group",
    http_tags=["cap_ontology"],
    description=(
        "LLM-infer all relations within or between groups. "
        "Inputs: group_a (str), group_b (str empty=same as group_a), "
        "min_strength (float default 0.3), overwrite (bool default false), "
        "concurrency (int default 2), prefer_gpu (bool default true). "
        "Returns {job_id} immediately and emits cap_ontology.job_progress events."
    ),
)
async def co_auto_group(
    group_a:      str,
    group_b:      str   = "",
    min_strength: float = 0.3,
    overwrite:    bool  = False,
    concurrency:  int   = 2,
    prefer_gpu:   bool  = True,
    trace_id=None,
):
    a_caps = [c for c in _all_cap_names() if c.split(".")[0] == group_a]
    b_caps = [c for c in _all_cap_names() if c.split(".")[0] == (group_b or group_a)]
    if not a_caps:
        return {"error": f"no caps in group: {group_a}"}
    if not b_caps:
        return {"error": f"no caps in group: {group_b or group_a}"}
    pairs: List[Tuple[str, str]] = []
    for f in a_caps:
        for t in b_caps:
            if f == t: continue
            if not overwrite and _db_get(f, t):
                continue
            pairs.append((f, t))
    if not pairs:
        return {"error": "no pairs to process (already filled? use overwrite=true)"}
    jid = _new_job("auto_group", len(pairs))
    asyncio.create_task(_run_pairs(jid, pairs, float(min_strength),
                                   bool(prefer_gpu), int(concurrency)))
    return {"job_id": jid, "total_pairs": len(pairs),
            "groups": [group_a, group_b or group_a]}


@capability(
    "cap_ontology.auto_grid", memory="off",
    http_method="POST", http_path="/cap_ontology/auto_grid",
    http_tags=["cap_ontology"],
    description=(
        "LLM-infer relations across the FULL grid (slow). Use with caution. "
        "Inputs: min_strength (float default 0.4), overwrite (bool false), "
        "concurrency (int 2), prefer_gpu (true), max_pairs (int 1000 — safety cap)."
    ),
)
async def co_auto_grid(
    min_strength: float = 0.4,
    overwrite:    bool  = False,
    concurrency:  int   = 2,
    prefer_gpu:   bool  = True,
    max_pairs:    int   = 1000,
    trace_id=None,
):
    caps = _all_cap_names()
    pairs: List[Tuple[str, str]] = []
    for f in caps:
        for t in caps:
            if f == t: continue
            if not overwrite and _db_get(f, t):
                continue
            pairs.append((f, t))
            if len(pairs) >= int(max_pairs): break
        if len(pairs) >= int(max_pairs): break
    if not pairs:
        return {"error": "no pairs to process (grid full? overwrite=true to redo)"}
    jid = _new_job("auto_grid", len(pairs))
    asyncio.create_task(_run_pairs(jid, pairs, float(min_strength),
                                   bool(prefer_gpu), int(concurrency)))
    return {"job_id": jid, "total_pairs": len(pairs)}


@capability(
    "cap_ontology.job_status", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/job_status",
    http_tags=["cap_ontology"],
    description="Get status of an auto-generation job. Input: job_id.",
)
async def co_job_status(job_id: str, trace_id=None):
    job = _AUTO_JOBS.get(job_id)
    if not job:
        return {"error": "unknown job_id"}
    return job


@capability(
    "cap_ontology.jobs", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/jobs",
    http_tags=["cap_ontology"],
    description="List recent auto-generation jobs.",
)
async def co_jobs(trace_id=None):
    return {"jobs": list(_AUTO_JOBS.values())[-30:]}


@capability(
    "cap_ontology.stats", memory="off", silent=True,
    http_method="GET", http_path="/cap_ontology/stats",
    http_tags=["cap_ontology"],
    description="Aggregate stats: relation count, auto vs manual, group coverage.",
)
async def co_stats(trace_id=None):
    rels = _db_all()
    auto = sum(1 for r in rels if r["auto"])
    manual = len(rels) - auto
    groups: Dict[str, int] = {}
    nodes = set()
    for r in rels:
        nodes.add(r["from"]); nodes.add(r["to"])
        g = r["from"].split(".")[0]
        groups[g] = groups.get(g, 0) + 1
    return {
        "total":        len(rels),
        "auto":         auto,
        "manual":       manual,
        "covered_caps": len(nodes),
        "total_caps":   len(CAPABILITY_REGISTRY),
        "by_group":     groups,
    }


# ─────────────────────────────────────────────────────────────────────────────
# BRIDGE: export cap-mesh as a top-level ontology in the existing system
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "cap_ontology.export_to_ontologies", memory="off",
    http_method="POST", http_path="/cap_ontology/export_to_ontologies",
    http_tags=["cap_ontology", "ontologies"],
    description=(
        "Export the current cap-mesh as a single ontology in the canonical "
        "ontologies.* store (one entity per cap, one relationship per relation). "
        "Useful for the Ontologies Browser and for ontologies.apply consumers. "
        "Inputs: name (str, default 'cap_mesh'), domain (str, default 'capability_mesh'), "
        "min_strength (float, default 0.0)."
    ),
)
async def co_export_to_ontologies(
    name: str = "cap_mesh",
    domain: str = "capability_mesh",
    min_strength: float = 0.0,
    trace_id=None,
):
    rels = [r for r in _db_all() if r["strength"] >= float(min_strength or 0)]
    nodes = set()
    for r in rels:
        nodes.add(r["from"]); nodes.add(r["to"])
    entities = []
    for cap in sorted(nodes):
        cap_obj = CAPABILITY_REGISTRY.get(cap, {})
        entities.append({
            "name": cap,
            "description": (cap_obj.get("description") or "")[:280],
            "attributes": list((cap_obj.get("schema") or {}).get("properties", {}).keys()),
        })
    relationships = [{
        "from": r["from"], "to": r["to"],
        "label": r["relation"] or "related",
        "description": r["description"],
    } for r in rels]
    # Try to find existing cap_mesh ontology
    try:
        from Vera.Orchestration.skills import ONTOLOGIES, ontologies_create, ontologies_update
        existing = next((o for o in ONTOLOGIES.values() if o.get("name") == name), None)
        if existing:
            return await ontologies_update(
                id=existing["id"],
                entities=json.dumps(entities),
                relationships=json.dumps(relationships),
                domain=domain,
                description=f"Auto-exported cap mesh ({len(rels)} relations, {len(nodes)} caps)",
            )
        else:
            return await ontologies_create(
                name=name, description=f"Auto-exported cap mesh ({len(rels)} relations, {len(nodes)} caps)",
                domain=domain,
                entities=json.dumps(entities),
                relationships=json.dumps(relationships),
                tags="cap-mesh,auto-export",
            )
    except Exception as e:
        log.warning("cap_ontology.export_to_ontologies bridge failed: %s", e)
        return {"error": f"bridge failed: {e}",
                "entities": len(entities), "relationships": len(relationships)}


# ─────────────────────────────────────────────────────────────────────────────
# UI PANEL REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_PANEL = _HERE / "cap_ontology_panel.html"
if not _PANEL.exists():
    _PANEL = _HERE.parent / "cap_ontology_panel.html"

from fastapi.responses import HTMLResponse


@APP.get("/cap_ontology/panel", response_class=HTMLResponse)
async def serve_cap_ontology_panel():
    if _PANEL.exists():
        return HTMLResponse(_PANEL.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h2 style='font-family:monospace;color:#c96b6b;padding:24px'>"
        "cap_ontology_panel.html not found</h2>"
    )


register_ui(
    panel_id="cap-ontology",
    label="Cap Ontology",
    icon="◇",
    mode="tab",
    tab_order=13,
    html='<iframe src="/cap_ontology/panel" style="width:100%;height:100%;border:none"></iframe>',
    ui_caps=[
        "cap_ontology.list", "cap_ontology.matrix", "cap_ontology.set",
        "cap_ontology.delete", "cap_ontology.auto_pair",
        "cap_ontology.auto_group", "cap_ontology.suggest",
    ],
)

log.info("cap_ontology: registered (%d existing relations)", _db_count())

"""
cap_ontology_agent_integration.py  —  Inject ontology context into agent calls
==============================================================================
Optional companion module.  When loaded, it provides a helper
`build_ontology_system_prompt_fragment(domain_caps, include_hidden=True)`
that returns a system-prompt fragment describing how the agent's allowed
capabilities relate to one another, plus *adjacency* hints to capabilities
the agent cannot call directly (described by relation only — name+schema
are intentionally hidden).

It is consumed by:
  - agents.py        — when constructing the agent's system prompt
  - DAG planner      — when restricting available_caps for an agent
  - chat_panel       — when composing tool_caps lists

Why it lives in its own module:
  - Keeps cap_ontology.py focused on storage + CRUD
  - Keeps agents.py free of ontology imports (decouples failure modes)
  - Allows opt-in: if this module isn't loaded, agents work as before

Usage:
    from cap_ontology_agent_integration import build_ontology_system_prompt_fragment
    sys_extra = await build_ontology_system_prompt_fragment(agent.domain_caps)
    full_system_prompt = base_system + ("\n\n" + sys_extra if sys_extra else "")
"""

# from __future__ import annotations

import logging
from typing import List, Optional

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import capability, CAPABILITY_REGISTRY

log = logging.getLogger("vera.cap_ontology.agent")


async def build_ontology_system_prompt_fragment(
    domain_caps: List[str],
    include_hidden: bool = True,
    max_relations: int = 60,
) -> str:
    """
    Returns a planner-ready snippet describing capability relations relevant
    to the given agent. Returns "" if no relations exist.

    The snippet has two sections:
      1. relations between caps the agent can call
      2. (if include_hidden) edges to capabilities the agent CANNOT call,
         described only by the relation label — the hidden cap's name and
         schema are not exposed.
    """
    co_ctx = CAPABILITY_REGISTRY.get("cap_ontology.context_for")
    if not co_ctx or not co_ctx.get("func"):
        return ""
    try:
        result = await co_ctx["func"](
            available_caps=",".join(domain_caps or []),
            include_hidden=bool(include_hidden),
            max_relations=int(max_relations),
        )
    except Exception as e:
        log.debug("cap_ontology context_for failed: %s", e)
        return ""
    snippet = (result or {}).get("snippet", "")
    if not snippet:
        return ""
    return (
        "## Capability mesh (how the tools relate)\n"
        + snippet
        + "\n\n(These are documented relations between capabilities. "
        "Edges to <hidden capability> describe adjacent functionality you cannot "
        "call directly — useful for understanding the broader system, but do not "
        "attempt to invoke a hidden capability.)"
    )


@capability(
    "cap_ontology.agent_context", memory="off", silent=True,
    http_method="POST", http_path="/cap_ontology/agent_context",
    http_tags=["cap_ontology"],
    description=(
        "Build the ontology system-prompt fragment for an agent. "
        "Inputs: domain_caps (CSV or JSON list), include_hidden (bool default true), "
        "max_relations (int default 60). Output: {fragment}."
    ),
)
async def co_agent_context(
    domain_caps:    str,
    include_hidden: bool = True,
    max_relations:  int  = 60,
    trace_id=None,
):
    import json as _json
    if isinstance(domain_caps, str):
        try:
            caps = _json.loads(domain_caps)
            if not isinstance(caps, list):
                raise ValueError
        except Exception:
            caps = [c.strip() for c in domain_caps.split(",") if c.strip()]
    else:
        caps = list(domain_caps or [])
    fragment = await build_ontology_system_prompt_fragment(
        caps, include_hidden=include_hidden, max_relations=max_relations,
    )
    return {"fragment": fragment, "length": len(fragment)}


log.info("cap_ontology_agent_integration: helper registered")