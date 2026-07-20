"""
portainer_capabilities.py — Portainer integration (Phase 4)
===========================================================

Lets Vera drive a Portainer instance (the popular Docker/stack management UI) as
a first-class backend: enumerate its environments, containers and stacks, act on
containers, and *provision Portainer itself* on a registered Docker host when it
isn't running yet.

  portainer.save / list / delete       — connection store (API key sealed)
  portainer.ping                       — reachability + version
  portainer.endpoints                  — environments Portainer manages
  portainer.containers                 — containers in an endpoint
  portainer.stacks                     — stacks Portainer manages
  portainer.container.action           — start/stop/restart/kill/remove a container
  portainer.provision                  — docker-run Portainer CE if absent

Auth uses a Portainer **API key** (Settings → API tokens) sent as `X-API-Key`.
Provisioning goes through the sandbox-gated `docker.run` capability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso, enum_schema,
)

try:
    from Vera.vera.security import secrets as vsecrets
    _HAS_SECRETS = True
except Exception:                       # pragma: no cover
    vsecrets = None                     # type: ignore
    _HAS_SECRETS = False

log = logging.getLogger("vera.remote.portainer")
KEY_PORTAINER = "vera:remote:portainer"


def _redis():
    return getattr(_orch, "REDIS", None)


def _cap_raw(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


def _seal(v: str) -> str:
    if v and _HAS_SECRETS:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


def _open(v: str) -> str:
    if v and _HAS_SECRETS:
        try:
            return vsecrets.open_secret(v)
        except Exception:
            return v
    return v


async def _all() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_PORTAINER)
    except Exception:
        return []
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda p: (p.get("label") or p.get("id") or ""))
    return out


async def _get(pid: str, opened: bool = False) -> Optional[Dict]:
    r = _redis()
    if not r or not pid:
        return None
    raw = await r.hget(KEY_PORTAINER, pid)
    if not raw:
        return None
    rec = json.loads(raw)
    if opened:
        rec = dict(rec)
        rec["api_key"] = _open(rec.get("api_key", ""))
    return rec


async def _default() -> Optional[Dict]:
    allp = await _all()
    for p in allp:
        if p.get("default"):
            return await _get(p["id"], opened=True)
    return await _get(allp[0]["id"], opened=True) if allp else None


async def _resolve(portainer_id: str) -> Optional[Dict]:
    return await _get(portainer_id, opened=True) if portainer_id else await _default()


async def _papi(rec: Dict, method: str, path: str,
                body: Optional[Dict] = None) -> Dict:
    """Portainer API call. Returns {ok, status, data} or {ok:false, error}."""
    url = rec["url"].rstrip("/") + path
    headers = {"X-API-Key": rec.get("api_key", "")}
    try:
        async with httpx.AsyncClient(verify=bool(rec.get("verify_tls")), timeout=25) as c:
            r = await c.request(method, url, headers=headers,
                                json=body if body is not None else None)
            if r.status_code >= 400:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}",
                        "status": r.status_code}
            try:
                data = r.json()
            except Exception:
                data = r.text
            return {"ok": True, "status": r.status_code, "data": data}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
#  CONNECTION STORE
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "portainer.save",
    http_method="POST", http_path="/remote/portainer/save", http_tags=["remote", "portainer"],
    memory="off",
    description="Add/update a Portainer connection. API key is sealed. Inputs: "
                "label (str), url (str! — e.g. https://host:9443), api_key (str — "
                "blank keeps existing), verify_tls (bool=false), make_default "
                "(bool), id (str — update). Output: {ok, portainer(redacted)}.",
)
async def cap_portainer_save(label: str = "", url: str = "", api_key: str = "",
                             verify_tls: bool = False, make_default: bool = False,
                             id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"ok": False, "error": "store unavailable"}
    if not url and not id:
        return {"ok": False, "error": "url required"}
    pid = id or uuid.uuid4().hex[:12]
    existing = await _get(pid) or {}
    rec = dict(existing) if existing else {"id": pid, "created": now_iso()}
    rec["id"] = pid
    if url:
        rec["url"] = url.rstrip("/")
    rec["label"] = label or rec.get("label") or rec.get("url", pid)
    if api_key:
        rec["api_key"] = _seal(api_key)
    rec.setdefault("api_key", "")
    rec["verify_tls"] = bool(verify_tls)
    rec["default"] = bool(make_default) or rec.get("default", False)
    rec["updated"] = now_iso()
    if make_default:
        for other in await _all():
            if other["id"] != pid and other.get("default"):
                other["default"] = False
                await r.hset(KEY_PORTAINER, other["id"], json.dumps(other))
    await r.hset(KEY_PORTAINER, pid, json.dumps(rec))
    red = {k: v for k, v in rec.items() if k != "api_key"}
    red["has_api_key"] = bool(rec.get("api_key"))
    return {"ok": True, "portainer": red}


@capability(
    "portainer.list",
    http_method="GET", http_path="/remote/portainer/list", http_tags=["remote", "portainer"],
    memory="off", silent=True,
    description="List Portainer connections (API key redacted). Output: {portainer:[...]}.",
)
async def cap_portainer_list(trace_id=None) -> Dict:
    out = []
    for p in await _all():
        red = {k: v for k, v in p.items() if k != "api_key"}
        red["has_api_key"] = bool(p.get("api_key"))
        out.append(red)
    return {"portainer": out, "count": len(out)}


@capability(
    "portainer.delete",
    http_method="POST", http_path="/remote/portainer/delete", http_tags=["remote", "portainer"],
    description="Delete a Portainer connection. Input: id (str!). Output: {ok}.",
)
async def cap_portainer_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    await r.hdel(KEY_PORTAINER, id)
    return {"ok": True, "deleted": id}


# ═════════════════════════════════════════════════════════════════════════════
#  READ / ACT
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "portainer.ping",
    http_method="POST", http_path="/remote/portainer/ping", http_tags=["remote", "portainer"],
    memory="off",
    description="Check a Portainer instance is reachable and authenticated. "
                "Input: portainer_id (str — blank = default). Output: {ok, version}.",
)
async def cap_portainer_ping(portainer_id: str = "", trace_id=None) -> Dict:
    rec = await _resolve(portainer_id)
    if not rec:
        return {"ok": False, "error": "no Portainer connection configured"}
    res = await _papi(rec, "GET", "/api/system/status")
    if not res.get("ok"):
        res = await _papi(rec, "GET", "/api/status")  # older Portainer
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "unreachable")}
    data = res.get("data") or {}
    return {"ok": True, "version": data.get("Version", data.get("version", ""))}


@capability(
    "portainer.endpoints",
    http_method="POST", http_path="/remote/portainer/endpoints", http_tags=["remote", "portainer"],
    memory="off",
    description="List the environments (endpoints) Portainer manages. Input: "
                "portainer_id (str). Output: {ok, endpoints:[{id,name,type,url,"
                "status}]}.",
)
async def cap_portainer_endpoints(portainer_id: str = "", trace_id=None) -> Dict:
    rec = await _resolve(portainer_id)
    if not rec:
        return {"ok": False, "error": "no Portainer connection configured"}
    res = await _papi(rec, "GET", "/api/endpoints")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    eps = [{"id": e.get("Id"), "name": e.get("Name", ""), "type": e.get("Type"),
            "url": e.get("URL", ""), "status": e.get("Status")}
           for e in (res.get("data") or [])]
    return {"ok": True, "endpoints": eps}


@capability(
    "portainer.containers",
    http_method="POST", http_path="/remote/portainer/containers", http_tags=["remote", "portainer"],
    memory="off",
    description="List containers in a Portainer endpoint. Inputs: portainer_id "
                "(str), endpoint_id (int!), all (bool=true). Output: {ok, "
                "containers:[{id,name,image,state,status}]}.",
)
async def cap_portainer_containers(portainer_id: str = "", endpoint_id: int = 0,
                                   all: bool = True, trace_id=None) -> Dict:
    rec = await _resolve(portainer_id)
    if not rec:
        return {"ok": False, "error": "no Portainer connection configured"}
    if not endpoint_id:
        return {"ok": False, "error": "endpoint_id required"}
    res = await _papi(rec, "GET",
                      f"/api/endpoints/{endpoint_id}/docker/containers/json?all="
                      f"{'true' if all else 'false'}")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    cs = [{"id": (c.get("Id") or "")[:12], "name": (c.get("Names") or ["?"])[0].lstrip("/"),
           "image": c.get("Image", ""), "state": c.get("State", ""), "status": c.get("Status", "")}
          for c in (res.get("data") or [])]
    return {"ok": True, "containers": cs}


@capability(
    "portainer.stacks",
    http_method="POST", http_path="/remote/portainer/stacks", http_tags=["remote", "portainer"],
    memory="off",
    description="List the stacks Portainer manages. Input: portainer_id (str). "
                "Output: {ok, stacks:[{id,name,type,endpoint_id,status}]}.",
)
async def cap_portainer_stacks(portainer_id: str = "", trace_id=None) -> Dict:
    rec = await _resolve(portainer_id)
    if not rec:
        return {"ok": False, "error": "no Portainer connection configured"}
    res = await _papi(rec, "GET", "/api/stacks")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    ss = [{"id": s.get("Id"), "name": s.get("Name", ""), "type": s.get("Type"),
           "endpoint_id": s.get("EndpointId"), "status": s.get("Status")}
          for s in (res.get("data") or [])]
    return {"ok": True, "stacks": ss}


@capability(
    "portainer.container.action",
    http_method="POST", http_path="/remote/portainer/container/action", http_tags=["remote", "portainer"],
    description="Act on a container through Portainer. Inputs: portainer_id (str), "
                "endpoint_id (int!), container (str! — id/name), action "
                "('start'|'stop'|'restart'|'kill'|'pause'|'unpause'|'remove'). "
                "Output: {ok} or {error}.",
    schema=enum_schema(action=["start", "stop", "restart", "kill", "pause",
                               "unpause", "remove"]),
)
async def cap_portainer_container_action(portainer_id: str = "", endpoint_id: int = 0,
                                         container: str = "", action: str = "",
                                         trace_id=None) -> Dict:
    rec = await _resolve(portainer_id)
    if not rec:
        return {"ok": False, "error": "no Portainer connection configured"}
    if not (endpoint_id and container and action):
        return {"ok": False, "error": "endpoint_id, container and action required"}
    base = f"/api/endpoints/{endpoint_id}/docker/containers/{container}"
    if action == "remove":
        res = await _papi(rec, "DELETE", base + "?force=true")
    else:
        res = await _papi(rec, "POST", base + f"/{action}")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    await emit_event({"type": "remote.portainer.action", "container": container,
                      "action": action, "endpoint": endpoint_id})
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
#  PROVISION
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "portainer.provision",
    http_method="POST", http_path="/remote/portainer/provision", http_tags=["remote", "portainer"],
    description="Provision Portainer CE on a Docker host (via sandbox-gated "
                "docker.run) if it isn't already running. Inputs: host_id (str — "
                "docker host, default local), edition ('ce'|'ee', default ce), "
                "https_port (int=9443), http_port (int=9000). Output: {ok, "
                "container_id, url, note}.",
    schema=enum_schema(edition=["ce", "ee"]),
)
async def cap_portainer_provision(host_id: str = "", edition: str = "ce",
                                  https_port: int = 9443, http_port: int = 9000,
                                  trace_id=None) -> Dict:
    run = _cap_raw("docker.run")
    ps = _cap_raw("docker.ps")
    if not run:
        return {"ok": False, "error": "docker.run unavailable (docker module not loaded)"}
    # Skip if a portainer container already exists on the host.
    if ps:
        try:
            existing = (await ps(host_id=host_id or "local", all=True)).get("containers", [])
            for c in existing:
                if "portainer" in (c.get("Image", "") + " ".join(c.get("Names", []))).lower():
                    return {"ok": True, "already": True,
                            "note": "a Portainer container already exists on this host",
                            "url": f"https://<host>:{https_port}"}
        except Exception:
            pass
    image = f"portainer/portainer-{'ee' if edition == 'ee' else 'ce'}:latest"
    res = await run(
        host_id=host_id or "local", image=image, name="vera-portainer",
        ports=f"{https_port}:9443,{http_port}:9000", pull=True,
        volumes="/var/run/docker.sock:/var/run/docker.sock,portainer_data:/data")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "docker.run failed")}
    await emit_event({"type": "remote.portainer.provisioned",
                      "container_id": res.get("container_id", "")[:12]})
    return {"ok": True, "container_id": res.get("container_id", ""),
            "url": f"https://<host>:{https_port}",
            "note": "Open the URL to set the admin password, then create an API "
                    "key (Settings → API tokens) and save it with portainer.save."}


log.info("portainer_capabilities loaded")
