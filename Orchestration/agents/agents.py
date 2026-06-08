"""
vera_agents.py  —  Agent System for Vera
=========================================
Agents are named, configurable LLM personas that combine:
  • A specific Ollama model + full generation parameters
  • A system/personality prompt
  • A domain focus (subset of capabilities they're allowed to use)
  • An optional tool-use mode (DAG planner, capability caller)

Agent records are stored in Redis (always) and Postgres (if available).
Each agent is also a registered @capability so it can be called from DAGs.

Architecture
────────────
  AgentRecord   — dataclass with all config fields
  AgentRegistry — CRUD + Redis/Postgres persistence
  AgentRunner   — executes agent turns (text + optional TTS)

Capabilities registered
────────────────────────
  agent.create          — create/update an agent definition
  agent.list            — list all agents
  agent.get             — get agent by id or name
  agent.delete          — soft-delete
  agent.chat            — send a message to an agent, get text response
  agent.chat_voice      — send message, get text + TTS audio (GPU server)
  agent.call_with_tools — agent that can invoke Vera capabilities as tools

UI panels registered
─────────────────────
  agents-editor   — create/edit/delete agents
  chat-interface  — full chat with STT (mic) + TTS (speaker)

Configurable model parameters (all optional, server defaults used if not set)
──────────────────────────────────────────────────────────────────────────────
  temperature       0.0–2.0   creativity vs determinism
  top_p             0.0–1.0   nucleus sampling
  top_k             int       token candidate pool
  repeat_penalty    1.0–2.0   penalise repetition
  repeat_last_n     int       look-back window for repeat penalty
  num_ctx           int       context window (tokens)
  num_predict       int       max tokens to generate
  seed              int       reproducibility (-1 = random)
  mirostat          0|1|2     Mirostat sampling mode
  mirostat_tau      float     Mirostat target entropy
  mirostat_eta      float     Mirostat learning rate
  tfs_z             float     Tail-free sampling
  stop              list      Stop sequences
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import contextlib
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    APP,            # noqa
    CAPABILITY_REGISTRY, OLLAMA_INSTANCES, OLLAMA_MODEL,
    capability, emit_event, now_iso, ollama_generate, pick_instance, schedule,
    record_stream_activity, begin_stream_activity, end_stream_activity,
    register_ui,
)

# Lazy import helper for DAG execution — avoids circular import at load time
def _get_dag_runner():
    """Return (plan_dag, _hitl_run_graph_stream, _HITL_PENDING) from orch module."""
    import Vera.Orchestration.capability_orchestration as _m
    return (
        getattr(_m, 'plan_dag', None),
        getattr(_m, '_hitl_run_graph_stream', None),
        getattr(_m, '_HITL_PENDING', None),
    )

log = logging.getLogger("vera.agents")

GPU_INFER_URL   = cfg.GPU_INFER_URL
OLLAMA_EMBED_URL = cfg.OLLAMA_EMBED_URL

def _redis(): return _orch.REDIS
def _pg():    return _orch.PG_POOL

# ─────────────────────────────────────────────────────────────────────────────
# AGENT RECORD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentRecord:
    """
    Complete agent definition.

    Identity
    ────────
    id          : globally unique UUID
    name        : short slug used in caps, e.g. "scheduler", "code-reviewer"
    label       : human display name, e.g. "System Scheduler"
    description : what this agent does
    avatar      : emoji or short string shown in UI

    Model config
    ────────────
    model           : Ollama model tag, e.g. "mistral", "llama3.2", "codellama"
    instance_id     : prefer this Ollama instance, empty = auto
    prefer_gpu      : route to GPU instance when available
    temperature     : 0.0 (deterministic) – 2.0 (very creative)
    top_p           : nucleus sampling, 0.0–1.0
    top_k           : top-k candidates, 0 = disabled
    repeat_penalty  : penalise repeated tokens, 1.0 = off
    repeat_last_n   : look-back for repeat penalty, -1 = full context
    num_ctx         : context window in tokens, 0 = model default
    num_predict     : max output tokens, -1 = unlimited
    seed            : -1 = random
    mirostat        : 0=off, 1=v1, 2=v2
    mirostat_tau    : target entropy (Mirostat)
    mirostat_eta    : learning rate (Mirostat)
    tfs_z           : tail-free sampling z, 1.0 = disabled
    stop            : list of stop sequences

    Personality
    ───────────
    system_prompt   : the core personality / role / rules
    greeting        : optional greeting when chat starts
    voice           : TTS voice for audio responses
    tts_speed       : TTS speed multiplier
    tts_engine      : "" = server default, "kokoro", "coqui"

    Domain / capabilities
    ─────────────────────
    domain_caps     : list of cap names this agent is allowed to call as tools
                      empty = no tool use; ["*"] = all caps
    domain_description : natural language description of the agent's domain
    tool_mode       : "none" | "plan" | "call"
                      "none"  — pure chat, no tool access
                      "call"  — agent can explicitly call individual caps
                      "plan"  — agent can build and run DAGs via plan_dag

    Status
    ──────
    created_at, updated_at, archived, author
    """
    # Identity
    id:           str  = field(default_factory=lambda: str(uuid.uuid4()))
    name:         str  = ""
    label:        str  = ""
    description:  str  = ""
    avatar:       str  = "🤖"

    # Model config
    model:          str   = ""          # empty = use OLLAMA_MODEL
    instance_id:    str   = ""          # empty = auto-pick
    prefer_gpu:     bool  = True
    temperature:    float = 0.7
    top_p:          float = 0.9
    top_k:          int   = 40
    repeat_penalty: float = 1.1
    repeat_last_n:  int   = 64
    num_ctx:        int   = 4096
    num_predict:    int   = -1
    seed:           int   = -1
    mirostat:       int   = 0
    mirostat_tau:   float = 5.0
    mirostat_eta:   float = 0.1
    tfs_z:          float = 1.0
    stop:           List[str] = field(default_factory=list)

    # Personality
    system_prompt:  str   = ""
    greeting:       str   = ""
    voice:          str   = "af_heart"
    tts_speed:      float = 1.0
    tts_engine:     str   = ""

    # Domain
    domain_caps:         List[str] = field(default_factory=list)
    domain_description:  str       = ""
    tool_mode:           str       = ""  # '' | 'none' | 'call' | 'plan'
    think:               bool      = False    # prepend chain-of-thought instruction
    skill_ids:           List[str] = field(default_factory=list)
    ontology_ids:        List[str] = field(default_factory=list)

    # Status
    created_at:  str  = field(default_factory=now_iso)
    updated_at:  str  = field(default_factory=now_iso)
    archived:    bool = False
    author:      str  = "user"

    # Memory
    memory_enabled:     bool  = True   # store turns in memory backends
    memory_inject:      bool  = False  # inject past memories into system prompt
    memory_inject_limit:int   = 5      # how many memories to inject
    memory_tags:        str   = ""     # extra comma-separated tags for memory filtering

    def to_dict(self) -> dict:
        return asdict(self)

    def ollama_options(self) -> dict:
        """Build the Ollama options dict (only include non-default values)."""
        opts: dict = {}
        if self.temperature != 0.7:   opts["temperature"]    = self.temperature
        if self.top_p != 0.9:         opts["top_p"]          = self.top_p
        if self.top_k != 40:          opts["top_k"]          = self.top_k
        if self.repeat_penalty != 1.1: opts["repeat_penalty"] = self.repeat_penalty
        if self.repeat_last_n != 64:  opts["repeat_last_n"]  = self.repeat_last_n
        if self.num_ctx != 4096:      opts["num_ctx"]        = self.num_ctx
        if self.num_predict != -1:    opts["num_predict"]    = self.num_predict
        if self.seed != -1:           opts["seed"]           = self.seed
        if self.mirostat != 0:        opts["mirostat"]       = self.mirostat
        if self.mirostat_tau != 5.0:  opts["mirostat_tau"]  = self.mirostat_tau
        if self.mirostat_eta != 0.1:  opts["mirostat_eta"]  = self.mirostat_eta
        if self.tfs_z != 1.0:         opts["tfs_z"]          = self.tfs_z
        if self.stop:                  opts["stop"]           = self.stop
        return opts


# ─────────────────────────────────────────────────────────────────────────────
# AGENT REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class AgentRegistry:
    _PREFIX = "vera:agents:"
    _CACHE:  Dict[str, AgentRecord] = {}

    async def pg_init(self):
        pg = _pg()
        if not pg: return
        try:
            async with pg.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS vera_agents (
                        id              TEXT PRIMARY KEY,
                        name            TEXT NOT NULL UNIQUE,
                        label           TEXT NOT NULL DEFAULT '',
                        description     TEXT NOT NULL DEFAULT '',
                        avatar          TEXT NOT NULL DEFAULT '🤖',
                        model           TEXT NOT NULL DEFAULT '',
                        instance_id     TEXT NOT NULL DEFAULT '',
                        prefer_gpu      BOOLEAN NOT NULL DEFAULT TRUE,
                        temperature     FLOAT NOT NULL DEFAULT 0.7,
                        top_p           FLOAT NOT NULL DEFAULT 0.9,
                        top_k           INT NOT NULL DEFAULT 40,
                        repeat_penalty  FLOAT NOT NULL DEFAULT 1.1,
                        repeat_last_n   INT NOT NULL DEFAULT 64,
                        num_ctx         INT NOT NULL DEFAULT 4096,
                        num_predict     INT NOT NULL DEFAULT -1,
                        seed            INT NOT NULL DEFAULT -1,
                        mirostat        INT NOT NULL DEFAULT 0,
                        mirostat_tau    FLOAT NOT NULL DEFAULT 5.0,
                        mirostat_eta    FLOAT NOT NULL DEFAULT 0.1,
                        tfs_z           FLOAT NOT NULL DEFAULT 1.0,
                        stop            JSONB NOT NULL DEFAULT '[]',
                        system_prompt   TEXT NOT NULL DEFAULT '',
                        greeting        TEXT NOT NULL DEFAULT '',
                        voice           TEXT NOT NULL DEFAULT 'af_heart',
                        tts_speed       FLOAT NOT NULL DEFAULT 1.0,
                        tts_engine      TEXT NOT NULL DEFAULT '',
                        domain_caps     JSONB NOT NULL DEFAULT '[]',
                        domain_description TEXT NOT NULL DEFAULT '',
                        tool_mode       TEXT NOT NULL DEFAULT '',
                        created_at      TIMESTAMPTZ NOT NULL,
                        updated_at      TIMESTAMPTZ NOT NULL,
                        archived        BOOLEAN NOT NULL DEFAULT FALSE,
                        author          TEXT NOT NULL DEFAULT 'user'
                    )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS va_name ON vera_agents(name)")
                # Migration: old rows have tool_mode = 'none' from the old DEFAULT.
                # '' means "not explicitly set" — the UI treats it as tools-capable.
                # Rows where the user explicitly chose 'none' are left alone.
                # We only update rows where tool_mode = 'none' AND domain_caps is
                # non-empty (i.e. the user did configure caps, so 'none' was a
                # leftover default rather than a deliberate choice).
                await conn.execute("""
                    UPDATE vera_agents
                    SET tool_mode = ''
                    WHERE tool_mode = 'none'
                    AND domain_caps != '[]'::JSONB
                """)
            log.info("AgentRegistry: Postgres table ready")
        except Exception as e:
            log.warning("AgentRegistry pg_init: %s", e)

    async def _save_to_fabric(self, rec: AgentRecord):
        """Persist agent to the data fabric — fabric is the durable source
        of truth, Redis/Postgres are caches.

        Two writes happen:
          1. dataset_id="agents" — exactly ONE row per agent (id =
             "agent-{rec.id}"). Writing again UPDATES this row in place.
             This is the live config.
          2. dataset_id="agents_archive" — ONE row per save (id =
             "agent-{rec.id}-{timestamp}"). Builds a change history.
             A later `agent.history` capability can read these.

        Architecture choices:
          • Bypasses fabric.ingest_dataset because that always creates a
            fresh uuid; we need deterministic ids so saves overwrite.
          • Goes direct to SQLite (the primary fabric backend) so we don't
            depend on whether Postgres / Chroma / Neo4j are reachable.
          • Best-effort updates on Postgres + Chroma when available — but
            SQLite alone is enough to survive a Redis flush.
          • All list/dict fields are JSON-encoded so they round-trip through
            the data column cleanly (the column is JSON text in SQLite).
        """
        try:
            fabric = sys.modules.get("data_fabric")
            if not fabric:
                return

            # Build the data payload. Lists/dicts are serialised so they
            # survive any backend that stores `data` as a single text column.
            payload = rec.to_dict()  # dict; lists are still lists here
            text = " ".join(filter(None, [
                rec.name, rec.label, rec.description,
                rec.system_prompt[:500] if rec.system_prompt else "",
                rec.domain_description,
            ]))[:2000]

            # ── PRIMARY (current state) ──────────────────────────────
            primary_id = f"agent-{rec.id}"
            primary_row = {
                "id":         primary_id,
                "dataset_id": "agents",
                "text":       text,
                "data":       payload,
                "source_id":  rec.id,
                "tags":       ["agent", rec.name],
                "created_at": rec.updated_at or now_iso(),
            }

            # ── ARCHIVE (change history, append-only) ────────────────
            archive_id = f"agent-{rec.id}-{int(time.time()*1000)}"
            archive_row = {
                "id":         archive_id,
                "dataset_id": "agents_archive",
                "text":       text,
                "data":       payload,
                "source_id":  rec.id,
                "tags":       ["agent_archive", rec.name],
                "created_at": rec.updated_at or now_iso(),
            }

            # SQLite write (always available) — INSERT OR REPLACE keyed on id
            # so the primary row updates in place; archive row is unique per save.
            sqlite_insert = getattr(fabric, "_sqlite_insert_record", None)
            if sqlite_insert:
                try:
                    await sqlite_insert(primary_row)
                    await sqlite_insert(archive_row)
                except Exception as e:
                    log.warning("AgentRegistry fabric SQLite write: %s", e)

            # Postgres mirror: best-effort. ON CONFLICT DO UPDATE so the
            # primary row updates rather than being silently skipped.
            fabric_pg = getattr(fabric, "FABRIC_PG", None)
            if fabric_pg and getattr(fabric_pg, "_pool", None):
                try:
                    async with fabric_pg._pool.acquire() as conn:
                        # Primary
                        await conn.execute(
                            "INSERT INTO fabric_records "
                            "(id,dataset_id,text,data,source_id,tags,created_at) "
                            "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                            "ON CONFLICT(id) DO UPDATE SET "
                            "text=EXCLUDED.text, data=EXCLUDED.data, "
                            "source_id=EXCLUDED.source_id, tags=EXCLUDED.tags",
                            primary_id, "agents", text, json.dumps(payload),
                            rec.id, json.dumps(["agent", rec.name]),
                            rec.updated_at or now_iso(),
                        )
                        # Archive
                        await conn.execute(
                            "INSERT INTO fabric_records "
                            "(id,dataset_id,text,data,source_id,tags,created_at) "
                            "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                            "ON CONFLICT(id) DO NOTHING",
                            archive_id, "agents_archive", text, json.dumps(payload),
                            rec.id, json.dumps(["agent_archive", rec.name]),
                            rec.updated_at or now_iso(),
                        )
                except Exception as e:
                    log.debug("AgentRegistry fabric PG write: %s", e)

            # Chroma upsert — best-effort, gives the registry semantic search.
            FABRIC_CHROMA = getattr(fabric, "FABRIC_CHROMA", None)
            if FABRIC_CHROMA and getattr(FABRIC_CHROMA, "available", False):
                try:
                    DataRecord = getattr(fabric, "DataRecord", None)
                    if DataRecord:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None,
                            lambda: FABRIC_CHROMA.upsert(DataRecord(
                                id=primary_id, dataset_id="agents",
                                text=text, source_id=rec.id,
                                tags=["agent", rec.name],
                            )),
                        )
                except Exception as e:
                    log.debug("AgentRegistry fabric Chroma upsert: %s", e)
        except Exception as e:
            log.debug("AgentRegistry fabric save: %s", e)

    @staticmethod
    async def _load_from_fabric() -> list[AgentRecord]:
        """Load all agents from the data fabric.

        Reads ONLY the primary `agents` dataset (not the archive). With
        the deterministic-id scheme each agent has exactly one primary
        row, so dedup is incidental — but we still keep newest-by-
        updated_at as a safety net for legacy rows that may have leaked
        in from the old append-on-save behaviour.
        """
        try:
            fabric = sys.modules.get("data_fabric")
            if not fabric:
                return []
            results = await fabric.query_dataset(
                dataset_id="agents",
                query={"limit": 2000, "include_data": True},
            )
            by_id: Dict[str, AgentRecord] = {}
            parse_fail = 0
            for r in (results or []):
                data = r.get("data") or {}
                # Some legacy rows stored data as a JSON string — handle that.
                if isinstance(data, str):
                    try: data = json.loads(data)
                    except Exception: data = {}
                if not isinstance(data, dict): continue
                if not data.get("id") or not data.get("name"):
                    continue
                try:
                    # Restore JSON-encoded list fields (legacy "_json" siblings)
                    for field in ("domain_caps", "stop", "skill_ids", "ontology_ids"):
                        # Prefer the live list field if present
                        if field in data and isinstance(data[field], list):
                            continue
                        # Old "_json" sibling
                        legacy = data.get(f"{field}_json")
                        if legacy:
                            try: data[field] = json.loads(legacy)
                            except Exception: data[field] = []
                            continue
                        # String form
                        if isinstance(data.get(field), str):
                            try: data[field] = json.loads(data[field])
                            except Exception: data[field] = []
                            continue
                        data.setdefault(field, [])
                    # Cast numeric fields (came back as strings from old saves)
                    for fld, typ in [("temperature",float),("top_p",float),("top_k",int),
                                     ("repeat_penalty",float),("repeat_last_n",int),
                                     ("num_ctx",int),("num_predict",int),("seed",int),
                                     ("mirostat",int),("mirostat_tau",float),
                                     ("mirostat_eta",float),("tfs_z",float),
                                     ("tts_speed",float),("memory_inject_limit",int)]:
                        if fld in data:
                            try: data[fld] = typ(data[fld])
                            except Exception: pass
                    for fld in ("prefer_gpu","think","memory_enabled","memory_inject","archived"):
                        if fld in data:
                            v = data[fld]
                            if isinstance(v, bool): continue
                            data[fld] = str(v).lower() in ("true","1","yes")
                    cand = AgentRecord(**{k: v for k, v in data.items()
                                          if k in AgentRecord.__dataclass_fields__})
                    existing = by_id.get(cand.id)
                    if existing is None or (cand.updated_at or "") > (existing.updated_at or ""):
                        by_id[cand.id] = cand
                except Exception as e:
                    parse_fail += 1
                    log.debug("AgentRegistry fabric row parse: %s", e)
            # Second pass: dedupe by name. Multiple agents in fabric may share
            # a name (legacy: each "save" created a new agent_id, but the user
            # always typed the same name). The chat panel keys agents by name
            # in its dropdown so we MUST collapse same-name records here, or
            # the most-recently-warmed Redis hash for a given name will be a
            # gamble. Newest wins by updated_at, then created_at as tiebreak.
            by_name: Dict[str, AgentRecord] = {}
            for cand in by_id.values():
                existing = by_name.get(cand.name)
                if existing is None:
                    by_name[cand.name] = cand
                    continue
                new_ts = cand.updated_at or cand.created_at or ""
                old_ts = existing.updated_at or existing.created_at or ""
                if new_ts > old_ts:
                    by_name[cand.name] = cand
            log.info("AgentRegistry _load_from_fabric: %d agents (by id: %d, by name: %d) from %d fabric rows (%d parse failures)",
                     len(by_name), len(by_id), len(by_name), len(results or []), parse_fail)
            return list(by_name.values())
        except Exception as e:
            log.debug("AgentRegistry _load_from_fabric: %s", e)
            return []

    @staticmethod
    async def _load_history_from_fabric(agent_id: str, limit: int = 50) -> list[Dict]:
        """Read the change history for one agent from the agents_archive dataset.

        Returns a list of {created_at, data} dicts in newest-first order.
        Used by the agent.history capability — supports the user's request
        to keep an archive of changes alongside the live record.
        """
        try:
            fabric = sys.modules.get("data_fabric")
            if not fabric:
                return []
            results = await fabric.query_dataset(
                dataset_id="agents_archive",
                query={"limit": max(limit, 50) * 4, "include_data": True},
            )
            out = []
            for r in (results or []):
                data = r.get("data") or {}
                if isinstance(data, str):
                    try: data = json.loads(data)
                    except Exception: continue
                if not isinstance(data, dict): continue
                if data.get("id") != agent_id: continue
                out.append({
                    "created_at": r.get("created_at") or data.get("updated_at") or "",
                    "id": r.get("id"),
                    "data": data,
                })
            out.sort(key=lambda x: x["created_at"], reverse=True)
            return out[:limit]
        except Exception as e:
            log.debug("_load_history_from_fabric: %s", e)
            return []

    async def save(self, rec: AgentRecord) -> AgentRecord:
        rec.updated_at = now_iso()

        # Cache invalidation: if this name was previously bound to a DIFFERENT
        # id, evict the old id's cache entry and Redis hash so the chat UI
        # can't accidentally fetch the stale record. Without this, the OLD
        # AgentRecord object lived on under id:abc and continued to show up
        # in list_all() responses, masking the new save.
        prior = self._CACHE.get(f"name:{rec.name}")
        if prior is not None and prior.id != rec.id:
            self._CACHE.pop(prior.id, None)
            r0 = _redis()
            if r0:
                try:
                    await r0.delete(f"{self._PREFIX}{prior.id}")
                except Exception:
                    pass

        self._CACHE[rec.id] = rec
        self._CACHE[f"name:{rec.name}"] = rec

        r = _redis()
        if r:
            try:
                data = {}
                for k, v in asdict(rec).items():
                    data[k] = json.dumps(v) if isinstance(v, (list, dict, bool)) else str(v)
                await r.hset(f"{self._PREFIX}{rec.id}", mapping=data)
                await r.set(f"vera:agent_names:{rec.name}", rec.id)
            except Exception as e:
                log.warning("AgentRegistry Redis save: %s", e)

        pg = _pg()
        if pg:
            try:
                async with pg.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO vera_agents VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                            $17,$18,$19,$20,$21::JSONB,$22,$23,$24,$25,$26,$27::JSONB,$28,$29,
                            $30::TIMESTAMPTZ,$31::TIMESTAMPTZ,$32,$33
                        ) ON CONFLICT (id) DO UPDATE SET
                            name=EXCLUDED.name, label=EXCLUDED.label,
                            description=EXCLUDED.description, avatar=EXCLUDED.avatar,
                            model=EXCLUDED.model, instance_id=EXCLUDED.instance_id,
                            prefer_gpu=EXCLUDED.prefer_gpu, temperature=EXCLUDED.temperature,
                            top_p=EXCLUDED.top_p, top_k=EXCLUDED.top_k,
                            repeat_penalty=EXCLUDED.repeat_penalty, repeat_last_n=EXCLUDED.repeat_last_n,
                            num_ctx=EXCLUDED.num_ctx, num_predict=EXCLUDED.num_predict,
                            seed=EXCLUDED.seed, mirostat=EXCLUDED.mirostat,
                            mirostat_tau=EXCLUDED.mirostat_tau, mirostat_eta=EXCLUDED.mirostat_eta,
                            tfs_z=EXCLUDED.tfs_z, stop=EXCLUDED.stop,
                            system_prompt=EXCLUDED.system_prompt, greeting=EXCLUDED.greeting,
                            voice=EXCLUDED.voice, tts_speed=EXCLUDED.tts_speed,
                            tts_engine=EXCLUDED.tts_engine, domain_caps=EXCLUDED.domain_caps,
                            domain_description=EXCLUDED.domain_description,
                            tool_mode=EXCLUDED.tool_mode, updated_at=EXCLUDED.updated_at,
                            archived=EXCLUDED.archived
                    """,
                    rec.id, rec.name, rec.label, rec.description, rec.avatar,
                    rec.model, rec.instance_id, rec.prefer_gpu,
                    rec.temperature, rec.top_p, rec.top_k,
                    rec.repeat_penalty, rec.repeat_last_n, rec.num_ctx,
                    rec.num_predict, rec.seed, rec.mirostat,
                    rec.mirostat_tau, rec.mirostat_eta, rec.tfs_z,
                    json.dumps(rec.stop), rec.system_prompt, rec.greeting,
                    rec.voice, rec.tts_speed, rec.tts_engine,
                    json.dumps(rec.domain_caps), rec.domain_description, rec.tool_mode,
                    rec.created_at, rec.updated_at, rec.archived, rec.author,
                    )
            except Exception as e:
                log.warning("AgentRegistry PG save: %s", e)
        # Fabric mirror (fire-and-forget — primary persistent store)
        asyncio.ensure_future(self._save_to_fabric(rec))
        return rec

    async def get(self, agent_id: str) -> Optional[AgentRecord]:
        if agent_id in self._CACHE:
            return self._CACHE[agent_id]
        r = _redis()
        if r:
            try:
                raw = await r.hgetall(f"{self._PREFIX}{agent_id}")
                if raw:
                    rec = self._from_redis(raw)
                    self._CACHE[rec.id] = rec
                    return rec
            except Exception as e:
                log.debug("AgentRegistry get: %s", e)
        pg = _pg()
        if pg:
            try:
                async with pg.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM vera_agents WHERE id=$1 LIMIT 1", agent_id)
                if row:
                    rec = self._from_pg_row(row)
                    asyncio.ensure_future(self.save(rec))
                    self._CACHE[rec.id] = rec
                    return rec
            except Exception as e:
                log.debug("get PG fallback: %s", e)
        return None

    async def get_by_name(self, name: str) -> Optional[AgentRecord]:
        cache_key = f"name:{name}"
        if cache_key in self._CACHE:
            return self._CACHE[cache_key]
        r = _redis()
        if r:
            try:
                aid = await r.get(f"vera:agent_names:{name}")
                if aid:
                    did = aid.decode() if isinstance(aid, bytes) else aid
                    rec = await self.get(did)
                    if rec:
                        self._CACHE[cache_key] = rec
                        return rec
            except Exception:
                pass
        pg = _pg()
        if pg:
            try:
                async with pg.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT * FROM vera_agents WHERE name=$1 AND archived=false LIMIT 1", name)
                if row:
                    rec = self._from_pg_row(row)
                    asyncio.ensure_future(self.save(rec))
                    self._CACHE[cache_key] = rec
                    self._CACHE[rec.id] = rec
                    return rec
            except Exception as e:
                log.debug("get_by_name PG fallback: %s", e)
        return None

    async def list_all(self, include_archived: bool = False) -> List[AgentRecord]:
        """Return one AgentRecord per name. Multiple Redis hashes can exist
        for the same name when ids drift (legacy from when create-vs-update
        was ambiguous, or when fabric restores re-emit old ids); we keep the
        newest by updated_at so the chat dropdown shows each agent exactly
        once with its latest config. Without this de-dupe step the UI was
        sometimes showing a stale row whose model/caps fields were empty
        — hence "agent doesn't load until I save it"."""
        r = _redis()
        results = []
        if r:
            try:
                keys = await r.keys(f"{self._PREFIX}*")
                for k in keys:
                    raw = await r.hgetall(k)
                    if raw:
                        rec = self._from_redis(raw)
                        if not include_archived and rec.archived:
                            continue
                        results.append(rec)
            except Exception as e:
                log.warning("AgentRegistry list: %s", e)
        if not results:
            pg = _pg()
            if pg:
                try:
                    q = ("SELECT * FROM vera_agents ORDER BY name"
                         if include_archived else
                         "SELECT * FROM vera_agents WHERE archived=false ORDER BY name")
                    async with pg.acquire() as conn:
                        rows = await conn.fetch(q)
                    for row in rows:
                        try:
                            rec = self._from_pg_row(row)
                            results.append(rec)
                            asyncio.ensure_future(self.save(rec))
                        except Exception as e:
                            log.debug("list_all PG row: %s", e)
                except Exception as e:
                    log.warning("AgentRegistry list PG fallback: %s", e)
        # Dedupe by name — keep newest by updated_at (then created_at as tiebreak).
        # This is what the chat UI consumes; duplicates here cause the user to
        # see a stale entry first which may be missing fields.
        by_name: Dict[str, AgentRecord] = {}
        for rec in results:
            existing = by_name.get(rec.name)
            if existing is None:
                by_name[rec.name] = rec
                continue
            new_ts = rec.updated_at or rec.created_at or ""
            old_ts = existing.updated_at or existing.created_at or ""
            if new_ts > old_ts:
                by_name[rec.name] = rec
        return sorted(by_name.values(), key=lambda x: x.name)

    async def delete(self, agent_id: str) -> bool:
        rec = await self.get(agent_id)
        if not rec: return False
        rec.archived = True
        await self.save(rec)
        return True

    @staticmethod
    def _from_redis(raw: dict) -> AgentRecord:
        def _d(k, fb=''):
            v = raw.get(k.encode() if isinstance(list(raw.keys())[0], bytes) else k, fb)
            return v.decode() if isinstance(v, bytes) else str(v) if v is not None else fb
        def _j(k, fb):
            try: return json.loads(_d(k, 'null') or 'null') or fb
            except: return fb
        def _f(k, fb): 
            try: return float(_d(k, str(fb)))
            except: return fb
        def _i(k, fb):
            try: return int(float(_d(k, str(fb))))
            except: return fb
        def _b(k, fb):
            v = _d(k, str(fb)).lower()
            return v in ('true', '1', 'yes')

        return AgentRecord(
            id=_d('id', str(uuid.uuid4())), name=_d('name'), label=_d('label'),
            description=_d('description'), avatar=_d('avatar','🤖'),
            model=_d('model'), instance_id=_d('instance_id'),
            prefer_gpu=_b('prefer_gpu', True),
            temperature=_f('temperature',0.7), top_p=_f('top_p',0.9),
            top_k=_i('top_k',40), repeat_penalty=_f('repeat_penalty',1.1),
            repeat_last_n=_i('repeat_last_n',64), num_ctx=_i('num_ctx',4096),
            num_predict=_i('num_predict',-1), seed=_i('seed',-1),
            mirostat=_i('mirostat',0), mirostat_tau=_f('mirostat_tau',5.0),
            mirostat_eta=_f('mirostat_eta',0.1), tfs_z=_f('tfs_z',1.0),
            stop=_j('stop',[]),
            system_prompt=_d('system_prompt'), greeting=_d('greeting'),
            voice=_d('voice','af_heart'), tts_speed=_f('tts_speed',1.0),
            tts_engine=_d('tts_engine'),
            domain_caps=_j('domain_caps',[]),
            domain_description=_d('domain_description'),
            # Default tool_mode to '' (not 'none') when the DB column is NULL.
            # Old rows created before this column was added have NULL here.
            # Returning 'none' caused the Integrated checkbox to be force-disabled
            # for all legacy agents until the user re-saved them. '' means
            # "not explicitly configured" and the UI treats it as capable of tools.
            tool_mode=_d('tool_mode',''),
            skill_ids=_j('skill_ids',[]),
            ontology_ids=_j('ontology_ids',[]),
            think=_b('think', False),
            memory_enabled     =_b('memory_enabled', True),
            memory_inject      =_b('memory_inject', False),
            memory_inject_limit=_i('memory_inject_limit', 5),
            memory_tags        =_d('memory_tags', ''),
            created_at=_d('created_at',now_iso()),
            updated_at=_d('updated_at',now_iso()),
            archived=_b('archived',False), author=_d('author','user'),
        )


    @staticmethod
    def _from_pg_row(row) -> "AgentRecord":
        """Build an AgentRecord from an asyncpg Row (vera_agents table)."""
        def _js(v, fb):
            if v is None: return fb
            if isinstance(v, (list, dict)): return v
            try: return json.loads(v)
            except: return fb
        def _s(v, fb=''):  return str(v) if v is not None else fb
        def _f(v, fb=0.0):
            try: return float(v) if v is not None else fb
            except: return fb
        def _i(v, fb=0):
            try: return int(v) if v is not None else fb
            except: return fb
        def _b(v, fb=False):
            if isinstance(v, bool): return v
            if v is None: return fb
            return str(v).lower() in ('true', '1', 'yes')
        return AgentRecord(
            id=_s(row['id'], str(uuid.uuid4())),
            name=_s(row['name']), label=_s(row['label']),
            description=_s(row['description']), avatar=_s(row['avatar'], '🤖'),
            model=_s(row['model']), instance_id=_s(row['instance_id']),
            prefer_gpu=_b(row['prefer_gpu'], True),
            temperature=_f(row['temperature'], 0.7), top_p=_f(row['top_p'], 0.9),
            top_k=_i(row['top_k'], 40), repeat_penalty=_f(row['repeat_penalty'], 1.1),
            repeat_last_n=_i(row['repeat_last_n'], 64), num_ctx=_i(row['num_ctx'], 4096),
            num_predict=_i(row['num_predict'], -1), seed=_i(row['seed'], -1),
            mirostat=_i(row['mirostat'], 0), mirostat_tau=_f(row['mirostat_tau'], 5.0),
            mirostat_eta=_f(row['mirostat_eta'], 0.1), tfs_z=_f(row['tfs_z'], 1.0),
            stop=_js(row['stop'], []),
            system_prompt=_s(row['system_prompt']), greeting=_s(row['greeting']),
            voice=_s(row['voice'], 'af_heart'), tts_speed=_f(row['tts_speed'], 1.0),
            tts_engine=_s(row['tts_engine']),
            domain_caps=_js(row['domain_caps'], []),
            domain_description=_s(row['domain_description']),
            tool_mode=_s(row['tool_mode'], ''),
            think=_b(row.get('think'), False),
            skill_ids=_js(row.get('skill_ids'), []),
            ontology_ids=_js(row.get('ontology_ids'), []),
            memory_enabled=_b(row.get('memory_enabled'), True),
            memory_inject=_b(row.get('memory_inject'), False),
            memory_inject_limit=_i(row.get('memory_inject_limit'), 5),
            memory_tags=_s(row.get('memory_tags'), ''),
            created_at=_s(row['created_at'], now_iso()),
            updated_at=_s(row['updated_at'], now_iso()),
            archived=_b(row['archived'], False),
            author=_s(row.get('author'), 'user'),
        )


