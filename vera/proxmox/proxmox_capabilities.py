"""
proxmox_capabilities.py — Vera Proxmox VE control plane (Iteration 1)
====================================================================

A phone- and desktop-friendly Proxmox dashboard. This module is the backend:
it stores the cluster API credentials *encrypted in Redis* (Fernet, via
vera/security/secrets.py — never plaintext, never returned to the UI), exposes
live monitoring + guest lifecycle over the Proxmox API, and proxies Proxmox's
native xterm/noVNC consoles over a WebSocket so a browser on a phone can reach
any guest's terminal or desktop without being on the cluster LAN.

Discovery already exists separately as `netscan.proxmox.scan`
(execution/exec_capabilities.py) which writes the topology into the Neo4j aux
graph. This module is the *operations* surface and keeps its own credential
store so the token is sealed at rest.

Capabilities (group `proxmox.*`)
────────────────────────────────
  proxmox.cluster.save / .list / .delete   — credential store (token sealed)
  proxmox.status                           — live nodes + guests + storage
  proxmox.guest.action                     — start/stop/shutdown/reboot
  proxmox.console.ticket                   — proxy availability + deep-link

WebSocket
─────────
  /proxmox/console/ws/{cluster_id}/{node}/{kind}/{vmid}?mode=term|vnc
      Proxies Proxmox's vncwebsocket (xterm for term, RFB for vnc).

Redis layout
────────────
  vera:proxmox:clusters   hash  id -> JSON   (token + console_password sealed)

Console-auth note
─────────────────
Proxmox's vncwebsocket authenticates with a *PVEAuthCookie ticket* minted from
`/access/ticket` (username + password), NOT an API token. So the proxied
console requires the optional sealed `console_user`/`console_password`. When
only an API token is configured, monitoring + lifecycle still work and the UI
falls back to a deep-link into Proxmox's own noVNC (hence the "both" access
model). The token alone covers everything except the in-Vera console.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
from fastapi import WebSocket
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.proxmox")

_HERE = Path(__file__).parent
KEY_CLUSTERS = "vera:proxmox:clusters"

# Secret fields sealed before storage and never returned to the UI.
_SECRET_FIELDS = ("token", "console_password")

# Short-lived console sessions: a termproxy/vncproxy ticket is minted by
# proxmox.console.ticket and consumed seconds later by the WS route. Proxmox's
# console tickets are single-use and expire quickly, so we never persist these.
_CONSOLE_SESSIONS: Dict[str, Dict[str, Any]] = {}
_CONSOLE_TTL = 90.0  # seconds


def _prune_sessions() -> None:
    import time
    now = time.time()
    for sid in [k for k, v in _CONSOLE_SESSIONS.items()
                if now - v.get("ts", 0) > _CONSOLE_TTL]:
        _CONSOLE_SESSIONS.pop(sid, None)

# Optional dep — used only for the console proxy. Monitoring works without it.
try:
    import websockets  # type: ignore
    HAS_WS = True
except Exception:                       # pragma: no cover
    websockets = None                   # type: ignore
    HAS_WS = False
    log.info("proxmox: 'websockets' not installed — in-Vera console proxy "
             "disabled; the dashboard falls back to Proxmox deep-links. "
             "Install with: pip install websockets")


# ═════════════════════════════════════════════════════════════════════════════
#  STORE  (mirrors accounts_capabilities: seal on write, redact on read)
# ═════════════════════════════════════════════════════════════════════════════
def _redis():
    return getattr(_orch, "REDIS", None)


def _cap(name: str):
    """Resolve another capability's function by name (decoupled cross-module calls)."""
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


async def _all_raw() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    items = await r.hgetall(KEY_CLUSTERS)
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    return out


def _open(rec: Dict) -> Dict:
    """Copy with secret fields decrypted (server-side use only)."""
    out = dict(rec)
    for f in _SECRET_FIELDS:
        out[f] = vsecrets.open_secret(rec.get(f, ""))
    return out


def _redact(rec: Dict) -> Dict:
    """UI-safe copy: secrets stripped, replaced with has_* flags."""
    out = {}
    for k, v in rec.items():
        if k in _SECRET_FIELDS:
            out[f"has_{k}"] = bool(v)
        else:
            out[k] = v
    return out


async def _get_cluster(cluster_id: str, opened: bool = False) -> Optional[Dict]:
    r = _redis()
    if not r or not cluster_id:
        return None
    raw = await r.hget(KEY_CLUSTERS, cluster_id)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except Exception:
        return None
    return _open(rec) if opened else rec


# ═════════════════════════════════════════════════════════════════════════════
#  PVE API HELPERS
# ═════════════════════════════════════════════════════════════════════════════
def _api_base(rec: Dict) -> str:
    """Full API base, e.g. https://host:8006/api2/json (tolerant of either form)."""
    u = (rec.get("api_url") or "").rstrip("/")
    if not u.endswith("/api2/json"):
        u = u + "/api2/json"
    return u


def _web_root(rec: Dict) -> str:
    """Web UI root, e.g. https://host:8006 (for deep-links + ws host)."""
    u = (rec.get("api_url") or "").rstrip("/")
    if u.endswith("/api2/json"):
        u = u[: -len("/api2/json")]
    return u


def _host_port(rec: Dict) -> Tuple[str, int]:
    p = urlparse(_web_root(rec))
    return (p.hostname or ""), (p.port or 8006)


