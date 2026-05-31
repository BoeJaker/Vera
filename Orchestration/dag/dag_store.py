"""
vera_dag_store.py — DAG Store, Capability Relevance & Execution Monitor
=======================================================================

Three systems in one module:

1. CapabilityIndex
   ─────────────
   Enriches every registered capability at startup with:
     • category       — inferred from name prefix (llm, obs, memory, gpu, …)
     • tags           — from decorator + auto-generated from description
     • embedding      — Ollama vector of "name: description" (async, lazy)
     • doc            — full docstring / long description
     • examples       — usage examples (if provided via @capability kwarg)
   
   Exposes relevance_search(query, top_k) that scores every cap by:
     • BM25-style keyword overlap with description + tags
     • cosine similarity against query embedding (if available)
     • category boost when category word appears in query
   
   Used by plan_dag to pass only the most relevant caps to the LLM
   instead of the full catalogue.

2. DagStore
   ────────
   Persists named DAG definitions with rich metadata:
     • id, name, description, tags, category
     • dag (the JSON array), initial_state
     • created_at, updated_at, author
     • embedding  — vector of "name: description + tag string"
     • use_count, last_used, avg_runtime_ms
     • nodes_summary — extracted capability names + output keys
   
   Storage: Redis hash vera:dags:{id} (always), Postgres vera_dags (if available)
   Search:  keyword + vector + tag filter, same interface as MemoryBackend
   
   Provides @capability endpoints:
     dag.store_save   — save a DAG definition
     dag.store_list   — list with filters
     dag.store_get    — fetch by id or name
     dag.store_search — semantic + keyword search
     dag.store_delete — soft-delete
     dag.store_run    — load a stored DAG and execute it

3. ExecutionMonitor
   ─────────────────
   Wraps run_graph to:
     • Watch Redis event stream for cap.error events during execution
     • Collect errors per node in a per-trace_id error log
     • After execution, run an LLM error-correction pass if any node failed
       with {"error":…} in its output
     • Re-run failed nodes with corrected inputs, up to max_retries
     • Emit structured execution report: nodes run, errors, corrections made

Usage
─────
    import vera_dag_store      # side-effect: registers all capabilities
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import (
    APP,                   # noqa
    CAPABILITY_REGISTRY,
    capability, emit_event, now_iso, ollama_generate, schedule,
)

log = logging.getLogger("vera.dag_store")

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_EMBED_URL   = cfg.OLLAMA_EMBED_URL
OLLAMA_EMBED_MODEL = cfg.OLLAMA_EMBED_MODEL
MAX_CAPS_IN_PROMPT = int(os.getenv("MAX_CAPS_IN_PROMPT", "25"))
EMBED_CAPS_ON_START= os.getenv("EMBED_CAPS_ON_START", "1") == "1"

def _redis(): return _orch.REDIS
def _pg():    return _orch.PG_POOL


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY MAP  —  prefix → human category + semantic cluster
# ─────────────────────────────────────────────────────────────────────────────
CAP_CATEGORIES: Dict[str, str] = {
    "llm":       "language_model",
    "stt":       "speech_to_text",
    "tts":       "text_to_speech",
    "image":     "image_generation",
    "sd":        "image_generation",
    "gpu":       "gpu_inference",
    "memory":    "memory_storage",
    "obs":       "observability",
    "dag":       "dag_orchestration",
    "mcp":       "mcp_protocol",
    "ollama":    "ollama_management",
    "skills":    "skills_management",
    "ontologies":"ontologies_management",
    "text":      "text_processing",
    "data":      "data_processing",
    "math":      "computation",
    "http":      "http_client",
    "system":    "system_utilities",
    "pipeline":  "pipeline",
    "echo":      "debugging",
    "health":    "observability",
}

# Additional auto-tags derived from description keywords
_DESC_TAG_PATTERNS = [
    (r'\bsearch\b|\bsemanit\w+',       "search"),
    (r'\bvector\b|\bembedding',        "vector"),
    (r'\bgraph\b|\bneo4j',             "graph"),
    (r'\bsql\b|\bpostgres\b|\bquery\b',"database"),
    (r'\bstream\b|\bwebsocket',        "streaming"),
    (r'\bredis\b',                     "redis"),
    (r'\bjson\b',                      "json"),
    (r'\bfile\b|\bupload\b',           "file"),
    (r'\bimage\b|\bpng\b|\bjpeg',      "image"),
    (r'\baudio\b|\bwav\b|\bpcm',       "audio"),
    (r'\bsumm\w+\b',                   "summarization"),
    (r'\bclassif\w+',                  "classification"),
    (r'\btranslat\w+',                 "translation"),
    (r'\bgenerat\w+',                  "generation"),
    (r'\bplan\w*\b',                   "planning"),
    (r'\bping\b|\bhealth\b|\bstatus',  "monitoring"),
    (r'\burl\b|\bhttp\b|\bweb\b',      "http"),
]


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY INDEX
# ─────────────────────────────────────────────────────────────────────────────

class CapabilityIndex:
    """
    Enriched index of all registered capabilities.
    Built once at startup and refreshed when CAPABILITY_REGISTRY changes.

    Each entry (keyed by cap name) stores:
      category   : str   — from CAP_CATEGORIES or "general"
      tags       : list  — decorator tags + auto-extracted from description
      embedding  : list  — float vector from Ollama (nullable until computed)
      text       : str   — the text that was embedded ("name: description tags…")
      keywords   : set   — stemmed keywords for BM25-style scoring
    """

    def __init__(self):
        self._index: Dict[str, dict] = {}
        self._embed_queue: asyncio.Queue = asyncio.Queue()
        self._embed_running = False

    def build(self):
        """Synchronously build the index from the current CAPABILITY_REGISTRY."""
        for name, cap in CAPABILITY_REGISTRY.items():
            self._index[name] = self._enrich(name, cap)
        log.info("CapabilityIndex built — %d caps", len(self._index))

    def _enrich(self, name: str, cap: dict) -> dict:
        group    = name.split(".")[0]
        category = CAP_CATEGORIES.get(group, "general")
        desc     = cap.get("description", "") or ""
        dec_tags = list(cap.get("tags", [group]))

        # Auto-generate tags from description
        auto_tags = []
        for pattern, tag in _DESC_TAG_PATTERNS:
            if re.search(pattern, desc, re.IGNORECASE) and tag not in dec_tags:
                auto_tags.append(tag)

        all_tags = list(dict.fromkeys(dec_tags + auto_tags + [category, group]))

        # Keyword set for BM25-style matching
        stop = {"the","a","an","and","or","in","on","at","to","for","of","with","via",
                "is","are","was","be","have","has","can","will","that","this","from","by"}
        text_for_kw = f"{name} {desc} {' '.join(all_tags)}"
        keywords = {
            w.lower() for w in re.findall(r'\b[a-zA-Z][a-zA-Z0-9]{2,}\b', text_for_kw)
            if w.lower() not in stop
        }

        # Text for embedding
        params = ", ".join(
            k for k in cap.get("schema", {}).get("properties", {})
            if k not in ("trace_id",)
        )
        embed_text = f"{name}: {desc}. params: {params}. tags: {' '.join(all_tags)}"

        return {
            "name":       name,
            "category":   category,
            "tags":       all_tags,
            "embedding":  [],          # filled async
            "embed_text": embed_text,
            "keywords":   keywords,
            "description":desc,
        }

    async def start_embedding(self):
        """Background task: compute embeddings for all caps without one.

        Loads cached embeddings from Redis first (vera:cap_embeddings hash)
        so we only call Ollama for genuinely new/changed caps.
        """
        if not EMBED_CAPS_ON_START:
            return

        # ── Load cached embeddings from Redis ──
        r = _redis()
        cached: Dict[str, List[float]] = {}
        if r:
            try:
                raw = await r.hgetall("vera:cap_embeddings")
                for k, v in (raw or {}).items():
                    name_key = k if isinstance(k, str) else k.decode()
                    try:
                        vec = json.loads(v if isinstance(v, str) else v.decode())
                        if isinstance(vec, list) and len(vec) > 10:
                            cached[name_key] = vec
                    except Exception:
                        pass
                if cached:
                    log.info("CapabilityIndex: loaded %d cached embeddings from Redis", len(cached))
            except Exception as e:
                log.debug("cap embed cache load: %s", e)

        # Apply cached vectors and identify what still needs embedding
        need_embed = []
        for name, entry in self._index.items():
            if name in cached:
                entry["embedding"] = cached[name]
            elif not entry["embedding"]:
                need_embed.append((name, entry))

        if not need_embed:
            log.info("CapabilityIndex: all %d caps have cached embeddings — no Ollama calls needed",
                     len(self._index))
            return

        log.info("CapabilityIndex: %d/%d caps need embedding", len(need_embed), len(self._index))
        self._embed_running = True
        for name, entry in need_embed:
            await self._embed_one(name, entry)
            await asyncio.sleep(0.05)   # rate-limit
        self._embed_running = False
        log.info("CapabilityIndex: all embeddings computed")

    async def _embed_one(self, name: str, entry: dict):
        try:
            from Vera.Orchestration.capability_orchestration import ollama_embed
            vec = await ollama_embed(
                entry["embed_text"][:512], model=OLLAMA_EMBED_MODEL, timeout=15,
            )
            if vec:
                entry["embedding"] = vec
                # Cache to Redis so we don't re-embed on next restart
                r = _redis()
                if r:
                    try:
                        await r.hset("vera:cap_embeddings", name, json.dumps(vec))
                    except Exception:
                        pass
        except Exception as e:
            log.debug("embed %s: %s", name, e)

    def _cosine(self, a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot  = sum(x*y for x,y in zip(a,b))
        na   = math.sqrt(sum(x*x for x in a))
        nb   = math.sqrt(sum(y*y for y in b))
        return dot / (na * nb + 1e-9)

    async def relevance_search(
        self,
        query: str,
        top_k: int = MAX_CAPS_IN_PROMPT,
        category_filter: Optional[str] = None,
        tag_filter: Optional[List[str]] = None,
    ) -> List[Tuple[str, float]]:
        """
        Return (cap_name, score) list sorted by relevance to query.

        Scoring:
          +3  per query token that appears in cap name
          +2  per query token that appears in a tag
          +1  per query token that appears in description keywords
          +1  if query contains the cap's category word
          +0–3 cosine similarity × 3 (if embedding available)
        """
        if not self._index:
            self.build()

        # Tokenise query
        q_tokens = {
            w.lower() for w in re.findall(r'\b[a-zA-Z][a-zA-Z0-9]{2,}\b', query)
        }

        # Embed query
        q_emb: List[float] = []
        try:
            from Vera.Orchestration.capability_orchestration import ollama_embed
            vec = await ollama_embed(query[:512], model=OLLAMA_EMBED_MODEL, timeout=10)
            if vec:
                q_emb = vec
        except Exception:
            pass

        scores: Dict[str, float] = {}
        for name, entry in self._index.items():
            # Filters
            if category_filter and entry["category"] != category_filter:
                continue
            if tag_filter and not any(t in entry["tags"] for t in tag_filter):
                continue

            s = 0.0
            name_parts = set(name.replace(".", " ").replace("_", " ").split())

            # Token overlap
            for tok in q_tokens:
                if any(tok in p for p in name_parts):         s += 3.0
                if any(tok in t for t in entry["tags"]):       s += 2.0
                if any(tok in kw for kw in entry["keywords"]): s += 1.0

            # Category boost
            if entry["category"].replace("_", " ") in query.lower(): s += 1.5

            # Vector similarity
            if q_emb and entry["embedding"]:
                sim = self._cosine(q_emb, entry["embedding"])
                s += sim * 3.0

            scores[name] = s

        # Sort; take top_k but always include at least a minimal set
        ranked = sorted(scores.items(), key=lambda x: -x[1])

        # Always include a few general caps even if low score
        must_include = {"obs.health", "echo", "health.check"}
        result = [r for r in ranked if r[1] > 0]
        missing = [(n, 0.0) for n in must_include if n in self._index and n not in {r[0] for r in result}]
        result = (result + missing)[:top_k]

        return result

    def cap_signature(self, name: str) -> str:
        """Return a signature string for use in LLM planning prompts.
        Includes full parameter list with required markers, description, and io reads/writes."""
        cap   = CAPABILITY_REGISTRY.get(name, {})
        entry = self._index.get(name, {})
        props = cap.get("schema", {}).get("properties", {})
        req   = set(cap.get("schema", {}).get("required", []))
        io    = cap.get("io")
        params = ", ".join(
            f"{p}:{v.get('type','str')}{'!' if p in req else ''}"
            for p, v in props.items()
            if p not in ("trace_id",)
        )
        desc = (entry.get("description") or cap.get("description", ""))[:200]
        lines = [f"  {name}({params})\n    → {desc}"]
        if io:
            if io.inputs:
                ins = ", ".join(f"{k}({v[:50]})" for k, v in io.inputs.items())
                lines.append(f"    reads : {ins}")
            if io.outputs:
                outs = ", ".join(f"{k}({v[:50]})" for k, v in io.outputs.items())
                lines.append(f"    writes: {outs}")
        return "\n".join(lines)


# Global singleton
CAP_INDEX = CapabilityIndex()


# ─────────────────────────────────────────────────────────────────────────────
# DAG STORE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DagRecord:
    """A stored, reusable DAG definition with rich metadata."""
    id:             str   = field(default_factory=lambda: str(uuid.uuid4()))
    name:           str   = ""
    description:    str   = ""
    tags:           List[str] = field(default_factory=list)
    category:       str   = "general"
    dag:            List  = field(default_factory=list)      # the DAG array
    initial_state:  Dict  = field(default_factory=dict)
    rationale:      str   = ""
    session_id:     str   = ""
    author:         str   = "vera"
    created_at:     str   = field(default_factory=now_iso)
    updated_at:     str   = field(default_factory=now_iso)
    archived:       bool  = False
    use_count:      int   = 0
    last_used:      str   = ""
    avg_runtime_ms: float = 0.0
    # Derived
    nodes_summary:  List[str] = field(default_factory=list)   # ["cap1→key1", …]
    keywords:       List[str] = field(default_factory=list)
    embedding:      List[float] = field(default_factory=list)
    embedding_text: str   = ""
    content_hash:   str   = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("embedding", None)
        return d

    def compute_nodes_summary(self) -> List[str]:
        """Extract capability→outputkey strings from the dag array."""
        summary = []
        def _walk(dag):
            for node in dag:
                if isinstance(node, list) and isinstance(node[0], list):
                    for n in node: _walk([n])
                elif isinstance(node, list) and len(node) >= 2:
                    summary.append(f"{node[0]}→{node[1]}")
        _walk(self.dag)
        return summary

    def build_embed_text(self) -> str:
        caps_used = " ".join(self.nodes_summary)
        return (
            f"{self.name}: {self.description}. "
            f"capabilities: {caps_used}. "
            f"tags: {' '.join(self.tags)}. "
            f"category: {self.category}. "
            f"{self.rationale}"
        )


class DagStore:
    """
    Persistent store for reusable DAG definitions.
    Redis (always) + Postgres (if available) + in-memory cache.
    """

    _REDIS_PREFIX = "vera:dags:"
    _CACHE: Dict[str, DagRecord] = {}

    async def _pg_init(self):
        pg = _pg()
        if not pg: return
        try:
            async with pg.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS vera_dags (
                        id            TEXT PRIMARY KEY,
                        name          TEXT NOT NULL,
                        description   TEXT NOT NULL DEFAULT '',
                        tags          JSONB NOT NULL DEFAULT '[]',
                        category      TEXT NOT NULL DEFAULT 'general',
                        dag           JSONB NOT NULL DEFAULT '[]',
                        initial_state JSONB NOT NULL DEFAULT '{}',
                        rationale     TEXT NOT NULL DEFAULT '',
                        author        TEXT NOT NULL DEFAULT 'vera',
                        created_at    TIMESTAMPTZ NOT NULL,
                        updated_at    TIMESTAMPTZ NOT NULL,
                        archived      BOOLEAN NOT NULL DEFAULT FALSE,
                        use_count     INT NOT NULL DEFAULT 0,
                        last_used     TEXT NOT NULL DEFAULT '',
                        avg_runtime_ms FLOAT NOT NULL DEFAULT 0,
                        nodes_summary  JSONB NOT NULL DEFAULT '[]',
                        keywords       JSONB NOT NULL DEFAULT '[]',
                        embedding_text TEXT NOT NULL DEFAULT '',
                        content_hash   TEXT NOT NULL DEFAULT '',
                        search_vector  tsvector GENERATED ALWAYS AS (
                            to_tsvector('english',
                                coalesce(name,'') || ' ' ||
                                coalesce(description,'') || ' ' ||
                                coalesce(rationale,'') || ' ' ||
                                coalesce(embedding_text,''))
                        ) STORED
                    )
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS vd_cat ON vera_dags(category)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vd_fts ON vera_dags USING gin(search_vector)")
                await conn.execute("CREATE INDEX IF NOT EXISTS vd_tags ON vera_dags USING gin(tags)")
            log.info("DagStore: Postgres table ready")
        except Exception as e:
            log.warning("DagStore pg_init: %s", e)

    async def _embed(self, rec: DagRecord):
        if not rec.embedding_text:
            rec.embedding_text = rec.build_embed_text()
        try:
            from Vera.Orchestration.capability_orchestration import ollama_embed
            vec = await ollama_embed(
                rec.embedding_text[:512], model=OLLAMA_EMBED_MODEL, timeout=15,
            )
            if vec:
                rec.embedding = vec
        except Exception as e:
            log.debug("DagStore embed: %s", e)

    async def save(self, rec: DagRecord) -> DagRecord:
        """Persist a DAG record to Redis (and Postgres if available)."""
        rec.updated_at     = now_iso()
        rec.nodes_summary  = rec.compute_nodes_summary()
        rec.embedding_text = rec.build_embed_text()

        # Keywords
        stop = {"the","a","an","and","or","in","to","for","of","with","is","are"}
        text = f"{rec.name} {rec.description} {' '.join(rec.tags)} {' '.join(rec.nodes_summary)}"
        rec.keywords = list({
            w.lower() for w in re.findall(r'\b[a-zA-Z][a-zA-Z0-9]{2,}\b', text)
            if w.lower() not in stop
        })

        # Embedding (async, don't block)
        asyncio.create_task(self._embed(rec))

        # Cache
        self._CACHE[rec.id] = rec

        # Redis
        r = _redis()
        if r:
            try:
                data = {k: json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                        for k, v in asdict(rec).items() if k != "embedding"}
                await r.hset(f"{self._REDIS_PREFIX}{rec.id}", mapping=data)
                # Also store name→id index for lookup by name
                await r.set(f"vera:dag_names:{rec.name.lower()}", rec.id)
                log.info("DagStore saved: %s (%s)", rec.name, rec.id)
            except Exception as e:
                log.warning("DagStore Redis save: %s", e)

        # Postgres
        pg = _pg()
        if pg:
            try:
                async with pg.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO vera_dags (
                            id, name, description, tags, category, dag, initial_state,
                            rationale, author, created_at, updated_at, archived,
                            use_count, last_used, avg_runtime_ms,
                            nodes_summary, keywords, embedding_text, content_hash
                        ) VALUES (
                            $1,$2,$3,$4::JSONB,$5,$6::JSONB,$7::JSONB,
                            $8,$9,$10::TIMESTAMPTZ,$11::TIMESTAMPTZ,$12,
                            $13,$14,$15,$16::JSONB,$17::JSONB,$18,$19
                        ) ON CONFLICT (id) DO UPDATE SET
                            name=EXCLUDED.name, description=EXCLUDED.description,
                            tags=EXCLUDED.tags, category=EXCLUDED.category,
                            dag=EXCLUDED.dag, initial_state=EXCLUDED.initial_state,
                            rationale=EXCLUDED.rationale, updated_at=EXCLUDED.updated_at,
                            archived=EXCLUDED.archived, nodes_summary=EXCLUDED.nodes_summary,
                            keywords=EXCLUDED.keywords, embedding_text=EXCLUDED.embedding_text
                    """,
                    rec.id, rec.name, rec.description,
                    json.dumps(rec.tags), rec.category,
                    json.dumps(rec.dag), json.dumps(rec.initial_state),
                    rec.rationale, rec.author,
                    rec.created_at, rec.updated_at, rec.archived,
                    rec.use_count, rec.last_used, rec.avg_runtime_ms,
                    json.dumps(rec.nodes_summary), json.dumps(rec.keywords),
                    rec.embedding_text, rec.content_hash,
                    )
            except Exception as e:
                log.warning("DagStore PG save: %s", e)

        return rec

    async def get(self, dag_id: str) -> Optional[DagRecord]:
        if dag_id in self._CACHE:
            return self._CACHE[dag_id]
        r = _redis()
        if r:
            try:
                raw = await r.hgetall(f"{self._REDIS_PREFIX}{dag_id}")
                if raw:
                    return self._from_redis(raw)
            except Exception as e:
                log.warning("DagStore get: %s", e)
        return None

    async def get_by_name(self, name: str) -> Optional[DagRecord]:
        r = _redis()
        if r:
            try:
                dag_id = await r.get(f"vera:dag_names:{name.lower()}")
                if dag_id:
                    did = dag_id.decode() if isinstance(dag_id, bytes) else dag_id
                    return await self.get(did)
            except Exception:
                pass
        # Fall back to scan
        results = await self.search(name, limit=1)
        if results:
            return results[0][0]
        return None

    async def list_all(self, category: Optional[str] = None,
                       tag: Optional[str] = None,
                       include_archived: bool = False) -> List[DagRecord]:
        r = _redis()
        results = []
        if r:
            try:
                keys = await r.keys(f"{self._REDIS_PREFIX}*")
                for k in keys:
                    raw = await r.hgetall(k)
                    if raw:
                        rec = self._from_redis(raw)
                        if not include_archived and rec.archived:
                            continue
                        if category and rec.category != category:
                            continue
                        if tag and tag not in rec.tags:
                            continue
                        results.append(rec)
            except Exception as e:
                log.warning("DagStore list_all: %s", e)
        results.sort(key=lambda x: x.updated_at, reverse=True)
        return results

    async def search(self, query: str, limit: int = 10,
                     category: Optional[str] = None,
                     tags: Optional[List[str]] = None) -> List[Tuple[DagRecord, float]]:
        """Keyword + embedding similarity search across stored DAGs."""
        all_recs = await self.list_all(category=category)
        if not all_recs:
            return []

        # Embed query
        q_emb: List[float] = []
        try:
            from Vera.Orchestration.capability_orchestration import ollama_embed
            vec = await ollama_embed(query[:512], model=OLLAMA_EMBED_MODEL, timeout=10)
            if vec:
                q_emb = vec
        except Exception:
            pass

        def _cos(a, b):
            if not a or not b or len(a) != len(b): return 0.0
            dot = sum(x*y for x,y in zip(a,b))
            return dot / (math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b)) + 1e-9)

        q_tokens = {w.lower() for w in re.findall(r'\b\w{3,}\b', query)}
        scored = []
        for rec in all_recs:
            if tags and not any(t in rec.tags for t in tags):
                continue
            s = 0.0
            rec_kws = set(rec.keywords)
            # Keyword overlap
            for tok in q_tokens:
                if tok in rec.name.lower():          s += 3.0
                if any(tok in t for t in rec.tags):  s += 2.0
                if tok in rec_kws:                   s += 1.0
            # Vector sim
            if q_emb and rec.embedding:
                s += _cos(q_emb, rec.embedding) * 3.0
            # Recency boost (use_count)
            s += min(rec.use_count * 0.1, 0.5)
            scored.append((rec, s))

        return sorted(scored, key=lambda x: -x[1])[:limit]

    async def update_stats(self, dag_id: str, runtime_ms: float):
        rec = await self.get(dag_id)
        if not rec: return
        n = rec.use_count
        rec.use_count    = n + 1
        rec.last_used    = now_iso()
        rec.avg_runtime_ms = (rec.avg_runtime_ms * n + runtime_ms) / (n + 1)
        await self.save(rec)

    async def delete(self, dag_id: str) -> bool:
        rec = await self.get(dag_id)
        if not rec: return False
        rec.archived = True
        await self.save(rec)
        # If this DAG was promoted to a live capability, remove it from the registry
        cap_name = _DAG_CAP_REGISTRY.cap_name(rec.name)
        if CAPABILITY_REGISTRY.get(cap_name, {}).get("source") == "dag_cap":
            del CAPABILITY_REGISTRY[cap_name]
            await _DAG_CAP_REGISTRY.remove(cap_name)
            log.info("DagStore.delete: removed live capability %s", cap_name)
        return True

    @staticmethod
    def _from_redis(raw: dict) -> DagRecord:
        def _d(k, fallback=None):
            v = raw.get(k.encode() if isinstance(list(raw.keys())[0], bytes) else k, fallback)
            if isinstance(v, bytes): v = v.decode()
            return v
        def _j(k, fb):
            try:    return json.loads(_d(k, "null") or "null") or fb
            except: return fb

        return DagRecord(
            id            = _d("id", str(uuid.uuid4())),
            name          = _d("name", ""),
            description   = _d("description", ""),
            tags          = _j("tags", []),
            category      = _d("category", "general"),
            session_id    = _j("session_id", ""),
            dag           = _j("dag", []),
            initial_state = _j("initial_state", {}),
            rationale     = _d("rationale", ""),
            author        = _d("author", "vera"),
            created_at    = _d("created_at", now_iso()),
            updated_at    = _d("updated_at", now_iso()),
            archived      = _d("archived", "False") == "True",
            use_count     = int(_d("use_count", "0") or 0),
            last_used     = _d("last_used", ""),
            avg_runtime_ms= float(_d("avg_runtime_ms", "0") or 0),
            nodes_summary = _j("nodes_summary", []),
            keywords      = _j("keywords", []),
            embedding_text= _d("embedding_text", ""),
        )