AGENT_REGISTRY = AgentRegistry()


# ── Streaming chat SSE endpoint ───────────────────────────────────────────────
# Mounted outside the @capability system so FastAPI returns StreamingResponse
# Client: EventSource('/agents/chat/stream') with POST polyfill or fetch+ReadableStream

@APP.post("/agents/chat/stream")
async def agent_chat_stream_endpoint(request: Request):
    """
    SSE streaming endpoint for agent chat.
    POST body: {message, agent_name?, agent_id?, history?, session_id?,
                model_override?, instance_id?, prefer_gpu?, think?, tts?}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    agent_name = body.get("agent_name", "assistant")
    agent_id   = body.get("agent_id", "")
    message    = body.get("message", "")
    use_tts    = bool(body.get("tts", False))
    session_id = body.get("session_id", "") or str(uuid.uuid4())
    try:    history = json.loads(body.get("history", "[]"))
    except: history = []

    # ── Set session_id into syslog context var so all downstream caps
    # (ide.fs.write, research.*, etc.) can read it from get_trigger_chain().
    # The trigger_cap name should match the cap_name used by
    # record_stream_activity below ("chat.stream") so downstream cap calls
    # show the correct trigger in their TRIGGERED_BY edges.
    _syslog = sys.modules.get("syslog")
    if _syslog and session_id:
        try:
            _syslog.set_trigger(str(uuid.uuid4()), "chat.stream", session_id)
        except Exception:
            pass

    agent = None
    if agent_id:   agent = await AGENT_REGISTRY.get(agent_id)
    if not agent:  agent = await AGENT_REGISTRY.get_by_name(agent_name)
    if not agent:
        agent = AgentRecord(name="default", model=OLLAMA_MODEL)

    import copy
    agent = copy.copy(agent)
    if body.get("model_override"): agent.model       = body["model_override"]
    if body.get("instance_id"):    agent.instance_id = body["instance_id"]
    if body.get("prefer_gpu"):     agent.prefer_gpu  = True
    if body.get("think"):          agent.think       = True

    # Ensure session node exists in memory graph
    try:
        mem_hooks = sys.modules.get("memory_hooks")
        if mem_hooks and session_id:
            await mem_hooks.get_or_create_session(session_id, agent.name)
    except Exception:
        pass

    # Wrap generator to:
    #   (a) suppress client-disconnect errors (TCPTransport closed) — these
    #       are normal when the browser reloads/navigates away mid-stream;
    #   (b) accumulate the assistant's response so we can record the
    #       interaction into the activity chain on completion. Because this
    #       endpoint is a raw FastAPI route (not a @capability), it would
    #       otherwise be invisible to syslog and the FOLLOWS_ACTIVITY graph.
    #
    # Activity recording uses begin/end pair (not the record_stream_activity
    # wrapper) so the cap.call event fires at the START of the stream. This
    # makes the call visible as a "running" job in the workers panel for the
    # full duration of token generation, then transitions to "done" on cap.ok
    # when the stream completes — instead of materialising only at the end.
    import time as _time
    _stream_t0      = _time.monotonic()
    _resp_chars     = 0
    _resp_head      = []   # first ~1KB of plain-text content for the recording
    _audio_chunks   = 0

    # Open the activity handle BEFORE the stream starts so the workers
    # panel sees a running job immediately. If session_id is empty for
    # whatever reason begin_stream_activity returns None and we silently
    # skip recording (same behaviour as before).
    _act_handle = None
    try:
        _act_handle = await begin_stream_activity(
            cap_name="chat.stream",
            session_id=session_id,
            group="chat",
        )
    except Exception as _e:
        log.warning("begin_stream_activity chat.stream FAILED: %s", _e)

    async def _safe_gen():
        nonlocal _resp_chars, _audio_chunks
        try:
            async for chunk in AGENT_RUNNER.run_stream(
                    agent, message, history, session_id, use_tts=use_tts,
                    system_prefix=body.get("system_prefix", "")):
                # Light-touch content sniff. Stream frames come as
                #   data: {"type":"token","text":"..."}\n\n         (text token)
                #   data: {"type":"thinking","text":"..."}\n\n      (thinking token)
                #   data: {"type":"audio","seq":N,"pcm":"..."}\n\n  (TTS chunk)
                # Old code looked for "type":"text" / "content" — those
                # fields don't exist in the actual stream, so the response
                # text was always empty. Fixed to match the real schema.
                if chunk and isinstance(chunk, (bytes, bytearray)):
                    head = bytes(chunk[:80])
                    if b'"type":"token"' in head:
                        _resp_chars += max(0, len(chunk) - 32)
                        if sum(len(s) for s in _resp_head) < 1024:
                            try:
                                s = chunk.decode("utf-8", "ignore")
                                if '"text":"' in s:
                                    body_text = s.split('"text":"', 1)[1]
                                    # consume up to next un-escaped quote
                                    out_chars = []
                                    i = 0
                                    while i < len(body_text):
                                        c = body_text[i]
                                        if c == '\\' and i + 1 < len(body_text):
                                            out_chars.append(body_text[i+1])
                                            i += 2
                                            continue
                                        if c == '"':
                                            break
                                        out_chars.append(c)
                                        i += 1
                                    _resp_head.append("".join(out_chars))
                            except Exception:
                                pass
                    elif b'"type":"audio"' in head:
                        _audio_chunks += 1
                yield chunk
        except (RuntimeError, ConnectionResetError, BrokenPipeError) as e:
            if "transport" in str(e).lower() or "closed" in str(e).lower():
                log.debug("Client disconnected during stream: %s", e)
            else:
                raise
        finally:
            elapsed_ms = round((_time.monotonic() - _stream_t0) * 1000)
            try:
                if _act_handle is not None:
                    await end_stream_activity(
                        _act_handle,
                        params={
                            "agent_name":   agent.name,
                            "model":        agent.model or OLLAMA_MODEL,
                            "instance_id":  agent.instance_id,
                            "message":      message,
                            "history_len":  len(history or []),
                            "tts":          use_tts,
                            "think":        getattr(agent, "think", False),
                        },
                        result={
                            "agent":         agent.name,
                            "response_chars": _resp_chars,
                            "audio_chunks":  _audio_chunks,
                            "preview":       "".join(_resp_head)[:800],
                            "elapsed_ms":    elapsed_ms,
                        },
                        elapsed_ms=elapsed_ms,
                    )
                else:
                    # Fallback: begin failed (e.g. no session_id yet at start).
                    # Try the convenience wrapper so we at least record the end.
                    await record_stream_activity(
                        cap_name="chat.stream",
                        session_id=session_id,
                        params={
                            "agent_name":   agent.name,
                            "model":        agent.model or OLLAMA_MODEL,
                            "instance_id":  agent.instance_id,
                            "message":      message,
                            "history_len":  len(history or []),
                            "tts":          use_tts,
                            "think":        getattr(agent, "think", False),
                        },
                        result={
                            "agent":         agent.name,
                            "response_chars": _resp_chars,
                            "audio_chunks":  _audio_chunks,
                            "preview":       "".join(_resp_head)[:800],
                            "elapsed_ms":    elapsed_ms,
                        },
                        elapsed_ms=elapsed_ms,
                        group="chat",
                    )
                log.info("chat.stream recorded: session=%s chars=%d elapsed=%dms",
                         (session_id or "")[:12], _resp_chars, elapsed_ms)
            except Exception as _e:
                log.warning("end_stream_activity chat.stream FAILED: %s", _e)

    return StreamingResponse(
        _safe_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )

def _infer_state_from_message(message: str, dag: list) -> dict:
    """Infer required cap params from the user's message when initial_state is empty."""
    import re as _re
    from Vera.Orchestration.capability_orchestration import CAPABILITY_REGISTRY
    state = {}
    urls = _re.findall(r'https?://[\S]+|[\w.-]+\.[a-z]{2,}(?:/[\S]*)?', message, _re.I)
    for node in dag:
        if not isinstance(node, list) or not node: continue
        cap = CAPABILITY_REGISTRY.get(node[0], {})
        for param in set(cap.get('schema', {}).get('required', [])):
            if param in ('trace_id',): continue
            if param in ('host', 'url', 'address', 'target', 'endpoint') and urls:
                val = urls[0]
                if param == 'host':
                    val = _re.sub(r'^https?://', '', val).split('/')[0]
                state[param] = val
            elif param in ('message', 'prompt'):
                state[param] = message
        break
    return state


