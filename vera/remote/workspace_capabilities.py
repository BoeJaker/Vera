"""
workspace_capabilities.py — Vera Workspaces, remote FS, app-mount & MCP (Phase 2)
=================================================================================

Builds on the Phase-1 remote-access foundation (remote_capabilities.py) to give
each container / VM / host a **workspace**: a saved, VeraDash-style layout mixing
terminals, a file explorer, embedded Vera panels (IDE, notebook, monitor), and
mounted apps — all bound to one target.

Four capability families:

  fs.*         Remote filesystem browse/edit against a saved connection, so the
               file-explorer widget (and agents) can traverse a container/VM's
               disk. Runs through conn.exec (docker exec / ssh), portable across
               busybox + coreutils.
                 fs.list / fs.read / fs.write / fs.mkdir / fs.delete / fs.stat

  workspace.*  Persisted per-target layouts (Redis vera:remote:workspaces).
                 workspace.list / get / save / delete

  app.*        Detect apps running on a target and mount any of them as a panel
               through Vera's reverse proxy (so a phone off-LAN can operate them).
                 app.detect / mount / list / unmount / pair_mcp
               Reverse proxy: /remote/app/{app_id}/{path}

  mcp.detect   Probe a target for network MCP servers and register+pair the ones
               it finds into the existing mcp.catalog.

Agnostic of the running app; a KNOWN_APPS table only *labels* well-known ports.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)

log = logging.getLogger("vera.remote.workspace")

_HERE = Path(__file__).parent
KEY_WORKSPACES = "vera:remote:workspaces"
KEY_APPS = "vera:remote:apps"


def _redis():
    return getattr(_orch, "REDIS", None)


def _cap_raw(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


# ═════════════════════════════════════════════════════════════════════════════
#  REMOTE FILESYSTEM  (fs.*)  — runs over conn.exec (docker exec / ssh)
# ═════════════════════════════════════════════════════════════════════════════
_FS_MAX_READ = 512 * 1024   # cap single-file reads


async def _conn_exec(conn_id: str, command: str, *, kind: str = "",
                     docker_host_id: str = "", container: str = "",
                     ssh_host_id: str = "", timeout: int = 60) -> Dict:
    fn = _cap_raw("conn.exec")
    if not fn:
        return {"ok": False, "error": "conn.exec unavailable (remote module not loaded)"}
    return await fn(conn_id=conn_id, kind=kind, docker_host_id=docker_host_id,
                    container=container, ssh_host_id=ssh_host_id,
                    command=command, timeout=timeout)


def _parse_ls(out: str) -> List[Dict]:
    """Parse `ls -la` (coreutils + busybox). Returns [{name,type,size,perms,link}]."""
    entries: List[Dict] = []
    for line in (out or "").splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("total "):
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        perms, _links, _owner, _group, size, _mon, _day, _tm, name = parts
        typ = ("dir" if perms[0] == "d" else
               "link" if perms[0] == "l" else
               "file")
        link_target = ""
        if typ == "link" and " -> " in name:
            name, link_target = name.split(" -> ", 1)
        if name in (".", ".."):
            continue
        try:
            sz = int(size)
        except Exception:
            sz = 0
        entries.append({"name": name, "type": typ, "size": sz,
                        "perms": perms, "link": link_target})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


@capability(
    "fs.list",
    http_method="POST", http_path="/remote/fs/list", http_tags=["remote", "fs"],
    memory="off", silent=True,
    description="List a directory on a connection's target (container/VM/host). "
                "Inputs: conn_id (str) OR (kind + docker_host_id/container or "
                "ssh_host_id), path (str='/'). Output: {ok, path, entries:[{name,"
                "type,size,perms,link}]}.",
)
async def cap_fs_list(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                      container: str = "", ssh_host_id: str = "", path: str = "/",
                      trace_id=None) -> Dict:
    path = path or "/"
    res = await _conn_exec(conn_id, f"ls -la -- {shlex.quote(path)}", kind=kind,
                           docker_host_id=docker_host_id, container=container,
                           ssh_host_id=ssh_host_id, timeout=30)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "ls failed",
                "path": path, "entries": []}
    return {"ok": True, "path": path, "entries": _parse_ls(res.get("stdout", ""))}


@capability(
    "fs.stat",
    http_method="POST", http_path="/remote/fs/stat", http_tags=["remote", "fs"],
    memory="off", silent=True,
    description="Stat a path on a target. Inputs: conn_id (or target params), "
                "path (str!). Output: {ok, exists, type, size}.",
)
async def cap_fs_stat(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                      container: str = "", ssh_host_id: str = "", path: str = "",
                      trace_id=None) -> Dict:
    if not path:
        return {"ok": False, "error": "path required"}
    q = shlex.quote(path)
    cmd = (f"if [ -d {q} ]; then echo dir; elif [ -e {q} ]; then "
           f"echo file; else echo none; fi; wc -c < {q} 2>/dev/null || echo 0")
    res = await _conn_exec(conn_id, cmd, kind=kind, docker_host_id=docker_host_id,
                           container=container, ssh_host_id=ssh_host_id, timeout=20)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "stat failed"}
    lines = (res.get("stdout", "") or "").strip().splitlines()
    typ = lines[0].strip() if lines else "none"
    try:
        size = int(lines[1]) if len(lines) > 1 else 0
    except Exception:
        size = 0
    return {"ok": True, "exists": typ != "none",
            "type": ("dir" if typ == "dir" else "file" if typ == "file" else ""),
            "size": size}


@capability(
    "fs.read",
    http_method="POST", http_path="/remote/fs/read", http_tags=["remote", "fs"],
    memory="off",
    description="Read a file from a target (base64-safe for arbitrary bytes). "
                "Inputs: conn_id (or target params), path (str!), max_bytes "
                "(int=524288). Output: {ok, path, size, text, b64, truncated, binary}.",
)
async def cap_fs_read(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                      container: str = "", ssh_host_id: str = "", path: str = "",
                      max_bytes: int = _FS_MAX_READ, trace_id=None) -> Dict:
    if not path:
        return {"ok": False, "error": "path required"}
    n = max(1, min(int(max_bytes or _FS_MAX_READ), _FS_MAX_READ))
    q = shlex.quote(path)
    # head -c bounds the read; base64 makes the transport binary-safe.
    res = await _conn_exec(conn_id, f"head -c {n + 1} {q} | base64", kind=kind,
                           docker_host_id=docker_host_id, container=container,
                           ssh_host_id=ssh_host_id, timeout=45)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "read failed"}
    b64 = "".join((res.get("stdout", "") or "").split())
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:
        raw = b""
    truncated = len(raw) > n
    raw = raw[:n]
    try:
        text = raw.decode("utf-8")
        binary = False
    except Exception:
        text = raw.decode("utf-8", "replace")
        binary = b"\x00" in raw
    return {"ok": True, "path": path, "size": len(raw),
            "text": text, "b64": base64.b64encode(raw).decode(),
            "truncated": truncated, "binary": binary}


@capability(
    "fs.write",
    http_method="POST", http_path="/remote/fs/write", http_tags=["remote", "fs"],
    description="Write a file on a target (content transported base64 so any "
                "bytes/special chars survive). Inputs: conn_id (or target params), "
                "path (str!), content (str — utf-8 text) OR b64 (str — raw bytes). "
                "Output: {ok, path, bytes}.",
)
async def cap_fs_write(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                       container: str = "", ssh_host_id: str = "", path: str = "",
                       content: str = "", b64: str = "", trace_id=None) -> Dict:
    if not path:
        return {"ok": False, "error": "path required"}
    if b64:
        payload = "".join(b64.split())
        try:
            nbytes = len(base64.b64decode(payload, validate=False))
        except Exception:
            return {"ok": False, "error": "invalid b64"}
    else:
        data = (content or "").encode("utf-8")
        payload = base64.b64encode(data).decode()
        nbytes = len(data)
    q = shlex.quote(path)
    cmd = f"printf %s {shlex.quote(payload)} | base64 -d > {q}"
    res = await _conn_exec(conn_id, cmd, kind=kind, docker_host_id=docker_host_id,
                           container=container, ssh_host_id=ssh_host_id, timeout=45)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "write failed"}
    await emit_event({"type": "remote.fs.write", "path": path, "bytes": nbytes})
    return {"ok": True, "path": path, "bytes": nbytes}


@capability(
    "fs.mkdir",
    http_method="POST", http_path="/remote/fs/mkdir", http_tags=["remote", "fs"],
    description="Create a directory (mkdir -p) on a target. Inputs: conn_id (or "
                "target params), path (str!). Output: {ok, path}.",
)
async def cap_fs_mkdir(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                       container: str = "", ssh_host_id: str = "", path: str = "",
                       trace_id=None) -> Dict:
    if not path:
        return {"ok": False, "error": "path required"}
    res = await _conn_exec(conn_id, f"mkdir -p -- {shlex.quote(path)}", kind=kind,
                           docker_host_id=docker_host_id, container=container,
                           ssh_host_id=ssh_host_id, timeout=20)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "mkdir failed"}
    return {"ok": True, "path": path}


@capability(
    "fs.delete",
    http_method="POST", http_path="/remote/fs/delete", http_tags=["remote", "fs"],
    description="Delete a file or directory on a target. Inputs: conn_id (or "
                "target params), path (str!), recursive (bool=false — required to "
                "remove a non-empty directory). Output: {ok, path}.",
)
async def cap_fs_delete(conn_id: str = "", kind: str = "", docker_host_id: str = "",
                        container: str = "", ssh_host_id: str = "", path: str = "",
                        recursive: bool = False, trace_id=None) -> Dict:
    if not path or path.strip() in ("/", "//"):
        return {"ok": False, "error": "refusing to delete an empty or root path"}
    flag = "-rf" if recursive else "-f"
    res = await _conn_exec(conn_id, f"rm {flag} -- {shlex.quote(path)}", kind=kind,
                           docker_host_id=docker_host_id, container=container,
                           ssh_host_id=ssh_host_id, timeout=30)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "delete failed"}
    await emit_event({"type": "remote.fs.delete", "path": path})
    return {"ok": True, "path": path}


# ═════════════════════════════════════════════════════════════════════════════
#  WORKSPACES  (workspace.*)
# ═════════════════════════════════════════════════════════════════════════════
# A workspace record:
#   {id, label, target:{kind, conn_id, ...}, widgets:[{id,type,x,y,w,h,config}],
#    created, updated}
# widget.type ∈ terminal | files | app | panel   (panel embeds a Vera panel by id)
async def _all_workspaces() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_WORKSPACES)
    except Exception:
        return []
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda w: (w.get("label") or w.get("id") or ""))
    return out


@capability(
    "workspace.list",
    http_method="GET", http_path="/remote/workspace/list", http_tags=["remote"],
    memory="off", silent=True,
    description="List saved workspaces. Output: {workspaces:[{id,label,target,"
                "widget_count}], count}.",
)
async def cap_workspace_list(trace_id=None) -> Dict:
    ws = await _all_workspaces()
    return {"workspaces": [{"id": w.get("id"), "label": w.get("label"),
                            "target": w.get("target", {}),
                            "widget_count": len(w.get("widgets", [])),
                            "updated": w.get("updated")} for w in ws],
            "count": len(ws)}


@capability(
    "workspace.get",
    http_method="GET", http_path="/remote/workspace/get", http_tags=["remote"],
    memory="off", silent=True,
    description="Get a workspace's full definition (layout + widgets). "
                "Input: id (str!). Output: {ok, workspace}.",
)
async def cap_workspace_get(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    raw = await r.hget(KEY_WORKSPACES, id)
    if not raw:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "workspace": json.loads(raw)}


@capability(
    "workspace.save",
    http_method="POST", http_path="/remote/workspace/save", http_tags=["remote"],
    description="Create/update a workspace. Inputs: id (str — update if given), "
                "label (str!), target (object — {kind, conn_id, ...}), widgets "
                "(list — [{id,type,x,y,w,h,config}]). Output: {ok, workspace}.",
)
async def cap_workspace_save(id: str = "", label: str = "",
                             target: Optional[Dict] = None,
                             widgets: Optional[List] = None, trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"ok": False, "error": "store unavailable"}
    wid = id or uuid.uuid4().hex[:12]
    existing = {}
    if id:
        raw = await r.hget(KEY_WORKSPACES, id)
        existing = json.loads(raw) if raw else {}
    rec = dict(existing) if existing else {"id": wid, "created": now_iso()}
    rec["id"] = wid
    if label:
        rec["label"] = label
    elif not rec.get("label"):
        rec["label"] = f"workspace-{wid[:6]}"
    if target is not None:
        rec["target"] = target
    if widgets is not None:
        rec["widgets"] = widgets
    rec.setdefault("target", {})
    rec.setdefault("widgets", [])
    rec["updated"] = now_iso()
    await r.hset(KEY_WORKSPACES, wid, json.dumps(rec))
    await emit_event({"type": "remote.workspace.saved", "id": wid, "label": rec["label"]})
    return {"ok": True, "workspace": rec}


@capability(
    "workspace.delete",
    http_method="POST", http_path="/remote/workspace/delete", http_tags=["remote"],
    description="Delete a workspace by id. Input: id (str!). Output: {ok}.",
)
async def cap_workspace_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    await r.hdel(KEY_WORKSPACES, id)
    return {"ok": True, "deleted": id}


# ═════════════════════════════════════════════════════════════════════════════
#  APP DETECTION + MOUNT (reverse proxy)  (app.*)
# ═════════════════════════════════════════════════════════════════════════════
# port → (label, icon, scheme). Only *labels* well-known ports — mounting works
# for anything regardless of whether it is in this table.
KNOWN_APPS: Dict[int, Tuple[str, str, str]] = {
    3000:  ("Grafana / Flowise", "📊", "http"),
    3001:  ("App", "🔧", "http"),
    5678:  ("n8n", "🔗", "http"),
    8080:  ("qBittorrent / Web", "⬇", "http"),
    8081:  ("Web UI", "🌐", "http"),
    8083:  ("Web UI", "🌐", "http"),
    8123:  ("Home Assistant", "🏠", "http"),
    9000:  ("Portainer", "🐳", "http"),
    9443:  ("Portainer (TLS)", "🐳", "https"),
    32400: ("Plex", "🎬", "http"),
    5601:  ("Kibana", "📈", "http"),
    9090:  ("Prometheus", "🔥", "http"),
    3100:  ("Loki", "📜", "http"),
    8096:  ("Jellyfin", "🎞", "http"),
    6767:  ("Bazarr", "💬", "http"),
    7878:  ("Radarr", "🎥", "http"),
    8989:  ("Sonarr", "📺", "http"),
    2283:  ("Immich", "🖼", "http"),
    8384:  ("Syncthing", "🔄", "http"),
}
_DETECT_PORTS = sorted(KNOWN_APPS.keys()) + [80, 443, 8000, 8888, 5000]


async def _conn_host_addr(conn_id: str) -> str:
    """Resolve the browser/orchestrator-reachable host address for a connection.
    ssh/proxmox → the ssh host's address; docker → the docker host's address."""
    remote = next((m for n, m in list(sys.modules.items())
                   if m and n.endswith("remote_capabilities") and hasattr(m, "_get_conn")),
                  None)
    rec = await remote._get_conn(conn_id) if remote else None
    if not rec:
        return ""
    if rec.get("kind") in ("ssh", "proxmox") and rec.get("ssh_host_id"):
        s_list = _cap_raw("exec.ssh.hosts.list")
        if s_list:
            for h in (await s_list()).get("hosts", []):
                if h.get("id") == rec["ssh_host_id"]:
                    return h.get("host", "")
    if rec.get("kind") == "docker":
        d_hosts = _cap_raw("docker.hosts.list")
        if d_hosts:
            for h in (await d_hosts()).get("hosts", []):
                if h.get("id") == (rec.get("docker_host_id") or "local"):
                    url = h.get("url", "")
                    if url:
                        from urllib.parse import urlparse
                        return urlparse(url.replace("tcp://", "http://")).hostname or ""
                    return "127.0.0.1"  # local docker → published ports on loopback
    return ""


async def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        return True
    except Exception:
        return False


@capability(
    "app.detect",
    http_method="POST", http_path="/remote/app/detect", http_tags=["remote"],
    memory="off",
    description="Scan a target for running apps by probing well-known ports. "
                "Provide host directly OR conn_id (resolves the target's address). "
                "Inputs: host (str), conn_id (str), ports (str — comma-sep extra "
                "ports), timeout (float=1.0). Output: {host, apps:[{port,label,"
                "icon,scheme,url}]}.",
)
async def cap_app_detect(host: str = "", conn_id: str = "", ports: str = "",
                         timeout: float = 1.0, trace_id=None) -> Dict:
    if not host and conn_id:
        host = await _conn_host_addr(conn_id)
    if not host:
        return {"error": "could not resolve a host (pass host= or a resolvable conn_id)",
                "apps": []}
    plist = list(_DETECT_PORTS)
    for p in [x.strip() for x in (ports or "").split(",") if x.strip()]:
        try:
            plist.append(int(p))
        except Exception:
            pass
    plist = sorted(set(plist))
    results = await asyncio.gather(*[_tcp_open(host, p, timeout) for p in plist])
    apps = []
    for p, up in zip(plist, results):
        if not up:
            continue
        label, icon, scheme = KNOWN_APPS.get(p, ("Service", "🌐", "http"))
        apps.append({"port": p, "label": label, "icon": icon, "scheme": scheme,
                     "url": f"{scheme}://{host}:{p}"})
    await emit_event({"type": "remote.app.detect", "host": host, "found": len(apps)})
    return {"host": host, "apps": apps}


async def _all_apps() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_APPS)
    except Exception:
        return []
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda a: (a.get("label") or a.get("id") or ""))
    return out


async def _get_app(app_id: str) -> Optional[Dict]:
    r = _redis()
    if not r or not app_id:
        return None
    raw = await r.hget(KEY_APPS, app_id)
    return json.loads(raw) if raw else None


@capability(
    "app.mount",
    http_method="POST", http_path="/remote/app/mount", http_tags=["remote"],
    description="Mount an app running on a target so it can be embedded as a "
                "panel and reached through Vera's reverse proxy (phone-friendly, "
                "off-LAN). Inputs: label (str!), host (str!), port (int!), scheme "
                "('http'|'https'), conn_id (str — associate), verify_tls (bool=false), "
                "id (str — update). Output: {ok, app, proxy_path}.",
    schema={"properties": {"scheme": {"enum": ["http", "https"]}}},
)
async def cap_app_mount(label: str = "", host: str = "", port: int = 0,
                        scheme: str = "http", conn_id: str = "",
                        verify_tls: bool = False, id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"ok": False, "error": "store unavailable"}
    if not (host and port):
        return {"ok": False, "error": "host and port required"}
    aid = id or uuid.uuid4().hex[:12]
    rec = {"id": aid, "label": label or f"{host}:{port}", "host": host,
           "port": int(port), "scheme": scheme if scheme in ("http", "https") else "http",
           "conn_id": conn_id, "verify_tls": bool(verify_tls),
           "mcp_id": (await _get_app(aid) or {}).get("mcp_id", ""),
           "updated": now_iso()}
    await r.hset(KEY_APPS, aid, json.dumps(rec))
    await emit_event({"type": "remote.app.mounted", "id": aid, "label": rec["label"]})
    return {"ok": True, "app": rec, "proxy_path": f"/remote/app/{aid}/"}


@capability(
    "app.list",
    http_method="GET", http_path="/remote/app/list", http_tags=["remote"],
    memory="off", silent=True,
    description="List mounted apps. Output: {apps:[{id,label,host,port,scheme,"
                "conn_id,mcp_id,proxy_path}], count}.",
)
async def cap_app_list(trace_id=None) -> Dict:
    apps = await _all_apps()
    for a in apps:
        a["proxy_path"] = f"/remote/app/{a['id']}/"
    return {"apps": apps, "count": len(apps)}


@capability(
    "app.unmount",
    http_method="POST", http_path="/remote/app/unmount", http_tags=["remote"],
    description="Unmount an app by id. Input: id (str!). Output: {ok}.",
)
async def cap_app_unmount(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    await r.hdel(KEY_APPS, id)
    return {"ok": True, "deleted": id}


@capability(
    "app.pair_mcp",
    http_method="POST", http_path="/remote/app/pair_mcp", http_tags=["remote"],
    description="Associate a mounted app with an MCP catalog server so operating "
                "the app and driving its MCP live together. Inputs: id (str! — app), "
                "mcp_id (str! — mcp.catalog id). Output: {ok, app}.",
)
async def cap_app_pair_mcp(id: str = "", mcp_id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id or not mcp_id:
        return {"ok": False, "error": "id and mcp_id required"}
    rec = await _get_app(id)
    if not rec:
        return {"ok": False, "error": "app not found"}
    rec["mcp_id"] = mcp_id
    rec["updated"] = now_iso()
    await r.hset(KEY_APPS, id, json.dumps(rec))
    return {"ok": True, "app": rec}


# ---- reverse proxy ----------------------------------------------------------
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "content-length",
                "proxy-authorization", "te", "trailers", "transfer-encoding",
                "upgrade", "content-encoding"}


def _inject_base(html: bytes, base: str) -> bytes:
    """Inject <base href> so an app using root-relative assets loads under the
    proxy sub-path. Best-effort for apps that honour <base>."""
    try:
        s = html.decode("utf-8", "replace")
    except Exception:
        return html
    low = s.lower()
    tag = f'<base href="{base}">'
    if "<base " in low:
        return html
    i = low.find("<head")
    if i >= 0:
        j = low.find(">", i)
        if j >= 0:
            s = s[:j + 1] + tag + s[j + 1:]
            return s.encode("utf-8", "replace")
    return (tag + s).encode("utf-8", "replace")


@APP.api_route("/remote/app/{app_id}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
               include_in_schema=False)
@APP.api_route("/remote/app/{app_id}/{path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def remote_app_proxy(app_id: str, request: Request, path: str = ""):
    rec = await _get_app(app_id)
    if not rec:
        return JSONResponse({"error": f"unknown app: {app_id}"}, status_code=404)
    base = f"{rec['scheme']}://{rec['host']}:{rec['port']}"
    qs = request.url.query
    target = base + "/" + path + (("?" + qs) if qs else "")
    proxy_base = f"/remote/app/{app_id}/"

    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _HOP_HEADERS and k.lower() != "host"}
    body = await request.body()
    verify = bool(rec.get("verify_tls"))
    try:
        async with httpx.AsyncClient(verify=verify, timeout=45,
                                     follow_redirects=False) as c:
            up = await c.request(request.method, target, headers=fwd_headers,
                                 content=body if body else None)
    except Exception as e:
        return JSONResponse({"error": f"upstream {type(e).__name__}: {e}"},
                            status_code=502)

    resp_headers = {}
    for k, v in up.headers.items():
        lk = k.lower()
        if lk in _HOP_HEADERS:
            continue
        if lk == "location":
            # Rewrite absolute redirects that point back at the app origin.
            if v.startswith(base):
                v = proxy_base + v[len(base):].lstrip("/")
        resp_headers[k] = v

    ctype = up.headers.get("content-type", "")
    content = up.content
    if "text/html" in ctype.lower():
        content = _inject_base(content, proxy_base)
    return Response(content=content, status_code=up.status_code,
                    headers=resp_headers,
                    media_type=ctype.split(";")[0] if ctype else None)


# ═════════════════════════════════════════════════════════════════════════════
#  MCP DETECTION  (mcp.detect)
# ═════════════════════════════════════════════════════════════════════════════
_MCP_PATHS = ["/sse", "/mcp", "/message", "/mcp/sse", "/api/mcp"]


async def _probe_mcp(host: str, port: int, scheme: str = "http",
                     timeout: float = 2.0) -> Optional[Dict]:
    """Return an MCP descriptor if host:port looks like a network MCP server."""
    base = f"{scheme}://{host}:{port}"
    async with httpx.AsyncClient(verify=False, timeout=timeout,
                                 follow_redirects=True) as c:
        for p in _MCP_PATHS:
            url = base + p
            try:
                r = await c.get(url, headers={"Accept": "text/event-stream, application/json"})
            except Exception:
                continue
            ct = r.headers.get("content-type", "").lower()
            body = (r.text or "")[:400].lower()
            if "text/event-stream" in ct:
                return {"url": url, "transport": "sse", "signal": "event-stream"}
            if r.status_code < 500 and ("jsonrpc" in body or "\"protocolversion\"" in body
                                        or "mcp" in body or p in ("/mcp",) and r.status_code in (200, 405, 400)):
                return {"url": url, "transport": "http", "signal": f"http {r.status_code}"}
    return None


@capability(
    "mcp.detect",
    http_method="POST", http_path="/remote/mcp/detect", http_tags=["remote", "mcp"],
    memory="off",
    description="Probe a target for NETWORK MCP servers (SSE / streamable-HTTP) "
                "and register any found into the mcp.catalog. Provide host OR "
                "conn_id. Inputs: host (str), conn_id (str), ports (str — comma-sep; "
                "default a common set), register (bool=true). Output: {host, "
                "found:[{url,transport,port,catalog_id}]}.",
)
async def cap_mcp_detect(host: str = "", conn_id: str = "", ports: str = "",
                         register: bool = True, trace_id=None) -> Dict:
    if not host and conn_id:
        host = await _conn_host_addr(conn_id)
    if not host:
        return {"error": "could not resolve a host (pass host= or a resolvable conn_id)",
                "found": []}
    plist = [3000, 5678, 8000, 8080, 8787, 9000, 3333, 8931, 11434]
    for p in [x.strip() for x in (ports or "").split(",") if x.strip()]:
        try:
            plist.append(int(p))
        except Exception:
            pass
    plist = sorted(set(plist))

    async def _check(p):
        if not await _tcp_open(host, p, 1.0):
            return None
        desc = await _probe_mcp(host, p)
        return (p, desc) if desc else None

    results = [r for r in await asyncio.gather(*[_check(p) for p in plist]) if r]
    found = []
    upsert = _cap_raw("mcp.catalog.upsert")
    for p, desc in results:
        cat_id = ""
        if register and upsert:
            try:
                res = await upsert(label=f"{host}:{p} (detected)",
                                   category="detected", transport=desc["transport"],
                                   url=desc["url"], enabled=False,
                                   notes=f"Auto-detected on {host}:{p} ({desc['signal']})")
                cat_id = (res.get("server") or {}).get("id", "") if isinstance(res, dict) else ""
            except Exception as e:
                log.debug("mcp catalog upsert failed: %s", e)
        found.append({"port": p, "url": desc["url"], "transport": desc["transport"],
                      "signal": desc["signal"], "catalog_id": cat_id})
    await emit_event({"type": "remote.mcp.detect", "host": host, "found": len(found)})
    return {"host": host, "found": found}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/remote/workspace/panel", include_in_schema=False)
async def _workspace_panel():
    p = _HERE / "workspace_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>workspace_panel.html not found</p>")


register_ui(
    "workspaces",
    "Workspaces",
    "▦",
    html="""<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/remote/workspace/panel" style="flex:1;border:none;width:100%;height:100%;
          background:var(--bg0,#0d0f12)" allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    ui_caps=[
        "workspace.list", "workspace.get", "workspace.save", "workspace.delete",
        "conn.list", "conn.open", "conn.targets",
        "fs.list", "fs.read", "fs.write", "fs.mkdir", "fs.delete",
        "app.detect", "app.mount", "app.list", "app.unmount", "app.pair_mcp",
        "mcp.detect",
        # metrics widget → operator.sysinfo; notebook widget → notebook list.
        "operator.sysinfo", "research.notebook.list",
    ],
    mode="tab",
    tab_order=57,
)


log.info("workspace_capabilities loaded — fs.*, workspace.*, app.*, mcp.detect")