async def _pve(rec: Dict, method: str, path: str,
               data: Optional[Dict] = None) -> Tuple[Optional[Any], str]:
    """Token-authenticated PVE API call. `rec` must carry an *opened* token."""
    token = rec.get("token", "")
    if not token:
        return None, "no API token configured"
    url = _api_base(rec) + path
    headers = {"Authorization": f"PVEAPIToken={token}"}
    verify = bool(rec.get("verify_tls"))
    try:
        async with httpx.AsyncClient(timeout=20, verify=verify) as c:
            r = await c.request(method, url, headers=headers, data=data or {})
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            return r.json().get("data"), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES — credential store
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "proxmox.cluster.save",
    http_method="POST", http_path="/proxmox/cluster/save", http_tags=["proxmox"],
    memory="off",
    description="Create/update a Proxmox cluster connection. The API `token` "
                "('USER@REALM!TOKENID=SECRET') and optional `console_password` "
                "are SEALED (Fernet) before storage and never returned. Blank "
                "secret fields keep the existing sealed value. Inputs: id (str, "
                "optional — omit to create), label, api_url "
                "('https://host:8006' or full /api2/json), token, verify_tls "
                "(bool=false), console_user ('root@pam' — enables the in-Vera "
                "console proxy), console_password. Output: {ok, cluster(redacted)}.",
)
async def cap_cluster_save(
    id: str = "", label: str = "", api_url: str = "", token: str = "",
    verify_tls: bool = False, console_user: str = "",
    console_password: str = "", trace_id=None,
) -> Dict:
    r = _redis()
    if not r:
        return {"error": "store unavailable (no Redis)"}
    existing = None
    if id:
        raw = await r.hget(KEY_CLUSTERS, id)
        existing = json.loads(raw) if raw else None
    rec = dict(existing) if existing else {
        "id": str(uuid.uuid4()), "created": now_iso(),
    }
    for k, v in (("label", label), ("api_url", api_url),
                 ("verify_tls", bool(verify_tls)), ("console_user", console_user)):
        if v != "" or k not in rec:
            rec[k] = v
    if not rec.get("label"):
        rec["label"] = (api_url or "Proxmox").split("//", 1)[-1].split("/")[0]
    # Seal secrets only when a (truthy) new value is provided; else keep existing.
    try:
        if token:
            rec["token"] = vsecrets.seal(token)
        if console_password:
            rec["console_password"] = vsecrets.seal(console_password)
    except RuntimeError as e:
        return {"error": str(e)}
    rec["updated"] = now_iso()
    await r.hset(KEY_CLUSTERS, rec["id"], json.dumps(rec))
    return {"ok": True, "cluster": _redact(rec)}


@capability(
    "proxmox.cluster.list",
    http_method="GET", http_path="/proxmox/cluster/list", http_tags=["proxmox"],
    memory="off", silent=True,
    description="List saved Proxmox clusters (REDACTED — secrets replaced with "
                "has_token / has_console_password). Output: {clusters:[...]}.",
)
async def cap_cluster_list(trace_id=None) -> Dict:
    return {"clusters": [_redact(c) for c in await _all_raw()]}


@capability(
    "proxmox.cluster.delete",
    http_method="POST", http_path="/proxmox/cluster/delete", http_tags=["proxmox"],
    memory="off",
    description="Delete a saved Proxmox cluster connection. Input: id (str!). "
                "Output: {ok}.",
)
async def cap_cluster_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    await r.hdel(KEY_CLUSTERS, id)
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES — monitoring + lifecycle
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "proxmox.status",
    http_method="POST", http_path="/proxmox/status", http_tags=["proxmox"],
    memory="off", silent=True,
    description="Live cluster snapshot via /cluster/resources + /cluster/status. "
                "Input: cluster_id (str!). Output: {cluster:{name,quorate}, "
                "nodes:[{node,status,cpu,maxcpu,mem,maxmem,uptime}], "
                "guests:[{vmid,name,type,node,status,cpu,maxcpu,mem,maxmem,"
                "maxdisk,uptime,template}], storage:[{storage,node,disk,maxdisk,"
                "status}], counts}.",
)
async def cap_status(cluster_id: str = "", trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found", "nodes": [], "guests": []}

    resources, err = await _pve(rec, "GET", "/cluster/resources")
    if resources is None:
        return {"error": err, "nodes": [], "guests": [], "storage": []}

    nodes, guests, storage = [], [], []
    for it in resources or []:
        t = it.get("type")
        if t == "node":
            nodes.append({
                "node": it.get("node", ""), "status": it.get("status", ""),
                "cpu": it.get("cpu", 0), "maxcpu": it.get("maxcpu", 0),
                "mem": it.get("mem", 0), "maxmem": it.get("maxmem", 0),
                "uptime": it.get("uptime", 0),
            })
        elif t in ("qemu", "lxc"):
            guests.append({
                "vmid": it.get("vmid", 0), "name": it.get("name", ""),
                "type": t, "node": it.get("node", ""),
                "status": it.get("status", ""), "cpu": it.get("cpu", 0),
                "maxcpu": it.get("maxcpu", 0), "mem": it.get("mem", 0),
                "maxmem": it.get("maxmem", 0), "maxdisk": it.get("maxdisk", 0),
                "uptime": it.get("uptime", 0),
                "netin": it.get("netin", 0), "netout": it.get("netout", 0),
                "template": bool(it.get("template", 0)),
            })
        elif t == "storage":
            storage.append({
                "storage": it.get("storage", ""), "node": it.get("node", ""),
                "disk": it.get("disk", 0), "maxdisk": it.get("maxdisk", 0),
                "status": it.get("status", ""),
            })

    quorate, cname = None, rec.get("label", "")
    cstatus, _ = await _pve(rec, "GET", "/cluster/status")
    for it in cstatus or []:
        if it.get("type") == "cluster":
            quorate = bool(it.get("quorate", 0))
            cname = it.get("name", cname)

    nodes.sort(key=lambda n: n["node"])
    guests.sort(key=lambda g: (g["node"], g["vmid"]))
    return {
        "cluster": {"name": cname, "quorate": quorate},
        "nodes": nodes, "guests": guests, "storage": storage,
        "counts": {"nodes": len(nodes), "guests": len(guests),
                   "running": sum(1 for g in guests if g["status"] == "running")},
    }


_GUEST_ACTIONS = {"start", "stop", "shutdown", "reboot", "suspend", "resume"}


@capability(
    "proxmox.guest.action",
    http_method="POST", http_path="/proxmox/guest/action", http_tags=["proxmox"],
    memory="off",
    description="Lifecycle action on a guest. Inputs: cluster_id (str!), node "
                "(str!), guest_type ('qemu'|'lxc'), vmid (int!), action "
                "(start|stop|shutdown|reboot|suspend|resume). Output: {ok, upid} "
                "or {error}.",
)
async def cap_guest_action(
    cluster_id: str = "", node: str = "", guest_type: str = "",
    vmid: int = 0, action: str = "", trace_id=None,
) -> Dict:
    if action not in _GUEST_ACTIONS:
        return {"error": f"action must be one of {sorted(_GUEST_ACTIONS)}"}
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    upid, err = await _pve(
        rec, "POST", f"/nodes/{node}/{guest_type}/{vmid}/status/{action}")
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.guest.action", "cluster": cluster_id,
                      "node": node, "vmid": vmid, "action": action})
    return {"ok": True, "upid": upid}