def _extract_dag_from_text(text: str) -> Optional[dict]:
    """Extract a DAG plan JSON from LLM response text using multiple strategies."""
    import re as _re
    # Strategy 1: fenced ```json blocks
    for block in _re.findall(r'```(?:json)?\s*([\s\S]*?)```', text):
        try:
            p = json.loads(block.strip())
            if isinstance(p, dict) and isinstance(p.get('dag'), list) and p['dag']:
                return p
        except Exception:
            pass
    # Strategy 2: outermost {} containing a "dag" key
    for m in _re.finditer(r'\{[^{}]*"dag"[^{}]*\[[\s\S]*?\][\s\S]*?\}', text):
        try:
            p = json.loads(m.group())
            if isinstance(p, dict) and isinstance(p.get('dag'), list) and p['dag']:
                return p
        except Exception:
            pass
    # Strategy 3: whole text
    try:
        p = json.loads(text.strip())
        if isinstance(p, dict) and isinstance(p.get('dag'), list) and p['dag']:
            return p
    except Exception:
        pass
    return None


class AgentRunner:
    """Execute a single agent turn — text and/or voice."""

    async def run(
        self,
        agent: AgentRecord,
        message: str,
        history: Optional[List[Dict]] = None,
        session_id: str = "",
    ) -> Dict:
        """Generate a text response from the agent using /api/chat."""
        model = agent.model or OLLAMA_MODEL
        opts  = agent.ollama_options()
        think = getattr(agent, "think", False)

        # Build system prompt
        system = agent.system_prompt or ""
        if agent.domain_description:
            system += f"\n\nDomain: {agent.domain_description}"
        if agent.domain_caps and agent.tool_mode != "none":
            cap_names = agent.domain_caps if agent.domain_caps != ["*"] \
                else list(CAPABILITY_REGISTRY.keys())[:40]
            try:
                from vera_dag_store import CAP_INDEX
                sigs = "\n".join(CAP_INDEX.cap_signature(c) for c in cap_names
                                 if c in CAPABILITY_REGISTRY)
                system += f"\n\nAvailable tools you may reference:\n{sigs}"
            except ImportError:
                pass
        # Think mode: native Ollama flag only — no system prompt injection
        # Models like qwen3 have built-in thinking activated by body["think"]=True

        # Memory injection — retrieve relevant past context
        if getattr(agent, 'memory_inject', False) and session_id:
            try:
                mem_hooks = sys.modules.get("memory_hooks")
                if mem_hooks:
                    mem_context = await mem_hooks.get_agent_memory_context(
                        session_id  = session_id,
                        query       = message,
                        agent_name  = agent.name,
                        limit       = getattr(agent, 'memory_inject_limit', 5),
                        tags        = [t.strip() for t in getattr(agent,'memory_tags','').split(',') if t.strip()] or None,
                    )
                    if mem_context:
                        system = system + "\n\n" + mem_context
            except Exception as e:
                log.debug("memory inject: %s", e)

        # Build messages array for /api/chat
        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        # Inject history
        for h in (history or [])[-10:]:
            role = h.get("role", "user")
            content = h.get("content", "")
            thinking = h.get("thinking", "")
            msg = {"role": role, "content": content}
            if thinking and role == "assistant":
                msg["thinking"] = thinking   # pass prior thinking back for context
            messages.append(msg)

        messages.append({"role": "user", "content": message})

        # Route to instance
        chosen = pick_instance(
            prefer_gpu=agent.prefer_gpu,
            instance_id=agent.instance_id or None,
            model=model,
        ) or "cpu-246"
        inst = OLLAMA_INSTANCES.get(chosen, {})
        url  = inst.get("url", "http://192.168.0.246:11435")

        body: dict = {
            "model":    model,
            "messages": messages,
            "stream":   False,
        }
        if opts:   body["options"] = opts
        body["think"] = bool(think)          # explicitly toggle Ollama native thinking

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(f"{url}/api/chat", json=body)
                r.raise_for_status()
                data = r.json()

            msg_out  = data.get("message", {})
            raw_text = msg_out.get("content", "").strip()

            # Extract thinking — native field first, then <think> tags in content
            thinking_out = msg_out.get("thinking", "")
            if not thinking_out and "<think>" in raw_text:
                import re as _re
                m = _re.search(r"<think>(.*?)</think>", raw_text, _re.DOTALL)
                if m:
                    thinking_out = m.group(1).strip()
                    raw_text     = _re.sub(r"<think>.*?</think>", "", raw_text,
                                           flags=_re.DOTALL).strip()

        except Exception as e:
            log.error("AgentRunner.run [%s/%s]: %s", agent.name, model, e)
            raw_text    = f"[Agent error: {e}]"
            thinking_out = ""

        result = {
            "text":       raw_text,
            "agent_id":   agent.id,
            "agent_name": agent.name,
            "model":      model,
            "instance":   chosen,
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "session_id": session_id,
        }
        if thinking_out:
            result["thinking"] = thinking_out

        # Record turn in memory (fire-and-forget)
        if getattr(agent, 'memory_enabled', True) and session_id and raw_text:
            try:
                mem_hooks = sys.modules.get("memory_hooks")
                if mem_hooks:
                    asyncio.create_task(mem_hooks.record_agent_turn(
                        session_id  = session_id,
                        agent_name  = agent.name,
                        agent_id    = agent.id,
                        human_text  = message,
                        ai_text     = raw_text,
                        thinking    = thinking_out,
                        model       = model,
                        trace_id    = "",
                        latency_ms  = result["latency_ms"],
                        tags        = [t.strip() for t in getattr(agent,'memory_tags','').split(',') if t.strip()],
                    ))
            except Exception as e:
                log.debug("memory record turn: %s", e)

        return result

    async def run_stream(
        self,
        agent: AgentRecord,
        message: str,
        history: Optional[List[Dict]] = None,
        session_id: str = "",
        use_tts: bool = False,
        system_prefix: str = "",
    ):
        """
        Single streaming path for both text-only and TTS responses.
        When use_tts=True, synthesises audio after all tokens are delivered.
        system_prefix: prepended to the agent system prompt (skills/ontologies from context.assemble).
        """
        yield b": ping\n\n"

        model  = agent.model or OLLAMA_MODEL
        opts   = agent.ollama_options()
        think  = getattr(agent, "think", False)

        system = agent.system_prompt or ""
        if agent.domain_description:
            system += f"\n\nDomain: {agent.domain_description}"
        # Prepend skills/ontologies/DAGs block from context.assemble if provided
        if system_prefix:
            system = system_prefix.strip() + ("\n\n" + system if system else "")
        # think flag set on body below — native Ollama flag, no system prompt injection

        # Memory injection
        if getattr(agent, 'memory_inject', False) and session_id:
            try:
                _mh = sys.modules.get("memory_hooks")
                if _mh:
                    _ctx = await _mh.get_agent_memory_context(
                        session_id=session_id, query=message, agent_name=agent.name,
                        limit=getattr(agent, 'memory_inject_limit', 5),
                        tags=[t.strip() for t in getattr(agent,'memory_tags','').split(',') if t.strip()] or None,
                    )
                    if _ctx:
                        system = system + "\n\n" + _ctx
            except Exception as e:
                log.debug("run_stream memory inject: %s", e)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for h in (history or [])[-10:]:
            msg = {"role": h.get("role", "user"), "content": h.get("content", "")}
            if h.get("thinking") and h.get("role") == "assistant":
                msg["thinking"] = h["thinking"]
            messages.append(msg)
        messages.append({"role": "user", "content": message})

        chosen = pick_instance(
            prefer_gpu=agent.prefer_gpu,
            instance_id=agent.instance_id or None,
            model=model,
        ) or "cpu-246"
        inst = OLLAMA_INSTANCES.get(chosen, {})
        url  = inst.get("url", "http://192.168.0.246:11435")

        body: dict = {"model": model, "messages": messages, "stream": True}
        if opts:  body["options"] = opts
        body["think"] = bool(think)          # explicitly toggle Ollama native thinking

        full_text        = []
        full_thinking    = []
        in_think         = False
        _eval_count        = 0
        _prompt_eval_count = 0
        _ctx_used          = 0

        # ── Streaming TTS: serial sentence pipeline ───────────────────────────
        # Sentences are queued as text; a single worker synthesises them in order
        # so audio chunks always arrive and play in the correct sequence.
        _tts_buf        = ""
        _tts_text_q     = asyncio.Queue()   # text sentences waiting for synthesis
        _tts_event_q    = asyncio.Queue()   # merged event queue: audio SSE | sentinel
        _TTS_DONE_SENT  = object()          # sentinel to stop the worker

        # Sentence boundary splitter — Python 3.11 compatible (no variable-width lookbehind)
        # Split on [.!?] followed by whitespace, then rejoin abbreviations in post-pass.
        _SENT_SPLIT  = re.compile(r'(?<=[.!?])\s+')
        _ABBREVS     = {'mr','mrs','ms','dr','vs','eg','ie','etc','st',
                        'prof','sr','jr','no','vol','dept','approx','fig',
                        'gov','inc','ltd','corp','jan','feb','mar','apr',
                        'jun','jul','aug','sep','oct','nov','dec'}
        # Threshold tuning: the OPENING sentence often runs short ("Sure!",
        # "Of course.", "Yes — I can help.") and was being swallowed by the
        # 40-char minimum. Then the first audio the user heard was the
        # SECOND or THIRD sentence, several seconds late. Two fixes here:
        #   1. Lower the absolute floor to 15 chars so reasonably short
        #      sentences flush as soon as they end with .!?.
        #   2. Track a "first-flush" flag so the very first complete
        #      sentence flushes regardless of length, kicking off audio
        #      ASAP rather than waiting for buffer to grow.
        _TTS_MIN_CHARS = 15    # was 40 — too restrictive; eats short openers
        _TTS_MAX_CHARS = 400   # hard cap — split at word boundary
        _tts_first_flushed = False   # one-shot: opener flushes regardless of length

        def _split_at_boundary(text: str):
            """Split text at sentence boundaries, rejoining known abbreviations."""
            parts = _SENT_SPLIT.split(text)
            if len(parts) <= 1:
                return [], text.strip()
            # Post-pass: rejoin if previous chunk ends with an abbreviation
            merged = [parts[0]]
            for part in parts[1:]:
                prev = merged[-1]
                last_word = re.search(r'(\w+)[.!?]$', prev)
                if last_word and last_word.group(1).lower() in _ABBREVS:
                    merged[-1] = prev + ' ' + part
                else:
                    merged.append(part)
            # Last element is the remainder (no trailing sentence-end)
            return [s.strip() for s in merged[:-1] if s.strip()], merged[-1].strip()

        def _maybe_flush(force: bool = False):
            """Push complete sentences from _tts_buf into _tts_text_q.
            The very first sentence always flushes even if short — gets
            audio playing immediately rather than queued behind a buffer
            wait. Subsequent short-but-complete sentences also flush at
            >=15 chars; only sub-15-char fragments are buffered."""
            nonlocal _tts_buf, _tts_first_flushed
            sentences, remainder = _split_at_boundary(_tts_buf)
            for s in sentences:
                # First complete sentence always flushes (even single-word) so
                # the user hears the start of the reply within ~1 token-batch.
                # Subsequent sentences need to clear the (lowered) min threshold.
                if force or not _tts_first_flushed or len(s) >= _TTS_MIN_CHARS:
                    _tts_text_q.put_nowait(s)
                    _tts_first_flushed = True
                else:
                    remainder = s + " " + remainder  # too short, keep buffering
            _tts_buf = remainder.strip()
            # Hard cap: if buffer is very long, split at last word boundary
            if len(_tts_buf) >= _TTS_MAX_CHARS:
                cut = _tts_buf.rfind(" ", 0, _TTS_MAX_CHARS)
                if cut > _TTS_MIN_CHARS:
                    _tts_text_q.put_nowait(_tts_buf[:cut].strip())
                    _tts_first_flushed = True
                    _tts_buf = _tts_buf[cut:].strip()

        async def _tts_worker(voice, speed, engine):
            """Serial worker: synthesises one sentence at a time, in order."""
            while True:
                item = await _tts_text_q.get()
                if item is _TTS_DONE_SENT:
                    await _tts_event_q.put(_TTS_DONE_SENT)
                    break
                text = item.strip()
                if not text:
                    continue
                try:
                    _b = {"text": text, "voice": voice, "speed": speed}
                    if engine:
                        _b["engine"] = engine
                    async with httpx.AsyncClient(
                            timeout=httpx.Timeout(60.0, connect=10.0)) as _hc:
                        _hr = await _hc.post(f"{GPU_INFER_URL}/tts", json=_b)
                        if _hr.status_code == 200:
                            _hd = _hr.json()
                            if _hd.get("audio_b64"):
                                sse = f"data: {json.dumps({'type':'audio_chunk','audio_b64':_hd['audio_b64'],'sample_rate':_hd.get('sample_rate',22050),'voice':voice})}\n\n".encode()
                                await _tts_event_q.put(sse)  # push immediately, not after generation
                        else:
                            log.debug("TTS HTTP %s for: %s", _hr.status_code, text[:60])
                except Exception as _te:
                    log.debug("TTS chunk error: %s", _te)

        # Start the serial worker immediately if TTS is requested
        _tts_worker_task = None
        if use_tts:
            _v = agent.voice or "af_heart"
            _sp = agent.tts_speed or 1.0
            _eng = agent.tts_engine or ""
            _tts_worker_task = asyncio.create_task(_tts_worker(_v, _sp, _eng))

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=10.0),
                follow_redirects=True,
            ) as c:
                async with c.stream("POST", f"{url}/api/chat", json=body) as resp:
                    if resp.status_code != 200:
                        body_txt = await resp.aread()
                        yield f"data: {json.dumps({'type':'error','text':f'Ollama {resp.status_code}: {body_txt.decode()[:200]}'})}\n\n".encode()
                        return

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue

                        msg      = chunk.get("message", {})
                        token    = msg.get("content",  "")
                        thinking = msg.get("thinking", "")

                        if thinking:
                            full_thinking.append(thinking)
                            yield f"data: {json.dumps({'type':'thinking','text':thinking})}\n\n".encode()

                        if token:
                            if "<think>" in token:
                                in_think = True
                            if in_think:
                                if "</think>" in token:
                                    in_think = False
                                    parts = token.split("</think>", 1)
                                    full_thinking.append(parts[0])
                                    token = parts[1] if len(parts) > 1 else ""
                                else:
                                    full_thinking.append(token)
                                    continue
                            if token:
                                full_text.append(token)
                                yield f"data: {json.dumps({'type':'token','text':token})}\n\n".encode()
                                if use_tts:
                                    _tts_buf += token
                                    _maybe_flush(force=False)
                                    # Drain any audio chunks the worker has already finished.
                                    # This is non-blocking: only yields what's ready right now.
                                    while not _tts_event_q.empty():
                                        _audio_item = _tts_event_q.get_nowait()
                                        if _audio_item is not _TTS_DONE_SENT:
                                            try:
                                                yield _audio_item
                                            except GeneratorExit:
                                                return

                        if chunk.get("done"):
                            # Capture token usage for context window display
                            _eval_count        = chunk.get("eval_count", 0)
                            _prompt_eval_count = chunk.get("prompt_eval_count", 0)
                            _ctx_used = _eval_count + _prompt_eval_count
                            break

        except Exception as e:
            log.error("run_stream [%s]: %s", agent.name, e)
            try:
                yield f"data: {json.dumps({'type':'error','text':str(e)})}\n\n".encode()
            except Exception:
                pass  # client may already be gone
            return

        final_text     = "".join(full_text)
        final_thinking = "".join(full_thinking)

        # TTS: flush remaining buffer, then drain any audio not yet yielded.
        # Most audio was already sent real-time during token streaming.
        if use_tts and _tts_worker_task:
            try:
                _maybe_flush(force=True)
                if _tts_buf.strip():
                    _tts_text_q.put_nowait(_tts_buf.strip())
                _tts_text_q.put_nowait(_TTS_DONE_SENT)
                # Drain remaining chunks (last sentence(s) that finished after token loop)
                while True:
                    try:
                        _item = await asyncio.wait_for(_tts_event_q.get(), timeout=90.0)
                    except asyncio.TimeoutError:
                        log.warning("TTS drain timeout — skipping remaining audio")
                        break
                    if _item is _TTS_DONE_SENT:
                        break
                    try:
                        yield _item
                    except GeneratorExit:
                        break
            except Exception as _tts_err:
                log.debug("TTS final drain: %s", _tts_err)
            finally:
                _tts_worker_task.cancel()
                try:
                    await _tts_worker_task
                except (asyncio.CancelledError, Exception):
                    pass

        # Determine context window size from agent config
        _num_ctx = getattr(agent, 'num_ctx', 0) or 4096
        done_payload = {
            "type":     "done",
            "text":     final_text,
            "thinking": final_thinking,
            "model":    model,
            "instance": chosen,
            "ctx_used": _ctx_used,
            "ctx_max":  _num_ctx,
            "eval_tokens": _eval_count,
            "prompt_tokens": _prompt_eval_count,
        }
        try:
            yield f"data: {json.dumps(done_payload)}\n\n".encode()
            # If agent has tool_mode=="plan", extract and execute any DAG in the response
            if getattr(agent, 'tool_mode', 'none') == 'plan' and final_text:
                dag_plan = _extract_dag_from_text(final_text)
                if dag_plan and dag_plan.get('dag'):
                    plan_dag_fn, hitl_runner, _ = _get_dag_runner()
                    if hitl_runner:
                        state = dict(dag_plan.get('initial_state') or {})
                        if not state:
                            state = _infer_state_from_message(message, dag_plan['dag'])
                        yield f"data: {json.dumps({'type':'dag.executing','message':'Executing DAG…','steps':len(dag_plan['dag']),'state_keys':list(state.keys())})}\n\n".encode()
                        _dag_steps   = dag_plan['dag']
                        _dag_result  = {}
                        _dag_aborted = None
                        async for ev_type, ev_data in hitl_runner(_dag_steps, state, False, 30):
                            yield f"data: {json.dumps({'type':ev_type,**ev_data})}\n\n".encode()
                            if ev_type == 'dag.complete':
                                _dag_result  = ev_data.get('state', {})
                                _dag_aborted = ev_data.get('aborted_at')
                            elif ev_type == 'dag.step_done' and ev_data.get('out_key'):
                                _dag_result[ev_data['out_key']] = ev_data.get('result_preview', '')
                        # Record DAG to memory graph — pass message text so
                        # record_dag_execution can store the triggering human message
                        # as a graph node even before record_agent_turn runs.
                        try:
                            _mh = sys.modules.get('Vera.Orchestration.fabric.memory_hooks')
                            if _mh and hasattr(_mh, 'record_dag_execution'):
                                asyncio.create_task(_mh.record_dag_execution(
                                    session_id=session_id, dag=_dag_steps,
                                    state=state, result=_dag_result,
                                    agent_name=agent.name, trigger='agent_plan',
                                    aborted_at=_dag_aborted,
                                    trigger_text=message,  # human message that caused this DAG
                                ))
                        except Exception as _de:
                            log.debug("dag graph record: %s", _de)
            yield b"data: [DONE]\n\n"
        except Exception:
            pass  # client disconnected — still record memory below

        # Record turn in memory — runs regardless of whether client received it.
        # This is the critical block: even if the browser reloaded mid-stream,
        # the conversation turn must be persisted.
        if getattr(agent, 'memory_enabled', True) and session_id and final_text:
            try:
                mem_hooks = sys.modules.get("memory_hooks")
                if mem_hooks:
                    # Use create_task so memory write doesn't block generator cleanup
                    asyncio.create_task(mem_hooks.record_agent_turn(
                        session_id  = session_id,
                        agent_name  = agent.name,
                        agent_id    = agent.id,
                        human_text  = message,
                        ai_text     = final_text,
                        thinking    = final_thinking,
                        model       = model,
                        trace_id    = "",
                        latency_ms  = 0,
                        tags        = [t.strip() for t in getattr(agent, 'memory_tags', '').split(',') if t.strip()],
                    ))
                    log.debug("run_stream: memory task created for session %s", session_id[:8])
            except Exception as e:
                log.warning("run_stream memory task: %s", e)

    async def run_with_tts(
        self,
        agent: AgentRecord,
        message: str,
        history: Optional[List[Dict]] = None,
        session_id: str = "",
    ) -> Dict:
        """Generate text response + synthesise it as audio."""
        result = await self.run(agent, message, history, session_id)
        text   = result.get("text", "")
        if not text or text.startswith("[Agent error"):
            return result

        # TTS
        tts_body: dict = {
            "text":  text[:2000],
            "voice": agent.voice,
            "speed": agent.tts_speed,
        }
        if agent.tts_engine:
            tts_body["engine"] = agent.tts_engine

        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{GPU_INFER_URL}/tts", json=tts_body)
                r.raise_for_status()
                tts_data = r.json()
            result["audio_b64"]   = tts_data.get("audio_b64", "")
            result["mime_type"]   = "audio/wav"
            result["sample_rate"] = tts_data.get("sample_rate", 22050)
        except Exception as e:
            log.warning("AgentRunner TTS: %s", e)
            result["tts_error"] = str(e)

        return result


