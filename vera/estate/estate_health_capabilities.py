"""
estate_health_capabilities.py -- is the estate's infrastructure actually up?
=========================================================================

One read-only check behind the Estate tab's Overview. It answers the three
questions nothing on screen could answer when prod showed "no estate hosts
found" on 12 Sep 2026:

  * Is Vera connected to the Redis that holds its estate settings?
  * Did an infrastructure container on the Vera host fail to start, or lose
    a port race at boot?
  * Which running Proxmox guests will not come back after a host reboot?

Each source runs under its own timeout, so one slow answer cannot blank the
page. The rules live in estate_health_core.py; this module gathers the facts.

The Docker and Proxmox helpers are reached through the capability registry.
Capability modules load from _module_files under their bare file name
(docker_capabilities, not Vera.vera.workers.docker_capabilities), so an
import-path lookup finds nothing in a running Vera.

Capabilities
------------
  estate.health   findings from the state store, the Vera host's containers
                  and Proxmox guest autostart; cached for 45 s
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, now_iso
from Vera.vera.estate import estate_health_core as core

log = logging.getLogger("vera.estate")

SOURCE_TIMEOUT_S = 25.0
CACHE_TTL_S = 45.0
_INSPECT_CONCURRENCY = 4      # Engine inspect calls at once; the host runs 250+ containers
_CONFIG_CONCURRENCY = 6       # Proxmox guest config reads at once
_CACHE: Dict[str, Any] = {"at": 0.0, "result": None}


def _text(v: Any) -> Any:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v


def _display_url(url: str) -> str:
    """The Redis address without credentials."""
    p = urlparse(url or "")
    port = f":{p.port}" if p.port else ""
    db = (p.path or "/").lstrip("/") or "0"
    return f"{p.scheme or 'redis'}://{p.hostname or ''}{port}/{db}"


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    """The globals of the module that registered `cap_name`, or None when no
    such capability is loaded."""
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return None
    return getattr(inspect.unwrap(fn), "__globals__", None)


async def _state_store() -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"error": "Vera has no Redis connection"}
    info = {_text(k): _text(v) for k, v in ((await r.info("server")) or {}).items()}
    present = {key: bool(await r.exists(key)) for key in core.EXPECTED_STATE_KEYS}
    return core.state_store_section(info, present, _display_url(getattr(_orch, "REDIS_URL", "")),
                                    await r.dbsize())


async def _containers() -> Dict[str, Any]:
    dk = _module_of("docker.ps")
    if not dk or not all(k in dk for k in ("_get_host", "_engine_request", "_parse_engine_json")):
        return {"error": "the Docker capabilities are not loaded"}
    engine, parse = dk["_engine_request"], dk["_parse_engine_json"]
    host = dk["_get_host"]("local")
    status, body, _ = await engine(host, "GET", "/containers/json?all=true")
    if status != 200:
        return {"error": f"Docker Engine answered HTTP {status}"}
    rows = await parse(body, [])
    rows = rows if isinstance(rows, list) else []

    def name_of(row: Dict[str, Any]) -> str:
        return ((row.get("Names") or [""])[0] or "").lstrip("/")

    sandboxes, stopped = 0, []
    for row in rows:
        labels = row.get("Labels") or {}
        if core.is_sandbox(name_of(row), labels):
            sandboxes += 1
        elif core.needs_inspection(name_of(row), labels, row.get("State") or ""):
            stopped.append(row)
    gate = asyncio.Semaphore(_INSPECT_CONCURRENCY)

    async def read(row: Dict[str, Any]) -> Dict[str, Any]:
        async with gate:
            st, raw, _ = await engine(host, "GET", f"/containers/{row.get('Id')}/json")
        data = await parse(raw, {}) if st == 200 else {}
        data = data if isinstance(data, dict) else {}
        state = data.get("State") or {}
        return {"name": name_of(row), "labels": row.get("Labels") or {},
                "state": state.get("Status") or row.get("State") or "",
                "restart": ((data.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", ""),
                "exit_code": state.get("ExitCode"), "error": state.get("Error") or "",
                "finished_at": state.get("FinishedAt") or ""}

    details = await asyncio.gather(*(read(row) for row in stopped))
    return core.container_section(details, listed=len(rows), sandboxes=sandboxes)


async def _guests() -> Dict[str, Any]:
    px = _module_of("proxmox.status")
    if not px or not all(k in px for k in ("_all_raw", "_open", "_pve")):
        return {"error": "the Proxmox capabilities are not loaded"}
    pve = px["_pve"]
    records = await px["_all_raw"]()
    guests: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for host, group in core.group_clusters(records).items():
        rec, resources, err = None, None, ""
        for candidate in group:                     # the first record whose token works
            rec = px["_open"](candidate)
            resources, err = await pve(rec, "GET", "/cluster/resources?type=vm")
            if resources is not None:
                break
        if resources is None:
            errors.append({"host": host, "error": err})
            continue
        gate = asyncio.Semaphore(_CONFIG_CONCURRENCY)

        async def with_onboot(g: Dict[str, Any], rec=rec, gate=gate) -> Dict[str, Any]:
            async with gate:
                cfg, cerr = await pve(
                    rec, "GET", f"/nodes/{g.get('node')}/{g.get('type')}/{g.get('vmid')}/config")
            return {"vmid": g.get("vmid"), "name": g.get("name") or "", "type": g.get("type"),
                    "node": g.get("node"), "status": g.get("status"),
                    "template": bool(g.get("template")),
                    "onboot": (cfg or {}).get("onboot"),
                    "config_error": cerr if cfg is None else ""}

        running = [g for g in resources or []
                   if g.get("status") == "running" and not g.get("template")]
        guests.extend(await asyncio.gather(*(with_onboot(g) for g in running)))
    return core.guest_section(guests, records, errors)


@capability(
    "estate.health",
    http_method="GET", http_path="/estate/health", http_tags=["estate", "obs"],
    memory="off", silent=True,
    description="Is the estate's infrastructure up? One findings list from three sources, "
                "each under its own 25 s timeout: the state store (is Vera connected to the "
                "Redis that holds its estate settings), infrastructure containers on the Vera "
                "host (stopped though set to restart, never started, lost a port at boot; "
                "session and Loop Lab sandboxes are ignored), and running Proxmox guests "
                "that will not start after a host reboot, plus duplicate cluster records. "
                "Read-only, cached 45 s. Input: refresh (bool - skip the cache). Output: "
                "{level: ok|warn|error, counts:{error,warn,info}, findings:[{severity, "
                "section, subject, message, detail}], sections:{state_store, containers, "
                "guests: {label, facts, elapsed_ms, error, findings}}, checked_at, cached}.",
)
async def cap_estate_health(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    if not refresh and _CACHE["result"] and time.monotonic() - _CACHE["at"] < CACHE_TTL_S:
        return {**_CACHE["result"], "cached": True}

    async def run(name: str, source) -> tuple:
        started = time.monotonic()
        try:
            out = await asyncio.wait_for(source(), SOURCE_TIMEOUT_S)
        except asyncio.TimeoutError:
            out = {"error": f"no answer within {int(SOURCE_TIMEOUT_S)} s"}
        except Exception as e:
            log.warning("estate.health: %s check failed: %s", name, e)
            out = {"error": f"{type(e).__name__}: {e}"}
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return name, out

    sources = (("state_store", _state_store), ("containers", _containers), ("guests", _guests))
    result = core.summarize(dict(await asyncio.gather(*(run(n, s) for n, s in sources))))
    result["checked_at"] = now_iso()
    _CACHE.update(at=time.monotonic(), result=result)
    return {**result, "cached": False}