# ═════════════════════════════════════════════════════════════════════════════
#  LIFECYCLE — clone / create / destroy + storage browse + SSH enrol
#  (drives the Provision panel's Proxmox tab; all go through the PVE API)
# ═════════════════════════════════════════════════════════════════════════════
async def _nextid(rec: Dict) -> int:
    data, _ = await _pve(rec, "GET", "/cluster/nextid")
    try:
        return int(data)
    except Exception:
        return 0


async def _guest_ip(rec: Dict, node: str, gtype: str, vmid: int) -> str:
    """Best-effort guest IPv4: LXC interfaces/config, QEMU guest agent."""
    if gtype == "lxc":
        data, _ = await _pve(rec, "GET", f"/nodes/{node}/lxc/{vmid}/interfaces")
        for it in (data or []):
            if it.get("name") in ("lo",):
                continue
            ip = (it.get("inet") or "").split("/")[0]
            if ip and not ip.startswith("127."):
                return ip
        cfg, _ = await _pve(rec, "GET", f"/nodes/{node}/lxc/{vmid}/config")
        for part in (cfg or {}).get("net0", "").split(","):
            if part.startswith("ip=") and part[3:] not in ("dhcp", ""):
                return part[3:].split("/")[0]
    else:
        data, _ = await _pve(
            rec, "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        result = data.get("result") if isinstance(data, dict) else data
        for it in (result or []):
            if it.get("name") in ("lo",):
                continue
            for a in it.get("ip-addresses", []) or []:
                ip = a.get("ip-address", "")
                if a.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                    return ip
    return ""


@capability(
    "proxmox.guest.clone",
    http_method="POST", http_path="/proxmox/guest/clone", http_tags=["proxmox"],
    memory="off",
    description="Clone a VM/CT or template. Inputs: cluster_id (str!), node (str!), "
                "guest_type ('qemu'|'lxc'), vmid (int! — source), name (str — new "
                "name/hostname), newid (int — 0=auto next free), full (bool=true), "
                "storage (str — full-clone target). Output: {ok, newid} or {error}.",
)
async def cap_guest_clone(cluster_id: str = "", node: str = "", guest_type: str = "",
                          vmid: int = 0, name: str = "", newid: int = 0,
                          full: bool = True, storage: str = "", trace_id=None) -> Dict:
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    newid = int(newid or 0) or await _nextid(rec)
    if not newid:
        return {"error": "could not allocate a VMID"}
    body: Dict[str, Any] = {"newid": newid, "full": 1 if full else 0}
    if name:
        body["name" if guest_type == "qemu" else "hostname"] = name
    if storage:
        body["storage"] = storage
    _, err = await _pve(rec, "POST", f"/nodes/{node}/{guest_type}/{vmid}/clone", data=body)
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.guest.clone", "cluster": cluster_id,
                      "vmid": vmid, "newid": newid})
    return {"ok": True, "newid": newid}


@capability(
    "proxmox.guest.destroy",
    http_method="POST", http_path="/proxmox/guest/destroy", http_tags=["proxmox"],
    memory="off",
    description="PERMANENTLY delete a STOPPED guest and its disks. Inputs: "
                "cluster_id (str!), node (str!), guest_type ('qemu'|'lxc'), vmid "
                "(int!), purge (bool=true — also drop it from backup/replication "
                "jobs and remove unreferenced disks). Output: {ok} or {error}.",
)
async def cap_guest_destroy(cluster_id: str = "", node: str = "", guest_type: str = "",
                            vmid: int = 0, purge: bool = True, trace_id=None) -> Dict:
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    q = "?purge=1&destroy-unreferenced-disks=1" if purge else ""
    _, err = await _pve(rec, "DELETE", f"/nodes/{node}/{guest_type}/{vmid}{q}")
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.guest.destroy", "cluster": cluster_id, "vmid": vmid})
    return {"ok": True}