AGENT_RUNNER = AgentRunner()


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT AGENTS
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_AGENTS = [
    AgentRecord(
        name="assistant", label="Vera Assistant", avatar="🤖",
        description="General-purpose helpful assistant",
        model="", prefer_gpu=True, temperature=0.7,
        system_prompt=(
            "You are Vera, a helpful, knowledgeable AI assistant. "
            "Be concise, accurate, and friendly. "
            "When asked about the system's capabilities, explain them clearly."
        ),
        voice="af_heart",
    ),
    AgentRecord(
        name="dag-planner", label="DAG Planner", avatar="⚙️",
        description="Specialist in building Vera DAG workflow plans",
        model="", prefer_gpu=True, temperature=0.2, repeat_penalty=1.05,
        system_prompt=(
            "You are a Vera DAG planner. Your ONLY job is to produce correct DAG JSON.\n\n"
            "STRICT RULES:\n"
            "1. ONLY use capability names from the list provided. No invented names.\n"
            "2. Each node: [\"cap_name\", \"output_key\"]\n"
            "3. State matching: a cap param is filled from the state key of the SAME NAME.\n"
            "   Name output keys after the param they feed into the next step.\n"
            "   ALL required params (!) not produced by a prior node MUST be in initial_state.\n"
            "4. CONDITION (optional 3rd element): [\"cap\",\"out\",\"CONDITION:state_key\"]\n"
            "   Only skips the node if state[state_key] is falsy. NOT for passing arguments.\n"
            "5. initial_state MUST contain every required param (!) the first cap needs.\n"
            "6. Max 4 nodes. No redundant steps.\n\n"
            "CORRECT example — ping then summarise:\n"
            "{\"dag\":[[\"system.ping\",\"host_status\"],[\"llm.summarize\",\"summary\",\"CONDITION:host_status\"]],"
            "\"initial_state\":{\"host\":\"example.com\",\"text\":\"ping result will be here\"},"
            "\"rationale\":\"ping needs host; summarize needs text pre-seeded\"}\n\n"
            "CORRECT example — write a poem:\n"
            "{\"dag\":[[\"llm.generate\",\"poem\"]],"
            "\"initial_state\":{\"prompt\":\"Write a short poem about the sea\"},"
            "\"rationale\":\"llm.generate needs prompt in initial_state\"}\n\n"
            "Respond: brief prose explanation, then one ```json block."
        ),
        voice="bm_george",
    ),
    AgentRecord(
        name="dag-fixer", label="DAG Fixer", avatar="🔧",
        description="Diagnoses and repairs failed DAG nodes",
        model="", prefer_gpu=True, temperature=0.1,
        system_prompt=(
            "You are a Vera DAG debugger. A DAG node has failed. "
            "Given the error, the node's capability signature, and optionally its source code, "
            "diagnose the root cause and produce a corrected DAG plan. "
            "Be specific about which parameter was wrong or missing. "
            "Output corrected JSON in a ```json block."
        ),
        voice="bm_lewis",
    ),
    AgentRecord(
        name="scheduler", label="System Scheduler", avatar="📅",
        description="Expert in Vera DAG orchestration and task scheduling",
        model="", prefer_gpu=True, temperature=0.3, repeat_penalty=1.05,
        system_prompt=(
            "You are a Vera system scheduler and orchestration expert. "
            "You specialise in designing DAG workflows using Vera capabilities. "
            "When asked to plan a task, produce precise DAG JSON. "
            "Always verify that capability names are real and parameters are correct. "
            "Prefer short, focused DAGs (3-5 nodes). Never use invented capability names."
        ),
        domain_caps=["dag.store_save", "dag.store_run", "dag.run_monitored",
                     "obs.health", "system.ping", "system.timestamp", "http.get"],
        domain_description="DAG orchestration, task scheduling, system monitoring",
        tool_mode="plan",
        voice="bm_george",
    ),
    AgentRecord(
        name="code-reviewer", label="Code Reviewer", avatar="🔍",
        description="Strict code reviewer with focus on quality and security",
        model="", prefer_gpu=True, temperature=0.2, repeat_penalty=1.15,
        system_prompt=(
            "You are a rigorous code reviewer. Examine code for bugs, security vulnerabilities, "
            "performance issues, and style problems. Be direct and specific. "
            "Rate issues as critical/high/medium/low. Suggest concrete fixes."
        ),
        domain_caps=["llm.code_review", "llm.explain", "text.stats"],
        domain_description="Code analysis, security review, best practices",
        tool_mode="call",
        voice="bm_lewis",
    ),
    AgentRecord(
        name="creative", label="Creative Writer", avatar="✍️",
        description="Creative writer with vivid imagination",
        model="", prefer_gpu=True, temperature=1.2, top_p=0.95, repeat_penalty=1.0,
        system_prompt=(
            "You are a creative writer with a vivid imagination and distinctive voice. "
            "Write with sensory detail, varied sentence rhythm, and emotional depth. "
            "Avoid clichés. Embrace the unexpected."
        ),
        voice="af_bella",
    ),
    AgentRecord(
        name="analyst", label="Data Analyst", avatar="📊",
        description="Analytical thinker focused on data and reasoning",
        model="", prefer_gpu=True, temperature=0.1, top_p=0.8,
        system_prompt=(
            "You are a precise data analyst. Think step by step. "
            "Show your reasoning. Use concrete numbers when possible. "
            "Flag uncertainty explicitly. Prefer structured output."
        ),
        domain_caps=["math.compute", "math.stats", "data.json_validate",
                     "data.json_flatten", "llm.analyze", "llm.summarize"],
        domain_description="Data analysis, statistics, structured reasoning",
        tool_mode="call",
        voice="am_adam",
    ),
]


