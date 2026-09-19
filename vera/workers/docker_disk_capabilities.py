"""docker.disk.breakdown - where a Docker host's disk has gone.

docker.disk.status says how full the data disk is and how many session
sandboxes could be reaped; this says what the space IS: images, container
writable layers, volumes and build cache, what each could give back and why,
the containers that have grown, dangling images, orphan volumes. It is the
Engine's own accounting (/system/df) shaped by docker_disk_core.

/system/df walks every layer and took minutes on the Vera host (250+
containers) - long enough that a plain request timed out and the Engine then
answered "a disk usage operation is already running" to the next one. So the
answer is computed in the background and cached: the first call starts the
work and says so, later calls return the cached breakdown with its age, and
refresh starts it again. The panel polls while it is computing.

Capabilities
------------
  docker.disk.breakdown   the breakdown for one Docker host (cached, background)
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, now_iso
from Vera.vera.workers import docker_disk_core as core

log = logging.getLogger("vera.docker")

CACHE_TTL_S = 600.0
DF_TIMEOUT_S = 900.0
_STATE: Dict[str, Dict[str, Any]] = {}       # host_id -> {at, result, task, started, error}


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


async def _compute(host_id: str) -> None:
    st = _STATE.setdefault(host_id, {})
    st.update(started=time.time(), error="")
    try:
        dk = _module_of("docker.ps") or {}
        if not all(k in dk for k in ("_get_host", "_engine_request")):
            raise RuntimeError("the Docker capabilities are not loaded")
        rec = dk["_get_host"](host_id)
        if not rec:
            raise RuntimeError(f"no Docker host called {host_id!r}")
        status, body, _ct = await dk["_engine_request"](rec, "GET", "/system/df", timeout=DF_TIMEOUT_S)
        if status == 409:
            raise RuntimeError("the Engine is still finishing an earlier disk-usage walk; try again in a minute")
        if status != 200:
            raise RuntimeError(f"the Engine answered HTTP {status} to /system/df")
        df = json.loads(body or b"{}")
        extra = await _call("docker.disk.status") if host_id == "local" else {}
        result = core.summarize(df, reap=extra.get("reap") if isinstance(extra, dict) else None,
                                mount=extra if isinstance(extra, dict) and extra.get("total_gb") else None)
        result["host_id"] = host_id
        result["label"] = rec.get("label") or host_id
        result["computed_at"] = now_iso()
        result["took_s"] = round(time.time() - st["started"], 1)
        st.update(at=time.time(), result=result)
    except Exception as e:
        st["error"] = str(e)[:300]
        log.warning("docker.disk.breakdown(%s): %s", host_id, e)
    finally:
        st["task"] = None


def _start(host_id: str) -> None:
    st = _STATE.setdefault(host_id, {})
    if st.get("task") is not None and not st["task"].done():
        return
    st["task"] = asyncio.create_task(_compute(host_id))


@capability(
    "docker.disk.breakdown",
    http_method="GET", http_path="/docker/disk/breakdown", http_tags=["docker", "obs"],
    memory="off", silent=True,
    description="Where a Docker host's disk has gone: images, container writable layers, "
                "volumes and build cache with what each could give back and why; the "
                "containers that have grown, untagged images, orphan volumes, the session "
                "sandboxes the reaper would take; plain findings. The Engine's /system/df "
                "shaped by docker_disk_core. It is slow (minutes on a busy host) so it is "
                "computed in the background and cached 10 min: the first call starts it and "
                "answers {computing: true}; poll until computing is false. Inputs: host_id "
                "(str='local'), refresh (bool - compute again now). Output: {computing, "
                "started_at, age_s, error, breakdown:{layers_size, categories:[{name,label,"
                "size,count,reclaimable,why}], reclaimable_total, sandboxes, top_containers, "
                "top_images, top_volumes, findings, computed_at, took_s}}.",
)
async def cap_disk_breakdown(host_id: str = "local", refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    host_id = host_id or "local"
    want_refresh = bool(refresh) and str(refresh).lower() not in ("0", "false", "no")
    st = _STATE.setdefault(host_id, {})
    fresh = st.get("result") is not None and time.time() - st.get("at", 0) < CACHE_TTL_S
    if want_refresh or (not fresh and (st.get("task") is None or st["task"].done())):
        _start(host_id)
    computing = st.get("task") is not None and not st["task"].done()
    out: Dict[str, Any] = {"host_id": host_id, "computing": computing,
                           "started_at": st.get("started"), "error": st.get("error") or ""}
    if st.get("result") is not None:
        out["breakdown"] = st["result"]
        out["age_s"] = int(time.time() - st.get("at", time.time()))
    return out


log.info("docker_disk_capabilities ready - docker.disk.breakdown")