# Global singleton
DAG_STORE = DagStore()


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION MONITOR  —  Redis-watched execution with error correction
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionMonitor:
    """
    Wrap run_graph with Redis event monitoring and LLM error correction.

    After execution:
      1. Scan the result state for {"error":…} values
      2. For each error, build a correction prompt with:
         - The cap name + its schema
         - The error message
         - The state at the time of the call
      3. Ask the LLM to produce corrected input params
      4. Re-run the failed node with corrected params
      5. Report: nodes_run, errors_found, corrections_attempted, corrections_succeeded
    """

    async def run_and_correct(
        self,
        dag:            list,
        state:          dict,
        trace_id:       str,
        max_corrections: int = 3,
        supervised:      bool = False,
    ) -> dict:
        """Execute dag, then attempt LLM-driven error correction for failed nodes."""
        from Vera.Orchestration.capability_orchestration import run_graph, supervised_run_graph

        t0     = time.monotonic()
        fn     = supervised_run_graph if supervised else run_graph
        result = await fn(dag, dict(state))
        runtime_ms = (time.monotonic() - t0) * 1000

        # Find errors in result
        errors = {k: v for k, v in result.items()
                  if isinstance(v, dict) and "error" in v}

        corrections = []
        if errors and max_corrections > 0:
            corrections = await self._correct_errors(
                dag, state, result, errors, max_corrections
            )
            # Apply corrections
            for corr in corrections:
                if corr.get("success"):
                    result[corr["output_key"]] = corr["corrected_result"]

        await emit_event({
            "type":         "dag.execution_report",
            "trace_id":     trace_id,
            "runtime_ms":   round(runtime_ms),
            "nodes_run":    len(dag),
            "errors_found": len(errors),
            "corrections":  len([c for c in corrections if c.get("success")]),
            "error_keys":   list(errors.keys()),
        })

        return {
            "result":          result,
            "runtime_ms":      round(runtime_ms),
            "errors_found":    len(errors),
            "corrections":     corrections,
            "execution_report": {
                "nodes":       len(dag),
                "errors":      list(errors.keys()),
                "corrected":   [c["output_key"] for c in corrections if c.get("success")],
            }
        }

    async def _correct_errors(
        self,
        dag:     list,
        state:   dict,
        result:  dict,
        errors:  dict,
        max_n:   int,
    ) -> list:
        """For each failed node, ask the LLM to fix the inputs and retry."""
        from Vera.Orchestration.capability_orchestration import _flatten_dag

        corrections = []
        # Map output_key → node
        node_map = {node[1]: node for node in _flatten_dag(dag)
                    if not isinstance(node[0], list)}

        for output_key, error_val in list(errors.items())[:max_n]:
            node = node_map.get(output_key)
            if not node:
                continue

            cap_name = node[0]
            cap      = CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                continue

            # Build correction prompt
            schema_props = cap.get("schema", {}).get("properties", {})
            params_desc  = "\n".join(
                f"  {k}: {v.get('type','str')} — {v.get('description','')}"
                for k, v in schema_props.items()
                if k not in ("trace_id",)
            )
            state_snapshot = {
                k: (str(v)[:200] if not isinstance(v, (dict, list)) else json.dumps(v)[:200])
                for k, v in result.items()
                if k != output_key
            }

            prompt = (
                f"A DAG node failed. Fix the input parameters.\n\n"
                f"Capability: {cap_name}\n"
                f"Error: {json.dumps(error_val)}\n\n"
                f"Parameters this capability accepts:\n{params_desc}\n\n"
                f"Current state (available values):\n{json.dumps(state_snapshot, indent=2)[:1000]}\n\n"
                f"Return ONLY a JSON object of corrected input parameters: {{\"param\": \"value\"}}"
            )

            raw = await ollama_generate(
                prompt,
                system="You are a DAG error corrector. Return only a JSON object of corrected parameters.",
                json_mode=True,
                prefer_gpu=False,
            )

            corrected_params = {}
            try:
                corrected_params = json.loads(raw)
            except Exception:
                m = re.search(r'\{[\s\S]*\}', raw or "")
                if m:
                    try: corrected_params = json.loads(m.group())
                    except: pass

            corr_entry: dict = {
                "output_key":   output_key,
                "cap_name":     cap_name,
                "original_error": error_val,
                "corrected_params": corrected_params,
                "success":      False,
            }

            if corrected_params:
                # Merge with existing state
                merged = {**{k: v for k, v in result.items() if not isinstance(v, dict) or "error" not in v},
                          **corrected_params}
                # Re-run the single node
                accepted = set(schema_props.keys())
                run_params = {k: v for k, v in merged.items() if k in accepted}
                try:
                    corrected_result = await cap["func"](**run_params)
                    if not (isinstance(corrected_result, dict) and "error" in corrected_result):
                        corr_entry["success"]          = True
                        corr_entry["corrected_result"] = corrected_result
                        log.info("ExecutionMonitor: corrected %s → %s", cap_name, output_key)
                    else:
                        corr_entry["retry_error"] = corrected_result
                except Exception as e:
                    corr_entry["retry_exception"] = str(e)

            corrections.append(corr_entry)

        return corrections