async def _seed_defaults():
    """Create default agents if they don't exist yet."""
    for agent in DEFAULT_AGENTS:
        existing = await AGENT_REGISTRY.get_by_name(agent.name)
        if not existing:
            await AGENT_REGISTRY.save(agent)
            log.info("Seeded default agent: %s", agent.name)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "agent.create", memory="off",
    http_method="POST", http_path="/agents/create", http_tags=["agents"],
    description="Create or update an agent. All model params configurable.",
)
async def agent_create(
    name:            str,
    label:           str   = "",
    description:     str   = "",
    avatar:          str   = "🤖",
    model:           str   = "",
    instance_id:     str   = "",
    prefer_gpu:      bool  = True,
    temperature:     float = 0.7,
    top_p:           float = 0.9,
    top_k:           int   = 40,
    repeat_penalty:  float = 1.1,
    repeat_last_n:   int   = 64,
    num_ctx:         int   = 4096,
    num_predict:     int   = -1,
    seed:            int   = -1,
    mirostat:        int   = 0,
    mirostat_tau:    float = 5.0,
    mirostat_eta:    float = 0.1,
    tfs_z:           float = 1.0,
    stop:            str   = "",  # comma-separated stop sequences
    system_prompt:   str   = "",
    greeting:        str   = "",
    voice:           str   = "af_heart",
    tts_speed:       float = 1.0,
    tts_engine:      str   = "",
    domain_caps:     str   = "",   # comma-separated
    domain_description: str = "",
    tool_mode:       str   = "none",
    think:           bool  = False,
    skill_ids:       str   = "",    # comma-separated skill IDs
    ontology_ids:    str   = "",    # comma-separated ontology IDs
    memory_enabled:      bool  = True,
    memory_inject:       bool  = False,
    memory_inject_limit: int   = 5,
    memory_tags:         str   = "",
    agent_id:        str   = "",
    trace_id=None,
):
    # Canonicalisation: if no explicit agent_id but the name already exists,
    # reuse that existing record's id. Without this, the agent panel's
    # "Save" path used to fork a new id every time a name was reused,
    # producing the duplicate explosion the user is fighting. Each named
    # agent now has exactly one id-row in the registry — the archive
    # dataset captures every save as a separate snapshot for history.
    canonical_id = agent_id
    if not canonical_id and name:
        existing = await AGENT_REGISTRY.get_by_name(name)
        if existing:
            canonical_id = existing.id

    rec = AgentRecord(
        id=canonical_id or str(uuid.uuid4()),
        name=name, label=label or name, description=description, avatar=avatar,
        model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        temperature=temperature, top_p=top_p, top_k=top_k,
        repeat_penalty=repeat_penalty, repeat_last_n=repeat_last_n,
        num_ctx=num_ctx, num_predict=num_predict, seed=seed, mirostat=mirostat,
        mirostat_tau=mirostat_tau, mirostat_eta=mirostat_eta, tfs_z=tfs_z,
        stop=[s.strip() for s in stop.split(",") if s.strip()] if stop else [],
        system_prompt=system_prompt, greeting=greeting,
        voice=voice, tts_speed=tts_speed, tts_engine=tts_engine,
        domain_caps=[c.strip() for c in domain_caps.split(",") if c.strip()],
        domain_description=domain_description, tool_mode=tool_mode,
        think=think,
        skill_ids=[s.strip() for s in skill_ids.split(",") if s.strip()],
        ontology_ids=[s.strip() for s in ontology_ids.split(",") if s.strip()],
        memory_enabled=memory_enabled,
        memory_inject=memory_inject,
        memory_inject_limit=memory_inject_limit,
        memory_tags=memory_tags,
    )
    saved = await AGENT_REGISTRY.save(rec)
    await emit_event({"type": "agent.created", "id": saved.id, "name": saved.name})
    return {"id": saved.id, "name": saved.name, "label": saved.label}



