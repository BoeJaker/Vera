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
    """Cheap, non-blocking process/host resource sample (psutil, since-last-call).
    proc_mb/proc_cpu are specifically THIS Vera process (RSS = Resident Set
    Size, its actual physical-memory footprint) — see _top_processes() below
    for host-wide visibility beyond just this one process."""
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


_PROC_CACHE: Dict[int, "psutil.Process"] = {}   # pid -> psutil.Process, reused across
                                                 # ticks so cpu_percent() gets a real delta
# _top_processes() now runs off-thread (see _build_full) so a scheduled sample
# tick and a concurrent fresh=true request can genuinely overlap in real OS
# threads — this lock keeps _PROC_CACHE mutations serialized instead of racing.
_TOP_PROC_LOCK = asyncio.Lock()


async def _top_processes_threaded(n: int = 8) -> List[Dict]:
    async with _TOP_PROC_LOCK:
        return await asyncio.to_thread(_top_processes, n)


def _top_processes(n: int = 8) -> List[Dict]:
    """Host-wide top-N processes by RSS memory (not just this Vera process —
    see _resources() above for that). CPU%% needs the SAME Process object
    reused across ticks to get a real delta (a fresh wrapper's cpu_percent()
    always returns 0.0 on its first call, same psutil gotcha _resources()
    already works around for the single-process case above); this keeps a
    small persistent cache keyed by pid, evicting entries for pids that have
    exited since the last sampler tick."""
    if not _HAS_PSUTIL:
        return []
    try:
        seen_pids = set()
        rows = []
        for p in psutil.process_iter(['pid', 'name', 'username']):
            pid = p.info.get('pid')
            if pid is None:
                continue
            seen_pids.add(pid)
            proc = _PROC_CACHE.get(pid)
            if proc is None:
                proc = p
                try:
                    proc.cpu_percent(None)   # prime
                except Exception:
                    continue
                _PROC_CACHE[pid] = proc
            try:
                mem_mb = round(proc.memory_info().rss / 1e6, 1)
                cpu_pct = round(proc.cpu_percent(None), 1)
                rows.append({
                    "pid": pid, "name": p.info.get('name') or '?',
                    "user": (p.info.get('username') or '')[:14],
                    "mem_mb": mem_mb, "cpu_pct": cpu_pct,
                })
            except Exception:
                continue
        for pid in list(_PROC_CACHE.keys()):
            if pid not in seen_pids:
                del _PROC_CACHE[pid]
        rows.sort(key=lambda r: r["mem_mb"], reverse=True)
        return rows[:n]
    except Exception as e:
        log.debug("sysmon top_processes: %s", e)
        return []


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
    # .list.effective, not .list: this box has a real duplicate registration
    # (the local socket registered twice, once as kind=local and once as a
    # mis-saved kind=tcp pointing at the same socket) — .list is the raw,
    # undeduped registry (kept that way so the host management UI can still
    # show/delete the duplicate); .list.effective collapses same-engine
    # entries so every container on it isn't counted twice here.
    hl = await _call("docker.hosts.list.effective")
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
                "nodes": 0, "guests": 0, "running": 0, "quorate": None,
                "mem_used_gb": 0.0, "mem_total_gb": 0.0}
    counts = st.get("counts", {}) or {}
    cluster = st.get("cluster", {}) or {}
    # Cluster-wide RAM — summed from each PVE HOST node's own mem/maxmem (not
    # guests: a guest's "mem" is its allocation footprint on the host, double-
    # counting it on top of the host's own mem would inflate the total).
    nodes = st.get("nodes", []) or []
    mem_used = sum(n.get("mem", 0) for n in nodes)
    mem_total = sum(n.get("maxmem", 0) for n in nodes)
    # Per-guest breakdown — same "busiest first" idea as docker.stats.top's
    # container list, so the Proxmox dash tile can show real per-VM/CT rows
    # instead of just cluster-wide counts. No history here (unlike Docker's
    # deque-backed trend): each sample is a snapshot of proxmox.status, which
    # runs on the sysmon sampler cadence, not its own tracked-over-time cache
    # — showing a fabricated trend line off a single point would be worse
    # than showing none.
    guests_raw = st.get("guests", []) or []
    top_guests = sorted((
        {"vmid": g.get("vmid"), "name": g.get("name") or f"#{g.get('vmid')}",
         "type": g.get("type"), "node": g.get("node", ""), "status": g.get("status", ""),
         "cpu_pct": round((g.get("cpu") or 0) * 100, 1) if g.get("status") == "running" else None,
         "mem_mb": round(g.get("mem", 0) / 1e6, 1) if g.get("mem") else None,
         "mem_pct": round(100 * g["mem"] / g["maxmem"], 1) if g.get("mem") and g.get("maxmem") else None}
        for g in guests_raw if not g.get("template")
    ), key=lambda g: (g["cpu_pct"] is None, -(g["cpu_pct"] or 0)))[:12]
    return {
        "id":      cid,
        "label":   cluster.get("name") or c.get("label", cid),
        "ok":      True,
        "error":   "",
        "nodes":   counts.get("nodes", len(nodes)),
        "guests":  counts.get("guests", len(st.get("guests", []))),
        "running": counts.get("running", 0),
        "quorate": cluster.get("quorate"),
        "mem_used_gb":  round(mem_used / 1e9, 2),
        "mem_total_gb": round(mem_total / 1e9, 2),
        "top_guests": top_guests,
        # Hostnames of this cluster's PVE hosts (not guests) — used to match
        # against obs.node_temps' host labels, since the temp probe has no
        # direct Proxmox-cluster-membership field of its own to key on.
        "host_names": [n.get("node", "") for n in nodes if n.get("node")],
    }


