"""
Vera Performance & Diagnostics monitor.

Three things live here:

  1. LOG CAPTURE — tees the whole application log (uvicorn, httpx, vera.*) to a
     rotating file readable from the UI, via a QueueHandler → background
     QueueListener so the file IO never runs on the event loop. A ring buffer
     keeps the most recent lines for instant tailing without touching disk.

  2. STALL FEED — surfaces the event-loop stall/hang records captured by the
     orchestrator's watchdog (_orch.PERF_EVENTS): duration + the exact blocking
     call's stack. This is what turned "the WS keeps flapping" into "here's the
     line that blocked the loop".

  3. SCAN + REMEDIATE — a battery of health/performance checks that raise
     findings with a severity and a concrete remediation; a gated perf.remediate
     applies the safe ones (prune stale Redis consumers, fail-mark zombie jobs).

  Plus perf.note — a channel for an operator (or the assistant) to push a line
  of diagnostic output into the log + UI feed so it's visible in Vera.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import queue as _queue
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, now_iso, register_ui,
)

log = logging.getLogger("vera.perf")
_HERE = Path(__file__).parent

# ── Log capture ───────────────────────────────────────────────────────────────
LOG_DIR       = Path(os.getenv("VERA_LOG_DIR",
                               str(Path(__file__).resolve().parents[2] / "logs")))
LOG_FILE      = LOG_DIR / "vera.log"
LOG_MAX_BYTES = int(os.getenv("VERA_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUPS   = int(os.getenv("VERA_LOG_BACKUPS", "5"))
_RING: "deque[str]" = deque(maxlen=int(os.getenv("VERA_LOG_RING", "3000")))
_listener: Optional[logging.handlers.QueueListener] = None


class _RingHandler(logging.Handler):
    """Keeps the most recent formatted log lines in memory for instant tail."""
    def emit(self, record):
        try:
            _RING.append(self.format(record))
        except Exception:
            pass


def _install_log_capture() -> None:
    global _listener
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("perf: cannot create log dir %s: %s", LOG_DIR, e)
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    try:
        fileh = logging.handlers.RotatingFileHandler(
            str(LOG_FILE), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8")
        fileh.setFormatter(fmt)
    except Exception as e:
        log.warning("perf: file handler failed: %s", e)
        return
    ringh = _RingHandler()
    ringh.setFormatter(fmt)
    # QueueHandler is cheap + non-blocking (just enqueues); the listener thread
    # does the actual file/ring writes, so logging never adds event-loop latency.
    q: "_queue.SimpleQueue" = _queue.SimpleQueue()
    qh = logging.handlers.QueueHandler(q)
    _listener = logging.handlers.QueueListener(q, fileh, ringh,
                                               respect_handler_level=True)
    _listener.start()
    logging.getLogger().addHandler(qh)
    log.info("perf: log capture active → %s (rotating %d×%dMB, ring=%d lines)",
             LOG_FILE, LOG_BACKUPS, LOG_MAX_BYTES // (1024 * 1024), _RING.maxlen)


_install_log_capture()


# ── helpers ───────────────────────────────────────────────────────────────────
async def _call(name: str, **kw):
    """Invoke another capability by name; return its result or {'error':...}."""
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"cap not loaded: {name}"}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"cap has no func: {name}"}
    try:
        return await fn(**kw, trace_id=None)
    except Exception as e:
        return {"error": str(e)}


def _finding(fid, severity, area, title, detail, *,
             remediation="", remediable=False, remediation_id="", metric=None):
    return {"id": fid, "severity": severity, "area": area, "title": title,
            "detail": detail, "remediation": remediation,
            "remediable": bool(remediable), "remediation_id": remediation_id,
            "metric": metric}


# ── perf.stalls ───────────────────────────────────────────────────────────────
@capability(
    "perf.stalls",
    http_method="GET", http_path="/perf/stalls", http_tags=["monitor", "perf"],
    memory="off", silent=True,
    description="Event-loop stall/hang events captured by the watchdog. Each: "
                "{kind:'stall'|'hang'|'note', ts, stalled_ms, where, stack}. "
                "'hang' rows carry the exact blocking call's stack. "
                "Input: limit (int, default 100). Output: {events:[...], count}.",
)
async def perf_stalls(limit: int = 100, trace_id=None) -> Dict:
    evs = list(getattr(_orch, "PERF_EVENTS", []))
    evs = evs[-limit:] if (limit and limit < len(evs)) else evs
    evs = list(reversed(evs))   # newest first
    n_hang = sum(1 for e in evs if e.get("kind") == "hang")
    n_stall = sum(1 for e in evs if e.get("kind") == "stall")
    worst = max((int(e.get("stalled_ms") or 0) for e in evs), default=0)
    return {"events": evs, "count": len(evs),
            "hangs": n_hang, "stalls": n_stall, "worst_ms": worst}


# ── perf.log.tail / perf.log.files ────────────────────────────────────────────
@capability(
    "perf.log.tail",
    http_method="GET", http_path="/perf/log/tail", http_tags=["monitor", "perf"],
    memory="off", silent=True,
    description="Tail the captured application log. Serves from the in-memory "
                "ring (fast, no disk) unless a file= is given. "
                "Input: lines (int, default 200), grep (str, substring filter), "
                "file (str, a filename from perf.log.files to read from disk). "
                "Output: {lines:[...], count, source}.",
)
async def perf_log_tail(lines: int = 200, grep: str = "", file: str = "",
                        trace_id=None) -> Dict:
    lines = max(1, min(int(lines or 200), 5000))
    if file:
        # Read from a specific rotated file on disk (offloaded — file IO).
        target = (LOG_DIR / os.path.basename(file))
        def _read():
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    return f.read().splitlines()[-lines:]
            except Exception as e:
                return [f"[cannot read {target}: {e}]"]
        buf = await asyncio.get_event_loop().run_in_executor(None, _read)
        source = str(target)
    else:
        buf = list(_RING)[-lines:]
        source = "ring"
    if grep:
        g = grep.lower()
        buf = [ln for ln in buf if g in ln.lower()]
    return {"lines": buf, "count": len(buf), "source": source}


@capability(
    "perf.log.files",
    http_method="GET", http_path="/perf/log/files", http_tags=["monitor", "perf"],
    memory="off", silent=True,
    description="List captured log files on disk (the rotating vera.log set). "
                "Output: {dir, files:[{name, bytes, modified}]}.",
)
async def perf_log_files(trace_id=None) -> Dict:
    def _list():
        out = []
        try:
            for p in sorted(LOG_DIR.glob("vera.log*")):
                try:
                    st = p.stat()
                    out.append({"name": p.name, "bytes": st.st_size,
                                "modified": time.strftime(
                                    "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
                except Exception:
                    pass
        except Exception:
            pass
        return out
    files = await asyncio.get_event_loop().run_in_executor(None, _list)
    return {"dir": str(LOG_DIR), "files": files}


# ── perf.note ─────────────────────────────────────────────────────────────────
@capability(
    "perf.note",
    http_method="POST", http_path="/perf/note", http_tags=["monitor", "perf"],
    memory="off",
    description="Push a diagnostic line into the captured log + the Perf feed so "
                "it's visible in the UI. For surfacing test/probe output. "
                "Input: message (str!), level (str, info|warn|error). Output: {ok}.",
)
async def perf_note(message: str = "", level: str = "info", trace_id=None) -> Dict:
    if not message:
        return {"ok": False, "error": "message required"}
    lvl = {"info": logging.INFO, "warn": logging.WARNING,
           "warning": logging.WARNING, "error": logging.ERROR}.get(
        (level or "info").lower(), logging.INFO)
    log.log(lvl, "NOTE: %s", message)
    try:
        _orch.record_perf_event("note", message=str(message)[:2000], level=level)
    except Exception:
        pass
    return {"ok": True}


# ── perf.scan ─────────────────────────────────────────────────────────────────
async def _check_loop_stalls() -> List[Dict]:
    evs = list(getattr(_orch, "PERF_EVENTS", []))
    now = time.monotonic()
    recent = [e for e in evs if e.get("kind") in ("stall", "hang")
              and (now - float(e.get("mono") or 0)) < 900]  # last 15 min
    if not recent:
        return [_finding("loop", "ok", "Event loop",
                         "No loop stalls in the last 15 minutes", "")]
    hangs = [e for e in recent if e.get("kind") == "hang"]
    worst = max((int(e.get("stalled_ms") or 0) for e in recent), default=0)
    wheres = {}
    for e in hangs:
        w = e.get("where") or "?"
        wheres[w] = wheres.get(w, 0) + 1
    top = ", ".join(f"{w} ×{n}" for w, n in
                    sorted(wheres.items(), key=lambda kv: -kv[1])[:4])
    sev = "crit" if worst >= 3000 else "warn"
    detail = (f"{len(recent)} stall(s) in 15 min, worst {worst}ms."
              + (f" Blocking call(s): {top}" if top else
                 " (no stack — raise VERA_LOOP_HANG_DUMP_S coverage)"))
    return [_finding("loop", sev, "Event loop",
                     f"{len(recent)} event-loop stall(s) — WS flapping risk",
                     detail,
                     remediation=("A synchronous call is blocking the loop at the "
                                  "location above. Offload it to a thread "
                                  "(run_in_executor) or make it async. See the "
                                  "Stalls tab for the full stack."),
                     remediable=False, metric=worst)]


async def _check_consumers() -> List[Dict]:
    r = _orch.REDIS
    if not r:
        return []
    try:
        ginfo = await r.xinfo_groups("vera:tasks")
        n = 0
        for g in (ginfo or []):
            gn = g.get("name", b"")
            gn = gn.decode() if isinstance(gn, bytes) else gn
            if gn == "workers":
                n = int(g.get("consumers", 0) or 0)
                break
    except Exception:
        return []
    if n <= 200:
        return [_finding("consumers", "ok", "Redis stream",
                         f"{n} stream consumer(s) — healthy", "", metric=n)]
    return [_finding("consumers", "warn", "Redis stream",
                     f"{n} stale stream consumers accumulating",
                     "Dead consumers from prior process lives inflate "
                     "xinfo_consumers, whose synchronous parse can stall the loop.",
                     remediation="Prune consumers idle >10min with no pending.",
                     remediable=True, remediation_id="prune_consumers", metric=n)]


async def _check_zombie_jobs() -> List[Dict]:
    stats = await _call("jobs.stats")
    if not isinstance(stats, dict) or stats.get("error"):
        return []
    running = int((stats.get("stats") or {}).get("running_tracked",
                  stats.get("running_tracked", 0)) or 0)
    # Look at the ollama log for stuck 'running' entries.
    olog = await _call("jobs.ollama_log", limit=500)
    stuck = 0
    if isinstance(olog, dict):
        for e in (olog.get("requests") or olog.get("entries") or []):
            if isinstance(e, dict) and e.get("status") == "running":
                ts = float(e.get("ts") or 0)
                if ts and (time.time() - ts) > 1800:  # >30 min
                    stuck += 1
    if stuck == 0:
        return [_finding("zombies", "ok", "Jobs",
                         "No zombie 'running' ollama jobs", "")]
    return [_finding("zombies", "warn", "Jobs",
                     f"{stuck} ollama job(s) stuck 'running' >30min",
                     "Generations that lost their terminal event (restart / lost "
                     "WS) never clear and show as stale.",
                     remediation="Fail-mark stuck 'running' ollama jobs older "
                                 "than 2× the generation timeout.",
                     remediable=True, remediation_id="sweep_zombies", metric=stuck)]


async def _check_ollama() -> List[Dict]:
    inst = await _call("ollama.instances")
    nodes = []
    if isinstance(inst, dict):
        nodes = inst.get("instances") or inst.get("nodes") or []
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
    if not nodes:
        return []
    out = []
    offline = [n for n in nodes if str(n.get("status")) not in ("online",)]
    errored = [n for n in nodes if int(n.get("errors", 0) or 0) >= 3]
    online = [n for n in nodes if str(n.get("status")) == "online"]
    if offline:
        out.append(_finding(
            "ollama_offline", "warn", "Ollama",
            f"{len(offline)}/{len(nodes)} Ollama node(s) offline",
            "Offline: " + ", ".join(str(n.get("id") or n.get("label") or "?")
                                    for n in offline),
            remediation="Check the node's Ollama service; the health loop "
                        "re-probes every 20s and auto-recovers on success.",
            remediable=False, metric=len(offline)))
    if online and all(int(n.get("in_use", 0) or 0) > 0 for n in online):
        out.append(_finding(
            "ollama_saturated", "info", "Ollama",
            "Every online Ollama node is busy — no spare capacity",
            "Interactive requests will queue behind current generations.",
            remediation="Add a node, raise OLLAMA_NUM_PARALLEL on the GPU node, "
                        "or route background jobs (dream/loops) off the hot pool.",
            remediable=False))
    if not out:
        out.append(_finding("ollama", "ok", "Ollama",
                            f"{len(online)}/{len(nodes)} node(s) online, no errors", ""))
    return out


async def _check_resources() -> List[Dict]:
    s = await _call("sysmon.status")
    if not isinstance(s, dict) or s.get("error"):
        return []
    res = s.get("resources") or {}
    cpu = res.get("cpu")
    mem = res.get("mem")
    out = []
    try:
        if cpu is not None and float(cpu) >= 90:
            out.append(_finding("cpu", "warn", "Host", f"CPU {float(cpu):.0f}%",
                                "Sustained high CPU slows every request and can "
                                "cause loop stalls.", metric=float(cpu)))
        if mem is not None and float(mem) >= 90:
            out.append(_finding("mem", "warn", "Host", f"Memory {float(mem):.0f}%",
                                "High memory pressure risks OOM / swapping.",
                                metric=float(mem)))
    except Exception:
        pass
    if not out:
        out.append(_finding("resources", "ok", "Host",
                            f"CPU {cpu}% · Mem {mem}% — nominal", ""))
    return out


@capability(
    "perf.scan",
    http_method="GET", http_path="/perf/scan", http_tags=["monitor", "perf"],
    memory="off", silent=True,
    description="Run the performance/health checks and RAISE findings with "
                "severity + concrete remediation. Covers: event-loop stalls, "
                "stale Redis consumers, zombie ollama jobs, Ollama node health / "
                "saturation, host CPU/RAM. Some findings are auto-remediable via "
                "perf.remediate. Output: {findings:[...], summary:{crit,warn,info,ok}}.",
)
async def perf_scan(trace_id=None) -> Dict:
    findings: List[Dict] = []
    for check in (_check_loop_stalls, _check_consumers, _check_zombie_jobs,
                  _check_ollama, _check_resources):
        try:
            findings += await check()
        except Exception as e:
            log.debug("perf.scan check %s: %s", getattr(check, "__name__", "?"), e)
    order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
    findings.sort(key=lambda f: order.get(f.get("severity"), 4))
    summary = {k: sum(1 for f in findings if f.get("severity") == k)
               for k in ("crit", "warn", "info", "ok")}
    return {"findings": findings, "summary": summary, "ts": now_iso()}


# ── perf.remediate ────────────────────────────────────────────────────────────
async def _remediate_prune_consumers() -> Dict:
    try:
        import importlib
        jp = importlib.import_module("Vera.vera.workers.job_persistance")
        await jp._prune_stale_consumers()
        return {"ok": True, "detail": "pruned stale stream consumers"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _remediate_sweep_zombies() -> Dict:
    try:
        import importlib
        jp = importlib.import_module("Vera.vera.workers.job_persistance")
        await jp._sweep_stuck_running()
        return {"ok": True, "detail": "fail-marked stuck 'running' jobs"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_REMEDIATIONS = {
    "prune_consumers": _remediate_prune_consumers,
    "sweep_zombies":   _remediate_sweep_zombies,
}


@capability(
    "perf.remediate",
    http_method="POST", http_path="/perf/remediate", http_tags=["monitor", "perf"],
    memory="off",
    description="Apply a remediation raised by perf.scan. Only the safe, "
                "self-contained fixes are exposed here. "
                "Input: remediation_id (str! — 'prune_consumers' | 'sweep_zombies'). "
                "Output: {ok, detail|error}.",
)
async def perf_remediate(remediation_id: str = "", trace_id=None) -> Dict:
    fn = _REMEDIATIONS.get((remediation_id or "").strip())
    if not fn:
        return {"ok": False, "error": f"unknown remediation: {remediation_id}",
                "available": list(_REMEDIATIONS.keys())}
    log.info("perf.remediate: applying %s", remediation_id)
    res = await fn()
    try:
        _orch.record_perf_event("note",
                                message=f"remediation '{remediation_id}': "
                                        f"{res.get('detail') or res.get('error')}",
                                level="info")
    except Exception:
        pass
    return res


# ── Panel ─────────────────────────────────────────────────────────────────────
@APP.get("/perf/panel", include_in_schema=False)
async def _perf_panel():
    p = _HERE / "perf_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>perf_panel.html not found</p>")


register_ui(
    "perf-monitor",
    "Perf",
    "⚡",
    """<div id="perf-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/perf/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#181614)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["perf.stalls", "perf.log.tail", "perf.log.files", "perf.note",
             "perf.scan", "perf.remediate"],
    mode="inject",
    tab_order=58,
)

log.info("perf_capabilities ready — log capture + stall feed + scan/remediate")