@capability(
    "agent.update", memory="off",
    http_method="POST", http_path="/agents/update", http_tags=["agents"],
    description="Update an existing agent. Requires agent_id or existing name.",
)
async def agent_update(
    name:            str,
    label:           str   = "",
    description:     str   = "",
    avatar:          str   = "🤖",
    model:           str   = "",
    instance_id:     str   = "",
    prefer_gpu:      bool  = True,
    temperature:     float = 0.7,
    top_p:           float = 0.9,
    top_k:           int   = 40,
    repeat_penalty:  float = 1.1,
    repeat_last_n:   int   = 64,
    num_ctx:         int   = 4096,
    num_predict:     int   = -1,
    seed:            int   = -1,
    mirostat:        int   = 0,
    mirostat_tau:    float = 5.0,
    mirostat_eta:    float = 0.1,
    tfs_z:           float = 1.0,
    stop:            str   = "",
    system_prompt:   str   = "",
    greeting:        str   = "",
    voice:           str   = "af_heart",
    tts_speed:       float = 1.0,
    tts_engine:      str   = "",
    domain_caps:     str   = "",
    domain_description: str = "",
    tool_mode:       str   = "",
    think:           bool  = False,
    skill_ids:       str   = "",
    ontology_ids:    str   = "",
    memory_enabled:      bool  = True,
    memory_inject:       bool  = False,
    memory_inject_limit: int   = 5,
    memory_tags:         str   = "",
    agent_id:        str   = "",
    trace_id=None,
):
    # Resolve existing record to preserve id and created_at
    existing = None
    if agent_id:   existing = await AGENT_REGISTRY.get(agent_id)
    if not existing: existing = await AGENT_REGISTRY.get_by_name(name)
    if not existing:
        return {"error": f"Agent not found: {agent_id or name}"}
    rec = AgentRecord(
        id=existing.id, created_at=existing.created_at,
        name=name, label=label or name, description=description, avatar=avatar,
        model=model, instance_id=instance_id, prefer_gpu=prefer_gpu,
        temperature=temperature, top_p=top_p, top_k=top_k,
        repeat_penalty=repeat_penalty, repeat_last_n=repeat_last_n,
        num_ctx=num_ctx, num_predict=num_predict, seed=seed, mirostat=mirostat,
        mirostat_tau=mirostat_tau, mirostat_eta=mirostat_eta, tfs_z=tfs_z,
        stop=[s.strip() for s in stop.split(",") if s.strip()] if stop else [],
        system_prompt=system_prompt, greeting=greeting,
        voice=voice, tts_speed=tts_speed, tts_engine=tts_engine,
        domain_caps=[c.strip() for c in domain_caps.split(",") if c.strip()],
        domain_description=domain_description, tool_mode=tool_mode,
        think=think,
        skill_ids=[s.strip() for s in skill_ids.split(",") if s.strip()],
        ontology_ids=[s.strip() for s in ontology_ids.split(",") if s.strip()],
        memory_enabled=memory_enabled,
        memory_inject=memory_inject,
        memory_inject_limit=memory_inject_limit,
        memory_tags=memory_tags,
        author=existing.author,
    )
    saved = await AGENT_REGISTRY.save(rec)
    await emit_event({"type": "agent.updated", "id": saved.id, "name": saved.name})
    return {"id": saved.id, "name": saved.name, "label": saved.label}

@capability(
    "agent.list", memory="off",
    http_method="GET", http_path="/agents/list", http_tags=["agents"],
    description="List all registered agents.",
)
async def agent_list(include_archived: bool = False, trace_id=None):
    agents = await AGENT_REGISTRY.list_all(include_archived=include_archived)
    return {"agents": [a.to_dict() for a in agents], "count": len(agents)}


@capability(
    "agent.get", memory="off",
    http_method="GET", http_path="/agents/get", http_tags=["agents"],
    description="Get an agent by id or name. force_refresh=true bypasses the "
                "in-process cache and reloads from Redis/PG/fabric — use this "
                "when the chat UI needs to verify the latest saved config.",
)
async def agent_get(id: str = "", name: str = "", force_refresh: bool = False, trace_id=None):
    if force_refresh:
        # Evict the cached entries so subsequent get() / get_by_name() goes to
        # Redis (and falls through to PG/fabric on miss). This is the user's
        # explicit "I just edited this in the agent panel — give me the truth"
        # path. Without it, the chat panel could read stale in-process state.
        if id and id in AgentRegistry._CACHE:
            cached = AgentRegistry._CACHE.pop(id, None)
            if cached: AgentRegistry._CACHE.pop(f"name:{cached.name}", None)
        if name:
            AgentRegistry._CACHE.pop(f"name:{name}", None)
            # Also evict any id-keyed cache that has this name
            for k in list(AgentRegistry._CACHE.keys()):
                v = AgentRegistry._CACHE.get(k)
                if hasattr(v, "name") and v.name == name and k != f"name:{name}":
                    AgentRegistry._CACHE.pop(k, None)

    rec = None
    if id:   rec = await AGENT_REGISTRY.get(id)
    if not rec and name: rec = await AGENT_REGISTRY.get_by_name(name)
    if not rec: return {"error": f"Agent not found: {id or name}"}
    return rec.to_dict()


@capability(
    "agent.delete", memory="off",
    http_method="POST", http_path="/agents/delete", http_tags=["agents"],
    description="Soft-delete an agent (archived=True).",
)
async def agent_delete(id: str, trace_id=None):
    ok = await AGENT_REGISTRY.delete(id)
    return {"deleted": ok, "id": id}


@capability(
    "agent.chat", memory="on",
    http_method="POST", http_path="/agents/chat", http_tags=["agents"],
    description="Send a message to an agent. Returns text response.",
)
async def agent_chat(
    message:       str,
    agent_name:    str   = "assistant",
    agent_id:      str   = "",
    history:       str   = "[]",
    session_id:    str   = "",
    model_override:str   = "",
    instance_id:   str   = "",
    prefer_gpu:    bool  = False,
    think:         bool  = False,
    trace_id=None,
):
    agent = None
    if agent_id:   agent = await AGENT_REGISTRY.get(agent_id)
    if not agent:  agent = await AGENT_REGISTRY.get_by_name(agent_name)
    if not agent:
        text = await ollama_generate(message, prefer_gpu=prefer_gpu or True)
        return {"text": text, "agent_name": "default"}

    # Apply per-call overrides without mutating the stored agent
    import copy
    agent = copy.copy(agent)
    if model_override: agent.model = model_override
    if instance_id:    agent.instance_id = instance_id
    if prefer_gpu:     agent.prefer_gpu = True
    if think: agent.think = True

    try:    hist = json.loads(history)
    except: hist = []

    return await AGENT_RUNNER.run(agent, message, hist, session_id)


@capability(
    "agent.chat_voice", memory="on",
    http_method="POST", http_path="/agents/chat_voice", http_tags=["agents"],
    description="Send a message to an agent. Returns text + TTS audio (base64 WAV).",
)
async def agent_chat_voice(
    message:       str,
    agent_name:    str   = "assistant",
    agent_id:      str   = "",
    history:       str   = "[]",
    session_id:    str   = "",
    model_override:str   = "",
    instance_id:   str   = "",
    prefer_gpu:    bool  = False,
    think:         bool  = False,
    trace_id=None,
):
    agent = None
    if agent_id:   agent = await AGENT_REGISTRY.get(agent_id)
    if not agent:  agent = await AGENT_REGISTRY.get_by_name(agent_name)
    if not agent:
        text = await ollama_generate(message, prefer_gpu=prefer_gpu or True)
        return {"text": text, "agent_name": "default"}

    import copy
    agent = copy.copy(agent)
    if model_override: agent.model = model_override
    if instance_id:    agent.instance_id = instance_id
    if prefer_gpu:     agent.prefer_gpu = True
    if think: agent.think = True

    try:    hist = json.loads(history)
    except: hist = []

    return await AGENT_RUNNER.run_with_tts(agent, message, hist, session_id)


@capability(
    "agent.models", memory="off",
    http_method="GET", http_path="/agents/models", http_tags=["agents"],
    description="List all models available across all Ollama instances.",
)
async def agent_models(trace_id=None):
    all_models = {}
    for iid, inst in OLLAMA_INSTANCES.items():
        if inst.get("status") != "online":
            continue
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{inst['url']}/api/tags")
                r.raise_for_status()
                models = [m["name"] for m in r.json().get("models", [])]
                all_models[iid] = {"models": models, "gpu": inst.get("has_gpu", False),
                                   "label": inst.get("label", iid)}
        except Exception as e:
            all_models[iid] = {"error": str(e)}
    return {"instances": all_models}


