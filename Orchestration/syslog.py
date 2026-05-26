"""
vera_syslog.py  —  System Logging, Error Enrichment & Proactive Monitoring
===========================================================================

Architecture
────────────
Every capability call — success or failure — already emits cap.call / cap.ok /
cap.error events to the Redis event stream.  But those events are thin:
  {"type":"cap.error","name":"tts.synthesize","error":"connection refused"}

This module enriches them and writes structured log records to a dedicated
Redis stream vera:syslog that:
  • captures the full error with traceback
  • resolves the source file and function code for the failing cap
  • records the trigger_id chain (what called what)
  • classifies severity and category
  • provides queryable structured fields

Three subsystems
────────────────
1. SyslogWriter
   Listens to vera:events, intercepts cap.error / cap.call / cap.ok / worker.*
   events and writes enriched SyslogRecord entries to vera:syslog.
   Also patches the @capability decorator wrapper to capture tracebacks
   before they are swallowed into {"error":"..."} dicts.

2. Capabilities
   syslog.query   — search/filter log records (time, level, cap, keyword)
   syslog.errors  — recent errors with code context
   syslog.ask     — send a log entry + cap source code to an agent for analysis
   syslog.clear   — archive old entries
   syslog.monitor — background health check: scan recent logs, report issues
   syslog.monitor_start / syslog.monitor_stop — enable/disable proactive monitoring
   syslog.monitor_schedule — set check interval

3. DAG error context
   When run_graph produces {"error":...} in a node output, syslog.get_context()
   returns the most recent matching log entry so the LLM replanner has source
   code and stack trace context to work with.

Trigger ID chain
────────────────
Every cap call gets a trace_id (already exists).  We extend this to a
trigger chain:

  trace_id        — UUID for this specific invocation (already exists)
  trigger_id      — trace_id of the parent call that triggered this one
                    (propagated via context variable so it works across async)
  trigger_cap     — name of the capability that triggered this one
  session_id      — chat/workflow session that initiated the chain

The trigger chain is stored in a Python contextvars.ContextVar so it flows
automatically through async code without needing to thread it through every
function signature.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP, CAPABILITY_REGISTRY,
    capability, emit_event, now_iso, ollama_generate, schedule,
)

log = logging.getLogger("vera.syslog")

# ── Config ────────────────────────────────────────────────────────────────────
SYSLOG_STREAM      = "vera:syslog"
SYSLOG_MAXLEN      = int(os.getenv("SYSLOG_MAXLEN",      "5000"))
MONITOR_INTERVAL   = int(os.getenv("SYSLOG_MONITOR_INT", "300"))   # seconds
MONITOR_ENABLED    = os.getenv("SYSLOG_MONITOR",         "0") == "1"
CODE_CONTEXT_LINES = int(os.getenv("SYSLOG_CODE_LINES",  "30"))

def _redis(): return _orch.REDIS

# ── Context vars for trigger chain ───────────────────────────────────────────
_cv_trigger_id  : contextvars.ContextVar[str] = contextvars.ContextVar("trigger_id",  default="")
_cv_trigger_cap : contextvars.ContextVar[str] = contextvars.ContextVar("trigger_cap", default="")
_cv_session_id  : contextvars.ContextVar[str] = contextvars.ContextVar("session_id",  default="")

def get_trigger_chain() -> Dict[str, str]:
    return {
        "trigger_id":  _cv_trigger_id.get(""),
        "trigger_cap": _cv_trigger_cap.get(""),
        "session_id":  _cv_session_id.get(""),
    }

def set_trigger(trace_id: str, cap_name: str, session_id: str = ""):
    _cv_trigger_id.set(trace_id)
    _cv_trigger_cap.set(cap_name)
    if session_id:
        _cv_session_id.set(session_id)


# ─────────────────────────────────────────────────────────────────────────────
# SYSLOG RECORD
# ─────────────────────────────────────────────────────────────────────────────

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

@dataclass
class SyslogRecord:
    id:           str  = field(default_factory=lambda: str(uuid.uuid4()))
    ts:           str  = field(default_factory=now_iso)
    level:        str  = "INFO"          # DEBUG INFO WARNING ERROR CRITICAL
    category:     str  = "cap"           # cap | worker | system | agent | dag | proxy
    event_type:   str  = ""              # cap.error, worker.error, etc.

    # What happened
    message:      str  = ""
    detail:       str  = ""              # full error / traceback
    cap_name:     str  = ""
    cap_group:    str  = ""              # e.g. "tts", "llm", "gpu"

    # Trigger chain
    trace_id:     str  = ""
    trigger_id:   str  = ""
    trigger_cap:  str  = ""
    session_id:   str  = ""

    # Source context
    source_file:  str  = ""              # which .py file owns the cap
    source_func:  str  = ""              # function name
    source_code:  str  = ""              # relevant code snippet (for LLM analysis)
    lineno:       int  = 0

    # Extra structured data
    extra:        Dict = field(default_factory=dict)
    resolved:     bool = False           # set True when an agent suggests a fix

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("source_code", None)       # omit large field from default serialisation
        return d

    def to_redis(self) -> dict:
        """Flat string dict for Redis hset / xadd."""
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, dict):
                d[k] = json.dumps(v)
            elif not isinstance(v, str):
                d[k] = str(v)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE CODE RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_cap_source(cap_name: str) -> Dict[str, Any]:
    """
    Find the source file and extract function code for a registered capability.
    Returns {"file": str, "func": str, "code": str, "lineno": int}.
    """
    cap = CAPABILITY_REGISTRY.get(cap_name, {})
    raw_func = cap.get("raw") or cap.get("func")
    if not raw_func:
        return {}

    # Unwrap decorators
    fn = raw_func
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__

    try:
        source_file = inspect.getfile(fn)
    except (TypeError, OSError):
        source_file = ""

    try:
        lines, start = inspect.getsourcelines(fn)
        # Take up to CODE_CONTEXT_LINES lines
        snippet = "".join(lines[:CODE_CONTEXT_LINES])
        return {
            "file":   source_file,
            "func":   fn.__name__,
            "code":   snippet,
            "lineno": start,
        }
    except (OSError, TypeError):
        return {"file": source_file, "func": getattr(fn, "__name__", ""), "code": "", "lineno": 0}


# ─────────────────────────────────────────────────────────────────────────────
# SYSLOG WRITER
# ─────────────────────────────────────────────────────────────────────────────

class SyslogWriter:
    """
    Listens to vera:events Redis stream and writes enriched SyslogRecord
    entries to vera:syslog for every error/warning event.
    """

    async def write(self, rec: SyslogRecord):
        r = _redis()
        if not r:
            return
        try:
            await r.xadd(SYSLOG_STREAM, {"data": json.dumps(asdict(rec))},
                          maxlen=SYSLOG_MAXLEN)
        except Exception as e:
            log.debug("SyslogWriter.write: %s", e)

    async def write_cap_error(self,
                               cap_name: str,
                               error:    str,
                               trace_id: str = "",
                               tb:       str = "",
                               extra:    dict = None):
        """Emit a structured ERROR record for a failed capability call."""
        src = _resolve_cap_source(cap_name)
        chain = get_trigger_chain()
        rec = SyslogRecord(
            level        = "ERROR",
            category     = "cap",
            event_type   = "cap.error",
            message      = f"{cap_name} failed: {error[:200]}",
            detail       = tb or error,
            cap_name     = cap_name,
            cap_group    = cap_name.split(".")[0],
            trace_id     = trace_id,
            trigger_id   = chain["trigger_id"],
            trigger_cap  = chain["trigger_cap"],
            session_id   = chain["session_id"],
            source_file  = src.get("file", ""),
            source_func  = src.get("func", ""),
            source_code  = src.get("code", ""),
            lineno       = src.get("lineno", 0),
            extra        = extra or {},
        )
        await self.write(rec)
        log.debug("syslog: cap.error %s → %s", cap_name, error[:80])

    async def listen_loop(self):
        """Subscribe to vera:events and write syslog records for errors."""
        r = _redis()
        if not r:
            log.warning("SyslogWriter: Redis not available, listener not started")
            return

        last_id = "$"
        log.info("SyslogWriter listener started")
        while True:
            try:
                results = await r.xread({"vera:events": last_id}, count=50, block=5000)
            except Exception as e:
                log.debug("SyslogWriter xread: %s", e)
                await asyncio.sleep(2)
                continue

            if not results:
                continue

            for _, messages in results:
                for msg_id, data in messages:
                    last_id = msg_id
                    try:
                        raw = data.get(b"data", b"{}")
                        event = json.loads(raw)
                        await self._handle_event(event)
                    except Exception as e:
                        log.debug("SyslogWriter event: %s", e)

    async def _handle_event(self, event: dict):
        etype = event.get("type", "")
        level = "INFO"
        category = "system"

        if etype == "cap.error":
            cap_name  = event.get("name", "")
            error     = event.get("error", "")
            trace_id  = event.get("trace_id", "")
            attempt   = event.get("attempt", 1)
            src       = _resolve_cap_source(cap_name)
            chain     = get_trigger_chain()
            rec = SyslogRecord(
                level        = "ERROR",
                category     = "cap",
                event_type   = etype,
                message      = f"{cap_name} error (attempt {attempt}): {error[:200]}",
                detail       = error,
                cap_name     = cap_name,
                cap_group    = cap_name.split(".")[0] if cap_name else "",
                trace_id     = trace_id,
                trigger_id   = event.get("trigger_id", chain["trigger_id"]),
                trigger_cap  = event.get("trigger_cap", chain["trigger_cap"]),
                session_id   = event.get("session_id", chain["session_id"]),
                source_file  = src.get("file", ""),
                source_func  = src.get("func", ""),
                source_code  = src.get("code", ""),
                lineno       = src.get("lineno", 0),
                extra        = {"attempt": attempt},
            )
            await self.write(rec)

        elif etype == "worker.error":
            rec = SyslogRecord(
                level      = "ERROR",
                category   = "worker",
                event_type = etype,
                message    = f"Worker {event.get('worker','')} error on task {event.get('task','')}: {event.get('error','')[:200]}",
                detail     = event.get("error", ""),
                cap_name   = event.get("capability", ""),
                trace_id   = event.get("trace_id", ""),
                extra      = {"worker": event.get("worker", ""), "task": event.get("task", "")},
            )
            await self.write(rec)

        elif etype in ("system.error", "module.load_error"):
            rec = SyslogRecord(
                level      = "CRITICAL",
                category   = "system",
                event_type = etype,
                message    = event.get("message", str(event))[:300],
                detail     = event.get("traceback", ""),
                extra      = event,
            )
            await self.write(rec)

        elif etype == "ollama.proxy_request":
            # Log proxy requests at DEBUG level
            rec = SyslogRecord(
                level      = "DEBUG",
                category   = "proxy",
                event_type = etype,
                message    = f"Ollama proxy: {event.get('model','')} — {event.get('prompt','')[:80]}",
                cap_name   = "ollama.proxy",
                cap_group  = "ollama",
                extra      = event,
            )
            await self.write(rec)

        elif etype == "ollama.request":
            # Log every ollama_generate call with caller origin
            rec = SyslogRecord(
                level      = "INFO",
                category   = "ollama",
                event_type = etype,
                message    = (
                    f"Ollama request [{event.get('req_id','')}] "
                    f"model={event.get('model','')} inst={event.get('instance_id','')} "
                    f"caller={event.get('caller_file','')}:{event.get('caller_func','')} "
                    f"prompt={event.get('prompt_preview','')[:80]}"
                ),
                cap_name   = event.get("cap_name", "ollama.generate"),
                cap_group  = "ollama",
                trace_id   = event.get("req_id", ""),
                extra      = event,
            )
            await self.write(rec)

        elif etype == "ollama.request_done":
            rec = SyslogRecord(
                level      = "INFO",
                category   = "ollama",
                event_type = etype,
                message    = (
                    f"Ollama done [{event.get('req_id','')}] "
                    f"{event.get('elapsed_s',0)}s "
                    f"caller={event.get('caller_file','')}:{event.get('caller_func','')}"
                ),
                cap_name   = "ollama.generate",
                cap_group  = "ollama",
                trace_id   = event.get("req_id", ""),
                extra      = event,
            )
            await self.write(rec)

        elif etype == "ollama.request_error":
            rec = SyslogRecord(
                level      = "ERROR",
                category   = "ollama",
                event_type = etype,
                message    = (
                    f"Ollama FAILED [{event.get('req_id','')}] "
                    f"inst={event.get('instance_id','')} "
                    f"caller={event.get('caller_file','')}:{event.get('caller_func','')} "
                    f"err={event.get('error','')[:120]}"
                ),
                cap_name   = "ollama.generate",
                cap_group  = "ollama",
                trace_id   = event.get("req_id", ""),
                extra      = event,
            )
            await self.write(rec)

        # ── Track cap.call / cap.ok for chat and LLM groups ──────────────
        # These are raw FastAPI streaming endpoints (chat.stream, dag.plan)
        # that use begin_stream_activity / end_stream_activity and therefore
        # emit cap.call/cap.ok events. Previously invisible to syslog because
        # only cap.error was handled. Now we log them so the syslog panel
        # shows the full lifecycle of chat interactions and LLM calls.
        elif etype == "cap.call":
            cap_name = event.get("name", "")
            grp      = event.get("group", cap_name.split(".")[0] if cap_name else "")
            _TRACKED_CALL_GROUPS = {"chat", "llm", "vllm", "dag", "pipeline"}
            if grp in _TRACKED_CALL_GROUPS:
                rec = SyslogRecord(
                    level      = "INFO",
                    category   = "cap",
                    event_type = etype,
                    message    = f"{cap_name} started (group={grp})",
                    cap_name   = cap_name,
                    cap_group  = grp,
                    trace_id   = event.get("trace_id", ""),
                    trigger_id = event.get("trigger_id", ""),
                    trigger_cap= event.get("trigger_cap", ""),
                    session_id = event.get("session_id", ""),
                )
                await self.write(rec)

        elif etype == "cap.ok":
            cap_name = event.get("name", "")
            grp      = event.get("group", cap_name.split(".")[0] if cap_name else "")
            _TRACKED_OK_GROUPS = {"chat", "llm", "vllm", "dag", "pipeline"}
            if grp in _TRACKED_OK_GROUPS:
                elapsed = event.get("elapsed_ms", "")
                preview = event.get("preview", "")[:120]
                rec = SyslogRecord(
                    level      = "INFO",
                    category   = "cap",
                    event_type = etype,
                    message    = (
                        f"{cap_name} completed"
                        + (f" ({elapsed}ms)" if elapsed else "")
                        + (f" — {preview}" if preview else "")
                    ),
                    cap_name   = cap_name,
                    cap_group  = grp,
                    trace_id   = event.get("trace_id", ""),
                    session_id = event.get("session_id", ""),
                    extra      = {"elapsed_ms": elapsed, "preview": preview},
                )
                await self.write(rec)


SYSLOG = SyslogWriter()


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY WRAPPER PATCH  —  enrich cap errors with traceback + trigger chain
# ─────────────────────────────────────────────────────────────────────────────

def _patch_capability_wrapper():
    """
    Monkey-patch the @capability decorator wrapper (already installed in
    CAPABILITY_REGISTRY["func"]) to:
      1. Set the trigger chain context var before each call
      2. Capture full traceback on exception before it gets str()'d
      3. Emit to syslog with code context
      4. Also detect silent errors: if result is a dict with "error" key,
         emit a WARNING syslog entry (cap returned error without raising)
    """
    for cap_name, cap in CAPABILITY_REGISTRY.items():
        original_wrap = cap["func"]
        if getattr(original_wrap, "_syslog_patched", False):
            continue

        def _make_patched(name, orig, raw_fn):
            @functools.wraps(orig)
            async def _patched(**kw):
                tid = kw.get("trace_id") or str(uuid.uuid4())
                sid = kw.get("session_id", "") or _cv_session_id.get("")
                # Set this call as the current trigger for any nested caps
                token_id  = _cv_trigger_id.set(tid)
                token_cap = _cv_trigger_cap.set(name)
                token_sid = _cv_session_id.set(sid) if sid else None
                try:
                    result = await orig(**kw)

                    # Detect silent errors (cap returned {"error": "..."})
                    if isinstance(result, dict) and "error" in result:
                        err_msg = str(result["error"])
                        src = _resolve_cap_source(name)
                        asyncio.create_task(SYSLOG.write(SyslogRecord(
                            level       = "WARNING",
                            category    = "cap",
                            event_type  = "cap.silent_error",
                            message     = f"{name} returned error: {err_msg[:200]}",
                            detail      = err_msg,
                            cap_name    = name,
                            cap_group   = name.split(".")[0],
                            trace_id    = tid,
                            trigger_id  = _cv_trigger_id.get(""),
                            trigger_cap = _cv_trigger_cap.get(""),
                            session_id  = _cv_session_id.get(""),
                            source_file = src.get("file", ""),
                            source_func = src.get("func", ""),
                            source_code = src.get("code", ""),
                            lineno      = src.get("lineno", 0),
                            extra       = {k: str(v)[:100] for k, v in result.items() if k != "error"},
                        )))
                    return result

                except Exception as e:
                    tb = traceback.format_exc()
                    asyncio.create_task(SYSLOG.write_cap_error(
                        cap_name=name, error=str(e),
                        trace_id=tid, tb=tb,
                    ))
                    raise
                finally:
                    _cv_trigger_id.reset(token_id)
                    _cv_trigger_cap.reset(token_cap)
                    if token_sid is not None:
                        _cv_session_id.reset(token_sid)

            _patched._syslog_patched = True
            return _patched

        import functools  # noqa: already imported at top, this is a safety guard
        cap["func"] = _make_patched(cap_name, original_wrap, cap.get("raw"))

    log.info("Capability wrappers patched for syslog (silent error detection)")


# ─────────────────────────────────────────────────────────────────────────────
# LOG QUERY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _read_syslog(
    limit:     int            = 100,
    level:     Optional[str]  = None,
    category:  Optional[str]  = None,
    cap_name:  Optional[str]  = None,
    keyword:   Optional[str]  = None,
    since_id:  str            = "0",
    reverse:   bool           = True,
) -> List[dict]:
    r = _redis()
    if not r:
        return []
    try:
        if reverse:
            raw = await r.xrevrange(SYSLOG_STREAM, count=limit * 3)
        else:
            raw = await r.xrange(SYSLOG_STREAM, min=since_id, count=limit * 3)
    except Exception as e:
        log.warning("syslog read: %s", e)
        return []

    results = []
    for entry_id, data in raw:
        try:
            raw_data = data.get(b"data", b"{}")
            rec = json.loads(raw_data)
            rec["_redis_id"] = entry_id.decode() if isinstance(entry_id, bytes) else entry_id

            if level    and rec.get("level")    != level:     continue
            if category and rec.get("category") != category:  continue
            if cap_name and rec.get("cap_name") != cap_name:  continue
            if keyword:
                kw = keyword.lower()
                searchable = f"{rec.get('message','')} {rec.get('detail','')} {rec.get('cap_name','')}".lower()
                if kw not in searchable:
                    continue

            results.append(rec)
            if len(results) >= limit:
                break
        except Exception:
            continue

    return results


# ─────────────────────────────────────────────────────────────────────────────
# PROACTIVE MONITOR
# ─────────────────────────────────────────────────────────────────────────────

_monitor_task: Optional[asyncio.Task] = None
_monitor_enabled = MONITOR_ENABLED
_monitor_interval = MONITOR_INTERVAL


async def _monitor_loop():
    """
    Periodically scan recent syslog entries for error patterns.
    When errors are found, ask an LLM agent to analyse and suggest fixes.
    Publishes findings as vera:events type syslog.monitor_report.
    """
    global _monitor_enabled
    log.info("Syslog monitor started (interval=%ds)", _monitor_interval)
    while _monitor_enabled:
        await asyncio.sleep(_monitor_interval)
        if not _monitor_enabled:
            break
        try:
            await _run_monitor_check()
        except Exception as e:
            log.error("Monitor check failed: %s", e)


async def _run_monitor_check():
    """Run a single monitor check pass."""
    # Get recent errors (last monitor interval worth)
    errors = await _read_syslog(limit=20, level="ERROR")
    warnings = await _read_syslog(limit=10, level="WARNING")

    if not errors and not warnings:
        return

    # Group by cap_name for dedup
    by_cap: Dict[str, list] = {}
    for rec in errors + warnings:
        cn = rec.get("cap_name", "unknown")
        by_cap.setdefault(cn, []).append(rec)

    # Build summary for LLM
    summary_lines = []
    for cap_n, recs in list(by_cap.items())[:5]:
        latest = recs[0]
        count  = len(recs)
        summary_lines.append(
            f"- {cap_n}: {count}x {latest.get('level','?')} — {latest.get('message','')[:120]}"
        )
    summary = "\n".join(summary_lines)

    # Ask the LLM for analysis
    prompt = (
        f"You are the Vera system monitor. Recent errors have been detected:\n\n"
        f"{summary}\n\n"
        f"Provide a brief analysis (2-4 sentences) identifying the most likely root causes "
        f"and suggesting concrete remediation steps. Focus on actionable advice."
    )

    analysis = await ollama_generate(
        prompt,
        system="You are a system reliability engineer analysing Vera AI platform errors. Be concise and specific.",
        prefer_gpu=True,
    )

    report = {
        "type":        "syslog.monitor_report",
        "ts":          now_iso(),
        "error_count": len(errors),
        "warn_count":  len(warnings),
        "caps_affected": list(by_cap.keys())[:10],
        "analysis":    analysis,
        "top_errors":  [{"cap": r.get("cap_name"), "msg": r.get("message","")[:100]}
                        for r in errors[:5]],
    }

    await emit_event(report)
    log.info("Monitor check: %d errors, %d warnings across %d caps",
             len(errors), len(warnings), len(by_cap))


# ─────────────────────────────────────────────────────────────────────────────
# DAG ERROR CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

async def get_dag_error_context(cap_name: str, error_msg: str) -> str:
    """
    Called by the DAG replanner when a node fails.
    Returns a context string: recent syslog entries for this cap + code snippet.
    Used to give the LLM replanner rich context for error correction.
    """
    entries = await _read_syslog(limit=3, cap_name=cap_name, level="ERROR")
    src = _resolve_cap_source(cap_name)

    parts = [f"Error in capability '{cap_name}': {error_msg}"]

    if entries:
        latest = entries[0]
        if latest.get("detail") and latest["detail"] != error_msg:
            parts.append(f"\nFull error detail:\n{latest['detail'][:500]}")

    if src.get("code"):
        parts.append(f"\nSource code ({src.get('file','?')}):\n```python\n{src['code'][:800]}\n```")

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "syslog.query", memory="off",
    http_method="POST", http_path="/syslog/query", http_tags=["syslog"],
    description="Query the system log. Filter by level, category, cap_name, or keyword.",
)
async def syslog_query(
    limit:    int  = 100,
    level:    str  = "",    # DEBUG INFO WARNING ERROR CRITICAL
    category: str  = "",    # cap worker system agent dag proxy
    cap_name: str  = "",
    keyword:  str  = "",
    trace_id=None,
):
    entries = await _read_syslog(
        limit=limit, level=level or None, category=category or None,
        cap_name=cap_name or None, keyword=keyword or None,
    )
    return {"entries": entries, "count": len(entries)}


@capability(
    "syslog.errors", memory="off",
    http_method="GET", http_path="/syslog/errors", http_tags=["syslog"],
    description="Recent errors and warnings with source code context for LLM analysis.",
)
async def syslog_errors(
    limit:         int  = 20,
    include_code:  bool = True,
    trace_id=None,
):
    entries = await _read_syslog(limit=limit, level="ERROR")
    warnings = await _read_syslog(limit=10, level="WARNING")

    # Add source code to entries if requested
    if include_code:
        for e in entries + warnings:
            cn = e.get("cap_name", "")
            if cn and not e.get("source_code"):
                src = _resolve_cap_source(cn)
                e["source_code"] = src.get("code", "")
                e["source_file"] = src.get("file", "")

    return {
        "errors":   entries,
        "warnings": warnings,
        "total_errors": len(entries),
        "total_warnings": len(warnings),
    }


@capability(
    "syslog.ask", memory="on",
    http_method="POST", http_path="/syslog/ask", http_tags=["syslog"],
    description="Send a syslog entry + its source code + user question to the LLM for analysis. "
                "Returns diagnosis and suggested fix.",
)
async def syslog_ask(
    question:    str,
    log_id:      str  = "",   # _redis_id of a specific entry, or empty for latest error
    cap_name:    str  = "",   # filter to errors from this cap
    agent_name:  str  = "assistant",
    trace_id=None,
):
    # Fetch the relevant log entry
    if log_id:
        r = _redis()
        entries = []
        if r:
            try:
                raw = await r.xrange(SYSLOG_STREAM, min=log_id, max=log_id, count=1)
                for _, data in raw:
                    entries = [json.loads(data.get(b"data", b"{}"))]
            except Exception:
                pass
    else:
        entries = await _read_syslog(
            limit=1, level="ERROR",
            cap_name=cap_name or None,
        )

    if not entries:
        return {"error": "No matching log entry found", "answer": ""}

    entry = entries[0]
    cn    = entry.get("cap_name", cap_name)
    src   = _resolve_cap_source(cn) if cn else {}

    # Build rich context prompt
    context = f"""System log entry:
  Timestamp:  {entry.get('ts','')}
  Level:      {entry.get('level','')}
  Capability: {entry.get('cap_name','')} ({entry.get('cap_group','')})
  Message:    {entry.get('message','')}
  Detail:     {entry.get('detail','')[:600]}
  Trace ID:   {entry.get('trace_id','')}
  Trigger:    {entry.get('trigger_cap','')} → {entry.get('cap_name','')}
  File:       {entry.get('source_file', src.get('file',''))}"""

    code = entry.get("source_code") or src.get("code", "")
    if code:
        context += f"\n\nSource code:\n```python\n{code[:1200]}\n```"

    prompt = f"{context}\n\nUser question: {question}"

    system = (
        "You are a Vera platform engineer analysing system errors. "
        "Given a log entry and optionally the source code of the failing capability, "
        "provide a clear diagnosis and specific remediation steps. "
        "Be concise and technical. If you can see the source code, reference specific lines."
    )

    answer = await ollama_generate(prompt, system=system, prefer_gpu=True)

    return {
        "question":  question,
        "log_entry": entry,
        "answer":    answer,
        "cap_name":  cn,
    }


@capability(
    "syslog.monitor_start", memory="off",
    http_method="POST", http_path="/syslog/monitor/start", http_tags=["syslog"],
    description="Start the proactive error monitor. Periodically scans logs and uses LLM to suggest fixes.",
)
async def syslog_monitor_start(
    interval: int = 300,   # seconds between checks
    trace_id=None,
):
    global _monitor_task, _monitor_enabled, _monitor_interval
    _monitor_enabled  = True
    _monitor_interval = max(60, interval)

    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()

    _monitor_task = asyncio.create_task(_monitor_loop())
    return {"status": "started", "interval_s": _monitor_interval}


@capability(
    "syslog.monitor_stop", memory="off",
    http_method="POST", http_path="/syslog/monitor/stop", http_tags=["syslog"],
    description="Stop the proactive error monitor.",
)
async def syslog_monitor_stop(trace_id=None):
    global _monitor_task, _monitor_enabled
    _monitor_enabled = False
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
    return {"status": "stopped"}


@capability(
    "syslog.monitor_run", memory="on",
    http_method="POST", http_path="/syslog/monitor/run", http_tags=["syslog"],
    description="Run a single proactive monitor check immediately. Returns analysis.",
)
async def syslog_monitor_run(trace_id=None):
    await _run_monitor_check()
    # Return the most recent monitor report from events
    r = _redis()
    if r:
        try:
            recent = await r.xrevrange("vera:events", count=10)
            for _, data in recent:
                ev = json.loads(data.get(b"data", b"{}"))
                if ev.get("type") == "syslog.monitor_report":
                    return ev
        except Exception:
            pass
    return {"status": "completed", "message": "Check ran but no report found in events"}


@capability(
    "syslog.clear", memory="off",
    http_method="POST", http_path="/syslog/clear", http_tags=["syslog"],
    description="Trim the system log to the last N entries.",
)
async def syslog_clear(keep: int = 500, trace_id=None):
    r = _redis()
    if not r:
        return {"error": "Redis not available"}
    try:
        await r.xtrim(SYSLOG_STREAM, maxlen=keep, approximate=False)
        length = await r.xlen(SYSLOG_STREAM)
        return {"status": "trimmed", "remaining": length}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "syslog.status", memory="off",
    http_method="GET", http_path="/syslog/status", http_tags=["syslog"],
    description="Syslog status: record count, monitor state, recent error summary.",
)
async def syslog_status(trace_id=None):
    r = _redis()
    length = 0
    if r:
        try:
            length = await r.xlen(SYSLOG_STREAM)
        except Exception:
            pass

    recent_errors = await _read_syslog(limit=5, level="ERROR")
    return {
        "stream":          SYSLOG_STREAM,
        "record_count":    length,
        "monitor_enabled": _monitor_enabled,
        "monitor_interval_s": _monitor_interval,
        "recent_errors":   [{"cap": e.get("cap_name"), "msg": e.get("message","")[:80], "ts": e.get("ts")}
                             for e in recent_errors],
    }


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def _startup():
    _patch_capability_wrapper()
    asyncio.create_task(SYSLOG.listen_loop())
    if MONITOR_ENABLED:
        global _monitor_task
        _monitor_task = asyncio.create_task(_monitor_loop())
    log.info("vera_syslog ready — monitor=%s interval=%ds",
             "on" if MONITOR_ENABLED else "off", MONITOR_INTERVAL)

    # Emit a startup log entry
    await SYSLOG.write(SyslogRecord(
        level="INFO", category="system", event_type="system.startup",
        message=f"Vera started — {len(CAPABILITY_REGISTRY)} capabilities loaded",
    ))


schedule(_startup, interval=999999, name="syslog_startup")
try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup())
except Exception:
    pass