EXEC_MONITOR = ExecutionMonitor()


# ─────────────────────────────────────────────────────────────────────────────
# DAG-AS-CAPABILITY  —  promote stored DAGs to live CAPABILITY_REGISTRY entries
# ─────────────────────────────────────────────────────────────────────────────

class _DagCapRegistry:
    """
    Tracks which stored DAGs have been promoted to live capabilities.
    Registrations are persisted to Redis hash  vera:dag_caps  so they
    survive restarts (restored in _startup).
    """
    _REDIS_SET = "vera:dag_caps"

    def __init__(self):
        self._registered: Dict[str, str] = {}  # cap_name → dag_id

    @staticmethod
    def cap_name(dag_name: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "_", dag_name.lower().strip())
        return f"dag.{safe}"

    async def persist(self, cap_name: str, dag_id: str):
        self._registered[cap_name] = dag_id
        try:
            r = _redis()
            if r:
                await r.hset(self._REDIS_SET, cap_name, dag_id)
        except Exception:
            pass

    async def remove(self, cap_name: str):
        self._registered.pop(cap_name, None)
        try:
            r = _redis()
            if r:
                await r.hdel(self._REDIS_SET, cap_name)
        except Exception:
            pass

    async def restore_from_redis(self):
        """Re-register DAGs that were promoted in a prior session."""
        try:
            r = _redis()
            if not r:
                return
            raw = await r.hgetall(self._REDIS_SET)
            for k, v in raw.items():
                cap_name = k.decode() if isinstance(k, bytes) else k
                dag_id   = v.decode() if isinstance(v, bytes) else v
                if cap_name not in self._registered:
                    rec = await DAG_STORE.get(dag_id)
                    if rec and not rec.archived:
                        await _register_dag_as_cap(rec)
        except Exception as e:
            log.warning("_DagCapRegistry.restore_from_redis: %s", e)

    def list(self) -> Dict[str, str]:
        return dict(self._registered)