async def _proxmox_summary() -> Dict:
    cl = await _call("proxmox.cluster.list")
    if _is_error(cl):
        return {"ok": False, "error": cl["error"], "clusters": [],
                "configured": 0, "nodes": 0, "guests": 0, "running": 0, "mem_pct": None}
    clusters_in = (cl or {}).get("clusters", []) if isinstance(cl, dict) else []
    clusters = list(await asyncio.gather(
        *[_proxmox_cluster_summary(c) for c in clusters_in])) if clusters_in else []
    mem_used = sum(c.get("mem_used_gb", 0) for c in clusters)
    mem_total = sum(c.get("mem_total_gb", 0) for c in clusters)
    top_guests = sorted(
        (g for c in clusters for g in c.get("top_guests", [])),
        key=lambda g: (g["cpu_pct"] is None, -(g["cpu_pct"] or 0)))[:12]
    host_names = [n for c in clusters for n in c.get("host_names", [])]
    return {
        "ok":         any(c["ok"] for c in clusters),
        "error":      "",
        "clusters":   clusters,
        "configured": len(clusters),
        "nodes":      sum(c["nodes"] for c in clusters),
        "guests":     sum(c["guests"] for c in clusters),
        "running":    sum(c["running"] for c in clusters),
        "mem_used_gb":  round(mem_used, 2),
        "mem_total_gb": round(mem_total, 2),
        "mem_pct":    round(100 * mem_used / mem_total, 1) if mem_total else None,
        "top_guests": top_guests,
        "host_names": host_names,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Full snapshot + sampler
# ─────────────────────────────────────────────────────────────────────────────
async def _build_full() -> Dict:
    # _top_processes() walks every host process via psutil.process_iter, which
    # is a synchronous /proc scan — run it off-thread so a host with hundreds
    # of processes (this box, with dozens of docker containers) can't stall
    # the single event loop for 1s+ every sample tick (confirmed live via
    # perf.stalls as the direct cause of WS ping/pong timeouts — see doc 37).
    proxmox, docker, ollama, temps, nodes_list, top_processes = await asyncio.gather(
        _proxmox_summary(), _docker_summary(), _ollama_summary(),
        _call("obs.node_temps"), _call("nodes.list"), _top_processes_threaded())
    # Proxmox-specific peak temp: a PVE host's obs.node_temps entry is keyed
    # by ssh_host_id, and a guest/cluster's own "node" field is the PVE-
    # internal hostname (e.g. "corp") — NEITHER of those is the SSH-registry
    # *label* (e.g. "PVE01"), so the only real cross-reference is each
    # unified-estate machine's own proxmox.node field, which is nodes.list's
    # job to know. Without this hop, host_names (PVE hostnames) would get
    # matched straight against temp host labels and silently match nothing.
    machines = (nodes_list or {}).get("nodes") or []
    pve_node_to_host_id = {m["proxmox"]["node"]: m.get("ssh_host_id")
                            for m in machines if m.get("proxmox") and m["proxmox"].get("node")}
    pmx_host_ids = {pve_node_to_host_id[n] for n in (proxmox.get("host_names") or [])
                     if n in pve_node_to_host_id}
    temp_hosts_all = (temps or {}).get("hosts") or []
    pmx_temp_vals = [h.get("max_c") for h in temp_hosts_all
                       if h.get("max_c") is not None and h.get("host_id") in pmx_host_ids]
    return {"ts": now_iso(), "t": time.time(), "resources": _resources(),
            "top_processes": top_processes,
            "proxmox": proxmox, "docker": docker, "ollama": ollama, "temps": temps,
            "pmx_temp_max": max(pmx_temp_vals) if pmx_temp_vals else None}


def _compact(full: Dict, qlen: Optional[int], workers: int) -> Dict:
    p, d, o = full["proxmox"], full["docker"], full["ollama"]
    res = full.get("resources", {})
    temp_hosts = (full.get("temps") or {}).get("hosts") or []
    temp_vals = [h.get("max_c") for h in temp_hosts if h.get("max_c") is not None]
    return {
        "t":            round(full.get("t") or time.time(), 1),
        "cpu":          res.get("cpu"),
        "mem":          res.get("mem"),
        "proc_mb":      res.get("proc_mb"),
        "proc_cpu":     res.get("proc_cpu"),
        "pmx_running":  p.get("running", 0),
        "pmx_guests":   p.get("guests", 0),
        "pmx_nodes":    p.get("nodes", 0),
        "pmx_mem_pct":  p.get("mem_pct"),
        "dkr_running":  d.get("running", 0),
        "dkr_containers": d.get("containers", 0),
        "dkr_hosts":    d.get("reachable", 0),
        "oll_online":   o.get("online", 0),
        "oll_total":    o.get("total", 0),
        "oll_inuse":    o.get("in_use", 0),
        "temp_max":     max(temp_vals) if temp_vals else None,
        "pmx_temp_max": full.get("pmx_temp_max"),
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
                "clusters, Docker hosts, the Ollama cluster, and per-host core "
                "temperatures — plus this Vera process's own CPU/RAM AND the "
                "host's top-8 processes by memory (top_processes). Served "
                "from the sampler cache (the heavy proxmox/docker calls run "
                "once per SYSMON_SAMPLE_SEC, not per request); pass fresh=true "
                "to force a live rebuild. Output: {ts, t, cached, age_s, "
                "resources:{cpu,mem,proc_mb,...}, top_processes:[{pid,name,"
                "user,mem_mb,cpu_pct}], proxmox:{...,top_guests:[{vmid,name,"
                "type,node,status,cpu_pct,mem_mb,mem_pct}]}, docker:{...}, "
                "ollama:{...}, temps:{hosts:[...]}}.",
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
                "pmx_mem_pct, dkr_running, dkr_containers, oll_online, oll_inuse, "
                "temp_max, pmx_temp_max, queue, workers, caps}. Input: limit (int, default 240). "
                "Output: {samples:[...], count, interval_s, psutil}.",
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
