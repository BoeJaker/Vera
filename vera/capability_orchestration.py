"""
vera_orchestrator.py  –  v3
============================
Single-decorator architecture: @capability is the ONLY registration primitive.
Every @capability can optionally declare an HTTP route — which replaces the
need for @APP.get / @APP.post entirely.  All routes are automatically:

  • Callable via MCP  (/mcp/tools  /mcp/call  /ws/mcp)
  • Callable via REST  (auto-mounted at the declared path)
  • Observable via Redis event stream
  • Distributed via Redis Streams when mode="distributed"
  • Retried on failure
  • Schema-reflected for the harness UI

Capabilities that declare http_method + http_path are additionally mounted
as standard REST endpoints with full OpenAPI docs.

Endpoints:
  Ollama  : http://192.168.0.250:11435  (GPU)
             http://192.168.0.246:11435  (CPU A)
             http://192.168.0.247:11435  (CPU B)
  Redis   : redis://<BACKEND_HOST>:6379
  Postgres: postgresql://postgres:password@<BACKEND_HOST>:5432/llm
"""

import asyncio, contextvars, copy, functools, hashlib, inspect, json, logging, os, sys, time, uuid
import logging.handlers  # noqa: E402  (submodule; `import logging` alone won't load it)
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Union

# HF tokenizers spins up Rust/rayon worker threads on first use; on a server that
# forks subprocesses (docker/exec spawns) that both wastes CPU competing with the
# event loop and risks a fork-after-parallelism deadlock (the "process just got
# forked, after parallelism has already been used" warning seen in the logs).
# Disable it process-wide before anything can import/use tokenizers; override with
# an explicit env if ever needed.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import httpx
import uvicorn

# One SSL context shared by every ad-hoc AsyncClient below (verify=_SSL_CTX).
# Without it each client construction loads + parses the whole certifi CA
# bundle — pure CPU on the event loop, and the health loop builds one client
# per node per heartbeat, which alone shows up as >1s loop stalls.
import ssl as _ssl
try:
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX = _ssl.create_default_context()
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, Response

from pathlib import Path 
_HERE = Path(__file__).parent

# ── Optional backends ─────────────────────────────────────────────────────────
try:
    import redis.asyncio as aioredis; HAS_REDIS = True
except ImportError:
    aioredis = None; HAS_REDIS = False

try:
    import asyncpg; HAS_PG = True
except ImportError:
    asyncpg = None; HAS_PG = False

try:
    import chromadb; HAS_CHROMA = True
except ImportError:
    chromadb = None; HAS_CHROMA = False

try:
    from neo4j import AsyncGraphDatabase; HAS_NEO = True
    _NEO_IMPORT_ERR = ""
except ImportError as _e:
    AsyncGraphDatabase = None; HAS_NEO = False
    _NEO_IMPORT_ERR = str(_e)

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("vera.orch")

# Set by long-running callers (e.g. the agentic loop) so the ollama.* events
# emitted during generation can be scoped to that run's session and surfaced in
# its UI (which Ollama node served the request). Defaults to "" (unscoped).
OLLAMA_EVENT_SESSION: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "ollama_event_session", default="")

# ── Background-work marking + interactive priority ───────────────────────────
# BACKGROUND_LLM is set (to a short label like "dream:<cycle>" / "v8:<pid>" /
# "fabric:ingest") by autonomous drivers — the dream scheduler, the V8 program
# orchestrator, fabric post-ingest NLP — for the duration of their run. Every
# ollama_generate issued inside that context is then treated as BACKGROUND
# work: while a human has been interactive recently (see note_interactive),
# background requests are demoted off the GPU pool onto CPU nodes so the GPU
# stays free for the person actually using the system. contextvars propagate
# through create_task, so one set() at the driver's entry covers its whole tree.
BACKGROUND_LLM: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "vera_background_llm", default="")

# Which KIND of caller is driving the current request, for the duration of
# one /mcp/call — set only by _make_mcp_call_handler when the request body
# carries an explicit caller_kind (currently just "mcp", sent by
# vera_mcp_bridge.py, the shim Claude Code launches). /mcp/call is ALSO used
# by the browser chat UI to execute capabilities, which never sets this —
# so the honest default (empty here) is "some UI/browser caller", never
# guessed further. Read by evolve.* run-recording to populate a real
# triggered_by field (claude_code / autonomous via BACKGROUND_LLM / user).
CALLER_KIND: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "vera_caller_kind", default="")

# The capability currently being served over HTTP (set by the REST handler
# factories). Lets a cap distinguish a DIRECT human/UI HTTP invocation of
# itself from an internal (agentic / pipeline) call — used e.g. by the fabric
# LLM-NLP gate, where only the human UI toggle may enable LLM extraction.
CURRENT_HTTP_CAP: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "vera_current_http_cap", default="")

# Last time a human was interactive (chat generation, or an explicit UI ping).
LAST_INTERACTIVE_TS: float = 0.0

# Runtime config, hydrated from Redis (KEY_OLLAMA_INTERACTIVE) on startup and
# editable via ollama.interactive.set / the routing panel.
#   enabled              — demote background LLM work off GPUs while human active
#   window_s             — how long after the last interaction the human counts as active
#   defer_background     — additionally SKIP STARTING new background runs
#                          (dream scheduler fires, v8 program ticks) in that window
#   background_always_cpu— ALWAYS keep background LLM work (dream/v8/fabric NLP)
#                          off the GPU, whether or not a human is active. The
#                          deterministic control: the GPU is reserved for
#                          foreground/chat work, full stop.
INTERACTIVE_PRIORITY: Dict[str, Any] = {
    "enabled": True, "window_s": 180, "defer_background": True,
    "background_always_cpu": False,
}
KEY_OLLAMA_INTERACTIVE = "vera:ollama:interactive_priority"

# Job types that mark the human as interactive when NOT inside a background ctx.
_INTERACTIVE_JOB_TYPES = {"chat", "vision", "code"}


def note_interactive(source: str = "") -> None:
    """Stamp 'a human is actively using the system now'. Called from the chat
    generation path automatically and from POST /activity/ping by UIs."""
    global LAST_INTERACTIVE_TS
    LAST_INTERACTIVE_TS = time.time()


def interactive_recent(window_s: Optional[float] = None) -> bool:
    """True when a human interacted within the configured window."""
    w = float(window_s if window_s is not None
              else INTERACTIVE_PRIORITY.get("window_s", 180) or 180)
    return (time.time() - LAST_INTERACTIVE_TS) < w


def defer_background_now() -> bool:
    """True when autonomous drivers should HOLD OFF starting new background
    runs because a human is active (interactive-priority defer switch)."""
    return bool(INTERACTIVE_PRIORITY.get("enabled", True)
                and INTERACTIVE_PRIORITY.get("defer_background", True)
                and interactive_recent())


if not HAS_NEO:
    log.warning("neo4j driver NOT installed (%s) — Neo4j backend will stay down. "
                "Fix: pip install 'neo4j>=5.19,<6.0'", _NEO_IMPORT_ERR)

# ── Config (from central cfg — single source of truth)
from Vera.vera.config import cfg

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL    = cfg.REDIS_URL
POSTGRES_URL = cfg.POSTGRES_URL
OLLAMA_MODEL       = cfg.OLLAMA_MODEL
OLLAMA_EMBED_URL   = cfg.OLLAMA_EMBED_URL
OLLAMA_EMBED_MODEL = cfg.OLLAMA_EMBED_MODEL
EMBED_PROVIDER     = getattr(cfg, "EMBED_PROVIDER", "ollama")
# Generation HTTP timeout (seconds) and model keep-alive. The old hardcoded 120s
# ceiling silently failed slow planner-scale generations (a cold 9b doing
# structured JSON over a large catalog routinely exceeds 120s → ReadTimeout →
# empty output → "Planning failed"). Make it configurable and default generously.
# 300s proved too tight in practice: big-context jobs queued behind a node's
# semaphore on slow CPU nodes routinely blew through it, mass-failing calls
# ("stale_timeout" storms in the Jobs panel). Default 900s; override via env.
# keep_alive keeps the model resident so the next call skips cold-load latency.
OLLAMA_GEN_TIMEOUT = float(os.environ.get("OLLAMA_GEN_TIMEOUT", "900"))

# ── Background vs foreground work ────────────────────────────────────────────
# BACKGROUND jobs are self-scheduled thinking that nobody is waiting on: they
# recur on a timer, so skipping one costs nothing. FOREGROUND work (a chat turn,
# an agentic-loop step) has someone or something blocked on it.
#
# The distinction exists because ollama serialises per node. A background job
# that fails over onto a busy node does not just wait — it queues AHEAD of / with
# foreground work and starves it. Measured live: the dream director's 12.7k-char
# think timed out on a CPU node, failed over onto the GPU serving an agentic
# loop, and the loop's 12-second call died at the 900s ceiling.
# Override with VERA_BACKGROUND_JOB_TYPES (csv) — prefixes match.
_BACKGROUND_JOB_TYPES = tuple(
    j.strip() for j in os.environ.get(
        "VERA_BACKGROUND_JOB_TYPES",
        "dream_director,dream_,director_,idle_,journal_,reflect").split(",")
    if j.strip())


def _is_background_job(job_type: str) -> bool:
    jt = (job_type or "").strip().lower()
    return bool(jt) and jt.startswith(_BACKGROUND_JOB_TYPES)
# Embedding HTTP timeout. Embeds are quick to COMPUTE but Ollama serialises
# requests per node, so an embed sent while a big generation runs waits
# server-side inside this budget — the old 30s default failed embeds that were
# merely queued behind a large job. Waiting is not failure: default generously.
OLLAMA_EMBED_TIMEOUT = float(os.environ.get("OLLAMA_EMBED_TIMEOUT", "300"))
# Embed de-duplication (shared by EVERY caller of ollama_embed — fabric _embed,
# memory embed_text, etc). The same query text was being embedded 2×+ by
# concurrent callers and retries; each duplicate is a full /api/embed on the
# serialised embed node, jamming it. _EMBED_INFLIGHT collapses concurrent
# identical embeds onto one request; _EMBED_RESULT_CACHE reuses a recent vector.
# Keyed by (model, normalize, provider, text) since those change the vector.
_EMBED_INFLIGHT: Dict[str, "asyncio.Future"] = {}
_EMBED_RESULT_CACHE: "Dict[str, tuple]" = {}   # key → (vec, monotonic_ts)
_EMBED_CACHE_MAX = int(os.environ.get("OLLAMA_EMBED_CACHE_MAX", "512") or 512)
_EMBED_CACHE_TTL = float(os.environ.get("OLLAMA_EMBED_CACHE_TTL", "120") or 120)
# Moderate keep-alive: keeps a model warm across a loop's sequential generations
# (and short bursts of iterative use) without holding VRAM as long as a big value
# would on a shared GPU. Fully overridable via env.
OLLAMA_KEEP_ALIVE  = os.environ.get("OLLAMA_KEEP_ALIVE", "10m")

TASK_STREAM   = "vera:tasks"
RESULT_STREAM = "vera:results"
EVENT_STREAM  = "vera:events"
GROUP_WORKERS = "workers"
GROUP_RESULTS = "orchestrator"

# ── Runtime state ─────────────────────────────────────────────────────────────
CAPABILITY_REGISTRY: Dict[str, dict]           = {}
WORKER_REGISTRY:     Dict[str, dict]           = {}
STREAM_SUBS:         Dict[str, List[Callable]] = {}
WS_CONNECTIONS:      List[tuple]               = []
SCHEDULED_TASKS:     List[dict]                = []
PENDING_RESULTS:     Dict[str, asyncio.Future] = {}
RUNNING_TASKS:       Dict[str, asyncio.Task]   = {}   # task_id -> in-flight cap Task (for cancel)
CANCELLED_TASKS:     set                       = set()  # task_ids cancelled while still queued
MCP_SERVERS:         Dict[str, str]            = {}
LOADED_MODULES:      List[dict]                = []   # [{name, path, caps, status}]

# Cross-host job cancellation: cluster.job.stop publishes a task_id on this
# pub/sub channel (every worker process subscribes via cancel_listener) and adds
# it to the cancelled set so a not-yet-started queued task is skipped on pickup.
REDIS_CANCEL_CHANNEL = "vera:cancel"
REDIS_CANCEL_SET     = "vera:cancelled"

# Shutdown hooks — modules append a zero-arg coroutine to have it awaited as
# Vera stops (see `lifespan`). Needed because @app.on_event("shutdown") is
# ignored when a lifespan handler is used, so module-level teardown had no
# working registration point at all.
SHUTDOWN_HOOKS: List[Any] = []


def register_shutdown_hook(fn) -> None:
    """Register a coroutine to run during Vera shutdown. Idempotent."""
    if fn not in SHUTDOWN_HOOKS:
        SHUTDOWN_HOOKS.append(fn)


# UI panel registry — modules call register_ui() to inject harness panels.
# Lives here (not in vera_capabilities) so /ui/panels always exists.
UI_PANELS: Dict[str, dict] = {}

def register_ui(panel_id: str, label: str, icon: str, html: str, js: str = "",
                ui_caps: List[str] = None,
                mode: str = "inject",
                tab_order: int = 100,
                specialist_agent: str = "",
                specialist_loop_profile: str = "",
                specialist_context_cap: str = ""):
    """Register a built-in UI panel.

    mode:
      "inject"  — panel HTML is injected into the Media sub-switcher (default)
      "tab"     — panel gets its own top-level tab in the harness, auto-created on load
      "mount"   — panel is injected into a pre-declared mount point (skills, ontologies, etc.)
      "element" — registered + listed (dashboard widget loader, custom top-level
                  tabs) but NOT auto-rendered anywhere; for panels that live
                  inside another panel's sub-tabs (e.g. memory graph in Fabric)

    tab_order: integer sort key for auto-tabs (lower = further left); default 100.
    ui_caps: list of capability names this panel uses.
    specialist_agent / specialist_loop_profile: optional declarative binding —
    the DEFAULT_AGENTS persona (single-turn) / LOOP_PROFILES preset (autonomous)
    this panel is "expert" in, generalizing the markets studio's hardcoded COP
    widget (agent="quant-strategist", loop_profile="markets-quant") into a
    per-panel declaration ANY panel can make. Read by ui.panel.specialist and
    by <vera-panel-copilot> (panel_copilot_element.js) to embed a scoped
    copilot without each panel reimplementing the chat/loop wiring, and by
    chat's panel-dispatch defer-to-specialist mode (opt-in) to route a panel-
    scoped request through this specialist instead of generic context
    injection. Blank = no binding, panel behaves exactly as before.
    specialist_context_cap: optional capability name that returns a fresh,
    panel-relevant context snapshot to hand the specialist BEFORE it answers
    — not just the right persona, the right live data (e.g. markets: an
    up-to-date scan of active portfolios/assets; business: the full store
    list, zoomable to one active store). The capability is called with
    whatever the caller's own panel-state/entity params supply (e.g. an
    entity_id extracted from the open panel's live state) and its result is
    passed as agent.consult's `context` argument — no new execution engine,
    just real data prepended to the same consult/loop call. Blank = no
    context injection, specialist answers from persona alone as before.
    """
    UI_PANELS[panel_id] = {
        "id":        panel_id,
        "label":     label,
        "icon":      icon,
        "html":      html,
        "js":        js,
        "ui_caps":   ui_caps or [],
        "mode":      mode,
        "tab_order": tab_order,
        "specialist_agent":        specialist_agent,
        "specialist_loop_profile": specialist_loop_profile,
        "specialist_context_cap":  specialist_context_cap,
    }

REDIS = PG_POOL = CHROMA = NEO = None

# COORD_REDIS — a SHARED coordination Redis handle used for cross-process
# primitives that must be visible across prod AND every dev sandbox container
# (currently: the Ollama GPU gate / "one big queue"). Dev containers run their
# DATA on an isolated Redis DB, but coordination MUST be shared, so this always
# targets the same Redis SERVER as REDIS_URL forced onto VERA_COORD_REDIS_DB
# (default 0 = prod's shared DB). When it equals the data DB (prod's own case)
# COORD_REDIS is just REDIS reused. Keys are namespaced 'vera:ollama:gate:*' so
# they never collide with data on a shared DB.
COORD_REDIS = None
COORD_REDIS_DB = int(os.environ.get("VERA_COORD_REDIS_DB", "0") or 0)

# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CLUSTER
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_INSTANCES: Dict[str, dict] = {
    "gpu-250": {"url":"http://192.168.0.250:11435","label":"GPU Node","has_gpu":True,"enabled":True,
                "priority":0,"status":"unknown","latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0},
    "cpu-246": {"url":"http://192.168.0.246:11435","label":"CPU Node A","has_gpu":False,"enabled":True,
                "priority":1,"status":"unknown","latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0},
    "cpu-247": {"url":"http://192.168.0.247:11435","label":"CPU Node B","has_gpu":False,"enabled":True,
                "priority":2,"status":"unknown","latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0},
}

# ─────────────────────────────────────────────────────────────────────────────
# CLUSTER ROUTING — job-type aware, profile-based, persisted to Redis
# ─────────────────────────────────────────────────────────────────────────────
# A "job type" describes the KIND of work so the router can send it to suitable
# nodes (e.g. embeddings are light and should stay on CPU; chat/code prefer GPU).
# Callers may pass job_type explicitly; otherwise it's inferred from the calling
# capability/model (see _infer_job_type). Rules live in named PROFILES; the
# active profile's rule for a job type overrides the built-in DEFAULT below.
OLLAMA_JOB_TYPES: List[str] = [
    "embedding", "naming", "summarize", "chat", "dream", "vision", "code", "default",
    # Named research roles (PoC for cap-declared routing styles): the research
    # subsystem runs three distinct LLM workloads with different needs —
    #   planner: strategic, big-context, benefits from GPU;
    #   reader:  bulk page digestion/summarising, fine on CPU nodes;
    #   writer:  long-form report generation, GPU + big context.
    "research_planner", "research_reader", "research_writer",
    # The dream DIRECTOR (ambient thought orchestrator) runs continuously on
    # CPU nodes — it must never contend with user-facing GPU work.
    "dream_director",
    # Media services served by the GPU inference server(s) (edge/GPU_inference.py):
    # routed across MEDIA_INSTANCES by resolve_media(), not pick_instance().
    "stt", "tts", "imagegen",
]

def _rule(job_type: str, *, prefer_gpu: bool = False, deny_gpu: bool = False,
          pin: str = "", allow: Optional[List[str]] = None,
          deny: Optional[List[str]] = None, model: str = "",
          avoid_embed: bool = False) -> dict:
    # `model` (optional) pins a specific model for this job type — lets light
    # work (naming, summarisation) run a smaller/faster model than chat/code.
    # `avoid_embed` steers this job type OFF whichever node currently serves
    # embeddings (resolved dynamically at pick time — the embed node can be
    # re-pinned at runtime), so long generations don't starve the embed path.
    return {"job_type": job_type, "prefer_gpu": prefer_gpu, "deny_gpu": deny_gpu,
            "pin": pin, "allow": list(allow or []), "deny": list(deny or []),
            "model": model or "", "avoid_embed": bool(avoid_embed)}

# Built-in default routing — always shown in the UI as the baseline. Embeddings
# are CPU-only (light, should never tie up a GPU); generative work prefers GPU.
# `naming` (chat titles + simple/utility LLM ops) is CPU-only by default so it
# never ties up a GPU — pin it to a specific CPU node and/or a lighter model in
# the Workers & Ollama tab's routing editor.
DEFAULT_ROUTING_RULES: Dict[str, dict] = {
    "embedding": _rule("embedding", deny_gpu=True),
    # naming + summarize are light utility LLM ops that run INLINE in latency-
    # sensitive paths (chat title generation; history compaction before a reply).
    # Keep them off the embedding node (avoid_embed) so they land on an idle CPU
    # node instead of queueing behind embedding traffic — a summarize stuck
    # behind embeds on the same node stalled every message in a long chat.
    "naming":    _rule("naming",    deny_gpu=True, avoid_embed=True),
    "summarize": _rule("summarize", deny_gpu=True, avoid_embed=True),
    "chat":      _rule("chat",      prefer_gpu=True),
    "dream":     _rule("dream",     prefer_gpu=True),
    "vision":    _rule("vision",    prefer_gpu=True),
    "code":      _rule("code",      prefer_gpu=True),
    # Baseline for untyped work: least-busy with GPU preference (GPU-first).
    # pick_instance always load-balances; any explicit rule/profile that says
    # otherwise (deny_gpu, pin, allow/deny) overrides this default.
    "default":   _rule("default",   prefer_gpu=True),
    # Research role defaults (see OLLAMA_JOB_TYPES note).
    "research_planner": _rule("research_planner", prefer_gpu=True),
    "research_reader":  _rule("research_reader",  deny_gpu=True),
    "research_writer":  _rule("research_writer",  prefer_gpu=True),
    # Ambient thought orchestrator — CPU only, so it can run during user
    # activity without touching the GPU pool. avoid_embed keeps its long
    # generations off the embedding node: a multi-minute director thought
    # holding that node's single generation slot starved every embed call
    # (and vice versa — director thoughts queued behind embedding bursts).
    "dream_director":   _rule("dream_director",   deny_gpu=True, avoid_embed=True),
    # Media services — GPU-first across the media nodes that actually have the
    # service installed (resolve_media checks each node's /health service list).
    "stt":      _rule("stt",      prefer_gpu=True),
    "tts":      _rule("tts",      prefer_gpu=True),
    "imagegen": _rule("imagegen", prefer_gpu=True),
}

# In-memory routing state (hydrated from Redis on startup, see
# _load_ollama_persistence). Profiles map name -> {label, rules:{job_type:rule}}.
ROUTING: Dict[str, Any] = {
    "active_profile": "default",
    "profiles": {
        "default": {"label": "Default", "rules": {}},   # {} -> inherit DEFAULT_ROUTING_RULES
    },
}

KEY_OLLAMA_ROUTING = "vera:ollama:routing"   # JSON: {active_profile, profiles}
KEY_OLLAMA_NODES   = "vera:ollama:nodes"     # JSON: {iid: {enabled,priority,label,url,has_gpu,num_ctx}}
KEY_OLLAMA_EMBED   = "vera:ollama:embed"     # JSON: {embed_model, prefer_gpu, pinned_instance}


def _match_glob(iid: str, pattern: str) -> bool:
    """Exact id match, or simple 'prefix-*' / '*' glob."""
    if pattern == "*" or pattern == iid:
        return True
    if pattern.endswith("*") and iid.startswith(pattern[:-1]):
        return True
    return False


def _resolve_rule(job_type: Optional[str]) -> dict:
    """The effective rule for a job type: active-profile override, else default."""
    jt = (job_type or "default").strip() or "default"
    prof = ROUTING.get("profiles", {}).get(ROUTING.get("active_profile", "default"), {})
    rules = (prof.get("rules") or {})
    if jt in rules and rules[jt]:
        return rules[jt]
    if jt in DEFAULT_ROUTING_RULES:
        return DEFAULT_ROUTING_RULES[jt]
    return DEFAULT_ROUTING_RULES["default"]


_JOB_TYPE_CALLER_HINTS = [
    # (substring in cap_name/caller_func/file, job_type)
    ("embed", "embedding"), ("autoname", "naming"), ("auto_name", "naming"),
    ("title", "naming"), ("dream", "dream"), ("summariz", "summarize"),
    ("vision", "vision"), ("code", "code"), ("chat", "chat"), ("agent", "chat"),
]

def _infer_job_type(caller: Optional[dict], model: Optional[str]) -> str:
    """Best-effort job type from the calling cap/func/file and model name."""
    mdl = (model or "").lower()
    if "embed" in mdl:
        return "embedding"
    hay = ""
    if caller:
        hay = " ".join(str(caller.get(k, "")) for k in
                       ("cap_name", "caller_func", "caller_file", "caller_module")).lower()
    for sub, jt in _JOB_TYPE_CALLER_HINTS:
        if sub in hay:
            return jt
    return "default"


# ─────────────────────────────────────────────────────────────────────────────
# PER-CAPABILITY / GROUP ROUTING — routing rules keyed by cap name or glob
# ─────────────────────────────────────────────────────────────────────────────
# The job-type profiles above route by KIND of work; these rules route by WHO is
# asking — a specific capability ("fabric.summarize") or a whole group
# ("research.*"). Two layers:
#   • USER rules      — configured in the routing UI, persisted, always win.
#   • DECLARED rules  — registered in code by subsystems that know what routing
#     style suits their operations (register_cap_routing). Shown in the UI as
#     the baseline; a USER rule for the same pattern overrides it.
# A rule may force a job_type, apply node filters (pin/allow/deny/prefer_gpu/
# deny_gpu), pin a model, and ESCALATE on prompt length: when the prompt exceeds
# `escalate_chars`, the overrides in `escalate` are merged in (e.g. big prompts
# jump to the GPU node / a larger-context model).
KEY_OLLAMA_CAP_ROUTING = "vera:ollama:cap_routing"   # JSON: {pattern: rule}
KEY_OLLAMA_ROUTE_STATS = "vera:ollama:route_stats"   # JSON: {stat_key: stats}

CAP_ROUTING_USER: Dict[str, dict] = {}
CAP_ROUTING_DECLARED: Dict[str, dict] = {}


def _cap_rule(pattern: str, *, job_type: str = "", label: str = "",
              declared_by: str = "", prefer_gpu: bool = False,
              deny_gpu: bool = False, pin: str = "", allow: Optional[List[str]] = None,
              deny: Optional[List[str]] = None, model: str = "",
              escalate_chars: int = 0, escalate: Optional[dict] = None,
              options: Optional[dict] = None) -> dict:
    return {"pattern": pattern, "job_type": job_type or "", "label": label or pattern,
            "declared_by": declared_by or "", "prefer_gpu": bool(prefer_gpu),
            "deny_gpu": bool(deny_gpu), "pin": pin or "", "allow": list(allow or []),
            "deny": list(deny or []), "model": model or "",
            "escalate_chars": int(escalate_chars or 0),
            "escalate": dict(escalate or {}),
            # Sampling options (temperature/top_p/num_ctx/…) a rule may pin for
            # its job/role — merged under the caller's explicit options at send.
            "options": dict(options or {})}


def register_cap_routing(pattern: str, **kw) -> dict:
    """Subsystems DECLARE the routing style that best suits a capability or
    group (e.g. the researcher registering its planner/reader/writer roles).
    Declared rules are live immediately, visible in the routing UI, and can be
    overridden there (a USER rule with the same pattern wins)."""
    rule = _cap_rule(pattern, **kw)
    CAP_ROUTING_DECLARED[pattern] = rule
    return rule


def _resolve_cap_routing(cap_name: str) -> Optional[dict]:
    """Longest-pattern match for a caller cap: USER rules first, then DECLARED."""
    if not cap_name:
        return None
    best: Optional[dict] = None
    best_len = -1
    for layer in (CAP_ROUTING_USER, CAP_ROUTING_DECLARED):
        for pat, rule in layer.items():
            if _match_glob(cap_name, pat) and len(pat) > best_len:
                best, best_len = rule, len(pat)
        if best is not None:
            return best      # user layer wins outright when any pattern matched
    return best


# ── Declared routing styles — PoC: the researcher's three named roles ────────
# The research subsystem's LLM traffic falls into three distinct workloads, so
# it declares a routing style per role. These are live defaults, visible and
# overridable in the routing UI (a USER rule on the same pattern wins).
register_cap_routing("research.plan*", job_type="research_planner",
                     label="Researcher · planner", declared_by="research",
                     prefer_gpu=True)
register_cap_routing("research.write*", job_type="research_writer",
                     label="Researcher · writer", declared_by="research",
                     prefer_gpu=True)
register_cap_routing("research.report*", job_type="research_writer",
                     label="Researcher · report writer", declared_by="research",
                     prefer_gpu=True)
# Bulk reading stays on CPU nodes, but big digests escalate to GPU.
register_cap_routing("research.*", job_type="research_reader",
                     label="Researcher · reader", declared_by="research",
                     deny_gpu=True, escalate_chars=12000,
                     escalate={"deny_gpu": False, "prefer_gpu": True})


# ─────────────────────────────────────────────────────────────────────────────
# ROLE-BASED ROUTING PROFILES — named roles (thinker / writer / verifier / …)
# ─────────────────────────────────────────────────────────────────────────────
# Subsystems that run several distinct LLM personas (the researcher and the IDE
# both use a thinker/writer/verifier trio) REGISTER a routing profile mapping
# each role to a routing rule (job_type, prefer_gpu/deny_gpu, pin, allow/deny,
# model, length escalation). Callers then RESOLVE a role instead of picking an
# Ollama node themselves, so all their traffic flows through the cluster
# router. Two layers, same precedence model as per-cap rules:
#   • DECLARED — registered in code (register_routing_profile), live defaults.
#   • USER     — edited in the Model Routing page, persisted, wins per role.
KEY_OLLAMA_ROLE_PROFILES = "vera:ollama:role_profiles"   # JSON: {profile: {label, owner, roles}}

ROLE_PROFILES_DECLARED: Dict[str, dict] = {}
ROLE_PROFILES_USER: Dict[str, dict] = {}


def _role_rule(profile: str, role: str, r: Optional[dict] = None) -> dict:
    """Normalise a role's routing rule (same shape as a per-cap rule; the
    pattern slot carries 'profile/role' so shared merge/logging code works)."""
    r = r or {}
    rule = _cap_rule(f"{profile}/{role}",
                     job_type=r.get("job_type", "") or "",
                     label=r.get("label") or f"{profile} · {role}",
                     declared_by=r.get("declared_by", profile),
                     prefer_gpu=bool(r.get("prefer_gpu")),
                     deny_gpu=bool(r.get("deny_gpu")),
                     pin=r.get("pin", "") or "", allow=r.get("allow") or [],
                     deny=r.get("deny") or [], model=r.get("model", "") or "",
                     escalate_chars=int(r.get("escalate_chars") or 0),
                     escalate=r.get("escalate") or {},
                     options=r.get("options") or {})
    rule["role"] = role
    return rule


def register_routing_profile(name: str, *, label: str = "", owner: str = "",
                             roles: Optional[Dict[str, dict]] = None) -> dict:
    """Subsystems DECLARE a routing profile: a named set of roles, each with
    the routing rule that suits it (e.g. research → thinker/writer/verifier).
    The profile is live immediately, shown in the Model Routing page, and any
    role can be overridden there (a USER role for the same profile wins)."""
    prof = {"name": name, "label": label or name, "owner": owner or name,
            "roles": {r: _role_rule(name, r, spec)
                      for r, spec in (roles or {}).items()}}
    ROLE_PROFILES_DECLARED[name] = prof
    return prof


def _effective_role_profiles() -> Dict[str, dict]:
    """Merged view: declared profiles with USER role overrides applied on top;
    user-only profiles included as-is."""
    out: Dict[str, dict] = {}
    for name, prof in ROLE_PROFILES_DECLARED.items():
        merged = {"name": name, "label": prof.get("label", name),
                  "owner": prof.get("owner", ""),
                  "roles": dict(prof.get("roles", {}))}
        user = ROLE_PROFILES_USER.get(name) or {}
        for role, r in (user.get("roles") or {}).items():
            merged["roles"][role] = r
        out[name] = merged
    for name, prof in ROLE_PROFILES_USER.items():
        if name not in out:
            out[name] = {"name": name, "label": prof.get("label", name),
                         "owner": prof.get("owner", "user"),
                         "roles": dict(prof.get("roles") or {})}
    return out


def resolve_role_rule(profile: str, role: str) -> Optional[dict]:
    """Effective rule for a profile role: USER override, else DECLARED."""
    user = (ROLE_PROFILES_USER.get(profile) or {}).get("roles") or {}
    if role in user:
        return user[role]
    return ((ROLE_PROFILES_DECLARED.get(profile) or {}).get("roles") or {}).get(role)


def _merge_rule_over_base(rule: Optional[dict], base: dict,
                          prompt_chars: int = 0) -> dict:
    """Merge a cap/role rule over its job-type base rule, applying the rule's
    length-based escalation when prompt_chars crosses the threshold."""
    eff = dict(base or {})
    if not rule:
        return eff
    for k in ("prefer_gpu", "deny_gpu", "pin", "allow", "deny", "model"):
        v = rule.get(k)
        if v:
            eff[k] = v
    # Sampling options merge key-by-key: the rule's options override the base's;
    # the caller's explicit options still win later in ollama_generate.
    ropts = rule.get("options") or {}
    if ropts or base.get("options"):
        eff["options"] = {**(base.get("options") or {}), **ropts}
    esc_at = int(rule.get("escalate_chars") or 0)
    if esc_at > 0 and prompt_chars >= esc_at and rule.get("escalate"):
        for k, v in (rule.get("escalate") or {}).items():
            # Booleans apply even when False — an escalation must be able to
            # LIFT a base deny_gpu (e.g. reader jumps to GPU on big digests).
            if k in ("prefer_gpu", "deny_gpu"):
                eff[k] = bool(v)
            elif k in ("pin", "allow", "deny", "model") and v:
                eff[k] = v
            elif k == "options" and isinstance(v, dict):
                eff["options"] = {**(eff.get("options") or {}), **v}
    return eff


def resolve_role(profile: str, role: str, *, model: str = "",
                 prompt_chars: int = 0,
                 explain: Optional[dict] = None) -> Optional[dict]:
    """Resolve a profile role to a concrete Ollama node through the cluster
    router — the front door for subsystems that used to pick nodes themselves.
    Merges the role's rule over its job-type rule (plus length escalation),
    then runs pick_instance. Returns {instance_id, url, label, model, job_type,
    rule, reason} or None when no node is routable."""
    rule = resolve_role_rule(profile, role)
    jt = (rule or {}).get("job_type") or "default"
    eff = _merge_rule_over_base(rule, _resolve_rule(jt) or {}, prompt_chars)
    eff_model = model or eff.get("model") or ""
    exp = explain if explain is not None else {}
    chosen = pick_instance(model=eff_model or None, job_type=jt,
                           rule_override=eff, explain=exp)
    if not chosen:
        return None
    inst = OLLAMA_INSTANCES.get(chosen, {})
    return {"instance_id": chosen, "url": inst.get("url", ""),
            "label": inst.get("label", chosen),
            "has_gpu": bool(inst.get("has_gpu")),
            "model": eff_model or "", "job_type": jt, "profile": profile,
            "role": role, "rule": eff, "reason": exp.get("reason") or []}


async def _save_role_profiles() -> None:
    if not REDIS:
        return
    try:
        await REDIS.set(KEY_OLLAMA_ROLE_PROFILES, json.dumps(ROLE_PROFILES_USER))
    except Exception as e:
        log.warning("save role profiles: %s", e)


# ── Routing/request statistics — layout, models, tokens and time per request ──
# EMA per (model | instance | job_type): request count, elapsed, output tokens,
# tokens/sec and prompt size. Used to (a) show real throughput in the routing
# UI, (b) estimate how long a request will take on a node, which feeds the
# instance picker's tie-break. Persisted to Redis (debounced).
_ROUTE_STATS: Dict[str, dict] = {}
_ROUTE_STATS_DIRTY = {"n": 0}
_ROUTE_STATS_EMA = 0.3


def _route_stats_key(model: str, iid: str, job_type: str) -> str:
    return f"{model or '?'}|{iid or '?'}|{job_type or 'default'}"


def _route_stats_update(model: str, iid: str, job_type: str,
                        elapsed_s: float, tokens: int, prompt_chars: int) -> None:
    key = _route_stats_key(model, iid, job_type)
    s = _ROUTE_STATS.get(key)
    tps = (tokens / elapsed_s) if (elapsed_s and elapsed_s > 0 and tokens) else 0.0
    if not s:
        s = {"model": model, "instance": iid, "job_type": job_type, "n": 0,
             "ema_elapsed_s": elapsed_s, "ema_tokens": float(tokens or 0),
             "ema_tps": tps, "ema_prompt_chars": float(prompt_chars or 0)}
        _ROUTE_STATS[key] = s
    a = _ROUTE_STATS_EMA
    s["n"] += 1
    s["ema_elapsed_s"] = round((1 - a) * s["ema_elapsed_s"] + a * elapsed_s, 3)
    s["ema_tokens"] = round((1 - a) * s["ema_tokens"] + a * float(tokens or 0), 1)
    if tps > 0:
        s["ema_tps"] = round((1 - a) * (s["ema_tps"] or tps) + a * tps, 2)
    if prompt_chars:
        s["ema_prompt_chars"] = round((1 - a) * (s["ema_prompt_chars"] or prompt_chars)
                                      + a * float(prompt_chars), 0)
    s["last_ts"] = now_iso()
    _ROUTE_STATS_DIRTY["n"] += 1
    if _ROUTE_STATS_DIRTY["n"] >= 10:
        _ROUTE_STATS_DIRTY["n"] = 0
        try:
            asyncio.get_running_loop().create_task(_save_route_stats())
        except Exception:
            pass


def _route_tps(model: str, iid: str) -> float:
    """Best observed tokens/sec for a model on a node, across job types."""
    best = 0.0
    prefix = f"{model or '?'}|{iid or '?'}|"
    for k, s in _ROUTE_STATS.items():
        if k.startswith(prefix):
            best = max(best, float(s.get("ema_tps") or 0.0))
    return best


def estimate_request_seconds(model: str, iid: str, job_type: str,
                             prompt_chars: int) -> Optional[float]:
    """Predicted wall time for a request from the rolling stats — scaled by how
    the prompt compares to the typical prompt seen for this (model, node, job)."""
    s = _ROUTE_STATS.get(_route_stats_key(model, iid, job_type))
    if not s or not s.get("n"):
        return None
    base = float(s.get("ema_elapsed_s") or 0.0)
    if base <= 0:
        return None
    typical = float(s.get("ema_prompt_chars") or 0.0)
    if typical > 0 and prompt_chars > 0:
        scale = max(0.5, min(3.0, prompt_chars / typical))
        return round(base * scale, 2)
    return round(base, 2)


async def _save_route_stats() -> None:
    if not REDIS:
        return
    try:
        await REDIS.set(KEY_OLLAMA_ROUTE_STATS, json.dumps(_ROUTE_STATS))
    except Exception as e:
        log.debug("save route stats: %s", e)


async def _save_cap_routing() -> None:
    if not REDIS:
        return
    try:
        await REDIS.set(KEY_OLLAMA_CAP_ROUTING, json.dumps(CAP_ROUTING_USER))
    except Exception as e:
        log.warning("save cap routing: %s", e)


async def _save_routing() -> None:
    if not REDIS:
        return
    try:
        await REDIS.set(KEY_OLLAMA_ROUTING, json.dumps({
            "active_profile": ROUTING.get("active_profile", "default"),
            "profiles": ROUTING.get("profiles", {}),
        }))
    except Exception as e:
        log.warning("save routing: %s", e)


async def _save_nodes() -> None:
    if not REDIS:
        return
    try:
        snapshot = {iid: {"enabled": i.get("enabled", True),
                          "priority": i.get("priority", 0),
                          "label": i.get("label", iid),
                          "url": i.get("url", ""),
                          "has_gpu": i.get("has_gpu", False),
                          "num_ctx": i.get("num_ctx", 4096)}
                    for iid, i in OLLAMA_INSTANCES.items()}
        await REDIS.set(KEY_OLLAMA_NODES, json.dumps(snapshot))
    except Exception as e:
        log.warning("save nodes: %s", e)


async def _load_ollama_persistence() -> None:
    """Hydrate routing profiles, node enabled/priority and embed config from
    Redis once the connection is up. Safe to call when Redis is unavailable."""
    global _EMBED_PREFER_GPU, _EMBED_INSTANCE_ID
    for _ in range(60):                 # wait up to ~60s for backends to connect
        if REDIS:
            break
        await asyncio.sleep(1)
    if not REDIS:
        log.info("ollama persistence: Redis unavailable, using in-memory defaults")
        return
    try:
        raw = await REDIS.get(KEY_OLLAMA_ROUTING)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict) and doc.get("profiles"):
                ROUTING["profiles"] = doc["profiles"]
                ROUTING["active_profile"] = doc.get("active_profile", "default")
    except Exception as e:
        log.warning("load routing: %s", e)
    try:
        raw = await REDIS.get(KEY_OLLAMA_NODES)
        if raw:
            nodes = json.loads(raw)
            for iid, cfg in (nodes or {}).items():
                if iid in OLLAMA_INSTANCES:
                    OLLAMA_INSTANCES[iid].update({
                        "enabled": cfg.get("enabled", True),
                        "priority": cfg.get("priority", OLLAMA_INSTANCES[iid].get("priority", 0)),
                        "label": cfg.get("label", OLLAMA_INSTANCES[iid].get("label", iid)),
                        "num_ctx": cfg.get("num_ctx", OLLAMA_INSTANCES[iid].get("num_ctx", 4096)),
                    })
                elif cfg.get("url"):
                    # Restore a previously-added node that isn't in the defaults.
                    add_ollama_instance(iid, cfg["url"],
                                        has_gpu=cfg.get("has_gpu", False),
                                        label=cfg.get("label", iid))
                    OLLAMA_INSTANCES[iid]["enabled"] = cfg.get("enabled", True)
                    OLLAMA_INSTANCES[iid]["priority"] = cfg.get("priority", OLLAMA_INSTANCES[iid]["priority"])
    except Exception as e:
        log.warning("load nodes: %s", e)
    try:
        raw = await REDIS.get(KEY_OLLAMA_EMBED)
        if raw:
            ec = json.loads(raw)
            if ec.get("embed_model"):
                globals()["OLLAMA_EMBED_MODEL"] = ec["embed_model"]
            _EMBED_PREFER_GPU = bool(ec.get("prefer_gpu", False))
            _EMBED_INSTANCE_ID = ec.get("pinned_instance") or None
    except Exception as e:
        log.warning("load embed config: %s", e)
    try:
        raw = await REDIS.get(KEY_OLLAMA_CAP_ROUTING)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                CAP_ROUTING_USER.clear()
                for pat, r in doc.items():
                    if isinstance(r, dict):
                        CAP_ROUTING_USER[pat] = _cap_rule(pat, **{
                            k: r.get(k) for k in ("job_type", "label", "prefer_gpu",
                                                  "deny_gpu", "pin", "allow", "deny",
                                                  "model", "escalate_chars", "escalate")
                            if r.get(k) is not None})
    except Exception as e:
        log.warning("load cap routing: %s", e)
    try:
        raw = await REDIS.get(KEY_OLLAMA_ROLE_PROFILES)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                ROLE_PROFILES_USER.clear()
                for name, prof in doc.items():
                    if not isinstance(prof, dict):
                        continue
                    ROLE_PROFILES_USER[name] = {
                        "label": prof.get("label", name),
                        "owner": prof.get("owner", "user"),
                        "roles": {r: _role_rule(name, r, v)
                                  for r, v in (prof.get("roles") or {}).items()
                                  if isinstance(v, dict)},
                    }
    except Exception as e:
        log.warning("load role profiles: %s", e)
    try:
        raw = await REDIS.get(KEY_MEDIA_NODES)
        if raw:
            nodes = json.loads(raw)
            for iid, cfg_ in (nodes or {}).items():
                if not isinstance(cfg_, dict):
                    continue
                if iid in MEDIA_INSTANCES:
                    MEDIA_INSTANCES[iid].update({
                        "enabled": cfg_.get("enabled", True),
                        "priority": cfg_.get("priority", MEDIA_INSTANCES[iid].get("priority", 0)),
                        "label": cfg_.get("label", MEDIA_INSTANCES[iid].get("label", iid)),
                    })
                elif cfg_.get("url"):
                    # Restore a user-added media node that isn't in the seeds.
                    add_media_instance(iid, cfg_["url"],
                                       label=cfg_.get("label", iid),
                                       has_gpu=cfg_.get("has_gpu", False),
                                       enabled=cfg_.get("enabled", True),
                                       priority=cfg_.get("priority", 0))
    except Exception as e:
        log.warning("load media nodes: %s", e)
    try:
        raw = await REDIS.get(KEY_OLLAMA_ROUTE_STATS)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                _ROUTE_STATS.update({k: v for k, v in doc.items() if isinstance(v, dict)})
    except Exception as e:
        log.debug("load route stats: %s", e)
    try:
        raw = await REDIS.get(KEY_OLLAMA_INTERACTIVE)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                INTERACTIVE_PRIORITY.update({
                    "enabled": bool(doc.get("enabled", True)),
                    "window_s": max(10, int(doc.get("window_s", 180) or 180)),
                    "defer_background": bool(doc.get("defer_background", True)),
                    "background_always_cpu": bool(doc.get("background_always_cpu", False)),
                })
    except Exception as e:
        log.debug("load interactive priority: %s", e)
    log.info("ollama persistence: hydrated (profile=%s, %d nodes, %d cap rules, %d stat keys)",
             ROUTING.get("active_profile"), len(OLLAMA_INSTANCES),
             len(CAP_ROUTING_USER), len(_ROUTE_STATS))

# Per-instance concurrency semaphores for Ollama — limits simultaneous
# in-flight requests per node to 1 (Ollama queues internally but multiple
# concurrent httpx connections cause request pile-ups and timeouts).
# Callers that want parallelism across *different* nodes are unaffected.
# Use acquire/release via `async with _ollama_sem(iid):` pattern.
_OLLAMA_SEMAPHORES: Dict[str, asyncio.Semaphore] = {}
_OLLAMA_SEM_LIMIT = int(os.environ.get("OLLAMA_CONCURRENCY", "1"))

def _ollama_sem(iid: str) -> asyncio.Semaphore:
    """Return (creating if needed) the per-instance Semaphore."""
    if iid not in _OLLAMA_SEMAPHORES:
        _OLLAMA_SEMAPHORES[iid] = asyncio.Semaphore(_OLLAMA_SEM_LIMIT)
    return _OLLAMA_SEMAPHORES[iid]


# Optional bound on how long a request may WAIT for a free generation slot on
# its routed node. Default 0 = wait as long as it takes: queueing behind a
# large job (or many jobs) is legitimate progress, not a failure — timeouts are
# reserved for a request that is actually running and producing nothing. While
# queued, _ollama_liveness_heartbeat keeps the job's records fresh so the panel
# stale-check and the stuck-running sweeper know it is alive.
OLLAMA_QUEUE_TIMEOUT = float(os.environ.get("OLLAMA_QUEUE_TIMEOUT", "0") or 0)

# Cross-process GPU gate ("one big queue"). Feature-flagged (VERA_OLLAMA_GATE);
# a no-op until enabled AND a coordination Redis is connected, so it deploys
# dark and can never wedge generation. See vera/ollama_gate.py.
from Vera.vera import ollama_gate as _gate   # noqa: E402
_GATE_ON = _gate.gate_enabled()
# Dev-sandbox write guard (strict no-op in prod). See vera/sandbox_guard.py.
from Vera.vera.sandbox_guard import write_blocked as _sbx_write_blocked   # noqa: E402


def _split_redis_url(url: str):
    """(scheme, authority, data_db) from a redis URL — no regex, so it is safe
    to call from any scope. authority keeps any user:pass@host:port intact."""
    u = url or "redis://localhost:6379"
    sep = u.find("://")
    scheme = u[:sep] if sep >= 0 else "redis"
    rest = u[sep + 3:] if sep >= 0 else u
    cut = len(rest)
    for ch in ("/", "?"):
        i = rest.find(ch)
        if i >= 0:
            cut = min(cut, i)
    authority = rest[:cut]
    data_db = 0
    tail = rest[cut:]
    if tail.startswith("/"):
        seg = tail[1:].split("?", 1)[0]
        if seg.isdigit():
            data_db = int(seg)
    return scheme, authority, data_db


async def _ensure_coord_redis():
    """Lazily connect the SHARED coordination Redis for the Ollama gate — the
    'one big queue' lives here so prod + every dev container see the same slot
    leases. Idempotent + cached, and robust to whichever path connected REDIS
    first (main connector, worker loop, or a probe). When the coordination DB
    equals the data DB (prod's own case) REDIS is reused with no 2nd connection.
    Fail-open: any error leaves COORD_REDIS None and the gate a no-op."""
    global COORD_REDIS
    if COORD_REDIS is not None or not HAS_REDIS:
        return COORD_REDIS
    try:
        scheme, authority, data_db = _split_redis_url(REDIS_URL)
        if data_db == COORD_REDIS_DB and REDIS is not None:
            COORD_REDIS = REDIS
            log.info("✓ Ollama gate coord Redis = data Redis (DB %d)", COORD_REDIS_DB)
            return COORD_REDIS
        coord_url = f"{scheme}://{authority}/{COORD_REDIS_DB}"
        _cr = aioredis.from_url(coord_url, decode_responses=False,
                                socket_connect_timeout=4, socket_timeout=4)
        await _cr.ping()
        COORD_REDIS = _cr
        log.info("✓ Ollama gate coord Redis connected: %s (data DB %d)",
                 coord_url, data_db)
    except Exception as e:
        log.warning("coord Redis connect failed — Ollama gate stays a no-op: %s", e)
    return COORD_REDIS


async def _ollama_liveness_heartbeat(req_id: str, iid: str, mdl: str,
                                     caller: dict, prompt_preview: str,
                                     stop_evt: "asyncio.Event",
                                     interval: float = 120.0) -> None:
    """Re-emit a lightweight ollama.request while a request waits for its
    node's generation slot. Each emission refreshes updated_ts in the job
    store, the K_OLLAMA log entry and the panel's tracker — so a request that
    is legitimately QUEUED never trips the stale detector or the stuck-running
    sweeper. Stops the moment the slot is acquired (stop_evt set)."""
    waited = 0.0
    while not stop_evt.is_set():
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            waited += interval
            try:
                await emit_event({
                    "type": "ollama.request", "req_id": req_id,
                    "model": mdl, "instance_id": iid,
                    "phase": "queued", "queued_s": int(waited),
                    "caller_file": caller.get("caller_file", ""),
                    "caller_func": caller.get("caller_func", ""),
                    "cap_name": caller.get("cap_name", ""),
                    "prompt_preview": prompt_preview,
                })
            except Exception:
                pass

@asynccontextmanager
async def _ollama_slot(iid: str, timeout: Optional[float] = None):
    """`async with _ollama_sem(iid)` with a bounded acquisition wait. Raises a
    plain Exception on queue timeout so ollama_generate's normal error path
    (request_error event + node fallback) handles it like any other failure."""
    wait = OLLAMA_QUEUE_TIMEOUT if timeout is None else timeout
    sem = _ollama_sem(iid)
    if wait and wait > 0:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=wait)
        except asyncio.TimeoutError:
            raise Exception(
                f"queue timeout on {iid}: no free generation slot after "
                f"{int(wait)}s (node busy with earlier requests)")
    else:
        await sem.acquire()
    # ── Cross-process GPU gate ("one big queue") ─────────────────────────────
    # The local semaphore above serialises WITHIN this process; this lease
    # serialises the same node ACROSS prod + every dev sandbox, so they don't
    # flood one GPU. Fail-open by construction: a None lease (gate off / node
    # ungated / coord Redis down / queued past the wait budget) just means the
    # caller proceeds unslotted — generation is never blocked by the gate.
    _lease = None
    if _GATE_ON:
        try:
            if COORD_REDIS is None:
                await _ensure_coord_redis()
            _cap = _gate.capacity_for(bool(OLLAMA_INSTANCES.get(iid, {}).get("has_gpu")))
            if _cap > 0 and COORD_REDIS is not None:
                _lease = await _gate.acquire(
                    COORD_REDIS, iid, _cap, _gate.ttl_ms(), _gate.wait_s())
        except Exception as _ge:
            log.debug("ollama gate acquire skipped for %s: %s", iid, _ge)
    try:
        yield
    finally:
        if _lease is not None:
            try:
                await _gate.release(COORD_REDIS, _lease)
            except Exception:
                pass
        sem.release()


def add_ollama_instance(iid: str, url: str, has_gpu: bool = False, label: str = ""):
    OLLAMA_INSTANCES[iid] = {"url":url,"label":label or iid,"has_gpu":has_gpu,"enabled":True,
                              "priority":len(OLLAMA_INSTANCES),"status":"unknown",
                              "latency_ms":None,"models":[],"in_use":0,"last_check":None,"errors":0}

async def _ping_instance(iid: str, inst: dict):
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=5) as c:
            r = await c.get(f"{inst['url']}/api/tags"); r.raise_for_status()
            ms     = round((time.monotonic()-t0)*1000)
            models = [m["name"] for m in r.json().get("models",[])]
            inst.update(status="online",latency_ms=ms,models=models,last_check=now_iso(),errors=0)
    except Exception as e:
        inst.update(status="offline",latency_ms=None,last_check=now_iso(),errors=inst["errors"]+1)
        log.debug("Ping [%s] failed: %s", iid, e)

async def instance_health_loop(interval: float = 20.0):
    while True:
        await asyncio.gather(*[_ping_instance(iid,inst) for iid,inst in OLLAMA_INSTANCES.items()],return_exceptions=True)
        await emit_event({"type":"ollama.health","instances":{iid:{"status":i["status"],"latency_ms":i["latency_ms"]} for iid,i in OLLAMA_INSTANCES.items()}})
        # Media nodes (GPU inference servers) ride the same heartbeat.
        try:
            await asyncio.gather(*[_ping_media_instance(iid, inst)
                                   for iid, inst in MEDIA_INSTANCES.items()],
                                 return_exceptions=True)
        except Exception:
            pass
        await asyncio.sleep(interval)


# ─────────────────────────────────────────────────────────────────────────────
# MEDIA NODES — STT / TTS / image-gen servers, routed like LLM nodes
# ─────────────────────────────────────────────────────────────────────────────
# The GPU inference server (edge/GPU_inference.py) serves Whisper STT, TTS and
# Stable Diffusion on port 8765. Historically every caller hit cfg.GPU_INFER_URL
# directly; now the servers are ROUTABLE NODES: each is health-probed for which
# services it actually has installed, and resolve_media() picks a node per
# service through the same rule machinery (job types stt / tts / imagegen —
# GPU-first by default, pin/allow/deny/deny_gpu editable in the Model Routing
# page). Candidate nodes are seeded on every Ollama host, so installing the
# server on a CPU node makes it routable automatically on the next heartbeat.
MEDIA_SERVICES = ("stt", "tts", "imagegen")
# /health payload key → service name
_MEDIA_SERVICE_KEYS = {"whisper": "stt", "tts": "tts", "stable_diffusion": "imagegen"}
MEDIA_INSTANCES: Dict[str, dict] = {}
KEY_MEDIA_NODES = "vera:media:nodes"
_MEDIA_FALLBACK_URL = os.environ.get("GPU_INFER_URL", "http://192.168.0.250:8765")


def add_media_instance(iid: str, url: str, label: str = "", has_gpu: bool = False,
                       enabled: bool = True, priority: int = 0,
                       seeded: bool = False) -> dict:
    MEDIA_INSTANCES[iid] = {
        "url": url.rstrip("/"), "label": label or iid, "has_gpu": has_gpu,
        "enabled": enabled, "priority": priority, "status": "unknown",
        "services": [], "detail": {}, "in_use": 0, "errors": 0,
        "last_check": None, "seeded": seeded,
    }
    return MEDIA_INSTANCES[iid]


def _seed_media_instances() -> None:
    """Primary node from GPU_INFER_URL + a candidate on every other Ollama
    host (same port) — candidates stay 'offline' until the inference server is
    actually installed there, at which point the heartbeat lights them up."""
    from urllib.parse import urlparse
    global _MEDIA_FALLBACK_URL
    try:
        from Vera.vera.config import cfg as _cfg
        _MEDIA_FALLBACK_URL = getattr(_cfg, "GPU_INFER_URL", _MEDIA_FALLBACK_URL)
    except Exception:
        pass
    pu = urlparse(_MEDIA_FALLBACK_URL)
    port = pu.port or 8765
    seen = {pu.hostname}
    add_media_instance("media-gpu", _MEDIA_FALLBACK_URL, label="Media · GPU server",
                       has_gpu=True, priority=0, seeded=True)
    for iid, inst in OLLAMA_INSTANCES.items():
        h = urlparse(inst.get("url", "")).hostname
        if not h or h in seen:
            continue
        seen.add(h)
        add_media_instance(f"media-{iid}", f"http://{h}:{port}",
                           label=f"Media · {inst.get('label', iid)}",
                           has_gpu=inst.get("has_gpu", False),
                           priority=1 + int(inst.get("priority", 0)), seeded=True)


_seed_media_instances()


async def _ping_media_instance(iid: str, inst: dict) -> None:
    """GET /health on a media node; record which services it serves."""
    try:
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=4) as c:
            r = await c.get(f"{inst['url']}/health")
            r.raise_for_status()
            d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        inst.update(
            status="online", errors=0, last_check=now_iso(),
            services=[svc for key, svc in _MEDIA_SERVICE_KEYS.items() if d.get(key)],
            detail={k: d.get(k) for k in ("tts_engine", "gpu", "cuda", "device",
                                          "sample_rate") if k in d},
        )
    except Exception as e:
        inst.update(status="offline", last_check=now_iso(),
                    errors=inst.get("errors", 0) + 1)
        log.debug("media ping [%s] failed: %s", iid, e)


async def _save_media_nodes() -> None:
    if not REDIS:
        return
    try:
        snapshot = {iid: {"url": i.get("url", ""), "label": i.get("label", iid),
                          "has_gpu": i.get("has_gpu", False),
                          "enabled": i.get("enabled", True),
                          "priority": i.get("priority", 0),
                          "seeded": i.get("seeded", False)}
                    for iid, i in MEDIA_INSTANCES.items()}
        await REDIS.set(KEY_MEDIA_NODES, json.dumps(snapshot))
    except Exception as e:
        log.warning("save media nodes: %s", e)


def resolve_media(service: str = "", explain: Optional[dict] = None) -> Optional[dict]:
    """Pick the media node for a service (stt / tts / imagegen) through the
    routing rules — the media analogue of pick_instance. Only online+enabled
    nodes that report the service are candidates (any online node when the
    service is unknown/blank). The service's job-type rule applies: pin wins,
    deny_gpu/allow/deny filter, prefer_gpu (default) selects GPU nodes first,
    then least busy → priority. Returns {instance_id, url, label, has_gpu,
    services, reason} or None."""
    trail: List[str] = []
    cands = {iid: i for iid, i in MEDIA_INSTANCES.items()
             if i.get("status") == "online" and i.get("enabled", True)}
    if not cands:
        trail.append("no online media nodes")
        if explain is not None:
            explain["reason"] = trail
        return None
    if service:
        with_svc = {iid: i for iid, i in cands.items()
                    if service in (i.get("services") or [])}
        if with_svc:
            cands = with_svc
            trail.append(f"nodes serving '{service}': {list(with_svc)}")
        else:
            trail.append(f"no node reports '{service}' — trying any online node")
    rule = _resolve_rule(service) if service in MEDIA_SERVICES else {}
    if rule:
        pin = rule.get("pin") or ""
        if pin and pin in cands:
            trail.append(f"rule pin → {pin}")
            cands = {pin: cands[pin]}
        else:
            if rule.get("deny_gpu"):
                nong = {iid: i for iid, i in cands.items() if not i.get("has_gpu")}
                if nong:
                    cands = nong; trail.append("deny_gpu: GPU nodes excluded")
            allow = rule.get("allow") or []
            if allow:
                filt = {iid: i for iid, i in cands.items()
                        if any(_match_glob(iid, p) for p in allow)}
                if filt:
                    cands = filt; trail.append(f"allow filter {allow}")
            deny = rule.get("deny") or []
            if deny:
                filt = {iid: i for iid, i in cands.items()
                        if not any(_match_glob(iid, p) for p in deny)}
                if filt:
                    cands = filt; trail.append(f"deny filter {deny}")
    prefer_gpu = rule.get("prefer_gpu", True) if rule else True
    if prefer_gpu and not (rule or {}).get("deny_gpu"):
        gpu = {iid: i for iid, i in cands.items() if i.get("has_gpu")}
        if gpu:
            cands = gpu; trail.append("prefer_gpu: GPU media nodes")
    chosen = min(cands, key=lambda k: (cands[k].get("in_use", 0),
                                       cands[k].get("priority", 0)))
    trail.append(f"picked '{chosen}' (in_use={cands[chosen].get('in_use', 0)}, "
                 f"prio={cands[chosen].get('priority', 0)})")
    if explain is not None:
        explain["reason"] = trail
        explain["chosen"] = chosen
    i = MEDIA_INSTANCES[chosen]
    return {"instance_id": chosen, "url": i.get("url", ""),
            "label": i.get("label", chosen), "has_gpu": bool(i.get("has_gpu")),
            "services": list(i.get("services") or []), "job_type": service or "media",
            "reason": trail}


def media_base(service: str = "") -> str:
    """Resolved base URL for a media service; falls back to the configured
    GPU_INFER_URL when no media node is routable (e.g. before the first
    heartbeat), so callers always get a target."""
    res = resolve_media(service)
    return res["url"] if res and res.get("url") else _MEDIA_FALLBACK_URL


from contextlib import asynccontextmanager as _asynccontextmanager


@_asynccontextmanager
async def media_slot(service: str = ""):
    """Resolve a media node for `service` and hold a routing slot on it for
    the duration — heavy callers (STT/TTS/image-gen) use this so concurrent
    media work spreads across nodes by least-busy. Yields the resolved node
    dict (a fallback stub pointing at GPU_INFER_URL when nothing is routable)."""
    res = resolve_media(service)
    if not res:
        yield {"instance_id": "", "url": _MEDIA_FALLBACK_URL,
               "label": "fallback", "has_gpu": True, "services": [],
               "job_type": service or "media", "reason": ["fallback: no routable media node"]}
        return
    node = MEDIA_INSTANCES.get(res["instance_id"])
    if node is not None:
        node["in_use"] = node.get("in_use", 0) + 1
    try:
        yield res
    finally:
        if node is not None:
            node["in_use"] = max(0, node.get("in_use", 1) - 1)

# When each node was last handed work. Only used as the FINAL tie-break in
# _pick_best, so it spreads otherwise-identical candidates round-robin.
_LAST_PICKED: Dict[str, float] = {}


def _embed_node_id() -> str:
    """The instance id embeddings currently route to, or "" if indeterminate.
    Resolution: runtime pinned instance (embed config UI) → embedding rule's
    pin → the node whose URL matches OLLAMA_EMBED_URL."""
    pinned = globals().get("_EMBED_INSTANCE_ID")
    if pinned and pinned in OLLAMA_INSTANCES:
        return pinned
    try:
        rule_pin = (_resolve_rule("embedding") or {}).get("pin") or ""
        if rule_pin and rule_pin in OLLAMA_INSTANCES:
            return rule_pin
    except Exception:
        pass
    embed_url = (OLLAMA_EMBED_URL or "").rstrip("/")
    if embed_url:
        # Exact match first, then a looser host:port match so a trivial format
        # difference (scheme, trailing slash, host vs IP) doesn't silently
        # defeat avoid_embed. _hostport strips scheme + path.
        def _hostport(u: str) -> str:
            u = (u or "").split("://", 1)[-1]
            return u.split("/", 1)[0].strip().lower()
        for iid, inst in OLLAMA_INSTANCES.items():
            if (inst.get("url") or "").rstrip("/") == embed_url:
                return iid
        want = _hostport(embed_url)
        for iid, inst in OLLAMA_INSTANCES.items():
            if want and _hostport(inst.get("url") or "") == want:
                return iid
    return ""


def pick_instance(prefer_gpu: bool = False, instance_id: Optional[str] = None,
                  model: Optional[str] = None, job_type: Optional[str] = None,
                  rule_override: Optional[dict] = None,
                  explain: Optional[dict] = None,
                  ctx_need: int = 0) -> Optional[str]:
    # `explain`, when passed, is filled with the decision trail so callers can
    # log/emit WHY a node was chosen (rule applied, filters, tie-break).
    trail: List[str] = []
    def _note(msg): trail.append(msg)
    def _out(chosen):
        if explain is not None:
            explain["reason"] = trail
            explain["chosen"] = chosen
        return chosen
    # Only online AND enabled nodes are routable. Disabling a node (enabled=False)
    # removes it from all routing while leaving it pingable in the panel.
    online = {iid:i for iid,i in OLLAMA_INSTANCES.items()
              if i.get("status")=="online" and i.get("enabled", True)}
    if not online:
        # No CONFIRMED-online node. Rather than strand every caller (which forces
        # subsystems onto hardcoded fallbacks / dead IPs), route optimistically to
        # enabled nodes that simply haven't been health-probed yet ("unknown" —
        # e.g. within the first heartbeat after startup, or if the health loop
        # stalled). Genuinely "offline" nodes (a ping actually failed) stay out.
        online = {iid:i for iid,i in OLLAMA_INSTANCES.items()
                  if i.get("status") in (None, "unknown") and i.get("enabled", True)}
        if online:
            _note("no confirmed-online node — routing optimistically to unprobed node(s)")
        else:
            _note("no online or unprobed nodes")
            return _out(None)
    # An explicit pin always wins (as long as it's online+enabled).
    if instance_id and instance_id in online:
        _note(f"caller pinned instance '{instance_id}'")
        return _out(instance_id)

    # ── Routing rule: caller-supplied override (per-cap rule), else the active
    #    profile's job-type rule, else the built-in default. ──────────────────
    rule = rule_override if rule_override else (_resolve_rule(job_type) if job_type else None)
    if rule:
        if rule_override:
            _note(f"cap rule '{rule.get('pattern', '?')}' applied")
        else:
            _note(f"job-type rule '{job_type}' applied")
        pin = rule.get("pin") or ""
        if pin and pin in online:
            _note(f"rule pin → {pin}")
            return _out(pin)
        if rule.get("deny_gpu"):
            nong = {iid:i for iid,i in online.items() if not i.get("has_gpu")}
            if nong:
                online = nong; _note("deny_gpu: GPU nodes excluded")
        allow = rule.get("allow") or []
        if allow:
            filt = {iid:i for iid,i in online.items()
                    if any(_match_glob(iid, p) for p in allow)}
            if filt:
                online = filt; _note(f"allow filter {allow} → {list(filt)}")
        deny = rule.get("deny") or []
        if deny:
            filt = {iid:i for iid,i in online.items()
                    if not any(_match_glob(iid, p) for p in deny)}
            if filt:
                online = filt; _note(f"deny filter {deny} → {list(filt)}")
        # avoid_embed: steer off the node embeddings route to — but only when
        # another candidate remains (sharing beats failing on a 1-node pool).
        if rule.get("avoid_embed"):
            emb = _embed_node_id()
            if emb and emb in online and len(online) > 1:
                online = {iid:i for iid,i in online.items() if iid != emb}
                _note(f"avoid_embed: '{emb}' excluded (embedding node)")
        # Rule's prefer_gpu augments the caller's preference.
        if rule.get("prefer_gpu") and not prefer_gpu:
            prefer_gpu = True; _note("rule prefers GPU")

    def _has_model(inst, mdl):
        """Check if an instance has a model — flexible name matching."""
        if not mdl: return True
        models = inst.get("models", [])
        mdl_base = mdl.split(":")[0]
        for m in models:
            if m == mdl or m.startswith(mdl + ":") or m.split(":")[0] == mdl_base:
                return True
        return False

    def _pick_best(candidates):
        # Least busy first, then configured priority, then OBSERVED throughput
        # for this model (rolling tokens/sec stats) so equally-idle nodes route
        # to the one that has actually served this model fastest.
        #
        # …then LEAST-RECENTLY-USED, which is what actually spreads sequential
        # work. in_use only differs while calls overlap; a stream of one-at-a-time
        # requests (embeddings, above all) sees in_use==0 on every node every
        # time, so the earlier keys tie and `min` deterministically returns the
        # same node forever. Measured: 8,541 of 8,541 embeddings — 100% — landed
        # on one CPU node while its peer sat idle. The LRU term breaks that tie so
        # consecutive requests alternate, without disturbing the busy/priority/
        # throughput ordering that comes first.
        chosen = min(candidates,
                     key=lambda k: (candidates[k]["in_use"], candidates[k]["priority"],
                                    -_route_tps(model or "", k),
                                    _LAST_PICKED.get(k, 0.0)))
        _LAST_PICKED[chosen] = time.time()
        others = [k for k in candidates if k != chosen]
        _note(f"picked '{chosen}' (in_use={candidates[chosen]['in_use']}, "
              f"prio={candidates[chosen]['priority']}, "
              f"tps={_route_tps(model or '', chosen) or 'n/a'})"
              + (f" over {others}" if others else ""))
        return chosen

    # Build model-aware candidate sets
    has_model = {iid:i for iid,i in online.items() if _has_model(i, model)} if model else online

    # ── Oversized-context escalation: GPU → CPU ──────────────────────────────
    # The GPU's value is LATENCY on requests that fit in VRAM (~105 tok/s here).
    # A request whose context exceeds the safe window would force ollama to
    # offload part of the model and drop to ~33 tok/s — and worse, it evicts the
    # nicely-seated model, so every SUBSEQUENT small request pays too until it
    # reloads. The CPU nodes have 50 GB of RAM and no VRAM cliff: they are slower
    # per token but they degrade gracefully instead of falling off a cliff.
    #
    # So: anything that does not fit goes to CPU, which keeps the GPU free as the
    # fast lane for in-context work and pushes the big job into the background.
    # Only when NO CPU node can serve it does the GPU take it anyway (spilling
    # beats failing).
    if ctx_need and has_model:
        _gpu_ids = [iid for iid, i in has_model.items() if i.get("has_gpu")]
        _cpu_ids = [iid for iid, i in has_model.items() if not i.get("has_gpu")]
        if _gpu_ids and _cpu_ids:
            _fits = any(ctx_need <= (gpu_safe_ctx(model or "", g) or 10**9) for g in _gpu_ids)
            if not _fits:
                _lim = max((gpu_safe_ctx(model or "", g) for g in _gpu_ids), default=0)
                has_model = {iid: i for iid, i in has_model.items() if not i.get("has_gpu")}
                prefer_gpu = False
                _note(f"ctx escalation: ~{ctx_need} tokens exceeds GPU window {_lim} — "
                      f"routing to CPU ({_cpu_ids}) to keep the GPU free and avoid a spill")

    if prefer_gpu:
        # Best: GPU node that has the model
        gpu_with_model = {iid:i for iid,i in has_model.items() if i["has_gpu"]}
        if gpu_with_model:
            _note("prefer_gpu: GPU node with model")
            return _out(_pick_best(gpu_with_model))
        # Next: any node that has the model
        if has_model:
            _note("prefer_gpu: no GPU has the model — any node with model")
            return _out(_pick_best(has_model))
        # Last resort: any GPU node (will likely 404 but may auto-pull)
        gpu_any = {iid:i for iid,i in online.items() if i["has_gpu"]}
        if gpu_any:
            _note("prefer_gpu: model nowhere — any GPU node")
            return _out(_pick_best(gpu_any))

    # Non-GPU preference: prefer nodes with the model
    if has_model:
        return _out(_pick_best(has_model))

    # No node has the model — pick least busy, log a warning
    if model:
        log.warning("pick_instance: model '%s' not found on any online node — routing to least busy", model)
        _note(f"model '{model}' on no node — least busy fallback")
    return _out(_pick_best(online))


# ── Context-window detection ────────────────────────────────────────────────
# Instead of hand-tuning num_ctx per agent/node/worker, ask Ollama what the
# model's real context window is (POST /api/show → model_info[<arch>.context_length])
# and use that. Cached per (url, model) since it never changes for a given model.
_MODEL_CTX_CACHE: Dict[str, int] = {}            # "url::model" -> context_length
# Global ceiling on the auto-detected window (0 = no cap → use the full model max).
# Lets an operator dial big-context models (e.g. 128k) down cluster-wide.
OLLAMA_MAX_AUTO_CTX = int(os.environ.get("OLLAMA_MAX_AUTO_CTX", "0"))


def _extract_ctx_from_show(info: dict) -> Optional[int]:
    """Pull the context length out of an /api/show `model_info` block.
    Prefers the architecture-specific key (e.g. llama.context_length,
    qwen3.context_length); falls back to any *.context_length key."""
    if not isinstance(info, dict):
        return None
    arch = info.get("general.architecture", "")
    if arch and isinstance(info.get(f"{arch}.context_length"), (int, float)):
        return int(info[f"{arch}.context_length"])
    for k, v in info.items():
        if k.endswith(".context_length") and isinstance(v, (int, float)):
            return int(v)
    return None


async def ollama_model_ctx(model: str, instance_id: Optional[str] = None,
                            prefer_gpu: bool = False) -> Optional[int]:
    """Return a model's true max context length from Ollama /api/show.

    Cached per (instance url, model). Returns None when it can't be determined
    (Ollama unreachable, model not present, or no context_length in the report).
    """
    if not model:
        return None
    chosen = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id, model=model)
    if not chosen:
        return None
    url = OLLAMA_INSTANCES.get(chosen, {}).get("url", "")
    if not url:
        return None
    key = f"{url}::{model}"
    if key in _MODEL_CTX_CACHE:
        return _MODEL_CTX_CACHE[key] or None
    ctx = None
    try:
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=8) as c:
            r = await c.post(f"{url}/api/show", json={"model": model})
            if r.status_code == 200:
                ctx = _extract_ctx_from_show(r.json().get("model_info", {}) or {})
    except Exception as e:
        log.debug("ollama_model_ctx [%s/%s]: %s", chosen, model, e)
    if ctx:
        _MODEL_CTX_CACHE[key] = ctx
    return ctx


# ── Adaptive per-node / per-model context sizing ─────────────────────────────
# A single cluster-wide ceiling is the wrong shape for a mixed cluster. The GPU
# node has a hard VRAM cliff: ask for more KV cache than fits and ollama silently
# offloads part of the MODEL to CPU, which cost ~3x throughput here (105 tok/s at
# 28672 → 33 tok/s at 32768). The CPU nodes have 50 GB of system RAM and no such
# cliff — capping them to the GPU's limit just throws context away for nothing.
#
# So the ceiling is resolved per (node, model), and — because a static estimate
# can never track quantisation, KV type or what else is resident — it LEARNS:
# after a generation we read /api/ps, and any partial residency shrinks the cached
# window for that pair and is reported (log + event) instead of silently costing
# throughput forever.
_NODE_MODEL_CTX: Dict[str, int] = {}      # "iid::model" -> learned-safe num_ctx
_CTX_STEP = 4096                          # granularity for grow/shrink
_CTX_FLOOR = 4096

# Fraction of a GPU's VRAM assumed usable for weights+KV. The rest is driver,
# compute buffers and fragmentation. Measured: a 12 GB card seated 9.22 GB.
_VRAM_USABLE = float(os.environ.get("OLLAMA_VRAM_USABLE_FRAC", "0.78"))

# Residency sampling. Checking /api/ps after every generation would add a round
# trip per call; instead sample a slice, and ALWAYS check when throughput looks
# like a spill (a partly-offloaded 9B drops from ~105 tok/s to ~33 here, so
# anything under this on a GPU node is worth confirming).
_SPILL_SAMPLE   = float(os.environ.get("OLLAMA_SPILL_SAMPLE", "0.05"))
_SPILL_TPS_HINT = float(os.environ.get("OLLAMA_SPILL_TPS_HINT", "60"))


def _node_hw(iid: str) -> dict:
    """Detected hardware for a node (catalog fills this); {} when unknown."""
    try:
        cat = sys.modules.get("catalog_capabilities") or \
              sys.modules.get("Vera.vera.catalog.catalog_capabilities")
        return (getattr(cat, "NODE_HW", {}) or {}).get(iid, {}) or {}
    except Exception:
        return {}


def _auto_ctx_for(model: str, iid: str, detected_max: int) -> int:
    """The context window to request for `model` on node `iid`.

    GPU node  → whatever we have LEARNED is safe, else a VRAM-derived estimate,
                else the global OLLAMA_MAX_AUTO_CTX ceiling.
    CPU node  → the model's full window; system RAM is plentiful and there is no
                spill cliff to fall off, so the GPU-shaped ceiling does not apply.
    """
    key = f"{iid}::{model}"
    learned = _NODE_MODEL_CTX.get(key)
    if learned:
        return max(_CTX_FLOOR, min(detected_max or learned, learned))
    inst = OLLAMA_INSTANCES.get(iid, {}) or {}
    hw = _node_hw(iid)
    vram = float(hw.get("vram_gb") or 0.0)
    if not inst.get("has_gpu") or vram <= 0:
        # CPU node (or GPU with unknown VRAM → treat conservatively as uncapped
        # by the VRAM ceiling but still bounded by the model's own max).
        return detected_max or _CTX_FLOOR
    # Rough seat check: KV cache grows ~linearly with context. We don't know the
    # model's per-token KV cost here, so use the global ceiling as the GPU
    # default and let the residency probe correct it downward if it was wrong.
    ceiling = OLLAMA_MAX_AUTO_CTX or detected_max or _CTX_FLOOR
    return max(_CTX_FLOOR, min(detected_max or ceiling, ceiling))


def gpu_safe_ctx(model: str, iid: str) -> int:
    """The largest context `iid` can serve for `model` WITHOUT spilling to CPU.
    Synchronous (routing runs in a hot path): uses the learned value when we have
    one, else the configured ceiling. 0 = unknown / not a GPU node."""
    inst = OLLAMA_INSTANCES.get(iid, {}) or {}
    if not inst.get("has_gpu"):
        return 0
    return _NODE_MODEL_CTX.get(f"{iid}::{model}") or OLLAMA_MAX_AUTO_CTX or 0


# Chars-per-token is model-dependent; 3.4 is a deliberately LOW estimate so the
# token count comes out high and a borderline request escalates rather than
# squeaking onto the GPU and spilling. Output is counted too — the KV cache has
# to hold it.
_CHARS_PER_TOKEN = float(os.environ.get("OLLAMA_CHARS_PER_TOKEN", "3.4"))
_CTX_RESERVE_OUT = int(os.environ.get("OLLAMA_CTX_RESERVE_OUT", "1024"))


def est_ctx_tokens(prompt: str = "", system: str = "", num_predict: int = 0) -> int:
    """Rough tokens this request needs resident: prompt + system + room to answer."""
    chars = len(prompt or "") + len(system or "")
    return int(chars / max(_CHARS_PER_TOKEN, 1.0)) + max(int(num_predict or 0), _CTX_RESERVE_OUT)


async def note_ctx_residency(iid: str, model: str, requested_ctx: int) -> Optional[dict]:
    """Read /api/ps and report whether `model` is FULLY resident on `iid`.

    Returns {resident_pct, size, size_vram, spilled, ctx} or None if unknown.
    On a partial load it shrinks the learned window for this (node, model) by one
    step so the next request seats fully, logs a WARNING (so it shows in the job
    log) and emits `ollama.cpu_spill` for the UI. This is the self-correcting
    half of _auto_ctx_for: an estimate that was too generous fixes itself once,
    rather than quietly costing throughput on every later call."""
    inst = OLLAMA_INSTANCES.get(iid) or {}
    if not inst.get("url") or not inst.get("has_gpu"):
        return None                      # CPU nodes have no VRAM residency to check
    try:
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=8) as c:
            r = await c.get(f"{inst['url']}/api/ps")
            if r.status_code != 200:
                return None
            rows = (r.json() or {}).get("models") or []
    except Exception:
        return None
    mdl_base = (model or "").split(":")[0]
    for m in rows:
        nm = str(m.get("name") or "")
        if not (nm == model or nm.startswith(model + ":") or nm.split(":")[0] == mdl_base):
            continue
        size = int(m.get("size") or 0)
        vram = int(m.get("size_vram") or 0)
        if size <= 0:
            return None
        pct = round(100 * vram / size)
        out = {"resident_pct": pct, "size": size, "size_vram": vram,
               "spilled": pct < 99, "ctx": m.get("context_length")}
        if out["spilled"]:
            key = f"{iid}::{model}"
            cur = _NODE_MODEL_CTX.get(key) or requested_ctx or _CTX_STEP * 2
            new = max(_CTX_FLOOR, (cur - _CTX_STEP))
            if new < cur:
                _NODE_MODEL_CTX[key] = new
            log.warning(
                "OLLAMA CPU SPILL on %s: %s is only %d%% resident (%.2f/%.2f GB) at "
                "num_ctx=%s — generation runs partly on CPU (~3x slower). "
                "Reducing this node+model's context to %d.",
                iid, model, pct, vram / 2**30, size / 2**30, out["ctx"], new)
            try:
                await emit_event({"type": "ollama.cpu_spill", "instance_id": iid,
                                  "model": model, "resident_pct": pct,
                                  "size_bytes": size, "vram_bytes": vram,
                                  "context_length": out["ctx"],
                                  "requested_ctx": requested_ctx,
                                  "new_ctx": new})
            except Exception:
                pass
        return out
    return None


async def effective_num_ctx(model: str, instance_id: Optional[str] = None,
                             prefer_gpu: bool = False, manual: int = 0) -> int:
    """Resolve the context window to actually use for a request.

    Defaults to the model's full detected max. `manual` (>0) caps it *down*
    (so a per-agent/worker setting can only shrink, never inflate, the window);
    OLLAMA_MAX_AUTO_CTX applies the same cap cluster-wide. Falls back to
    `manual or 4096` when detection fails.
    """
    detected = await ollama_model_ctx(model, instance_id, prefer_gpu)
    ctx = detected or manual or 4096
    if manual and manual > 0:
        ctx = min(ctx, manual)
    # Per-node/per-model ceiling. Resolving the node here (rather than applying a
    # single global cap) is what lets a 50 GB CPU node keep the model's full
    # window while the GPU node stays inside its VRAM cliff. _auto_ctx_for folds
    # in OLLAMA_MAX_AUTO_CTX for GPU nodes, so the global setting still applies
    # where it makes sense.
    iid = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id, model=model)
    if iid:
        ctx = min(ctx, _auto_ctx_for(model, iid, detected or ctx))
    elif OLLAMA_MAX_AUTO_CTX > 0:
        ctx = min(ctx, OLLAMA_MAX_AUTO_CTX)
    return max(_CTX_FLOOR, ctx)


def _ollama_caller_info(depth: int = 3) -> dict:
    """Walk the call stack to identify who triggered this Ollama request.
    Returns {caller_file, caller_func, caller_module, cap_name} for logging."""
    import traceback as _tb
    info = {"caller_file": "", "caller_func": "", "caller_module": "", "cap_name": ""}
    try:
        stack = _tb.extract_stack(limit=depth + 5)
        # Walk backwards skipping frames inside this file
        this_file = str(Path(__file__).name)
        for frame in reversed(stack[:-1]):  # skip the _ollama_caller_info frame
            fname = os.path.basename(frame.filename)
            if fname != this_file and not fname.startswith("<"):
                info["caller_file"] = fname
                info["caller_func"] = frame.name
                info["caller_module"] = fname.replace(".py", "")
                break
        # Try to extract capability name from further up the stack
        for frame in reversed(stack):
            if frame.name.startswith("cap_") or "capability" in frame.name.lower():
                info["cap_name"] = frame.name
                break
    except Exception:
        pass
    return info


# ── Ollama request log (in-process ring buffer + structured event emission) ──
_OLLAMA_REQUEST_LOG: List[dict] = []      # ring buffer, max 500
_OLLAMA_REQUEST_LOG_MAX = 500


def _err_text(e: Exception, limit: int = 300) -> str:
    """Human-readable error string that is never empty.

    httpx timeout/transport exceptions stringify to '' — that empty string is
    what surfaced as 'unknown' in the jobs panel. Always lead with the
    exception class so even a message-less error identifies itself.
    """
    msg = str(e).strip()
    if not msg:
        if isinstance(e, httpx.ConnectTimeout):
            msg = "connection timed out"
        elif isinstance(e, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            msg = "timed out waiting for response (generation exceeded the request timeout)"
        elif isinstance(e, httpx.ConnectError):
            msg = "connection failed (node down or unreachable)"
        elif isinstance(e, httpx.RemoteProtocolError):
            msg = "connection dropped mid-response"
        else:
            msg = repr(e)
    return f"{type(e).__name__}: {msg}"[:limit]


_ARGS_SECRET_RE = None  # compiled lazily


def _args_preview(kw: dict, limit: int = 600) -> str:
    """Compact single-line `k=v` preview of a capability's arguments for the
    jobs/observe panels. Values are truncated per-key, secrets masked, and the
    whole string capped so events stay small."""
    global _ARGS_SECRET_RE
    if _ARGS_SECRET_RE is None:
        import re as _re
        _ARGS_SECRET_RE = _re.compile(r"(pass(word)?|token|secret|api_?key|credential|auth)", _re.I)
    try:
        parts = []
        for k, v in kw.items():
            if k in ("trace_id",):
                continue
            if _ARGS_SECRET_RE.search(str(k)):
                parts.append(f"{k}=***")
                continue
            try:
                s = v if isinstance(v, str) else json.dumps(v, default=str)
            except Exception:
                s = str(v)
            s = " ".join(str(s).split())
            if len(s) > 160:
                s = s[:160] + "…"
            parts.append(f"{k}={s}")
        out = ", ".join(parts)
        return out[:limit] + ("…" if len(out) > limit else "")
    except Exception:
        return ""


def _args_compact(kw: dict, max_keys: int = 10, max_val: int = 160) -> dict:
    """Structured (masked, truncated) argument snapshot of a capability call.
    Same masking rules as _args_preview but returned as a dict so panel-side
    consumers (the live cap-activity mirror) can map args onto UI fields."""
    global _ARGS_SECRET_RE
    if _ARGS_SECRET_RE is None:
        _args_preview({})  # compiles the shared secret-mask regex
    out: dict = {}
    try:
        for k, v in kw.items():
            if k in ("trace_id",):
                continue
            if len(out) >= max_keys:
                break
            if _ARGS_SECRET_RE.search(str(k)):
                out[k] = "***"
                continue
            try:
                s = v if isinstance(v, str) else json.dumps(v, default=str)
            except Exception:
                s = str(v)
            s = str(s)
            out[k] = s[:max_val] + ("…" if len(s) > max_val else "")
    except Exception:
        pass
    return out


# ── Live panel mirroring of capability activity ──────────────────────────────
# Every non-silent cap execution that carries a session id is mirrored onto the
# session's panel-dispatch channel (vera:panel:dispatch:{sid}) as a
# __cap_activity__ pseudo-action. The chat panel forwards it to the mounted
# panel iframe when the cap belongs to that panel, where vera-panel-bridge.js
# renders a live agent-activity feed (and panels can natively mirror the call).
# Fire-and-forget: nothing awaits a reply and failures never affect the cap.
_PANEL_MIRROR_ENABLED = os.getenv("PANEL_CAP_MIRROR", "1").lower() not in ("0", "false", "off")
# Groups that would be noise (or feedback loops) in a panel activity feed.
_PANEL_MIRROR_SKIP_GROUPS = {"panel", "chat", "obs", "health"}


async def _mirror_cap_activity(phase: str, name: str, sid: str, tid: str,
                               group: str, **extra):
    if not (_PANEL_MIRROR_ENABLED and REDIS and sid):
        return
    if group in _PANEL_MIRROR_SKIP_GROUPS:
        return
    try:
        await REDIS.publish(
            f"vera:panel:dispatch:{sid}",
            json.dumps({"request_id": new_id(), "session_id": sid,
                        "no_ack": True, "action": "__cap_activity__",
                        "ts": now_iso(),
                        "payload": {"phase": phase, "cap": name, "group": group,
                                    "trace_id": tid, **extra}}, default=str))
    except Exception:
        pass


async def ollama_generate(prompt: str, system: str = "", json_mode: bool = False,
                           model: Optional[str] = None, instance_id: Optional[str] = None,
                           prefer_gpu: bool = False, stream_cb: Optional[Callable] = None,
                           caller_override: Optional[dict] = None,
                           job_type: Optional[str] = None,
                           think: Optional[bool] = None,
                           options: Optional[dict] = None,
                           keep_alive: Optional[str] = None,
                           timeout: Optional[float] = None,
                           profile: Optional[str] = None,
                           role: Optional[str] = None,
                           meta_out: Optional[dict] = None) -> str:
    # ── Identify caller and log the request ──────────────────────────────────
    # caller_override lets an intermediary cap (e.g. llm.generate) pass
    # through the true upstream caller rather than appearing as the caller.
    caller   = caller_override if caller_override else _ollama_caller_info()
    # Job-type routing: explicit job_type wins; otherwise a per-cap rule may
    # force one; otherwise infer from the caller. A role-profile role
    # (profile= + role=, e.g. ide/thinker) outranks per-cap rules and rides
    # the same merge path below.
    role_rule = resolve_role_rule(profile, role) if (profile and role) else None
    cap_rule = role_rule or _resolve_cap_routing(str(caller.get("cap_name") or ""))
    eff_job_type = ((job_type or "").strip()
                    or (cap_rule or {}).get("job_type", "")
                    or _infer_job_type(caller, model))
    prompt_chars = len(prompt or "") + len(system or "")
    # Effective rule: the per-cap rule (if any) is merged OVER the job-type
    # rule, and its length-based ESCALATION merged over that when the prompt
    # crosses the configured threshold.
    escalated = False
    try:
        base_rule = dict(_resolve_rule(eff_job_type) or {})
    except Exception:
        base_rule = {}
    eff_rule = base_rule
    rule_source = f"profile:{eff_job_type}"
    if cap_rule:
        eff_rule = _merge_rule_over_base(cap_rule, base_rule, prompt_chars)
        eff_rule["pattern"] = cap_rule.get("pattern", "")
        rule_source = (f"role:{cap_rule.get('pattern', '')}" if role_rule
                       else f"cap:{cap_rule.get('pattern', '')}")
        esc_at = int(cap_rule.get("escalate_chars") or 0)
        escalated = bool(esc_at > 0 and prompt_chars >= esc_at
                         and cap_rule.get("escalate"))
    # A routing rule may pin a lighter model for this job type (e.g. naming /
    # summarize). The caller's explicit model always wins over the rule's.
    eff_model = model or (eff_rule or {}).get("model") or None
    # ── vLLM delegation: a rule/profile pin of "vllm:<id>" (or "vllm:*" for
    # any node) sends this request to the vLLM backend instead of Ollama — the
    # router treats vLLM servers as routable targets. A caller-explicit
    # instance_id still wins (it names an Ollama node). If no vLLM node is
    # online the request falls through to normal Ollama routing.
    _pin = str((eff_rule or {}).get("pin") or "")
    if _pin.startswith("vllm:") and not instance_id:
        try:
            from Vera.vera.vllm.vllm_capabilities import (
                vllm_generate as _vllm_gen, pick_vllm_instance as _vllm_pick)
            _vid = _pin.split(":", 1)[1]
            _vid = None if _vid in ("", "*") else _vid
            if _vllm_pick(instance_id=_vid) is None:
                raise RuntimeError("no online vLLM instance")
            _opts = options or {}
            _np = int(_opts.get("num_predict") or 0)
            return await _vllm_gen(
                (f"{system}\n\n{prompt}" if system else prompt),
                model=eff_model or None,
                instance_id=_vid,
                prefer_gpu=prefer_gpu or bool((eff_rule or {}).get("prefer_gpu")),
                max_tokens=(_np if _np > 0 else 1024),
                temperature=float(_opts.get("temperature", 0.7)),
                top_p=float(_opts.get("top_p", 0.9)),
                guided_json=({"type": "object"} if json_mode else None),
                stream_cb=stream_cb,
                caller_override=caller,
            )
        except Exception as _ve:
            log.warning("vllm delegation for pin '%s' failed (%s) — "
                        "falling back to Ollama routing", _pin, _ve)
    # ── API-provider delegation: a rule/profile pin of "provider:<id>" (or a
    # model ref "provider:<id>/<model>") sends this request to an external API
    # provider via providers.chat — the routing table can route any caller to
    # OpenAI/Anthropic/custom endpoints, with usage + cost recorded by the
    # providers module. A caller-explicit instance_id (an Ollama node) still
    # wins; failures fall through to normal Ollama routing.
    _prov_id = _prov_model = ""
    if not instance_id:
        if _pin.startswith("provider:"):
            _pp = _pin.split(":", 1)[1]
            _prov_id, _, _pm = _pp.partition("/")
            _prov_model = _pm or str(eff_model or "")
        elif str(eff_model or "").startswith("provider:"):
            _pp = str(eff_model).split(":", 1)[1]
            _prov_id, _, _prov_model = _pp.partition("/")
    if _prov_id:
        try:
            _pc = CAPABILITY_REGISTRY.get("providers.chat")
            _pfn = (_pc.get("raw") or _pc.get("func")) if _pc else None
            if not _pfn:
                raise RuntimeError("providers module not loaded")
            _opts = options or {}
            _np = int(_opts.get("num_predict") or 0)
            _sys = system or ""
            if json_mode:
                _sys = (_sys + "\n\nRespond with a single valid JSON object "
                               "and nothing else.").strip()
            _res = await _pfn(provider=_prov_id, model=_prov_model,
                              prompt=prompt, system=_sys,
                              max_tokens=(_np if _np > 0 else 1024),
                              caller=str(caller.get("cap_name") or "ollama_generate"))
            if not isinstance(_res, dict) or _res.get("error"):
                raise RuntimeError(str((_res or {}).get("error", "no response"))[:200])
            _txt = _res.get("text", "") or ""
            if stream_cb:
                try:
                    _maybe = stream_cb(_txt)
                    if inspect.isawaitable(_maybe):
                        await _maybe
                except Exception:
                    pass
            await emit_event({"type": "ollama.request_done",
                              "req_id": str(uuid.uuid4())[:12],
                              "model": f"provider:{_prov_id}/{_res.get('model', _prov_model)}",
                              "instance_id": f"provider:{_prov_id}",
                              "job_type": eff_job_type, "rule_source": rule_source,
                              "caller_file": caller.get("file", ""),
                              "caller_func": caller.get("func", ""),
                              "cost_usd": _res.get("cost_usd")})
            return _txt
        except Exception as _pe:
            log.warning("provider delegation '%s' failed (%s) — "
                        "falling back to Ollama routing", _prov_id, _pe)
            if str(eff_model or "").startswith("provider:"):
                eff_model = None   # a provider ref is meaningless to Ollama
    # ── Interactive priority ────────────────────────────────────────────────
    # A human-facing generation stamps "human active now"; a BACKGROUND one
    # (dream cycles, V8 programs, fabric NLP — anything inside a BACKGROUND_LLM
    # context) is demoted off the GPU pool while the human is active, provided
    # a CPU node is online to take it. The GPU stays free for the person.
    bg_label = BACKGROUND_LLM.get("")
    if not bg_label and eff_job_type in _INTERACTIVE_JOB_TYPES:
        note_interactive(eff_job_type)
    bg_demoted = False
    if bg_label and INTERACTIVE_PRIORITY.get("enabled", True):
        # Demote background work off the GPU when the human is active OR when
        # background_always_cpu pins ALL background work to CPU deterministically.
        _always = bool(INTERACTIVE_PRIORITY.get("background_always_cpu"))
        if _always or interactive_recent():
            _cpu_up = any(i.get("status") == "online" and i.get("enabled", True)
                          and not i.get("has_gpu")
                          for i in OLLAMA_INSTANCES.values())
            if _cpu_up and not instance_id:
                # Route background work exactly like the dream orchestrator's own
                # LLM (dream_director rule): CPU-only AND off the embedding node,
                # so long generations never tie up the GPU or the embed slot.
                eff_rule = {**(eff_rule or {}), "deny_gpu": True, "prefer_gpu": False,
                            "avoid_embed": True, "pin": ""}
                prefer_gpu = False
                bg_demoted = True
                rule_source += ("+bg-cpu" if _always else "+interactive-backoff")
    route_explain: dict = {}
    # How much context this request actually needs — lets the router send an
    # oversized one to a CPU node instead of spilling the GPU (see pick_instance).
    _ctx_need = est_ctx_tokens(prompt, system, (options or {}).get("num_predict") or 0)
    chosen = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id,
                           model=eff_model, job_type=eff_job_type,
                           rule_override=(eff_rule if (cap_rule or bg_demoted) else None),
                           explain=route_explain, ctx_need=_ctx_need) or "cpu-246"
    if bg_demoted:
        route_explain.setdefault("reason", []).append(
            f"background '{bg_label}' demoted off GPU (human active)")
    routing_info = {
        "job_type":     eff_job_type,
        "rule_source":  rule_source,
        "escalated":    escalated,
        "prompt_chars": prompt_chars,
        "background":   bg_label,
        "interactive_backoff": bg_demoted,
        "reason":       route_explain.get("reason") or [],
        "est_seconds":  estimate_request_seconds(eff_model or OLLAMA_MODEL, chosen,
                                                 eff_job_type, prompt_chars),
    }
    inst   = OLLAMA_INSTANCES[chosen]
    # RESERVE the slot on the chosen node NOW — synchronously, before the first
    # `await` below (emit_event). pick_instance sorts by `in_use`, so if we defer
    # the increment until after an await, several concurrent LLM calls (e.g. many
    # parallel loop steps) all see the same node as "least busy" and stampede onto
    # it, then serialise behind its concurrency semaphore while other nodes sit
    # idle — on a slow CPU node that reads as a lockup. Reserving here closes that
    # race so the next picker sees this node's raised load and spreads out. The
    # single `finally` at the end releases it exactly once.
    inst["in_use"] = inst.get("in_use", 0) + 1
    mdl    = eff_model or OLLAMA_MODEL
    body   = {"model":mdl,"prompt":prompt,"stream":stream_cb is not None}
    if system:    body["system"]  = system
    if json_mode: body["format"]  = "json"
    # keep_alive keeps the model resident between calls (avoids cold-reload
    # latency that dominates slow single-call planning). Per-call override wins.
    body["keep_alive"] = keep_alive if keep_alive is not None else OLLAMA_KEEP_ALIVE
    # Tuning options (temperature, num_ctx, num_predict, …) — e.g. an agent's
    # ollama_options(). A large num_ctx here is what stops a big planner prompt
    # being silently truncated to the model's default window. The effective
    # routing rule may pin sampling defaults (a role profile's temperature/
    # num_ctx); the caller's explicit options always win key-by-key.
    _rule_opts = (eff_rule or {}).get("options") or {}
    _merged_opts = {**_rule_opts, **(dict(options) if options else {})}
    if _merged_opts:
        body["options"] = _merged_opts
    gen_timeout = float(timeout) if timeout else OLLAMA_GEN_TIMEOUT
    # Reasoning models (e.g. Qwen3) route their <think> output into a separate
    # `thinking` field under native-thinking Ollama, leaving `response` empty if
    # the answer never lands — which silently breaks JSON callers. Default such
    # callers to think=False (the API-level switch; the `/no_think` prompt token
    # is ignored by native-thinking models). Explicit `think` always wins.
    if think is None and json_mode:
        think = False
    if think is not None:
        body["think"] = think
    req_id   = str(uuid.uuid4())[:12]
    t_start  = time.time()
    prompt_preview = (prompt or "")[:120].replace("\n", " ")

    log.info(
        "ollama_req [%s] model=%s inst=%s job=%s rule=%s%s est=%ss chars=%d caller=%s:%s route=%s prompt=%s",
        req_id, mdl, chosen, eff_job_type, rule_source,
        " ESCALATED" if escalated else "",
        routing_info.get("est_seconds") if routing_info.get("est_seconds") is not None else "?",
        prompt_chars,
        caller["caller_file"], caller["caller_func"],
        "; ".join(routing_info.get("reason") or [])[:300],
        prompt_preview,
    )

    req_entry = {
        "req_id": req_id, "model": mdl, "instance": chosen,
        "caller_file": caller["caller_file"], "caller_func": caller["caller_func"],
        "prompt_preview": prompt_preview, "ts": now_iso(),
        "status": "running",
        "job_type": eff_job_type, "rule_source": rule_source,
        "escalated": escalated, "prompt_chars": prompt_chars,
        "route_reason": "; ".join(routing_info.get("reason") or [])[:400],
        "est_seconds": routing_info.get("est_seconds"),
    }

    # NOTE: in_use is already reserved above (right after pick_instance) to close
    # the concurrent-routing race — do NOT increment it again here.
    # Acquire per-instance semaphore — serialises to one in-flight request per
    # node (OLLAMA_CONCURRENCY env, default 1) so the rest queue until it frees.
    try:
        # Emit inside the try so a caller cancelling us at this await still runs
        # the finally below that releases the in_use slot. Before this move a
        # CancelledError here leaked the reservation permanently, and leaked
        # slots accumulated until the header meter showed dozens of phantom
        # "active" requests (and routing saw every node as busy).
        try:
            await emit_event({
                "type":        "ollama.request",
                "req_id":      req_id,
                "model":       mdl,
                "instance_id": chosen,
                "instance_url": inst.get("url", ""),
                "session_id":  OLLAMA_EVENT_SESSION.get(""),
                "job_type":    eff_job_type,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "caller_module": caller["caller_module"],
                "cap_name":    caller["cap_name"],
                "prompt_preview": prompt_preview,
                "prompt_full": (prompt or "")[:16000],
                "json_mode":   json_mode,
                "prefer_gpu":  prefer_gpu,
                "streaming":   stream_cb is not None,
                "routing":     routing_info,
                # Submitted but not yet holding the node's generation slot —
                # the panel shows this as QUEUED until the 'generating' phase
                # event below (or a queue heartbeat) supersedes it.
                "phase":       "queued",
            })
        except Exception:
            pass  # never let logging break generation
        # ── Timeout semantics: fail only when NOTHING is happening ──────────
        #  • connect (15s): a dead node fails fast.
        #  • read (gen_timeout): the request below ALWAYS streams from Ollama
        #    (even when the caller wants a single string), so the read timeout
        #    applies BETWEEN chunks — it is a stall detector, not a cap on
        #    total generation time. A generation actively producing tokens can
        #    run as long as it needs.
        #  • queue wait: unbounded by default (OLLAMA_QUEUE_TIMEOUT bounds it).
        #    Waiting behind a large job or a busy node is progress, not
        #    failure; a liveness heartbeat below refreshes the job's records
        #    while it waits so nothing mistakes it for dead.
        _gto = httpx.Timeout(gen_timeout, connect=15.0)
        _hb_stop = asyncio.Event()
        _hb_task = asyncio.create_task(_ollama_liveness_heartbeat(
            req_id, chosen, mdl, caller, prompt_preview, _hb_stop))
        try:
            # timeout=timeout (not omitted): a caller that passed an explicit
            # per-call timeout gets it enforced on the QUEUE WAIT too, not just
            # the post-connection read below — without this, `timeout=` only
            # ever bounded the read/stall phase, and the wait for a free
            # generation slot stayed governed SOLELY by the global
            # OLLAMA_QUEUE_TIMEOUT (default 0 = unbounded) no matter what a
            # caller asked for. Callers that don't pass timeout (the vast
            # majority) are unaffected — same unbounded-queue-wait default as
            # always, since `timeout` is None there too.
            async with _ollama_slot(chosen, timeout=timeout):
                _hb_stop.set()   # slot acquired — the queue heartbeat can stop
                # Phase transition: queued → generating (panel moves the job
                # from the Queued tab to Running the moment the node starts).
                try:
                    await emit_event({
                        "type": "ollama.request", "req_id": req_id,
                        "model": mdl, "instance_id": chosen,
                        "phase": "generating",
                        "caller_file": caller["caller_file"],
                        "caller_func": caller["caller_func"],
                        "cap_name": caller["cap_name"],
                        "prompt_preview": prompt_preview,
                        "queued_s": round(time.time() - t_start, 1),
                    })
                except Exception:
                    pass
                body["stream"] = True   # always stream so silence == stall
                async with httpx.AsyncClient(verify=_SSL_CTX, timeout=_gto) as c:
                    async with c.stream("POST",f"{inst['url']}/api/generate",json=body) as resp:
                        if resp.status_code != 200:
                            err_body = ""
                            async for chunk in resp.aiter_bytes():
                                err_body += chunk.decode("utf-8", errors="replace")
                            raise Exception(f"ollama returned {resp.status_code}: {err_body[:500]}")
                        buf=[]; tbuf=[]; meta={}
                        _last_beat = time.time()
                        async for line in resp.aiter_lines():
                            if not line: continue
                            try:
                                d=json.loads(line)
                                tok=d.get("response","")
                                if tok:
                                    buf.append(tok)
                                    if stream_cb: await stream_cb(tok)
                                # Reasoning models may emit only `thinking` tokens;
                                # collect them (without streaming) as a fallback.
                                elif d.get("thinking"): tbuf.append(d["thinking"])
                                if d.get("done"): meta = d
                            except Exception: pass
                            # A long but actively-progressing generation emits no
                            # events of its own — refresh the job records every
                            # ~2.5 min so the panel's stale detector and the
                            # stuck-running sweeper never flag a request that is
                            # visibly producing tokens.
                            if time.time() - _last_beat > 150:
                                _last_beat = time.time()
                                try:
                                    await emit_event({
                                        "type": "ollama.request", "req_id": req_id,
                                        "model": mdl, "instance_id": chosen,
                                        "phase": "generating",
                                        "caller_file": caller["caller_file"],
                                        "caller_func": caller["caller_func"],
                                        "cap_name": caller["cap_name"],
                                        "prompt_preview": prompt_preview,
                                        "tokens_so_far": len(buf) + len(tbuf),
                                        "elapsed_s": round(time.time() - t_start, 1),
                                    })
                                except Exception:
                                    pass
                        result = "".join(buf) or "".join(tbuf)
                        elapsed = round(time.time() - t_start, 2)
                        eval_count = int(meta.get("eval_count") or len(buf))
                        # Surface truncation to the caller: Ollama sets
                        # done_reason="length" when it stopped because the output
                        # hit the context/num_predict ceiling (not a natural EOS).
                        # Without this the caller saves a half-finished file and
                        # believes it is complete.
                        if meta_out is not None:
                            _dr = str(meta.get("done_reason") or "")
                            meta_out.update({"done_reason": _dr,
                                             "eval_count": eval_count,
                                             "truncated": _dr == "length"})
                        # Residency check — is the model actually ALL on the GPU?
                        # A partial load is invisible in every other signal: the
                        # call succeeds, just ~3x slower. Sampled (a fraction of
                        # calls, plus always when throughput looks bad) so it adds
                        # one cheap GET rather than a round trip per generation.
                        _resid = None
                        try:
                            _tps = eval_count / elapsed if elapsed > 0 else 0
                            # Sample without pulling in `random`: the sub-second
                            # part of the clock is an adequate uniform source here.
                            _samp = (time.time() % 1.0) < _SPILL_SAMPLE
                            if eval_count > 8 and (_tps < _SPILL_TPS_HINT or _samp):
                                _resid = await note_ctx_residency(chosen, mdl,
                                                                  _merged_opts.get("num_ctx") or 0)
                        except Exception:
                            _resid = None
                        log.info("ollama_done [%s] %.2fs eval_count=%s tok/s=%.1f%s caller=%s:%s",
                                 req_id, elapsed, eval_count,
                                 (eval_count / elapsed if elapsed > 0 else 0),
                                 (f" GPU={_resid['resident_pct']}%%"
                                  + (" SPILL→CPU" if _resid.get("spilled") else "")
                                  if _resid else ""),
                                 caller["caller_file"], caller["caller_func"])
                        req_entry.update({"status": "done", "elapsed_s": elapsed,
                                          "eval_count": eval_count, "tokens": len(buf),
                                          "tok_per_s": round(eval_count / elapsed, 2) if elapsed > 0 else 0,
                                          "gpu_resident_pct": (_resid or {}).get("resident_pct"),
                                          "cpu_spill": bool((_resid or {}).get("spilled"))})
                        _ollama_log_append(req_entry)
                        _route_stats_update(mdl, chosen, eff_job_type,
                                            elapsed, eval_count, prompt_chars)
                        try:
                            await emit_event({
                                "type": "ollama.request_done", "req_id": req_id,
                                "model": mdl, "instance_id": chosen,
                                "caller_file": caller["caller_file"],
                                "caller_func": caller["caller_func"],
                                "elapsed_s": elapsed, "eval_count": eval_count,
                                "token_count": len(buf),
                                "job_type": eff_job_type,
                                "est_seconds": routing_info.get("est_seconds"),
                                "tok_per_s": (round(eval_count / elapsed, 2)
                                              if elapsed > 0 else 0),
                                "gpu_resident_pct": (_resid or {}).get("resident_pct"),
                                "cpu_spill": bool((_resid or {}).get("spilled")),
                                "num_ctx": _merged_opts.get("num_ctx"),
                            })
                        except Exception:
                            pass
                        return result
        finally:
            _hb_stop.set()
            _hb_task.cancel()
    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        err_str = _err_text(e)
        log.error("ollama_generate [%s] FAILED after %.2fs inst=%s caller=%s:%s err=%s",
                  req_id, elapsed, chosen,
                  caller["caller_file"], caller["caller_func"], err_str)
        # Don't flip a node offline on a single error — a slow generation that
        # exceeds the HTTP timeout (common for large-context jobs on CPU nodes)
        # would otherwise cascade the whole node out of rotation and starve
        # every subsequent request. Only mark offline after repeated failures;
        # the health loop re-probes every 20s and clears errors on success.
        inst["errors"] += 1
        if inst["errors"] >= 3:
            inst["status"] = "offline"
        req_entry.update({"status": "error", "elapsed_s": elapsed,
                          "error": err_str})
        _ollama_log_append(req_entry)
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "elapsed_s": elapsed, "error": err_str,
                "error_type": type(e).__name__,
            })
        except Exception:
            pass
        # Failover order: prefer IDLE nodes. Falling a job over onto a node that is
        # already generating just queues it behind that work (ollama serialises per
        # node), so the retry inherits the original wait instead of escaping it.
        _fb_order = sorted(
            OLLAMA_INSTANCES.items(),
            key=lambda kv: (kv[1].get("in_use", 0), kv[1].get("priority", 99)))
        for fb_id, fb_inst in _fb_order:
            if fb_id==chosen or fb_inst["status"]!="online": continue
            # ── Don't let a BACKGROUND job displace foreground work ─────────
            # Observed live: the dream director's 12.7k-char think timed out on a
            # CPU node and failed over onto the GPU that was mid-generation for an
            # agentic loop. The loop's own 12-second call then starved behind it
            # and died at the 900s ceiling, and the loop degraded onto a CPU node.
            # One slow background call became two failed calls plus a downgraded
            # run. A background job may only fail over onto an IDLE node; if there
            # isn't one it waits for its next scheduled tick instead.
            if _is_background_job(eff_job_type) and fb_inst.get("in_use", 0) > 0:
                log.info("ollama_fallback [%s] skipping %s — busy (in_use=%s) and '%s' "
                         "is a background job; not displacing foreground work",
                         req_id, fb_id, fb_inst.get("in_use", 0), eff_job_type)
                continue
            # Skip nodes that don't have the model
            fb_models = fb_inst.get("models", [])
            mdl_base = mdl.split(":")[0]
            if fb_models and not any(m == mdl or m.startswith(mdl+":") or m.split(":")[0] == mdl_base for m in fb_models):
                log.debug("ollama_fallback [%s] skipping %s — model '%s' not available", req_id, fb_id, mdl)
                continue
            try:
                log.info("ollama_fallback [%s] trying %s", req_id, fb_id)
                # Route the fallback through the SAME per-instance semaphore +
                # in_use accounting as a primary request, so it honours the
                # "one in-flight request per node" contract instead of piling an
                # extra concurrent generation onto the fallback node.
                fb_inst["in_use"] = fb_inst.get("in_use", 0) + 1
                try:
                    # Same only-fail-when-idle semantics as the primary path:
                    # unbounded queue wait UNLESS the caller passed an explicit
                    # timeout (see the primary path's _ollama_slot call above),
                    # streamed response so the read timeout is a stall detector
                    # rather than a total cap.
                    async with _ollama_slot(fb_id, timeout=timeout):
                        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=httpx.Timeout(gen_timeout, connect=15.0)) as c:
                            async with c.stream("POST", f"{fb_inst['url']}/api/generate",
                                                json={**body, "stream": True}) as r:
                                if r.status_code != 200:
                                    err_detail = (await r.aread()).decode("utf-8", errors="replace")[:300]
                                    log.warning("ollama_fallback [%s] %s returned %d: %s", req_id, fb_id, r.status_code, err_detail)
                                    continue
                                fbuf=[]; ftbuf=[]
                                async for line in r.aiter_lines():
                                    if not line: continue
                                    try:
                                        d=json.loads(line)
                                        if d.get("response"): fbuf.append(d["response"])
                                        elif d.get("thinking"): ftbuf.append(d["thinking"])
                                    except Exception: pass
                        fb_elapsed = round(time.time() - t_start, 2)
                        log.info("ollama_fallback [%s] OK on %s after %.2fs",
                                 req_id, fb_id, fb_elapsed)
                        req_entry.update({"status": "done_fallback",
                                          "fallback_instance": fb_id,
                                          "elapsed_s": fb_elapsed})
                        return "".join(fbuf) or "".join(ftbuf)
                finally:
                    fb_inst["in_use"] = max(0, fb_inst.get("in_use", 1) - 1)
            except Exception: pass
        return ""
    except asyncio.CancelledError:
        # Caller cancelled us (loop/dream preemption, client abort, a wrapping
        # wait_for). `except Exception` doesn't catch this, so emit a terminal
        # event to avoid a stuck-"running" zombie, then re-raise.
        elapsed = round(time.time() - t_start, 2)
        req_entry.update({"status": "error", "elapsed_s": elapsed,
                          "error": "cancelled by caller (timeout/abort)"})
        _ollama_log_append(req_entry)
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "elapsed_s": elapsed, "error": "cancelled by caller",
                "error_type": "CancelledError",
            })
        except Exception:
            pass
        raise
    finally:
        inst["in_use"]=max(0,inst["in_use"]-1)


def _ollama_log_append(entry: dict):
    """Append to the in-process ring buffer."""
    _OLLAMA_REQUEST_LOG.append(entry)
    if len(_OLLAMA_REQUEST_LOG) > _OLLAMA_REQUEST_LOG_MAX:
        del _OLLAMA_REQUEST_LOG[:-_OLLAMA_REQUEST_LOG_MAX]


async def ollama_embed(text: str, model: Optional[str] = None,
                       instance_id: Optional[str] = None,
                       prefer_gpu: bool = False,
                       timeout: Optional[float] = None,
                       normalize: bool = False,
                       provider: Optional[str] = None) -> Optional[List[float]]:
    """De-duplicating wrapper around the real embed (_ollama_embed_impl).

    The same text was being embedded 2×+ by concurrent callers / retries; each
    duplicate is a full /api/embed on the serialised embed node. This collapses
    them: a fresh cached vector is reused; an identical embed already in flight
    is awaited instead of firing a second request. A caller pinning a specific
    instance bypasses the cache (it wants that node's exact result). Key
    includes model/normalize/provider since those change the vector.
    """
    if not text or not text.strip():
        return None
    if instance_id:
        # Explicit pin — don't serve from a cache that may hold another node's
        # vector; run it directly.
        return await _ollama_embed_impl(text, model, instance_id, prefer_gpu,
                                        timeout, normalize, provider)

    _mdl = model or OLLAMA_EMBED_MODEL
    _key = (f"{_mdl}|{int(bool(normalize))}|{provider or ''}|"
            + hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24])

    hit = _EMBED_RESULT_CACHE.get(_key)
    if hit is not None:
        _vec, _ts = hit
        if (time.monotonic() - _ts) < _EMBED_CACHE_TTL:
            return list(_vec) if _vec is not None else None
        _EMBED_RESULT_CACHE.pop(_key, None)

    inflight = _EMBED_INFLIGHT.get(_key)
    if inflight is not None:
        try:
            _vec = await asyncio.shield(inflight)   # our cancel ≠ shared cancel
            return list(_vec) if _vec is not None else None
        except Exception:
            return None

    loop = asyncio.get_event_loop()
    fut: "asyncio.Future" = loop.create_future()
    _EMBED_INFLIGHT[_key] = fut
    try:
        vec = await _ollama_embed_impl(text, model, instance_id, prefer_gpu,
                                       timeout, normalize, provider)
        if vec is not None:
            _EMBED_RESULT_CACHE[_key] = (list(vec), time.monotonic())
            if len(_EMBED_RESULT_CACHE) > _EMBED_CACHE_MAX:
                # Evict the oldest ~10% by timestamp (cheap, infrequent).
                for _k, _ in sorted(_EMBED_RESULT_CACHE.items(),
                                    key=lambda kv: kv[1][1])[:_EMBED_CACHE_MAX // 10 + 1]:
                    _EMBED_RESULT_CACHE.pop(_k, None)
        if not fut.done():
            fut.set_result(vec)
        return vec
    finally:
        if _EMBED_INFLIGHT.get(_key) is fut:
            _EMBED_INFLIGHT.pop(_key, None)
        # ALWAYS resolve so shielded waiters never hang (incl. cancellation,
        # which skips the normal return path above).
        if not fut.done():
            fut.set_result(None)


async def _ollama_embed_impl(text: str, model: Optional[str] = None,
                       instance_id: Optional[str] = None,
                       prefer_gpu: bool = False,
                       timeout: Optional[float] = None,
                       normalize: bool = False,
                       provider: Optional[str] = None) -> Optional[List[float]]:
    """Generate a text embedding via Ollama, with full job logging.

    Tries /api/embed (Ollama ≥0.4) first, then /api/embeddings (older).
    Emits ollama.request / ollama.request_done / ollama.request_error events
    so every embed call appears in the Workers panel Jobs tab.

    Parameters
    ----------
    text         : str   — text to embed (truncated to 4096 chars)
    model        : str   — embedding model (default: OLLAMA_EMBED_MODEL)
    instance_id  : str   — pin to a specific Ollama instance
    prefer_gpu   : bool  — prefer GPU instances for routing
    timeout      : float — HTTP timeout in seconds (default OLLAMA_EMBED_TIMEOUT,
                           generous because Ollama may queue the embed behind a
                           running generation on the same node)
    normalize    : bool  — L2-normalise the returned vector

    Returns
    -------
    List[float] or None on failure.
    """
    if not text or not text.strip():
        return None

    # §1: opt-in fastembed (ONNX Runtime CPU) backend. Off by default — taken
    # when the effective provider (per-call `provider` override or the global
    # EMBED_PROVIDER) is "fastembed". Any failure (lib missing, model download
    # error, …) falls through to the Ollama path below, so this is fully
    # back-compatible. provider="ollama" forces the Ollama path even when the
    # global default is fastembed (used by the embed.provider.check migration guard).
    if (provider or EMBED_PROVIDER) == "fastembed":
        try:
            import Vera.vera.fabric.fastembed_provider as _fe
            if _fe.available():
                vec = await asyncio.get_event_loop().run_in_executor(
                    None, _fe.embed, text[:4096])
                if vec:
                    if normalize:
                        _n = (sum(v * v for v in vec)) ** 0.5 or 1.0
                        vec = [v / _n for v in vec]
                    return vec
        except Exception as _fe_err:
            log.debug("fastembed provider failed, falling back to Ollama: %s", _fe_err)

    mdl = model or OLLAMA_EMBED_MODEL
    # Apply runtime embed config: prefer_gpu / pinned_instance from UI settings
    eff_prefer_gpu = prefer_gpu or _EMBED_PREFER_GPU
    eff_instance   = instance_id or _EMBED_INSTANCE_ID
    chosen = pick_instance(prefer_gpu=eff_prefer_gpu, instance_id=eff_instance,
                           model=mdl, job_type="embedding") or "cpu-246"
    inst = OLLAMA_INSTANCES.get(chosen)
    if not inst:
        return None
    url = inst.get("url", "")
    # Reserve the routing slot synchronously (before the emit_event await below)
    # so concurrent embed calls see this node's raised load and spread out,
    # rather than all picking the same "least busy" node across the await gap.
    inst["in_use"] = inst.get("in_use", 0) + 1

    caller = _ollama_caller_info()
    req_id = str(uuid.uuid4())[:12]
    t_start = time.time()
    text_preview = (text or "")[:120].replace("\n", " ")

    log.info(
        "ollama_embed [%s] model=%s inst=%s caller=%s:%s text=%s",
        req_id, mdl, chosen,
        caller["caller_file"], caller["caller_func"],
        text_preview,
    )

    req_entry = {
        "req_id": req_id, "model": mdl, "instance": chosen,
        "caller_file": caller["caller_file"], "caller_func": caller["caller_func"],
        "prompt_preview": f"[embed] {text_preview}", "ts": now_iso(),
        "status": "running",
    }

    # NOTE: in_use already reserved above (right after pick_instance).
    # Resolve the effective timeout: caller override wins, else the generous
    # env default — an embed queued server-side behind a generation must WAIT,
    # not fail; only a truly unresponsive node should trip this.
    _emb_timeout = httpx.Timeout(float(timeout) if timeout else OLLAMA_EMBED_TIMEOUT,
                                 connect=10.0)

    try:
        # Emit inside the try so a cancellation at this await still releases the
        # in_use slot in the finally below (same leak fix as ollama_generate).
        try:
            await emit_event({
                "type":         "ollama.request",
                "req_id":       req_id,
                "model":        mdl,
                "instance_id":  chosen,
                "instance_url": url,
                "caller_file":  caller["caller_file"],
                "caller_func":  caller["caller_func"],
                "caller_module": caller["caller_module"],
                "cap_name":     caller["cap_name"] or "ollama.embed",
                "prompt_preview": f"[embed] {text_preview}",
                "prompt_full":  f"[embed] {(text or '')[:16000]}",
                "json_mode":    False,
                "prefer_gpu":   prefer_gpu,
                "streaming":    False,
            })
        except Exception:
            pass
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=_emb_timeout) as c:
            # Try new endpoint first (Ollama ≥0.4)
            r = await c.post(f"{url}/api/embed",
                             json={"model": mdl, "input": text[:4096]})
            if r.status_code != 200:
                # Fall back to legacy endpoint
                r = await c.post(f"{url}/api/embeddings",
                                 json={"model": mdl, "prompt": text[:4096]})
            if r.status_code != 200:
                elapsed = round(time.time() - t_start, 2)
                err_str = f"HTTP {r.status_code} from {chosen}"
                if r.text:
                    err_str += f": {r.text[:200]}"
                log.warning("ollama_embed [%s] FAILED status=%d inst=%s",
                            req_id, r.status_code, chosen)
                req_entry.update({"status": "error", "elapsed_s": elapsed,
                                  "error": err_str})
                _ollama_log_append(req_entry)
                try:
                    await emit_event({
                        "type": "ollama.request_error", "req_id": req_id,
                        "model": mdl, "instance_id": chosen,
                        "caller_file": caller["caller_file"],
                        "caller_func": caller["caller_func"],
                        "elapsed_s": elapsed,
                        "error": err_str,
                    })
                except Exception:
                    pass
                return None

            data = r.json()
            emb = data.get("embeddings")
            if emb and isinstance(emb, list):
                vec = emb[0] if isinstance(emb[0], list) else emb
            else:
                vec = data.get("embedding")

            if not vec:
                elapsed = round(time.time() - t_start, 2)
                req_entry.update({"status": "error", "elapsed_s": elapsed,
                                  "error": "no_vector_in_response"})
                _ollama_log_append(req_entry)
                try:
                    await emit_event({
                        "type": "ollama.request_error", "req_id": req_id,
                        "model": mdl, "instance_id": chosen,
                        "caller_file": caller["caller_file"],
                        "caller_func": caller["caller_func"],
                        "elapsed_s": elapsed, "error": "no_vector_in_response",
                    })
                except Exception:
                    pass
                return None

            # Optionally L2-normalise
            if normalize:
                try:
                    import numpy as np
                    arr = np.array(vec, dtype="float32")
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        vec = (arr / norm).tolist()
                except ImportError:
                    pass  # skip normalisation if numpy unavailable

            elapsed = round(time.time() - t_start, 2)
            log.info("ollama_embed_done [%s] %.2fs dim=%d caller=%s:%s",
                     req_id, elapsed, len(vec),
                     caller["caller_file"], caller["caller_func"])
            req_entry.update({"status": "done", "elapsed_s": elapsed,
                              "dimensions": len(vec)})
            _ollama_log_append(req_entry)
            try:
                await emit_event({
                    "type": "ollama.request_done", "req_id": req_id,
                    "model": mdl, "instance_id": chosen,
                    "caller_file": caller["caller_file"],
                    "caller_func": caller["caller_func"],
                    "elapsed_s": elapsed, "dimensions": len(vec),
                })
            except Exception:
                pass
            return vec

    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        err_str = _err_text(e)
        log.error("ollama_embed [%s] FAILED after %.2fs inst=%s err=%s",
                  req_id, elapsed, chosen, err_str)
        inst["errors"] = inst.get("errors", 0) + 1
        req_entry.update({"status": "error", "elapsed_s": elapsed,
                          "error": err_str})
        _ollama_log_append(req_entry)
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "elapsed_s": elapsed, "error": err_str,
                "error_type": type(e).__name__,
            })
        except Exception:
            pass

        # Fallback: try other online instances
        for fb_id, fb_inst in OLLAMA_INSTANCES.items():
            if fb_id == chosen or fb_inst.get("status") != "online":
                continue
            try:
                log.info("ollama_embed_fallback [%s] trying %s", req_id, fb_id)
                async with httpx.AsyncClient(verify=_SSL_CTX, timeout=_emb_timeout) as c:
                    r = await c.post(f"{fb_inst['url']}/api/embed",
                                     json={"model": mdl, "input": text[:4096]})
                    if r.status_code != 200:
                        r = await c.post(f"{fb_inst['url']}/api/embeddings",
                                         json={"model": mdl, "prompt": text[:4096]})
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    emb = data.get("embeddings")
                    if emb and isinstance(emb, list):
                        vec = emb[0] if isinstance(emb[0], list) else emb
                    else:
                        vec = data.get("embedding")
                    if vec:
                        fb_elapsed = round(time.time() - t_start, 2)
                        log.info("ollama_embed_fallback [%s] OK on %s dim=%d %.2fs",
                                 req_id, fb_id, len(vec), fb_elapsed)
                        req_entry.update({"status": "done_fallback",
                                          "fallback_instance": fb_id,
                                          "elapsed_s": fb_elapsed,
                                          "dimensions": len(vec)})
                        _ollama_log_append(req_entry)
                        return vec
            except Exception:
                pass
        return None
    except asyncio.CancelledError:
        # A caller cancelled us (e.g. execute_query wraps _embed in a 10s
        # wait_for). `except Exception` above does NOT catch CancelledError, so
        # without this the job stayed "running" forever → 1200s stale_timeout
        # zombies in the Ollama log. Emit a terminal event, then re-raise so the
        # cancellation still propagates.
        elapsed = round(time.time() - t_start, 2)
        req_entry.update({"status": "error", "elapsed_s": elapsed,
                          "error": "cancelled by caller (timeout/abort)"})
        _ollama_log_append(req_entry)
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": req_id,
                "model": mdl, "instance_id": chosen,
                "caller_file": caller["caller_file"],
                "caller_func": caller["caller_func"],
                "elapsed_s": elapsed, "error": "cancelled by caller",
                "error_type": "CancelledError",
            })
        except Exception:
            pass
        raise
    finally:
        inst["in_use"] = max(0, inst.get("in_use", 1) - 1)

# ─────────────────────────────────────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────────────────────────────────────
def new_id()  -> str: return str(uuid.uuid4())
def now_iso() -> str: return datetime.utcnow().isoformat()+"Z"

# ── Type annotation → JSON-Schema helpers ─────────────────────────────────────
import typing as _typing

_SCHEMA_TMAP: Dict[Any, str] = {
    int: "integer", float: "number", bool: "boolean",
    str: "string",  list: "array",   dict: "object",
    bytes: "string",
}

# Mapping of common type-name strings → JSON-schema types. Used when
# `from __future__ import annotations` is active (PEP 563), in which case
# annotations are stored as strings rather than resolved class objects.
_STRING_ANN_MAP: Dict[str, dict] = {
    "int":     {"type": "integer"},
    "float":   {"type": "number"},
    "bool":    {"type": "boolean"},
    "str":     {"type": "string"},
    "bytes":   {"type": "string"},
    "list":    {"type": "array"},
    "List":    {"type": "array"},
    "dict":    {"type": "object"},
    "Dict":    {"type": "object"},
    "Any":     {},
    "any":     {},
}


def _resolve_string_annotation(s: str) -> Optional[dict]:
    """Resolve a string annotation (from PEP 563) to a JSON-schema fragment.

    Handles:
      - Bare names: "int", "float", "bool", "str", "list", "dict"
      - Optional[T] / Optional["T"] → unwrap
      - List[T] / Dict[K,V] / Tuple[...] → array / object / array
      - Union[A, B, ...] → anyOf
      - "Optional[Dict[str,Any]]" → object (unwraps Optional)
      - typing-style "List[int]" with item types
    """
    s = s.strip()
    if not s:
        return None

    # Drop quotes for forward-references like "int"
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1].strip()

    # Bare type-name lookup
    if s in _STRING_ANN_MAP:
        return dict(_STRING_ANN_MAP[s])

    # Generic forms: Optional[X], List[X], Dict[K,V], Union[A,B], Tuple[...]
    import re as _re
    m = _re.match(r"^([A-Za-z_][\w.]*)\[(.+)\]$", s)
    if m:
        gname = m.group(1).split(".")[-1]   # strip module prefix like typing.
        inner = m.group(2).strip()
        if gname in ("Optional",):
            return _resolve_string_annotation(inner) or {"type": "string"}
        if gname in ("List", "list", "Sequence", "Iterable", "Tuple", "tuple", "Set", "set", "FrozenSet"):
            schema: dict = {"type": "array"}
            inner_schema = _resolve_string_annotation(inner)
            if inner_schema and "type" in inner_schema:
                schema["items"] = inner_schema
            return schema
        if gname in ("Dict", "dict", "Mapping"):
            return {"type": "object"}
        if gname in ("Union",):
            # Split by commas at top level
            parts = _split_top_level(inner)
            non_none = [p.strip() for p in parts if p.strip() not in ("None", "type(None)")]
            if len(non_none) == 1:
                return _resolve_string_annotation(non_none[0])
            return {"anyOf": [_resolve_string_annotation(p) or {"type": "string"} for p in non_none]}

    # Otherwise treat as opaque object (forward reference to a custom class)
    return None


def _split_top_level(s: str, sep: str = ",") -> List[str]:
    """Split string by separator at top level only (respect bracket nesting)."""
    parts: List[str] = []
    depth = 0
    cur = []
    for ch in s:
        if ch in "[(":
            depth += 1
            cur.append(ch)
        elif ch in "])":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _resolve_annotation(ann) -> Optional[dict]:
    """
    Recursively convert a Python type annotation to a JSON-Schema fragment.

    Handles:
      - Plain types: int, float, bool, str, list, dict
      - Optional[T]  → unwrap, mark not-required at call site
      - List[T]      → {"type":"array","items":<T>}
      - Dict[K,V]    → {"type":"object"}
      - Union[A,B]   → {"anyOf":[<A>,<B>]}  (non-Optional multi-type)
      - typing.Any   → {}  (no constraint)
      - Unannotated  → None  (caller falls back to default-value inference)
      - PEP 563 string annotations (from __future__ import annotations):
        "int", "Optional[float]", "List[str]", etc. resolved via name lookup.
    """
    if ann is inspect.Parameter.empty:
        return None

    # PEP 563: when `from __future__ import annotations` is active, annotations
    # are stored as STRINGS rather than resolved class objects. Handle that
    # FIRST so we don't fall through to the string fallback at the bottom.
    if isinstance(ann, str):
        return _resolve_string_annotation(ann)

    # Plain type
    if ann in _SCHEMA_TMAP:
        return {"type": _SCHEMA_TMAP[ann]}

    # typing.Any — no constraint
    if ann is _typing.Any:
        return {}

    origin = getattr(ann, "__origin__", None)
    args   = getattr(ann, "__args__", ()) or ()

    # Union / Optional
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            # Optional[X] — resolve inner, caller handles required flag
            return _resolve_annotation(non_none[0])
        # Multi-type Union → anyOf
        return {"anyOf": [_resolve_annotation(a) or {"type": "string"} for a in non_none]}

    # List[T] / list
    if origin in (list,) or origin is getattr(_typing, "List", None):
        schema: dict = {"type": "array"}
        if args:
            inner = _resolve_annotation(args[0])
            if inner is not None:
                schema["items"] = inner
        return schema

    # Dict[K,V] / dict
    if origin in (dict,) or origin is getattr(_typing, "Dict", None):
        return {"type": "object"}

    # Tuple → array
    if origin is tuple or origin is getattr(_typing, "Tuple", None):
        return {"type": "array"}

    # Bare subclass (catches subclasses of int, str, etc.)
    if isinstance(ann, type):
        if issubclass(ann, bool):  return {"type": "boolean"}
        if issubclass(ann, int):   return {"type": "integer"}
        if issubclass(ann, float): return {"type": "number"}
        if issubclass(ann, str):   return {"type": "string"}
        if issubclass(ann, list):  return {"type": "array"}
        if issubclass(ann, dict):  return {"type": "object"}

    return {"type": "string"}   # safe fallback


def _infer_from_default(default) -> Optional[dict]:
    """Infer a JSON-Schema type from a default value's Python type."""
    if default is inspect.Parameter.empty or default is None:
        return None
    t = type(default)
    if t is bool:   return {"type": "boolean"}   # before int — bool is int subclass
    if t is int:    return {"type": "integer"}
    if t is float:  return {"type": "number"}
    if t is str:    return {"type": "string"}
    if t is list:   return {"type": "array"}
    if t is dict:   return {"type": "object"}
    return None


def _is_optional_annotation(ann) -> bool:
    """Return True if the annotation is Optional[X] i.e. Union[X, None].

    Also handles PEP 563 string annotations like "Optional[int]" which appear
    when modules use `from __future__ import annotations`.
    """
    if ann is inspect.Parameter.empty:
        return False
    # PEP 563: string-form annotations
    if isinstance(ann, str):
        s = ann.strip()
        # Drop quotes from forward-references
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            s = s[1:-1].strip()
        if s.startswith("Optional[") and s.endswith("]"):
            return True
        if s.startswith("Union[") and s.endswith("]"):
            inner = s[len("Union["):-1]
            parts = _split_top_level(inner)
            for p in parts:
                if p.strip() in ("None", "type(None)", "NoneType"):
                    return True
        return False
    origin = getattr(ann, "__origin__", None)
    args   = getattr(ann, "__args__", ()) or ()
    return origin is Union and type(None) in args


def generate_schema(func: Callable) -> dict:
    """
    Derive a JSON-Schema object from a function's type annotations.

    Resolution order per parameter:
      1. Annotation via _resolve_annotation — handles Optional, List, Dict,
         Union, nested generics, and plain types correctly.
      2. Default-value inference — catches unannotated params that have typed
         defaults (e.g. ``limit=10`` with no annotation → integer).
      3. Falls back to {"type": "string"} — always produces valid schema.

    Required: a param is required only when it has NO default value AND its
    annotation is not Optional[...] / Union[..., None].
    """
    sig   = inspect.signature(func)
    props: dict = {}
    req:   list = []
    _SKIP = {"trace_id", "self", "request", "kwargs", "stream_cb"}

    for k, v in sig.parameters.items():
        if k in _SKIP:
            continue

        ann     = v.annotation
        default = v.default

        # 1. Try annotation
        prop = _resolve_annotation(ann)

        # 2. Fall back to default-value type inference
        if prop is None:
            prop = _infer_from_default(default) or {"type": "string"}

        prop = dict(prop)   # ensure mutable copy

        # Add default when it's a concrete value
        if default is not inspect.Parameter.empty and default is not None:
            prop["default"] = default

        props[k] = prop

        # Required: no default AND not Optional
        if default is inspect.Parameter.empty and not _is_optional_annotation(ann):
            req.append(k)

    return {"type": "object", "properties": props, "required": req}


def _merge_schema(auto: dict, override: dict) -> dict:
    """
    Deep-merge an explicit schema override onto the auto-generated schema.

    Strategy:
      - `required` comes from the real function signature (auto) — the source
        of truth for what Python actually needs. The override may extend it.
      - `properties` are merged per-property: auto supplies `type` (and
        `items`, `anyOf`, etc.) derived from the annotation; the override
        enriches with `description`, `enum`, `format`, constraints, etc.
        Override keys win over auto keys on a per-property basis.
      - Override-only properties (e.g. a dict param described in detail) are
        accepted as-is.
      - Top-level keys beyond `properties`/`required` come from the override.
    """
    merged_props: dict = {}

    for pname, pauto in auto.get("properties", {}).items():
        merged_props[pname] = dict(pauto)

    for pname, pover in override.get("properties", {}).items():
        if pname in merged_props:
            merged_props[pname] = {**merged_props[pname], **pover}
        else:
            merged_props[pname] = dict(pover)

    merged_req = sorted(set(auto.get("required", [])) | set(override.get("required", [])))

    return {**override, "type": "object",
            "properties": merged_props, "required": merged_req}


# ─────────────────────────────────────────────────────────────────────────────
# ENUM / MULTIPLE-CHOICE HELPERS
#
# Options for a multiple-choice arg used to live only in prose descriptions, so
# neither UIs nor the LLM planner could see the allowed set. These build the
# `schema=` override that declares real JSON-Schema `enum`s, which then flow to
# every consumer: /mcp/tools, the cap-hub auto-form (renders a <select>), and
# the LLM-facing capability signatures (see _format_param_sig).
# ─────────────────────────────────────────────────────────────────────────────
def enum_schema(**choices) -> dict:
    """Build a `schema=` override declaring single-select option lists.

        @capability("tts.synthesize", ...,
            schema=enum_schema(engine=["", "kokoro", "coqui"]))

    Each value is either a plain list of options, or a dict to add more
    per-field keys alongside the enum:

        enum_schema(mode={"enum": ["a", "b"], "description": "..."})

    The auto-generated type/default from the Python signature is preserved by
    _merge_schema; you only supply the choices.
    """
    props: dict = {}
    for name, opts in choices.items():
        props[name] = dict(opts) if isinstance(opts, dict) else {"enum": list(opts)}
    return {"properties": props}


def multi_enum_schema(**choices) -> dict:
    """Build a `schema=` override declaring multi-select (array-of-enum) args.
    Use for a param the caller may pick SEVERAL of — annotate it ``List[str]``:

        @capability("dream.render", ...,
            schema=multi_enum_schema(channels=["email", "chat", "project"]))

    Renders as a <select multiple> in the cap-hub form and as ``name:[str]=a|b``
    in the LLM signature.
    """
    props: dict = {}
    for name, opts in choices.items():
        props[name] = {"type": "array", "items": {"enum": list(opts)}}
    return {"properties": props}


def enum_options(prop: dict) -> list:
    """Return a param's declared option list — single-select (``enum``) or
    multi-select (``items.enum`` on an array) — or ``[]`` if it has none.
    One accessor so every consumer (LLM signatures, correction prompts, UIs)
    reads choices the same way."""
    items = prop.get("items") or {}
    return list(prop.get("enum") or items.get("enum") or [])


def _format_param_sig(pname: str, prop: dict, required: set) -> str:
    """Render one parameter for an LLM-facing capability signature, surfacing
    enum choices so the model picks from the allowed set instead of guessing.

        name:type            plain
        name:type!           required (no default)
        name:type=a|b|c      single-select enum
        name:[str]=a|b|c     multi-select (array of enum)
    """
    is_multi = prop.get("type") == "array"
    items    = prop.get("items") or {}
    tag      = f"[{items.get('type', 'str')}]" if is_multi else prop.get("type", "str")
    star     = "!" if pname in required else ""
    opts     = enum_options(prop)
    choices  = ""
    if opts:
        shown = "|".join(str(o) for o in opts[:12])
        if len(opts) > 12:
            shown += "|…"
        choices = f"={shown}"
    return f"{pname}:{tag}{star}{choices}"


def _remove_ws(ws,stream):
    try: WS_CONNECTIONS.remove((ws,stream))
    except ValueError: pass

# ─────────────────────────────────────────────────────────────────────────────
# EVENTS  (before capability decorator — it references emit_event)
# ─────────────────────────────────────────────────────────────────────────────
# ── Single-writer outbound queues ────────────────────────────────────────────
# A Starlette/uvicorn WebSocket allows exactly ONE concurrent writer. Vera
# writes to each socket from many tasks at once — the emit_event broadcast, the
# per-`call` result tasks, dag/plan tasks, and the receive-loop's own replies.
# Two overlapping send_json() calls (or, worse, a send_json cancelled mid-frame
# by asyncio.wait_for on timeout) desync the WebSocket framing, and uvicorn
# then aborts the connection — surfacing to the browser as code 1006 and the
# whole-UI reconnect storm. THE FIX: every outbound frame for a connection goes
# through its own bounded queue, drained by a single writer task that awaits
# each send to completion (never cancelled). One socket ⇒ one writer ⇒ no
# interleaving, no half-written frames. A slow client only backs up its OWN
# queue; when that fills we drop oldest (state is re-sent on the next event /
# poll) so one stuck tab can never stall the loop or another tab.
WS_OUT: Dict[int, asyncio.Queue] = {}   # id(ws) → outbound frame queue
_WS_OUT_MAX = int(os.environ.get("WS_OUT_QUEUE_MAX", "2000") or 2000)
_WS_CLOSE = object()                     # sentinel: tells a writer to stop


def _ws_enqueue(ws, payload: dict) -> None:
    """Queue one frame for a connection's writer (non-blocking, lossy on
    overflow). Safe to call from any task without awaiting."""
    q = WS_OUT.get(id(ws))
    if q is None:
        return   # connection has no writer (already torn down)
    try:
        q.put_nowait(payload)
    except asyncio.QueueFull:
        # Client isn't draining fast enough — drop the OLDEST frame to make
        # room for this newer one (latest state matters most; panels also poll).
        try:
            q.get_nowait()
            q.put_nowait(payload)
        except Exception:
            pass


async def _ws_writer(ws) -> None:
    """Drain one connection's outbound queue, sending each frame to completion.
    This is the ONLY coroutine that writes to `ws`. On any send failure the
    connection is unsubscribed and the writer exits."""
    q = WS_OUT.get(id(ws))
    if q is None:
        return
    try:
        while True:
            item = await q.get()
            if item is _WS_CLOSE:
                return
            try:
                await ws.send_json(item)
            except Exception:
                # Socket is gone/broken — stop writing and drop the connection
                # from every subscription so the broadcast loop skips it.
                WS_CONNECTIONS[:] = [p for p in WS_CONNECTIONS if p[0] is not ws]
                return
    finally:
        WS_OUT.pop(id(ws), None)


def _ws_start_writer(ws) -> None:
    if id(ws) not in WS_OUT:
        WS_OUT[id(ws)] = asyncio.Queue(maxsize=_WS_OUT_MAX)
        asyncio.create_task(_ws_writer(ws))


def _ws_stop_writer(ws) -> None:
    q = WS_OUT.get(id(ws))
    if q is not None:
        try:
            q.put_nowait(_WS_CLOSE)
        except Exception:
            WS_OUT.pop(id(ws), None)


# Back-compat shim: some call sites still reference _ws_send_bounded. It now
# just enqueues (the queue's writer performs the actual, uncancelled send).
async def _ws_send_bounded(ws, sub, payload: dict, timeout: float = 5.0):
    _ws_enqueue(ws, payload)

# ── Loop RESUME persistence ───────────────────────────────────────────────────
# A per-session replay buffer + run-state hash so a client that reloaded the page
# — or the app that RESTARTED (Redis outlives it, per the deployment) — can
# reconstruct an in-progress or finished agentic-loop run and re-attach to its
# live event stream. Keyed by session_id, which chat/loop runs already thread
# through. See /workshop/agent_loop/session_state + /reattach in dag_workshop.
_RESUME_TTL        = int(os.getenv("VERA_RESUME_TTL", "604800") or 604800)   # 7 days
_RESUME_MAX_EVENTS = int(os.getenv("VERA_RESUME_MAX_EVENTS", "4000") or 4000)

async def _persist_loop_event(event: dict, ev_json: str):
    """Best-effort append of an agent-loop event to its session's replay list +
    keep the run-state current. Skips transient high-frequency token events (the
    final cards carry the result); unrelated events (no session_id / not a loop
    event) are ignored."""
    if not REDIS:
        return
    sid   = event.get("session_id") or ""
    etype = event.get("type") or ""
    if not sid or not etype.startswith("agent_loop") or etype.endswith("_token"):
        return
    try:
        lkey, rkey = f"vera:loop:events:{sid}", f"vera:loop:run:{sid}"
        upd = {"updated_at": event.get("ts", now_iso())}
        if etype.endswith(".triage_start"):
            upd["status"] = "running"
            upd["started_at"] = event.get("ts", now_iso())
            if event.get("goal"):
                upd["goal"] = str(event.get("goal"))[:800]
        elif etype.endswith(".done"):
            upd["status"] = "done"
        elif etype.endswith(".error"):
            upd["status"] = "error"
        pipe = REDIS.pipeline()
        pipe.rpush(lkey, ev_json)
        pipe.ltrim(lkey, -_RESUME_MAX_EVENTS, -1)
        pipe.expire(lkey, _RESUME_TTL)
        pipe.hset(rkey, mapping=upd)
        pipe.expire(rkey, _RESUME_TTL)
        pipe.zadd("vera:loop:sessions", {sid: time.time()})
        await pipe.execute()
    except Exception as e:
        if "MISCONF" not in str(e):
            log.debug("resume persist: %s", e)


try:
    from Vera.vera.provenance import event_stamp as _prov_stamp
except Exception:                       # provenance must never break event emit
    def _prov_stamp(_ev):  # type: ignore
        return None


def _session_stamp(event: dict) -> None:
    """Stamp the session + caller that TRIGGERED this event (§5.1 provenance) —
    the 'which session' hop, so any event/error ties back not just to the commit
    (via _prov_stamp) but to the Claude-Code / chat / loop session behind it.
    Reuses the SAME resolution as the activity graph: an explicit session already
    on the event → the syslog trigger chain (set per /mcp/call) → the last-known
    _CURRENT_SESSION fallback. `via` records the caller KIND (e.g. 'mcp' = a
    Claude Code cap call). setdefault semantics; never overwrites; never raises —
    emitting an event must not depend on this succeeding."""
    try:
        if not event.get("sid") and not event.get("session_id"):
            sid = ""
            _sys = sys.modules.get("syslog")
            if _sys and hasattr(_sys, "get_trigger_chain"):
                sid = ((_sys.get_trigger_chain() or {}).get("session_id") or "")
            sid = sid or _CURRENT_SESSION
            if sid:
                event["sid"] = sid
        ck = CALLER_KIND.get("")
        if ck and not event.get("via"):
            event["via"] = ck
    except Exception:
        pass


async def emit_event(event: dict):
    event.setdefault("ts", now_iso())
    _prov_stamp(event)     # compact git {ver, br, dirty} → correlate any event to code
    _session_stamp(event)  # {sid, via} → correlate any event to the session that triggered it
    ev_json = json.dumps(event)
    if REDIS:
        try:
            # Stream — persistent, replayable history
            await REDIS.xadd(EVENT_STREAM, {"data": ev_json}, maxlen=5000)
            # Pub/sub — zero-latency fan-out for any live subscribers
            await REDIS.publish("vera:events:live", ev_json)
        except Exception as _re:
            if "MISCONF" not in str(_re):
                log.debug("emit_event Redis: %s", _re)
        await _persist_loop_event(event, ev_json)
    # Fan out to live subscribers by enqueueing to each connection's writer —
    # a non-blocking put, so a slow/stuck client can't stall this producer (or,
    # via a cancelled send, corrupt its socket). One writer per connection does
    # the actual sending.
    _seen = set()
    payload = {"type": "event", "data": event}
    for ws, sub in list(WS_CONNECTIONS):
        if sub == "__events__" and id(ws) not in _seen:
            _seen.add(id(ws))
            _ws_enqueue(ws, payload)

async def emit_stream(name: str, trace_id: str, payload: Any, capability: str):
    msg={"stream":name,"trace_id":trace_id,"capability":capability,"payload":payload,"ts":now_iso()}
    if REDIS:
        try:
            await asyncio.gather(
                REDIS.publish(f"stream:{name}",json.dumps(msg)),
                REDIS.xadd(f"vera:stream:{name}",{"data":json.dumps(msg)},maxlen=500),
                return_exceptions=True)
        except: pass
    # ── Cross-publish token streams to vera:events:live ──────────────────
    # The agent loop SSE bridge (workshop_agent_loop_stream) subscribes ONLY
    # to vera:events:live and looks for events with type "stream.token".
    # Without this bridge, emit_stream("tokens",...) from llm.generate /
    # ollama never reaches the agent loop output UI.
    if name == "tokens" and REDIS:
        try:
            ev = {"type": "stream.token",
                  "token": (payload.get("token","") if isinstance(payload,dict) else str(payload)),
                  "trace_id": trace_id, "capability": capability,
                  "source": "llm", "ts": now_iso()}
            await REDIS.publish("vera:events:live", json.dumps(ev))
        except: pass
    _sseen = set()
    _payload = {"type": "stream", "data": msg}
    for ws, sub in list(WS_CONNECTIONS):
        if sub == name and id(ws) not in _sseen:
            _sseen.add(id(ws))
            _ws_enqueue(ws, _payload)
    for cb in STREAM_SUBS.get(name,[]):
        asyncio.create_task(cb(msg))

def subscribe_stream(name: str, cb: Callable):
    STREAM_SUBS.setdefault(name,[]).append(cb)

# ─────────────────────────────────────────────────────────────────────────────
# REDIS DISPATCH
# ─────────────────────────────────────────────────────────────────────────────
async def dispatch_task(cap_name: str, payload: dict, trace_id: str) -> str:
    task_id=new_id()
    rec={"id":task_id,"capability":cap_name,"payload":json.dumps(payload),"trace_id":trace_id,"ts":now_iso()}
    if REDIS: await REDIS.xadd(TASK_STREAM,rec,maxlen=5000,approximate=True)
    else:
        cap=CAPABILITY_REGISTRY.get(cap_name)
        if cap: asyncio.create_task(_run_local(cap,task_id,payload,trace_id))
    return task_id

async def _run_local(cap,task_id,payload,trace_id):
    # Skip if already cancelled before it started.
    if task_id in CANCELLED_TASKS:
        CANCELLED_TASKS.discard(task_id)
        fut=PENDING_RESULTS.get(task_id)
        if fut and not fut.done(): fut.set_result({"error":"cancelled","cancelled":True})
        return
    inner = asyncio.ensure_future(cap["raw"](**payload,trace_id=trace_id))
    RUNNING_TASKS[task_id] = inner
    try:
        result = await inner
    except asyncio.CancelledError:
        result = {"error":"cancelled","cancelled":True}
        await emit_event({"type":"worker.cancelled","worker":"local","task":task_id})
    except Exception as e:
        result = {"error":str(e)}
    finally:
        RUNNING_TASKS.pop(task_id, None)
    fut=PENDING_RESULTS.get(task_id)
    if fut and not fut.done(): fut.set_result(result)


async def _is_task_cancelled(task_id: str) -> bool:
    """True (consuming the flag) if a task was cancelled while still queued —
    checked from both the local set and the shared Redis set."""
    if task_id in CANCELLED_TASKS:
        CANCELLED_TASKS.discard(task_id)
        return True
    if REDIS:
        try:
            if await REDIS.sismember(REDIS_CANCEL_SET, task_id):
                await REDIS.srem(REDIS_CANCEL_SET, task_id)
                return True
        except Exception:
            pass
    return False


async def cancel_listener():
    """Subscribe to the cross-host cancel channel and cancel any in-flight cap
    task running in THIS process whose id is published by cluster.job.stop."""
    while not REDIS:
        await asyncio.sleep(5)
    try:
        pubsub = REDIS.pubsub()
        await pubsub.subscribe(REDIS_CANCEL_CHANNEL)
    except Exception as e:
        log.warning("cancel_listener: subscribe failed: %s", e)
        return
    log.info("cancel_listener: subscribed to %s", REDIS_CANCEL_CHANNEL)
    try:
        async for msg in pubsub.listen():
            try:
                if msg.get("type") != "message":
                    continue
                raw = msg.get("data")
                tid = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
                t = RUNNING_TASKS.get(tid)
                if t and not t.done():
                    t.cancel()
                    log.info("cancel_listener: cancelled running task %s", tid)
            except Exception as e:
                log.debug("cancel_listener msg: %s", e)
    except Exception as e:
        log.debug("cancel_listener loop ended: %s", e)

# ── Per-cap timeout heuristics ──────────────────────────────────────────
# The previous 30s default was the root cause of nearly every "Task timed
# out" the user saw inside a DAG. LLM generation alone routinely takes
# 60-180s for non-trivial output; research and deep-research need minutes.
# We pick a budget based on the cap name prefix; only short, deterministic
# caps (echo, math, json) keep the snappy default.
_CAP_TIMEOUT_OVERRIDES = {
    # Long-running LLM work
    "llm.generate":     300.0,
    "llm.summarize":    240.0,
    "llm.analyze":      240.0,
    "llm.code_review":  240.0,
    "llm.translate":    180.0,
    "llm.classify":     180.0,
    "llm.chat":         300.0,
    # Research is async (returns a job_id quickly) but the cap call itself
    # may need to do auth/setup before returning. 60s is generous.
    "research.run":      60.0,
    "research.report":   60.0,
    "research.parallel": 60.0,
    "research.deep":     60.0,
    # DAG composer caps run a whole sub-DAG inside a single task — give
    # them headroom proportional to typical DAG depth.
    "dag.run":           600.0,
    "dag.plan_and_run":  600.0,
    "dag.from_goal":     180.0,
    # Browser caps (playwright launch + page load + interaction)
    "browser.screenshot": 90.0,
    "browser.content":    90.0,
    "browser.click":     120.0,
    "browser.form":      120.0,
    # Memory operations are usually fast but bulk ones can take a while
    "memory.search":      30.0,
    "memory.bulk_store": 120.0,
    
}

import inspect as _inspect

def _filter_kwargs_for_func(func: Callable, kw: dict) -> dict:
    """Return a copy of *kw* containing only keys that *func* actually accepts.

    If *func* has a **kwargs catch-all, all keys are passed through unchanged.
    Otherwise, any keys not in the function's signature are silently dropped
    (and logged at DEBUG level) so the caller never hits a TypeError from an
    unexpected keyword argument — a common issue when the dream/DAG system
    passes config dicts that have more keys than the target cap expects.
    """
    try:
        sig = _inspect.signature(func)
    except (ValueError, TypeError):
        return kw  # can't introspect — pass everything

    # If function has **kwargs, anything goes
    if any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kw

    accepted = set(sig.parameters.keys())
    filtered = {}
    dropped  = []
    for k, v in kw.items():
        if k in accepted:
            filtered[k] = v
        else:
            dropped.append(k)
    if dropped:
        log.debug("_filter_kwargs: dropped %s for %s", dropped, getattr(func, "__name__", "?"))
    return filtered


def _cap_timeout(name: str, default: float = 60.0) -> float:
    """Pick a sensible timeout for a cap by name. Specific override wins;
    otherwise the prefix gives a hint (llm.* → long, exec.* → medium).
    The caller can pass an explicit timeout in the payload to override."""
    if name in _CAP_TIMEOUT_OVERRIDES:
        return _CAP_TIMEOUT_OVERRIDES[name]
    if name.startswith("llm."):     return 240.0
    if name.startswith("research."):return 60.0
    if name.startswith("exec."):    return 300.0
    if name.startswith("ml."):      return 600.0
    if name.startswith("ide."):     return 120.0
    if name.startswith("browser."):return 90.0
    return default


async def wait_for_result(task_id: str, timeout: float = 60.0) -> Any:
    fut=asyncio.get_event_loop().create_future()
    PENDING_RESULTS[task_id]=fut
    try: return await asyncio.wait_for(asyncio.shield(fut),timeout)
    except asyncio.TimeoutError:
        PENDING_RESULTS.pop(task_id,None)
        return {"error":"timeout","task_id":task_id,"timeout_s":timeout}

async def worker_loop(worker_id: str):
    """
    Worker loop with Redis retry.  If Redis is unavailable at startup the loop
    waits and retries every 5 s rather than exiting — so workers on Host B will
    pick up tasks as soon as the Redis connection recovers.
    """
    global REDIS

    # ── Wait for Redis (retry indefinitely) ──────────────────────────────────
    while not REDIS:
        log.warning("Worker %s: Redis not connected — retrying in 5s "
                    "(check REDIS_URL=%s, Redis bind-address, and requirepass)", worker_id, REDIS_URL)
        await asyncio.sleep(5)
        if not REDIS and HAS_REDIS:
            try:
                candidate = aioredis.from_url(REDIS_URL, decode_responses=False,
                                              socket_connect_timeout=4,
                                              socket_timeout=4)
                await candidate.ping()
                REDIS = candidate
                log.info("Worker %s: Redis reconnected ✓", worker_id)
            except Exception as e:
                log.warning("Worker %s: Redis reconnect failed: %s", worker_id, e)

    # ── Register in shared Redis hash (visible to ALL hosts) ─────────────────
    reg = {
        "id":           worker_id,
        "status":       "starting",
        "capabilities": json.dumps(list(CAPABILITY_REGISTRY.keys())),
        "cap_count":    len(CAPABILITY_REGISTRY),
        "tasks_done":   0,
        "tasks_failed": 0,
        "started":      now_iso(),
        "host":         os.uname().nodename,
        "pid":          os.getpid(),
        "current_task": "",
        "task_started": "",
        "ollama_instance": "",
    }
    WORKER_REGISTRY[worker_id] = dict(reg)
    try:
        # Explicitly JSON-encode list/dict fields — str() produces invalid JSON
        redis_reg = {k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v))
                     for k, v in reg.items()}
        await REDIS.hset(f"vera:workers:{worker_id}", mapping=redis_reg)
        await REDIS.expire(f"vera:workers:{worker_id}", 120)
    except Exception as e:
        log.warning("Worker registry push failed: %s", e)

    try:
        await REDIS.xgroup_create(TASK_STREAM, GROUP_WORKERS, id="$", mkstream=True)
    except Exception:
        pass   # group already exists

    WORKER_REGISTRY[worker_id]["status"] = "idle"
    log.info("Worker %s ready (%d caps)", worker_id, len(CAPABILITY_REGISTRY))

    while True:
        # Refresh TTL and write all live fields — not just status
        try:
            w = WORKER_REGISTRY[worker_id]
            await REDIS.hset(f"vera:workers:{worker_id}", mapping={
                "status":       str(w.get("status", "idle")),
                "tasks_done":   str(w.get("tasks_done", 0)),
                "tasks_failed": str(w.get("tasks_failed", 0)),
                "current_task": str(w.get("current_task", "")),
                "task_started": str(w.get("task_started", "")),
                "ollama_instance": str(w.get("ollama_instance", "")),
            })
            await REDIS.expire(f"vera:workers:{worker_id}", 120)
        except Exception:
            pass

        try:
            resp = await REDIS.xreadgroup(
                GROUP_WORKERS, worker_id, {TASK_STREAM: ">"}, count=1, block=5000
            )
        except Exception as e:
            err_str = str(e)
            if "MISCONF" in err_str or "unable to persist" in err_str:
                # Redis can't write to disk — back off 30s and warn once per minute
                log.warning("worker: Redis persistence error (MISCONF) — "
                            "check disk space or set 'save \"\"' in redis.conf. "
                            "Backing off 30s.")
                await asyncio.sleep(30)
            elif "timeout" in err_str.lower() or "timed out" in err_str.lower():
                pass  # normal block-read keepalive, not an error
            else:
                log.error("xreadgroup: %s", e)
                await asyncio.sleep(2)
            continue

        if not resp:
            continue

        for _, messages in resp:
            for msg_id, data in messages:
                task_id  = data[b"id"].decode()
                cap_name = data[b"capability"].decode()
                payload  = json.loads(data[b"payload"])
                trace_id = data[b"trace_id"].decode()
                cap      = CAPABILITY_REGISTRY.get(cap_name)

                # Queued-cancel guard: if this task was stopped before a worker
                # picked it up, ack + discard it instead of running.
                if await _is_task_cancelled(task_id):
                    await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                    await REDIS.xdel(TASK_STREAM, msg_id)
                    await REDIS.xadd(RESULT_STREAM, {
                        "id": task_id, "error": "cancelled", "trace_id": trace_id,
                    })
                    await emit_event({"type": "worker.cancelled",
                                      "worker": worker_id, "task": task_id})
                    continue

                WORKER_REGISTRY[worker_id]["status"] = f"running:{cap_name}"
                WORKER_REGISTRY[worker_id]["current_task"] = cap_name
                WORKER_REGISTRY[worker_id]["task_started"] = now_iso()
                await emit_event({
                    "type": "worker.start", "worker": worker_id,
                    "capability": cap_name, "task": task_id,
                })

                if not cap:
                    # Check Redis for workers on OTHER hosts that have this cap
                    other_has_cap = False
                    try:
                        rkeys = await REDIS.keys("vera:workers:*")
                        for rk in rkeys:
                            raw_w = await REDIS.hgetall(rk)
                            if not raw_w: continue
                            wid_b = raw_w.get(b"id", b"")
                            other_wid = wid_b.decode() if isinstance(wid_b, bytes) else str(wid_b)
                            if other_wid == worker_id: continue
                            caps_b = raw_w.get(b"capabilities", b"[]")
                            caps_str = caps_b.decode() if isinstance(caps_b, bytes) else str(caps_b)
                            try:
                                other_caps = json.loads(caps_str)
                            except Exception:
                                try:
                                    import ast as _ast
                                    other_caps = _ast.literal_eval(caps_str)
                                except Exception:
                                    other_caps = []
                            if cap_name in other_caps:
                                other_has_cap = True
                                break
                    except Exception as e:
                        log.debug("other_has_cap Redis check: %s", e)
                    if other_has_cap:
                        log.debug("Worker %s: skipping %s — another worker has it", worker_id, cap_name)
                        await asyncio.sleep(0.1)
                        await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                        await REDIS.xdel(TASK_STREAM, msg_id)
                        # Re-add so another consumer picks it up
                        await REDIS.xadd(TASK_STREAM, {
                            "id": task_id, "capability": cap_name,
                            "payload": json.dumps(payload), "trace_id": trace_id, "ts": now_iso(),
                        }, maxlen=5000, approximate=True)
                    else:
                        log.warning("Worker %s: no handler for %s on any worker", worker_id, cap_name)
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "error": f"no_worker_for:{cap_name}", "trace_id": trace_id,
                        })
                        await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                        await REDIS.xdel(TASK_STREAM, msg_id)
                else:
                    # Run the cap as a separate task so cluster.job.stop can
                    # cancel it cooperatively (cancel() interrupts at next await).
                    inner = asyncio.ensure_future(cap["raw"](**payload, trace_id=trace_id))
                    RUNNING_TASKS[task_id] = inner
                    try:
                        result = await inner
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "result": json.dumps(result), "trace_id": trace_id,
                        }, maxlen=5000)
                        WORKER_REGISTRY[worker_id]["tasks_done"] += 1
                        await emit_event({"type": "worker.done", "worker": worker_id, "task": task_id})
                    except asyncio.CancelledError:
                        # Intentional cancel of the inner task — not the worker loop.
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "error": "cancelled", "trace_id": trace_id,
                        })
                        await emit_event({
                            "type": "worker.cancelled", "worker": worker_id, "task": task_id,
                        })
                    except Exception as e:
                        await REDIS.xadd(RESULT_STREAM, {
                            "id": task_id, "error": str(e), "trace_id": trace_id,
                        })
                        WORKER_REGISTRY[worker_id]["tasks_failed"] += 1
                        await emit_event({
                            "type": "worker.error", "worker": worker_id,
                            "task": task_id, "error": str(e),
                        })
                    finally:
                        RUNNING_TASKS.pop(task_id, None)
                        await REDIS.xack(TASK_STREAM, GROUP_WORKERS, msg_id)
                        await REDIS.xdel(TASK_STREAM, msg_id)

                WORKER_REGISTRY[worker_id]["status"] = "idle"
                WORKER_REGISTRY[worker_id]["current_task"] = ""
                WORKER_REGISTRY[worker_id]["task_started"] = ""

async def result_listener():
    """
    Listen for task results on the shared result stream.

    Each host uses its own hostname as the consumer name within the shared
    GROUP_RESULTS consumer group.  This means every result is delivered to
    exactly one host — the one whose PENDING_RESULTS dict holds the future.

    Cross-host delivery: if Host A dispatched the task, the future lives in
    Host A's PENDING_RESULTS. Host B executes the task and writes the result
    to RESULT_STREAM. All hosts read the stream; only Host A finds the future
    and resolves it. The others ACK without doing anything (no future found).
    This is correct — ACKing without a matching future is a no-op.
    """
    if not REDIS: return
    # Per-host consumer name prevents two hosts sharing one consumer slot
    consumer_name = f"host-{os.uname().nodename}"
    try:
        await REDIS.xgroup_create(RESULT_STREAM, GROUP_RESULTS, id="$", mkstream=True)
    except Exception:
        pass   # group already exists
    log.info("Result listener started (consumer=%s)", consumer_name)
    while True:
        try:
            resp = await REDIS.xreadgroup(
                GROUP_RESULTS, consumer_name, {RESULT_STREAM: ">"}, count=10, block=5000
            )
        except Exception as e:
            log.error("result_listener: %s", e)
            await asyncio.sleep(2)
            continue
        if not resp:
            continue
        for _, messages in resp:
            for msg_id, data in messages:
                task_id = data[b"id"].decode()
                fut     = PENDING_RESULTS.get(task_id)
                if fut and not fut.done():
                    if b"result" in data:
                        fut.set_result(json.loads(data[b"result"]))
                    else:
                        fut.set_result({"error": data.get(b"error", b"unknown").decode()})
                    PENDING_RESULTS.pop(task_id, None)
                # Always ACK — even if this host didn't own the future
                # (another host may have already resolved it from its own listener)
                await REDIS.xack(RESULT_STREAM, GROUP_RESULTS, msg_id)

async def _pg_archive(task_id,result_json):
    # Dev sandboxes share prod's Postgres — don't archive their task results into
    # prod's table (strict no-op in prod; see vera/sandbox_guard.py).
    if _sbx_write_blocked():
        return
    try:
        async with PG_POOL.acquire() as conn:
            await conn.execute("INSERT INTO vera_task_results(task_id,result,ts) VALUES($1,$2::jsonb,NOW()) ON CONFLICT DO NOTHING",task_id,result_json)
    except Exception as e: log.warning("PG archive: %s",e)

# ─────────────────────────────────────────────────────────────────────────────
#  ██  CAPABILITY DECORATOR  — the single registration primitive
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# FRAMEWORK-WIDE ACTIVITY RECORDING
# Every non-silent, non-infrastructure capability call with a session_id is
# recorded to the memory graph (FOLLOWS_ACTIVITY chain) and data fabric.
#
# Architecture: fire-and-forget queue drained by a background worker.
# The capability wrapper enqueues a lightweight dict (no awaiting, no blocking).
# The _activity_worker() coroutine drains the queue every 2s, batching writes.
# This means zero overhead on the hot path and no risk of breaking cap calls.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio as _act_asyncio
import queue as _queue_mod

# In-process state
_ACT_QUEUE: "asyncio.Queue" = None           # created lazily in first enqueue
_ACT_SESSION_CURSOR: dict   = {}             # session_id -> last node_id
_ACT_SESSION_ROOT:   dict   = {}             # session_id -> root node_id (cached)
_ACT_FABRIC_DEDUP:   set    = set()          # trace_id dedup
_CURRENT_SESSION:    str    = ""             # last known session_id (fallback for caps without explicit session)

# Capability groups that are too noisy / infrastructure — skip recording
_ACT_SKIP_GROUPS = frozenset({
    # Infrastructure / polling
    "obs", "health", "ollama", "ui", "mcp", "memory",
    "syslog", "cluster", "db", "stream", "caps", "session",
    # Fabric would create infinite recording loop
    "fabric",
    # Agent infrastructure — high frequency, handled separately
    "agent",
})

# NOTE: there used to be a separate _ACT_RICH_GROUPS set for ide/research/nlp
# that suppressed activity recording for those groups on the assumption they
# had their own richer per-module recording. That's no longer true — there
# is one unified recording path now (_act_enqueue → _activity_worker) and
# every tracked group writes the same call+output linked structure.


# Activity-recording size limits. Tuned so a single queue item stays under
# ~16 KB even for chatty caps. The underlying activity_worker truncates again
# when writing to the graph so individual graph nodes stay reasonable.
_ACT_PARAMS_MAX_BYTES   = 4096    # serialised params dict cap
_ACT_RESULT_MAX_BYTES   = 8192    # serialised result cap
_ACT_PREVIEW_MAX_CHARS  = 400     # human-readable preview line


def _act_safe_params(kw: dict) -> dict:
    """
    Return a sanitised copy of cap params suitable for storage.

    Drops anything that's clearly too large or sensitive-shaped (binary blobs,
    secrets-looking keys). Truncates long string values. Keeps the dict
    structure so the recorded params are still queryable.
    """
    SECRETY = ("password", "secret", "token", "api_key", "apikey",
               "auth", "credential", "ssh_key", "private_key")
    out: dict = {}
    for k, v in kw.items():
        if k == "trace_id":
            continue
        kl = k.lower()
        if any(s in kl for s in SECRETY):
            out[k] = "[redacted]"
            continue
        if isinstance(v, str):
            out[k] = v[:1000]            # trim very long strings
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:200] for x in v[:25]]
        elif isinstance(v, dict):
            out[k] = {kk: (str(vv)[:200] if not isinstance(vv, (int, float, bool, type(None))) else vv)
                      for kk, vv in list(v.items())[:25]}
        else:
            out[k] = str(v)[:300]
    return out


def _act_extract_text(result):
    """Pick the most informative text out of a cap result for the preview line."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "response", "summary", "content", "answer",
                    "output", "result", "preview", "status", "error",
                    "path", "name", "job_id"):
            v = result.get(key)
            if v and isinstance(v, str):
                return v
        # Fall back to JSON repr (truncated)
        try:
            return json.dumps(result)[:_ACT_PREVIEW_MAX_CHARS]
        except Exception:
            return str(result)[:_ACT_PREVIEW_MAX_CHARS]
    return str(result)[:_ACT_PREVIEW_MAX_CHARS]


def _act_enqueue(cap_name: str, group: str, session_id: str,
                 trace_id: str, kw: dict, result: object,
                 elapsed_ms: int,
                 trigger_id: str = "", trigger_cap: str = ""):
    """
    Non-blocking enqueue of a capability call for background recording.

    Called from inside the capability wrapper AND from streaming endpoints
    that don't go through the wrapper. Must never await or raise.

    The recorded payload contains:
      - cap_name, group, session_id, trace_id, trigger_id, trigger_cap
      - safe_params : sanitised copy of input kwargs (size-bounded)
      - result_full : serialised cap result (size-bounded)
      - preview     : short human-readable preview line for syslog/UI
      - elapsed_ms, ts

    The activity_worker drains this queue and writes ONE rich graph node per
    cap call carrying both input and output, linked via FOLLOWS_ACTIVITY to
    the previous chain step.
    """
    global _ACT_QUEUE
    # Fall back to the orchestrator's current session id when the caller
    # didn't supply one. This matters for raw streaming endpoints (bash,
    # ssh, ide) where the panel may not include session_id in the request
    # body but the syslog trigger context does have it.
    if not session_id:
        try:
            _vera_syslog = sys.modules.get("syslog")
            if _vera_syslog:
                session_id = (_vera_syslog.get_trigger_chain() or {}).get(
                    "session_id", "")
        except Exception:
            pass
    if not session_id:
        session_id = _CURRENT_SESSION
    if not session_id:
        return  # genuinely orphaned call — drop
    if group in _ACT_SKIP_GROUPS:
        return
    if not sys.modules.get("data_fabric"):
        return  # backends not loaded yet
    try:
        if _ACT_QUEUE is None:
            try:
                _act_asyncio.get_running_loop()
                _ACT_QUEUE = _act_asyncio.Queue(maxsize=2000)
            except RuntimeError:
                return  # no running loop yet — skip

        safe_params = _act_safe_params(kw or {})
        # Truncate the JSON repr to keep the queue item compact even for
        # chatty caps. We store both a human preview and the full structured
        # result so downstream queries can drill in.
        try:
            params_json = json.dumps(safe_params, default=str)[:_ACT_PARAMS_MAX_BYTES]
        except Exception:
            params_json = str(safe_params)[:_ACT_PARAMS_MAX_BYTES]
        try:
            if isinstance(result, (dict, list)):
                result_json = json.dumps(result, default=str)[:_ACT_RESULT_MAX_BYTES]
            else:
                result_json = str(result)[:_ACT_RESULT_MAX_BYTES]
        except Exception:
            result_json = str(result)[:_ACT_RESULT_MAX_BYTES]

        preview_text = _act_extract_text(result)[:_ACT_PREVIEW_MAX_CHARS]

        _ACT_QUEUE.put_nowait({
            "cap_name":    cap_name,
            "group":       group,
            "session_id":  session_id,
            "trace_id":    trace_id,
            "trigger_id":  trigger_id,
            "trigger_cap": trigger_cap,
            "safe_params": safe_params,        # for graph metadata
            "params_json": params_json,        # for full-text storage
            "result_json": result_json,        # for full-text storage
            "preview":     preview_text,
            "elapsed_ms":  elapsed_ms,
            "ts":          now_iso(),
        })
    except Exception:
        pass  # full queue or other error — always silent


async def begin_stream_activity(
    cap_name:    str,
    session_id:  str,
    *,
    trace_id:    str = "",
    group:       str = "",
    silent:      bool = False,
):
    """
    Emit a cap.call event for a streaming endpoint at the START of the stream.

    Returns a dict carrying state needed by the matching end_stream_activity
    call (trace_id, group, trigger_chain, t0). Pass it into end_stream_activity
    when the stream completes.

    Returns None if no session_id can be resolved (recording is dropped).
    Falls back to the syslog trigger chain and then `_CURRENT_SESSION` so
    streaming endpoints that don't take session_id in their body still record.
    """
    if not session_id:
        try:
            _vera_syslog = sys.modules.get("syslog")
            if _vera_syslog:
                session_id = (_vera_syslog.get_trigger_chain() or {}).get(
                    "session_id", "")
        except Exception:
            pass
    if not session_id:
        session_id = _CURRENT_SESSION
    if not session_id:
        return None
    tid   = trace_id or new_id()
    grp   = group or cap_name.split(".", 1)[0]
    chain = {}
    try:
        _vera_syslog = sys.modules.get("syslog")
        if _vera_syslog:
            chain = _vera_syslog.get_trigger_chain() or {}
    except Exception:
        pass
    if not silent:
        try:
            await emit_event({
                "type":        "cap.call",
                "name":        cap_name,
                "trace_id":    tid,
                "session_id":  session_id,
                "trigger_id":  chain.get("trigger_id", ""),
                "trigger_cap": chain.get("trigger_cap", ""),
                "group":       grp,
            })
        except Exception:
            pass
    return {
        "cap_name":   cap_name,
        "session_id": session_id,
        "trace_id":   tid,
        "group":      grp,
        "chain":      chain,
        "t0":         time.monotonic(),
        "silent":     silent,
    }


async def end_stream_activity(
    handle: dict,
    params: dict,
    result: object,
    elapsed_ms: int = 0,
):
    """
    Companion to begin_stream_activity. Emits cap.ok and enqueues the rich
    activity item for the activity_worker to write a graph node-pair.

    `handle` is the dict returned by begin_stream_activity. If it's None
    (the begin call was dropped because session_id was empty), this is a no-op.
    """
    if not handle:
        return
    cap_name   = handle["cap_name"]
    session_id = handle["session_id"]
    tid        = handle["trace_id"]
    grp        = handle["group"]
    chain      = handle.get("chain", {})
    silent     = handle.get("silent", False)
    if elapsed_ms <= 0:
        elapsed_ms = round((time.monotonic() - handle["t0"]) * 1000)
    if not silent:
        _preview = _act_extract_text(result)[:200]
        try:
            await emit_event({
                "type":        "cap.ok",
                "name":        cap_name,
                "trace_id":    tid,
                "session_id":  session_id,
                "group":       grp,
                "elapsed_ms":  elapsed_ms,
                "preview":     _preview,
            })
        except Exception:
            pass
    _act_enqueue(
        cap_name=cap_name, group=grp, session_id=session_id,
        trace_id=tid, kw=params, result=result,
        elapsed_ms=elapsed_ms,
        trigger_id=chain.get("trigger_id", ""),
        trigger_cap=chain.get("trigger_cap", ""),
    )


async def record_stream_activity(
    cap_name:    str,
    session_id:  str,
    params:      dict,
    result:      object,
    elapsed_ms:  int,
    *,
    trace_id:    str = "",
    group:       str = "",
    silent:      bool = False,
):
    """
    Convenience wrapper: emit cap.call, cap.ok, and enqueue activity in one
    shot. Use when you don't need to interleave the cap.call event with
    other output (i.e. the recording happens in a `finally:` block at the
    very end of a stream).

    For richer stream visualisation in the Observe panel — where you want
    the cap.call to appear at the START of the stream, not at the end —
    use begin_stream_activity / end_stream_activity instead.
    """
    handle = await begin_stream_activity(
        cap_name, session_id, trace_id=trace_id, group=group, silent=silent,
    )
    if handle is None:
        return
    await end_stream_activity(handle, params, result, elapsed_ms)


async def _activity_worker():
    """
    Background coroutine. Drains _ACT_QUEUE every 2s, writes ONE rich
    graph node per cap call, plus a fabric entry. Runs for the lifetime
    of the server.

    Graph structure produced per cap call (single node, not a pair):

                    [previous chain step]
                            │
                            │  FOLLOWS_ACTIVITY
                            ▼
                    [cap_call_node : event/tool]
                       (carries both input and output)

        ─ The cap node carries:
            • `text`       — short [cap_name] hint with first few params
            • `full_text`  — Cap / Trace / Group / Trigger / Params / Result
            • `metadata`   — full structured params + preview + elapsed_ms

        ─ Edges:
            • Neo4j backend auto-creates `(:Session)-[:CONTAINS]->(:Memory)`
              for every Memory node with a session_id; we don't add an
              extra SESSION_CONTENT edge. The Neo4j auto-edge is the single
              authoritative session→cap relationship.
            • FOLLOWS_ACTIVITY links the previous chain step → this node.
            • TRIGGERED_BY is intentionally NOT emitted — the trigger_id
              we receive from syslog is a trace_id (call-correlation key),
              not a graph node id, so wiring an edge to it would create
              the "unresolved" placeholder nodes the user sees in the
              memory graph panel. Trigger context is preserved inside the
              cap node's `metadata` for queries.
    """
    global _ACT_QUEUE
    if _ACT_QUEUE is None:
        _ACT_QUEUE = _act_asyncio.Queue(maxsize=2000)

    log.info("activity_worker: started (single-node mode)")
    while True:
        try:
            await _act_asyncio.sleep(2.0)
            # Only process if memory system is loaded (avoids hammering during startup)
            if not sys.modules.get("data_fabric"):
                continue
            batch = []
            while not _ACT_QUEUE.empty() and len(batch) < 50:
                try:
                    batch.append(_ACT_QUEUE.get_nowait())
                except Exception:
                    break
            if not batch:
                continue

            mem_mod = sys.modules.get("memory")
            hooks   = sys.modules.get("memory_hooks")
            fabric  = sys.modules.get("data_fabric")

            for item in batch:
                sid          = item["session_id"]
                cap_name     = item["cap_name"]
                group        = item["group"]
                trace_id     = item["trace_id"]
                trigger_id   = item.get("trigger_id", "")
                trigger_cap  = item.get("trigger_cap", "")
                safe_params  = item.get("safe_params", {})
                params_json  = item.get("params_json", "")
                result_json  = item.get("result_json", "")
                preview      = item.get("preview", "")
                elapsed_ms   = item["elapsed_ms"]
                ts           = item["ts"]

                _is_dag = group == "dag" or "dag" in cap_name

                cap_id = str(uuid.uuid4())

                # Short text shown as the node label in the graph panel.
                # Includes 1–3 of the most descriptive param fields so the
                # node is informative without expanding it.
                text = "[" + cap_name + "]"
                if safe_params:
                    hint_parts = []
                    for k, v in list(safe_params.items())[:3]:
                        hv = str(v)
                        if len(hv) > 40:
                            hv = hv[:38] + "…"
                        hint_parts.append(f"{k}={hv}")
                    if hint_parts:
                        text += " " + ", ".join(hint_parts)
                if preview:
                    # Append a → preview so the node text reflects what
                    # actually happened, not just what was asked
                    p = preview[:120].replace("\n", " ").strip()
                    if p:
                        text += " → " + p
                text = text[:500]

                # Full-text body: structured for grep-style searches and
                # for the panel's expanded-node detail view.
                full_text = (
                    "Cap: " + cap_name + "\n"
                    + "Trace: " + trace_id + "\n"
                    + "Group: " + group + "\n"
                    + "Elapsed: " + str(elapsed_ms) + "ms\n"
                    + ("Trigger: " + trigger_cap
                       + (" via " + trigger_id[:12] if trigger_id else "")
                       + "\n" if trigger_cap else "")
                    + "\n--- INPUT ---\n" + params_json
                    + "\n\n--- OUTPUT ---\n" + result_json
                )

                # ── Persist via memory backend (single node) ──────────────
                graph_ok = False
                if mem_mod:
                    try:
                        MEMORY, MemRecord = mem_mod.MEMORY, mem_mod.MemoryRecord
                        rec = MemRecord(
                            id=cap_id, session_id=sid, trace_id=trace_id,
                            record_type=("dag_step" if _is_dag else "cap_call"),
                            source_type="tool",
                            category=("dag.step" if _is_dag else "cap." + group),
                            tags=[group, cap_name, "capability"]
                                 + (["dag"] if _is_dag else []),
                            keywords=[cap_name, group] + list(safe_params.keys())[:5],
                            text=text,
                            full_text=full_text,
                            importance=0.5 if _is_dag else 0.4,
                            capability=cap_name,
                            ai_output=group in ("llm", "agent", "chat"),
                            metadata={
                                "trace_id":    trace_id,
                                "trigger_id":  trigger_id,
                                "trigger_cap": trigger_cap,
                                "elapsed_ms":  elapsed_ms,
                                "group":       group,
                                "is_dag":      _is_dag,
                                "params":      safe_params,
                                "preview":     preview,
                            },
                            created_at=ts, updated_at=ts,
                        )
                        await MEMORY.store(rec)
                        graph_ok = True
                    except Exception as e:
                        log.warning("activity_worker store [%s]: %s",
                                    cap_name, e)

                # ── Edges ─────────────────────────────────────────────────
                # FOLLOWS_ACTIVITY links cap nodes in sequence.
                # TRIGGERED_BY_MSG links from the most recent human/AI
                # message to the first cap call in a new turn — bridging
                # the message chain and the cap chain. We only emit this
                # edge once per turn (when the prior cursor was a message
                # node, not another cap), detected via memory_hooks._LAST_MSG.
                if graph_ok and hooks:
                    try:
                        prior = _ACT_SESSION_CURSOR.get(sid, "")
                        if prior and prior != cap_id:
                            await hooks._link_nodes(
                                prior, cap_id, "FOLLOWS_ACTIVITY",
                                {"cap": cap_name, "ts": ts},
                                session_id=sid)

                        # Cross-link: if memory_hooks knows the last AI
                        # message node for this session, and we don't
                        # already have a cap node as the prior (i.e. this
                        # is the FIRST cap in a new turn), add a
                        # TRIGGERED_BY_MSG edge from the message → cap.
                        last_msg = getattr(hooks, "_LAST_MSG", {}).get(sid, "")
                        if last_msg and last_msg != cap_id and last_msg != prior:
                            await hooks._link_nodes(
                                last_msg, cap_id, "TRIGGERED_BY_MSG",
                                {"cap": cap_name, "ts": ts},
                                session_id=sid)
                    except Exception as e:
                        log.debug("activity_worker edges [%s]: %s", cap_name, e)

                # Cursor advances to this node — sets the chain head for
                # the next FOLLOWS_ACTIVITY link. The _TrackingCursor wrapper
                # also bumps the per-session chain counter.
                if graph_ok:
                    _ACT_SESSION_CURSOR[sid] = cap_id

                # ── Fabric ─────────────────────────────────────────────────
                # Never ingest fabric/memory/obs cap activity back into
                # fabric — doing so creates an event → ingest → event
                # cascade that doubles memory every ~20s.
                _SKIP_GROUPS = {"fabric", "memory", "obs", "health", "ui"}
                if group not in _SKIP_GROUPS and fabric:
                    dk = "cap:" + sid + ":" + trace_id
                    if dk not in _ACT_FABRIC_DEDUP:
                        try:
                            await fabric.ingest_dataset(
                                dataset_id="caps." + group,
                                data=[{
                                    "text":       text,
                                    "cap_name":   cap_name,
                                    "group":      group,
                                    "trace_id":   trace_id,
                                    "session_id": sid,
                                    "elapsed_ms": elapsed_ms,
                                    "params":     params_json,
                                    "result":     result_json,
                                    "preview":    preview,
                                    "node_id":    cap_id,
                                    "ts":         ts,
                                }],
                                source="capability_framework",
                                source_id=sid,
                                tags=[group, cap_name, "capability"],
                            )
                            _ACT_FABRIC_DEDUP.add(dk)
                            if len(_ACT_FABRIC_DEDUP) > 50000:
                                _ACT_FABRIC_DEDUP.clear()
                        except Exception as e:
                            log.debug("activity_worker fabric [%s]: %s", cap_name, e)

        except _act_asyncio.CancelledError:
            break
        except Exception as e:
            log.debug("activity_worker loop: %s", e)


def capability(
    name:        str,
    *,
    mode:        str            = "local",
    retries:     int            = 0,
    streams:     List[str]      = None,
    description: str            = None,
    tags:        List[str]      = None,
    # memory: how the unified activity recorder treats this cap.
    #   "on"   — record richly (call + output graph nodes, full params/result
    #            text, FOLLOWS_ACTIVITY chain links). DEFAULT.
    #   "off"  — opt out entirely (no graph node, no fabric entry).
    # The legacy "auto" value is accepted for compatibility and treated as "on".
    memory:      str            = "on",
    silent:      bool           = False,   # suppress cap.call/cap.ok events (polling caps)
    # ── Schema override ─────────────────────────────────────────────────────
    # Optional JSON-Schema fragment to enrich the auto-generated schema.
    # The decorator always runs generate_schema(func) to derive types and the
    # required list from the real Python signature.  If `schema` is provided
    # it is deep-merged on top via _merge_schema: descriptions, enums,
    # formats, and constraints from `schema` win, while auto-detected types
    # and defaults fill in anything the override omits.
    # Only supply `properties` (and optionally `required`) — the top-level
    # "type":"object" wrapper is always set automatically.
    schema:      Optional[dict] = None,
    # ── HTTP route options ──────────────────────────────────────────────────
    http_method: Optional[str]  = None,   # "GET" | "POST" | "PUT" | "DELETE"
    http_path:   Optional[str]  = None,   # e.g. "/ui/panels"  "/health"
    http_tags:   List[str]      = None,   # OpenAPI tags  (defaults to [group])
    # ── MCP config ─────────────────────────────────────────────────────────
    mcp_expose:  bool           = True,   # include in /mcp/tools listing
):
    """
    Unified registration decorator.

    @capability("ui.panels", memory="off", silent=True, http_method="GET", http_path="/ui/panels")
    async def get_ui_panels(trace_id=None):
        ...

    The decorated function is:
      1. Registered in CAPABILITY_REGISTRY (MCP + harness)
      2. Optionally mounted as a REST endpoint (if http_method + http_path given)
      3. All calls emitted as Redis events (observable) — unless silent=True
      4. Available via distributed dispatch if mode="distributed"

    silent=True suppresses cap.call/cap.ok events — use for high-frequency
    polling capabilities (health checks, obs.*, ui.panels etc.) to keep the
    syslog and terminal clean.

    http_method/path can be set AFTER decoration via cap["http_method"] etc
    because routes are mounted during lifespan, not at import time.
    """
    def deco(func):
        _auto_schema   = generate_schema(func)
        _final_schema  = _merge_schema(_auto_schema, schema) if schema else _auto_schema
        group  = name.split(".")[0]

        @functools.wraps(func)
        async def wrap(**kw):
            tid     = kw.pop("trace_id",None) or new_id()
            attempt = 0; last_err = None
            # Pull trigger chain from context vars (set by vera_syslog patcher)
            _vera_syslog = sys.modules.get("syslog")
            chain = _vera_syslog.get_trigger_chain() if _vera_syslog else {}
            _t0 = time.monotonic()
            while attempt <= retries:
                try:
                    _sid = (kw.get("session_id","") or chain.get("session_id","")
                            or _CURRENT_SESSION)
                    if not silent:
                        await emit_event({
                            "type":        "cap.call",
                            "name":        name,
                            "attempt":     attempt,
                            "trace_id":    tid,
                            "session_id":  _sid,
                            "trigger_id":  chain.get("trigger_id",""),
                            "trigger_cap": chain.get("trigger_cap",""),
                            "group":       group,
                            "args_preview": _args_preview(kw),
                        })
                        await _mirror_cap_activity("call", name, _sid, tid, group,
                                                   args=_args_compact(kw))
                    if mode=="distributed" and REDIS:
                        task_id=await dispatch_task(name,kw,tid)
                        # Per-cap timeout: LLM caps need 240-300s, research
                        # needs 60s, DAG composer caps need 600s. The previous
                        # 30s blanket default caused every llm.generate inside
                        # a DAG to fail with a useless "task timed out" error.
                        # Honour an explicit `_timeout` field in payload kw if
                        # the caller has special needs.
                        _t = float(kw.pop("_timeout", 0)) or _cap_timeout(name)
                        result =await wait_for_result(task_id, timeout=_t)
                    else:
                        # Filter kwargs to only those the function accepts,
                        # preventing TypeError on unexpected keyword arguments
                        # (e.g. dream system passing 'iterate' to a cap that
                        # doesn't have it in its signature).
                        # Route trace_id through the same signature filter so caps
                        # whose signature can't accept it (no trace_id param and no
                        # **kwargs, e.g. openclaw.status) don't raise
                        # 'unexpected keyword argument'. Covers every call path
                        # (HTTP, DAG, direct, MCP) since all flow through wrap.
                        _call_kw = _filter_kwargs_for_func(func, {**kw, "trace_id": tid})
                        result=await func(**_call_kw)
                    for s in (streams or []):
                        await emit_stream(s,tid,result,name)
                    _elapsed_ms = round((time.monotonic()-_t0)*1000)
                    # Build result preview regardless of silent
                    _preview = ""
                    if isinstance(result, dict):
                        for _k in ("text","response","content","summary","result",
                                   "status","job_id","error","path","name"):
                            _v = result.get(_k)
                            if _v and isinstance(_v, str):
                                _preview = _v[:200]; break

                    # Cache result in Redis (all caps, even silent) for state inspection
                    if REDIS:
                        try:
                            _cache = {"name": name, "trace_id": tid,
                                      "session_id": _sid, "elapsed_ms": _elapsed_ms,
                                      "ts": now_iso(), "preview": _preview,
                                      "result": json.dumps(result)[:4096]
                                               if isinstance(result, (dict,list)) else str(result)[:4096]}
                            await REDIS.setex(
                                f"vera:cap:result:{name}",
                                300,  # 5 min TTL — recent state always inspectable
                                json.dumps(_cache)
                            )
                            # Also keep a sorted set of recent cap calls for monitoring
                            await REDIS.zadd("vera:cap:recent",
                                {json.dumps({"name": name, "tid": tid, "sid": _sid,
                                             "ts": now_iso(), "elapsed_ms": _elapsed_ms,
                                             "preview": _preview}): time.time()})
                            await REDIS.zremrangebyrank("vera:cap:recent", 0, -501)
                        except Exception:
                            pass

                    if not silent:
                        await emit_event({
                            "type":        "cap.ok",
                            "name":        name,
                            "trace_id":    tid,
                            "session_id":  _sid,
                            "group":       group,
                            "elapsed_ms":  _elapsed_ms,
                            "preview":     _preview,
                        })
                        await _mirror_cap_activity("ok", name, _sid, tid, group,
                                                   elapsed_ms=_elapsed_ms,
                                                   preview=_preview)
                    # Unified activity recording. memory="off" opts out;
                    # everything else (default "on", legacy "auto") records
                    # richly via the activity worker. silent=True caps still
                    # opt out so polling/health caps don't flood the graph.
                    if (_sid and memory != "off" and not silent):
                        _act_enqueue(
                            cap_name=name, group=group, session_id=_sid,
                            trace_id=tid, kw=kw, result=result,
                            elapsed_ms=_elapsed_ms,
                            trigger_id=chain.get("trigger_id", ""),
                            trigger_cap=chain.get("trigger_cap", ""),
                        )
                    return result
                except Exception as e:
                    last_err=e; attempt+=1
                    _elapsed_ms = round((time.monotonic()-_t0)*1000)
                    # Never empty — message-less exceptions (httpx timeouts etc.)
                    # otherwise surface as "unknown" in the jobs panels.
                    _err_str = str(e).strip() or _err_text(e)
                    # Capture the FULL traceback here, inside the except, where
                    # sys.exc_info() still points at the original failure (down to
                    # the real failing line). Carried on the event so the syslog
                    # record has the proper traceback, not just the message.
                    import traceback as _tb
                    _err_tb = _tb.format_exc()
                    # Cache error in Redis
                    if REDIS:
                        try:
                            await REDIS.setex(
                                f"vera:cap:error:{name}",
                                600,  # 10 min TTL
                                json.dumps({"name": name, "error": _err_str,
                                            "ts": now_iso(), "trace_id": tid,
                                            "elapsed_ms": _elapsed_ms})
                            )
                        except Exception:
                            pass
                    # cap.error ALWAYS emits — never silenced
                    await emit_event({
                        "type":        "cap.error",
                        "name":        name,
                        "error":       _err_str,
                        "error_type":  type(e).__name__,
                        "args_preview": _args_preview(kw),
                        "traceback":   _err_tb,
                        "attempt":     attempt,
                        "trace_id":    tid,
                        "session_id":  kw.get("session_id","") or chain.get("session_id",""),
                        "trigger_id":  chain.get("trigger_id",""),
                        "trigger_cap": chain.get("trigger_cap",""),
                        "group":       group,
                        "elapsed_ms":  _elapsed_ms,
                    })
                    await _mirror_cap_activity(
                        "error", name,
                        kw.get("session_id","") or chain.get("session_id","") or _CURRENT_SESSION,
                        tid, group, error=_err_str[:300], elapsed_ms=_elapsed_ms)
                    if attempt<=retries: await asyncio.sleep(0.5*attempt)
            raise last_err

        CAPABILITY_REGISTRY[name]={
            "func":        wrap,
            "raw":         func,
            "schema":      _final_schema,
            "description": description or func.__doc__ or "",
            "streams":     streams or [],
            "mode":        mode,
            "retries":     retries,
            "tags":        tags or [group],
            "source":      "local",
            "mcp_expose":  mcp_expose,
            "memory":      memory,
            "silent":      silent,
            # HTTP route metadata — used at lifespan mount time
            "http_method": http_method,
            "http_path":   http_path,
            "http_tags":   http_tags or [http_path.split("/")[1] if http_path else group],
        }
        return wrap
    return deco


# ─────────────────────────────────────────────────────────────────────────────
# MCP PROXY REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────
async def register_mcp_server(base_url: str, server_name: str) -> List[str]:
    try:
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=10) as c:
            r=await c.get(f"{base_url}/mcp/tools"); r.raise_for_status(); tools=r.json()
    except Exception as e:
        log.error("register_mcp_server %s: %s",server_name,e); return []
    registered=[]
    for tool in tools:
        tool_name=f"{server_name}.{tool['name']}"
        async def _proxy(_url=base_url,_tool=tool["name"],**kwargs):
            tid=kwargs.pop("trace_id",new_id())
            async with httpx.AsyncClient(verify=_SSL_CTX, timeout=60) as c:
                r=await c.post(f"{_url}/mcp/call",json={"name":_tool,"arguments":kwargs,"trace_id":tid})
                r.raise_for_status(); return r.json()
        CAPABILITY_REGISTRY[tool_name]={
            "func":_proxy,"raw":_proxy,
            "schema":tool.get("schema",{"type":"object","properties":{}}),
            "description":tool.get("description",f"Proxied from {server_name}"),
            "streams":[],"mode":"proxy","source":"mcp_proxy",
            "server":server_name,"server_url":base_url,
            "tags":["proxy",server_name],"mcp_expose":True,
            "http_method":None,"http_path":None,"http_tags":["proxy"],
        }
        registered.append(tool_name)
    MCP_SERVERS[server_name]=base_url
    await emit_event({"type":"mcp_server.registered","name":server_name,"tools":registered,"url":base_url})
    return registered

# ─────────────────────────────────────────────────────────────────────────────
# DAG ENGINE
# ─────────────────────────────────────────────────────────────────────────────
async def run_graph(graph: list, state: dict, trace_id: str = "") -> dict:
    """Execute a DAG graph. trace_id flows through all cap calls for traceability."""
    _dag_trace = trace_id or new_id()
    for node in graph:
        if isinstance(node,list) and isinstance(node[0],list):
            results=await asyncio.gather(*[run_graph([n],dict(state),_dag_trace) for n in node],return_exceptions=True)
            for r in results:
                if isinstance(r,dict): state.update(r)
            continue
        cap_name,out_key,*rest=node; cond=rest[0] if rest else None
        if cond:
            if callable(cond) and not cond(state): continue
            if isinstance(cond,str) and cond.startswith("CONDITION:") and not state.get(cond.split(":",1)[1]): continue
        cap=CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            if out_key: state[out_key]={"error":f"unknown_cap:{cap_name}"}
            continue
        try:
            accepted=set(cap["schema"].get("properties",{}).keys())
            params={k:v for k,v in state.items() if k in accepted}
            result=await cap["func"](**params, trace_id=_dag_trace)
            if out_key: state[out_key]=result
            # Detect silent errors and enrich state with syslog context
            if isinstance(result, dict) and "error" in result:
                ctx = await _get_syslog_context(cap_name, str(result["error"]))
                if ctx and out_key:
                    state[f"_err_ctx_{out_key}"] = ctx
        except Exception as e:
            err_msg = str(e)
            ctx = await _get_syslog_context(cap_name, err_msg)
            state[out_key or f"_err_{cap_name}"] = {"error": err_msg, "syslog_context": ctx}
    return state


async def _get_syslog_context(cap_name: str, error_msg: str) -> str:
    """Fetch syslog error context for a cap — non-blocking, returns empty string on failure."""
    try:
        vera_syslog = sys.modules.get("syslog")
        if vera_syslog:
            return await vera_syslog.get_dag_error_context(cap_name, error_msg)
    except Exception:
        pass
    return ""

async def supervised_run_graph(graph: list, state: dict, supervision_every: int = 1, max_node_retries: int = 2) -> dict:
    log_entries=[]
    i=0
    while i<len(graph):
        node=graph[i]
        if isinstance(node,list) and isinstance(node[0],list):
            results=await asyncio.gather(*[run_graph([n],dict(state)) for n in node],return_exceptions=True)
            for r in results:
                if isinstance(r,dict): state.update(r)
            log_entries.append({"step":i,"type":"parallel","branches":len(node)})
        else:
            cap_name,out_key,*_=node; cap=CAPABILITY_REGISTRY.get(cap_name)
            if not cap:
                if out_key: state[out_key]={"error":"unknown"}
                log_entries.append({"step":i,"cap":cap_name,"error":"unknown"})
            else:
                attempt=0
                while attempt<=max_node_retries:
                    try:
                        accepted=set(cap["schema"].get("properties",{}).keys())
                        result=await cap["func"](**{k:v for k,v in state.items() if k in accepted})
                        if out_key: state[out_key]=result
                        log_entries.append({"step":i,"cap":cap_name,"attempt":attempt}); break
                    except Exception as e:
                        attempt+=1
                        if attempt>max_node_retries:
                            if out_key: state[out_key]={"error":str(e)}
                            log_entries.append({"step":i,"cap":cap_name,"error":str(e)})
        if (i+1)%supervision_every==0 and i<len(graph)-1:
            decision=await _llm_supervise(log_entries,state,graph[i+1:])
            action=decision.get("action","continue")
            await emit_event({"type":"supervision.checkpoint","step":i,"action":action,"decision":decision})
            if action=="abort":
                state["__aborted__"]=decision.get("reason","LLM aborted"); break
            elif action=="retry_node": graph.insert(i+1,node)
            elif action=="insert_node":
                cap_ins=decision.get("capability")
                if cap_ins and cap_ins in CAPABILITY_REGISTRY:
                    graph.insert(i+1,[cap_ins,decision.get("output_key","_inserted")])
        i+=1
    return state

async def _llm_supervise(log_entries, state, remaining):
    system=('You supervise a DAG. Review results, decide: continue|abort|retry_node|insert_node. '
            'Return ONLY JSON: {"action":"...","reason":"...","capability":"...","output_key":"..."}')
    raw=await ollama_generate(
        f"Log(last 5):\n{json.dumps(log_entries[-5:],indent=2)}\nState keys:{list(state.keys())}\nNext:{[n[0] if not isinstance(n[0],list) else 'parallel' for n in remaining[:3]]}",
        system=system,json_mode=True)
    try: return json.loads(raw)
    except: return {"action":"continue","reason":"parse_failed"}

async def plan_dag(goal: str, available_caps: Optional[List[str]] = None) -> dict:
    """
    Ask the LLM to produce a validated DAG plan for a natural-language goal.

    System prompt includes:
      - Full parameter schemas for every capability (not just names)
      - Strict DAG syntax rules with a worked example
      - Explicit instruction to ONLY use caps from the provided list
      - JSON extraction that tolerates chatty LLM responses
    """
    cap_keys = available_caps or list(CAPABILITY_REGISTRY.keys())

    # Build rich capability reference with types and required markers
    def _cap_sig(k):
        cap = CAPABILITY_REGISTRY.get(k, {})
        props = cap.get("schema", {}).get("properties", {})
        req   = set(cap.get("schema", {}).get("required", []))
        params = ", ".join(
            _format_param_sig(p, v, req)
            for p, v in props.items()
            if p not in ("trace_id",)
        )
        desc = cap.get("description", "")[:80]
        return f"  {k}({params}) — {desc}"

    cap_desc = "\n".join(_cap_sig(k) for k in cap_keys)

    system = (
        "You are a Vera DAG planner. Build a minimal, correct DAG for the user's goal.\n\n"
        "RULES (violating any rule produces a broken DAG):\n"
        "1. ONLY use capability names from the provided list — no invented names.\n"
        "2. Output keys are arbitrary snake_case strings — they become state keys for later nodes.\n"
        "3. Capability inputs are matched from the state dict by parameter name.\n"
        "   Put required inputs in initial_state.\n"
        "4. Node formats:\n"
        "   Sequential  : [\"cap_name\", \"output_key\"]\n"
        "   Parallel    : [[\"cap_a\",\"key_a\"],[\"cap_b\",\"key_b\"]]  <-- array of arrays\n"
        "   Conditional : [\"cap_name\", \"output_key\", \"CONDITION:prior_key\"]\n"
        "5. Keep DAGs short (3-7 nodes). Do not add redundant steps.\n"
        "6. system.ping accepts: host(str!). Use it to check if a URL/host is reachable.\n"
        "7. http.get accepts: url(str!). Use it to fetch a URL and check the response.\n\n"
        "ALSO frame the plan with these fields (same convention as the agentic loop):\n"
        "  • problem    — restate the goal in one sentence (the problem/goal).\n"
        "  • subgoals   — ordered list of {step, description, caps:[...]} (steps/subgoals).\n"
        "  • validation — how success is verified once the DAG has run.\n\n"
        "EXAMPLE — check if example.com is up:\n"
        '{"problem":"Determine whether example.com is reachable and summarise its response.",'
        '"subgoals":[{"step":"fetch","description":"GET the URL","caps":["http.get"]},'
        '{"step":"summarise","description":"Summarise the response","caps":["llm.generate"]}],'
        '"validation":"site_resp is non-empty and the summary describes a valid HTTP response.",'
        '"dag":[["http.get","site_resp"],["llm.generate","summary","CONDITION:site_resp"]],'
        '"initial_state":{"url":"http://example.com","prompt":"Summarise this HTTP response: {{site_resp}}"},'
        '"rationale":"Fetch the URL then summarise the result"}\n\n'
        "Return your response as brief prose explanation followed by a single ```json code block "
        "containing the plan. No other JSON blocks."
    )

    raw = await ollama_generate(
        f"Goal: {goal}\n\nAvailable capabilities:\n{cap_desc}",
        system=system,
        prefer_gpu=True,
        # Don't use json_mode — it prevents the LLM from adding the code fence we parse
    )

    if not raw:
        return {"error": "LLM returned empty response", "dag": [], "initial_state": {}}

    # Robust extraction — same 4-strategy approach as the client-side extractor
    def _extract(text: str) -> Optional[dict]:
        import re as _re
        # Strategy 1: fenced ```json blocks (preferred)
        for block in _re.findall(r'```(?:json)?\s*([\s\S]*?)```', text):
            try:
                p = json.loads(block.strip())
                if isinstance(p, dict) and isinstance(p.get("dag"), list):
                    return p
            except Exception:
                pass
        # Strategy 2: outermost {} object
        m = _re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                p = json.loads(m.group())
                if isinstance(p, dict) and isinstance(p.get("dag"), list):
                    return p
            except Exception:
                pass
        # Strategy 3: whole text
        try:
            p = json.loads(text.strip())
            if isinstance(p, dict) and isinstance(p.get("dag"), list):
                return p
        except Exception:
            pass
        return None

    plan = _extract(raw)
    if not plan:
        return {"error": f"Could not parse DAG from LLM response", "raw": raw[:500],
                "dag": [], "initial_state": {}}

    # Validate — reject any node whose capability is not in the registry
    unknown = []
    for node in _flatten_dag(plan.get("dag", [])):
        cap_name = node[0]
        if cap_name not in CAPABILITY_REGISTRY:
            unknown.append(cap_name)
    if unknown:
        log.warning("plan_dag: unknown caps in plan: %s", unknown)
        # Attempt self-correction: re-run with the bad caps highlighted
        fix_prompt = (
            f"Goal: {goal}\n\n"
            f"Your previous plan used these INVALID capability names that don't exist: {unknown}\n"
            f"You MUST only use capabilities from this list. Try again.\n\n"
            f"Available capabilities:\n{cap_desc}"
        )
        raw2 = await ollama_generate(fix_prompt, system=system, prefer_gpu=True)
        plan2 = _extract(raw2 or "")
        if plan2:
            unknown2 = [
                node[0] for node in _flatten_dag(plan2.get("dag", []))
                if node[0] not in CAPABILITY_REGISTRY
            ]
            if len(unknown2) < len(unknown):
                plan = plan2
                unknown = unknown2

        if unknown:
            plan["warnings"] = [f"Unknown capability in plan: {u}" for u in unknown]

    plan.setdefault("initial_state", {})
    plan.setdefault("rationale", "")
    # Plan convention (problem → subgoals → validation), mirroring the agentic loop.
    plan.setdefault("problem", goal)
    plan.setdefault("subgoals", [])
    plan.setdefault("validation", "")
    return plan

def _flatten_dag(dag):
    flat=[]
    for node in dag:
        if isinstance(node,list) and isinstance(node[0],list):
            for n in node: flat.extend(_flatten_dag([n]))
        else: flat.append(node)
    return flat

async def route_llm(prompt: str, prefer: Optional[str] = None) -> Any:
    candidates=sorted([k for k in CAPABILITY_REGISTRY if "llm" in k.lower()],
                      key=lambda k:(0 if k==prefer else 1))
    for cap_name in candidates:
        try: return await CAPABILITY_REGISTRY[cap_name]["func"](prompt=prompt)
        except Exception as e: log.warning("route_llm %s: %s",cap_name,e)
    text=await ollama_generate(prompt)
    return {"text":text,"model":OLLAMA_MODEL,"fallback":True}

# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────
# LEECH BOOT (dev sandbox): heavy AMBIENT scheduled jobs that generate Ollama
# traffic (embeddings/benchmarks/model pulls), crawl/fetch external sources, or
# auto-run loops/programs must NOT fire inside a dev sandbox — the sandbox should
# leech prod's already-computed state (read-through) rather than independently
# recompute it, and its GPU budget belongs to the loop/test actually under study.
# One-time _startup module-init hooks (interval 999999) are NOT here: a sandbox
# still needs its panels/state initialised. A job may also self-declare via
# schedule(..., skip_in_sandbox=True). See is_dev_sandbox()'s docstring.
_SANDBOX_SKIP_JOBS = {
    "agent_rag_refresh",       # re-embeds every agent's knowledge dataset
    "bench_node_perf",         # runs benchmark GENERATIONS against the nodes
    "model_sync",              # polls every Ollama node's model inventory
    "catalog_autoopt",         # model optimisation sweeps
    "catalog_pull_sweep",      # background model PULLS
    "cal_auto_sync",           # external calendar sync (no side effects from a sandbox)
    "longterm_scheduler",      # would auto-fire scheduled LOOPS in the sandbox
    "v8_program_tick",         # would auto-advance long-horizon PROGRAMS
    "worldview_startup_load",  # loads/embeds the worldview model
}
_SANDBOX_SKIP_LOGGED = set()


def schedule(fn: Callable, interval: float, name: Optional[str] = None,
             skip_in_sandbox: bool = False):
    SCHEDULED_TASKS.append({"fn": fn, "int": interval, "name": name or fn.__name__,
                            "last": None, "runs": 0, "skip_in_sandbox": skip_in_sandbox})

async def scheduler_loop():
    _sandbox = is_dev_sandbox()
    while True:
        now=datetime.utcnow()
        for task in SCHEDULED_TASKS:
            # Leech boot: never fire heavy ambient jobs inside a dev sandbox.
            if _sandbox and (task.get("skip_in_sandbox")
                             or task["name"] in _SANDBOX_SKIP_JOBS):
                if task["name"] not in _SANDBOX_SKIP_LOGGED:
                    _SANDBOX_SKIP_LOGGED.add(task["name"])
                    log.info("leech boot: skipping ambient job '%s' in dev sandbox",
                             task["name"])
                continue
            last=task["last"]
            if last is None or (now-last).total_seconds()>=task["int"]:
                task["last"]=now; task["runs"]+=1
                asyncio.create_task(task["fn"]())
                # NOTE: do NOT emit scheduler.run events — they flood the WS/obsIngest
                # causing O(n) unshift() churn on _obsEvents every second. Log only.
                log.debug("scheduler: %s run #%d", task["name"], task["runs"])
        await asyncio.sleep(1)

# ─────────────────────────────────────────────────────────────────────────────
#  ██  BUILT-IN CAPABILITIES  (all declared with @capability)
#      These replace every former @APP.get / @APP.post route.
# ─────────────────────────────────────────────────────────────────────────────

# ── Observability ─────────────────────────────────────────────────────────────

@capability("obs.provenance", memory="off", silent=True,
            http_method="GET", http_path="/obs/provenance", http_tags=["obs"],
            description="Git provenance of the RUNNING process: which commit/branch it "
                        "is on, whether the checkout is dirty (uncommitted code), plus "
                        "instance/pid/start time — computed once at boot. Every emitted "
                        "event also carries a compact {ver, br, dirty} so any event, log, "
                        "run or error correlates to the exact code that produced it. "
                        "Output: {git_sha, git_sha_short, branch, dirty, instance, pid, "
                        "started_at}.")
async def cap_obs_provenance(trace_id=None):
    from Vera.vera.provenance import get_provenance
    return get_provenance()


# ── MCP ───────────────────────────────────────────────────────────────────────

@capability("mcp.tools", memory="off", silent=True,
            http_method="GET", http_path="/mcp/tools", http_tags=["mcp"],
            mcp_expose=False,
            description="List all registered capabilities (MCP tool manifest).")
async def mcp_tools(trace_id=None):
    return [
        {"name":k,"description":v.get("description",""),"schema":v.get("schema",{}),
         "mode":v.get("mode","local"),"source":v.get("source","local"),
         "streams":v.get("streams",[]),"tags":v.get("tags",[])}
        for k,v in CAPABILITY_REGISTRY.items()
        if v.get("mcp_expose",True)
    ]

@capability("mcp.call", memory="auto",
            http_method="POST", http_path="/mcp/call", http_tags=["mcp"],
            mcp_expose=False,
            description="Invoke any capability by name via MCP protocol.")
async def mcp_call_endpoint(name: str, arguments: str = "", trace_id=None):
    """
    Called internally (from WS or DAG) with arguments already a dict.
    When called via REST the generic handler passes **body, so name and arguments
    arrive as kwargs — arguments will be a dict from JSON body.
    The str type hint is just for schema generation; we handle both.
    """
    if isinstance(arguments, str):
        try:    args = json.loads(arguments) if arguments.strip() else {}
        except: args = {}
    else:
        args = arguments or {}
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        raise HTTPException(404, f"Unknown capability: {name}")
    tid    = trace_id or new_id()
    result = await cap["func"](**args, trace_id=tid)
    return {"type": "tool_result", "tool_name": name, "trace_id": tid, "content": result}


def _make_mcp_call_handler():
    """
    Dedicated handler for POST /mcp/call.
    Accepts body: {"name": "...", "arguments": {...}, "trace_id": "..."}
    where arguments is always a plain dict (not a JSON string).
    This bypasses the generic _make_post_handler to avoid the str/dict type confusion.
    """
    async def _handler(request: Request):
        try:
            raw  = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            raise HTTPException(400, "Invalid JSON body")

        name = body.get("name")
        if not name:
            raise HTTPException(400, "Missing 'name' in request body")

        args = body.get("arguments") or {}
        if isinstance(args, str):
            try:    args = json.loads(args)
            except: args = {}

        cap = CAPABILITY_REGISTRY.get(name)
        if not cap:
            raise HTTPException(404, f"Unknown capability: {name}")

        # Filter args to accepted params — prevents unexpected kwarg errors
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        if accepted:
            args = {k: v for k, v in args.items() if k in accepted}

        # Server-side type coercion using the cap schema.
        # The LLM often emits integers and booleans as strings
        # (e.g. timeout="300" or timeout="300s" instead of timeout=300).
        # Without coercion these reach the cap function as strings and cause
        # TypeErrors at runtime (e.g. asyncio.wait_for(coro, timeout="300s")).
        import re as _re
        _UNIT_RE = _re.compile(r'^([\d.]+)\s*(ms|s|m|h|kb|mb|gb)?$', _re.I)
        schema_props = cap.get("schema", {}).get("properties", {})
        if schema_props:
            for k, v in list(args.items()):
                if v is None or k not in schema_props:
                    continue
                declared = schema_props[k].get("type", "")
                try:
                    if declared in ("integer", "number") and not isinstance(v, (int, float)):
                        sv = str(v).strip()
                        # Strip common unit suffixes that LLMs append:
                        # "300s" → "300", "10ms" → "10", "5m" → "5"
                        um = _UNIT_RE.match(sv)
                        if um:
                            sv = um.group(1)
                        n = float(sv)
                        args[k] = int(round(n)) if declared == "integer" else n
                    elif declared == "boolean" and not isinstance(v, bool):
                        sv = str(v).lower()
                        args[k] = sv in ("true", "1", "yes", "on")
                    elif declared == "string" and not isinstance(v, str):
                        args[k] = str(v)
                    elif declared == "array" and isinstance(v, str):
                        try:
                            args[k] = json.loads(v)
                        except Exception:
                            pass
                    elif declared == "object" and isinstance(v, str):
                        try:
                            args[k] = json.loads(v)
                        except Exception:
                            pass
                except Exception:
                    pass  # keep original if coercion fails

        tid = body.get("trace_id") or new_id()

        # Session plumb-through — the chat panel (and other UIs) execute caps via
        # this endpoint, and per-session routing (sandbox containers, event
        # scoping) needs to know WHICH session is calling. Accept a top-level
        # session_id (or one inside arguments), inject it into the cap call when
        # the cap accepts it, and set the syslog trigger chain so NESTED cap
        # calls made by this cap inherit the session too (same mechanism
        # chat.stream uses).
        _raw_args = body.get("arguments")
        session_id = str(body.get("session_id") or "").strip()
        if not session_id and isinstance(_raw_args, dict):
            session_id = str(_raw_args.get("session_id") or "").strip()
        if session_id:
            if "session_id" in accepted:
                args.setdefault("session_id", session_id)
            try:
                _vera_syslog = sys.modules.get("syslog")
                if _vera_syslog and hasattr(_vera_syslog, "set_trigger"):
                    _vera_syslog.set_trigger(tid, "mcp.call", session_id)
            except Exception:
                pass

        caller_kind = str(body.get("caller_kind") or "").strip()
        _ck_token = CALLER_KIND.set(caller_kind) if caller_kind else None
        try:
            result = await cap["func"](**args, trace_id=tid)
            return await _json_response(
                {"type": "tool_result", "tool_name": name, "trace_id": tid, "content": result}
            )
        except HTTPException:
            raise
        except asyncio.CancelledError:
            log.debug("mcp/call cancelled (client disconnected) for %s", name)
            raise
        except Exception as e:
            log.error("mcp/call cap %s: %s", name, e)
            raise HTTPException(500, str(e))
        finally:
            if _ck_token is not None:
                CALLER_KIND.reset(_ck_token)

    _handler.__name__ = "_post_mcp_call"
    return _handler

@capability("mcp.servers", memory="off", silent=True,
            http_method="GET", http_path="/mcp/servers", http_tags=["mcp"],
            description="List registered external MCP servers.")
async def mcp_servers_list(trace_id=None):
    return {"servers":MCP_SERVERS}

@capability("mcp.register_server", memory="off",
            http_method="POST", http_path="/mcp/servers/register", http_tags=["mcp"],
            description="Register an external MCP server and proxy its capabilities.")
async def mcp_register_server(url: str, name: str = "", trace_id=None):
    srv_name=name or url
    registered=await register_mcp_server(url,srv_name)
    return {"registered":registered,"server":srv_name,"count":len(registered)}

# ── Dev-mode controls ─────────────────────────────────────────────────────────
# Operations that are useful while ITERATING on Vera itself and unacceptable in
# normal running. Gated on VERA_DEV_MODE so they simply do not function unless
# the operator has opted in; the caps stay visible so the gate is discoverable
# rather than a mystery 404.

def dev_mode_on() -> bool:
    return str(os.environ.get("VERA_DEV_MODE", "")).strip().lower() in ("1", "true", "yes", "on")


def is_dev_sandbox() -> bool:
    """True inside a Loop Lab dev sandbox (docker-compose.dev.yml sets
    VERA_IS_DEV_SANDBOX=1 — see evolve_capabilities.py's _dev_compose_yaml).

    A sandbox is a FULL Vera process sharing prod's real backing services —
    crucially including the same physical Ollama nodes (no isolation there,
    unlike Redis/Postgres which get a dedicated DB). Every background job that
    auto-starts on process boot (dream's scheduler + ambient director loop,
    scheduled auto-ingest, ...) therefore also runs inside every sandbox,
    silently generating Ollama traffic that competes with whatever loop/test
    is actually being run there — invisible because it's not part of any
    session anyone is watching. Confirmed live 2026-08-03: an 80+ minute
    orchestrator-planning hang in a Loop Lab test coincided with exactly this
    — the sandbox's own dream director had no reason not to be running too.
    Call sites that start ambient/incidental background work (not the actual
    thing under test) should check this and skip in a sandbox."""
    return str(os.environ.get("VERA_IS_DEV_SANDBOX", "")).strip().lower() in ("1", "true", "yes", "on")


def _relaunch_argv() -> List[str]:
    """The argv to re-exec this process with — how it was actually started."""
    return [sys.executable, "-m", "Vera.vera.capability_orchestration"]


async def _do_restart(delay: float) -> None:
    """Re-exec Vera in place after `delay` seconds.

    os.execv REPLACES this process image: same PID, same parent, same cwd and
    environment (so PYTHONPATH from build.sh survives). That matters here because
    `build.sh run` is a ONE-SHOT — it does not restart the process — so simply
    exiting would take Vera down with nothing to bring it back. execv also fails
    SAFE: on error the current process keeps running rather than dying.

    Shutdown hooks run first so anything holding external state (e.g. the Loop
    Lab sandbox in follow-host mode) is released before the swap.
    """
    await asyncio.sleep(max(0.2, delay))
    log.warning("DEV RESTART: re-exec %s", " ".join(_relaunch_argv()))
    for _hook in list(SHUTDOWN_HOOKS):
        try:
            await _hook()
        except Exception as e:
            log.warning("restart: shutdown hook failed: %s", e)
    for closer, name in ((getattr(REDIS, "aclose", None), "redis"),
                         (getattr(PG_POOL, "close", None), "postgres"),
                         (getattr(NEO, "close", None), "neo4j")):
        try:
            if closer:
                await closer()
        except Exception as e:
            log.debug("restart: closing %s: %s", name, e)
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execv(sys.executable, _relaunch_argv())
    except Exception as e:                      # execv only returns on failure
        log.error("DEV RESTART FAILED — Vera is still running on the OLD code: %s", e)


@capability("sys.dev.restart", memory="off",
            http_method="POST", http_path="/sys/dev/restart", http_tags=["sys"],
            description="DEV MODE ONLY. Restart Vera in place so freshly-edited source is "
                        "loaded, without anyone having to intervene on the host. Re-execs the "
                        "process (same PID/parent), running shutdown hooks and closing backends "
                        "first; `build.sh run` does not respawn, so this re-exec — not an exit — "
                        "is what makes it safe. Requires VERA_DEV_MODE=1 and confirm=True. "
                        "In-flight work IS lost: agentic loops, chat streams and queued jobs are "
                        "killed mid-execution. Inputs: confirm (bool!), delay_s (float, default "
                        "1.5 — time to return this response before the swap), reason (str). "
                        "Output: {ok, restarting, pid, argv}.")
async def cap_sys_dev_restart(confirm: bool = False, delay_s: float = 1.5,
                              reason: str = "", trace_id=None):
    if not dev_mode_on():
        return {"ok": False, "error": "dev mode is off — set VERA_DEV_MODE=1 to enable "
                                      "sys.dev.restart", "dev_mode": False}
    if not confirm:
        return {"ok": False, "error": "confirm=True is required — this kills all in-flight "
                                      "work (agentic loops, chat streams, queued jobs)",
                "dev_mode": True}
    log.warning("sys.dev.restart requested%s", f" — {reason}" if reason else "")
    try:
        await emit_event({"type": "sys.dev.restart", "reason": reason,
                          "pid": os.getpid(), "delay_s": delay_s})
    except Exception:
        pass
    # Detached so THIS request can return before the process image is replaced —
    # otherwise the caller only ever sees a dropped connection.
    asyncio.create_task(_do_restart(float(delay_s or 1.5)))
    return {"ok": True, "restarting": True, "pid": os.getpid(),
            "argv": _relaunch_argv(), "delay_s": float(delay_s or 1.5),
            "note": "Vera is re-execing; it should answer again within a few seconds. "
                    "In-flight loops/streams are gone."}


async def _do_stop(delay: float) -> None:
    """Clean shutdown after `delay` seconds — runs the same shutdown hooks/backend
    closes as _do_restart but exits instead of re-execing. Only meaningful when
    something OUTSIDE this process (a supervisor, or a person at the host) will
    bring it back up; `build.sh run`/`sys.dev.restart`'s own re-exec are the two
    ways Vera comes back after this, this capability does neither on its own."""
    await asyncio.sleep(max(0.2, delay))
    log.warning("DEV STOP: shutting down (pid %s)", os.getpid())
    for _hook in list(SHUTDOWN_HOOKS):
        try:
            await _hook()
        except Exception as e:
            log.warning("stop: shutdown hook failed: %s", e)
    for closer, name in ((getattr(REDIS, "aclose", None), "redis"),
                         (getattr(PG_POOL, "close", None), "postgres"),
                         (getattr(NEO, "close", None), "neo4j")):
        try:
            if closer:
                await closer()
        except Exception as e:
            log.debug("stop: closing %s: %s", name, e)
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)   # hard exit — no atexit/finally surprises mid-shutdown


@capability("sys.dev.stop", memory="off",
            http_method="POST", http_path="/sys/dev/stop", http_tags=["sys"],
            description="DEV MODE ONLY. Cleanly shut Vera DOWN (not restart) — closes "
                        "Redis/Postgres/Neo4j and exits. Nothing brings it back up "
                        "automatically (`build.sh run` is one-shot); use this only when "
                        "something external is expected to relaunch it, or when you "
                        "genuinely want it down. Requires VERA_DEV_MODE=1 and confirm=True. "
                        "Inputs: confirm (bool!), delay_s (float, default 1.5), reason (str). "
                        "Output: {ok, stopping, pid}.")
async def cap_sys_dev_stop(confirm: bool = False, delay_s: float = 1.5,
                           reason: str = "", trace_id=None):
    if not dev_mode_on():
        return {"ok": False, "error": "dev mode is off — set VERA_DEV_MODE=1 to enable "
                                      "sys.dev.stop", "dev_mode": False}
    if not confirm:
        return {"ok": False, "error": "confirm=True is required — this kills all in-flight "
                                      "work and does NOT come back on its own",
                "dev_mode": True}
    log.warning("sys.dev.stop requested%s", f" — {reason}" if reason else "")
    try:
        await emit_event({"type": "sys.dev.stop", "reason": reason,
                          "pid": os.getpid(), "delay_s": delay_s})
    except Exception:
        pass
    asyncio.create_task(_do_stop(float(delay_s or 1.5)))
    return {"ok": True, "stopping": True, "pid": os.getpid(),
            "delay_s": float(delay_s or 1.5),
            "note": "Vera is shutting down and will NOT relaunch on its own."}


def _env_file_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


# Deliberately broad and keyword-based rather than an allowlist: a keyword
# denylist is easy to get wrong (FABRIC_S3_ACCESS slipped past the first
# version of this list, undetected until a live call actually printed the
# access key in cleartext), so err toward over-redacting. Still gated behind
# VERA_DEV_MODE below as defense in depth — this list should never be the
# ONLY thing standing between a secret and an HTTP response.
_ENV_SECRET_HINTS = ("SECRET", "PASSWORD", "PASS", "TOKEN", "KEY", "CREDENTIAL",
                     "ACCESS", "AUTH", "PRIVATE", "APIKEY", "CERT")


def _env_redact(key: str, val: str) -> str:
    return "••••••••" if any(h in key.upper() for h in _ENV_SECRET_HINTS) else val


@capability("sys.env.get", memory="off", silent=True,
            http_method="GET", http_path="/sys/env/get", http_tags=["sys"],
            description="DEV MODE ONLY. Read the repo-root .env file (the one "
                        "vera.config._load_dotenv_files reads at process start for "
                        "native/non-docker launches). Secret-looking keys (see "
                        "_ENV_SECRET_HINTS) are redacted, but this is a keyword "
                        "heuristic, not a guarantee — gated on VERA_DEV_MODE for "
                        "that reason, same as the rest of the sys.dev.* family. "
                        "Output: {path, vars: {key: value}}.")
async def cap_sys_env_get(trace_id=None):
    if not dev_mode_on():
        return {"ok": False, "error": "dev mode is off — set VERA_DEV_MODE=1 to enable "
                                      "sys.env.get", "dev_mode": False}
    path = _env_file_path()
    out: Dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, _, v = line.partition("=")
            k = k.strip()
            if k:
                out[k] = _env_redact(k, v.strip())
    return {"path": str(path), "vars": out}


@capability("sys.env.set", memory="off",
            http_method="POST", http_path="/sys/env/set", http_tags=["sys"],
            description="DEV MODE ONLY. Set (or add) a KEY=VALUE line in the repo-root .env "
                        "file — the generic 'control other system variables' knob (e.g. "
                        "VERA_CLAUDE_SESSIONS_INGEST_INTERVAL, OLLAMA_MAX_AUTO_CTX). Preserves "
                        "every other line/comment/order in the file. Also updates the CURRENT "
                        "process's os.environ so anything that reads the var live (not just at "
                        "import time) sees it immediately — but most .env values (this one "
                        "included) are only read ONCE at process/module import, so a restart "
                        "(sys.dev.restart) is usually still required for it to actually take "
                        "effect; pass restart=True to chain one automatically. Requires "
                        "VERA_DEV_MODE=1 and confirm=True. Inputs: key (str!), value (str!), "
                        "confirm (bool!), restart (bool — also restart Vera after writing). "
                        "Output: {ok, path, key, value, restarting}.")
async def cap_sys_env_set(key: str = "", value: str = "", confirm: bool = False,
                          restart: bool = False, trace_id=None):
    if not dev_mode_on():
        return {"ok": False, "error": "dev mode is off — set VERA_DEV_MODE=1 to enable "
                                      "sys.env.set", "dev_mode": False}
    key = (key or "").strip()
    if not key or not key.replace("_", "").isalnum():
        return {"ok": False, "error": "key must be a non-empty alphanumeric/underscore name"}
    if not confirm:
        return {"ok": False, "error": "confirm=True is required", "dev_mode": True}
    path = _env_file_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    new_line = f"{key}={value}"
    found = False
    for i, raw in enumerate(lines):
        line = raw.strip()
        probe = line[len("export "):] if line.startswith("export ") else line
        if probe.split("=", 1)[0].strip() == key and not line.startswith("#"):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value
    log.warning("sys.env.set: %s=%s (restart=%s)", key, _env_redact(key, value), restart)
    try:
        await emit_event({"type": "sys.env.set", "key": key,
                          "value": _env_redact(key, value), "restart": restart})
    except Exception:
        pass
    out = {"ok": True, "path": str(path), "key": key, "value": value, "restarting": False}
    if restart:
        r = await cap_sys_dev_restart(confirm=True, reason=f"sys.env.set {key}")
        out["restarting"] = bool(r.get("restarting"))
    return out


# ── Observability ─────────────────────────────────────────────────────────────

@capability("obs.health", memory="off", silent=True,
            http_method="GET", http_path="/health", http_tags=["obs"],
            description="Overall orchestrator health: backends, workers, caps, Ollama nodes.")
async def obs_health(trace_id=None):
    return {"redis":bool(REDIS),"postgres":bool(PG_POOL),"chroma":bool(CHROMA),
            "neo4j":bool(NEO),"workers":len(WORKER_REGISTRY),"caps":len(CAPABILITY_REGISTRY),
            "mcp_servers":len(MCP_SERVERS),
            "ollama":{iid:{"status":i["status"],"latency_ms":i["latency_ms"],"has_gpu":i["has_gpu"]}
                      for iid,i in OLLAMA_INSTANCES.items()},
            "mode":"distributed" if REDIS else "local"}


@capability("obs.neo4j_diag", memory="off", silent=True,
            http_method="GET", http_path="/health/neo4j", http_tags=["obs"],
            description="Diagnose why the Neo4j backend is up/down: shows the driver "
                        "package presence, the RESOLVED uri/user (from cfg/env — password "
                        "masked), whether NEO is connected, and a LIVE connectivity probe "
                        "with the exact error if it fails. Use this when /health shows neo4j:false.")
async def obs_neo4j_diag(trace_id=None):
    uri  = getattr(cfg, "NEO4J_URI", "bolt://localhost:7687")
    user = (getattr(cfg, "NEO4J_USER", "") or "")
    pw   = (getattr(cfg, "NEO4J_PASS", "") or "")
    out = {
        "driver_installed": HAS_NEO,
        "driver_import_error": _NEO_IMPORT_ERR,
        "resolved_uri": uri,
        "resolved_user": user,
        "password_set": bool(pw),
        "connected": bool(NEO),
        "probe": None,
    }
    if not HAS_NEO:
        out["probe"] = {"ok": False, "error": "neo4j driver not installed (pip install neo4j)"}
        return out
    # Live probe with the configured auth, then no-auth fallback — report the
    # ACTUAL error class so the cause (bad host, wrong/expired password, auth
    # disabled, refused connection) is unambiguous.
    attempts = []
    for label, auth in (("with-auth", (user, pw) if user else None), ("no-auth", None)):
        drv = None
        try:
            drv = AsyncGraphDatabase.driver(uri, auth=auth)
            await drv.verify_connectivity()
            attempts.append({"auth": label, "ok": True})
            out["probe"] = {"ok": True, "auth": label, "uri": uri}
            break
        except Exception as e:
            attempts.append({"auth": label, "ok": False,
                             "error_type": type(e).__name__, "error": str(e)[:300]})
        finally:
            if drv is not None:
                try: await drv.close()
                except Exception: pass
    if not out["probe"]:
        out["probe"] = {"ok": False, "attempts": attempts}
    else:
        out["probe"]["attempts"] = attempts
    return out

@capability("ollama.gate.status", memory="off", silent=True,
            http_method="GET", http_path="/ollama/gate", http_tags=["ollama", "obs"],
            description="Cross-process Ollama GPU gate ('one big queue') status: "
                        "whether it is enabled, the coordination Redis DB, and per-node "
                        "slot capacity / held / free (live occupancy shared across prod + "
                        "every dev sandbox). Output: {enabled, coord_db, nodes:[...]}.")
async def ollama_gate_status(trace_id=None):
    await _ensure_coord_redis()
    nodes = []
    for iid, inst in OLLAMA_INSTANCES.items():
        cap = _gate.capacity_for(bool(inst.get("has_gpu")))
        if cap <= 0:
            nodes.append({"node": iid, "has_gpu": bool(inst.get("has_gpu")),
                          "capacity": 0, "gated": False})
            continue
        occ = await _gate.occupancy(COORD_REDIS, iid, cap)
        nodes.append({"node": iid, "has_gpu": bool(inst.get("has_gpu")),
                      "gated": True, **occ})
    return {"enabled": _GATE_ON, "coord_connected": COORD_REDIS is not None,
            "coord_db": COORD_REDIS_DB,
            "gpu_cap": _gate.capacity_for(True), "node_cap": _gate.capacity_for(False),
            "nodes": nodes}


@capability("obs.workers", memory="off", silent=True,
            http_method="GET", http_path="/workers", http_tags=["obs"],
            description="Worker registry — all hosts merged from Redis + local fallback.")
async def obs_workers(trace_id=None):
    merged = {}

    # Read ALL workers from Redis first (source of truth across hosts)
    if REDIS:
        try:
            keys = await REDIS.keys("vera:workers:*")
            for k in keys:
                raw = await REDIS.hgetall(k)
                if not raw:
                    continue
                # Decode bytes keys/values
                rec = {
                    (rk.decode() if isinstance(rk, bytes) else rk):
                    (rv.decode() if isinstance(rv, bytes) else rv)
                    for rk, rv in raw.items()
                }
                # Derive worker id from key if not in record
                key_str = k.decode() if isinstance(k, bytes) else k
                wid = rec.get("id") or key_str.rsplit(":", 1)[-1]

                # Deserialise capabilities — handle both JSON and str() formats
                caps_raw = rec.get("capabilities", "[]")
                try:
                    rec["capabilities"] = json.loads(caps_raw)
                except (json.JSONDecodeError, TypeError):
                    # Fallback: try to parse Python repr single-quoted list
                    try:
                        import ast
                        rec["capabilities"] = ast.literal_eval(caps_raw)
                    except Exception:
                        rec["capabilities"] = []

                # Coerce all numeric fields
                for field in ("tasks_done", "tasks_failed", "cap_count"):
                    try:
                        rec[field] = int(float(rec.get(field, 0) or 0))
                    except (ValueError, TypeError):
                        rec[field] = 0

                # Ensure all expected fields exist
                rec.setdefault("host", "unknown")
                rec.setdefault("status", "unknown")
                rec.setdefault("started", "")
                rec.setdefault("current_task", "")
                rec.setdefault("task_started", "")
                rec.setdefault("ollama_instance", "")
                rec.setdefault("pid", "")

                merged[wid] = rec
        except Exception as e:
            log.warning("obs.workers Redis scan: %s", e)

    # Overlay local in-process data (more accurate for this host's workers)
    for wid, local in WORKER_REGISTRY.items():
        if wid in merged:
            # Update Redis record with live in-process values
            merged[wid].update({
                "status":       local.get("status", "idle"),
                "tasks_done":   local.get("tasks_done", 0),
                "tasks_failed": local.get("tasks_failed", 0),
                "current_task": local.get("current_task", ""),
                "task_started": local.get("task_started", ""),
                "ollama_instance": local.get("ollama_instance", ""),
                "capabilities": local.get("capabilities", []) if isinstance(local.get("capabilities"), list)
                                 else merged[wid].get("capabilities", []),
            })
        else:
            # Worker exists locally but not in Redis yet — include it
            rec = dict(local)
            if isinstance(rec.get("capabilities"), str):
                try:
                    rec["capabilities"] = json.loads(rec["capabilities"])
                except Exception:
                    rec["capabilities"] = []
            merged[wid] = rec

    return merged

@capability("obs.pending", memory="off", silent=True,
            http_method="GET", http_path="/pending", http_tags=["obs"],
            description="Pending result futures (tasks awaiting distributed completion).")
async def obs_pending(trace_id=None):
    return {"count":len(PENDING_RESULTS),"ids":list(PENDING_RESULTS.keys())}

@capability("obs.scheduler", memory="off", silent=True,
            http_method="GET", http_path="/scheduler", http_tags=["obs"],
            description="Scheduled background jobs — name, interval, run count, last run.")
async def obs_scheduler(trace_id=None):
    return [{"name":t["name"],"interval":t["int"],"runs":t["runs"],
             "last":t["last"].isoformat() if t["last"] else None}
            for t in SCHEDULED_TASKS]

@capability("obs.events", memory="off", silent=True,
            http_method="GET", http_path="/events", http_tags=["obs"],
            description="Recent events from Redis event stream.")
async def obs_events(limit: int = 100, trace_id=None):
    if not REDIS: return []
    data=await REDIS.xrevrange(EVENT_STREAM,count=min(limit,500))
    return [json.loads(x[1][b"data"]) for x in data]

@capability("obs.stream_history", memory="off", silent=True,
            http_method="GET", http_path="/streams/history", http_tags=["obs"],
            description="Recent messages from a named Redis stream. Pass ?name=stream_name&limit=50")
async def obs_stream_history(name: str = "", limit: int = 50, trace_id=None):
    if not REDIS or not name: return []
    data=await REDIS.xrevrange(f"vera:stream:{name}",count=min(limit,500))
    return [json.loads(x[1][b"data"]) for x in data]

@capability("obs.redis", memory="off", silent=True,
            http_method="GET", http_path="/redis/inspect", http_tags=["obs"],
            description="Redis server info and vera:* stream key statistics.")
async def obs_redis(trace_id=None):
    if not REDIS: return {"error":"Redis not connected"}
    try:
        info=await REDIS.info(); keys=await REDIS.keys("vera:*"); stats={}
        for k in keys:
            ks=k.decode() if isinstance(k,bytes) else k
            t=(await REDIS.type(k)).decode()
            stats[ks]={"type":t,**({"length":await REDIS.xlen(k)} if t=="stream" else {})}
        return {"connected_clients":info.get("connected_clients"),
                "used_memory_human":info.get("used_memory_human"),
                "uptime_days":info.get("uptime_in_days"),"keys":stats}
    except Exception as e: return {"error":str(e)}

@capability("obs.diagnostics", memory="off", silent=True,
            http_method="GET", http_path="/diagnostics", http_tags=["obs"],
            description="Full connection diagnostics — Redis reachability, bind config, "
                        "worker count, host identity. Use to debug multi-host setup.")
async def obs_diagnostics(trace_id=None):
    import socket
    diag: dict = {
        "host":        socket.gethostname(),
        "redis_url":   REDIS_URL,
        "redis_connected": bool(REDIS),
        "worker_count_local": len(WORKER_REGISTRY),
        "caps": len(CAPABILITY_REGISTRY),
        "mode": "distributed" if REDIS else "local",
    }

    # Try a live Redis ping even if REDIS is already set
    if HAS_REDIS:
        try:
            probe = aioredis.from_url(REDIS_URL, decode_responses=False,
                                      socket_connect_timeout=3, socket_timeout=3)
            await probe.ping()
            info  = await probe.info("server")
            diag["redis_ping"]    = "ok"
            diag["redis_version"] = info.get("redis_version")
            diag["redis_bind"]    = info.get("bind", "not reported")
            diag["redis_port"]    = info.get("tcp_port")
            # Check worker keys from ALL hosts
            wkeys = await probe.keys("vera:workers:*")
            diag["workers_in_redis"] = len(wkeys)
            diag["worker_ids"] = [k.decode().split(":")[-1] for k in wkeys]
            await probe.aclose()
        except Exception as e:
            diag["redis_ping"]  = f"FAILED: {e}"
            diag["redis_hint"]  = (
                "Ping succeeded but Redis connection failed. Common causes: "
                "(1) redis.conf 'bind 127.0.0.1' — change to 'bind 0.0.0.0' or add this host's IP. "
                "(2) 'requirepass' set — add :password@ to REDIS_URL. "
                "(3) 'protected-mode yes' with no bind — set protected-mode no. "
                "(4) firewall blocking port 6379."
            )
    else:
        diag["redis_hint"] = "redis.asyncio not installed"

    return diag

# ── Ollama ────────────────────────────────────────────────────────────────────

@capability("obs.modules", memory="off", silent=True,
            http_method="GET", http_path="/modules", http_tags=["obs"],
            description="List all capability modules loaded at startup — name, path, caps added, status.")
async def obs_modules(trace_id=None):
    return {"modules": LOADED_MODULES, "count": len(LOADED_MODULES)}


# ── Cross-subsystem dashboard aggregation ──────────────────────────────────────
# Loose-coupled lookup into another module's capability — same pattern as
# monitor_capabilities.py's _call(), tolerant of a capability not being loaded
# or raising, so one bad subsystem never breaks the whole snapshot.
async def _cap_call(cap_name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"{cap_name} not loaded"}
    kw.setdefault("trace_id", "")
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _gather_subsystem_snapshot() -> Dict:
    """Fan out once across the subsystems the main-dash health strip and the
    topology map both need. Kept as one shared helper so neither reimplements
    the other's gather logic. `nodes`/`cluster` are only consumed by the
    topology map today, but there's no reason for it to run a second parallel
    gather just for two more cheap (cache-backed, no live SSH) calls."""
    sysmon, mesh, loop_lab, sandboxes, mimic, nodes, cluster, temps, memstats, redis_info, docker_stats, perf = await asyncio.gather(
        _cap_call("sysmon.status"),
        _cap_call("mesh.nodes"),
        _cap_call("evolve.sandbox.status"),
        _cap_call("sandbox.session.list"),
        _cap_call("cluster.mimic.status"),
        _cap_call("nodes.list"),
        _cap_call("obs.cluster"),
        _cap_call("obs.node_temps"),
        _cap_call("memory.stats"),
        _cap_call("obs.redis"),
        _cap_call("docker.stats.top"),
        _cap_call("perf.stalls", limit=5),
    )
    return {"sysmon": sysmon, "mesh": mesh, "loop_lab": loop_lab,
            "sandboxes": sandboxes, "mimic": mimic, "nodes": nodes,
            "cluster": cluster, "temps": temps, "memstats": memstats, "redis": redis_info,
            "docker_stats": docker_stats, "perf": perf}


def _dot_state(ok: bool, exists: bool, warn: bool = False) -> str:
    """Centralised ok/warn/err/unknown classification so every consumer of
    _gather_subsystem_snapshot() (health strip, topology map) agrees on what
    a color means."""
    if not exists:
        return "unknown"
    if ok:
        return "ok"
    return "warn" if warn else "err"


@capability("dash.health.summary", memory="off", silent=True,
            http_method="GET", http_path="/dash/health/summary", http_tags=["obs"],
            description="One compact ok/warn/err/unknown dot per subsystem "
                        "(proxmox, docker, ollama, mesh, loop_lab, sandboxes, "
                        "mimic, fabric, redis) for the main-dashboard health "
                        "strip. Fans out via _gather_subsystem_snapshot(), "
                        "never raises.")
async def dash_health_summary(trace_id=None) -> Dict:
    snap = await _gather_subsystem_snapshot()
    sysmon = snap["sysmon"] if isinstance(snap["sysmon"], dict) else {}
    pmx = sysmon.get("proxmox") or {}
    dkr = sysmon.get("docker") or {}
    oll = sysmon.get("ollama") or {}

    mesh = snap["mesh"] if isinstance(snap["mesh"], dict) else {}
    mesh_nodes = mesh.get("nodes") or []
    mesh_bad = sum(1 for n in mesh_nodes if n.get("status") in ("offline", "stale"))
    mesh_ok_nodes = sum(1 for n in mesh_nodes if n.get("status") == "online")

    ll = snap["loop_lab"] if isinstance(snap["loop_lab"], dict) else {}

    sbx = snap["sandboxes"] if isinstance(snap["sandboxes"], dict) else {}
    sbx_ok = "sandboxes" in sbx and not sbx.get("error")

    mim = snap["mimic"] if isinstance(snap["mimic"], dict) else {}

    mem = snap["memstats"] if isinstance(snap["memstats"], dict) else {}
    backs = (mem.get("backends") or {}) if isinstance(mem.get("backends"), dict) else {}
    fabric_backs = [backs.get(k) or {} for k in ("postgres", "chroma", "neo4j")]
    fabric_ok = sum(1 for b in fabric_backs if b.get("connected"))

    rds = snap["redis"] if isinstance(snap["redis"], dict) else {}
    redis_ok = bool(rds) and not rds.get("error")

    return {
        "proxmox":   _dot_state(bool(pmx.get("ok")), bool(pmx.get("configured")), warn=False),
        "docker":    _dot_state(bool(dkr.get("ok")), bool(dkr.get("total_hosts")), warn=False),
        "ollama":    _dot_state(oll.get("online", 0) == oll.get("total", 0) and oll.get("total", 0) > 0,
                                 bool(oll.get("total")), warn=bool(oll.get("online"))),
        "mesh":      _dot_state(bool(mesh_nodes) and mesh_bad == 0, bool(mesh_nodes),
                                 warn=mesh_ok_nodes > 0),
        "loop_lab":  _dot_state(bool(ll.get("up")) and not ll.get("paused"), "sandbox" in ll,
                                 warn=bool(ll.get("paused"))),
        "sandboxes": _dot_state(sbx_ok, "sandboxes" in sbx, warn=False),
        "mimic":     _dot_state(bool(mim.get("mounted")) and not mim.get("paused"),
                                 bool(mim.get("mounted")), warn=bool(mim.get("paused"))),
        "fabric":    _dot_state(fabric_ok == len(fabric_backs) and fabric_ok > 0, bool(backs),
                                 warn=fabric_ok > 0),
        "redis":     _dot_state(redis_ok, bool(rds), warn=False),
        "ts": now_iso(),
    }


def _temp_status(max_c: Optional[float]) -> str:
    if max_c is None:
        return "unknown"
    if max_c < 70:
        return "ok"
    return "warn" if max_c < 85 else "err"


# obs.node_temps' `error` field is either benign ("no sensors installed on an
# otherwise-fine host") or a real connectivity/auth failure from the SSH probe
# itself. Only the latter should ever demote a node's status — see the topology
# accuracy fix below (a temp probe with no sensors doesn't mean the node's dead).
_TEMP_BENIGN_ERR_PREFIXES = ("no sensors available", "no temperature readings returned")


def _node_leaf_status(backends: List[str], temp_entry: Optional[Dict]) -> str:
    st = "ok" if backends else "unknown"
    if not temp_entry:
        return st
    max_c = temp_entry.get("max_c")
    if max_c is not None:
        tst = _temp_status(max_c)
        if tst in ("warn", "err"):
            return tst
        return st
    err = str(temp_entry.get("error") or "")
    if err and not err.startswith(_TEMP_BENIGN_ERR_PREFIXES):
        # the temp probe's own SSH call hit a real error (permission denied,
        # connection refused, ...) — that's live evidence this "reachable"
        # backend flag is stale, not a dead giveaway of a working host.
        return "err"
    return st


@capability("topology.snapshot", memory="off", silent=True,
            http_method="GET", http_path="/topology/snapshot", http_tags=["obs"],
            description="Live node/edge snapshot of the whole Vera stack for the "
                        "main-dashboard SVG topology map: one hub node, one "
                        "category node per subsystem (Nodes/Workers/Ollama/Mesh/"
                        "Fabric/Docker/Sandboxes), a standalone Redis service "
                        "node, three subsystem-source service nodes (Dream/Chat/"
                        "DAG) plus three kind='monitor' meta nodes (User/Perf/"
                        "Error Monitor — animation endpoints, always 'ok' since "
                        "they're code paths not machines, except Perf which "
                        "reflects a real recent perf.stalls reading; the "
                        "frontend pins 'monitor' nodes to a reserved wedge so "
                        "they don't get lost among a dozen-plus categories), "
                        "and one leaf per "
                        "actual machine/worker/instance/mesh device/Proxmox "
                        "guest/Docker container/session sandbox with an "
                        "ok/warn/err/unknown status (host leaves fold in "
                        "obs.node_temps where available, and a real SSH/auth "
                        "error from the temp probe — as opposed to merely 'no "
                        "sensors installed' — demotes a node to err instead of "
                        "leaving it looking falsely healthy; Proxmox guest "
                        "leaves have no sensor of their own so they INHERIT "
                        "their physical host's max_c by hostname match). "
                        "Reuses _gather_subsystem_snapshot() — the same fan-out "
                        "dash.health.summary uses — rather than a second gather. "
                        "The host page's handleLiveEvent() feeds this same "
                        "element live vera:events directly: worker.start/.done, "
                        "ollama.request/.done/.error (routed through the "
                        "caller's inferred origin — Fabric/Dream/Chat/DAG — then "
                        "the Ollama category, then the exact instance, instead "
                        "of a flat category→leaf hop), and cap.call/cap.ok (the "
                        "universal per-capability activity event every non-"
                        "silent capability already emits — see "
                        "_mirror_cap_activity — mapped by the cap's group prefix "
                        "onto whichever node represents that subsystem: dream/"
                        "chat/agent_loop_v*/dag* -> the matching service node, "
                        "docker -> the Docker category, sandbox/evolve -> the "
                        "Sandboxes category, chat/ide/operator/accounts/"
                        "calendar/email -> User first, perf -> the Perf node, "
                        "any cap.error/ollama.request_error -> an extra chip "
                        "to Error Monitor regardless of which group raised "
                        "it), filtered against the same noisy-group skip-list "
                        "convention _ACT_SKIP_GROUPS already uses server-side "
                        "(obs/health/ui/mcp/session/syslog/panel never "
                        "animate) so a poll-heavy subsystem can't flood the "
                        "map. Also emits 'serves' edges linking a machine leaf "
                        "directly to the exact Ollama instance / Docker host "
                        "nodes.list's own merge already resolved it backs, and "
                        "a Docker container leaf to the exact Fabric backend "
                        "(Postgres/Chroma/Neo4j) its name matches (on top of "
                        "the generic category edge) — real hardware-backs-"
                        "service links, never guessed. Location/network/"
                        "data layers aren't implemented: Vera doesn't collect "
                        "rack/physical placement or flow-level network data "
                        "today, and this map doesn't fake a layer off data "
                        "that doesn't exist. "
                        "Output: {nodes:[{id,label,kind,status,detail,temp_c}], "
                        "edges:[{from,to,kind}], ts}.")
async def topology_snapshot(trace_id=None) -> Dict:
    snap = await _gather_subsystem_snapshot()
    nodes_out: List[Dict] = [{"id": "hub", "label": "Vera", "kind": "hub", "status": "ok", "detail": ""}]
    edges_out: List[Dict] = []

    def _worst(states: List[str]) -> str:
        if "err" in states:
            return "err"
        if "warn" in states:
            return "warn"
        if "ok" in states:
            return "ok"
        return "unknown"

    def _cat(cat_id: str, label: str, status: str):
        nodes_out.append({"id": cat_id, "label": label, "kind": "category", "status": status, "detail": ""})
        edges_out.append({"from": "hub", "to": cat_id})

    temps = snap["temps"] if isinstance(snap["temps"], dict) else {}
    temp_by_host = {h.get("host_id"): h for h in (temps.get("hosts") or []) if h.get("host_id")}

    # ── Nodes: the unified estate (SSH hosts merged with Proxmox/Docker/Ollama/vLLM links) ──
    nl = snap["nodes"] if isinstance(snap["nodes"], dict) else {}
    machines = nl.get("nodes") or []
    if machines:
        leaf_states = []
        for m in machines:
            t = temp_by_host.get(m.get("ssh_host_id"))
            leaf_states.append(_node_leaf_status(m.get("backends") or [], t))
        _cat("cat:nodes", "Nodes", _worst(leaf_states))
        for m, st in zip(machines, leaf_states):
            t = temp_by_host.get(m.get("ssh_host_id"))
            bits = []
            if m.get("backends"):
                bits.append("/".join(m["backends"]))
            if t and t.get("max_c") is not None:
                bits.append(f"{t['max_c']}°C")
            elif t and t.get("error") and not str(t["error"]).startswith(_TEMP_BENIGN_ERR_PREFIXES):
                bits.append(str(t["error"])[:60])
            nid = f"node:{m.get('id', '')}"
            nodes_out.append({"id": nid, "label": m.get("label") or m.get("id"), "kind": "node",
                               "status": st, "detail": " · ".join(bits),
                               "temp_c": t.get("max_c") if t else None})
            edges_out.append({"from": "cat:nodes", "to": nid})

    # ── Proxmox guests: individual VMs/CTs, as leaves under the same "Nodes"
    #    category. A guest has no sensor of its own — it INHERITS the max_c of
    #    the physical PVE host it runs on, which is what "the nodes representing
    #    their hardware" means for something virtual. The cross-reference is
    #    each machine's own proxmox.node (the PVE-internal hostname, e.g.
    #    "corp" — NOT the same as the machine's Vera label, e.g. "PVE01": a
    #    guest's `node` field in proxmox.status is that internal hostname, so
    #    matching on the SSH-registry *label* would silently match nothing).
    #    Capped to the busiest ones (same top_guests already computed for the
    #    dashboard's Proxmox tile) rather than every guest, or a cluster with
    #    40+ guests turns this ring into noise. ──
    sysmon_pmx = ((snap["sysmon"] or {}).get("proxmox") or {}) if isinstance(snap.get("sysmon"), dict) else {}
    top_guests = sysmon_pmx.get("top_guests") or []
    pve_node_to_host_id = {m["proxmox"]["node"]: m.get("ssh_host_id")
                            for m in machines if m.get("proxmox") and m["proxmox"].get("node")}
    if top_guests:
        def _guest_status(g):
            if g.get("status") != "running":
                return "unknown"
            return "ok"
        leaf_states = [_guest_status(g) for g in top_guests]
        for g, st in zip(top_guests, leaf_states):
            host_temp = temp_by_host.get(pve_node_to_host_id.get(g.get("node")))
            bits = [g.get("type", ""), g.get("status", "")]
            if g.get("cpu_pct") is not None:
                bits.append(f"{g['cpu_pct']}% cpu")
            if host_temp and host_temp.get("max_c") is not None:
                bits.append(f"{host_temp['max_c']}°C (host)")
            nid = f"guest:{g.get('vmid')}"
            nodes_out.append({"id": nid, "label": g.get("name") or f"#{g.get('vmid')}", "kind": "node",
                               "status": st, "detail": " · ".join(b for b in bits if b),
                               "temp_c": host_temp.get("max_c") if host_temp else None})
            edges_out.append({"from": "cat:nodes", "to": nid})

    # ── Workers: obs.cluster's live worker registry ──
    cl = snap["cluster"] if isinstance(snap["cluster"], dict) else {}
    workers = cl.get("workers") or {}
    if workers:
        def _worker_status(raw: str) -> str:
            if raw == "idle" or raw.startswith("running"):
                return "ok"
            if raw == "provisioned":
                return "warn"
            return "unknown"
        leaf_states = [_worker_status(str(w.get("status", ""))) for w in workers.values()]
        _cat("cat:workers", "Workers", _worst(leaf_states))
        for (wid, w), st in zip(workers.items(), leaf_states):
            nid = f"worker:{wid}"
            nodes_out.append({"id": nid, "label": wid, "kind": "worker", "status": st,
                               "detail": str(w.get("status", ""))})
            edges_out.append({"from": "cat:workers", "to": nid})

    # ── Ollama: obs.cluster's live per-instance status (richer/fresher than the
    #    sysmon copy, which is sampled on its own 10s cadence and can lag) ──
    oll = cl.get("ollama") or {}
    if oll:
        leaf_states = ["ok" if i.get("status") == "online" else "err" for i in oll.values()]
        _cat("cat:ollama", "Ollama", _worst(leaf_states))
        for (iid, inst), st in zip(oll.items(), leaf_states):
            nid = f"ollama:{iid}"
            nodes_out.append({"id": nid, "label": inst.get("label", iid), "kind": "ollama", "status": st,
                               "detail": f"{inst.get('model_count', 0)} models"
                                         + (f" · {inst.get('vram_used_gb')}GB" if inst.get("vram_used_gb") else "")})
            edges_out.append({"from": "cat:ollama", "to": nid})

    # ── Mesh nodes ──
    mesh = snap["mesh"] if isinstance(snap["mesh"], dict) else {}
    mesh_nodes = mesh.get("nodes") or []
    if mesh_nodes:
        mesh_status_map = {"online": "ok", "stale": "warn", "new": "warn", "offline": "err"}
        leaf_states = [mesh_status_map.get(n.get("status", "offline"), "err") for n in mesh_nodes]
        _cat("cat:mesh", "Mesh", _worst(leaf_states))
        for n, st in zip(mesh_nodes, leaf_states):
            nid = f"mesh:{n.get('node_id', '')}"
            detail = f"{n['rssi']} dBm" if n.get("rssi") is not None else n.get("status", "")
            nodes_out.append({"id": nid, "label": n.get("name") or n.get("node_id"), "kind": "mesh",
                               "status": st, "detail": detail})
            edges_out.append({"from": "cat:mesh", "to": nid})

    # ── Fabric: the data fabric's storage backends (Postgres/Chroma/Neo4j) —
    #    same memory.stats the dashboard's own Postgres/Chroma/Neo4j tiles use ──
    mem = snap["memstats"] if isinstance(snap["memstats"], dict) else {}
    backs = mem.get("backends") or {}
    if backs:
        fabric_labels = {"postgres": "Postgres", "chroma": "Chroma", "neo4j": "Neo4j"}
        leaf_states = ["ok" if (backs.get(k) or {}).get("connected") else "err" for k in fabric_labels]
        _cat("cat:fabric", "Fabric", _worst(leaf_states))
        for (k, label), st in zip(fabric_labels.items(), leaf_states):
            b = backs.get(k) or {}
            nid = f"fabric:{k}"
            if k == "postgres":
                detail = f"{b.get('total', 0)} records" if b.get("connected") else (b.get("error") or "disconnected")
            elif k == "chroma":
                detail = f"{b.get('count', 0)} vectors" if b.get("connected") else (b.get("error") or "disconnected")
            else:
                detail = f"{b.get('nodes', 0)} nodes · {b.get('relationships', 0)} edges" if b.get("connected") else (b.get("error") or "disconnected")
            nodes_out.append({"id": nid, "label": label, "kind": "node", "status": st, "detail": detail})
            edges_out.append({"from": "cat:fabric", "to": nid})

    # ── Redis: the event bus / task queue backbone — a single service, not a
    #    list, so it hangs straight off the hub rather than under a category ──
    rds = snap["redis"] if isinstance(snap["redis"], dict) else {}
    if rds:
        redis_ok = not rds.get("error")
        detail = (f"{rds.get('connected_clients', '?')} clients · {rds.get('used_memory_human', '?')}"
                   if redis_ok else (rds.get("error") or "disconnected"))
        nodes_out.append({"id": "svc:redis", "label": "Redis", "kind": "service",
                           "status": "ok" if redis_ok else "err", "detail": detail})
        edges_out.append({"from": "hub", "to": "svc:redis"})

    # ── Docker: individual containers, not just the host summary the sysmon
    #    tile already covers — reuses docker.stats.top's already-capped
    #    busiest-per-host list (querying every container here too would be
    #    the same "hundreds of containers" problem that capability's own doc
    #    already solved once). ──
    dstats = snap.get("docker_stats") if isinstance(snap.get("docker_stats"), dict) else {}
    dhosts = dstats.get("hosts") or {}
    all_containers = [c for h in dhosts.values() for c in (h.get("containers") or [])]
    if all_containers:
        all_containers.sort(key=lambda c: -(c.get("cpu_pct") or 0))
        leaf_states = ["ok"] * len(all_containers[:14])
        _cat("cat:docker", "Docker", "ok")
        for c in all_containers[:14]:
            nid = f"docker:{c.get('id', '')}"
            bits = []
            if c.get("cpu_pct") is not None:
                bits.append(f"{c['cpu_pct']}% cpu")
            if c.get("mem_mb") is not None:
                bits.append(f"{round(c['mem_mb'])}MB")
            nodes_out.append({"id": nid, "label": c.get("name") or c.get("id"), "kind": "node",
                               "status": "ok", "detail": " · ".join(bits)})
            edges_out.append({"from": "cat:docker", "to": nid})

    # ── Sandboxes: session sandboxes (chat/agent-loop scratch containers) +
    #    the Loop Lab dev sandbox — this subsystem had zero topology presence
    #    before; only active/up ones are shown as leaves (most session
    #    sandboxes in the list are "absent"/"exited" history, not live). ──
    sbx = snap["sandboxes"] if isinstance(snap["sandboxes"], dict) else {}
    active_sbx = [s for s in (sbx.get("sandboxes") or []) if s.get("active")]
    lab = snap["loop_lab"] if isinstance(snap["loop_lab"], dict) else {}
    have_lab = bool(lab.get("sandbox"))
    if active_sbx or have_lab:
        cat_states = (["ok"] * len(active_sbx)) + (["ok" if (lab.get("up") and not lab.get("paused")) else "warn"] if have_lab else [])
        _cat("cat:sandboxes", "Sandboxes", _worst(cat_states) if cat_states else "unknown")
        for s in active_sbx[:14]:
            nid = f"sandbox:{s.get('session_id', '')}"
            nodes_out.append({"id": nid, "label": s.get("label") or s.get("session_id"), "kind": "node",
                               "status": "ok", "detail": f"{s.get('source', '')} · {s.get('state', '')}"})
            edges_out.append({"from": "cat:sandboxes", "to": nid})
        if have_lab:
            lab_ok = lab.get("up") and not lab.get("paused")
            probe = lab.get("probe") or {}
            detail = "paused (idle)" if lab.get("paused") else (probe.get("error") or "up" if lab_ok else "down")
            nodes_out.append({"id": "sandbox:looplab", "label": "Loop Lab", "kind": "node",
                               "status": "ok" if lab_ok else "warn", "detail": detail})
            edges_out.append({"from": "cat:sandboxes", "to": "sandbox:looplab"})

    # ── Hardware -> service structured edges: nodes.list's own _build_nodes()
    #    merge already resolves which physical machine backs which Ollama
    #    instance (URL-host match or the catalog's NODE_SSH map) and which
    #    Docker host id a machine runs — that's a REAL "this hardware serves
    #    that service" relationship, not a guess, so it gets its own edge on
    #    top of the generic category edge instead of everything only ever
    #    fanning out under "Nodes"/"Ollama"/"Docker" with no link between
    #    them. ("192.168.0.138 serves vera" made literal: the machine leaf
    #    gets a direct edge to the exact service leaf it hosts.) Docker is
    #    coarser — individual containers in docker.stats.top aren't tied back
    #    to a specific physical host today, so a matching machine links to
    #    the whole Docker category rather than a fabricated per-container
    #    edge. ──
    existing_ids = {n["id"] for n in nodes_out}
    docker_host_ids_seen = set(dhosts.keys()) if all_containers else set()
    for m in machines:
        nid = f"node:{m.get('id', '')}"
        if nid not in existing_ids:
            continue
        for oi in (m.get("ollama") or []):
            oid = f"ollama:{oi.get('id', '')}"
            if oid in existing_ids:
                edges_out.append({"from": nid, "to": oid, "kind": "serves"})
        dhid = m.get("docker_host_id")
        if dhid and dhid in docker_host_ids_seen:
            edges_out.append({"from": nid, "to": "cat:docker", "kind": "serves"})

    # ── Fabric backends actually run AS Docker containers on this stack
    #    (Postgres/Chroma/Neo4j all deploy that way here) — nodes.list has no
    #    link for this (it only tracks Ollama/vLLM/Proxmox against a machine),
    #    so this is a best-effort NAME match against the same docker.stats.top
    #    containers already rendered as cat:docker leaves above. Only fires
    #    when a container name actually contains the backend's own name —
    #    never a guess dressed up as a link, and silently adds nothing on a
    #    stack where fabric isn't containerised. ──
    _FABRIC_CONTAINER_HINTS = {
        "postgres": ("postgres", "pg"),
        "chroma": ("chroma",),
        "neo4j": ("neo4j",),
    }
    if all_containers:
        for c in all_containers[:14]:
            cid = f"docker:{c.get('id', '')}"
            if cid not in existing_ids:
                continue
            cname = str(c.get("name") or "").lower()
            for fkey, hints in _FABRIC_CONTAINER_HINTS.items():
                fid = f"fabric:{fkey}"
                if fid in existing_ids and any(h in cname for h in hints):
                    edges_out.append({"from": cid, "to": fid, "kind": "serves"})

    # ── Subsystem sources: Dream / Chat / DAG (agent loops) don't have a
    #    machine inventory of their own — they're code paths, always "up" if
    #    Vera itself is up — but they're the actual origin of most live
    #    activity on this map (see the frontend's applyEvent(), which routes
    #    cap.call events whose group matches one of these onto the matching
    #    node). Included purely as animation endpoints so "where did this
    #    request come from" has somewhere real to point at instead of
    #    everything flying out of the Ollama category regardless of origin. ──
    for sid, label in (("svc:dream", "Dream"), ("svc:chat", "Chat"), ("svc:dag", "DAG / Loops")):
        nodes_out.append({"id": sid, "label": label, "kind": "service", "status": "ok", "detail": ""})
        edges_out.append({"from": "hub", "to": sid})

    # ── User / Perf / Error Monitor: three more fixed animation endpoints,
    #    same "code path, not a machine, always up if Vera is up" treatment
    #    as Dream/Chat/DAG above.
    #      - User is the SOURCE every human-initiated touchpoint (chat, IDE,
    #        operator, accounts, calendar, email — see the frontend's
    #        USER_TOUCHPOINT_GROUPS) routes through first, so those events
    #        have somewhere real to originate from on the map instead of
    #        appearing to spawn out of the subsystem they happened to land in.
    #      - Perf reflects the SAME watchdog stall/hang feed
    #        <vera-error-radar> already polls (perf.stalls) — real "warn" if
    #        the last few captured events include a genuine stall/hang,
    #        never a fabricated always-green light.
    #      - Error Monitor is a SINK: every cap.error / ollama.request_error,
    #        regardless of which group raised it, gets an extra chip flown
    #        here (frontend), so failures visibly flow through the system to
    #        one place instead of only ever recolouring their origin node. ──
    perf = snap.get("perf") if isinstance(snap.get("perf"), dict) else {}
    recent_stalls = [e for e in (perf.get("events") or []) if e.get("kind") in ("stall", "hang")]
    for sid, label, status, detail in (
        ("user", "User", "ok", "chat / IDE / operator / API entry points"),
        ("svc:perf", "Perf",
         "warn" if recent_stalls else "ok",
         (f"{len(recent_stalls)} recent stall(s)" if recent_stalls else "no stalls")),
        ("svc:errors", "Error Monitor", "ok", "cap.error / ollama errors flow here"),
    ):
        # kind="monitor" (not "service") — the frontend gives these three a
        # dashed ring + a reserved, always-findable wedge at the top of the
        # hub ring instead of competing for space among a dozen-plus
        # subsystem categories, which is what made them hard to spot before.
        nodes_out.append({"id": sid, "label": label, "kind": "monitor", "status": status, "detail": detail})
        edges_out.append({"from": "hub", "to": sid})

    return {"nodes": nodes_out, "edges": edges_out, "ts": now_iso()}


@capability("ollama.instances", memory="off", silent=True,
            http_method="GET", http_path="/ollama/instances", http_tags=["ollama"],
            description="Live status of all Ollama cluster nodes.")
async def cap_ollama_instances(trace_id=None):
    return {iid:{"url":i["url"],"label":i["label"],"has_gpu":i["has_gpu"],
                 "enabled":i.get("enabled", True),
                 "status":i["status"],"latency_ms":i["latency_ms"],"models":i["models"],
                 "in_use":i["in_use"],"errors":i["errors"],"last_check":i["last_check"],
                 "num_ctx":i.get("num_ctx", 4096)}
            for iid,i in OLLAMA_INSTANCES.items()}

@capability("ollama.add_instance", memory="off",
            http_method="POST", http_path="/ollama/instances/add", http_tags=["ollama"],
            description="Dynamically add an Ollama instance to the cluster.")
async def cap_add_instance(id: str, url: str, has_gpu: bool = False, label: str = "", trace_id=None):
    add_ollama_instance(id,url,has_gpu=has_gpu,label=label)
    await _ping_instance(id,OLLAMA_INSTANCES[id])
    await _save_nodes()       # persist so the added node survives a reboot
    return OLLAMA_INSTANCES[id]

# ── Cluster routing: node enable/disable + job-type rules + profiles ─────────

@capability("ollama.node.config", memory="off",
            http_method="POST", http_path="/ollama/node/config", http_tags=["ollama"],
            description="Configure an Ollama node: enable/disable it (disabled nodes are "
                        "skipped by all routing), set priority or label. Persists across "
                        "reboot. Fields: id (str!), enabled (bool), priority (int), label (str).")
async def cap_ollama_node_config(id: str, enabled: Optional[bool] = None,
                                  priority: Optional[int] = None, label: str = "",
                                  trace_id=None):
    inst = OLLAMA_INSTANCES.get(id)
    if not inst:
        return {"error": f"Unknown instance: {id}", "available": list(OLLAMA_INSTANCES.keys())}
    if enabled is not None:
        inst["enabled"] = bool(enabled)
    if priority is not None:
        inst["priority"] = int(priority)
    if label:
        inst["label"] = label
    await _save_nodes()
    await emit_event({"type": "ollama.node.config", "id": id,
                      "enabled": inst.get("enabled", True), "priority": inst.get("priority")})
    return {"ok": True, "id": id, "enabled": inst.get("enabled", True),
            "priority": inst.get("priority"), "label": inst.get("label")}


@capability("ollama.interactive.get", memory="off", silent=True,
            http_method="GET", http_path="/ollama/interactive", http_tags=["ollama"],
            description="Get the interactive-priority config: whether BACKGROUND LLM "
                        "work (dream cycles, V8 programs, fabric NLP) is demoted off "
                        "the GPU while a human is active, the activity window, and "
                        "whether new background runs are deferred entirely. Output: "
                        "{enabled, window_s, defer_background, human_active, "
                        "seconds_since_interactive}.")
async def cap_ollama_interactive_get(trace_id=None):
    since = (time.time() - LAST_INTERACTIVE_TS) if LAST_INTERACTIVE_TS else None
    return {**INTERACTIVE_PRIORITY,
            "human_active": interactive_recent(),
            "background_on_cpu": bool(INTERACTIVE_PRIORITY.get("background_always_cpu")
                                      or (INTERACTIVE_PRIORITY.get("enabled", True)
                                          and interactive_recent())),
            "seconds_since_interactive": round(since, 1) if since is not None else None}


@capability("ollama.interactive.set", memory="off",
            http_method="POST", http_path="/ollama/interactive/set", http_tags=["ollama"],
            description="Set the interactive-priority config (persisted). Fields: "
                        "enabled (bool — demote background LLM work off GPUs while a "
                        "human is active), window_s (int seconds the human counts as "
                        "active after their last interaction), defer_background (bool — "
                        "also hold off STARTING new background runs — dream scheduler "
                        "fires / V8 program ticks — in that window), background_always_cpu "
                        "(bool — keep ALL background LLM work off the GPU at all times, "
                        "not just while a human is active). Output: {ok, config}.")
async def cap_ollama_interactive_set(enabled: Optional[bool] = None,
                                     window_s: Optional[int] = None,
                                     defer_background: Optional[bool] = None,
                                     background_always_cpu: Optional[bool] = None,
                                     trace_id=None):
    if enabled is not None:
        INTERACTIVE_PRIORITY["enabled"] = bool(enabled)
    if window_s is not None:
        try:
            INTERACTIVE_PRIORITY["window_s"] = max(10, int(window_s))
        except Exception:
            return {"ok": False, "error": "window_s must be an integer"}
    if defer_background is not None:
        INTERACTIVE_PRIORITY["defer_background"] = bool(defer_background)
    if background_always_cpu is not None:
        INTERACTIVE_PRIORITY["background_always_cpu"] = bool(background_always_cpu)
    if REDIS:
        try:
            await REDIS.set(KEY_OLLAMA_INTERACTIVE, json.dumps(INTERACTIVE_PRIORITY))
        except Exception as e:
            log.debug("save interactive priority: %s", e)
    await emit_event({"type": "ollama.interactive.config", **INTERACTIVE_PRIORITY})
    return {"ok": True, "config": dict(INTERACTIVE_PRIORITY)}


@capability("activity.ping", memory="off", silent=True,
            http_method="POST", http_path="/activity/ping", http_tags=["obs"],
            description="Mark the human as ACTIVE now (UIs call this on real user "
                        "interactions — sending a chat message, clicking run). Feeds "
                        "the interactive-priority GPU backoff for background work. "
                        "Input: source (str, optional). Output: {ok}.")
async def cap_activity_ping(source: str = "", trace_id=None):
    note_interactive(source)
    return {"ok": True, "window_s": INTERACTIVE_PRIORITY.get("window_s", 180)}


def _profile_rules_full(prof: dict) -> Dict[str, dict]:
    """A profile's rules merged over the built-in defaults (for display)."""
    merged = {jt: dict(r) for jt, r in DEFAULT_ROUTING_RULES.items()}
    for jt, r in (prof.get("rules") or {}).items():
        if r:
            merged[jt] = r
    return merged

@capability("ollama.routing.get", memory="off", silent=True,
            http_method="GET", http_path="/ollama/routing", http_tags=["ollama"],
            description="Get cluster routing: active profile, all profiles, the built-in "
                        "DEFAULT rules (always shown as the baseline), the job-type list, "
                        "and current node config. Output: {active_profile, profiles, "
                        "defaults, job_types, nodes}.")
async def cap_ollama_routing_get(trace_id=None):
    active = ROUTING.get("active_profile", "default")
    profiles_out = {}
    for name, prof in ROUTING.get("profiles", {}).items():
        profiles_out[name] = {"label": prof.get("label", name),
                              "rules": prof.get("rules", {}),
                              "effective": _profile_rules_full(prof)}
    return {
        "active_profile": active,
        "profiles":       profiles_out,
        "defaults":       DEFAULT_ROUTING_RULES,
        "job_types":      OLLAMA_JOB_TYPES,
        "nodes": {iid: {"label": i.get("label", iid), "has_gpu": i.get("has_gpu", False),
                        "enabled": i.get("enabled", True), "priority": i.get("priority", 0),
                        "status": i.get("status", ""), "models": i.get("models", [])}
                  for iid, i in OLLAMA_INSTANCES.items()},
    }

@capability("ollama.routing.save", memory="off",
            http_method="POST", http_path="/ollama/routing/save", http_tags=["ollama"],
            description="Create or update a routing profile's rules, and optionally set it "
                        "active. A rule overrides the default for one job type. "
                        "Fields: profile (str! — name), label (str), "
                        "rules (dict job_type->{prefer_gpu,deny_gpu,pin,allow:[],deny:[],model}), "
                        "activate (bool). Omitted job types inherit the DEFAULT. Persists.")
async def cap_ollama_routing_save(profile: str, label: str = "",
                                   rules: Optional[Dict[str, Any]] = None,
                                   activate: bool = False, trace_id=None):
    if not profile:
        return {"error": "profile name required"}
    if isinstance(rules, str):
        try:
            rules = json.loads(rules) if rules.strip() else {}
        except Exception:
            return {"error": "rules must be a JSON object"}
    profs = ROUTING.setdefault("profiles", {})
    existing = profs.get(profile, {})
    # Normalise each rule through _rule() so missing keys get sane defaults.
    clean: Dict[str, dict] = {}
    for jt, r in (rules or existing.get("rules", {}) or {}).items():
        if not isinstance(r, dict):
            continue
        clean[jt] = _rule(jt, prefer_gpu=bool(r.get("prefer_gpu")),
                          deny_gpu=bool(r.get("deny_gpu")), pin=r.get("pin", "") or "",
                          allow=r.get("allow") or [], deny=r.get("deny") or [],
                          model=r.get("model", "") or "",
                          avoid_embed=bool(r.get("avoid_embed")))
    profs[profile] = {"label": label or existing.get("label", profile), "rules": clean}
    if activate or ROUTING.get("active_profile") not in profs:
        ROUTING["active_profile"] = profile
    await _save_routing()
    await emit_event({"type": "ollama.routing.saved", "profile": profile,
                      "active": ROUTING.get("active_profile")})
    return {"ok": True, "active_profile": ROUTING.get("active_profile"),
            "profile": profile, "rules": clean}

@capability("ollama.profile.activate", memory="off",
            http_method="POST", http_path="/ollama/profile/activate", http_tags=["ollama"],
            description="Switch the active routing profile. Field: profile (str!). Persists.")
async def cap_ollama_profile_activate(profile: str, trace_id=None):
    if profile not in ROUTING.get("profiles", {}):
        return {"error": f"unknown profile: {profile}",
                "available": list(ROUTING.get("profiles", {}).keys())}
    ROUTING["active_profile"] = profile
    await _save_routing()
    await emit_event({"type": "ollama.routing.activated", "profile": profile})
    return {"ok": True, "active_profile": profile}

@capability("ollama.profile.delete", memory="off",
            http_method="POST", http_path="/ollama/profile/delete", http_tags=["ollama"],
            description="Delete a routing profile (the 'default' profile cannot be deleted). "
                        "Field: profile (str!). Persists.")
async def cap_ollama_profile_delete(profile: str, trace_id=None):
    if profile == "default":
        return {"error": "the default profile cannot be deleted"}
    profs = ROUTING.get("profiles", {})
    if profile not in profs:
        return {"error": f"unknown profile: {profile}"}
    profs.pop(profile, None)
    if ROUTING.get("active_profile") == profile:
        ROUTING["active_profile"] = "default"
    await _save_routing()
    await emit_event({"type": "ollama.routing.deleted", "profile": profile})
    return {"ok": True, "active_profile": ROUTING.get("active_profile")}

# ── Model purpose tags — user-maintained {model: [purposes]} ─────────────────
# Lets the operator tag models by what they're good at (planning, code, chat,
# summarise, fast, big-context, vision, …). Shown in the routing editors so
# picking a model for a job type / cap rule is informed; also consumable by
# anything that wants "a model tagged X".
KEY_OLLAMA_MODEL_TAGS = "vera:ollama:model_tags"
MODEL_TAGS: Dict[str, List[str]] = {}
_MODEL_TAGS_LOADED = {"v": False}


async def _model_tags_hydrate() -> None:
    if _MODEL_TAGS_LOADED["v"]:
        return
    _MODEL_TAGS_LOADED["v"] = True
    if not REDIS:
        return
    try:
        raw = await REDIS.get(KEY_OLLAMA_MODEL_TAGS)
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                MODEL_TAGS.update({str(k): [str(t) for t in v][:12]
                                   for k, v in doc.items() if isinstance(v, list)})
    except Exception as e:
        log.debug("load model tags: %s", e)


@capability("ollama.model_tags.get", memory="off", silent=True,
            http_method="GET", http_path="/ollama/model_tags", http_tags=["ollama"],
            description="User-maintained model PURPOSE tags {model: [tags]} — e.g. "
                        "planning, code, chat, summarise, fast, big-context, vision. "
                        "Query: tag (str — filter to models carrying that tag).")
async def cap_ollama_model_tags_get(tag: str = "", trace_id=None):
    await _model_tags_hydrate()
    tags = dict(MODEL_TAGS)
    if tag:
        tags = {m: ts for m, ts in tags.items() if tag in ts}
    return {"tags": tags}


@capability("ollama.model_tags.set", memory="off",
            http_method="POST", http_path="/ollama/model_tags/set", http_tags=["ollama"],
            description="Set the purpose tags for one model (replaces its list; empty "
                        "clears). Inputs: model (str!), tags (csv or JSON list). Persists.")
async def cap_ollama_model_tags_set(model: str, tags: Any = "", trace_id=None):
    await _model_tags_hydrate()
    if not (model or "").strip():
        return {"error": "model required"}
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags) if tags.strip().startswith("[") else None
        except Exception:
            parsed = None
        tag_list = ([str(t) for t in parsed] if isinstance(parsed, list)
                    else [t.strip() for t in tags.split(",") if t.strip()])
    elif isinstance(tags, list):
        tag_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tag_list = []
    model = model.strip()
    if tag_list:
        MODEL_TAGS[model] = tag_list[:12]
    else:
        MODEL_TAGS.pop(model, None)
    if REDIS:
        try:
            await REDIS.set(KEY_OLLAMA_MODEL_TAGS, json.dumps(MODEL_TAGS))
        except Exception as e:
            log.warning("save model tags: %s", e)
    await emit_event({"type": "ollama.model_tags.saved", "model": model, "tags": tag_list})
    return {"ok": True, "model": model, "tags": tag_list}


@capability("ollama.cap_routing.get", memory="off", silent=True,
            http_method="GET", http_path="/ollama/cap_routing", http_tags=["ollama"],
            description="Per-capability/group LLM routing rules. Output: {user, declared, "
                        "job_types, nodes}. USER rules (editable) override DECLARED rules "
                        "(registered in code by subsystems, e.g. the researcher's roles).")
async def cap_ollama_cap_routing_get(trace_id=None):
    return {
        "user":      CAP_ROUTING_USER,
        "declared":  CAP_ROUTING_DECLARED,
        "job_types": OLLAMA_JOB_TYPES,
        "nodes":     {iid: {"label": i.get("label", iid), "has_gpu": i.get("has_gpu", False),
                            "enabled": i.get("enabled", True), "status": i.get("status", "")}
                      for iid, i in OLLAMA_INSTANCES.items()},
    }


@capability("ollama.cap_routing.save", memory="off",
            http_method="POST", http_path="/ollama/cap_routing/save", http_tags=["ollama"],
            description="Create/update a per-capability routing rule. Fields: pattern "
                        "(str! — cap name or 'prefix.*' glob), job_type (str), label (str), "
                        "prefer_gpu/deny_gpu (bool), pin (str — instance id), allow/deny "
                        "(list[str] — instance globs), model (str), escalate_chars (int — "
                        "prompt-length threshold), escalate (dict — overrides applied above "
                        "the threshold, e.g. {\"prefer_gpu\": true, \"model\": \"...\"}). "
                        "Persists; overrides any code-declared rule for the same pattern.")
async def cap_ollama_cap_routing_save(pattern: str, job_type: str = "", label: str = "",
                                      prefer_gpu: bool = False, deny_gpu: bool = False,
                                      pin: str = "", allow: Optional[List[str]] = None,
                                      deny: Optional[List[str]] = None, model: str = "",
                                      escalate_chars: int = 0,
                                      escalate: Optional[dict] = None, trace_id=None):
    if not (pattern or "").strip():
        return {"error": "pattern required"}
    if isinstance(escalate, str):
        try:
            escalate = json.loads(escalate) if escalate.strip() else {}
        except Exception:
            return {"error": "escalate must be a JSON object"}
    rule = _cap_rule(pattern.strip(), job_type=job_type, label=label,
                     prefer_gpu=prefer_gpu, deny_gpu=deny_gpu, pin=pin,
                     allow=allow, deny=deny, model=model,
                     escalate_chars=escalate_chars, escalate=escalate)
    CAP_ROUTING_USER[pattern.strip()] = rule
    await _save_cap_routing()
    await emit_event({"type": "ollama.cap_routing.saved", "pattern": pattern.strip()})
    return {"ok": True, "rule": rule}


@capability("ollama.cap_routing.delete", memory="off",
            http_method="POST", http_path="/ollama/cap_routing/delete", http_tags=["ollama"],
            description="Delete a per-capability routing rule (USER layer only — a "
                        "code-declared rule for the pattern, if any, becomes active again). "
                        "Field: pattern (str!).")
async def cap_ollama_cap_routing_delete(pattern: str, trace_id=None):
    if pattern not in CAP_ROUTING_USER:
        return {"error": f"no user rule for pattern: {pattern}",
                "declared": pattern in CAP_ROUTING_DECLARED}
    CAP_ROUTING_USER.pop(pattern, None)
    await _save_cap_routing()
    await emit_event({"type": "ollama.cap_routing.deleted", "pattern": pattern})
    return {"ok": True}


# ── Role-profile routing (research / ide / …) ────────────────────────────────

@capability("ollama.role_profiles.get", memory="off", silent=True,
            http_method="GET", http_path="/ollama/role_profiles", http_tags=["ollama"],
            description="Role-based routing profiles (research/ide/…): each maps named "
                        "roles (thinker/writer/verifier/…) to routing rules. Output: "
                        "{declared, user, effective, job_types, nodes}. USER roles "
                        "(editable) override DECLARED ones per role.")
async def cap_ollama_role_profiles_get(trace_id=None):
    return {
        "declared":  ROLE_PROFILES_DECLARED,
        "user":      ROLE_PROFILES_USER,
        "effective": _effective_role_profiles(),
        "job_types": OLLAMA_JOB_TYPES,
        "nodes": {iid: {"label": i.get("label", iid), "has_gpu": i.get("has_gpu", False),
                        "enabled": i.get("enabled", True), "status": i.get("status", ""),
                        "priority": i.get("priority", 0), "in_use": i.get("in_use", 0),
                        "models": i.get("models", [])}
                  for iid, i in OLLAMA_INSTANCES.items()},
    }


@capability("ollama.role_profiles.save", memory="off",
            http_method="POST", http_path="/ollama/role_profiles/save", http_tags=["ollama"],
            description="Create or override a role-routing profile (USER layer — wins over "
                        "the code-declared profile per role). Fields: profile (str!), "
                        "label (str), roles (dict role -> {job_type, prefer_gpu, deny_gpu, "
                        "pin, allow, deny, model, escalate_chars, escalate}). Persists.")
async def cap_ollama_role_profiles_save(profile: str, label: str = "",
                                        roles: Optional[Dict[str, Any]] = None,
                                        trace_id=None):
    name = (profile or "").strip()
    if not name:
        return {"error": "profile name required"}
    if isinstance(roles, str):
        try:
            roles = json.loads(roles) if roles.strip() else {}
        except Exception:
            return {"error": "roles must be a JSON object"}
    existing = ROLE_PROFILES_USER.get(name) or {}
    declared = ROLE_PROFILES_DECLARED.get(name) or {}
    clean = {r: _role_rule(name, r, v)
             for r, v in (roles or {}).items() if isinstance(v, dict)}
    ROLE_PROFILES_USER[name] = {
        "label": label or existing.get("label") or declared.get("label", name),
        "owner": existing.get("owner") or declared.get("owner", "user"),
        "roles": clean or (existing.get("roles") or {}),
    }
    await _save_role_profiles()
    await emit_event({"type": "ollama.role_profiles.saved", "profile": name})
    return {"ok": True, "profile": name,
            "effective": _effective_role_profiles().get(name)}


@capability("ollama.role_profiles.delete", memory="off",
            http_method="POST", http_path="/ollama/role_profiles/delete", http_tags=["ollama"],
            description="Delete a role-profile USER override — the whole profile, or a "
                        "single role via the optional `role` field. The code-declared "
                        "profile, if any, becomes active again. Fields: profile (str!), "
                        "role (str).")
async def cap_ollama_role_profiles_delete(profile: str, role: str = "", trace_id=None):
    prof = ROLE_PROFILES_USER.get(profile)
    if not prof:
        return {"error": f"no user override for profile: {profile}",
                "declared": profile in ROLE_PROFILES_DECLARED}
    if role:
        (prof.get("roles") or {}).pop(role, None)
        if not prof.get("roles"):
            ROLE_PROFILES_USER.pop(profile, None)
    else:
        ROLE_PROFILES_USER.pop(profile, None)
    await _save_role_profiles()
    await emit_event({"type": "ollama.role_profiles.deleted",
                      "profile": profile, "role": role})
    return {"ok": True}


@capability("llm.route.resolve", memory="off", silent=True,
            http_method="GET", http_path="/llm/route/resolve", http_tags=["ollama"],
            description="Preview/resolve a routing decision WITHOUT generating: which node "
                        "would serve a request and why. Query: profile+role (resolve a "
                        "role-profile role), or service (stt|tts|imagegen — resolve a media "
                        "node), or job_type, or cap_name; plus model (str) and "
                        "prompt_chars (int — triggers length escalation). Output: "
                        "{instance_id, url, label, model, job_type, rule, reason}.")
async def cap_llm_route_resolve(profile: str = "", role: str = "", job_type: str = "",
                                cap_name: str = "", model: str = "", service: str = "",
                                prompt_chars: int = 0, trace_id=None):
    chars = int(prompt_chars or 0)
    if service:
        res = resolve_media(service)
        return res or {"error": "no routable media node", "service": service,
                       "fallback": _MEDIA_FALLBACK_URL}
    if profile and role:
        res = resolve_role(profile, role, model=model, prompt_chars=chars)
        return res or {"error": "no routable node", "profile": profile, "role": role}
    rule = _resolve_cap_routing(cap_name) if cap_name else None
    jt = (job_type or (rule or {}).get("job_type") or "default")
    eff = _merge_rule_over_base(rule, _resolve_rule(jt) or {}, chars)
    exp: dict = {}
    chosen = pick_instance(model=(model or eff.get("model") or None), job_type=jt,
                           rule_override=eff, explain=exp)
    if not chosen:
        return {"error": "no routable node", "job_type": jt, "rule": eff,
                "reason": exp.get("reason") or []}
    inst = OLLAMA_INSTANCES.get(chosen, {})
    return {"instance_id": chosen, "url": inst.get("url", ""),
            "label": inst.get("label", chosen), "has_gpu": bool(inst.get("has_gpu")),
            "model": model or eff.get("model") or "", "job_type": jt,
            "rule_source": (f"cap:{rule.get('pattern', '')}" if rule
                            else f"profile:{jt}"),
            "rule": eff, "reason": exp.get("reason") or []}


# ── Media node routing caps (STT / TTS / image-gen servers) ──────────────────

@capability("media.nodes", memory="off", silent=True,
            http_method="GET", http_path="/media/nodes", http_tags=["media", "ollama"],
            description="Media (GPU-inference) nodes and the services each one serves "
                        "(stt / tts / imagegen, detected via /health). Output: {nodes, "
                        "services, fallback_url}. Candidates are seeded on every cluster "
                        "host — install the inference server there and the node goes "
                        "online on the next heartbeat.")
async def cap_media_nodes(trace_id=None):
    return {"nodes": MEDIA_INSTANCES, "services": list(MEDIA_SERVICES),
            "fallback_url": _MEDIA_FALLBACK_URL}


@capability("media.node.add", memory="off",
            http_method="POST", http_path="/media/node/add", http_tags=["media"],
            description="Register a media (GPU-inference) node. Fields: id (str!), "
                        "url (str! — e.g. http://host:8765), label (str), has_gpu (bool), "
                        "priority (int). Persists; health-probed on the next heartbeat.")
async def cap_media_node_add(id: str, url: str, label: str = "",
                             has_gpu: bool = False, priority: int = 0, trace_id=None):
    iid = (id or "").strip()
    if not iid or not (url or "").strip():
        return {"error": "id and url required"}
    inst = add_media_instance(iid, url.strip(), label=label, has_gpu=has_gpu,
                              priority=int(priority or 0))
    await _ping_media_instance(iid, inst)
    await _save_media_nodes()
    await emit_event({"type": "media.node.added", "id": iid, "url": url})
    return {"ok": True, "id": iid, "node": inst}


@capability("media.node.remove", memory="off",
            http_method="POST", http_path="/media/node/remove", http_tags=["media"],
            description="Remove a media node from routing. Field: id (str!). Seeded "
                        "candidates reappear (disabled state persists via config instead).")
async def cap_media_node_remove(id: str, trace_id=None):
    if id not in MEDIA_INSTANCES:
        return {"error": f"unknown media node: {id}"}
    MEDIA_INSTANCES.pop(id, None)
    await _save_media_nodes()
    await emit_event({"type": "media.node.removed", "id": id})
    return {"ok": True}


@capability("media.node.config", memory="off",
            http_method="POST", http_path="/media/node/config", http_tags=["media"],
            description="Update a media node's routing config: enabled (bool — false "
                        "removes it from routing), priority (int), label (str). "
                        "Field: id (str!). Persists.")
async def cap_media_node_config(id: str, enabled: Optional[bool] = None,
                                priority: Optional[int] = None, label: str = "",
                                trace_id=None):
    inst = MEDIA_INSTANCES.get(id)
    if not inst:
        return {"error": f"unknown media node: {id}"}
    if enabled is not None:
        inst["enabled"] = bool(enabled)
    if priority is not None:
        inst["priority"] = int(priority)
    if label:
        inst["label"] = label
    await _save_media_nodes()
    await emit_event({"type": "media.node.config", "id": id})
    return {"ok": True, "node": inst}


@capability("media.ping", memory="off",
            http_method="POST", http_path="/media/ping", http_tags=["media"],
            description="Ping a media node now and refresh its status + service list. "
                        "Field: id (str!).")
async def cap_media_ping(id: str, trace_id=None):
    inst = MEDIA_INSTANCES.get(id)
    if not inst:
        return {"error": f"unknown media node: {id}"}
    await _ping_media_instance(id, inst)
    return inst


@capability("ollama.route_stats", memory="off", silent=True,
            http_method="GET", http_path="/ollama/route_stats", http_tags=["ollama"],
            description="Rolling per-(model,instance,job_type) request statistics: count, "
                        "EMA elapsed seconds, output tokens, tokens/sec and prompt size — "
                        "the data the router uses to gauge how long a request will take. "
                        "Query: model (str, filter), instance (str, filter), "
                        "estimate_prompt_chars (int — include a per-key duration estimate).")
async def cap_ollama_route_stats(model: str = "", instance: str = "",
                                 estimate_prompt_chars: int = 0, trace_id=None):
    out = []
    for k, s in _ROUTE_STATS.items():
        if model and s.get("model") != model:
            continue
        if instance and s.get("instance") != instance:
            continue
        row = dict(s)
        if estimate_prompt_chars > 0:
            row["est_seconds"] = estimate_request_seconds(
                s.get("model", ""), s.get("instance", ""), s.get("job_type", ""),
                int(estimate_prompt_chars))
        out.append(row)
    out.sort(key=lambda r: (-int(r.get("n") or 0)))
    return {"stats": out, "keys": len(_ROUTE_STATS)}


@capability("ollama.ping_instance", memory="off",
            http_method="POST", http_path="/ollama/ping", http_tags=["ollama"],
            description="Ping a specific Ollama instance and update its status.")
async def cap_ping_instance(instance_id: str, trace_id=None):
    inst=OLLAMA_INSTANCES.get(instance_id)
    if not inst: return {"error":f"Unknown instance: {instance_id}"}
    await _ping_instance(instance_id,inst); return inst

@capability("ollama.pull", memory="off",
            http_method="POST", http_path="/ollama/pull", http_tags=["ollama"],
            description="Pull a model onto a specific Ollama instance.")
async def cap_ollama_pull(model: str, instance_id: str, trace_id=None):
    inst=OLLAMA_INSTANCES.get(instance_id)
    if not inst: return {"error":f"Unknown instance: {instance_id}"}
    try:
        async with httpx.AsyncClient(verify=_SSL_CTX, timeout=600) as c:
            r=await c.post(f"{inst['url']}/api/pull",json={"name":model,"stream":False})
            r.raise_for_status(); return {"model":model,"instance":instance_id,"status":"pulled",**r.json()}
    except Exception as e: return {"model":model,"instance":instance_id,"error":str(e)}

@capability("ollama.request_log", memory="off",
            http_method="GET", http_path="/ollama/request_log", http_tags=["ollama"],
            description="Return the recent Ollama request log (in-process ring buffer). "
                        "Shows caller, model, instance, timing, and status for every "
                        "ollama_generate call. Query: limit (int, default 50), "
                        "caller_file (str, filter), status (str, filter).")
async def cap_ollama_request_log(limit: int = 50, caller_file: str = "",
                                  status: str = "", trace_id=None):
    entries = list(reversed(_OLLAMA_REQUEST_LOG))  # newest first
    if caller_file:
        entries = [e for e in entries if caller_file in e.get("caller_file", "")]
    if status:
        entries = [e for e in entries if e.get("status", "") == status]
    return {"entries": entries[:limit], "total": len(_OLLAMA_REQUEST_LOG)}


# ── Embedding configuration (runtime-adjustable) ────────────────────────────

_EMBED_PREFER_GPU: bool = False   # default: route via normal pick_instance
_EMBED_INSTANCE_ID: Optional[str] = None  # pin to specific instance, or None

@capability("ollama.embed_config", memory="off",
            http_method="GET", http_path="/ollama/embed_config",
            http_tags=["ollama"],
            description="Return current embedding configuration: model, URL, "
                        "preferred instance, GPU preference.")
async def cap_ollama_embed_config(trace_id=None):
    return {
        "embed_model":      OLLAMA_EMBED_MODEL,
        "embed_url":        OLLAMA_EMBED_URL,
        "prefer_gpu":       _EMBED_PREFER_GPU,
        "pinned_instance":  _EMBED_INSTANCE_ID,
        "instances":        {iid: {"label": i.get("label",""), "has_gpu": i.get("has_gpu", False),
                                   "status": i.get("status",""), "models": i.get("models",[])}
                             for iid, i in OLLAMA_INSTANCES.items()},
    }

@capability("ollama.embed_config_set", memory="off",
            http_method="POST", http_path="/ollama/embed_config",
            http_tags=["ollama"],
            description="Update embedding configuration at runtime. "
                        "Fields: embed_model (str), prefer_gpu (bool), "
                        "pinned_instance (str or empty to clear).")
async def cap_ollama_embed_config_set(
    embed_model: str = "",
    prefer_gpu: Optional[bool] = None,
    pinned_instance: str = "",
    trace_id=None,
):
    global OLLAMA_EMBED_MODEL, OLLAMA_EMBED_URL
    global _EMBED_PREFER_GPU, _EMBED_INSTANCE_ID
    changes = {}
    if embed_model:
        OLLAMA_EMBED_MODEL = embed_model
        changes["embed_model"] = embed_model
    if prefer_gpu is not None:
        _EMBED_PREFER_GPU = prefer_gpu
        changes["prefer_gpu"] = prefer_gpu
    if pinned_instance == "__clear__":
        _EMBED_INSTANCE_ID = None
        changes["pinned_instance"] = None
    elif pinned_instance:
        if pinned_instance in OLLAMA_INSTANCES:
            _EMBED_INSTANCE_ID = pinned_instance
            changes["pinned_instance"] = pinned_instance
        else:
            return {"error": f"Unknown instance: {pinned_instance}",
                    "available": list(OLLAMA_INSTANCES.keys())}
    # Persist so the embedding routing survives a reboot.
    if REDIS:
        try:
            await REDIS.set(KEY_OLLAMA_EMBED, json.dumps({
                "embed_model": OLLAMA_EMBED_MODEL,
                "prefer_gpu": _EMBED_PREFER_GPU,
                "pinned_instance": _EMBED_INSTANCE_ID,
            }))
        except Exception as e:
            log.warning("save embed config: %s", e)
    await emit_event({"type": "ollama.embed_config_changed", **changes})
    return {
        "embed_model":      OLLAMA_EMBED_MODEL,
        "embed_url":        OLLAMA_EMBED_URL,
        "prefer_gpu":       _EMBED_PREFER_GPU,
        "pinned_instance":  _EMBED_INSTANCE_ID,
        "changes":          changes,
    }


# ── DAG ───────────────────────────────────────────────────────────────────────

@capability("dag.run", memory="on",
            http_method="POST", http_path="/dag/run", http_tags=["dag"],
            description="Execute a DAG against an initial state. Set supervised=true for LLM checkpoints.")
async def cap_dag_run(dag: list = None, state: dict = None, supervised: bool = False, trace_id=None):
    fn=supervised_run_graph if supervised else run_graph
    result=await fn(dag or [],state or {})
    return {"trace_id":trace_id or new_id(),"result":result}

@capability("dag.plan", memory="on",
            http_method="POST", http_path="/dag/plan", http_tags=["dag"],
            description="Ask the LLM to produce a DAG execution plan for a natural-language goal.")
async def cap_dag_plan(goal: str, capabilities: list = None, trace_id=None):
    return await plan_dag(goal,capabilities)

@capability("dag.plan_and_run", memory="on",
            http_method="POST", http_path="/dag/plan_and_run", http_tags=["dag"],
            description="Plan a DAG from a goal then immediately execute it.")
async def cap_dag_plan_and_run(goal: str, supervised: bool = True, trace_id=None):
    plan=await plan_dag(goal)
    if plan.get("error") and not plan.get("dag"): return {"error":plan["error"]}
    fn=supervised_run_graph if supervised else run_graph
    result=await fn(plan.get("dag",[]),plan.get("initial_state",{}))
    return {"plan":plan,"result":result,"supervised":supervised}

@capability("cluster.instance_update", memory="off",
            http_method="POST", http_path="/cluster/instance/update",
            http_tags=["cluster"],
            description="Update mutable fields on an Ollama instance (num_ctx, label, etc).")
async def cluster_instance_update(id: str, num_ctx: int = 0, label: str = "",
                                   trace_id=None):
    inst = OLLAMA_INSTANCES.get(id)
    if not inst:
        return {"error": f"Instance not found: {id}"}
    if num_ctx > 0:
        inst["num_ctx"] = num_ctx
    if label:
        inst["label"] = label
    await emit_event({"type": "cluster.instance_updated", "id": id, "num_ctx": num_ctx})
    return {"id": id, "num_ctx": inst.get("num_ctx", 0), "label": inst.get("label", id)}


# ── LLM ───────────────────────────────────────────────────────────────────────

@capability("llm.route", memory="off",
            http_method="POST", http_path="/llm/route", http_tags=["llm"],
            description="Route a prompt through the best available LLM capability with fallback chain.")
async def cap_llm_route(prompt: str, prefer: str = "", trace_id=None):
    return await route_llm(prompt,prefer=prefer or None)

@capability("ollama.model_ctx", memory="off",
            http_method="GET", http_path="/ollama/model_ctx", http_tags=["ollama"],
            description="Detect a model's true max context window from Ollama "
                        "(/api/show → model_info.<arch>.context_length). Returns the "
                        "detected context_length plus the effective num_ctx after "
                        "applying the optional manual cap / OLLAMA_MAX_AUTO_CTX.")
async def cap_ollama_model_ctx(model: str, instance_id: str = "", prefer_gpu: bool = False,
                                manual: int = 0, trace_id=None):
    detected  = await ollama_model_ctx(model, instance_id or None, prefer_gpu)
    effective = await effective_num_ctx(model, instance_id or None, prefer_gpu, manual)
    return {
        "model":          model,
        "instance":       pick_instance(prefer_gpu=prefer_gpu,
                                        instance_id=instance_id or None, model=model),
        "context_length": detected,
        "effective":      effective,
        "max_auto_ctx":   OLLAMA_MAX_AUTO_CTX or None,
    }

# ── Minimal built-ins (full LLM group lives in vera_capabilities.py) ──────────

@capability("echo", http_method="POST", http_path="/debug/echo", http_tags=["debug"], memory="off", description="Echo a message back with timestamp.")
async def _echo(message: str, trace_id=None):
    return {"echo":message,"ts":now_iso(),"trace_id":trace_id}

@capability("health.check", http_method="GET", http_path="/debug/health", http_tags=["debug"], memory="off", silent=True, description="Quick health check — alias of obs.health.")
async def _health(trace_id=None):
    return await obs_health(trace_id=trace_id)

# ui.panels lives here (not in vera_capabilities) so /ui/panels is ALWAYS available
# even when vera_capabilities.py hasn't loaded yet.
@capability("ui.panels", memory="off", silent=True,
            http_method="GET", http_path="/ui/panels", http_tags=["ui"],
            description="List all registered built-in UI panels injected by capability modules.")
async def _ui_panels(trace_id=None):
    return list(UI_PANELS.values())

@capability("ui.panel.specialist", memory="off", silent=True,
            http_method="GET", http_path="/ui/panel/specialist", http_tags=["ui"],
            description="The declarative specialist binding for one panel, if "
                        "any (see register_ui's specialist_agent/"
                        "specialist_loop_profile — the generalized form of the "
                        "markets studio's hardcoded COP widget). Used by "
                        "<vera-panel-copilot> to know which agent/loop-profile "
                        "to embed, and by chat's panel-dispatch defer-to-"
                        "specialist mode. Input: panel_id (str!). Output: "
                        "{panel_id, specialist_agent, specialist_loop_profile, "
                        "bound: bool}.")
async def _ui_panel_specialist(panel_id: str = "", trace_id=None):
    p = UI_PANELS.get(panel_id) or {}
    agent = p.get("specialist_agent", "")
    profile = p.get("specialist_loop_profile", "")
    context_cap = p.get("specialist_context_cap", "")
    return {"panel_id": panel_id, "specialist_agent": agent,
            "specialist_loop_profile": profile, "specialist_context_cap": context_cap,
            "bound": bool(agent or profile)}

@capability("caps.specialist", memory="off", silent=True,
            http_method="GET", http_path="/caps/specialist", http_tags=["ui", "agents"],
            description="The specialist bound to a CAPABILITY (not a panel) — "
                        "derived directly from the panel bindings (register_ui's "
                        "specialist_agent/specialist_loop_profile/"
                        "specialist_context_cap), never a separate registry to "
                        "keep in sync. This is the unification point between "
                        "panel-dispatch (chat/panel_copilot, which already knows "
                        "which panel is open) and a running agentic loop (which "
                        "only knows the capability NAME it's about to call, no "
                        "panel context at all) — both resolve to the same "
                        "specialist through the same underlying binding. Finds "
                        "the panel whose ui_caps contains cap_name exactly, or "
                        "(family=True, default) shares its dot-prefix family "
                        "(e.g. 'mesh.firmware.flash' matches a panel whose "
                        "ui_caps includes any 'mesh.*' capability). Input: "
                        "cap_name (str!), family (bool default True). Output: "
                        "{cap_name, panel_id, specialist_agent, "
                        "specialist_loop_profile, specialist_context_cap, bound}.")
async def _caps_specialist(cap_name: str = "", family: bool = True, trace_id=None):
    empty = {"cap_name": cap_name, "panel_id": "", "specialist_agent": "",
             "specialist_loop_profile": "", "specialist_context_cap": "", "bound": False}
    if not cap_name:
        return empty
    prefix = cap_name.split(".")[0] + "." if family and "." in cap_name else None
    for pid, p in UI_PANELS.items():
        caps = p.get("ui_caps") or []
        if not isinstance(caps, list):
            continue   # a few legacy registrations pass a malformed ui_caps
        agent = p.get("specialist_agent", "")
        profile = p.get("specialist_loop_profile", "")
        if not (agent or profile):
            continue
        if cap_name in caps or (prefix and any(isinstance(c, str) and c.startswith(prefix) for c in caps)):
            return {"cap_name": cap_name, "panel_id": pid,
                    "specialist_agent": agent, "specialist_loop_profile": profile,
                    "specialist_context_cap": p.get("specialist_context_cap", ""),
                    "bound": True}
    return empty

async def _heartbeat():
    await emit_event({"type":"heartbeat","caps":len(CAPABILITY_REGISTRY),"workers":len(WORKER_REGISTRY)})

schedule(_heartbeat,30,"heartbeat")

# ─────────────────────────────────────────────────────────────────────────────
# APP + LIFESPAN
# ─────────────────────────────────────────────────────────────────────────────

# Heartbeat the async watchdog bumps every tick; a separate daemon thread reads
# it to catch a stall WHILE it's happening (the async watchdog only runs AFTER
# the loop frees, by which point the blocker is gone).
_LOOP_HEARTBEAT = [0.0]

# Structured record of loop stalls/hangs so the Perf monitor UI (perf_capabilities)
# can display them without scraping logs. Bounded ring buffer; newest last.
from collections import deque as _deque
PERF_EVENTS: "_deque" = _deque(maxlen=int(os.getenv("VERA_PERF_EVENTS_MAX", "300") or 300))

def record_perf_event(kind: str, **fields) -> None:
    """Append a performance event (loop stall / hang / note). Best-effort, never
    raises. `kind`: 'stall' | 'hang' | 'note'. Consumed by perf.stalls cap."""
    try:
        ev = {"kind": kind, "ts": now_iso(), "mono": time.monotonic()}
        ev.update(fields)
        PERF_EVENTS.append(ev)
    except Exception:
        pass
# Stack-dump threshold: a stall lasting at least this long gets the main-thread
# stack dumped (naming the exact blocking call). Must be ≤ what you want to
# diagnose. The dumper thread polls fast enough to catch stalls near this bound.
_LOOP_HANG_DUMP_S = float(os.getenv("VERA_LOOP_HANG_DUMP_S", "1") or 1)

def _start_stall_stack_dumper():
    """Daemon thread that dumps the MAIN thread's stack when the event loop goes
    unresponsive — i.e. catches the exact synchronous call blocking the loop
    (sync SQLite/subprocess/requests/CPU) in the act. The async watchdog can
    only report the DURATION after the fact; this names the offending line."""
    import threading, sys as _sys, traceback as _tb
    main_tid = threading.main_thread().ident
    # Poll well under the threshold so a stall of ~_LOOP_HANG_DUMP_S is caught
    # while it's still blocking (not after it clears).
    poll = max(0.1, min(0.25, _LOOP_HANG_DUMP_S / 3.0))

    def _run():
        last_dumped = 0.0
        while True:
            time.sleep(poll)
            hb = _LOOP_HEARTBEAT[0]
            if hb <= 0:
                continue
            stalled = time.monotonic() - hb
            if stalled >= _LOOP_HANG_DUMP_S and hb != last_dumped:
                last_dumped = hb
                frame = _sys._current_frames().get(main_tid)
                stack = "".join(_tb.format_stack(frame)) if frame else "(no frame)"
                log.error("EVENT LOOP HUNG >%.1fs — main thread is stuck HERE "
                          "(this is the blocking call starving WebSockets):\n%s",
                          stalled, stack)
                # Record for the Perf monitor UI (best-effort). Extract the
                # deepest Vera/app frame as a compact "where" for the table.
                try:
                    _where = ""
                    for _ln in reversed((stack or "").splitlines()):
                        _s = _ln.strip()
                        if _s.startswith("File \"") and "/site-packages/" not in _s:
                            _where = _s.replace("File \"", "").split("\"")[0]
                            _lno = _s.split("line ", 1)[1].split(",")[0] if "line " in _s else ""
                            _where = f"{_where.split('/')[-1]}:{_lno}"
                            break
                    record_perf_event("hang", stalled_ms=round(stalled * 1000),
                                      where=_where, stack=stack[-4000:])
                except Exception:
                    pass

    t = threading.Thread(target=_run, name="loop-stall-dumper", daemon=True)
    t.start()
    log.info("loop-stall stack dumper active (dumps the blocking call after >%.1fs)",
             _LOOP_HANG_DUMP_S)


def _busy_thread_stacks(max_threads: int = 6, tail_frames: int = 6) -> str:
    """Compact stacks of non-idle worker threads. Used when a loop stall had no
    in-the-act 'EVENT LOOP HUNG' dump: a GIL-holding C call in a worker thread
    starves the dumper thread exactly like it starves the loop, so nothing gets
    captured — but right after the loop resumes, that worker is usually still
    inside the call, so sampling all threads NOW often names it."""
    import sys as _sys, traceback as _tb, threading as _th, os.path as _osp
    idle_basenames = {"threading.py", "queue.py", "selectors.py", "socket.py",
                      "thread.py", "connection.py"}
    main_tid = _th.main_thread().ident
    names = {t.ident: t.name for t in _th.enumerate()}
    out = []
    for tid, frame in _sys._current_frames().items():
        name = names.get(tid, str(tid))
        if tid == main_tid or str(name).startswith("loop-stall"):
            continue
        stack = _tb.extract_stack(frame)
        if not stack:
            continue
        if _osp.basename(stack[-1].filename) in idle_basenames:
            continue  # parked in a lock/queue/select wait — not a GIL suspect
        out.append("— thread %s:\n%s" % (
            name, "".join(_tb.format_list(stack[-tail_frames:]))))
        if len(out) >= max_threads:
            break
    return "\n".join(out) or "(all worker threads idle/waiting)"


async def _loop_lag_watchdog(interval: float = 0.5):
    """Detect event-loop stalls — the prime suspect for WS 1005/1006 flapping.

    Sleeps `interval` in a tight loop and measures how much LONGER than that it
    actually took to wake. Extra time == the loop was blocked by synchronous
    work (a cap doing CPU/blocking IO on the loop thread, a giant json.dumps in
    emit_event, a tight non-awaiting loop). While the loop is blocked, uvicorn
    can't service WebSocket frames or ping/pong, and connections drop. This logs
    the stall duration; the companion stack-dumper thread names the exact call
    (for stalls ≥ _LOOP_HANG_DUMP_S — shorter ones warn but can't be captured
    after the fact, so the message says so).
    """
    log.info("loop-lag watchdog active (warns when the event loop stalls)")
    warn_ms = float(os.getenv("VERA_LOOP_LAG_WARN_MS", "500") or 500)
    dump_ms = _LOOP_HANG_DUMP_S * 1000.0
    _LOOP_HEARTBEAT[0] = time.monotonic()
    _start_stall_stack_dumper()
    while True:
        t0 = time.monotonic()
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        _LOOP_HEARTBEAT[0] = time.monotonic()   # loop is alive
        lag_ms = (time.monotonic() - t0 - interval) * 1000.0
        if lag_ms >= warn_ms:
            dumped = False
            _threads_txt = ""
            if lag_ms >= dump_ms:
                # Did the dumper thread actually catch this one in the act?
                # It misses when a worker thread holds the GIL in a long C call
                # (the dumper is starved too) or the stall was many short
                # callbacks rather than one contiguous block.
                try:
                    _now = time.monotonic()
                    dumped = any(ev.get("kind") == "hang" and
                                 (_now - ev.get("mono", 0.0)) < (lag_ms / 1000.0 + 2.0)
                                 for ev in list(PERF_EVENTS)[-6:])
                except Exception:
                    dumped = False
                if dumped:
                    _hint = ("A separate 'EVENT LOOP HUNG' line with the exact "
                             "blocking call's stack should accompany this.")
                else:
                    _threads_txt = _busy_thread_stacks()
                    _hint = ("The stack dumper did NOT catch it in the act — "
                             "typical of a GIL-holding C call in a worker thread "
                             "or a burst of short callbacks. Busy worker-thread "
                             "stacks sampled now (culprit may still be running):\n"
                             + _threads_txt)
            else:
                _hint = (f"Too brief for a stack dump (< {_LOOP_HANG_DUMP_S:.1f}s "
                         "threshold); lower VERA_LOOP_HANG_DUMP_S to capture its "
                         "stack, or ignore — a sub-second blip rarely drops a WS.")
            log.warning("EVENT LOOP STALLED for %.0fms — WS frames/ping were "
                        "blocked this whole time (sync/CPU-bound cap, a large "
                        "emit_event payload, or a non-awaiting loop). %s",
                        lag_ms, _hint)
            try:
                extra = {"threads": _threads_txt[-4000:]} if _threads_txt else {}
                record_perf_event("stall", stalled_ms=round(lag_ms),
                                  dumped=dumped, **extra)
            except Exception:
                pass


_LOG_QLISTENERS: list = []


class _PassthroughQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that keeps record.args intact for the downstream listener.

    The stdlib QueueHandler.prepare() pre-renders the message and nulls
    record.args (so records survive cross-process pickling). That breaks any
    downstream handler with a custom formatter that re-reads args — notably
    uvicorn's AccessFormatter, which does
    `(client, method, path, version, status) = record.args` and blows up with
    'cannot unpack non-iterable NoneType' once args is None. Our QueueListener
    runs in-process on a thread, so there's nothing to pickle: pass a shallow
    copy of the record through with args preserved and let the real handler's
    formatter do its job."""
    def prepare(self, record):
        return copy.copy(record)


def _offload_blocking_log_handlers():
    """Move plain StreamHandlers (root basicConfig stderr + uvicorn's stdout
    access/error loggers) behind QueueHandler→QueueListener pairs. A blocked
    console (paused terminal, SSH backpressure, journald stall) otherwise
    blocks the event loop — a captured 1.5s stall was uvicorn's access logger
    inside stream.write. One queue PER logger so records keep their original
    handler routing (no cross-logger duplication)."""
    if _LOG_QLISTENERS:
        return
    import logging.handlers as _lh  # noqa: F401  (ensures logging.handlers loaded)
    import queue as _q
    for name in (None, "uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        # Exact type only — leave RotatingFileHandler/QueueHandler/etc alone.
        plain = [h for h in lg.handlers if type(h) is logging.StreamHandler]
        if not plain:
            continue
        q = _q.SimpleQueue()
        for h in plain:
            lg.removeHandler(h)
        listener = _lh.QueueListener(q, *plain, respect_handler_level=True)
        listener.start()
        _LOG_QLISTENERS.append(listener)
        lg.addHandler(_PassthroughQueueHandler(q))
    if _LOG_QLISTENERS:
        log.info("logging: console writes moved off-loop for %d logger(s)",
                 len(_LOG_QLISTENERS))


_GC_T0 = [0.0]

def _gc_pause_timer(phase, info):
    """gc callback — times every collection and records slow ones as perf
    events. The hang dumper samples whatever Python frame GC happened to be
    in (often a neo4j __del__ finalizer), which makes GC pauses masquerade
    as unrelated code; this names them explicitly in the stall feed."""
    if phase == "start":
        _GC_T0[0] = time.monotonic()
        return
    dt_ms = (time.monotonic() - _GC_T0[0]) * 1000.0
    if dt_ms >= float(os.getenv("VERA_GC_WARN_MS", "200") or 200):
        record_perf_event("gc", gen=info.get("generation"),
                          collected=info.get("collected"),
                          uncollectable=info.get("uncollectable"),
                          stalled_ms=round(dt_ms))


async def _gc_pacer():
    """Run collections on OUR schedule instead of CPython's. Thresholds are
    raised at startup so automatic gen-2 passes (1-2s loop stalls on this heap —
    the hang traces bottoming out in neo4j workspace __del__ were GC finalizer
    sweeps) essentially never fire mid-request; this task does the equivalent
    work at a paced, observable moment so cyclic garbage (neo4j session/result
    graphs, parsed LLM JSON) can't accumulate unbounded.

    gc.collect() holds the GIL for its whole duration, so a full (gen-2)
    collection freezes the loop for however long it takes to rescan every
    post-startup survivor — a caught pause was 1.78s, and to_thread can't help
    because the collection won't release the GIL. So split the work: the young
    generations (where request-scoped cyclic garbage actually lives and dies)
    are reaped cheaply EVERY cycle, and the expensive full gen-2 sweep runs only
    once per VERA_GC_FULL_EVERY cycles. That keeps gen-2 accumulation bounded
    while cutting how often the multi-hundred-ms freeze can land ~15x."""
    import gc
    interval = float(os.getenv("VERA_GC_PACE_S", "120") or 120)
    full_every = max(1, int(os.getenv("VERA_GC_FULL_EVERY", "15") or 15))
    cycle = 0
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        cycle += 1
        full = (cycle % full_every == 0)
        t0 = time.monotonic()
        try:
            # gc.collect(1) reaps gen-0 + gen-1 without touching the large gen-2
            # survivor set; gc.collect() (== gen-2) is the full, expensive sweep.
            n = gc.collect() if full else gc.collect(1)
        except Exception:
            continue
        dt_ms = (time.monotonic() - t0) * 1000.0
        if dt_ms >= 200:
            log.info("gc pacer: %s collect freed %d objects in %.0fms "
                     "(paced — full sweep every %d cycles)",
                     "full" if full else "young-gen", n, dt_ms, full_every)


async def _openbao_autounseal_boot():
    """If OpenBao KMS auto-unseal is configured, open the vault on startup."""
    await asyncio.sleep(8)          # let redis/backends connect first
    try:
        cap = CAPABILITY_REGISTRY.get("identity.openbao.unseal")
        fn = (cap.get("raw") or cap.get("func")) if cap else None
        if not fn:
            return
        r = await fn(boot=True)
        if r and r.get("ok") and r.get("sealed") is False and r.get("submitted"):
            log.info("openbao auto-unseal: vault unsealed at boot (kms=%s)",
                     r.get("kms_backend"))
    except Exception as e:
        log.debug("openbao auto-unseal boot skipped: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS, PG_POOL, CHROMA, NEO

    # uvicorn has configured its loggers by now — detach console writes from
    # the loop thread before any traffic arrives.
    try:
        _offload_blocking_log_handlers()
    except Exception as _e:
        log.warning("log-handler offload failed: %s", _e)

    # ── DB connections run in background so the server is available immediately ──
    # On reboot, Redis/Postgres may take 10-30s to start. Running them in lifespan
    # before yield blocks ALL HTTP/WS connections until they complete or timeout.
    # Instead: attempt once quickly, then retry in background every 5s.

    # Each backend connects in its OWN coroutine so a slow/unreachable one
    # (e.g. Postgres retrying, or a hung Chroma client) can't starve the others.
    # Previously these ran sequentially in a single coroutine, so if an earlier
    # backend blocked, Neo4j (last) never initialised and /health showed it down
    # even when the Neo4j server was perfectly healthy.

    async def _connect_redis():
        global REDIS
        if not HAS_REDIS:
            log.warning("redis.asyncio not installed — pip install 'redis[asyncio]'")
            return
        if REDIS is not None:
            return
        for _attempt in range(1, 999):
            try:
                _r = aioredis.from_url(
                    REDIS_URL, decode_responses=False,
                    socket_connect_timeout=4, socket_timeout=4,
                )
                await _r.ping()
                info = await _r.info("server")
                REDIS = _r
                log.info("✓ Redis connected (attempt %d): %s v%s",
                         _attempt, REDIS_URL, info.get("redis_version", "?"))
                await emit_event({"type": "backend.connected", "backend": "redis"})
                await _ensure_coord_redis()
                return
            except Exception as e:
                if _attempt == 1:
                    log.error(
                        "✗ Redis not ready yet (will retry every 5s): %s\n"
                        "  URL  : %s\n"
                        "  Hint : check bind address in redis.conf, requirepass, firewall",
                        e, REDIS_URL,
                    )
                await asyncio.sleep(5)

    async def _connect_postgres():
        global PG_POOL
        if not HAS_PG or PG_POOL is not None:
            return
        for _attempt in range(1, 999):
            try:
                PG_POOL = await asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)
                async with PG_POOL.acquire() as conn:
                    await conn.execute(
                        "CREATE TABLE IF NOT EXISTS vera_task_results"
                        "(task_id TEXT PRIMARY KEY,result JSONB,"
                        "ts TIMESTAMPTZ DEFAULT NOW())"
                    )
                log.info("✓ Postgres connected (attempt %d)", _attempt)
                await emit_event({"type": "backend.connected", "backend": "postgres"})
                return
            except Exception as e:
                if _attempt == 1:
                    log.warning("Postgres not ready yet (will retry): %s", e)
                await asyncio.sleep(5)

    async def _connect_chroma():
        global CHROMA
        if not HAS_CHROMA or CHROMA is not None:
            return
        try:
            # Run in a thread — some chromadb client versions do blocking I/O
            # (a heartbeat) on construction, which would stall the event loop.
            CHROMA = await asyncio.to_thread(chromadb.HttpClient, host="localhost", port=8008)
            log.info("✓ ChromaDB")
            await emit_event({"type": "backend.connected", "backend": "chroma"})
        except Exception as e:
            log.warning("ChromaDB unavail: %s", e)

    async def _connect_neo4j():
        global NEO
        if not HAS_NEO or NEO is not None:
            return
        # Use the configured URI/credentials (was hardcoded to localhost/no-auth,
        # which ignored NEO4J_URI/USER/PASS). verify_connectivity() makes /health
        # reflect reality instead of a lazily-created (always-truthy) driver.
        uri  = getattr(cfg, "NEO4J_URI", "bolt://localhost:7687")
        user = (getattr(cfg, "NEO4J_USER", "") or "")
        pw   = (getattr(cfg, "NEO4J_PASS", "") or "")
        auth_variants = [((user, pw) if user else None)]
        if user:                       # fall back to no-auth (server may have auth disabled)
            auth_variants.append(None)
        for _attempt in range(1, 13):  # ~1 min of retries
            for auth in auth_variants:
                drv = None
                try:
                    drv = AsyncGraphDatabase.driver(uri, auth=auth)
                    await drv.verify_connectivity()
                    NEO = drv
                    log.info("✓ Neo4j connected (attempt %d): %s (auth=%s)",
                             _attempt, uri, "yes" if auth else "no")
                    await emit_event({"type": "backend.connected", "backend": "neo4j"})
                    return
                except Exception as e:
                    if _attempt == 1 and auth is auth_variants[-1]:
                        log.warning("Neo4j not ready yet (will retry): %s  [uri=%s]", e, uri)
                    if drv is not None:
                        try: await drv.close()
                        except Exception: pass
            await asyncio.sleep(5)
        log.error("✗ Neo4j unreachable after retries [uri=%s] — /health will show neo4j:false", uri)

    async def _connect_backends():
        await asyncio.gather(
            _connect_redis(), _connect_postgres(),
            _connect_chroma(), _connect_neo4j(),
            return_exceptions=True,
        )

    # ── Load companion capability modules ────────────────────────────────────
    # Search for vera_capabilities.py / vera_skills.py relative to THIS file,
    # so they load correctly regardless of the working directory.
    import importlib.util as _ilu
    _here = os.path.dirname(os.path.abspath(__file__))

    # Extra module paths: env var VERA_MODULES="path1.py,path2.py" adds more
    _module_files = [
        os.path.join(_here, "capabilities/capabilities.py"),
        os.path.join(_here, "capabilities/cap_hub_capabilities.py"),
        os.path.join(_here, "capabilities/cap_tracking.py"),
        os.path.join(_here, "fabric/memory.py"),
        os.path.join(_here, "fabric/memory_hooks.py"),
        os.path.join(_here, "fabric/data_fabric_collectors.py"),
        os.path.join(_here, "fabric/data_fabric.py"),
        os.path.join(_here, "fabric/curation_capabilities.py"),
        os.path.join(_here, "fabric/embed_provider_capabilities.py"),
        os.path.join(_here, "fabric/fabric_web_acquisition.py"),
        os.path.join(_here, "fabric/memory_second_order.py"),
        os.path.join(_here, "fabric/session_notes.py"),
        os.path.join(_here, "fabric/context.py"),
        os.path.join(_here, "fabric/memory_retrieval.py"),
        os.path.join(_here, "fabric/discovery.py"),
        os.path.join(_here, "fabric/knowledgebase.py"),
        os.path.join(_here, "text/text_ops_capabilities.py"),
        os.path.join(_here, "skills/skills.py"),
        os.path.join(_here, "skills/skills_owl.py"),
        os.path.join(_here, "dag/dag_store.py"),
        os.path.join(_here, "dag/dag_workshop_capabilities.py"),
        os.path.join(_here, "dag/loop_profiles.py"),
        os.path.join(_here, "dag/loop_orchestrator.py"),
        os.path.join(_here, "agents/agents.py"),
        os.path.join(_here, "workers/cluster.py"),
        os.path.join(_here, "workers/syslog.py"),
        os.path.join(_here, "workers/observe_elements_capabilities.py"),
        os.path.join(_here, "elements/general_widgets_capabilities.py"),
        os.path.join(_here, "elements/flow_builder_capabilities.py"),
        os.path.join(_here, "ui builder/ui_capabilities.py"),
        os.path.join(_here, "ide/ide_capabilities.py"),
        os.path.join(_here, "ide/ide_code_capabilities.py"),
        os.path.join(_here, "ide/ide_remote_capabilities.py"),
        os.path.join(_here, "ide/ide_claude_sessions_capabilities.py"),
        os.path.join(_here, "ide/vscode_capabilities.py"),
        os.path.join(_here, "ide/ide_inspect_capabilities.py"),
        os.path.join(_here, "research/research_fabric.py"),        
        # os.path.join(_here, "research_capabilities.py"),
        # os.path.join(_here, "research_recall_capabilities.py"),
        # os.path.join(_here, "research_activity_capabilities.py"),
        os.path.join(_here, "web/web_capabilities.py"),
        os.path.join(_here, "web/web_api_capabilities.py"),
        os.path.join(_here, "telegram/telegram_capabilities.py"),
        os.path.join(_here, "dream/dream_capabilities.py"),
        os.path.join(_here, "dream/project_capabilities.py"),
        os.path.join(_here, "execution/exec_capabilities.py"),
        os.path.join(_here, "proxmox/proxmox_capabilities.py"),
        os.path.join(_here, "proxmox/pxstore_capabilities.py"),
        os.path.join(_here, "monitor/monitor_capabilities.py"),
        os.path.join(_here, "monitor/perf_capabilities.py"),
        os.path.join(_here, "babblefish/babblefish_capabilities.py"),
        os.path.join(_here, "netmon/netmon_capabilities.py"),
        os.path.join(_here, "provisioning/provisioning_capabilities.py"),
        os.path.join(_here, "provisioning/identity_capabilities.py"),
        os.path.join(_here, "provisioning/lldap_capabilities.py"),
        os.path.join(_here, "provisioning/identity_resolver.py"),
        os.path.join(_here, "provisioning/identity_migrate.py"),
        os.path.join(_here, "provisioning/openbao_identity.py"),
        os.path.join(_here, "provisioning/enroll_capabilities.py"),
        os.path.join(_here, "provisioning/software_capabilities.py"),
        os.path.join(_here, "provisioning/components_capabilities.py"),
        os.path.join(_here, "provisioning/stores_capabilities.py"),
        os.path.join(_here, "provisioning/security_provision_capabilities.py"),
        os.path.join(_here, "provisioning/autoenroll_capabilities.py"),
        os.path.join(_here, "networking/netgraph_capabilities.py"),
        os.path.join(_here, "networking/netsec_capabilities.py"),
        os.path.join(_here, "interaction/interaction_capabilities.py"),
        os.path.join(_here, "foundry/foundry_capabilities.py"),
        os.path.join(_here, "workers/docker_capabilities.py"),
        os.path.join(_here, "workers/workers.py"),
        os.path.join(_here, "workers/nodes_capabilities.py"),
        os.path.join(_here, "remote/remote_capabilities.py"),
        os.path.join(_here, "remote/workspace_capabilities.py"),
        os.path.join(_here, "remote/operator_capabilities.py"),
        os.path.join(_here, "remote/portainer_capabilities.py"),
        os.path.join(_here, "remote/metrics_capabilities.py"),
        os.path.join(_here, "remote/session_sandbox_capabilities.py"),
        os.path.join(_here, "web/browser_capabilities.py"),
        os.path.join(_here, "vllm/vllm_capabilities.py"),
        os.path.join(_here, "catalog/catalog_capabilities.py"),
        os.path.join(_here, "catalog/benchmark_capabilities.py"),
        os.path.join(_here, "machine learning/ml_workshop.py"),
        os.path.join(_here, "machine learning/ml_training.py"),
        os.path.join(_here, "machine learning/ml_onnx.py"),
        os.path.join(_here, "markets/markets_capabilities.py"),
        os.path.join(_here, "markets/markets_data_capabilities.py"),
        os.path.join(_here, "markets/markets_analysis_capabilities.py"),
        os.path.join(_here, "markets/markets_lab_capabilities.py"),
        os.path.join(_here, "markets/markets_studio_capabilities.py"),
        os.path.join(_here, "markets/markets_evolve_capabilities.py"),
        os.path.join(_here, "mesh/mesh_capabilities.py"),
        os.path.join(_here, "mesh/mesh_toolkit_capabilities.py"),
        os.path.join(_here, "mesh/mesh_ui_capabilities.py"),
        os.path.join(_here, "mesh/mesh_boards_capabilities.py"),
        os.path.join(_here, "build/build_capabilities.py"),
        os.path.join(_here, "board/board_capabilities.py"),
        os.path.join(_here, "openclaw/openclaw_capabilities.py"),
        os.path.join(_here, "providers/providers_capabilities.py"),
        # os.path.join(_here, "dream/dream_research_integration.py"),
        # os.path.join(_here, "project_research_extension.py"),
        os.path.join(_here, "ontologies/cap_ontology.py"),
        os.path.join(_here, "chat/chat_panels_capabilities.py"),
        os.path.join(_here, "agent_loop_output_capabilities.py"),
        os.path.join(_here, "activity/activity_capabilities.py"),
        os.path.join(_here, "worldview/worldview_jepa.py"),
        os.path.join(_here, "research/researcher_api.py"),
        os.path.join(_here, "research/nlp_capabilities.py"),
        os.path.join(_here, "vector browser/vector_browser_capabilites.py"),
        os.path.join(_here, "workers/job_persistance.py"),
        os.path.join(_here, "accounts/accounts_capabilities.py"),
        os.path.join(_here, "calendar/calendar_capabilities.py"),
        os.path.join(_here, "calendar/longterm_scheduler.py"),
        os.path.join(_here, "email/email_capabilities.py"),
        os.path.join(_here, "render/render_capabilities.py"),
        os.path.join(_here, "render/chat_render_capabilities.py"),
        os.path.join(_here, "media/media_capabilities.py"),
        os.path.join(_here, "images/image_fabric.py"),
        os.path.join(_here, "character/character_capabilities.py"),
        os.path.join(_here, "spritegen/spritegen_capabilities.py"),
        os.path.join(_here, "podcast/podcast_capabilities.py"),
        os.path.join(_here, "commerce/commerce_capabilities.py"),
        os.path.join(_here, "commerce/commerce_platforms.py"),
        os.path.join(_here, "commerce/commerce_vinted.py"),
        os.path.join(_here, "commerce/commerce_pricing_capabilities.py"),
        os.path.join(_here, "commerce/commerce_uk_tax.py"),
        os.path.join(_here, "commerce/commerce_fulfilment.py"),
        os.path.join(_here, "commerce/commerce_listing.py"),
        os.path.join(_here, "commerce/commerce_intake.py"),
        os.path.join(_here, "commerce/commerce_market.py"),
        os.path.join(_here, "commerce/commerce_enrich.py"),
        os.path.join(_here, "commerce/commerce_ops.py"),
        os.path.join(_here, "commerce/commerce_stores.py"),
        os.path.join(_here, "business/business_capabilities.py"),
        os.path.join(_here, "business/business_sim.py"),
        os.path.join(_here, "business/thermal_printer_capabilities.py"),
        os.path.join(_here, "mcp/mcp_catalog_capabilities.py"),
        os.path.join(_here, "evolve/evolve_capabilities.py"),
        # Operator: general observe→think→act web/computer operator (drives any
        # web UI or web-served VM). Loaded after evolve so its documentation
        # mission can reach evolve.sandbox.* for the loop-lab target.
        os.path.join(_here, "operator/operator_web_capabilities.py"),
        # Integrations Hub: integration-centric layer (embed/interact/api/mcp/ssh
        # with enforced per-app access) over app.mount/operator/mcp-catalog/identity.
        # Loaded last so its lazy cap lookups resolve every referenced subsystem.
        os.path.join(_here, "integrations/integrations_capabilities.py"),
        os.path.join(_here, "vera_graph_panels.py")

    ]
    _extra = os.getenv("VERA_MODULES", "")
    if _extra:
        _module_files += [p.strip() for p in _extra.split(",") if p.strip()]

    for _fpath in _module_files:
        _mod_name = os.path.splitext(os.path.basename(_fpath))[0]
        if not os.path.exists(_fpath):
            log.warning("Module not found (skipping): %s", _fpath)
            continue
        if _mod_name in sys.modules:
            log.debug("Module already loaded: %s", _mod_name)
            continue
        try:
            _spec = _ilu.spec_from_file_location(_mod_name, _fpath)
            _mod  = _ilu.module_from_spec(_spec)
            sys.modules[_mod_name] = _mod
            _caps_before = len(CAPABILITY_REGISTRY)
            _spec.loader.exec_module(_mod)
            _caps_added = len(CAPABILITY_REGISTRY) - _caps_before
            log.info("✓ %-25s caps=%-3d  ui_panels=%d",
                     _mod_name, len(CAPABILITY_REGISTRY), len(UI_PANELS))
            LOADED_MODULES.append({
                "name": _mod_name, "path": _fpath,
                "caps_added": _caps_added, "status": "ok",
            })
        except Exception as e:
            log.error("✗ %s failed to load: %s", _mod_name, e)
            import traceback as _tb; log.error(_tb.format_exc())
            LOADED_MODULES.append({
                "name": _mod_name, "path": _fpath,
                "caps_added": 0, "status": f"error: {e}",
            })
            _caps_before = len(CAPABILITY_REGISTRY)
    
    # import Vera.vera.cap_tracking as cap_tracking
    # cap_tracking.install(sys.modules[__name__])
    
    import Vera.vera.agents.agents_context_patch

    _ct = sys.modules.get("cap_tracking")
    if _ct:
        _ct.install(sys.modules[__name__])
        asyncio.create_task(_activity_worker())
        log.info("activity_worker: started via cap_tracking_config")
    elif os.getenv("VERA_ACTIVITY_RECORDING", "0") == "1":
        asyncio.create_task(_activity_worker())

    # Mount ALL capability HTTP routes in one pass
    _mount_all_http_routes(app)

    # Install asyncio uncaught-exception handler
    try:
        import asyncio as _aio
        _aio.get_event_loop().set_exception_handler(_asyncio_exception_handler)
    except Exception:
        pass

    # Pre-warm + pin the default executor. run_in_executor spawns pool threads
    # LAZILY: a submit that finds no idle worker calls Thread.start(), which
    # blocks until the new thread's bootstrap gets the GIL — under load that
    # wait alone stalls the loop >1s (hang dumps ending in threading.py
    # _started.wait inside run_in_executor). Spawning the whole pool once at
    # startup makes every later submit enqueue-only.
    try:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        import threading as _thr
        _n_exec = min(32, (os.cpu_count() or 8) + 4)
        _default_exec = _TPE(max_workers=_n_exec, thread_name_prefix="vera-exec")
        _release = _thr.Event()
        # Each submit sees every worker busy (all parked on the event), so the
        # pool is forced to spawn all _n_exec threads right here.
        for _ in range(_n_exec):
            _default_exec.submit(_release.wait, 10)
        _release.set()
        asyncio.get_running_loop().set_default_executor(_default_exec)
        log.info("default executor pre-warmed: %d threads", _n_exec)
    except Exception as _e:
        log.debug("executor pre-warm skipped: %s", _e)

    # Background tasks — start before yield so they run immediately
    worker_id=f"worker-{new_id()[:8]}"
    asyncio.create_task(_loop_lag_watchdog())      # detect event-loop stalls (WS-flap diag)
    asyncio.create_task(_connect_backends())       # DB connections with retry — non-blocking
    asyncio.create_task(worker_loop(worker_id))
    asyncio.create_task(result_listener())
    asyncio.create_task(cancel_listener())
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(instance_health_loop(interval=20))
    asyncio.create_task(_load_ollama_persistence())   # hydrate routing/nodes/embed from Redis
    asyncio.create_task(_openbao_autounseal_boot())    # KMS auto-unseal OpenBao if configured
    # Activity recording — disabled by default, enable with VERA_ACTIVITY_RECORDING=1
    if os.getenv("VERA_ACTIVITY_RECORDING", "0") == "1":
        asyncio.create_task(_activity_worker())
        log.info("activity_worker: started (VERA_ACTIVITY_RECORDING=1)")
    else:
        log.debug("activity_worker: disabled (set VERA_ACTIVITY_RECORDING=1 to enable)")

    # Full-GC pauses were stalling the loop >1s (hang traces bottoming out in
    # weakref callbacks / SSLContext.__new__ / bare run_until_complete = a
    # gen-2 collection caught mid-allocation). This process carries a huge
    # permanent object graph — every module, the cap registry, panel HTML —
    # that each gen-2 collection rescans on the loop thread. Collect once now,
    # then freeze() the startup graph into the permanent generation so future
    # collections only scan objects allocated after startup.
    try:
        import gc as _gc
        _gc.collect()
        _gc.freeze()
        # freeze() alone wasn't enough: post-startup survivors (neo4j
        # session/result cycles, cap results, parsed JSON) keep growing, and
        # automatic gen-2 passes over them still stalled the loop 1-2s (hang
        # traces ending in neo4j workspace.__del__ = the finalizer sweep of
        # such a pass). Raise thresholds so automatic full collections are
        # rare, and let _gc_pacer do the equivalent work on a schedule.
        _gc.set_threshold(int(os.getenv("VERA_GC_GEN0", "10000") or 10000),
                          int(os.getenv("VERA_GC_GEN1", "25") or 25),
                          int(os.getenv("VERA_GC_GEN2", "25") or 25))
        if _gc_pause_timer not in _gc.callbacks:
            _gc.callbacks.append(_gc_pause_timer)
        asyncio.create_task(_gc_pacer())
        log.info("gc: froze %d startup objects out of gen-2 scans; "
                 "thresholds %s, paced full collect every %ss",
                 _gc.get_freeze_count(), _gc.get_threshold(),
                 os.getenv("VERA_GC_PACE_S", "120"))
    except Exception:
        pass

    log.info("Vera Orchestrator v3 ready — %d caps, %d Ollama nodes",len(CAPABILITY_REGISTRY),len(OLLAMA_INSTANCES))
    yield
    # ── Shutdown hooks ───────────────────────────────────────────────────────
    # Modules register coroutines here to be run as Vera stops. This is the ONLY
    # reliable place: FastAPI ignores @app.on_event("shutdown") once a `lifespan`
    # is supplied (which it is, right here), so a module registering that way
    # would silently never fire.
    for _hook in list(SHUTDOWN_HOOKS):
        try:
            await _hook()
        except Exception as _he:
            log.warning("shutdown hook %s failed: %s",
                        getattr(_hook, "__name__", "?"), _he)
    if REDIS:   await REDIS.aclose()
    if PG_POOL: await PG_POOL.close()
    if NEO:     await NEO.close()
    log.info("Vera shut down")


def _make_get_handler(cap: dict, cap_name: str):
    """
    Build a GET handler that reads query params, type-coerces them from the
    capability schema, and wraps execution with full disconnect safety.
    Uses a factory function to avoid the closure-in-loop variable capture bug.
    """
    schema_props = cap["schema"].get("properties", {})

    async def _handler(request: Request):
        params   = dict(request.query_params)
        coerced  = {}
        for k, v in params.items():
            if k == "trace_id":
                continue
            stype = schema_props.get(k, {}).get("type", "string")
            try:
                if   stype == "integer": coerced[k] = int(v)
                elif stype == "number":  coerced[k] = float(v)
                elif stype == "boolean": coerced[k] = v.lower() in ("true", "1", "yes")
                else:                    coerced[k] = v
            except (ValueError, TypeError):
                coerced[k] = v
        # Mark this cap as directly HTTP-invoked for the duration of the call
        # (lets caps distinguish a human/UI request from an internal one).
        _tok = CURRENT_HTTP_CAP.set(cap_name)
        try:
            result = await cap["func"](**coerced, trace_id=new_id())
            # Caps may return a Response directly (HTMLResponse panels,
            # PlainTextResponse sources, StreamingResponse) — pass through.
            if isinstance(result, Response):
                return result
            return await _json_response(result)
        except HTTPException:
            raise
        except Exception as e:
            log.error("GET cap %s: %s", cap_name, e, exc_info=True)
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        finally:
            CURRENT_HTTP_CAP.reset(_tok)

    _handler.__name__ = f"_get_{cap_name.replace('.','_')}"
    return _handler


def _make_post_handler(cap: dict, cap_name: str):
    """
    Build a POST handler.  Body is received as a raw Request so we can:
      - avoid the mutable-default-dict  body: dict = {}  footgun
      - handle disconnect (client gone before slow cap finishes) cleanly
      - support both {"name":"x","arguments":{}} envelope and flat {"key":"val"} bodies

    Body keys are filtered to the function's schema so unknown UI fields never
    cause 'unexpected keyword argument' errors — forward/backward compatible.
    """
    # Pre-compute accepted parameter names from schema (excludes trace_id)
    _accepted = set(cap.get("schema", {}).get("properties", {}).keys())
    # A cap whose function takes **kwargs accepts MORE than its schema advertises:
    # the schema builder skips VAR_KEYWORD, so a thin delegating cap (e.g.
    # dag.agent_loop_v7(goal, **kwargs) forwarding to the v6 runner) publishes only
    # its named params. Filtering to that schema silently dropped every other field
    # — session_id, max_steps, condense_output … — so the endpoint ran pure defaults
    # while appearing to accept the request. Skip the filter for those caps; the
    # callee's own signature (and _filter_kwargs) still rejects genuine typos.
    try:
        _takes_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in inspect.signature(cap["func"]).parameters.values())
    except (ValueError, TypeError):
        _takes_kwargs = False

    async def _handler(request: Request):
        # Read body — empty body is fine (no params needed)
        try:
            raw = await request.body()
            body: dict = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}

        tid = body.pop("trace_id", None) or new_id()

        # Filter to only accepted params — prevents 'unexpected keyword argument'
        # when the UI sends fields the current function version doesn't know about yet.
        # If _accepted is empty (no schema), pass everything through unchanged.
        if _accepted and not _takes_kwargs:
            body = {k: v for k, v in body.items() if k in _accepted}

        _tok = CURRENT_HTTP_CAP.set(cap_name)
        try:
            result = await cap["func"](**body, trace_id=tid)
            if isinstance(result, Response):
                return result
            return await _json_response(result)
        except HTTPException:
            raise
        except (asyncio.CancelledError, RuntimeError) as e:
            if isinstance(e, asyncio.CancelledError) or \
               "transport" in str(e).lower() or "closed" in str(e).lower():
                log.debug("Client disconnected: %s", cap_name)
                raise HTTPException(499, "Client disconnected")
            raise
        except Exception as e:
            # exc_info: a bare str(e) is unusable for a KeyError/AttributeError,
            # whose message is just the missing key ("'code.author'") with no hint
            # where it came from. The traceback is the only way to locate it.
            log.error("POST cap %s: %s", cap_name, e, exc_info=True)
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        finally:
            CURRENT_HTTP_CAP.reset(_tok)

    _handler.__name__ = f"_post_{cap_name.replace('.','_')}"
    return _handler


def _json_safe(obj: Any) -> Any:
    """Recursively make an object JSON-serialisable, replacing unserializable
    values. Scalars are returned AS-IS: the old code called json.dumps() on
    every leaf to 'test' it, so a large result did millions of tiny json.dumps
    calls and blocked the event loop >1s (WS flap). A str/int/float/bool/None is
    always JSON-safe — never re-encode it."""
    # Fast path: the overwhelming majority of leaves are plain scalars.
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {(k if isinstance(k, str) else str(k)): _json_safe(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bytes):
        try: return obj.decode("utf-8", "replace")
        except Exception: return str(obj)
    # Unknown/complex type (rare) — only NOW pay for a serializability probe.
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


async def _json_response(result: Any) -> Response:
    """Sanitize + JSON-encode a capability result off the event loop. On large
    results (e.g. 50k-bar indicator series) the _json_safe walk plus
    JSONResponse's own json.dumps are >1s of pure CPU — enough to stall the
    loop and flap every WebSocket. Encoding matches JSONResponse.render()."""
    def _render() -> bytes:
        return json.dumps(
            _json_safe(result), ensure_ascii=False, allow_nan=False,
            indent=None, separators=(",", ":"),
        ).encode("utf-8")
    body = await asyncio.to_thread(_render)
    # A single Response body reaches uvicorn as ONE transport.write(); over
    # TLS that's one synchronous _sslobj.write() encrypting the whole thing
    # (a captured >1s loop stall on multi-MB results). Stream large bodies in
    # chunks so each write is bounded and the loop breathes between them.
    if len(body) <= 1 << 20:
        return Response(content=body, media_type="application/json")
    async def _chunks(b=body, step=256 * 1024):
        view = memoryview(b)
        for i in range(0, len(b), step):
            yield bytes(view[i:i + step])
    return StreamingResponse(_chunks(), media_type="application/json",
                             headers={"Content-Length": str(len(body))})


def _mount_all_http_routes(app: FastAPI):
    """
    Single pass — mounts every @capability that declared http_method + http_path,
    then auto-mounts POST /<cap>/<name> for every cap without an explicit route.

    Key correctness guarantees:
      • No mutable default arguments (body: dict = {} bug avoided via Request)
      • No closure-in-loop variable capture (factory functions used)
      • RuntimeError / CancelledError from client disconnect caught per-handler
      • GET params type-coerced from schema
      • POST body read from raw Request, not FastAPI model binding
    """
    claimed_paths: set = set()

    # ── Pass 1: explicit http_method + http_path declared on the capability ──────
    for cap_name, cap in list(CAPABILITY_REGISTRY.items()):
        method = (cap.get("http_method") or "").upper()
        path   = cap.get("http_path") or ""
        if not method or not path:
            continue

        claimed_paths.add((method, path))
        api_tags = cap.get("http_tags") or [cap_name.split(".")[0]]
        summary  = (cap.get("description") or cap_name)[:120]

        # /mcp/call gets its own hand-crafted handler (name+arguments envelope)
        if path == "/mcp/call":
            handler = _make_mcp_call_handler()
        elif method == "GET":
            handler = _make_get_handler(cap, cap_name)
        else:
            handler = _make_post_handler(cap, cap_name)

        try:
            app.add_api_route(path, handler, methods=[method],
                              tags=api_tags, summary=summary)
            log.debug("Mounted: %s %s → %s", method, path, cap_name)
        except Exception as e:
            log.warning("Route mount failed %s %s: %s", method, path, e)

    # ── Pass 2: auto-mount POST /<group>/<name> for every remaining cap ──────────
    for cap_name, cap in list(CAPABILITY_REGISTRY.items()):
        if cap.get("source") == "mcp_proxy":
            continue  # proxy caps forward to remote — no local REST needed

        auto_path = "/" + cap_name.replace(".", "/")
        if any((m, auto_path) in claimed_paths for m in ("GET", "POST", "PUT", "DELETE")):
            continue  # already explicitly mounted

        handler = _make_post_handler(cap, cap_name)
        try:
            app.add_api_route(
                auto_path, handler, methods=["POST"],
                tags=cap.get("tags") or [cap_name.split(".")[0]],
                summary=(cap.get("description") or cap_name)[:120],
            )
        except Exception as e:
            log.warning("Auto-route failed %s: %s", cap_name, e)

    log.info("HTTP routes mounted: %d explicit, %d auto-POST",
             len(claimed_paths),
             sum(1 for c in CAPABILITY_REGISTRY.values()
                 if not c.get("http_path") and c.get("source") != "mcp_proxy"))


APP = FastAPI(title="Vera Orchestrator", version="3.0", lifespan=lifespan)
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@APP.get("/ui/elements/research_card.js", include_in_schema=False)
async def _serve_research_card_js():
    """<vera-research-card> — the ONE research progress/report renderer, shared
    by the chat panel and the agent-loop output so a fix lands in both."""
    from fastapi.responses import Response as _Resp
    p = Path(__file__).parent / "research_card_element.js"
    if p.exists():
        return _Resp(content=p.read_text(encoding="utf-8"),
                     media_type="application/javascript",
                     headers={"Cache-Control": "no-cache"})
    return _Resp(content="console.warn('vera-research-card element JS not found');",
                 media_type="application/javascript")


@APP.get("/ui/elements/panel_copilot.js", include_in_schema=False)
async def _serve_panel_copilot_js():
    from fastapi.responses import Response as _Resp
    p = Path(__file__).parent / "panel_copilot_element.js"
    if p.exists():
        return _Resp(content=p.read_text(encoding="utf-8"),
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})
    return _Resp(content="console.warn('vera-panel-copilot element JS not found');",
                media_type="application/javascript")


# ── Client config injection ───────────────────────────────────────────────────
# Browser panels can't read the Python config, so the single source-of-truth
# domain (cfg.BACKEND_HOST) is handed to the frontend here: a tiny script setting
# window.__VERA_DOMAIN__ / window.__VERA_BASE__ is injected into every served HTML
# document. Panels resolve their backend as
#     window.location.origin || window.__VERA_BASE__ || <location-derived>
# so no hostname is hardcoded anywhere in the UI.
_VERA_BASE_URL = (f"{'https' if cfg.TLS_ENABLED else 'http'}://"
                  f"{cfg.BACKEND_HOST}:{cfg.ORCHESTRATOR_PORT}")
_CLIENT_CONFIG_SNIPPET = (
    "<script>window.__VERA_DOMAIN__=%s;window.__VERA_BASE__=%s;</script>" % (
        json.dumps(cfg.BACKEND_HOST), json.dumps(_VERA_BASE_URL))
).encode("utf-8")


@APP.middleware("http")
async def _inject_client_config(request: Request, call_next):
    """Inject the configured domain into every served HTML page (see above)."""
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", "").lower():
        return response
    if response.headers.get("content-encoding"):
        return response  # don't touch compressed bodies
    from starlette.responses import Response as _Resp
    body = b"".join([chunk async for chunk in response.body_iterator])
    if response.status_code == 200 and body:
        lower = body.lower()
        idx = lower.find(b"<head")
        gt = body.find(b">", idx) if idx != -1 else -1
        if gt != -1:
            body = body[:gt + 1] + _CLIENT_CONFIG_SNIPPET + body[gt + 1:]
        else:
            body = _CLIENT_CONFIG_SNIPPET + body
    headers = dict(response.headers)
    headers.pop("content-length", None)
    headers.pop("content-type", None)
    return _Resp(content=body, status_code=response.status_code,
                 headers=headers, media_type=response.headers.get("content-type"))


@APP.get("/memgraph/panel", include_in_schema=False)
async def _memgraph_panel_route():
    from fastapi.responses import HTMLResponse
    from pathlib import Path as _P
    p = _P(__file__).parent / "fabric/memory_graph_panel.html"
    return HTMLResponse(
        p.read_text(encoding="utf-8") if p.exists()
        else "<p style='color:red'>memory_graph_panel.html not found</p>"
    )

try:
    register_ui(
        "memory-graph", "Memory Graph", "",
        """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/memgraph/panel"
          style="flex:1;border:none;width:100%;height:100%"
          allow="clipboard-read; clipboard-write">
  </iframe>
  </div>""",
        "", ui_caps=["memory.graph_full","memory.session_nodes","memory.session_edges",
                     "memory.all_nodes","memory.all_edges","memory.traverse",
                     "memory.label","memory.label_session","cap_tracking.set_session"],
        # Folded into the Fabric panel's "Memory" sub-tab (fabric_panel.html).
        # mode="element" keeps it in the registry so it can still be added as a
        # dashboard widget or promoted back to a top-level tab from the tab bar's
        # "+" picker.
        mode="element", tab_order=72,
        specialist_agent="fabric-librarian",
        specialist_loop_profile="fabric-discovery",
    )
except Exception as _mge:
    log.warning("memgraph register_ui: %s", _mge)

# ── Model Routing — moved into Workers & Ollama (its "Model Routing" pane,
# workers_ollama_panel.html, lazily mounts the same /ui/panels/model-routing
# route this used to register as its own top-level tab) — no longer a
# standalone top-level page. Route + panel file (routing_panel.html) stay
# live below (_model_routing_panel), just not independently register_ui'd.
#
# try:
#     register_ui(
#         "model-routing", "Model Routing", "⇶",
#         '<div id="panel-model-routing" style="height:100%;overflow:hidden;background:var(--bg0)"></div>',
#         r"""
# (function mountModelRoutingPanel() {
#   var mount = document.getElementById('panel-model-routing');
#   if (!mount || mount._mrMounted) return;
#   mount._mrMounted = true;
#   var frame = document.createElement('iframe');
#   var backendBase = (document.getElementById('backendUrl') || {}).value || '';
#   backendBase = backendBase.replace(/\/$/, '') || window._veraBase || (window.__VERA_BASE__||('http://'+location.hostname+':8999'));
#   frame.src = backendBase + '/ui/panels/model-routing';
#   frame.style.cssText = 'width:100%;height:100%;border:none;display:block;background:var(--bg0,#181614)';
#   frame.allow = 'clipboard-read; clipboard-write';
#   mount.appendChild(frame);
# })();
# """,
#         ui_caps=[
#             "ollama.routing.get", "ollama.routing.save", "ollama.profile.activate",
#             "ollama.profile.delete", "ollama.cap_routing.get", "ollama.cap_routing.save",
#             "ollama.cap_routing.delete", "ollama.role_profiles.get",
#             "ollama.role_profiles.save", "ollama.role_profiles.delete",
#             "llm.route.resolve", "ollama.route_stats", "ollama.request_log",
#             "media.nodes", "media.node.add", "media.node.remove",
#             "media.node.config", "media.ping", "vllm.status",
#         ],
#         mode="tab", tab_order=2,
#     )
# except Exception as _mre:
#     log.warning("model-routing register_ui: %s", _mre)


# ── Global exception capture → syslog WS feed ─────────────────────────────────
import traceback as _traceback
import logging   as _logging

class _VeraWsLogHandler(_logging.Handler):
    """
    Routes Python ERROR/CRITICAL log records to the WS syslog feed.
    Installed once at startup so uvicorn, httpx, asyncio errors all show up.
    """
    def emit(self, record: _logging.LogRecord):
        if record.levelno < _logging.ERROR:
            return
        # Drop client-disconnect tracebacks (e.g. uvicorn's "Exception in ASGI
        # application" when a polling client closed the socket mid-response).
        if record.exc_info and record.exc_info[1] is not None \
                and _is_client_disconnect(record.exc_info[1]):
            return
        try:
            msg = self.format(record)
            exc = ""
            if record.exc_info:
                exc = "".join(_traceback.format_exception(*record.exc_info))
            payload = {
                "type":    "syslog.error",
                "level":   record.levelname,
                "logger":  record.name,
                "message": msg,
                "exc":     exc[-2000:] if exc else "",
                "file":    f"{record.pathname}:{record.lineno}",
            }
            import asyncio as _aio
            loop = None
            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                pass
            if loop and loop.is_running():
                loop.create_task(emit_event(payload))
        except Exception:
            pass  # never let the handler crash the app

_ws_log_handler = _VeraWsLogHandler()
_ws_log_handler.setLevel(_logging.ERROR)
_ws_log_handler.setFormatter(_logging.Formatter("%(name)s: %(message)s"))
# Install on root logger — catches everything (uvicorn, httpx, vera.*)
_logging.getLogger().addHandler(_ws_log_handler)

def _is_client_disconnect(exc: BaseException) -> bool:
    """
    True for exceptions caused by the client closing the connection mid-response,
    not by a real server fault. uvloop raises a bare RuntimeError when uvicorn
    writes to an already-closed transport; starlette raises ClientDisconnect.
    These are routine for short-interval dashboard polling and must not be
    escalated to CRITICAL syslog noise.
    """
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return True
    if type(exc).__name__ == "ClientDisconnect":
        return True
    if isinstance(exc, RuntimeError):
        m = str(exc)
        if "the handler is closed" in m or "unable to perform operation on" in m:
            return True
    return False

@APP.exception_handler(Exception)
async def _global_exception_handler(request, exc):
    """Catch unhandled route exceptions and emit to syslog before re-raising."""
    from fastapi.responses import JSONResponse
    # Never intercept WebSocket upgrade requests — returning JSONResponse here
    # sends HTTP 500 instead of 101, which silently breaks the WS handshake
    if request.headers.get("upgrade", "").lower() == "websocket":
        raise exc
    # Client closed the socket before we could write the response — benign for
    # polling endpoints. Don't emit syslog noise; the response can't be sent
    # anyway since the transport is already gone.
    if _is_client_disconnect(exc):
        raise exc
    tb = _traceback.format_exc()
    await emit_event({
        "type":    "syslog.error",
        "level":   "CRITICAL",
        "logger":  "vera.asgi",
        "message": f"Unhandled exception in {request.url.path}: {exc}",
        "exc":     tb[-2000:],
        "file":    "",
    })
    return JSONResponse({"error": str(exc)}, status_code=500)

def _asyncio_exception_handler(loop, context):
    """Catch unhandled asyncio task exceptions and emit to syslog."""
    exc  = context.get("exception")
    msg  = context.get("message", "unknown")
    tb   = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else ""
    loop.create_task(emit_event({
        "type":    "syslog.error",
        "level":   "ERROR",
        "logger":  "vera.asyncio",
        "message": f"Async task error: {msg}",
        "exc":     tb[-2000:],
        "file":    "",
    }))


# ── DAG HITL streaming ───────────────────────────────────────────────────────

_HITL_PENDING: dict = {}

@APP.post("/dag/hitl/respond")
async def dag_hitl_respond(request: Request):
    """Receive human approval/rejection for a paused HITL DAG step."""
    import json as _json
    try:    body = await request.json()
    except: body = {}
    trace_id     = body.get("trace_id", "")
    action       = body.get("action", "approve")
    edited_raw   = body.get("edited_params", "{}")
    try:    ep = _json.loads(edited_raw) if isinstance(edited_raw, str) else (edited_raw or {})
    except: ep = {}
    fut = _HITL_PENDING.get(trace_id)
    if not fut or fut.done():
        return {"error": f"No pending HITL for trace_id={trace_id}"}
    fut.set_result({"action": action, "edited_params": ep})
    return {"status": "received", "action": action, "trace_id": trace_id}


async def _hitl_run_graph_stream(graph, state, hitl, auto_approve_secs):
    import json as _json, uuid as _uuid
    for i, node in enumerate(graph):
        is_parallel = isinstance(node, list) and isinstance(node[0], list)
        cap_name = None if is_parallel else (node[0] if isinstance(node, list) else node)
        out_key  = None if is_parallel else (node[1] if isinstance(node, list) and len(node)>1 else None)

        yield "dag.step_start", {"step":i,"total":len(graph),
                                  "cap":cap_name or "[parallel]","out_key":out_key}

        if hitl and not is_parallel:
            step_trace = str(_uuid.uuid4())
            cap_obj = CAPABILITY_REGISTRY.get(cap_name, {})
            accepted = set(cap_obj.get("schema",{}).get("properties",{}).keys())
            step_params = {k:v for k,v in state.items() if k in accepted}
            fut = asyncio.get_event_loop().create_future()
            _HITL_PENDING[step_trace] = fut
            yield "dag.hitl_request", {"step":i,"cap":cap_name,"out_key":out_key,
                                        "params":step_params,"trace_id":step_trace,
                                        "auto_approve_secs":auto_approve_secs}
            try:    decision = await asyncio.wait_for(fut, timeout=float(auto_approve_secs))
            except asyncio.TimeoutError: decision = {"action":"approve","edited_params":{}}
            finally: _HITL_PENDING.pop(step_trace, None)

            if decision["action"] == "reject":
                yield "dag.hitl_rejected", {"step":i,"cap":cap_name}
                yield "dag.complete", {"state":state,"aborted_at":i,"reason":"user rejected"}
                return
            if decision["action"] == "edit" and decision.get("edited_params"):
                state.update(decision["edited_params"])

        try:
            if is_parallel:
                results = await asyncio.gather(*[run_graph([n],dict(state)) for n in node],
                                               return_exceptions=True)
                for r in results:
                    if isinstance(r, dict): state.update(r)
                yield "dag.step_done", {"step":i,"parallel":True}
            else:
                cap_obj = CAPABILITY_REGISTRY.get(cap_name, {})
                if not cap_obj:
                    if out_key: state[out_key] = {"error":"unknown cap"}
                    yield "dag.step_error", {"step":i,"cap":cap_name,"error":"unknown cap"}
                else:
                    accepted = set(cap_obj["schema"].get("properties",{}).keys())
                    result = await cap_obj["func"](**{k:v for k,v in state.items() if k in accepted})
                    if out_key: state[out_key] = result
                    yield "dag.step_done", {"step":i,"cap":cap_name,"out_key":out_key,
                                             "result_preview":str(result)[:200] if result else None}
        except Exception as e:
            if out_key: state[out_key] = {"error":str(e)}
            yield "dag.step_error", {"step":i,"cap":cap_name or "[parallel]","error":str(e)}

    yield "dag.complete", {"state":state}
    # Record to memory graph
    try:
        import sys as _sys
        _mh = _sys.modules.get('memory_hooks')
        if _mh and hasattr(_mh, 'record_dag_execution'):
            import asyncio as _aio
            _aio.create_task(_mh.record_dag_execution(
                session_id=state.get('__session_id__', ''),
                dag=graph, state=state, result=state,
                agent_name=state.get('__agent_name__', ''),
                trigger='chat_dag',
            ))
    except Exception:
        pass


@APP.post("/dag/plan_stream")
async def dag_plan_stream_endpoint(request: Request):
    """
    SSE endpoint for DAG planning and execution.

    Body fields:
      goal              : str  — natural language goal
      mode              : str  — "oneshot" | "stepwise"
                          oneshot  = plan entire DAG then execute it
                          stepwise = plan one cap at a time, execute, observe, repeat
      execute           : bool — run after planning (default true)
      hitl              : bool — pause for human approval before each step
      auto_approve_secs : int  — seconds before auto-approve (default 30)
      state             : dict — seed state (merged with plan's initial_state)
      session_id        : str  — caller's session id; required for activity
                                 recording. Without it, the call still runs
                                 but does not appear in syslog as a cap.call.

    Activity recording
    ──────────────────
    This is a raw FastAPI route — it doesn't go through the @capability
    wrapper. We call record_stream_activity() in the finally block of the
    inner async generator so the stream appears as a cap.call/cap.ok pair
    in syslog and in the FOLLOWS_ACTIVITY chain like any other cap.
    Internal cap calls executed by the planner (via run_graph etc.) emit
    their own cap.call/cap.ok independently — they're TRIGGERED_BY this one.
    """
    import json as _json
    try:    body = await request.json()
    except: body = {}
    goal              = body.get("goal", "")
    mode              = body.get("mode", "oneshot")   # "oneshot" | "stepwise"
    do_execute        = bool(body.get("execute", True))
    hitl              = bool(body.get("hitl", True))
    auto_approve_secs = int(body.get("auto_approve_secs", 30))
    seed_state        = dict(body.get("state") or {})
    session_id        = body.get("session_id", "") or ""

    async def _gen():
        import time as _time
        _t0_stream = _time.monotonic()
        # Counters / accumulators for the recorded activity entry
        plan_dag_arr  = []
        plan_rationale = ""
        plan_error     = ""
        steps_emitted  = 0
        last_state_keys: list = []

        def _sse(t, d):
            return f"data: {_json.dumps({'type':t,**d})}\n\n".encode()

        try:
            if not goal:
                plan_error = "No goal provided"
                yield _sse("dag.error", {"error": plan_error}); return

            # ── STEPWISE MODE ─────────────────────────────────────────────────
            # The LLM plans one capability at a time, executes it, observes the
            # result, then decides what to do next.
            if mode == "stepwise":
                async for ev_type, ev_data in _stepwise_run(
                        goal, seed_state, hitl, auto_approve_secs):
                    if ev_type == "step.complete":
                        steps_emitted += 1
                    elif ev_type == "dag.error":
                        plan_error = (ev_data or {}).get("error", "")
                    yield _sse(ev_type, ev_data)
                yield b"data: [DONE]\n\n"
                return

            # ── ONESHOT MODE ──────────────────────────────────────────────────
            yield _sse("dag.planning", {"goal": goal})
            try:
                plan = await plan_dag(goal)
            except Exception as e:
                plan_error = str(e)
                yield _sse("dag.error", {"error": plan_error}); return

            if plan.get("error") and not plan.get("dag"):
                plan_error = plan["error"]
                yield _sse("dag.error", {"error": plan_error}); return

            dag_arr      = plan.get("dag", [])
            plan_dag_arr = dag_arr
            plan_rationale = plan.get("rationale", "")
            # CRITICAL: merge the plan's initial_state with any seed state from caller
            plan_state   = dict(plan.get("initial_state") or {})
            plan_state.update(seed_state)          # caller seed takes precedence

            yield _sse("dag.plan_ready", {
                "dag":          dag_arr,
                "initial_state": plan_state,
                "rationale":    plan_rationale,
                "problem":      plan.get("problem", ""),
                "subgoals":     plan.get("subgoals", []),
                "validation":   plan.get("validation", ""),
                "steps":        len(dag_arr),
                "execute":      do_execute,
                "hitl":         hitl,
            })

            if not do_execute:
                yield _sse("dag.done", {"dag": dag_arr}); return

            async for ev_type, ev_data in _hitl_run_graph_stream(
                    dag_arr, plan_state, hitl, auto_approve_secs):
                if ev_type == "step.complete":
                    steps_emitted += 1
                yield _sse(ev_type, ev_data)
                if isinstance(ev_data, dict) and "state" in ev_data:
                    last_state_keys = list((ev_data.get("state") or {}).keys())[:20]

            yield b"data: [DONE]\n\n"
        finally:
            elapsed_ms = round((_time.monotonic() - _t0_stream) * 1000)
            try:
                await record_stream_activity(
                    cap_name="dag.plan.stream", session_id=session_id,
                    params={
                        "goal":              goal,
                        "mode":              mode,
                        "execute":           do_execute,
                        "hitl":              hitl,
                        "auto_approve_secs": auto_approve_secs,
                        "seed_state_keys":   list(seed_state.keys())[:20],
                    },
                    result={
                        "dag":             plan_dag_arr,
                        "dag_steps":       len(plan_dag_arr),
                        "steps_emitted":   steps_emitted,
                        "rationale":       plan_rationale[:600],
                        "error":           plan_error or None,
                        "final_state_keys": last_state_keys,
                        "elapsed_ms":      elapsed_ms,
                    },
                    elapsed_ms=elapsed_ms,
                    group="dag",
                )
            except Exception as _e:
                log.debug("record_stream_activity dag.plan.stream: %s", _e)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


async def _stepwise_run(goal: str, state: dict, hitl: bool, auto_approve_secs: int):
    """
    Agentic step-by-step execution loop.
    Each iteration:
      1. LLM decides what single capability to call next (given goal + state)
      2. User approves (if HITL)
      3. Capability executes
      4. Result added to state
      5. LLM decides whether to continue or stop
    """
    import json as _json, uuid as _uuid

    cap_keys = list(CAPABILITY_REGISTRY.keys())

    def _cap_sig(k):
        cap  = CAPABILITY_REGISTRY.get(k, {})
        props = cap.get("schema", {}).get("properties", {})
        req  = set(cap.get("schema", {}).get("required", []))
        params = ", ".join(
            _format_param_sig(p, v, req)
            for p, v in props.items() if p not in ("trace_id",)
        )
        return f"  {k}({params})"

    cap_desc = "\n".join(_cap_sig(k) for k in cap_keys)

    SYSTEM = (
        "You are a Vera agent executing a goal step by step. "
        "At each step you output a JSON object with one of two shapes:\n"
        '  NEXT STEP:  {"action":"call","cap":"capability_name","params":{"key":"value"},"out_key":"result_key","reason":"why"}\n'
        '  FINISHED:   {"action":"done","summary":"what was accomplished"}\n'
        "Rules:\n"
        "- Only use capability names from the provided list.\n"
        "- params must match the capability signature exactly.\n"
        "- out_key names the state key where the result will be stored.\n"
        "- Output a SINGLE JSON object, no markdown, no explanation outside the JSON.\n"
    )

    step = 0
    MAX_STEPS = 12
    history = []  # list of {cap, result_summary} for context

    while step < MAX_STEPS:
        # Build context for the LLM
        state_summary = {k: str(v)[:200] for k, v in state.items()}
        hist_text = "\n".join(
            f"Step {i+1}: called {h['cap']} → {h['result'][:100]}"
            for i, h in enumerate(history)
        )
        prompt = (
            f"Goal: {goal}\n\n"
            f"Steps taken so far:\n{hist_text or 'None yet'}\n\n"
            f"Current state keys: {list(state.keys())}\n\n"
            f"Available capabilities:\n{cap_desc}\n\n"
            "What is the next single step to take? Output JSON only."
        )

        yield "dag.step_planning", {"step": step, "goal": goal}

        try:
            raw = await ollama_generate(prompt, system=SYSTEM, prefer_gpu=True)
        except Exception as e:
            yield "dag.error", {"error": f"LLM step planning failed: {e}"}
            return

        # Parse JSON response
        import re as _re
        decision = None
        for attempt in [raw, _re.search(r'\{[\s\S]*\}', raw or "")]:
            txt = attempt if isinstance(attempt, str) else (attempt.group() if attempt else "")
            try:
                d = json.loads(txt.strip())
                if isinstance(d, dict) and d.get("action") in ("call", "done"):
                    decision = d; break
            except Exception:
                pass

        if not decision:
            yield "dag.error", {"error": f"Could not parse step decision from LLM: {(raw or '')[:200]}"}
            return

        if decision["action"] == "done":
            yield "dag.complete", {
                "state": state,
                "summary": decision.get("summary", "Goal completed"),
                "steps_taken": step,
            }
            return

        # action == "call"
        cap_name = decision.get("cap", "")
        params   = dict(decision.get("params") or {})
        out_key  = decision.get("out_key", f"result_{step}")
        reason   = decision.get("reason", "")

        # Merge params into state so cap can find them
        run_state = dict(state)
        run_state.update(params)

        yield "dag.step_start", {
            "step": step, "cap": cap_name, "out_key": out_key,
            "params": params, "reason": reason,
        }

        if hitl:
            step_trace = str(_uuid.uuid4())
            fut = asyncio.get_event_loop().create_future()
            _HITL_PENDING[step_trace] = fut
            yield "dag.hitl_request", {
                "step": step, "cap": cap_name, "out_key": out_key,
                "params": params, "trace_id": step_trace,
                "auto_approve_secs": auto_approve_secs,
                "reason": reason,
            }
            try:
                dec = await asyncio.wait_for(fut, timeout=float(auto_approve_secs))
            except asyncio.TimeoutError:
                dec = {"action": "approve", "edited_params": {}}
            finally:
                _HITL_PENDING.pop(step_trace, None)

            if dec["action"] == "reject":
                yield "dag.hitl_rejected", {"step": step, "cap": cap_name}
                yield "dag.complete", {"state": state, "aborted_at": step, "reason": "user rejected"}
                return
            if dec["action"] == "edit" and dec.get("edited_params"):
                run_state.update(dec["edited_params"])
                params.update(dec["edited_params"])

        # Execute the capability
        cap_obj = CAPABILITY_REGISTRY.get(cap_name)
        if not cap_obj:
            yield "dag.step_error", {"step": step, "cap": cap_name, "error": "unknown capability"}
            state[out_key] = {"error": "unknown capability"}
        else:
            try:
                accepted = set(cap_obj["schema"].get("properties", {}).keys())
                result   = await cap_obj["func"](**{k: v for k, v in run_state.items() if k in accepted})
                state[out_key] = result
                result_preview = str(result)[:300] if result is not None else "null"
                history.append({"cap": cap_name, "result": result_preview})
                yield "dag.step_done", {
                    "step": step, "cap": cap_name, "out_key": out_key,
                    "result_preview": result_preview,
                }
            except Exception as e:
                err = str(e)
                state[out_key] = {"error": err}
                history.append({"cap": cap_name, "result": f"ERROR: {err}"})
                yield "dag.step_error", {"step": step, "cap": cap_name, "error": err}

        step += 1

    # Hit MAX_STEPS
    yield "dag.complete", {
        "state": state,
        "summary": f"Reached maximum {MAX_STEPS} steps",
        "steps_taken": step,
    }


@APP.get("/", include_in_schema=False)
async def _home():
    from fastapi.responses import HTMLResponse
    p = _HERE / "capability_orchestration.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>capability_orchestration.html not found</p>")


@APP.get("/ui/panels/agents-skills-ontologies", include_in_schema=False)
async def _aso_panel():
    """Combined agents/skills/ontologies shell panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "agents_skills_ontologies_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>agents_skills_ontologies_panel.html not found</p>")


@APP.get("/ui/panels/workers-ollama", include_in_schema=False)
async def _workers_ollama_panel():
    """Combined workers / ollama / jobs panel with configurable dashboard."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "workers/workers_ollama_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>workers_ollama_panel.html not found</p>")


@APP.get("/ui/panels/model-routing", include_in_schema=False)
async def _model_routing_panel():
    """Top-level Model Routing page: nodes, job-type profiles, role profiles
    (research/ide thinker-writer-verifier), per-cap rules and live activity."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "routing_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>routing_panel.html not found</p>")


# Generic catch-all panel server. Lets the chat panel embed any registered
# UI panel by id (e.g. /ui/panels/file/dag_workshop_panel.html) without
# every panel needing its own hand-written endpoint. Strict suffix check
# blocks path traversal and arbitrary file reads — only files matching
# *_panel.html in the project root are served.
@APP.get("/ui/panels/file/{panel_filename:path}", include_in_schema=False)
async def _serve_panel_file(panel_filename: str):
    from fastapi.responses import HTMLResponse
    # Whitelist: only serve sibling files matching *_panel.html, no traversal
    if (".." in panel_filename or "/" in panel_filename or "\\" in panel_filename
        or not panel_filename.endswith("_panel.html")):
        return HTMLResponse("<p style='color:red'>Bad panel filename.</p>", status_code=400)
    p = _HERE / panel_filename
    if not p.exists():
        return HTMLResponse(f"<p style='color:red'>{panel_filename} not found</p>",
                            status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@APP.get("/ui/panel/window", include_in_schema=False)
async def _ui_panel_window(id: str = ""):
    """Render any registered UI panel as a standalone, self-contained page so a
    dashboard widget can be "popped out" into its own browser window. Wraps the
    panel's fragment html+js exactly like the dashboard widget loader's srcdoc
    (theme via /ui/vera-ui.js + the same api(path,method,body) shim), so panels
    that only ever ran inside a widget iframe work identically in a real window."""
    from fastapi.responses import HTMLResponse
    p = UI_PANELS.get(id)
    if not p:
        return HTMLResponse(f"<p style='color:#c96b6b'>Panel not found: {id}</p>",
                            status_code=404)
    body = p.get("html") or "<div style='padding:10px;color:#888'>No content</div>"
    js = p.get("js") or ""
    label = (p.get("label") or id).replace("<", "&lt;")
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Vera — {label}</title>"
        "<script src='/ui/vera-ui.js'></script>"
        "<style>html,body{height:100%;margin:0;background:var(--bg0,#181614);"
        "color:var(--text,#d6d9df);font-family:var(--mono,ui-monospace,monospace);"
        "font-size:11px}*{box-sizing:border-box}</style>"
        "<script>window.BASE=window.location.origin;"
        "window.base=function(){return window.location.origin;};"
        "window.api=async function(path,method,body){method=method||'GET';"
        "var opts={method:method,headers:{'Content-Type':'application/json'}};"
        "if(body!=null)opts.body=JSON.stringify(body);"
        "try{var r=await fetch(window.location.origin+path,opts);var t=await r.text();"
        "return t&&t.trim()?JSON.parse(t):null;}catch(e){return null;}};</script>"
        "</head><body>" + body +
        ("<script>try{" + js + "\n}catch(e){console.error('[panel]',e);}</script>" if js else "") +
        "</body></html>"
    )
    return HTMLResponse(doc)


@APP.get("/ui/panels/agents-panel", include_in_schema=False)
async def _agents_panel():
    """Standalone agents editor panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "agents/agent_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>agent_panel.html not found</p>")


@APP.get("/ui/panels/skills-panel", include_in_schema=False)
async def _skills_panel():
    """Standalone skills editor panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "skills/skills_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>skills_panel.html not found</p>")


@APP.get("/ui/panels/ontologies-panel", include_in_schema=False)
async def _ontologies_panel():
    """Standalone ontologies browser panel."""
    from fastapi.responses import HTMLResponse
    p = _HERE / "ontologies/ontologies_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>ontologies_panel.html not found</p>")


# @APP.get("/worldview/panel", include_in_schema=False)
# async def _worldview():
#     from fastapi.responses import HTMLResponse
#     p = _HERE / "worldview.html"
#     return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
#         else "<p style='color:red'>worldview_panel.html not found</p>")

# register_ui(
#     "worldview-ui",
#     "worldview",
#     "",
#     """<div id="worldview-panel-mount" style="height:100%;display:flex;flex-direction:column;">
#   <iframe src="/worldview/panel"
#           style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
#           allow="clipboard-read; clipboard-write">
#   </iframe>
# </div>""",
#  "",
#     ui_caps=[

#     ],
#     mode="tab",
#     tab_order=78,
# )

# @APP.get("/chat/panel", include_in_schema=False)
# async def _chat():
#     from fastapi.responses import HTMLResponse
#     p = _HERE / "chat_panel.html"
#     return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
#                         else "<p style='color:red'>chat_panel.html not found</p>")

# register_ui(
#     "chat-panel",
#     "Chat 2",
#     "",
#     """<div id="chat-panel-mount" style="height:100%;display:flex;flex-direction:column;">
#   <iframe src="/chat/panel"
#           style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
#           allow="clipboard-read; clipboard-write">
#   </iframe>
# </div>""",
#  "",
#     ui_caps=[
#         "dream.scheduler.start", "dream.scheduler.stop", "dream.scheduler.status",
#         "dream.cycle.run", "dream.cycle.cancel",
#         "dream.preview", "dream.preview.last",
#         "dream.trigger.list", "dream.trigger.get", "dream.trigger.upsert",
#         "dream.trigger.delete", "dream.trigger.toggle", "dream.trigger.generate",
#         "dream.whitelist.list", "dream.whitelist.set",
#         "dream.config.get", "dream.config.set",
#         "dream.history", "dream.last",
#         "dream.hitl.pending", "dream.hitl.respond",
#         "dream.llm.tokens",
#         "dream.sensors.list", "dream.stages.list",
#         "dream.director.assess", "dream.timeline",
#         "dream.stage.stepwise_execute",
#     ],
#     mode="tab",
#     tab_order=78,
# )


# Note: _disconnect_guard middleware was removed because starlette's
# `call_next()` in @app.middleware("http") consumes the entire response
# body before returning — this silently kills all streaming responses.
# Disconnect errors are handled per-generator instead (try/except in each
# streaming endpoint's async generator).

# ── WebSocket MCP (stays as raw WebSocket — cannot be a @capability) ──────────

@APP.websocket("/ws/mcp")
async def ws_mcp(ws: WebSocket):
    await ws.accept(); client_id=new_id()[:8]
    # Start this connection's single writer task, then route EVERY outbound
    # frame (greeting, per-call results, broadcasts, replies) through its queue
    # so nothing ever writes the socket concurrently. The receive loop below is
    # the sole reader — one reader + one writer is the only safe arrangement.
    _ws_start_writer(ws)
    _ws_enqueue(ws, {
        "type":"connected","client_id":client_id,
        "capabilities":list(CAPABILITY_REGISTRY.keys()),
        "servers":list(MCP_SERVERS.keys()),
        "ollama_instances":{iid:{"label":i["label"],"has_gpu":i["has_gpu"],"status":i["status"]}
                            for iid,i in OLLAMA_INSTANCES.items()},
        "mode":"distributed" if REDIS else "local",
    })
    try:
        while True:
            msg=await ws.receive_json(); action=msg.get("action")

            if action=="call":
                cap_name=msg.get("name"); cap=CAPABILITY_REGISTRY.get(cap_name)
                tid=msg.get("trace_id") or new_id()
                # Keep _CURRENT_SESSION up to date so cap activity recording works
                global _CURRENT_SESSION
                _ws_sid = (msg.get("arguments") or {}).get("session_id","") if isinstance(msg.get("arguments"),dict) else ""
                if _ws_sid:
                    _CURRENT_SESSION = _ws_sid
                if not cap:
                    _ws_enqueue(ws, {"type":"error","trace_id":tid,"message":f"Unknown: {cap_name}"}); continue
                # Snapshot args now — avoid capturing loop variable in closure
                _args = dict(msg.get("arguments") or {})
                async def _call(_cap=cap, _name=cap_name, _tid=tid, _args=_args, _ws=ws):
                    try:
                        result = await _cap["func"](**_args, trace_id=_tid)
                        safe   = _json_safe(result)
                        _ws_enqueue(_ws, {"type":"tool_result","tool_name":_name,"trace_id":_tid,"content":safe})
                    except Exception as e:
                        _ws_enqueue(_ws, {"type":"error","tool_name":_name,"trace_id":_tid,"message":str(e)})
                asyncio.create_task(_call())

            elif action=="subscribe":
                WS_CONNECTIONS.append((ws,msg.get("stream")))
                _ws_enqueue(ws, {"type":"subscribed","stream":msg.get("stream")})
            elif action=="subscribe_events":
                WS_CONNECTIONS.append((ws,"__events__"))
                _ws_enqueue(ws, {"type":"subscribed","stream":"__events__"})
            elif action=="unsubscribe":
                _remove_ws(ws,msg.get("stream"))
                _ws_enqueue(ws, {"type":"unsubscribed","stream":msg.get("stream")})

            elif action=="dag_run":
                tid=new_id(); graph=list(msg.get("dag",[])); state=dict(msg.get("state",{}))
                sup=bool(msg.get("supervised",False))
                async def _dag(_g=graph,_s=state,_tid=tid,_sup=sup,_ws=ws):
                    try:
                        fn=supervised_run_graph if _sup else run_graph
                        result=await fn(_g,_s)
                        _ws_enqueue(_ws, {"type":"dag_result","trace_id":_tid,"result":_json_safe(result)})
                    except Exception as e: log.error("WS dag_run: %s",e)
                asyncio.create_task(_dag())

            elif action=="plan_and_run":
                goal=str(msg.get("goal","")); tid=new_id()
                async def _par(_goal=goal,_tid=tid,_ws=ws):
                    _ws_enqueue(_ws, {"type":"planning","trace_id":_tid,"goal":_goal})
                    plan=await plan_dag(_goal)
                    _ws_enqueue(_ws, {"type":"plan_ready","trace_id":_tid,"plan":_json_safe(plan)})
                    result=await supervised_run_graph(plan.get("dag",[]),plan.get("initial_state",{}))
                    _ws_enqueue(_ws, {"type":"dag_result","trace_id":_tid,"result":_json_safe(result)})
                asyncio.create_task(_par())

            elif action=="register_server":
                url=msg.get("url")
                if url:
                    registered=await register_mcp_server(url,msg.get("name") or url)
                    _ws_enqueue(ws, {"type":"server_registered","name":msg.get("name"),"capabilities":registered})

            elif action=="ollama_instances":
                _ws_enqueue(ws, {"type":"ollama_instances","instances":OLLAMA_INSTANCES})

            elif action=="ping":
                _ws_enqueue(ws, {"type":"pong","ts":now_iso()})

    except WebSocketDisconnect: log.info("WS disconnected: %s",client_id)
    except Exception as e: log.error("WS error [%s]: %s",client_id,e)
    finally:
        WS_CONNECTIONS[:]=[p for p in WS_CONNECTIONS if p[0] is not ws]
        _ws_stop_writer(ws)


def _ensure_self_signed_cert(certfile: str, keyfile: str) -> None:
    """Generate a self-signed cert/key pair at the given paths if either is missing.

    This is "just enough" TLS to give the browser a secure context so the Web
    Serial API and getUserMedia (webcam/mic) become available when Vera is opened
    over the LAN. http://localhost is already a secure context, so a cert is only
    needed for non-localhost origins. The cert is self-signed, so browsers show a
    one-time "not trusted" warning that must be accepted — the origin is still a
    secure context once accepted.
    """
    cp, kp = Path(certfile), Path(keyfile)
    if cp.exists() and kp.exists():
        return

    import ipaddress, socket
    from datetime import timedelta
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    cp.parent.mkdir(parents=True, exist_ok=True)
    kp.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    host = socket.gethostname()
    dns = {n for n in {"localhost", host, f"{host}.local", cfg.BACKEND_HOST} if n}
    ips = {"127.0.0.1", "::1"}
    try:
        ips.add(socket.gethostbyname(host))
    except OSError:
        pass
    # socket.gethostbyname(hostname) is unreliable in containers — Debian-based
    # images map the container's own hostname to 127.0.1.1 in /etc/hosts, so the
    # LAN-facing IP clients actually connect through (e.g. a Docker host's
    # 192.168.x.x) never makes it into the SAN list, and hostname verification
    # then fails for every non-loopback client. The UDP "connect" trick below
    # asks the OS routing table for the local address it would use to reach the
    # outside world — no packets are sent (UDP connect is local-only) — which is
    # the actual LAN IP in the overwhelming majority of single-NIC deployments.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    # Explicit override/addition for anything auto-detection can't reach
    # (a second NIC, a NAT'd Docker host IP, a hostname reverse-proxied from
    # elsewhere): comma-separated DNS names and/or IPs.
    for extra in os.getenv("TLS_EXTRA_SANS", "").split(","):
        extra = extra.strip()
        if not extra:
            continue
        try:
            ips.add(str(ipaddress.ip_address(extra)))
        except ValueError:
            dns.add(extra)
    san = [x509.DNSName(n) for n in sorted(dns)]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in sorted(ips)]

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vera-orchestrator")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(days=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))

    kp.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Generated self-signed TLS cert at %s (key: %s), SAN=%s",
             cp, kp, [str(g.value) for g in san])


if __name__=="__main__":
    ssl_kwargs: Dict[str, Any] = {}
    if cfg.TLS_ENABLED:
        _ensure_self_signed_cert(cfg.TLS_CERTFILE, cfg.TLS_KEYFILE)
        ssl_kwargs = {"ssl_certfile": cfg.TLS_CERTFILE, "ssl_keyfile": cfg.TLS_KEYFILE}
        log.info("HTTPS enabled — serving on https://%s:%s",
                 cfg.ORCHESTRATOR_HOST, cfg.ORCHESTRATOR_PORT)
    # WebSocket keepalive tuning. Defaults (ping every 20s, drop if no pong in
    # 20s) are too tight when a dream/loop burst briefly stalls the event loop:
    # the pong is delayed and uvicorn drops the socket → the whole-UI reconnect
    # flap. Ping more often but tolerate a much longer pong delay so a transient
    # stall no longer kills the connection. Both env-overridable.
    _ws_ping_interval = float(os.getenv("VERA_WS_PING_INTERVAL", "20") or 20)
    _ws_ping_timeout  = float(os.getenv("VERA_WS_PING_TIMEOUT", "75") or 75)
    uvicorn.run("Vera.vera.capability_orchestration:APP",
                host=cfg.ORCHESTRATOR_HOST, port=cfg.ORCHESTRATOR_PORT,
                reload=False,
                ws_ping_interval=_ws_ping_interval,
                ws_ping_timeout=_ws_ping_timeout,
                timeout_keep_alive=int(os.getenv("VERA_HTTP_KEEPALIVE", "75") or 75),
                **ssl_kwargs)