_DAG_CAP_REGISTRY = _DagCapRegistry()


async def _register_dag_as_cap(rec) -> str:
    """
    Wrap a DagRecord into a live CAPABILITY_REGISTRY entry.

    The generated capability name is  dag.{safe_name}.
    Its schema is derived from rec.initial_state keys (overridable at call time).
    Type hints and output declarations can be embedded in the record's tags:
      "input:key_name:type"   → typed input param
      "output:field_name:desc" → declared output field (for io descriptor)
    """
    from Vera.Orchestration.capability_orchestration import (
        CAPABILITY_REGISTRY, CapabilityIO, now_iso,
    )

    cap_name = _DAG_CAP_REGISTRY.cap_name(rec.name)

    # Parse type/output hints from tags
    type_hints:   Dict[str, str] = {}
    output_hints: Dict[str, str] = {}
    for tag in rec.tags:
        if tag.startswith("input:"):
            parts = tag.split(":")
            if len(parts) >= 2:
                type_hints[parts[1]] = parts[2] if len(parts) > 2 else "string"
        elif tag.startswith("output:"):
            parts = tag.split(":")
            if len(parts) >= 2:
                output_hints[parts[1]] = parts[2] if len(parts) > 2 else "value"

    # Schema from initial_state keys + any extra declared inputs
    props: Dict[str, dict] = {}
    for key in rec.initial_state:
        props[key] = {"type": type_hints.get(key, "string"),
                      "description": f"Override initial_state[{key!r}]"}
    for key, typ in type_hints.items():
        if key not in props:
            props[key] = {"type": typ, "description": f"Input: {key}"}

    schema = {"type": "object", "properties": props, "required": []}

    # io descriptor for planner
    io = CapabilityIO(
        inputs  = {k: v.get("description", k) for k, v in props.items()},
        outputs = output_hints or {s.split("→")[1]: f"output from {s.split('→')[0]}"
                                   for s in rec.nodes_summary if "→" in s},
    )

    _dag_id = rec.id
    _dag    = rec.dag
    _init   = dict(rec.initial_state)
    _desc   = rec.description or f"Stored DAG: {rec.name}"
    _tags   = list(rec.tags)

    async def _dag_cap_fn(**kwargs):
        _trace_id = kwargs.pop("trace_id", None)
        state     = {**_init, **{k: v for k, v in kwargs.items()}}
        tid       = _trace_id or new_id_fn()
        result    = await EXEC_MONITOR.run_and_correct(_dag, state, tid)
        await DAG_STORE.update_stats(_dag_id, result.get("runtime_ms", 0))
        return result.get("result", result)

    _dag_cap_fn.__name__ = cap_name
    _dag_cap_fn.__doc__  = _desc

    group = "dag"
    CAPABILITY_REGISTRY[cap_name] = {
        "func":        _dag_cap_fn,
        "raw":         _dag_cap_fn,
        "schema":      schema,
        "description": _desc,
        "streams":     [],
        "mode":        "local",
        "retries":     0,
        "tags":        _tags + ["dag_cap", group],
        "source":      "dag_cap",
        "mcp_expose":  True,
        "memory":      "on",
        "io":          io,
        "http_method": "POST",
        "http_path":   f"/dag/cap/{cap_name.replace('.', '/')}",
        "http_tags":   ["dag", "dag_cap"],
    }

    await _DAG_CAP_REGISTRY.persist(cap_name, _dag_id)
    log.info("dag.register: %s → capability %s", rec.name, cap_name)
    return cap_name
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "caps.search", memory="off",
    http_method="POST", http_path="/caps/search", http_tags=["caps"],
    description="Search capabilities by semantic relevance to a query. "
                "Returns ranked list with signatures, categories, and tags.",
)
async def caps_search(
    query:    str,
    top_k:    int  = 20,
    category: str  = "",
    tags:     str  = "",
    trace_id=None,
):
    results = await CAP_INDEX.relevance_search(
        query,
        top_k=top_k,
        category_filter=category or None,
        tag_filter=[t.strip() for t in tags.split(",") if t.strip()] or None,
    )
    return {
        "results": [
            {
                "name":      name,
                "score":     round(score, 3),
                "signature": CAP_INDEX.cap_signature(name),
                "category":  CAP_INDEX._index.get(name, {}).get("category"),
                "tags":      CAP_INDEX._index.get(name, {}).get("tags", []),
            }
            for name, score in results
        ],
        "count": len(results),
        "query": query,
    }


