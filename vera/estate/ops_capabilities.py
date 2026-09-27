"""ops.* -- Live operations: the whole estate as planes of nodes, the requests
in flight running the pipes between them (Estate > Live ops).

`ops.snapshot` gathers the readers the page needs - each through its own
capability function with its own timeout, so one slow reader (estate.health
takes seconds) delays nothing else and a failed one is named rather than
waited for - and hands the answers to ops_core.build(). The answer is cached
for a few seconds: the page polls, and several pages may poll at once.

`GET /ops/panel` serves the page. `ops.node.events` filters the event ring
to one node for its log.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability
from Vera.vera.estate import ops_core as core

log = logging.getLogger("vera.ops")

# reader -> timeout (s). The slow ones get longer, but nothing waits on them.
READERS: Dict[str, float] = {
    "estate.machines": 12.0, "sysmon.status": 6.0, "sysmon.history": 6.0, "obs.health": 6.0,
    "obs.workers": 6.0, "obs.scheduler": 6.0, "obs.events": 6.0, "obs.node_temps": 8.0,
    "ollama.instances": 8.0, "ollama.gate.status": 6.0, "ollama.request_log": 6.0,
    "docker.stack.status": 8.0, "fabric.health": 6.0, "dream.scheduler.status": 6.0,
    "background.status": 6.0, "evolve.sandbox.list": 8.0, "evolve.pipeline.list": 8.0,
    "loops.program.list": 6.0, "mesh.nodes": 6.0, "estate.health": 20.0,
    "evolve.errors.list": 8.0, "bench.node_perf.history": 6.0, "estate.registration": 12.0,
    "activity.sessions": 6.0, "ide.remote.instances": 6.0, "vfs.peer.list": 8.0,
}
CACHE_S = 5.0
_cache: Dict[str, Any] = {"at": 0.0, "out": None, "inflight": None}


_timing: Dict[str, int] = {}


async def _read(name: str, timeout: float, **kwargs: Any) -> Any:
    fn = (_orch.CAPABILITY_REGISTRY.get(name) or {}).get("func")
    if fn is None:
        return {"error": f"{name} is not loaded"}
    t0 = time.monotonic()
    try:
        return await asyncio.wait_for(fn(**kwargs), timeout)
    except asyncio.TimeoutError:
        return {"error": f"{name} took longer than {timeout:.0f} s"}
    except Exception as e:  # a reader that raises is a reader that failed
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        _timing[name] = int((time.monotonic() - t0) * 1000)


def _own_ips() -> List[str]:
    out = []
    try:
        host = socket.gethostname()
        out.append(socket.gethostbyname(host))
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if ip not in out and not str(ip).startswith("127."):
                out.append(ip)
    except Exception:
        pass
    return out


def _connections() -> Dict[str, Any]:
    """The peers holding TCP connections to Vera's own port right now - the process's own view, nothing is scanned."""
    import os as _os
    try:
        import psutil
    except Exception as e:  # noqa: BLE001
        return {"error": f"psutil is not available: {e}"}
    port = int(_os.getenv("ORCHESTRATOR_PORT", "8999") or 8999)
    peers: Dict[str, int] = {}
    try:
        for cn in psutil.net_connections(kind="tcp"):
            if cn.status != psutil.CONN_ESTABLISHED or not cn.laddr or not cn.raddr or cn.laddr.port != port:
                continue
            ip = str(cn.raddr.ip)
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            peers[ip] = peers.get(ip, 0) + 1
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return {"port": port, "peers": [{"ip": k, "n": v} for k, v in sorted(peers.items(), key=lambda kv: -kv[1])]}


async def _gather() -> Dict[str, Any]:
    names = list(READERS)
    answers = await asyncio.gather(*(_read(n, READERS[n]) for n in names))
    src = dict(zip(names, answers))
    t0 = time.monotonic()
    src["ops.connections"] = await asyncio.to_thread(_connections)
    _timing["ops.connections"] = int((time.monotonic() - t0) * 1000)
    names.append("ops.connections")
    out = core.build(src, own_ips=_own_ips())
    # how long each reader took this time, slowest first - the page's Sources card shows it
    out["timing"] = dict(sorted(((n, _timing.get(n, 0)) for n in names), key=lambda kv: -kv[1]))
    return out


def _start_gather() -> "asyncio.Future":
    if _cache["inflight"] is None:
        fut = asyncio.ensure_future(_gather())
        def _done(f: "asyncio.Future") -> None:
            _cache["inflight"] = None
            if not f.cancelled() and f.exception() is None:
                _cache["out"] = f.result()
                _cache["at"] = time.monotonic()
        fut.add_done_callback(_done)
        _cache["inflight"] = fut
    return _cache["inflight"]


@capability(
    "ops.snapshot",
    http_method="GET", http_path="/ops/snapshot", http_tags=["estate", "ops"],
    memory="off", silent=True,
    description="Live operations: the estate as six planes of nodes (work in flight, Vera core, "
                "services, runtimes, hosts, devices & mesh) placed on a lattice of domains "
                "(compute, data, storage, edge, dev), the pipes between them (requests, reads + "
                "writes, runs on, repo + build, mesh radio), what is in flight right now, the "
                "findings pinned to their nodes, the last events, and per-node series. Assembled "
                "from the existing readers, each with its own timeout; a reader that fails is "
                "named in `sources`. Cached 5 s. Output: {planes, nodes, links, inflight, "
                "inflight_kinds, errors, events, series, counts, sources, ts}.",
)
async def ops_snapshot(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    # stale-while-revalidate: the last snapshot answers at once (gathering takes seconds - the estate's readers run
    # side by side, the slowest decides); a fresh one is gathered in the background when it is older than CACHE_S.
    # Only the very first call, with nothing cached, and an explicit refresh wait for the gather.
    now = time.monotonic()
    have = _cache["out"] is not None
    if have and not refresh:
        age = now - _cache["at"]
        if age >= CACHE_S:
            _start_gather()
        return dict(_cache["out"], cached=True, age_s=round(age, 1), refreshing=_cache["inflight"] is not None)
    out = await _start_gather()
    return dict(out, cached=False, age_s=0.0, refreshing=False)


@capability(
    "ops.node.events",
    http_method="GET", http_path="/ops/node/events", http_tags=["estate", "ops"],
    memory="off", silent=True,
    description="The last events that name a node (its label, id or capability group) - the "
                "log a node shows on the Live operations page. Input: node (a label or id), "
                "limit (default 60). Output: {node, events:[{ts, type, name, group, trace, text}]}.",
)
async def ops_node_events(node: str = "", limit: int = 60, trace_id=None) -> Dict[str, Any]:
    ev = await _read("obs.events", 6.0)
    if not isinstance(ev, list):
        return {"node": node, "events": [], "error": (ev or {}).get("error") if isinstance(ev, dict) else "no events"}
    key = str(node or "").lower()
    keys = {key, key.split(":", 1)[-1], core._norm(key)} - {""}
    out = []
    for e in ev:
        if not isinstance(e, dict):
            continue
        hay = " ".join(str(e.get(k) or "") for k in ("name", "group", "node_id", "instance", "instance_id", "model", "args_preview", "host", "detail")).lower()
        if key and not any(k in hay for k in keys):
            continue
        out.append({"ts": str(e.get("ts") or "")[11:19], "type": str(e.get("type") or ""), "name": str(e.get("name") or e.get("node_id") or e.get("model") or ""),
                    "group": str(e.get("group") or ""), "trace": str(e.get("trace_id") or "")[:8], "text": str(e.get("args_preview") or e.get("detail") or e.get("error") or "")[:120]})
        if len(out) >= int(limit or 60):
            break
    return {"node": node, "events": out}


_PANEL = Path(__file__).parent / "ops_panel.html"


@_orch.APP.get("/ops/panel", include_in_schema=False)
async def _ops_panel():
    """Live operations - the Estate's Live ops pane, also standalone."""
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists()
                        else "<p style='color:red'>ops_panel.html not found</p>")