@capability(
    "proxmox.lxc.create",
    http_method="POST", http_path="/proxmox/lxc/create", http_tags=["proxmox"],
    memory="off",
    description="Create an LXC container. Inputs: cluster_id (str!), node (str!), "
                "ostemplate (str! — volid, e.g. 'local:vztmpl/debian-12-…tar.zst'), "
                "hostname (str), storage (str='local-lvm'), cores (int=1), memory "
                "(int MB=512), disk (int GB=8), password (str), vmid (int 0=auto), "
                "net (str — default 'name=eth0,bridge=vmbr0,ip=dhcp'), unprivileged "
                "(bool=true), start (bool=true). Output: {ok, vmid} or {error}.",
)
async def cap_lxc_create(cluster_id: str = "", node: str = "", ostemplate: str = "",
                         hostname: str = "", storage: str = "local-lvm", cores: int = 1,
                         memory: int = 512, disk: int = 8, password: str = "",
                         vmid: int = 0, net: str = "", unprivileged: bool = True,
                         start: bool = True, trace_id=None) -> Dict:
    if not ostemplate:
        return {"error": "ostemplate (volid) required"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    vmid = int(vmid or 0) or await _nextid(rec)
    if not vmid:
        return {"error": "could not allocate a VMID"}
    body: Dict[str, Any] = {
        "vmid": vmid, "ostemplate": ostemplate,
        "hostname": hostname or f"ct-{vmid}", "storage": storage,
        "cores": int(cores or 1), "memory": int(memory or 512),
        "rootfs": f"{storage}:{int(disk or 8)}",
        "net0": net or "name=eth0,bridge=vmbr0,ip=dhcp",
        "unprivileged": 1 if unprivileged else 0,
        "start": 1 if start else 0,
    }
    if password:
        body["password"] = password
    _, err = await _pve(rec, "POST", f"/nodes/{node}/lxc", data=body)
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.lxc.create", "cluster": cluster_id, "vmid": vmid})
    return {"ok": True, "vmid": vmid}


@capability(
    "proxmox.storage.content",
    http_method="POST", http_path="/proxmox/storage/content", http_tags=["proxmox"],
    memory="off", silent=True,
    description="List a storage's content (e.g. browse CT templates / ISOs). "
                "Inputs: cluster_id (str!), node (str!), storage (str!), content "
                "(str — 'vztmpl'|'iso'|'images'|… optional filter). Output: "
                "{items:[{volid,format,size,content}]} or {error}.",
)
async def cap_storage_content(cluster_id: str = "", node: str = "", storage: str = "",
                              content: str = "", trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found", "items": []}
    if not (node and storage):
        return {"error": "node and storage required", "items": []}
    path = f"/nodes/{node}/storage/{storage}/content"
    if content:
        path += f"?content={quote(content)}"
    data, err = await _pve(rec, "GET", path)
    if data is None:
        return {"error": err, "items": []}
    return {"items": data}


@capability(
    "proxmox.guest.enroll",
    http_method="POST", http_path="/proxmox/guest/enroll", http_tags=["proxmox"],
    memory="off",
    description="Register a guest as an SSH host in Vera's shared credential store "
                "(used by remote exec, provisioning and SSH-managed Docker). "
                "Auto-detects the guest IP (LXC interfaces/config, QEMU guest agent) "
                "when not supplied. Inputs: cluster_id (str!), node (str!), "
                "guest_type ('qemu'|'lxc'), vmid (int!), user (str='root'), ip "
                "(str — blank=auto), password (str) OR key_path (str), port "
                "(int=22). Output: {ok, ip, ssh_host_id} or {error}.",
)
async def cap_guest_enroll(cluster_id: str = "", node: str = "", guest_type: str = "",
                           vmid: int = 0, user: str = "root", ip: str = "",
                           password: str = "", key_path: str = "", port: int = 22,
                           trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    if not ip:
        ip = await _guest_ip(rec, node, guest_type, int(vmid or 0))
    if not ip:
        return {"error": "could not auto-detect the guest IP — enter it manually "
                "(QEMU needs the guest agent running; LXC needs a reachable interface)."}
    save = _cap("exec.ssh.hosts.save")
    if not save:
        return {"error": "exec.ssh.hosts.save unavailable (execution module not loaded)"}
    res = await save(host=ip, user=user or "root", port=int(port or 22),
                     label=f"pve-{vmid}", auth="key" if key_path else "password",
                     password=password, key_path=key_path,
                     tags=f"proxmox,{cluster_id}")
    if not res.get("ok"):
        return {"error": res.get("error", "saving SSH credential failed")}
    hid = (res.get("host") or {}).get("id", "")
    await emit_event({"type": "proxmox.guest.enroll", "cluster": cluster_id,
                      "vmid": vmid, "ip": ip})
    return {"ok": True, "ip": ip, "ssh_host_id": hid}


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES — provisioning lifecycle (create / clone / destroy / enroll)
# ═════════════════════════════════════════════════════════════════════════════
import re as _re


async def _guest_ip(rec: Dict, node: str, guest_type: str, vmid: int) -> str:
    """Best-effort guest IP. LXC: parse net0 from config. QEMU: try the guest
    agent's network interfaces (if installed). Returns '' when undeterminable."""
    try:
        if guest_type == "lxc":
            cfg, err = await _pve(rec, "GET", f"/nodes/{node}/lxc/{vmid}/config")
            if cfg and isinstance(cfg, dict):
                net0 = cfg.get("net0", "")
                m = _re.search(r"ip=([0-9.]+)", net0)
                if m:
                    return m.group(1)
        else:
            data, err = await _pve(
                rec, "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
            ifaces = (data or {}).get("result", []) if isinstance(data, dict) else []
            for iface in ifaces:
                if iface.get("name") in ("lo", "lo0"):
                    continue
                for a in iface.get("ip-addresses", []) or []:
                    ip = a.get("ip-address", "")
                    if a.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                        return ip
    except Exception as e:
        log.debug("guest_ip(%s/%s): %s", node, vmid, e)
    return ""


@capability(
    "proxmox.nextid",
    http_method="POST", http_path="/proxmox/nextid", http_tags=["proxmox"],
    memory="off", silent=True,
    description="Return the next free VMID in the cluster. Input: cluster_id "
                "(str!). Output: {vmid}.",
)
async def cap_nextid(cluster_id: str = "", trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    data, err = await _pve(rec, "GET", "/cluster/nextid")
    if err:
        return {"error": err}
    return {"vmid": int(data) if data else 0}


@capability(
    "proxmox.storage.content",
    http_method="POST", http_path="/proxmox/storage/content", http_tags=["proxmox"],
    memory="off", silent=True,
    description="List storage content of a given type on a node (default "
                "'vztmpl' = LXC templates). Inputs: cluster_id (str!), node (str!), "
                "storage (str!), content (str='vztmpl'). Output: {items:[{volid,"
                "format,size}]}.",
)
async def cap_storage_content(cluster_id: str = "", node: str = "",
                              storage: str = "", content: str = "vztmpl",
                              trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    data, err = await _pve(
        rec, "GET",
        f"/nodes/{node}/storage/{storage}/content?content={content}")
    if err:
        return {"error": err}
    items = [{"volid": i.get("volid", ""), "format": i.get("format", ""),
              "size": i.get("size", 0)} for i in (data or [])]
    return {"items": items}


@capability(
    "proxmox.guest.clone",
    http_method="POST", http_path="/proxmox/guest/clone", http_tags=["proxmox"],
    memory="off",
    description="Clone an existing guest or template to a new VMID. Inputs: "
                "cluster_id (str!), node (str!), guest_type ('qemu'|'lxc'), vmid "
                "(int! source), newid (int! — blank/0 = auto), name (str), full "
                "(bool=True), storage (str), target (str — target node). "
                "Output: {ok, newid, upid}.",
)
async def cap_guest_clone(cluster_id: str = "", node: str = "",
                          guest_type: str = "", vmid: int = 0, newid: int = 0,
                          name: str = "", full: bool = True, storage: str = "",
                          target: str = "", trace_id=None) -> Dict:
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    if not newid:
        nid, err = await _pve(rec, "GET", "/cluster/nextid")
        if err:
            return {"error": f"nextid: {err}"}
        newid = int(nid)
    data: Dict = {"newid": int(newid), "full": 1 if full else 0}
    if name:
        data["hostname" if guest_type == "lxc" else "name"] = name
    if storage:
        data["storage"] = storage
    if target:
        data["target"] = target
    upid, err = await _pve(
        rec, "POST", f"/nodes/{node}/{guest_type}/{vmid}/clone", data)
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.guest.clone", "cluster": cluster_id,
                      "node": node, "source": vmid, "newid": newid})
    return {"ok": True, "newid": int(newid), "upid": upid}


@capability(
    "proxmox.lxc.create",
    http_method="POST", http_path="/proxmox/lxc/create", http_tags=["proxmox"],
    memory="off",
    description="Create a new LXC container from an OS template. Inputs: "
                "cluster_id (str!), node (str!), vmid (int — blank/0 = auto), "
                "ostemplate (str! volid, e.g. 'local:vztmpl/debian-12.tar.zst'), "
                "hostname (str), storage (str='local-lvm'), cores (int=1), memory "
                "(int=512 MB), disk (int=8 GB), password (str), ssh_public_keys "
                "(str), net0 (str — default DHCP on vmbr0), unprivileged (bool=True), "
                "start (bool=True). Output: {ok, vmid, upid}.",
)
async def cap_lxc_create(cluster_id: str = "", node: str = "", vmid: int = 0,
                         ostemplate: str = "", hostname: str = "",
                         storage: str = "local-lvm", cores: int = 1,
                         memory: int = 512, disk: int = 8, password: str = "",
                         ssh_public_keys: str = "", net0: str = "",
                         unprivileged: bool = True, start: bool = True,
                         trace_id=None) -> Dict:
    if not ostemplate:
        return {"error": "ostemplate required (a vztmpl volid)"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    if not vmid:
        nid, err = await _pve(rec, "GET", "/cluster/nextid")
        if err:
            return {"error": f"nextid: {err}"}
        vmid = int(nid)
    data: Dict = {
        "vmid": int(vmid), "ostemplate": ostemplate,
        "storage": storage, "cores": int(cores), "memory": int(memory),
        "rootfs": f"{storage}:{int(disk)}",
        "net0": net0 or "name=eth0,bridge=vmbr0,ip=dhcp",
        "unprivileged": 1 if unprivileged else 0,
        "start": 1 if start else 0,
    }
    if hostname:
        data["hostname"] = hostname
    if password:
        data["password"] = password
    if ssh_public_keys:
        data["ssh-public-keys"] = ssh_public_keys
    upid, err = await _pve(rec, "POST", f"/nodes/{node}/lxc", data)
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.lxc.create", "cluster": cluster_id,
                      "node": node, "vmid": vmid})
    return {"ok": True, "vmid": int(vmid), "upid": upid}


@capability(
    "proxmox.guest.destroy",
    http_method="POST", http_path="/proxmox/guest/destroy", http_tags=["proxmox"],
    memory="off",
    description="Destroy (permanently delete) a guest. Inputs: cluster_id (str!), "
                "node (str!), guest_type ('qemu'|'lxc'), vmid (int!), purge "
                "(bool=True — also remove from backup/HA configs). The guest "
                "should be stopped first. Output: {ok, upid}.",
)
async def cap_guest_destroy(cluster_id: str = "", node: str = "",
                            guest_type: str = "", vmid: int = 0,
                            purge: bool = True, trace_id=None) -> Dict:
    if guest_type not in ("qemu", "lxc"):
        return {"error": "guest_type must be 'qemu' or 'lxc'"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    path = f"/nodes/{node}/{guest_type}/{vmid}"
    if purge:
        path += "?purge=1&destroy-unreferenced-disks=1"
    upid, err = await _pve(rec, "DELETE", path)
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.guest.destroy", "cluster": cluster_id,
                      "node": node, "vmid": vmid})
    return {"ok": True, "upid": upid}


@capability(
    "proxmox.guest.enroll",
    http_method="POST", http_path="/proxmox/guest/enroll", http_tags=["proxmox"],
    memory="off",
    description="Register a Proxmox guest as a managed SSH host (the same registry "
                "the provisioning + terminal use), resolving its IP automatically "
                "when possible. Inputs: cluster_id (str!), node (str!), guest_type "
                "('qemu'|'lxc'), vmid (int!), user (str='root'), password (str), "
                "key_path (str), ip (str — override auto-detect), port (int=22), "
                "label (str). Output: {ok, ssh_host_id, ip, host}.",
)
async def cap_guest_enroll(cluster_id: str = "", node: str = "",
                           guest_type: str = "", vmid: int = 0, user: str = "root",
                           password: str = "", key_path: str = "", ip: str = "",
                           port: int = 22, label: str = "", trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    addr = ip or await _guest_ip(rec, node, guest_type, int(vmid))
    if not addr:
        return {"error": "could not determine guest IP — pass `ip` explicitly "
                         "(QEMU guests need the guest agent installed)"}
    save = _orch.CAPABILITY_REGISTRY.get("exec.ssh.hosts.save")
    if not save:
        return {"error": "exec.ssh.hosts.save capability unavailable"}
    res = await save["raw"](
        host=addr, user=user or "root", port=int(port or 22),
        label=label or f"pve:{vmid}@{node}",
        auth="key" if key_path else "password",
        password=password, key_path=key_path,
        tags=f"proxmox,{guest_type},{node}", trace_id=None,
    )
    if not res.get("ok"):
        return {"error": res.get("error", "save failed")}
    sid = (res.get("host") or {}).get("id", "")
    await emit_event({"type": "proxmox.guest.enroll", "cluster": cluster_id,
                      "vmid": vmid, "ip": addr, "ssh_host_id": sid})
    return {"ok": True, "ssh_host_id": sid, "ip": addr, "host": addr}


@capability(
    "proxmox.console.ticket",
    http_method="POST", http_path="/proxmox/console/ticket", http_tags=["proxmox"],
    memory="off",
    description="Open a guest console session. Inputs: cluster_id (str!), node, "
                "guest_type ('qemu'|'lxc'), vmid, mode ('term'|'vnc'). When "
                "console credentials + the websockets dep are present this mints "
                "a short-lived Proxmox console ticket and returns {proxy_available"
                ":true, ws_path, password}. The browser opens ws_path (xterm for "
                "term, noVNC for vnc); for vnc, `password` is the per-session VNC "
                "ticket. Otherwise {proxy_available:false, deeplink_url} → the UI "
                "opens Proxmox's own console. Always includes deeplink_url.",
)
async def cap_console_ticket(
    cluster_id: str = "", node: str = "", guest_type: str = "",
    vmid: int = 0, mode: str = "term", trace_id=None,
) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    has_creds = bool(rec.get("console_user") and rec.get("console_password"))
    host, port = _host_port(rec)
    console_kind = "kvm" if guest_type == "qemu" else "lxc"
    deeplink = (f"https://{host}:{port}/?console={console_kind}&novnc=1"
                f"&vmid={vmid}&node={node}&resize=scale")
    out = {"proxy_available": False, "needs_console_creds": not has_creds,
           "deeplink_url": deeplink}
    if not (has_creds and HAS_WS):
        return out

    # Mint the console ticket now; the WS route consumes it within seconds.
    info, err = await _console_open(rec, node, guest_type, str(vmid), mode)
    if not info:
        out["error"] = err
        return out
    _prune_sessions()
    import time
    sid = uuid.uuid4().hex
    # NOTE: `**info` carries the *proxy* port (termproxy/vncproxy, e.g. 5900) under
    # the key "port". The browser-facing vncwebsocket, however, lives on the PVE
    # *web* port (8006) and takes the proxy port only as the ?port= query arg. Keep
    # them in distinct keys ("web_port" vs "port") so `**info` can't clobber the web
    # port — doing so connected the WS to :5900 and produced ECONNREFUSED (errno 111).
    _CONSOLE_SESSIONS[sid] = {
        "host": host, "web_port": port, "node": node, "kind": guest_type,
        "vmid": str(vmid), "mode": mode, "verify_tls": bool(rec.get("verify_tls")),
        "ts": time.time(), **info,
    }
    out.update({
        "proxy_available": True,
        "ws_path": f"/proxmox/console/ws/{sid}",
        "password": info["vncticket"] if mode == "vnc" else "",
    })
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  CONSOLE WEBSOCKET PROXY
# ═════════════════════════════════════════════════════════════════════════════
async def _console_open(rec: Dict, node: str, kind: str, vmid: str,
                        mode: str) -> Tuple[Optional[Dict], str]:
    """Mint a PVEAuthCookie ticket then a termproxy/vncproxy session.
    Returns ({vncticket, port, user, cookie}, "") or (None, error)."""
    user = rec.get("console_user", "")
    pw = rec.get("console_password", "")
    if not user or not pw:
        return None, "no console credentials configured"
    # Proxmox /access/ticket needs a realm-qualified username (e.g. root@pam).
    # Be forgiving if the operator entered just "root".
    if "@" not in user:
        user = user + "@pam"
    base = _api_base(rec)
    verify = bool(rec.get("verify_tls"))
    try:
        async with httpx.AsyncClient(timeout=20, verify=verify) as c:
            tr = await c.post(base + "/access/ticket",
                              data={"username": user, "password": pw})
            if tr.status_code == 401:
                return None, (f"auth 401 for {user} — wrong console user/password, "
                              "or the user lacks console (PVEVMConsole) permission. "
                              "Use a realm-qualified login like root@pam.")
            if tr.status_code >= 400:
                return None, f"auth HTTP {tr.status_code}: {tr.text[:160]}"
            td = tr.json().get("data") or {}
            cookie = td.get("ticket", "")
            csrf = td.get("CSRFPreventionToken", "")
            if not cookie:
                return None, "auth returned no ticket"
            if kind in ("qemu", "lxc"):
                verb = "vncproxy" if mode == "vnc" else "termproxy"
                ppath = f"/nodes/{node}/{kind}/{vmid}/{verb}"
            else:
                verb = "vncshell" if mode == "vnc" else "termproxy"
                ppath = f"/nodes/{node}/{verb}"
            body: Dict[str, Any] = {}
            if mode == "vnc":
                body["websocket"] = 1
            pr = await c.post(base + ppath,
                              headers={"Cookie": f"PVEAuthCookie={cookie}",
                                       "CSRFPreventionToken": csrf},
                              data=body)
            if pr.status_code >= 400:
                return None, f"{verb} HTTP {pr.status_code}: {pr.text[:200]}"
            pd = pr.json().get("data") or {}
            return {"vncticket": pd.get("ticket", ""),
                    "port": pd.get("port", ""),
                    "user": pd.get("user", user),
                    "cookie": cookie}, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


async def _ws_connect(uri: str, sslctx, cookie: str):
    """websockets.connect across versions (additional_headers vs extra_headers)."""
    headers = {"Cookie": f"PVEAuthCookie={cookie}"}
    kw = dict(ssl=sslctx, subprotocols=["binary"], max_size=None)
    try:
        return await websockets.connect(uri, additional_headers=headers, **kw)
    except TypeError:
        return await websockets.connect(uri, extra_headers=headers, **kw)


async def _pipe(client: WebSocket, up) -> None:
    """Bidirectional byte/text pump between the browser WS and the PVE WS."""
    async def c2u():
        try:
            while True:
                m = await client.receive()
                if m.get("type") == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    await up.send(m["bytes"])
                elif m.get("text") is not None:
                    await up.send(m["text"])
        except Exception:
            pass
        finally:
            try:
                await up.close()
            except Exception:
                pass

    async def u2c():
        try:
            async for msg in up:
                if isinstance(msg, (bytes, bytearray)):
                    await client.send_bytes(bytes(msg))
                else:
                    await client.send_text(msg)
        except Exception:
            pass
        finally:
            try:
                await client.close()
            except Exception:
                pass

    await asyncio.gather(c2u(), u2c())


@APP.websocket("/proxmox/console/ws/{sid}")
async def proxmox_console_ws(websocket: WebSocket, sid: str):
    await websocket.accept(subprotocol="binary")
    sess = _CONSOLE_SESSIONS.pop(sid, None)   # single-use
    if not sess or not HAS_WS:
        try:
            await websocket.send_text("\r\n[vera] console session expired — reopen.\r\n")
        finally:
            await websocket.close(code=4404)
        return

    node, kind, vmid, mode = sess["node"], sess["kind"], sess["vmid"], sess["mode"]
    if kind in ("qemu", "lxc"):
        vpath = f"/api2/json/nodes/{node}/{kind}/{vmid}/vncwebsocket"
    else:
        vpath = f"/api2/json/nodes/{node}/vncwebsocket"
    uri = (f"wss://{sess['host']}:{sess['web_port']}{vpath}"
           f"?port={sess['port']}&vncticket={quote(sess['vncticket'])}")

    sslctx = ssl.create_default_context()
    if not sess.get("verify_tls"):
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE

    try:
        up = await _ws_connect(uri, sslctx, sess["cookie"])
    except Exception as e:
        log.warning("proxmox console upstream connect failed: %s", e)
        try:
            await websocket.send_text(f"\r\n[vera] upstream connect failed: {e}\r\n")
        except Exception:
            pass
        await websocket.close(code=4502)
        return

    try:
        # Proxmox term consoles expect the first frame to be "<user>:<ticket>\n";
        # noVNC (vnc mode) authenticates with the ticket as the VNC password,
        # which the browser supplies — so no server-side handshake there.
        if mode == "term":
            await up.send(f"{sess['user']}:{sess['vncticket']}\n")
        await _pipe(websocket, up)
    finally:
        try:
            await up.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  FIREWALL  (PVE firewall API — cluster / node / guest scoped rules)
# ═════════════════════════════════════════════════════════════════════════════
def _fw_base(scope: str, node: str, guest_type: str, vmid) -> Optional[str]:
    if scope == "cluster":
        return "/cluster/firewall"
    if scope == "node":
        return f"/nodes/{node}/firewall" if node else None
    if scope == "guest":
        if not (node and guest_type and vmid):
            return None
        return f"/nodes/{node}/{guest_type}/{vmid}/firewall"
    return None


@capability(
    "proxmox.fw.rules.list",
    http_method="POST", http_path="/proxmox/fw/rules/list", http_tags=["proxmox"],
    memory="off", silent=True,
    description="List firewall rules at a scope. Inputs: cluster_id (str!), scope "
                "('cluster'|'node'|'guest'), node, guest_type ('qemu'|'lxc'), vmid. "
                "Output: {rules:[{pos,action,type,source,dest,proto,dport,enable,"
                "comment}]} or {error}.",
)
async def cap_fw_rules_list(cluster_id: str = "", scope: str = "guest",
                            node: str = "", guest_type: str = "", vmid: int = 0,
                            trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found", "rules": []}
    base = _fw_base(scope, node, guest_type, vmid)
    if not base:
        return {"error": "missing node/guest_type/vmid for scope", "rules": []}
    data, err = await _pve(rec, "GET", base + "/rules")
    if data is None:
        return {"error": err, "rules": []}
    return {"rules": data}


@capability(
    "proxmox.fw.rule.add",
    http_method="POST", http_path="/proxmox/fw/rule/add", http_tags=["proxmox"],
    memory="off",
    description="Add a firewall rule (e.g. to ALLOW or DROP a connection). Inputs: "
                "cluster_id (str!), scope ('cluster'|'node'|'guest'), node, "
                "guest_type, vmid, action ('ACCEPT'|'DROP'|'REJECT'), type "
                "('in'|'out', default 'in'), source (CIDR/IP/ipset), dest, proto "
                "('tcp'|'udp'…), dport (str), comment, enable (bool=true). "
                "Output: {ok} or {error}.",
)
async def cap_fw_rule_add(cluster_id: str = "", scope: str = "guest",
                          node: str = "", guest_type: str = "", vmid: int = 0,
                          action: str = "ACCEPT", type: str = "in",
                          source: str = "", dest: str = "", proto: str = "",
                          dport: str = "", comment: str = "", enable: bool = True,
                          trace_id=None) -> Dict:
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    base = _fw_base(scope, node, guest_type, vmid)
    if not base:
        return {"error": "missing node/guest_type/vmid for scope"}
    body = {"action": action, "type": type, "enable": 1 if enable else 0}
    for k, v in (("source", source), ("dest", dest), ("proto", proto),
                 ("dport", dport), ("comment", comment)):
        if v:
            body[k] = v
    _, err = await _pve(rec, "POST", base + "/rules", data=body)
    if err:
        return {"error": err}
    await emit_event({"type": "proxmox.fw.rule.added", "cluster": cluster_id,
                      "scope": scope, "action": action})
    return {"ok": True}


@capability(
    "proxmox.fw.rule.delete",
    http_method="POST", http_path="/proxmox/fw/rule/delete", http_tags=["proxmox"],
    memory="off",
    description="Delete a firewall rule by position. Inputs: cluster_id (str!), "
                "scope, node, guest_type, vmid, pos (int!). Output: {ok} or {error}.",
)
async def cap_fw_rule_delete(cluster_id: str = "", scope: str = "guest",
                             node: str = "", guest_type: str = "", vmid: int = 0,
                             pos: int = -1, trace_id=None) -> Dict:
    if pos < 0:
        return {"error": "pos required"}
    rec = await _get_cluster(cluster_id, opened=True)
    if not rec:
        return {"error": "cluster not found"}
    base = _fw_base(scope, node, guest_type, vmid)
    if not base:
        return {"error": "missing node/guest_type/vmid for scope"}
    _, err = await _pve(rec, "DELETE", f"{base}/rules/{pos}")
    if err:
        return {"error": err}
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/proxmox/panel", include_in_schema=False)
async def _proxmox_panel():
    p = _HERE / "proxmox_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>proxmox_panel.html not found</p>")


# Standalone top-level tab retired — this UI is embedded as the Dashboard sub-tab
# of the workers/Ollama panel's Proxmox pane. The /proxmox/panel route is kept.
register_ui = (lambda *a, **k: None)
register_ui(
    "proxmox-panel",
    "Proxmox",
    "▤",
    """<div id="proxmox-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/proxmox/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "proxmox.cluster.save", "proxmox.cluster.list", "proxmox.cluster.delete",
        "proxmox.status", "proxmox.guest.action", "proxmox.console.ticket",
        "proxmox.fw.rules.list", "proxmox.fw.rule.add", "proxmox.fw.rule.delete",
    ],
    mode="tab",
    tab_order=55,
)


log.info("proxmox_capabilities ready — console_proxy=%s", HAS_WS)
