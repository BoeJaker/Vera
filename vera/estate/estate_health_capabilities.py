"""
estate_health_capabilities.py -- is the estate's infrastructure actually up?
=========================================================================

One read-only check behind the Estate tab's Overview. It answers the questions
nothing on screen could answer when prod showed "no estate hosts found" on
12 Sep 2026, and the ones that would have been next:

  * Is Vera connected to the Redis that holds its estate settings?
  * Did an infrastructure container on the Vera host fail to start, or lose
    a port race at boot?
  * Which running Proxmox guests will not come back after a host reboot?
  * Are backups running, and did any fail?
  * Is a disk damaged or unused, or is Docker's data disk filling up?
  * Are the file server (VFS-02) and the directory (FreeIPA) up?

Each source runs under its own timeout, so one slow answer cannot blank the
page. The rules live in estate_health_core.py; this module gathers the facts.

Other modules' helpers and capabilities are reached through the capability
registry. Capability modules load from _module_files under their bare file
name (docker_capabilities, not Vera.vera.workers.docker_capabilities), so an
import-path lookup finds nothing in a running Vera.

Capabilities
------------
  estate.health   findings from all six sources; cached for 120 s
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

# The backup and disk readers run a script on each Proxmox node over SSH (60 s
# limit of their own), so they get longer than the local checks.
SOURCE_TIMEOUTS_S = {"state_store": 10.0, "containers": 25.0, "guests": 25.0,
                     "backups": 75.0, "storage": 75.0, "services": 30.0}
CACHE_TTL_S = 120.0
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


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    """Run another capability's function directly. Its own {error}, or a
    failure to run it at all, comes back as {error}."""
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


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


async def _proxmox_nodes() -> Dict[str, Any]:
    """Online Proxmox nodes, once per API host even when several cluster
    records point at it: {targets:[{cluster_id, node}], errors:[{host, error}]}."""
    px = _module_of("proxmox.status")
    if not px or not all(k in px for k in ("_all_raw", "_open", "_pve")):
        return {"error": "the Proxmox capabilities are not loaded"}
    targets: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for host, group in core.group_clusters(await px["_all_raw"]()).items():
        rows, err, record = None, "", None
        for candidate in group:
            rows, err = await px["_pve"](px["_open"](candidate), "GET", "/cluster/resources?type=node")
            if rows is not None:
                record = candidate
                break
        if rows is None:
            errors.append({"host": host, "error": err})
            continue
        targets.extend({"cluster_id": record.get("id", ""), "node": n.get("node", "")}
                       for n in rows if n.get("status") == "online" and n.get("node"))
    return {"targets": targets, "errors": errors}


async def _per_node(cap_name: str) -> Dict[str, Any]:
    """Run a per-node pxstore capability on every online Proxmox node."""
    nodes = await _proxmox_nodes()
    if nodes.get("error"):
        return nodes
    reports = [{"node": t["node"],
                "status": await _call(cap_name, cluster_id=t["cluster_id"], node=t["node"])}
               for t in nodes["targets"]]
    reports += [{"node": e["host"], "status": {"error": e["error"]}} for e in nodes["errors"]]
    return {"reports": reports}


async def _backups() -> Dict[str, Any]:
    per = await _per_node("pxstore.backup.status")
    if per.get("error"):
        return {"error": per["error"]}
    return core.backups_section(per["reports"])


async def _storage() -> Dict[str, Any]:
    per, docker_disk = await asyncio.gather(_per_node("pxstore.disks"), _call("docker.disk.status"))
    reports = per.get("reports")
    if reports is None:
        reports = [{"node": "Proxmox", "status": {"error": per.get("error") or "no nodes"}}]
    return core.storage_section(reports, docker_disk)


async def _services() -> Dict[str, Any]:
    vfs, identity = await asyncio.gather(_call("vfs.health"), _call("identity.status"))
    return core.services_section(vfs, identity)


@capability(
    "estate.health",
    http_method="GET", http_path="/estate/health", http_tags=["estate", "obs"],
    memory="off", silent=True,
    description="Is the estate's infrastructure up? One findings list from six sources, each "
                "under its own timeout: the state store (is Vera connected to the Redis that "
                "holds its estate settings), infrastructure containers on the Vera host "
                "(stopped though set to restart, never started, lost a port at boot; session "
                "and Loop Lab sandboxes are ignored), running Proxmox guests that will not start "
                "after a host reboot and duplicate cluster records, backups (pxstore.backup."
                "status warnings, failed guest backups, no backup in 36 h), disks (damaged, "
                "importable or unused disks, Docker data disk filling up) and services (VFS-02 "
                "services, estate mounts, FreeIPA reachability). Read-only, cached 120 s. "
                "Input: refresh (bool - skip the cache). Output: {level: ok|warn|error, "
                "counts:{error,warn,info}, findings:[{severity, section, subject, message, "
                "detail}], sections:{state_store, containers, guests, backups, storage, "
                "services: {label, facts, elapsed_ms, error, findings}}, checked_at, cached}.",
)
async def cap_estate_health(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    if not refresh and _CACHE["result"] and time.monotonic() - _CACHE["at"] < CACHE_TTL_S:
        return {**_CACHE["result"], "cached": True}

    async def run(name: str, source) -> tuple:
        started = time.monotonic()
        limit = SOURCE_TIMEOUTS_S.get(name, 25.0)
        try:
            out = await asyncio.wait_for(source(), limit)
        except asyncio.TimeoutError:
            out = {"error": f"no answer within {int(limit)} s"}
        except Exception as e:
            log.warning("estate.health: %s check failed: %s", name, e)
            out = {"error": f"{type(e).__name__}: {e}"}
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return name, out

    sources = (("state_store", _state_store), ("containers", _containers), ("guests", _guests),
               ("backups", _backups), ("storage", _storage), ("services", _services))
    result = core.summarize(dict(await asyncio.gather(*(run(n, s) for n, s in sources))))
    result["checked_at"] = now_iso()
    _CACHE.update(at=time.monotonic(), result=result)
    return {**result, "cached": False}