@capability(
    "caps.list_categories", memory="off",
    http_method="GET", http_path="/caps/categories", http_tags=["caps"],
    description="List all capability categories and the caps in each.",
)
async def caps_list_categories(trace_id=None):
    cats: Dict[str, list] = {}
    for name, entry in CAP_INDEX._index.items():
        c = entry.get("category", "general")
        cats.setdefault(c, []).append(name)
    return {"categories": {k: sorted(v) for k, v in sorted(cats.items())}}


@capability(
    "caps.describe", memory="off",
    http_method="GET", http_path="/caps/describe", http_tags=["caps"],
    description="Return the full I/O descriptor for a capability — declared input params "
                "and output fields. Use this before writing input_map / output_map for a DAG node.",
)
async def caps_describe(name: str, trace_id=None):
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"Unknown capability: {name}"}
    schema = cap.get("schema", {})
    props  = schema.get("properties", {})
    req    = set(schema.get("required", []))
    io     = cap.get("io")
    return {
        "name":        name,
        "description": cap.get("description", ""),
        "source":      cap.get("source", "local"),
        "tags":        cap.get("tags", []),
        "schema_inputs": {
            p: {"type": v.get("type", "string"), "required": p in req,
                "description": v.get("description", "")}
            for p, v in props.items() if p not in ("trace_id",)
        },
        "io": io.to_dict() if io else {"inputs": {}, "outputs": {}},
        "example_node": (
            f'["{name}", "result_key", null, '
            + json.dumps({p: f"<state_key_for_{p}>" for p in (io.inputs if io else props) if p != "trace_id"})
            + ", "
            + (json.dumps({sk: f"<result field>" for sk in io.outputs}) if (io and io.outputs) else "null")
            + "]"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DAG-AS-CAPABILITY MANAGEMENT CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dag.register", memory="on",
    http_method="POST", http_path="/dag/register", http_tags=["dag"],
    description="Promote a stored DAG to a first-class capability so it can be used "
                "as a node inside other DAGs. The generated cap name is dag.{safe_name}. "
                "Embed type hints in tags as 'input:key:type' and 'output:field:desc'.",
)
async def dag_register(id: str = "", name: str = "", trace_id=None):
    rec = None
    if id:        rec = await DAG_STORE.get(id)
    if not rec and name: rec = await DAG_STORE.get_by_name(name)
    if not rec:
        return {"error": f"DAG not found: {id or name}"}
    if rec.archived:
        return {"error": f"DAG {rec.name!r} is archived and cannot be registered"}
    cap_name = await _register_dag_as_cap(rec)
    return {
        "registered":  True,
        "cap_name":    cap_name,
        "dag_id":      rec.id,
        "dag_name":    rec.name,
        "schema_keys": list(rec.initial_state.keys()),
        "nodes":       rec.nodes_summary,
    }


@capability(
    "dag.unregister", memory="off",
    http_method="POST", http_path="/dag/unregister", http_tags=["dag"],
    description="Remove a DAG-capability from the live registry. The stored DAG is not deleted.",
)
async def dag_unregister(cap_name: str = "", dag_name: str = "", trace_id=None):
    target = cap_name or _DAG_CAP_REGISTRY.cap_name(dag_name)
    if target not in CAPABILITY_REGISTRY:
        return {"error": f"No live capability found: {target}"}
    if CAPABILITY_REGISTRY[target].get("source") != "dag_cap":
        return {"error": f"{target} exists but is not a registered DAG-capability"}
    del CAPABILITY_REGISTRY[target]
    await _DAG_CAP_REGISTRY.remove(target)
    return {"unregistered": True, "cap_name": target}


@capability(
    "dag.list_registered", memory="off",
    http_method="GET", http_path="/dag/registered", http_tags=["dag"],
    description="List all DAGs currently promoted to live capabilities.",
)
async def dag_list_registered(trace_id=None):
    live = _DAG_CAP_REGISTRY.list()
    results = []
    for cap_name, dag_id in live.items():
        cap = CAPABILITY_REGISTRY.get(cap_name, {})
        io  = cap.get("io")
        results.append({
            "cap_name":    cap_name,
            "dag_id":      dag_id,
            "description": cap.get("description", ""),
            "schema":      cap.get("schema", {}),
            "io":          io.to_dict() if io else None,
        })
    return {"registered": results, "count": len(results)}


# ─────────────────────────────────────────────────────────────────────────────
# DAG STORE CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "dag.store_save", memory="on",
    http_method="POST", http_path="/dag/store/save", http_tags=["dag"],
    description="Save a DAG definition to the store. Generates embedding, keywords, and node summary automatically.",
)
async def dag_store_save(
    name:          str,
    dag:           str,           # JSON string or already-object (coerced below)
    description:   str   = "",
    tags:          str   = "",    # comma-separated
    category:      str   = "general",
    initial_state: str   = "{}",
    rationale:     str   = "",
    author:        str   = "vera",
    dag_id:        str   = "",    # if set, update existing record
    session_id:    str   = "",    # session that created this DAG
    trace_id=None,
):
    # Accept dag as JSON string or native list (if called from Python directly)
    if isinstance(dag, list):
        dag_arr = dag
    else:
        try:
            dag_arr = json.loads(dag)
        except Exception as e:
            return {"error": f"Invalid DAG JSON: {e}"}

    if isinstance(initial_state, dict):
        init = initial_state
    else:
        try:
            init = json.loads(initial_state) if initial_state else {}
        except Exception:
            init = {}

    # Populate session_id from trigger chain if not provided
    import sys as _sys
    _syslog = _sys.modules.get("syslog")
    _chain  = _syslog.get_trigger_chain() if _syslog else {}
    effective_session = session_id or _chain.get("session_id", "")

    rec = DagRecord(
        id            = dag_id or str(uuid.uuid4()),
        session_id    = effective_session,
        name          = name,
        description   = description,
        tags          = [t.strip() for t in tags.split(",") if t.strip()],
        category      = category,
        dag           = dag_arr,
        initial_state = init,
        rationale     = rationale,
        author        = author,
    )
    saved = await DAG_STORE.save(rec)
    return {"id": saved.id, "name": saved.name, "nodes": len(saved.nodes_summary),
            "nodes_summary": saved.nodes_summary, "keywords": saved.keywords[:10]}