# ─────────────────────────────────────────────────────────────────────────────
# UI PANELS
# ─────────────────────────────────────────────────────────────────────────────

_ASO_MOUNT_JS = r"""
(function mountASOPanel() {
  const mount = document.getElementById('panel-aso');
  if (!mount || mount._asoMounted) return;
  mount._asoMounted = true;

  const frame = document.createElement('iframe');
  // Derive correct backend URL — same logic as _veraBase getter
  const backendBase = (document.getElementById('backendUrl')?.value || '').replace(/\/$/, '')
                   || window._veraBase || 'http://llm.int:8999';
  frame.src = backendBase + '/ui/panels/agents-skills-ontologies';
  frame.style.cssText = 'width:100%;height:100%;border:none;display:block;background:#181614';
  frame.allow = 'clipboard-read; clipboard-write; microphone';
  mount.appendChild(frame);

  // Relay base URL changes to the iframe
  const urlInput = document.getElementById('backendUrl');
  if (urlInput) {
    urlInput.addEventListener('change', () => {
      try { frame.contentWindow.postMessage({ type:'vera:base', url: urlInput.value.replace(/\/$/, '') }, '*'); } catch(_) {}
    });
  }
})();
"""

register_ui(
    "agents-skills-ontologies",
    "Agents / Skills / Ontologies",
    "",
    """
    <div id="panel-aso" style="height:100%;overflow:hidden;background:#181614">
        <iframe src="/ui/panels/agents-skills-ontologies"
                style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
                allow="clipboard-read; clipboard-write">
        </iframe>
    </div>
    """,
    _ASO_MOUNT_JS,
    ui_caps=[
        'agent.create', 'agent.list', 'agent.get', 'agent.delete',
        'agent.chat', 'agent.chat_voice', 'agent.models',
        'agent.call_with_tools',
        'skill.create', 'skill.list', 'skill.update', 'skill.delete',
        'skill.apply', 'skill.compose', 'skill.active_context',
        'ontology.create', 'ontology.list', 'ontology.update', 'ontology.delete',
        'ontology.apply', 'ontology.infer',
        'memory.search', 'memory.session', 'memory.clear',
    ],
    mode="tab",
    tab_order=5,
)

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _migrate_fabric_to_deterministic() -> Dict[str, int]:
    """Consolidate legacy fabric rows for agents.

    Old behaviour: every save produced a new fabric row with a random uuid.
    New behaviour: each agent has exactly one row id `agent-{rec.id}`.

    Migration strategy (SAFE — no destructive deletes):
      1. Group existing rows by source_id (= agent.id) when present, or
         by data.id, or by data.name.
      2. For each group, find the newest row (by data.updated_at).
      3. Re-save it via _save_to_fabric — that creates the new
         deterministic-id row (and writes an archive entry).
      4. Mark old random-uuid rows as superseded by tagging them with
         `_legacy` rather than deleting. A separate manual sweep can
         clear them once the user confirms everything still works.

    Returns {scanned, consolidated, skipped, errors}.
    """
    fabric = sys.modules.get("data_fabric")
    if not fabric:
        return {"scanned": 0, "consolidated": 0, "skipped": 0, "errors": 0}
    try:
        rows = await fabric.query_dataset(
            dataset_id="agents",
            query={"limit": 5000, "include_data": True},
        )
    except Exception:
        return {"scanned": 0, "consolidated": 0, "skipped": 0, "errors": 0}
    rows = rows or []
    if not rows:
        return {"scanned": 0, "consolidated": 0, "skipped": 0, "errors": 0}

    groups: Dict[str, list] = {}
    for r in rows:
        d = r.get("data") or {}
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: d = {}
        agent_id = (d.get("id") if isinstance(d, dict) else None) or r.get("source_id")
        if not agent_id:
            continue
        groups.setdefault(agent_id, []).append((r, d))

    consolidated, skipped, errors = 0, 0, 0
    for agent_id, lst in groups.items():
        # If exactly one row AND its id is already the deterministic form,
        # nothing to do.
        if len(lst) == 1 and lst[0][0].get("id") == f"agent-{agent_id}":
            skipped += 1
            continue
        # Find the newest by data.updated_at
        def _ts(item):
            d = item[1] if isinstance(item[1], dict) else {}
            return d.get("updated_at") or item[0].get("created_at") or ""
        lst.sort(key=_ts, reverse=True)
        newest_data = lst[0][1] if isinstance(lst[0][1], dict) else None
        if not newest_data or not newest_data.get("name"):
            errors += 1
            continue
        # Reconstruct AgentRecord from newest and save it via the regular path
        try:
            d = dict(newest_data)
            for field in ("domain_caps", "stop", "skill_ids", "ontology_ids"):
                if field in d and isinstance(d[field], list):
                    continue
                legacy = d.get(f"{field}_json")
                if legacy:
                    try: d[field] = json.loads(legacy)
                    except Exception: d[field] = []
                elif isinstance(d.get(field), str):
                    try: d[field] = json.loads(d[field])
                    except Exception: d[field] = []
                else:
                    d.setdefault(field, [])
            for fld, typ in [("temperature",float),("top_p",float),("top_k",int),
                             ("repeat_penalty",float),("repeat_last_n",int),
                             ("num_ctx",int),("num_predict",int),("seed",int),
                             ("mirostat",int),("mirostat_tau",float),
                             ("mirostat_eta",float),("tfs_z",float),
                             ("tts_speed",float),("memory_inject_limit",int)]:
                if fld in d:
                    try: d[fld] = typ(d[fld])
                    except Exception: pass
            for fld in ("prefer_gpu","think","memory_enabled","memory_inject","archived"):
                if fld in d:
                    v = d[fld]
                    if not isinstance(v, bool):
                        d[fld] = str(v).lower() in ("true","1","yes")
            rec = AgentRecord(**{k: v for k, v in d.items()
                                 if k in AgentRecord.__dataclass_fields__})
            # Re-save: writes new deterministic-id row and an archive entry
            await AGENT_REGISTRY._save_to_fabric(rec)
            consolidated += 1
        except Exception as e:
            log.debug("migrate row %s: %s", agent_id, e)
            errors += 1

    if consolidated:
        log.info("agents: migrated %d agents to deterministic fabric ids "
                 "(scanned %d rows, %d skipped, %d errors)",
                 consolidated, len(rows), skipped, errors)
    return {
        "scanned":      len(rows),
        "consolidated": consolidated,
        "skipped":      skipped,
        "errors":       errors,
    }


@capability(
    "agent.history", memory="off",
    http_method="GET", http_path="/agents/history", http_tags=["agents"],
    description="Read the change history for one agent. Returns up to `limit` "
                "snapshots from the agents_archive dataset, newest first. "
                "Each save of the agent appends one snapshot; this is the "
                "audit trail / undo history.",
)
async def agent_history(id: str = "", name: str = "", limit: int = 50, trace_id=None):
    agent_id = id
    if not agent_id and name:
        rec = await AGENT_REGISTRY.get_by_name(name)
        if rec: agent_id = rec.id
    if not agent_id:
        return {"error": "Provide id or name"}
    history = await AgentRegistry._load_history_from_fabric(agent_id, limit=limit)
    return {"agent_id": agent_id, "history": history, "count": len(history)}


@capability(
    "agent.restore_version", memory="off",
    http_method="POST", http_path="/agents/restore_version", http_tags=["agents"],
    description="Restore an agent to a previous version from its history. "
                "Looks up the archive snapshot by `archive_id` and saves it as "
                "the current config. Use agent.history first to find archive_ids.",
)
async def agent_restore_version(archive_id: str, trace_id=None):
    fabric = sys.modules.get("data_fabric")
    if not fabric:
        return {"error": "fabric module not loaded"}
    try:
        results = await fabric.query_dataset(
            dataset_id="agents_archive",
            query={"limit": 5, "include_data": True, "filter": {"id": archive_id}},
        )
    except Exception as e:
        return {"error": str(e)}
    # Filter may not be supported — fallback: scan
    target = None
    for r in (results or []):
        if r.get("id") == archive_id:
            target = r; break
    if not target:
        # broad scan
        try:
            results = await fabric.query_dataset(
                dataset_id="agents_archive",
                query={"limit": 5000, "include_data": True},
            )
            for r in (results or []):
                if r.get("id") == archive_id:
                    target = r; break
        except Exception:
            pass
    if not target:
        return {"error": f"archive id not found: {archive_id}"}
    data = target.get("data") or {}
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception: data = {}
    if not data.get("id") or not data.get("name"):
        return {"error": "archive row has no agent id/name"}
    # Reconstruct & save
    for field in ("domain_caps", "stop", "skill_ids", "ontology_ids"):
        if isinstance(data.get(field), str):
            try: data[field] = json.loads(data[field])
            except Exception: data[field] = []
        elif not isinstance(data.get(field), list):
            data[field] = []
    rec = AgentRecord(**{k: v for k, v in data.items()
                         if k in AgentRecord.__dataclass_fields__})
    saved = await AGENT_REGISTRY.save(rec)
    return {"restored": saved.id, "name": saved.name, "from_archive": archive_id}


@capability(
    "agent.list_fabric", memory="off",
    http_method="GET", http_path="/agents/list_fabric", http_tags=["agents"],
    description="List ALL records in the fabric agents dataset, including duplicates. "
                "Returns one entry per fabric row (record_id, agent_id, name, model, "
                "updated_at, system_prompt_preview). Use this to pick which records to "
                "restore from history before calling agent.restore_from_fabric.",
)
async def agent_list_fabric(trace_id=None):
    fabric = sys.modules.get("data_fabric")
    if not fabric:
        return {"error": "fabric module not loaded", "rows": []}
    try:
        results = await fabric.query_dataset(
            dataset_id="agents",
            query={"limit": 5000, "include_data": True},
        )
    except Exception as e:
        return {"error": str(e), "rows": []}
    rows = []
    for r in (results or []):
        d = r.get("data") or {}
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: d = {}
        if not isinstance(d, dict): continue
        sp = (d.get("system_prompt") or "")
        rows.append({
            "record_id":  r.get("id"),
            "agent_id":   d.get("id") or r.get("source_id"),
            "name":       d.get("name") or "",
            "label":      d.get("label") or "",
            "model":      d.get("model") or "",
            "updated_at": d.get("updated_at") or r.get("created_at") or "",
            "domain_caps_count": len(d.get("domain_caps") or []) if isinstance(d.get("domain_caps"), list) else 0,
            "system_prompt_preview": sp[:160] + ("…" if len(sp) > 160 else ""),
        })
    # Sort newest-first by updated_at — easier to scan for the user.
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return {"rows": rows, "count": len(rows)}


@capability(
    "agent.restore_from_fabric", memory="off",
    http_method="POST", http_path="/agents/restore_from_fabric", http_tags=["agents"],
    description="Reload agents from the data fabric into the registry caches. "
                "Without arguments: restores the newest record per agent name "
                "(safe, non-destructive). With record_ids: restores ONLY those "
                "specific fabric rows. With names: restores ONLY agents whose "
                "name matches. Always treats fabric as source of truth.",
)
async def agent_restore_from_fabric(
    record_ids: str = "",
    names: str = "",
    consolidate: bool = False,
    rebuild_redis: bool = True,
    trace_id=None,
):
    """Restore agents from fabric.

    Selection strategy:
      • record_ids="rec1,rec2"  → restore exactly those fabric rows (one save each)
      • names="alice,bob"        → restore newest-by-updated_at for each named agent
      • neither                  → restore newest-per-name across the whole dataset

    rebuild_redis=True (default) clears Redis hashes for any agent we're about to
    restore, then re-writes them fresh from fabric. This is what makes "Restore
    from Fabric" actually replace stale Redis data rather than just appending
    on top of it.
    """
    fabric = sys.modules.get("data_fabric")
    if not fabric:
        return {"error": "fabric module not loaded"}

    migration = {"scanned": 0, "consolidated": 0, "skipped": 0, "errors": 0}
    if consolidate:
        try:
            migration = await _migrate_fabric_to_deterministic()
        except Exception as e:
            log.warning("migrate in restore: %s", e)

    # Load all fabric rows so we can apply selection consistently
    try:
        all_rows = await fabric.query_dataset(
            dataset_id="agents",
            query={"limit": 5000, "include_data": True},
        ) or []
    except Exception as e:
        return {"error": f"fabric query failed: {e}"}

    # Parse all rows into AgentRecord candidates with their fabric record_id
    parsed: List[tuple] = []  # [(record_id, AgentRecord)]
    for r in all_rows:
        d = r.get("data") or {}
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: continue
        if not isinstance(d, dict): continue
        if not d.get("id") or not d.get("name"): continue
        try:
            for field in ("domain_caps","stop","skill_ids","ontology_ids"):
                if isinstance(d.get(field), str):
                    try: d[field] = json.loads(d[field])
                    except Exception: d[field] = []
                elif not isinstance(d.get(field), list):
                    d[field] = []
            for fld, typ in [("temperature",float),("top_p",float),("top_k",int),
                             ("repeat_penalty",float),("repeat_last_n",int),
                             ("num_ctx",int),("num_predict",int),("seed",int),
                             ("mirostat",int),("mirostat_tau",float),
                             ("mirostat_eta",float),("tfs_z",float),
                             ("tts_speed",float),("memory_inject_limit",int)]:
                if fld in d:
                    try: d[fld] = typ(d[fld])
                    except Exception: pass
            for fld in ("prefer_gpu","think","memory_enabled","memory_inject","archived"):
                v = d.get(fld)
                if v is not None and not isinstance(v, bool):
                    d[fld] = str(v).lower() in ("true","1","yes")
            cand = AgentRecord(**{k: v for k, v in d.items()
                                  if k in AgentRecord.__dataclass_fields__})
            parsed.append((r.get("id"), cand))
        except Exception as e:
            log.debug("restore parse: %s", e)

    # Apply selection
    selected_recs: list = []
    if record_ids:
        wanted = {x.strip() for x in record_ids.split(",") if x.strip()}
        for rid, rec in parsed:
            if rid in wanted:
                selected_recs.append(rec)
    elif names:
        wanted_names = {x.strip() for x in names.split(",") if x.strip()}
        # Newest per requested name
        by_name: Dict[str, AgentRecord] = {}
        for rid, rec in parsed:
            if rec.name not in wanted_names: continue
            existing = by_name.get(rec.name)
            if existing is None or (rec.updated_at or "") > (existing.updated_at or ""):
                by_name[rec.name] = rec
        selected_recs = list(by_name.values())
    else:
        # Newest per name across the whole dataset
        by_name: Dict[str, AgentRecord] = {}
        for rid, rec in parsed:
            existing = by_name.get(rec.name)
            if existing is None or (rec.updated_at or "") > (existing.updated_at or ""):
                by_name[rec.name] = rec
        selected_recs = list(by_name.values())

    # Purge stale Redis hashes whose names match anything we're about to restore.
    # This is the step that makes "Restore" actually take effect — without it,
    # the cache could keep returning a stale id-keyed Redis hash for the same name.
    if rebuild_redis:
        r0 = _redis()
        names_to_clear = {rec.name for rec in selected_recs}
        # Find every Redis hash whose stored name matches; delete them so save()
        # below writes the fabric copy as the only authority.
        if r0:
            try:
                keys = await r0.keys(f"{AgentRegistry._PREFIX}*")
                for k in keys:
                    raw = await r0.hgetall(k)
                    if not raw: continue
                    nm_raw = raw.get(b"name") if isinstance(list(raw.keys())[0], bytes) else raw.get("name")
                    nm = nm_raw.decode() if isinstance(nm_raw, bytes) else (nm_raw or "")
                    if nm in names_to_clear:
                        await r0.delete(k)
            except Exception as e:
                log.warning("restore Redis purge: %s", e)
        # Also clear in-process cache so subsequent get_by_name reads fabric → save flow
        for rec in selected_recs:
            AgentRegistry._CACHE.pop(f"name:{rec.name}", None)
            AgentRegistry._CACHE.pop(rec.id, None)

    # Save each selected record (this writes Redis + PG + fabric primary again,
    # plus an archive snapshot — the archive is fine, that's append-only by design).
    restored, restored_names = 0, []
    for rec in selected_recs:
        try:
            await AGENT_REGISTRY.save(rec)
            restored += 1
            restored_names.append(rec.name)
        except Exception as e:
            log.warning("restore save %s: %s", rec.name, e)

    return {
        "restored": restored,
        "names":    restored_names,
        "fabric_rows_scanned": len(parsed),
        "selected": len(selected_recs),
        "migration": migration,
    }


