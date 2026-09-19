"""estate.entity.resolve - everything the estate knows about one thing.

Twelve entity kinds appear on six or more Estate pages, each page rendering
its own half. This answers `<kind>:<id>` with one record: what it is, its
facts, its standing in every registration plane (SSH login, directory, mesh,
certificate, backup), the other entities it touches, and where to go next.
It reads only what already exists - estate.machines, backup.status,
certs.list, netsec.mesh.members, identity.host.list, exec.ssh.hosts.list,
integration.list, docker.hosts.list, pxstore.inventory - each under a timeout,
and a reader that does not answer is named in the record rather than blanked.
The joins live in estate_entity_core.py.

Also serves /ui/vera-entity-drawer.js, the slide-over any panel opens on a
reference (veraEntityDrawer.open(ref)); the drawer's links go through
veraUI.openEntity, so no page learns another page's data model.

Capabilities
------------
  estate.entity.resolve   one record for one reference
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.responses import Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability
from Vera.vera.estate import estate_entity_core as core
from Vera.vera.estate.estate_nav_core import parse_entity

log = logging.getLogger("vera.estate")

READER_TIMEOUT_S = 30.0
CACHE_TTL_S = 20.0
_CACHE: Dict[str, Any] = {"at": 0.0, "sources": None}
_DRAWER = Path(__file__).parent / "vera-entity-drawer.js"


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await asyncio.wait_for(fn(**kwargs), READER_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"error": f"{cap_name} did not answer within {int(READER_TIMEOUT_S)} s"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


async def _inventory() -> Dict[str, Any]:
    """pxstore.inventory needs the cluster and node; take the first cluster's."""
    px = _module_of("proxmox.status") or {}
    if "_all_raw" not in px:
        return {"error": "the Proxmox capabilities are not loaded"}
    records = await px["_all_raw"]()
    if not records:
        return {"error": "no Proxmox cluster is registered"}
    rec = records[0]
    node = ""
    try:
        nodes, _err = await px["_pve"](px["_open"](rec), "GET", "/nodes")
        node = (nodes or [{}])[0].get("node", "")
    except Exception as e:
        log.debug("entity: node list failed: %s", e)
    if not node:
        return {"error": "the Proxmox node could not be named"}
    return await _call("pxstore.inventory", cluster_id=rec.get("id", ""), node=node)


async def _sources(refresh: bool = False) -> core.Sources:
    if not refresh and _CACHE["sources"] is not None and time.time() - _CACHE["at"] < CACHE_TTL_S:
        return _CACHE["sources"]
    names = ("estate.machines", "backup.status", "certs.list", "netsec.mesh.members",
             "identity.host.list", "exec.ssh.hosts.list", "integration.list", "docker.hosts.list")
    results = await asyncio.gather(*[_call(n) for n in names], _inventory())
    got = dict(zip(names + ("pxstore.inventory",), results))
    errors = {n: r["error"] for n, r in got.items() if isinstance(r, dict) and r.get("error")}
    ident = got["identity.host.list"]
    src = core.Sources(
        machines=got["estate.machines"].get("machines"),
        backups=got["backup.status"] if not got["backup.status"].get("error") else {},
        certs=got["certs.list"].get("certs"),
        mesh=got["netsec.mesh.members"].get("members"),
        identity=ident.get("hosts") or ident.get("result") or [],
        ssh_hosts=got["exec.ssh.hosts.list"].get("hosts"),
        integrations=got["integration.list"].get("integrations"),
        docker_hosts=got["docker.hosts.list"].get("hosts"),
        inventory=got["pxstore.inventory"] if not got["pxstore.inventory"].get("error") else {},
        errors=errors)
    _CACHE.update(at=time.time(), sources=src)
    return src


@capability(
    "estate.entity.resolve",
    http_method="GET", http_path="/estate/entity/resolve", http_tags=["estate"],
    memory="off", silent=True,
    description="Everything the estate knows about one thing, as one record. Input: ref "
                "(str! - <kind>:<id>; kinds: guest (vmid), host (SSH login id), pool, dataset, "
                "integration, cert (name), backup-job (id), mesh (host_id), identity (fqdn), "
                "docker-host), refresh (bool - re-read the sources; they are cached 20 s). "
                "Output: {found, ref, kind, id, noun, title, subtitle, facts:[{label,value}], "
                "planes:{ssh,directory,mesh,certificate,backup: {state: yes|no|n/a|unknown, "
                "detail, ref}}, related:[{ref,label,noun,detail}], links:[{label,ref|pane,sub}], "
                "errors:{reader: why}}. Read-only; joins existing readers, collects nothing new.",
)
async def cap_entity_resolve(ref: str = "", refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    if not parse_entity(ref):
        return {"found": False, "ref": ref, "error": "ref must be <kind>:<id>, e.g. guest:145 or pool:tank_sdh"}
    src = await _sources(bool(refresh) and str(refresh).lower() not in ("0", "false", "no"))
    return core.resolve(ref, src)


@APP.get("/ui/vera-entity-drawer.js", include_in_schema=False)
async def _entity_drawer_js():
    body = _DRAWER.read_text(encoding="utf-8") if _DRAWER.exists() else "console.error('vera-entity-drawer.js missing')"
    return Response(body, media_type="application/javascript")


log.info("estate_entity_capabilities ready - estate.entity.resolve")