@capability(
    "dag.store_list", memory="off",
    http_method="GET", http_path="/dag/store/list", http_tags=["dag"],
    description="List all stored DAGs. Filter by category or tag.",
)
async def dag_store_list(
    category:         str  = "",
    tag:              str  = "",
    include_archived: bool = False,
    trace_id=None,
):
    recs = await DAG_STORE.list_all(
        category=category or None,
        tag=tag or None,
        include_archived=include_archived,
    )
    return {
        "dags": [r.to_dict() for r in recs],
        "count": len(recs),
    }


@capability(
    "dag.store_get", memory="off",
    http_method="GET", http_path="/dag/store/get", http_tags=["dag"],
    description="Fetch a stored DAG by id or name.",
)
async def dag_store_get(id: str = "", name: str = "", trace_id=None):
    rec = None
    if id:   rec = await DAG_STORE.get(id)
    if not rec and name: rec = await DAG_STORE.get_by_name(name)
    if not rec: return {"error": f"DAG not found: {id or name}"}
    return rec.to_dict()


@capability(
    "dag.store_search", memory="off",
    http_method="POST", http_path="/dag/store/search", http_tags=["dag"],
    description="Semantic + keyword search across stored DAGs. "
                "Uses vector similarity of query against DAG embeddings.",
)
async def dag_store_search(
    query:    str,
    limit:    int  = 10,
    category: str  = "",
    tags:     str  = "",
    trace_id=None,
):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
    results  = await DAG_STORE.search(
        query, limit=limit,
        category=category or None,
        tags=tag_list,
    )
    return {
        "results": [
            {"dag": r.to_dict(), "score": round(s, 3)}
            for r, s in results
        ],
        "count": len(results),
        "query": query,
    }


@capability(
    "dag.store_delete", memory="off",
    http_method="POST", http_path="/dag/store/delete", http_tags=["dag"],
    description="Soft-delete a stored DAG (sets archived=True). "
                "If the DAG was registered as a live capability via dag.register, "
                "that capability is automatically removed from the registry too.",
)
async def dag_store_delete(id: str, trace_id=None):
    # Fetch name before deletion so we can report the cap_name
    rec = await DAG_STORE.get(id)
    if not rec:
        return {"deleted": False, "id": id, "error": "DAG not found"}
    cap_name = _DAG_CAP_REGISTRY.cap_name(rec.name)
    was_registered = CAPABILITY_REGISTRY.get(cap_name, {}).get("source") == "dag_cap"
    ok = await DAG_STORE.delete(id)
    return {
        "deleted":             ok,
        "id":                  id,
        "name":                rec.name,
        "capability_removed":  was_registered,
        "cap_name":            cap_name if was_registered else None,
    }


@capability(
    "dag.store_run", memory="on",
    http_method="POST", http_path="/dag/store/run", http_tags=["dag"],
    description="Load a stored DAG by id or name and execute it. "
                "Optionally override initial_state. Returns result + execution report.",
)
async def dag_store_run(
    id:            str  = "",
    name:          str  = "",
    state_override:str  = "{}",
    supervised:    bool = False,
    auto_correct:  bool = True,
    trace_id=None,
):
    rec = None
    if id:   rec = await DAG_STORE.get(id)
    if not rec and name: rec = await DAG_STORE.get_by_name(name)
    if not rec: return {"error": f"DAG not found: {id or name}"}

    try: override = json.loads(state_override)
    except: override = {}

    state = {**rec.initial_state, **override}
    tid   = trace_id or new_id_fn()
    t0    = time.monotonic()

    if auto_correct:
        result = await EXEC_MONITOR.run_and_correct(
            rec.dag, state, tid, supervised=supervised
        )
    else:
        from Vera.Orchestration.capability_orchestration import run_graph, supervised_run_graph
        fn     = supervised_run_graph if supervised else run_graph
        raw    = await fn(rec.dag, state)
        result = {"result": raw, "runtime_ms": round((time.monotonic()-t0)*1000)}

    # Update usage stats
    await DAG_STORE.update_stats(rec.id, result.get("runtime_ms", 0))
    await emit_event({"type": "dag.store_run", "id": rec.id, "name": rec.name, "trace_id": tid})

    return {"dag_id": rec.id, "dag_name": rec.name, **result}


