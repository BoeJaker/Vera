"""The agent registry, as capabilities.

`registry_core` owns the record shape and every decision worth testing; this
module owns persistence and the HTTP/MCP surface and as little else as it can
get away with.

Storage mirrors `skills.py` exactly - SQLite primary (always works, survives a
Redis flush), Redis as a secondary cache - because a second storage idiom in
the same estate is a second thing to debug at 2am, and this one is proven.

WHY THIS EXISTS AT ALL: a Loop Lab census row records what ran. It cannot say
who ran it, under which skill, with which tool, or what throwaway scripts got
written along the way, because none of those things exist anywhere Vera can
name. Seeding this registry with the operator's OWN toolkit is what closes
that gap - see `_BUILTIN_ENTRIES`, which is today's census programme written
down as data rather than as a story in a chat log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (   # noqa: F401
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, register_ui, schedule,
)

# The namespace trap: a module under vera/ is loaded by bare filename, so the
# dotted package spelling is not guaranteed to resolve the same way twice.
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.registry import registry_core as RC
except ImportError:                      # pragma: no cover
    from vera.registry import registry_core as RC   # type: ignore

log = logging.getLogger("vera.registry")

#: id -> entry. The SQLite table is the truth; this is the read path.
ENTRIES: Dict[str, Dict[str, Any]] = {}

_SQLITE_PATH = Path(__file__).parent / "vera_registry.db"


def _redis():
    return _orch.REDIS


def _sqlite_init():
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, data TEXT, updated_at TEXT
        );
    """)
    conn.commit()
    return conn


try:
    _c = _sqlite_init(); _c.close()
    log.debug("registry: SQLite ready at %s", _SQLITE_PATH)
except Exception as _e:                                    # pragma: no cover
    log.warning("registry: SQLite init failed: %s", _e)


def _sqlite_save(record: dict):
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        conn.execute("INSERT OR REPLACE INTO entries (id, kind, name, data, updated_at) "
                     "VALUES (?,?,?,?,?)",
                     (record["id"], record.get("kind", ""), record.get("name", ""),
                      json.dumps(record), record.get("updated_at", now_iso())))
        conn.commit(); conn.close()
    except Exception as e:                                 # pragma: no cover
        log.warning("registry sqlite_save: %s", e)


def _sqlite_delete(item_id: str):
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        conn.execute("DELETE FROM entries WHERE id=?", (item_id,))
        conn.commit(); conn.close()
    except Exception as e:                                 # pragma: no cover
        log.warning("registry sqlite_delete: %s", e)


def _sqlite_load_all() -> List[dict]:
    try:
        conn = sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)
        rows = conn.execute("SELECT data FROM entries").fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows if r[0]]
    except Exception as e:                                 # pragma: no cover
        log.warning("registry sqlite_load_all: %s", e)
        return []


async def _save(record: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sqlite_save, record)
    r = _redis()
    if r:
        try:
            await r.set("vera:registry:%s" % record["id"], json.dumps(record))
        except Exception as e:                             # pragma: no cover
            log.debug("registry redis save %s: %s", record["id"], e)


async def _drop(item_id: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sqlite_delete, item_id)
    r = _redis()
    if r:
        try:
            await r.delete("vera:registry:%s" % item_id)
        except Exception as e:                             # pragma: no cover
            log.debug("registry redis delete %s: %s", item_id, e)


# â”€â”€ builtins â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Today's census programme, written down as data. These are seeded once and are
# then ordinary entries - editable, deletable, and NOT re-seeded over an edit
# (see _startup_load), because a builtin that overwrites a human's correction
# every restart is worse than no builtin.

