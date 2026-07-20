"""
monitor_capabilities.py — Vera stack system monitor
===================================================

A single pop-out "is my stack healthy?" view across the three infrastructure
subsystems Vera already drives, plus time-series graphs of internal resource
usage and the headline stack stats:

  • Proxmox  — clusters / nodes / guests           (proxmox/proxmox_capabilities.py)
  • Docker   — hosts / containers                  (workers/docker_capabilities.py)
  • Ollama   — cluster nodes / models / load       (capability_orchestration.py)

This module adds NO new connection logic — it composes the existing capabilities
through CAPABILITY_REGISTRY so it stays correct as those subsystems evolve.

Sampling / history
──────────────────
A scheduled sampler (`sysmon_sampler`, every SYSMON_SAMPLE_SEC=10s) builds the
full snapshot ONCE per interval, caches it, and appends a compact row to an
in-memory ring buffer (process CPU/RAM via psutil + the headline counts). So:
  • viewers poll `/sysmon/status` and get the cached snapshot instantly — the
    heavy proxmox/docker calls run once per interval no matter how many tabs,
  • `/sysmon/history` returns the ring buffer to draw graphs over time.
History is per-process and in-memory (survives panel reloads, not server
restarts) — no multi-writer Redis contention with other Vera instances.

Capabilities (group `sysmon.*`)
───────────────────────────────
  sysmon.status    — GET /sysmon/status    aggregate health of all three stacks
  sysmon.history   — GET /sysmon/history    ring buffer of compact samples

Panel
─────
  GET /sysmon/panel   serves system_monitor_panel.html (self-contained; can be
                      floated in-page or opened in its own browser window).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, now_iso, register_ui, schedule,
)

log = logging.getLogger("vera.monitor")
_HERE = Path(__file__).parent

# psutil is optional (already used by workers.py). Resource graphs degrade to
# "n/a" when it is missing; everything else still works.
try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:                               # pragma: no cover
    psutil = None                               # type: ignore
    _HAS_PSUTIL = False

SAMPLE_SEC = max(2.0, float(os.getenv("SYSMON_SAMPLE_SEC", "10")))
HISTORY_MAX = int(os.getenv("SYSMON_HISTORY_MAX", "720"))     # ~2h at 10s
_STALE_SEC = max(SAMPLE_SEC * 2.5, 25.0)

_HISTORY: Deque[Dict] = deque(maxlen=HISTORY_MAX)
_LAST_FULL: Dict = {}
_LAST_T: float = 0.0
_PROC = None


# ─────────────────────────────────────────────────────────────────────────────
#  Loose-coupled call into another module's capability (same pattern as the v5
#  planner's `CAPABILITY_REGISTRY.get(...)` lookups — no import-order coupling).
# ─────────────────────────────────────────────────────────────────────────────
async def _call(cap_name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"{cap_name} not loaded"}
    kw.setdefault("trace_id", "")
    try:
        return await cap["func"](**kw)
    except Exception as e:                      # never let one stack break the snapshot
        log.debug("sysmon._call %s failed: %s", cap_name, e)
        return {"error": f"{type(e).__name__}: {e}"}


def _is_error(d: Any) -> bool:
    return isinstance(d, dict) and isinstance(d.get("error"), str) and len(d) == 1


def _resources() -> Dict:
    """Cheap, non-blocking process/host resource sample (psutil, since-last-call)."""
    if not _HAS_PSUTIL:
        return {"cpu": None, "mem": None, "proc_mb": None, "proc_cpu": None,
                "ram_used_gb": None, "ram_total_gb": None}
    global _PROC
    try:
        if _PROC is None:
            _PROC = psutil.Process()
            _PROC.cpu_percent(None)             # prime (first call returns 0.0)
            psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        ncpu = psutil.cpu_count() or 1
        return {
            "cpu":          round(psutil.cpu_percent(None), 1),
            "mem":          round(vm.percent, 1),
            "ram_used_gb":  round(vm.used / 1e9, 2),
            "ram_total_gb": round(vm.total / 1e9, 2),
            "proc_mb":      round(_PROC.memory_info().rss / 1e6, 1),
            "proc_cpu":     round(_PROC.cpu_percent(None) / ncpu, 1),
        }
    except Exception as e:
        log.debug("sysmon resources: %s", e)
        return {"cpu": None, "mem": None, "proc_mb": None, "proc_cpu": None,
                "ram_used_gb": None, "ram_total_gb": None}


async def _queue_len() -> Optional[int]:
    """Task backlog for the workers group: not-yet-delivered (lag) plus
    delivered-but-unacked (pending). XLEN would count the whole stream —
    including acked history that is never deleted — so it is NOT the backlog."""
    r = _orch.REDIS
    if not r:
        return None
    try:
        for g in await r.xinfo_groups(_orch.TASK_STREAM):
            name = g.get("name")
            if isinstance(name, bytes):
                name = name.decode()
            if name == _orch.GROUP_WORKERS:
                return int(g.get("lag") or 0) + int(g.get("pending") or 0)
        # No consumer group yet → nothing consumes, whole stream is backlog
        return int(await r.xlen(_orch.TASK_STREAM))
    except Exception:
        return None


async def _worker_count() -> int:
    """Cluster-wide worker count from Redis registrations (keys carry a 120s
    TTL so dead workers age out); falls back to the in-process registry."""
    try:
        r = _orch.REDIS
        if r:
            n = len(await r.keys("vera:workers:*"))
            if n:
                return n
    except Exception:
        pass
    return len(_orch.WORKER_REGISTRY)


# ─────────────────────────────────────────────────────────────────────────────
#  Per-stack summaries
# ─────────────────────────────────────────────────────────────────────────────
async def _ollama_summary() -> Dict:
    data = await _call("ollama.instances")
    nodes: List[Dict] = []
    err = ""
    if _is_error(data):
        err = data["error"]
    elif isinstance(data, dict):
        for iid, i in data.items():
            if not isinstance(i, dict):
                continue
            nodes.append({
                "id":         iid,
                "label":      i.get("label", iid),
                "status":     i.get("status", "unknown"),
                "has_gpu":    bool(i.get("has_gpu")),
                "enabled":    i.get("enabled", True),
                "in_use":     i.get("in_use", 0),
                "latency_ms": i.get("latency_ms"),
                "models":     len(i.get("models") or []),
                "url":        i.get("url", ""),
            })
    nodes.sort(key=lambda n: n["id"])
    online = sum(1 for n in nodes if n["status"] == "online")
    return {
        "ok":     online > 0,
        "error":  err,
        "online": online,
        "total":  len(nodes),
        "gpu":    sum(1 for n in nodes if n["has_gpu"] and n["status"] == "online"),
        "in_use": sum((n["in_use"] or 0) for n in nodes),
        "nodes":  nodes,
    }


async def _docker_host_summary(h: Dict) -> Dict:
    hid = h.get("id", "")
    ps = await _call("docker.ps", host_id=hid, all=True)
    reachable = isinstance(ps, dict) and not ps.get("error")
    conts = ps.get("containers", []) if isinstance(ps, dict) else []
    running = sum(1 for c in conts
                  if str(c.get("State", "")).lower() == "running")
    return {
        "id":         hid,
        "label":      h.get("label", hid),
        "kind":       h.get("kind", ""),
        "reachable":  reachable,
        "error":      ps.get("error", "") if isinstance(ps, dict) else "no response",
        "containers": len(conts),
        "running":    running,
    }


async def _docker_summary() -> Dict:
    hl = await _call("docker.hosts.list")
    if _is_error(hl):
        return {"ok": False, "error": hl["error"], "hosts": [],
                "total_hosts": 0, "reachable": 0, "running": 0, "containers": 0}
    hosts_in = (hl or {}).get("hosts", []) if isinstance(hl, dict) else []
    hosts = list(await asyncio.gather(
        *[_docker_host_summary(h) for h in hosts_in])) if hosts_in else []
    return {
        "ok":          any(h["reachable"] for h in hosts),
        "error":       "",
        "hosts":       hosts,
        "total_hosts": len(hosts),
        "reachable":   sum(1 for h in hosts if h["reachable"]),
        "running":     sum(h["running"] for h in hosts),
        "containers":  sum(h["containers"] for h in hosts),
    }


async def _proxmox_cluster_summary(c: Dict) -> Dict:
    cid = c.get("id", "")
    st = await _call("proxmox.status", cluster_id=cid)
    if not isinstance(st, dict) or st.get("error"):
        return {"id": cid, "label": c.get("label", cid), "ok": False,
                "error": (st or {}).get("error", "unreachable"),
                "nodes": 0, "guests": 0, "running": 0, "quorate": None}
    counts = st.get("counts", {}) or {}
    cluster = st.get("cluster", {}) or {}
    return {
        "id":      cid,
        "label":   cluster.get("name") or c.get("label", cid),
        "ok":      True,
        "error":   "",
        "nodes":   counts.get("nodes", len(st.get("nodes", []))),
        "guests":  counts.get("guests", len(st.get("guests", []))),
        "running": counts.get("running", 0),
        "quorate": cluster.get("quorate"),
    }


async def _proxmox_summary() -> Dict:
    cl = await _call("proxmox.cluster.list")
    if _is_error(cl):
        return {"ok": False, "error": cl["error"], "clusters": [],
                "configured": 0, "nodes": 0, "guests": 0, "running": 0}
    clusters_in = (cl or {}).get("clusters", []) if isinstance(cl, dict) else []
    clusters = list(await asyncio.gather(
        *[_proxmox_cluster_summary(c) for c in clusters_in])) if clusters_in else []
    return {
        "ok":         any(c["ok"] for c in clusters),
        "error":      "",
        "clusters":   clusters,
        "configured": len(clusters),
        "nodes":      sum(c["nodes"] for c in clusters),
        "guests":     sum(c["guests"] for c in clusters),
        "running":    sum(c["running"] for c in clusters),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Full snapshot + sampler
# ─────────────────────────────────────────────────────────────────────────────
async def _build_full() -> Dict:
    proxmox, docker, ollama = await asyncio.gather(
        _proxmox_summary(), _docker_summary(), _ollama_summary())
    return {"ts": now_iso(), "t": time.time(), "resources": _resources(),
            "proxmox": proxmox, "docker": docker, "ollama": ollama}


def _compact(full: Dict, qlen: Optional[int], workers: int) -> Dict:
    p, d, o = full["proxmox"], full["docker"], full["ollama"]
    res = full.get("resources", {})
    return {
        "t":            round(full.get("t") or time.time(), 1),
        "cpu":          res.get("cpu"),
        "mem":          res.get("mem"),
        "proc_mb":      res.get("proc_mb"),
        "proc_cpu":     res.get("proc_cpu"),
        "pmx_running":  p.get("running", 0),
        "pmx_guests":   p.get("guests", 0),
        "pmx_nodes":    p.get("nodes", 0),
        "dkr_running":  d.get("running", 0),
        "dkr_containers": d.get("containers", 0),
        "dkr_hosts":    d.get("reachable", 0),
        "oll_online":   o.get("online", 0),
        "oll_total":    o.get("total", 0),
        "oll_inuse":    o.get("in_use", 0),
        "caps":         len(CAPABILITY_REGISTRY),
        "workers":      workers,
        "queue":        qlen if qlen is not None else 0,
    }


async def _record(full: Dict) -> None:
    global _LAST_FULL, _LAST_T
    _LAST_FULL = full
    _LAST_T = full["t"]
    _HISTORY.append(_compact(full, await _queue_len(), await _worker_count()))


async def _sample_once():
    """Scheduled every SAMPLE_SEC — the single writer of the cache + history."""
    try:
        await _record(await _build_full())
    except Exception as e:
        log.debug("sysmon sample: %s", e)


schedule(_sample_once, SAMPLE_SEC, "sysmon_sampler")


# ─────────────────────────────────────────────────────────────────────────────
#  Capabilities
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "sysmon.status",
    http_method="GET", http_path="/sysmon/status", http_tags=["monitor"],
    memory="off", silent=True,
    description="One-call health snapshot of the local infra stack — Proxmox "
                "clusters, Docker hosts and the Ollama cluster — plus process "
                "CPU/RAM. Served from the sampler cache (the heavy proxmox/docker "
                "calls run once per SYSMON_SAMPLE_SEC, not per request); pass "
                "fresh=true to force a live rebuild. Output: {ts, t, cached, age_s, "
                "resources:{cpu,mem,proc_mb,...}, proxmox:{...}, docker:{...}, "
                "ollama:{...}}.",
)
async def cap_sysmon_status(fresh: bool = False, trace_id=None) -> Dict:
    age = (time.time() - _LAST_T) if _LAST_T else 1e9
    if fresh or not _LAST_FULL or age > _STALE_SEC:
        full = await _build_full()
        try:
            await _record(full)
        except Exception:
            pass
        return {**full, "cached": False, "age_s": 0.0}
    return {**_LAST_FULL, "cached": True, "age_s": round(age, 1)}


@capability(
    "sysmon.history",
    http_method="GET", http_path="/sysmon/history", http_tags=["monitor"],
    memory="off", silent=True,
    description="Time-series ring buffer behind the monitor graphs. Each sample: "
                "{t, cpu, mem, proc_mb, proc_cpu, pmx_running, pmx_guests, "
                "dkr_running, dkr_containers, oll_online, oll_inuse, queue, "
                "workers, caps}. Input: limit (int, default 240). Output: "
                "{samples:[...], count, interval_s, psutil}.",
)
async def cap_sysmon_history(limit: int = 240, trace_id=None) -> Dict:
    items = list(_HISTORY)
    if limit and limit < len(items):
        items = items[-limit:]
    return {"samples": items, "count": len(items),
            "interval_s": SAMPLE_SEC, "psutil": _HAS_PSUTIL}


# ─────────────────────────────────────────────────────────────────────────────
#  PANEL
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/sysmon/panel", include_in_schema=False)
async def _sysmon_panel():
    p = _HERE / "system_monitor_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>system_monitor_panel.html not found</p>")


register_ui(
    "system-monitor",
    "Monitor",
    "▦",
    """<div id="sysmon-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/sysmon/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#181614)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["sysmon.status", "sysmon.history", "proxmox.status",
             "proxmox.cluster.list", "docker.ps", "docker.hosts.list",
             "ollama.instances"],
    # Injectable dashboard widget (appears in the main dashboard's "+ Add Widget"
    # picker) rather than a top-level tab. The dashboard provides a generic
    # pop-out (float on the page / open in a new window) for every widget, so the
    # monitor no longer needs its own tab to be detachable.
    mode="inject",
    tab_order=57,
)

log.info("monitor_capabilities ready — sysmon sampler@%ss, psutil=%s",
         SAMPLE_SEC, _HAS_PSUTIL)