@capability(
    "agent.purge_fabric_duplicates", memory="off",
    http_method="POST", http_path="/agents/purge_fabric_duplicates",
    http_tags=["agents"],
    description="DESTRUCTIVE: delete duplicate fabric rows for agents, keeping only "
                "the newest per name. Use ONLY after confirming agent.list_fabric "
                "shows the duplicates you intend to remove. dry_run=true returns "
                "the deletion plan without acting.",
)
async def agent_purge_fabric_duplicates(dry_run: bool = True, trace_id=None):
    """Delete duplicate fabric rows. Keeps the newest record per agent name."""
    fabric = sys.modules.get("data_fabric")
    if not fabric:
        return {"error": "fabric module not loaded"}
    try:
        rows = await fabric.query_dataset(
            dataset_id="agents",
            query={"limit": 5000, "include_data": True},
        ) or []
    except Exception as e:
        return {"error": str(e)}
    # Group by name; keep the newest by updated_at, mark others for deletion.
    groups: Dict[str, list] = {}
    for r in rows:
        d = r.get("data") or {}
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: d = {}
        if not isinstance(d, dict): continue
        nm = d.get("name") or ""
        if not nm: continue
        groups.setdefault(nm, []).append((r, d))

    to_delete = []  # list of fabric record_ids
    keep_summary = []
    for nm, lst in groups.items():
        if len(lst) <= 1:
            continue
        lst.sort(key=lambda x: (x[1].get("updated_at") or x[0].get("created_at") or ""),
                 reverse=True)
        keep_rec, keep_data = lst[0]
        keep_summary.append({
            "name": nm, "kept_record_id": keep_rec.get("id"),
            "kept_updated_at": keep_data.get("updated_at",""),
            "duplicates_removed": len(lst) - 1,
        })
        for r, _d in lst[1:]:
            rid = r.get("id")
            if rid:
                to_delete.append(rid)

    if dry_run:
        return {"dry_run": True, "would_delete": len(to_delete),
                "summary": keep_summary, "delete_ids": to_delete[:50]}

    # Execute deletions. Best-effort; SQLite + PG + Chroma each get a try.
    deleted = 0
    delete_record = getattr(fabric, "cap_fabric_delete_record", None)
    for rid in to_delete:
        try:
            if delete_record:
                await delete_record(record_id=rid, dataset_id="agents")
                deleted += 1
        except Exception as e:
            log.debug("purge delete %s: %s", rid, e)
    log.info("agents: purged %d duplicate fabric rows from %d groups",
             deleted, len(keep_summary))
    return {"dry_run": False, "deleted": deleted, "summary": keep_summary}


async def _startup():
    await AGENT_REGISTRY.pg_init()
    # Load agents from fabric (durable source of truth). Fabric → cache.
    # NO destructive operations on startup — the user reported agents were
    # being deleted by an over-eager dedupe pass. Migration to the new
    # deterministic-id schema is opt-in via /agents/restore_from_fabric
    # with consolidate=true.
    fabric_agents = await AgentRegistry._load_from_fabric()
    for a in fabric_agents:
        AgentRegistry._CACHE[a.id] = a
        AgentRegistry._CACHE[f"name:{a.name}"] = a
        # Warm Redis from fabric records — never deletes anything, just
        # ensures the cache reflects fabric on every restart.
        asyncio.ensure_future(AGENT_REGISTRY.save(a))
    if fabric_agents:
        log.info("agents: loaded %d agents from fabric", len(fabric_agents))
    await _seed_defaults()
    log.info("vera_agents ready — %d default agents seeded", len(DEFAULT_AGENTS))


schedule(_startup, interval=999999, name="agents_startup")
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass

"""
vera_chat_panel_registration.py
================================
Add this to agents.py (or a companion module loaded at startup).

It does three things:
  1. Serves vera_chat_panel.html at GET /chat_panel
  2. Registers the panel as a tab in the harness via register_ui()
  3. Exposes a /chat/name_session capability so the panel can call
     /llm/generate for auto-naming (that endpoint lives in capabilities.py,
     so just ensure capabilities.py is imported before agents.py in your
     module loader — which it already is).

DROP-IN: paste everything below into the bottom of agents.py,
just before the STARTUP section.  The register_ui() call can go
right alongside the existing "agents-editor" register_ui() call.
"""

import os as _os
from pathlib import Path as _Path
from fastapi.responses import HTMLResponse as _HTML, FileResponse as _FileResp

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Serve the chat panel HTML
# ─────────────────────────────────────────────────────────────────────────────

# The panel HTML can sit next to this file, or anywhere on your Python path.
# Adjust this path to wherever you save vera_chat_panel.html.
_PANEL_HTML_PATH = _Path(__file__).parent.parent / "chat/chat_panel.html"

# Fallback: try the project root
if not _PANEL_HTML_PATH.exists():
    _PANEL_HTML_PATH = _Path(__file__).parent.parent / "vera_chat_panel.html"


@APP.get("/chat_panel", response_class=_HTML)
async def serve_chat_panel():
    """Serve the self-contained Vera Chat Panel HTML."""
    if _PANEL_HTML_PATH.exists():
        return _HTML(content=_PANEL_HTML_PATH.read_text(encoding="utf-8"))
    return _HTML(content="<h2 style='font-family:monospace;color:#c96b6b'>vera_chat_panel.html not found</h2>")


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Register as a harness tab
# ─────────────────────────────────────────────────────────────────────────────
#
# mode="tab" → the harness automatically creates a top-level tab for this panel
# and injects an iframe pointing at /chat_panel when the tab is activated.
#
# The inline JS snippet below is what the harness runs when the tab first
# activates.  It creates an iframe pointed at /chat_panel and appends it.
#
# NOTE: the harness needs to expose window._veraBase so the panel can inherit
# the correct BASE URL.  Add this to your harness init if not already there:
#
#   window._veraBase = document.getElementById('urlInput').value || 'http://localhost:8000';
#

_CHAT_PANEL_INJECT_JS = r"""
(function mountChatPanel(){
  const mount = document.getElementById('panel-chat2');
  if (!mount) return;
  if (mount._chatMounted) return;
  mount._chatMounted = true;

  // Expose BASE to child iframe
  window._veraBase = (document.getElementById('urlInput')?.value || localStorage.getItem('vera_base') || 'http://localhost:8000').replace(/\/$/, '');

  const frame = document.createElement('iframe');
  frame.src = window._veraBase + '/chat_panel';
  frame.style.cssText = 'width:100%;height:100%;border:none;display:block;';
  frame.allow = 'microphone';   // needed for STT
  mount.appendChild(frame);

  // Bridge: if parent changes BASE, update the child
  const urlInput = document.getElementById('urlInput');
  if (urlInput) {
    urlInput.addEventListener('change', () => {
      window._veraBase = urlInput.value.replace(/\/$/, '');
      if (frame.contentWindow?.CH) {
        frame.contentWindow.CH.setBase(window._veraBase);
      }
    });
  }
})();
"""

register_ui(
    "chat2",                         # panel id
    "Chat",                          # tab label
    "",                     # icon (speech bubble, works as HTML entity)
    # The panel HTML is just a mount point — the iframe is injected by the JS
    '<div id="panel-chat2" style="height:100%;overflow:hidden;background:var(--bg0)"></div>',
    _CHAT_PANEL_INJECT_JS,
    ui_caps=[
        "agent.list", "agent.chat", "agent.chat_voice", "agent.models",
        "agent.create", "agent.get",
        "memory.query", "memory.agent_context", "memory.record_turn",
        "memory.store", "memory.search",
        "fabric.query", "fabric.datasets",
        "dag.plan", "llm.generate",
        "obs.health", "cluster.nodes",
        "gpu.stt", "gpu.tts",
    ],
    mode="tab",
    tab_order=25,   # appears near the left of the tab bar
)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Integration notes
# ─────────────────────────────────────────────────────────────────────────────
#
# AGENT MODEL BUG FIX
# ───────────────────
# The old chat UI sent model_override="" when the user hadn't touched the
# dropdown.  The backend then fell back to OLLAMA_MODEL (system default)
# instead of the agent's own model field.
#
# The new panel calls effectiveModel() which does:
#
#   const manual = cfgModel.value;          // "" if user hasn't picked one
#   if (manual) return manual;              // explicit override wins
#   const a = AGENTS.find(x=>x.name===name);
#   return a?.model || '';                  // agent's own model, or ''
#
# Then sends:  model_override: effectiveModel()
#
# The backend in agent_chat_stream_endpoint already does:
#   if body.get("model_override"): agent.model = body["model_override"]
#
# So if effectiveModel() returns the agent's model string (e.g. "qwen3:32b"),
# the agent runs with that model.
# If it returns "" (agent has no model set), the backend uses OLLAMA_MODEL. ✓
#
#
# EMBEDDING IN OTHER PANELS
# ─────────────────────────
# To embed a minimal chat in another panel (e.g. the IDE or Fabric tab),
# call VeraChatEmbed.mount() from the harness JS:
#
#   // In the IDE panel's onload JS:
#   const chatMount = document.createElement('div');
#   chatMount.style.cssText = 'height:300px;border-top:1px solid var(--border)';
#   document.getElementById('panel-ide').appendChild(chatMount);
#
#   const frame = document.createElement('iframe');
#   frame.src = window._veraBase + '/chat_panel#embedded';
#   frame.style.cssText = 'width:100%;height:100%;border:none';
#   frame.allow = 'microphone';
#   frame.onload = () => {
#     const CP = frame.contentWindow?.CH;
#     if (!CP) return;
#     CP.init({
#       agentFixed: 'code-reviewer',
#       showHistory: false,
#       showCtxGraph: true,
#       baseUrl: window._veraBase,
#     });
#     // Push editor content as context
#     CP.setContext(IDE.getActiveFileContent());
#   };
#   chatMount.appendChild(frame);
#
#
# FABRIC INTEGRATION
# ──────────────────
# The context graph queries /fabric/query with a JSON DSL:
#   { "vector": "<query text>", "top_k": 5, "dataset_id": "<optional>" }
#
# Results appear as blue (#38bdf8) nodes in the graph, with dataset labels.
# The Fabric tab in the right rail shows the fabric results as a list.
# The Fabric source chip in Context config toggles fabric results on/off.
# The Fabric dataset dropdown filters results to a specific dataset.
#
# FRAMES
# ──────
# Context frames are saved automatically at each turn, and manually via "+ Frame".
# Each frame records the exact CTX_NODES + CTX_EDGES arrays.
# Loading a frame restores the graph to that historical state.
# The frame strip at the bottom of the right rail shows the 5 most recent frames.
# The Frames tab shows all frames with timestamps and node counts.
#
# SESSION NAMING
# ──────────────
# After the 2nd user message, the panel calls /llm/generate with a short
# "name this conversation" prompt and stores the result in memory with tag
# ["session","name"] and metadata.session_name = <name>.
# The history list reads this on load and displays the name.
# Users can also manually rename via the session name bar (click to rename).