_BUILTIN_ENTRIES: List[Dict[str, Any]] = [
    {
        "kind": "os", "name": "Claude Code",
        "summary": "Agent harness driving Vera from outside over /mcp/call; "
                   "attributes its work as controller=claude_code.",
        "body": "Sessions live as JSONL under ~/.claude/projects/. Vera ingests "
                "them via ide.claude_sessions.* - that is where the prompts and "
                "conversation log behind a census row can be read back.",
        "source": {"origin": "claude-code"},
        "interop": {"cap": "ide.claude_sessions.list", "protocol": "mcp"},
        "tags": ["harness", "operator"],
    },
    {
        "kind": "tool", "name": "/loop",
        "summary": "Self-paced recurring prompt: runs a task, then schedules its "
                   "own next wake-up rather than polling on a fixed interval.",
        "body": "Dynamic mode picks the delay from what it is waiting on. This is "
                "the tool that drives a census programme across many hours "
                "without a human re-triggering each step.",
        "source": {"origin": "claude-code", "path": "bundled:loop"},
        "tags": ["loop", "scheduling", "operator"],
    },
    {
        "kind": "skill", "name": "improve-vera-sandboxed",
        "summary": "Land changes through the sandboxed, adversarially-reviewed, "
                   "gated pipeline rather than editing prod's checkout.",
        "body": "pipeline.begin -> edit the worktree -> commit via "
                "sandbox.exec(where=worktree) -> unittest.run(markers=critical) "
                "-> pipeline.adopt(to=bleeding-edge) -> promote. Section 11 is "
                "the census-driven improvement loop.",
        "source": {"origin": "claude-code",
                   "path": ".claude/skills/improve-vera-sandboxed/SKILL.md"},
        "interop": {"cap": "evolve.pipeline.begin"},
        "tags": ["pipeline", "review", "census"],
    },
    {
        "kind": "loop", "name": "census programme",
        "summary": "Back-to-back Loop Lab tests with no improvement or review "
                   "step, run against one commit to measure the loop itself.",
        "body": "A census IS a sequence of suite tasks sharing a tag "
                "(vera/evolve/census_seed.py). Templates: default, model-compare, "
                "and the per-family sets exec/code/prose/data/research.",
        "source": {"origin": "claude-code",
                   "path": "/home/boejaker/loop-census/run_census.py"},
        "interop": {"cap": "evolve.suite.run"},
        "tags": ["census", "benchmark", "loop"],
        "helpers": [
            {"name": "run_census.py",
             "purpose": "runs one template's goals back to back against v7, "
                        "applying per-goal wall caps and writing one JSONL row "
                        "per goal",
             "path": "/home/boejaker/loop-census/run_census.py"},
            {"name": "run_operator_census.py",
             "purpose": "the same, for operator/browser goals, as a counterpart "
                        "that pre-empts operator regressions the loop census paid "
                        "for",
             "path": "/home/boejaker/loop-census/run_operator_census.py"},
        ],
    },
    {
        "kind": "technique", "name": "prove the code path ran",
        "summary": "Before crediting a fix for a better run, show mechanically "
                   "that the changed code executed at all.",
        "body": "Four improvement claims were withdrawn in one session; every "
                "one of them was caught by this and by nothing else. Two commits "
                "looked vindicated by better numbers and had never executed - one "
                "was gated on a session id that appears nowhere, the other guards "
                "a case every real call already avoids.",
        "source": {"origin": "claude-code"},
        "tags": ["evidence", "census", "discipline"],
    },
    {
        "kind": "technique", "name": "the noise floor",
        "summary": "Two censuses of the SAME commit differed by one capped goal "
                   "and 12.6% wall, so a result smaller than that is not a result.",
        "body": "Runs 49 and 50 ran the same commit: 11/1 vs 10/2 done/capped, "
                "9873s vs 8632s, one goal flipping pass<->cap with no code change, "
                "median per-goal ratio 1.44x and worst 1.71x. The earlier 48->49 "
                "'improvement' (2 goals, 18%) barely clears that and was never "
                "evidence. No single pair of censuses can establish a change this "
                "size; the cheap honest evidence is mechanical, not statistical.",
        "source": {"origin": "claude-code"},
        "tags": ["evidence", "census", "measurement"],
    },
]