@capability(
    "dag.run_monitored", memory="on",
    http_method="POST", http_path="/dag/run/monitored", http_tags=["dag"],
    description="Execute a DAG with Redis monitoring and LLM error correction. "
                "Failed nodes are automatically retried with LLM-corrected inputs.",
)
async def dag_run_monitored(
    dag:          str,            # JSON array string
    state:        str  = "{}",
    supervised:   bool = False,
    auto_correct: bool = True,
    trace_id=None,
):
    try: dag_arr = json.loads(dag)
    except Exception as e:
        return {"error": f"Invalid DAG JSON: {e}"}
    try: state_dict = json.loads(state)
    except: state_dict = {}

    tid = trace_id or new_id_fn()
    return await EXEC_MONITOR.run_and_correct(
        dag_arr, state_dict, tid,
        max_corrections=3 if auto_correct else 0,
        supervised=supervised,
    )


def new_id_fn():
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# PLAN_DAG INTEGRATION  —  patch orchestrator's plan_dag to use CAP_INDEX
# ─────────────────────────────────────────────────────────────────────────────

async def _plan_dag_with_index(goal: str, available_caps=None) -> dict:
    """
    Drop-in replacement for vera_orchestrator.plan_dag.
    Uses CapabilityIndex for cap selection and includes io descriptors in the
    prompt so the LLM can write correct input_map / output_map.

    Steps:
      1. Select relevant caps via CapabilityIndex
      2. Ask dag-planner agent (or ollama_generate) for a DAG plan
      3. Validate cap names, self-correct unknown caps
      4. POST-PLAN REPAIR — fix missing initial_state values, bad CONDITION
         elements, output key clashes, accounting for explicit input_map
    """
    import Vera.Orchestration.capability_orchestration as _o

    _EXCLUDE_GROUPS = {"obs","syslog","memory","caps","mcp","ui","health","echo","debug","ollama","agent"}

    if available_caps:
        relevant = [(k, 1.0) for k in available_caps if k in CAPABILITY_REGISTRY]
    else:
        all_rel = await CAP_INDEX.relevance_search(goal, top_k=MAX_CAPS_IN_PROMPT * 2)
        relevant = [(k, s) for k, s in all_rel if k.split(".")[0] not in _EXCLUDE_GROUPS][:MAX_CAPS_IN_PROMPT]

    for _ess in ["http.get", "system.ping", "llm.generate", "llm.summarize"]:
        if _ess in CAPABILITY_REGISTRY and not any(k == _ess for k, _ in relevant):
            relevant.append((_ess, 0.5))

    cap_names = [name for name, _ in relevant]
    cap_desc  = "\n".join(CAP_INDEX.cap_signature(n) for n in cap_names)

    _PLANNER_SYSTEM = (
        "You are a Vera DAG planner. Produce a correct, minimal DAG JSON.\n\n"

        "NODE FORMAT — 5-tuple (elements after [1] are optional, use null):\n"
        "  [cap_name, output_key, condition, input_map, output_map]\n\n"

        "ELEMENTS:\n"
        "  cap_name   : string — MUST be from the capability list.\n"
        "  output_key : string | null — state key for the full result.\n"
        "               Use a string when you want the entire result dict available.\n"
        "               Use null when output_map covers everything you need.\n"
        "  condition  : null | \"CONDITION:state_key\" — skip node if state[key] is falsy.\n"
        "  input_map  : null | {\"param_name\": \"state_key\"}\n"
        "               RENAME ONLY — maps a cap param name to a DIFFERENT state key.\n"
        "               The value must be an existing state key name, not a literal value.\n"
        "               Do NOT put literal values here. Literals go in initial_state.\n"
        "               null values in input_map are ignored — omit keys you don't need.\n"
        "  output_map : null | {\"state_key\": \"result_field\"}\n"
        "               Extract named fields from the result dict into state.\n"
        "               Check the cap's 'writes' line for available field names.\n\n"

        "CORRECT EXAMPLES:\n"
        "  Simple — param name matches state key (most common):\n"
        "    [\"http.get\", \"page_result\"]\n"
        "    initial_state: {\"url\": \"https://example.com\"}\n\n"
        "  output_map only — extract specific fields, no need for full result key:\n"
        "    [\"dag.store_list\", null, null, null, {\"items\": \"dags\"}]\n"
        "    → state[\"items\"] = result[\"dags\"]\n\n"
        "  output_key + output_map — keep full result AND extract a field:\n"
        "    [\"llm.generate\", \"llm_out\", null, null, {\"summary\": \"text\"}]\n"
        "    → state[\"llm_out\"] = full result, state[\"summary\"] = result[\"text\"]\n\n"
        "  input_map — state key 'article' feeds cap param 'prompt':\n"
        "    [\"llm.generate\", \"llm_out\", null, {\"prompt\": \"article\"}, {\"summary\": \"text\"}]\n\n"
        "  condition only:\n"
        "    [\"llm.generate\", \"summary\", \"CONDITION:site_ok\", null, null]\n\n"

        "WRONG — never put literal values in input_map:\n"
        "  BAD : [\"http.get\", \"page\", null, {\"url\": \"https://example.com\"}]\n"
        "  GOOD: [\"http.get\", \"page\"]  with  initial_state: {\"url\": \"https://example.com\"}\n\n"

        "WRONG — never put null as a value inside input_map or output_map dicts:\n"
        "  BAD : [\"skills.list\", null, null, {\"results\": null}]\n"
        "  GOOD: [\"skills.list\", null, null, null, {\"skills\": \"results\"}]\n\n"

        "RULES:\n"
        "1. Only use cap names from the list. Registered DAG caps appear as dag.*.\n"
        "2. Every required param (!) must be reachable: in initial_state, or produced\n"
        "   by a prior node's output_key or output_map target key.\n"
        "3. initial_state must hold ALL literal values the DAG needs.\n"
        "4. Use input_map ONLY to rename a prior output key to match the next cap's param.\n"
        "5. Parallel nodes: [[\"cap_a\",\"key_a\"],[\"cap_b\",\"key_b\"]] — array of arrays.\n"
        "6. Max 6 nodes. No redundant steps.\n\n"

        "Respond: brief explanation, then exactly one ```json block:\n"
        "  {\"dag\": [...], \"initial_state\": {...}, \"rationale\": \"...\"}"
    )

    prompt = (
        f"Goal: {goal}\n\n"
        f"Available capabilities (reads = param names, writes = result fields):\n{cap_desc}\n\n"
        "Put ALL literal values (URLs, prompts, queries, etc.) in initial_state. "
        "Use input_map only to rename a prior node's output key to match the next cap's param name. "
        "Use output_map to extract specific fields from a cap's result dict into named state keys."
    )

    raw = ""
    try:
        import vera_agents as _va
        planner_agent = await _va.AGENT_REGISTRY.get_by_name("dag-planner")
        if planner_agent:
            result = await _va.AGENT_RUNNER.run(planner_agent, prompt)
            raw = result.get("text", "")
            if raw:
                log.debug("dag-planner agent produced %d chars", len(raw))
    except Exception as e:
        log.debug("dag-planner agent unavailable, falling back: %s", e)

    if not raw:
        log.debug("dag-planner: falling back to direct ollama_generate")
        raw = await ollama_generate(prompt, system=_PLANNER_SYSTEM, prefer_gpu=True)

    if not raw:
        return {"error": "LLM empty", "dag": [], "initial_state": {}}

    plan = _extract_plan_local(raw)
    if not plan:
        return {"error": "Could not parse DAG from response",
                "raw": raw[:400], "dag": [], "initial_state": {}}

    # Validate cap names, self-correct
    unknown = [
        node[0] for node in _o._flatten_dag(plan.get("dag", []))
        if node[0] not in CAPABILITY_REGISTRY
    ]
    if unknown:
        log.warning("plan_dag: unknown caps %s — retrying", unknown)
        _retry_system = (
            "You are a Vera DAG planner. Only use capability names from the list. "
            "Respond with a single ```json block containing {dag, initial_state, rationale}."
        )
        fix_prompt = (
            f"Goal: {goal}\n\n"
            f"These capability names are INVALID (not registered): {unknown}\n"
            f"Available capabilities:\n{cap_desc}"
        )
        raw2  = await ollama_generate(fix_prompt, system=_retry_system, prefer_gpu=True)
        plan2 = _extract_plan_local(raw2 or "")
        if plan2:
            unk2 = [n[0] for n in _o._flatten_dag(plan2.get("dag", []))
                    if n[0] not in CAPABILITY_REGISTRY]
            if len(unk2) < len(unknown):
                plan = plan2; unknown = unk2
        if unknown:
            plan["warnings"] = [f"Unknown cap: {u}" for u in unknown]

    plan.setdefault("initial_state", {})
    plan.setdefault("rationale", "")

    plan = await _repair_plan(goal, plan, cap_names)
    return plan