async def _startup_load():
    """SQLite is the truth; builtins fill only the gaps.

    A builtin is seeded when its id is ABSENT, never over an existing row. The
    entries below are opinions about how to work, and an opinion that silently
    reinstates itself over a human's correction on every restart is worse than
    no opinion at all.
    """
    try:
        for rec in _sqlite_load_all():
            e = RC.normalise(rec)
            if e["id"]:
                ENTRIES[e["id"]] = e
        seeded = 0
        for raw in _BUILTIN_ENTRIES:
            e = RC.normalise(raw, now=now_iso())
            if e["id"] in ENTRIES:
                continue
            e["owner"] = {"agent": "claude", "session": ""}
            ENTRIES[e["id"]] = e
            await _save(e)
            seeded += 1
        log.info("registry: %d entries loaded (%d builtins seeded)",
                 len(ENTRIES), seeded)
    except Exception as e:                                 # pragma: no cover
        log.warning("registry startup load failed: %s", e)


# â”€â”€ capabilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@capability("registry.list", memory="off", silent=True,
            http_method="GET", http_path="/registry", http_tags=["registry"],
            description="List agent-registry entries - the skills, tools, loops, "
                        "techniques and LLM operating systems an agent drives Vera "
                        "WITH. Query: kind (skill|tool|loop|technique|os), owner "
                        "(agent name), tag, q (search), limit. "
                        "Output: {entries:[...], count, kinds}.",
            contract=_orch._inspection_contract("registry.list", effects=["read"]))
async def registry_list(kind: str = "", owner: str = "", tag: str = "",
                        q: str = "", limit: int = 100, trace_id=None):
    found = RC.search(list(ENTRIES.values()), q, kind=kind, owner=owner,
                      tag=tag, limit=limit)
    return {"entries": found, "count": len(found), "kinds": list(RC.KINDS)}


@capability("registry.get", memory="off", silent=True,
            http_method="GET", http_path="/registry/entry", http_tags=["registry"],
            description="One registry entry by id, with its interoperability "
                        "projection. Inputs: id (str!). "
                        "Output: {ok, entry, interop}.",
            contract=_orch._inspection_contract("registry.get", effects=["read"]))
async def registry_get(id: str = "", trace_id=None):
    e = ENTRIES.get(str(id or "").strip())
    if not e:
        return {"ok": False, "error": "no entry %r" % id,
                "known": sorted(ENTRIES)[:50]}
    return {"ok": True, "entry": e, "interop": RC.to_interop(e)}


@capability("registry.upsert", memory="off",
            http_method="POST", http_path="/registry/upsert", http_tags=["registry"],
            description="Create or update a registry entry. Pass the record: "
                        "{kind!, name!, summary!, body, source:{origin!,path,repo,"
                        "commit}, owner:{agent,session}, interop:{cap,mcp_tool,"
                        "protocol}, tags[], applies_to[], helpers:[{name!,purpose!,"
                        "path}]}. REFUSES an entry that says nothing about itself "
                        "(no summary, no kind, no origin) - pass force=true to "
                        "store it anyway. An update merges: fields it does not "
                        "mention are kept, and created_at is never overwritten. "
                        "Output: {ok, entry, problems[], created}.")
async def registry_upsert(entry: Optional[dict] = None, force: bool = False,
                          trace_id=None):
    entry = entry if isinstance(entry, dict) else {}
    probs = RC.problems(entry)
    if probs and not force:
        return {"ok": False, "problems": probs,
                "hint": "pass force=true to store it anyway"}
    eid = str(entry.get("id") or "").strip() or RC.entry_id(
        str(entry.get("kind") or ""), str(entry.get("name") or ""))
    existing = ENTRIES.get(eid)
    rec = (RC.merge(existing, entry, now=now_iso()) if existing
           else RC.normalise(dict(entry, id=eid), now=now_iso()))
    ENTRIES[rec["id"]] = rec
    await _save(rec)
    emit_event({"type": "registry.upsert", "id": rec["id"], "kind": rec["kind"],
                "name": rec["name"], "created": existing is None})
    return {"ok": True, "entry": rec, "problems": probs, "created": existing is None}


@capability("registry.delete", memory="off",
            http_method="POST", http_path="/registry/delete", http_tags=["registry"],
            description="Remove a registry entry. Inputs: id (str!). "
                        "Output: {ok, id}.")
async def registry_delete(id: str = "", trace_id=None):
    eid = str(id or "").strip()
    if eid not in ENTRIES:
        return {"ok": False, "error": "no entry %r" % eid}
    ENTRIES.pop(eid, None)
    await _drop(eid)
    emit_event({"type": "registry.delete", "id": eid})
    return {"ok": True, "id": eid}


@capability("registry.interop", memory="off", silent=True,
            http_method="GET", http_path="/registry/interop", http_tags=["registry"],
            description="Every entry projected onto the interoperability "
                        "boundaries (contract / resolution / policy / evidence), "
                        "schema vera.agent-registry-entry/v1. This is the view the "
                        "Agent Bridge summary reads. Query: kind. "
                        "Output: {schema, entries:[...], count, external_only}.",
            contract=_orch._inspection_contract("registry.interop", effects=["read"]))
async def registry_interop(kind: str = "", trace_id=None):
    rows = [RC.to_interop(e) for e in RC.search(list(ENTRIES.values()), "",
                                                kind=kind, limit=500)]
    return {"schema": "vera.agent-registry-entry/v1", "entries": rows,
            "count": len(rows),
            # Named, not implied: an entry nothing inside Vera can invoke is an
            # external technique, not a broken registration.
            "external_only": sum(1 for r in rows if r["resolution"]["external_only"])}


@capability("registry.sync_skill", memory="off",
            http_method="POST", http_path="/registry/sync_skill",
            http_tags=["registry"],
            description="Project a registry entry into Vera's own skills library "
                        "so chat and loops can attach it, keeping the provenance "
                        "tags that say where it really came from. Idempotent: "
                        "re-syncing updates the same skill rather than making a "
                        "second one. Inputs: id (str!). "
                        "Output: {ok, skill_id, created}.")
async def registry_sync_skill(id: str = "", trace_id=None):
    e = ENTRIES.get(str(id or "").strip())
    if not e:
        return {"ok": False, "error": "no entry %r" % id}
    payload = RC.to_vera_skill(e)
    create = CAPABILITY_REGISTRY.get("skills.create")
    update = CAPABILITY_REGISTRY.get("skills.update")
    if not create:
        return {"ok": False, "error": "skills.* is not loaded on this instance"}
    # skills.create mints its OWN uuid id and takes tags as a comma-separated
    # STRING, so the entry's id cannot be the skill's. The `registry:<id>` tag
    # is the join key instead, and finding it is what makes a re-sync an update
    # rather than a duplicate.
    marker = "registry:%s" % e["id"]
    sk = sys.modules.get("skills")
    existing_id = ""
    for sid_, rec in (getattr(sk, "SKILLS", {}) or {}).items() if sk else []:
        if marker in (rec.get("tags") or []):
            existing_id = sid_
            break
    args = {"name": payload["name"], "content": payload["content"],
            "description": payload["description"], "type": payload["type"],
            "tags": ",".join(payload["tags"]), "enabled": True}
    if existing_id and update:
        await update["func"](id=existing_id, **args)
        return {"ok": True, "skill_id": existing_id, "created": False}
    res = await create["func"](**args)
    new_id = (res or {}).get("id") or (res or {}).get("skill", {}).get("id", "")
    return {"ok": True, "skill_id": new_id, "created": True}


@capability("registry.import_skill", memory="off",
            http_method="POST", http_path="/registry/import_skill",
            http_tags=["registry"],
            description="Read a Vera skill back into the registry. A skill that "
                        "originally came from an external agent keeps that origin "
                        "- a round trip must not launder it into a Vera-native "
                        "one. Inputs: skill_id (str!). Output: {ok, entry}.")
async def registry_import_skill(skill_id: str = "", trace_id=None):
    sk = sys.modules.get("skills")
    rec = (getattr(sk, "SKILLS", {}) or {}).get(str(skill_id or "").strip()) if sk else None
    if not rec:
        return {"ok": False, "error": "no skill %r" % skill_id}
    e = RC.from_vera_skill(rec)
    e["id"] = e["id"] or RC.entry_id(e["kind"], e["name"])
    e = RC.normalise(e, now=now_iso())
    ENTRIES[e["id"]] = e
    await _save(e)
    return {"ok": True, "entry": e}


# Same startup shape skills.py uses. NOT `schedule(...)` - that takes a required
# interval, and getting it wrong here raises at import time, which does not fail
# one capability, it removes the module.
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup_load())
except Exception:                                          # pragma: no cover
    pass