async def _repair_plan(goal: str, plan: dict, cap_names: list) -> dict:
    """
    Deterministic post-planning repair pass.

    Problems fixed:
    A) CONDITION elements that aren't "CONDITION:key" strings → stripped,
       input_map / output_map dicts at position [2] also stripped cleanly
    B) Required params with no reachable state value → filled via LLM call.
       Accounts for explicit input_map: if input_map[param] points to an
       available key, the param is considered covered.
    C) Output key shadows a required param name → output key renamed
    """
    import Vera.Orchestration.capability_orchestration as _o

    dag   = plan.get("dag", [])
    state = dict(plan.get("initial_state", {}))

    if not dag:
        return plan

    # ── A) Fix malformed CONDITION elements ──────────────────────────────────
    repaired_dag = []
    for node in dag:
        if isinstance(node[0], list):
            repaired_dag.append([_fix_node(n) for n in node])
        else:
            repaired_dag.append(_fix_node(node))
    dag = repaired_dag

    # ── B) Find required params with no reachable value ───────────────────────
    available_keys = set(state.keys())
    missing_params: list = []   # (cap_name, param_name, param_type)

    for node in _o._flatten_dag(dag):
        cap_name   = node[0]
        out_key    = node[1] if len(node) > 1 else ""
        input_map  = node[3] if len(node) > 3 else None
        output_map = node[4] if len(node) > 4 else None
        cap        = CAPABILITY_REGISTRY.get(cap_name, {})
        schema     = cap.get("schema", {})
        props      = schema.get("properties", {})
        required   = set(schema.get("required", []))
        imap       = input_map or {}

        for param, pdef in props.items():
            if param == "trace_id":
                continue
            if param not in required:
                continue
            # Covered by input_map pointing to a non-null value?
            if param in imap:
                val = imap[param]
                # null in input_map means "absent" — not covered
                if val is not None and (not isinstance(val, str) or val in available_keys or val):
                    continue
            # Covered by same-name state key?
            if param in available_keys:
                continue
            missing_params.append((cap_name, param, pdef.get("type", "string")))

        # output_key may be None (LLM wrote null) — only add if truthy
        if out_key:
            available_keys.add(out_key)
        # output_map targets become available regardless of output_key
        if output_map:
            for sk, rf in output_map.items():
                if sk and rf is not None:  # skip null values in output_map
                    available_keys.add(sk)

    # ── C) Output key shadows a required param → rename ───────────────────────
    for i, node in enumerate(dag):
        if isinstance(node[0], list):
            continue
        cap_name = node[0]
        out_key  = node[1] if len(node) > 1 else ""
        cap      = CAPABILITY_REGISTRY.get(cap_name, {})
        required = set(cap.get("schema", {}).get("required", []))
        if out_key and out_key in required:
            new_key = cap_name.split(".")[-1] + "_out"
            log.info("repair: renaming output key '%s' → '%s' for %s", out_key, new_key, cap_name)
            dag[i] = [cap_name, new_key] + list(node[2:])
            for j in range(i + 1, len(dag)):
                n = dag[j]
                if len(n) > 2 and isinstance(n[2], str) and n[2] == f"CONDITION:{out_key}":
                    dag[j] = [n[0], n[1], f"CONDITION:{new_key}"] + list(n[3:])

    # ── Ask LLM to fill missing initial_state values ─────────────────────────
    if missing_params:
        seen, unique_missing = set(), []
        for item in missing_params:
            if item[1] not in seen:
                seen.add(item[1]); unique_missing.append(item)

        missing_list = "\n".join(
            f"  - {pname} ({ptype}) for capability {cname}"
            for cname, pname, ptype in unique_missing
            if pname not in state
        )

        if missing_list:
            fill_prompt = (
                f"The user's goal: {goal}\n\n"
                f"A DAG has been planned but the following required input values "
                f"are missing from initial_state:\n{missing_list}\n\n"
                f"Provide ONLY a JSON object with appropriate values for these "
                f"parameters based on the user's goal.\n"
                f"Be specific and use the actual values from the goal "
                f"(e.g. if goal mentions 'boejaker.com', use that exact hostname).\n"
                f"Return ONLY the JSON object, nothing else."
            )

            raw_fill = await ollama_generate(
                fill_prompt,
                system="Return only a JSON object of parameter name → value pairs. No prose.",
                json_mode=True,
                prefer_gpu=False,
            )

            if raw_fill:
                try:
                    filled = json.loads(raw_fill)
                    if isinstance(filled, dict):
                        for k, v in filled.items():
                            if k not in state:
                                state[k] = v
                                log.info("repair: filled initial_state[%s] = %r", k, v)
                except Exception:
                    m = re.search(r'\{[^{}]+\}', raw_fill)
                    if m:
                        try:
                            filled = json.loads(m.group())
                            for k, v in filled.items():
                                if k not in state:
                                    state[k] = v
                        except Exception:
                            pass

    plan["dag"]           = dag
    plan["initial_state"] = state
    return plan


def _fix_node(node: list) -> list:
    """
    Fix a single DAG node — strip malformed CONDITION elements while
    preserving input_map (position [3]) and output_map (position [4]).
    """
    if len(node) < 2:
        return node
    cap_name, out_key = node[0], node[1]
    input_map  = node[3] if len(node) > 3 else None
    output_map = node[4] if len(node) > 4 else None

    if len(node) > 2:
        cond = node[2]
        if isinstance(cond, str) and re.match(r'^CONDITION:[a-zA-Z_][a-zA-Z0-9_]*$', cond):
            # Valid condition — rebuild preserving all elements
            result = [cap_name, out_key, cond]
            if input_map is not None or output_map is not None:
                result.append(input_map)
            if output_map is not None:
                result.append(output_map)
            return result
        else:
            # Malformed CONDITION (a dict, a non-matching string, etc.) — strip it
            log.info("repair: stripped malformed CONDITION %r from node %s", cond, cap_name)
            result = [cap_name, out_key]
            if input_map is not None or output_map is not None:
                result.append(None)   # condition slot → null
                result.append(input_map)
            if output_map is not None:
                result.append(output_map)
            return result
    return list(node)


def _extract_plan_local(text: str) -> Optional[dict]:
    """Extract a plan dict from potentially chatty LLM text."""
    for block in re.findall(r'```(?:json)?\s*([\s\S]*?)```', text):
        try:
            p = json.loads(block.strip())
            if isinstance(p, dict) and isinstance(p.get("dag"), list):
                return p
        except Exception:
            pass
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            p = json.loads(m.group())
            if isinstance(p, dict) and isinstance(p.get("dag"), list):
                return p
        except Exception:
            pass
    try:
        p = json.loads(text.strip())
        if isinstance(p, dict) and isinstance(p.get("dag"), list):
            return p
    except Exception:
        pass
    return None


# Monkey-patch the orchestrator's plan_dag with the index-aware version
import Vera.Orchestration.capability_orchestration as _orch_mod
_orch_mod.plan_dag = _plan_dag_with_index
log.info("plan_dag patched to use CapabilityIndex")


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    CAP_INDEX.build()
    await DAG_STORE._pg_init()
    asyncio.create_task(CAP_INDEX.start_embedding())
    asyncio.create_task(_DAG_CAP_REGISTRY.restore_from_redis())
    log.info("vera_dag_store ready — %d caps indexed", len(CAP_INDEX._index))


schedule(_startup, interval=999999, name="dag_store_startup")
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass