"""
remote_capabilities.py — Vera Remote-Access foundation (Phase 1)
================================================================

Gives Vera a *unified, persistent* remote-access layer over the three target
kinds it already knows how to reach — Docker containers, SSH hosts / VMs, and
Proxmox guests — plus real **interactive terminals** for each, streamed to the
browser over a WebSocket and rendered by the shared ``<vera-terminal>`` element.

Everything else in the container/VM operations program (workspaces, the file
explorer, the operator system, per-session sandboxes) rides on this module.

Connection registry (``conn.*``)
────────────────────────────────
A *connection* is a saved, named handle onto a target so Vera can persist and
re-open it, drive it from an agent, and hang a workspace off it. It never
duplicates a credential — it *references* one that already lives in the Docker
host registry, the SSH host store, or the Proxmox cluster store.

  conn.list / conn.save / conn.delete    — persisted to Redis (vera:remote:connections)
  conn.targets                           — enumerate openable targets (containers/hosts/guests)
  conn.open                              — resolve a connection/target → terminal WS descriptor
  conn.exec                              — one-shot command against a connection (docker exec / ssh run)

Interactive terminals  (WebSocket)
──────────────────────────────────
  /remote/docker/term/ws/{host_id}/{container}   — TTY into a running container
  /remote/ssh/term/ws/{host_id}                  — interactive shell on an SSH host / VM
  (Proxmox guest consoles keep using the existing /proxmox/console/ws proxy.)

Wire protocol (browser ⇄ Vera, shared by both terminal routes)
  • client → server : JSON text frames
        {"d": "<keystrokes>"}         write input
        {"r": [cols, rows]}           resize the PTY
  • server → client : raw **binary** frames (terminal output bytes)

The Docker terminal uses the Engine API exec-hijack for local(unix)/tcp hosts
and asyncssh (remote PTY) for ssh hosts, falling back to a `docker exec -i`
subprocess when no raw transport is available (e.g. Windows npipe). The SSH
terminal uses asyncssh's ``create_process`` with a real remote PTY, so no local
pty is required — it works even when the orchestrator runs on Windows.

Sandbox: the shell *launch* command is run through the exec sandbox gate, so a
locked-down policy can refuse opening a terminal on a target. (Interactive input
inside an accepted session is, by nature, not per-keystroke gated.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)

log = logging.getLogger("vera.remote")

_HERE = Path(__file__).parent
KEY_CONNS = "vera:remote:connections"


# ─────────────────────────────────────────────────────────────────────────────
# Lazy bridges to the modules that own the underlying credentials / helpers.
# All resolved at call time (via sys.modules) so load order does not matter.
# ─────────────────────────────────────────────────────────────────────────────
def _redis():
    return getattr(_orch, "REDIS", None)


def _mod(suffix: str, needs: str):
    """Find a loaded module whose name ends with `suffix` and has attr `needs`."""
    m = sys.modules.get(suffix)
    if m is not None and hasattr(m, needs):
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith(suffix) and hasattr(mod, needs):
            return mod
    return None


def _exec_mod():
    return _mod("exec_capabilities", "_load_hosts")


def _docker_mod():
    return _mod("docker_capabilities", "_get_host")


def _cap_raw(name: str):
    """Resolve another capability's bare (un-wrapped) function by name."""
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
# A connection record:
#   {id, label, kind: 'docker'|'ssh'|'proxmox',
#    # docker:
#    docker_host_id, container,
#    # ssh / proxmox-guest:
#    ssh_host_id,
#    # proxmox context (informational + console deep-link):
#    cluster_id, node, guest_type, vmid,
#    shell, tags, meta, created, updated}
_VALID_KINDS = ("docker", "ssh", "proxmox")


async def _all_conns() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_CONNS)
    except Exception:
        return []
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda c: (c.get("label") or c.get("id") or ""))
    return out


async def _get_conn(conn_id: str) -> Optional[Dict]:
    r = _redis()
    if not r or not conn_id:
        return None
    raw = await r.hget(KEY_CONNS, conn_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@capability(
    "conn.list",
    http_method="GET", http_path="/remote/conn/list", http_tags=["remote"],
    memory="off", silent=True,
    description="List saved remote connections (docker containers / ssh hosts / "
                "proxmox guests). Output: {connections:[{id,label,kind,...}], count}.",
)
async def cap_conn_list(kind: str = "", trace_id=None) -> Dict:
    conns = await _all_conns()
    if kind:
        conns = [c for c in conns if c.get("kind") == kind]
    return {"connections": conns, "count": len(conns)}


@capability(
    "conn.save",
    http_method="POST", http_path="/remote/conn/save", http_tags=["remote"],
    description="Create/update a saved remote connection. A connection REFERENCES "
                "an existing credential — it never stores a new secret. Inputs: "
                "kind ('docker'|'ssh'|'proxmox'), label (str), "
                "docker_host_id (str — for docker), container (str — for docker), "
                "ssh_host_id (str — for ssh, and for proxmox once enrolled), "
                "cluster_id/node/guest_type/vmid (proxmox context), shell "
                "(str — default sh), tags (comma-sep), id (str — update if given). "
                "Output: {ok, connection}.",
    schema={"properties": {"kind": {"enum": list(_VALID_KINDS)}}},
)
async def cap_conn_save(
    kind: str = "ssh", label: str = "", docker_host_id: str = "",
    container: str = "", ssh_host_id: str = "", cluster_id: str = "",
    node: str = "", guest_type: str = "", vmid: int = 0, shell: str = "",
    tags: str = "", id: str = "", meta: Optional[Dict] = None, trace_id=None,
) -> Dict:
    r = _redis()
    if not r:
        return {"ok": False, "error": "store unavailable (no Redis)"}
    kind = (kind or "").strip().lower()
    if kind not in _VALID_KINDS:
        return {"ok": False, "error": f"kind must be one of {list(_VALID_KINDS)}"}
    if kind == "docker" and not container:
        return {"ok": False, "error": "container required for a docker connection"}
    if kind in ("ssh", "proxmox") and not ssh_host_id and not id:
        # proxmox guests become reachable once enrolled (proxmox.guest.enroll →
        # an ssh_host_id). Allow saving without one only when updating.
        if kind == "ssh":
            return {"ok": False, "error": "ssh_host_id required for an ssh connection"}

    existing = await _get_conn(id) if id else None
    cid = id or existing.get("id") if existing else id
    cid = cid or uuid.uuid4().hex[:12]
    rec = dict(existing) if existing else {"id": cid, "created": now_iso()}
    rec.update({
        "id": cid, "kind": kind,
        "label": label or rec.get("label") or (container or ssh_host_id or f"{kind}-{cid[:6]}"),
        "docker_host_id": docker_host_id or rec.get("docker_host_id", ""),
        "container": container or rec.get("container", ""),
        "ssh_host_id": ssh_host_id or rec.get("ssh_host_id", ""),
        "cluster_id": cluster_id or rec.get("cluster_id", ""),
        "node": node or rec.get("node", ""),
        "guest_type": guest_type or rec.get("guest_type", ""),
        "vmid": int(vmid) if vmid else rec.get("vmid", 0),
        "shell": shell or rec.get("shell", "") or "sh",
        "tags": tags if tags else rec.get("tags", ""),
        "meta": meta if meta is not None else rec.get("meta", {}),
        "updated": now_iso(),
    })
    await r.hset(KEY_CONNS, cid, json.dumps(rec))
    await emit_event({"type": "remote.conn.saved", "id": cid, "kind": kind,
                      "label": rec["label"]})
    return {"ok": True, "connection": rec}


@capability(
    "conn.delete",
    http_method="POST", http_path="/remote/conn/delete", http_tags=["remote"],
    description="Delete a saved remote connection by id. Input: id (str!). Output: {ok}.",
)
async def cap_conn_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    await r.hdel(KEY_CONNS, id)
    await emit_event({"type": "remote.conn.deleted", "id": id})
    return {"ok": True, "deleted": id}


@capability(
    "conn.targets",
    http_method="GET", http_path="/remote/conn/targets", http_tags=["remote"],
    memory="off", silent=True,
    description="Enumerate everything Vera can open a terminal into RIGHT NOW, "
                "without needing a saved connection first: running docker "
                "containers (across every registered docker host), stored ssh "
                "hosts, and (optionally) proxmox guests. Inputs: docker_host_id "
                "(str — limit to one host; blank = all), include_proxmox (bool=false — "
                "off by default; hits every saved cluster's API). "
                "Output: {docker:[...], ssh:[...], proxmox:[...]}.",
)
async def cap_conn_targets(docker_host_id: str = "", include_proxmox: bool = False,
                           trace_id=None) -> Dict:
    out: Dict[str, List[Dict]] = {"docker": [], "ssh": [], "proxmox": []}

    # Docker containers across hosts
    d_hosts = _cap_raw("docker.hosts.list")
    d_ps = _cap_raw("docker.ps")
    if d_hosts and d_ps:
        try:
            hosts = (await d_hosts()).get("hosts", [])
        except Exception:
            hosts = []
        for h in hosts:
            hid = h.get("id", "")
            if docker_host_id and hid != docker_host_id:
                continue
            try:
                ps = await d_ps(host_id=hid, all=False)
            except Exception:
                continue
            for c in ps.get("containers", []):
                out["docker"].append({
                    "docker_host_id": hid, "host_label": h.get("label", hid),
                    "id": (c.get("Id") or "")[:12],
                    "container": (c.get("Names") or ["?"])[0].lstrip("/"),
                    "image": c.get("Image", ""), "state": c.get("State", ""),
                    "status": c.get("Status", ""),
                })

    # SSH hosts
    s_list = _cap_raw("exec.ssh.hosts.list")
    if s_list:
        try:
            for h in (await s_list()).get("hosts", []):
                out["ssh"].append({
                    "ssh_host_id": h.get("id", ""), "label": h.get("label", ""),
                    "host": h.get("host", ""), "user": h.get("user", ""),
                    "port": h.get("port", 22), "tags": h.get("tags", ""),
                })
        except Exception:
            pass

    # Proxmox guests (opt-in — needs a live API round-trip per cluster)
    if include_proxmox:
        p_list = _cap_raw("proxmox.cluster.list")
        p_status = _cap_raw("proxmox.status")
        if p_list and p_status:
            try:
                clusters = (await p_list()).get("clusters", [])
            except Exception:
                clusters = []
            for cl in clusters:
                cid = cl.get("id", "")
                try:
                    st = await p_status(cluster_id=cid)
                except Exception:
                    continue
                for g in st.get("guests", []):
                    out["proxmox"].append({
                        "cluster_id": cid, "cluster_label": cl.get("label", cid),
                        "node": g.get("node", ""), "guest_type": g.get("type", ""),
                        "vmid": g.get("vmid", 0), "name": g.get("name", ""),
                        "status": g.get("status", ""),
                    })
    return out


@capability(
    "conn.open",
    http_method="POST", http_path="/remote/conn/open", http_tags=["remote"],
    memory="off",
    description="Resolve a connection (or an ad-hoc target) into a terminal "
                "descriptor the UI/<vera-terminal> can connect to. Pass conn_id "
                "OR direct target params (kind + docker_host_id/container or "
                "ssh_host_id, etc.). Inputs: conn_id (str), kind, docker_host_id, "
                "container, ssh_host_id, cluster_id, node, guest_type, vmid, "
                "shell (str), mode ('term'|'vnc' — proxmox only). Output: "
                "{ok, kind, ws_path, protocol, shell} or {proxy_available, "
                "deeplink_url,...} for proxmox consoles.",
)
async def cap_conn_open(
    conn_id: str = "", kind: str = "", docker_host_id: str = "",
    container: str = "", ssh_host_id: str = "", cluster_id: str = "",
    node: str = "", guest_type: str = "", vmid: int = 0, shell: str = "",
    mode: str = "term", trace_id=None,
) -> Dict:
    rec: Dict = {}
    if conn_id:
        rec = await _get_conn(conn_id) or {}
        if not rec:
            return {"ok": False, "error": f"unknown connection: {conn_id}"}
    kind = (kind or rec.get("kind") or "").lower()
    shell = shell or rec.get("shell") or "sh"

    if kind == "docker":
        hid = docker_host_id or rec.get("docker_host_id") or "local"
        c = container or rec.get("container") or ""
        if not c:
            return {"ok": False, "error": "container required"}
        from urllib.parse import quote
        ws = f"/remote/docker/term/ws/{quote(hid, safe='')}/{quote(c, safe='')}?shell={quote(shell)}"
        return {"ok": True, "kind": "docker", "protocol": "vera-term",
                "ws_path": ws, "shell": shell, "container": c, "docker_host_id": hid}

    if kind == "ssh" or (kind == "proxmox" and (ssh_host_id or rec.get("ssh_host_id"))):
        hid = ssh_host_id or rec.get("ssh_host_id") or ""
        if not hid:
            return {"ok": False, "error": "ssh_host_id required (enroll the guest first)"}
        from urllib.parse import quote
        ws = f"/remote/ssh/term/ws/{quote(hid, safe='')}?shell={quote(shell)}"
        return {"ok": True, "kind": "ssh", "protocol": "vera-term",
                "ws_path": ws, "shell": shell, "ssh_host_id": hid}

    if kind == "proxmox":
        # No enrolled SSH host — fall back to the native Proxmox console proxy.
        ticket = _cap_raw("proxmox.console.ticket")
        if not ticket:
            return {"ok": False, "error": "proxmox module not loaded"}
        return await ticket(
            cluster_id=cluster_id or rec.get("cluster_id", ""),
            node=node or rec.get("node", ""),
            guest_type=guest_type or rec.get("guest_type", ""),
            vmid=int(vmid or rec.get("vmid", 0)), mode=mode)

    return {"ok": False, "error": f"cannot open kind={kind!r}"}


@capability(
    "conn.exec",
    http_method="POST", http_path="/remote/conn/exec", http_tags=["remote"],
    description="Run a ONE-SHOT command against a saved connection (or ad-hoc "
                "target), unifying docker exec / ssh run so an agent can "
                "administrate any target the same way. Inputs: conn_id (str) OR "
                "(kind + target params), command (str!), workdir (str), "
                "timeout (int=120). Output: {ok, rc, stdout, stderr}.",
)
async def cap_conn_exec(
    conn_id: str = "", kind: str = "", docker_host_id: str = "",
    container: str = "", ssh_host_id: str = "", command: str = "",
    workdir: str = "", timeout: int = 120, trace_id=None,
) -> Dict:
    if not command.strip():
        return {"ok": False, "error": "command required"}
    rec: Dict = {}
    if conn_id:
        rec = await _get_conn(conn_id) or {}
        if not rec:
            return {"ok": False, "error": f"unknown connection: {conn_id}"}
    kind = (kind or rec.get("kind") or "").lower()

    if kind == "docker":
        fn = _cap_raw("docker.exec")
        if not fn:
            return {"ok": False, "error": "docker module not loaded"}
        return await fn(host_id=docker_host_id or rec.get("docker_host_id", "local"),
                        container=container or rec.get("container", ""),
                        command=command, workdir=workdir, timeout=int(timeout))

    hid = ssh_host_id or rec.get("ssh_host_id", "")
    if hid:
        fn = _cap_raw("exec.ssh.run")
        if not fn:
            return {"ok": False, "error": "exec module not loaded"}
        cmd = f"cd {shlex.quote(workdir)} && {command}" if workdir else command
        return await fn(command=cmd, host_id=hid, timeout=int(timeout))

    return {"ok": False, "error": f"cannot exec against kind={kind!r} "
                                  "(proxmox guests must be enrolled to an ssh host first)"}


# ─────────────────────────────────────────────────────────────────────────────
# TERMINAL WEBSOCKET PLUMBING
# ─────────────────────────────────────────────────────────────────────────────
def _sandbox_launch_ok(command: str) -> Tuple[bool, str]:
    """Gate the *launch* command through the exec sandbox (best-effort)."""
    ex = _exec_mod()
    if not ex or not hasattr(ex, "_sandbox_check"):
        return True, ""
    try:
        return ex._sandbox_check(command)
    except Exception:
        return True, ""


async def _ws_input_loop(ws: WebSocket, write_bytes, resize) -> None:
    """Read client JSON control frames and drive the PTY. Runs until disconnect.

    Protocol (client → server): {"d": "<data>"} to write, {"r": [cols, rows]}
    to resize. Raw text/bytes that aren't JSON are treated as literal input.
    """
    while True:
        try:
            msg = await ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            break
        if msg.get("type") == "websocket.disconnect":
            break
        data = msg.get("text")
        raw = msg.get("bytes")
        if data is not None:
            try:
                obj = json.loads(data)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                if "d" in obj and obj["d"]:
                    await write_bytes(obj["d"].encode("utf-8", "replace"))
                elif "r" in obj and isinstance(obj["r"], (list, tuple)) and len(obj["r"]) == 2:
                    try:
                        await resize(int(obj["r"][0]), int(obj["r"][1]))
                    except Exception:
                        pass
                continue
            await write_bytes(data.encode("utf-8", "replace"))
        elif raw is not None:
            await write_bytes(bytes(raw))


async def _send_bytes(ws: WebSocket, b: bytes) -> None:
    try:
        await ws.send_bytes(b)
    except Exception:
        raise


# ---- Docker: Engine API exec-hijack (local-unix / tcp) ----------------------
def _docker_transport(rec: Dict) -> Tuple[str, Any]:
    """Return ('unix', sockpath) | ('tcp', (host,port)) | ('none', None)."""
    d = _docker_mod()
    kind = rec.get("kind", "local")
    if kind == "tcp":
        # A unix socket mistakenly stored as tcp is recovered by the docker mod.
        sock = d._socket_from_url(rec.get("url", "")) if d else ""
        if sock and os.path.exists(sock):
            return "unix", sock
        base = (d._normalize_tcp(rec.get("url", "")) if d else rec.get("url", ""))
        p = urlparse(base)
        if p.hostname:
            return "tcp", (p.hostname, p.port or 2375)
        return "none", None
    # local
    sock = rec.get("socket") or (getattr(d, "_LOCAL_SOCK", "/var/run/docker.sock") if d else "/var/run/docker.sock")
    if sock and os.path.exists(sock):
        return "unix", sock
    dh = os.getenv("DOCKER_HOST", "")
    if dh.startswith(("tcp://", "http://", "https://")):
        p = urlparse(dh.replace("tcp://", "http://"))
        if p.hostname:
            return "tcp", (p.hostname, p.port or 2375)
    return "none", None


async def _engine_json(rec: Dict, method: str, path: str,
                       body: Optional[Dict] = None, timeout: float = 15.0
                       ) -> Tuple[int, bytes]:
    """Engine API call WITH an optional JSON body (the docker module's helper
    doesn't send bodies). Supports unix + tcp transports only."""
    import httpx
    kind, addr = _docker_transport(rec)
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if kind == "unix":
        base = "http://localhost"
        kwargs["transport"] = httpx.AsyncHTTPTransport(uds=addr)
    elif kind == "tcp":
        base = f"http://{addr[0]}:{addr[1]}"
    else:
        return 503, b'{"message":"no raw docker transport (unix/tcp)"}'
    async with httpx.AsyncClient(**kwargs) as c:
        r = await c.request(method, base + path,
                            json=body if body is not None else None)
        return r.status_code, r.content


async def _open_engine_raw(rec: Dict):
    """Open a raw (reader, writer) to the daemon for the exec-start hijack."""
    kind, addr = _docker_transport(rec)
    if kind == "unix":
        return await asyncio.open_unix_connection(addr)  # POSIX only
    if kind == "tcp":
        return await asyncio.open_connection(addr[0], addr[1])
    raise RuntimeError("no raw docker transport")


async def _read_http_headers(reader: asyncio.StreamReader) -> bytes:
    """Consume an HTTP response up to the blank line; return leftover body bytes."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(1024)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return rest


@APP.websocket("/remote/docker/term/ws/{host_id}/{container}")
async def remote_docker_term_ws(ws: WebSocket, host_id: str, container: str):
    await ws.accept()
    shell = ws.query_params.get("shell", "sh") or "sh"
    cols = int(ws.query_params.get("cols", "80") or 80)
    rows = int(ws.query_params.get("rows", "24") or 24)

    d = _docker_mod()
    rec = d._get_host(host_id) if d else None
    if not rec:
        await ws.send_bytes(f"\r\n[vera] unknown docker host: {host_id}\r\n".encode())
        await ws.close(code=4404)
        return

    launch = f"docker exec -it {container} {shell}"
    ok, reason = _sandbox_launch_ok(launch)
    if not ok:
        await ws.send_bytes(f"\r\n[vera] sandbox blocked: {reason}\r\n".encode())
        await ws.close(code=4403)
        return
    await emit_event({"type": "remote.term.open", "kind": "docker",
                      "host_id": host_id, "container": container})

    # ssh docker host → drive `docker exec -it` through a remote asyncssh PTY.
    if rec.get("kind") == "ssh":
        uh = await d._ssh_user_host(rec.get("ssh_host_id", "")) if d else ""
        await _ssh_pty_bridge(
            ws, ssh_host_id=rec.get("ssh_host_id", ""),
            command=f"docker exec -it {shlex.quote(container)} {shell}",
            cols=cols, rows=rows, banner=f"container {container} @ {uh or host_id}")
        return

    # local(unix)/tcp → Engine API exec-hijack; else subprocess fallback.
    try:
        status, resp = await _engine_json(
            rec, "POST", f"/containers/{container}/exec",
            {"AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
             "Tty": True, "Cmd": [shell]})
        if status not in (200, 201):
            raise RuntimeError(f"exec create HTTP {status}: {resp[:160]!r}")
        exec_id = json.loads(resp or b"{}").get("Id", "")
        if not exec_id:
            raise RuntimeError("no exec id")
        reader, writer = await _open_engine_raw(rec)
        start_body = json.dumps({"Detach": False, "Tty": True}).encode()
        req = (f"POST /exec/{exec_id}/start HTTP/1.1\r\n"
               f"Host: localhost\r\nContent-Type: application/json\r\n"
               f"Connection: Upgrade\r\nUpgrade: tcp\r\n"
               f"Content-Length: {len(start_body)}\r\n\r\n").encode() + start_body
        writer.write(req)
        await writer.drain()
        leftover = await _read_http_headers(reader)
    except Exception as e:
        log.info("docker term hijack failed (%s) — falling back to subprocess", e)
        await _docker_subprocess_bridge(ws, rec, container, shell, cols, rows)
        return

    async def _resize(c, r):
        await _engine_json(rec, "POST", f"/exec/{exec_id}/resize?h={r}&w={c}")
    await _resize(cols, rows)

    async def _write(b: bytes):
        try:
            writer.write(b)
            await writer.drain()
        except Exception:
            pass

    async def _pump_out():
        try:
            if leftover:
                await ws.send_bytes(leftover)
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                await ws.send_bytes(chunk)
        except Exception:
            pass

    out_task = asyncio.create_task(_pump_out())
    try:
        await _ws_input_loop(ws, _write, _resize)
    finally:
        out_task.cancel()
        try:
            writer.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


async def _docker_subprocess_bridge(ws: WebSocket, rec: Dict, container: str,
                                    shell: str, cols: int, rows: int) -> None:
    """Degraded fallback: `docker [-H] exec -i` over subprocess pipes (no TTY).
    Used when no raw transport is available (e.g. Windows npipe)."""
    d = _docker_mod()
    argv = await d._docker_argv(rec, ["exec", "-i", "-e", f"COLUMNS={cols}",
                                      "-e", f"LINES={rows}", "-e", "TERM=xterm",
                                      container, shell]) if d else None
    if not argv:
        await ws.send_bytes(b"\r\n[vera] docker module unavailable\r\n")
        await ws.close()
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except Exception as e:
        await ws.send_bytes(f"\r\n[vera] spawn failed: {e}\r\n".encode())
        await ws.close()
        return
    await ws.send_bytes(b"\x1b[90m[vera] no PTY on this host - degraded shell "
                        b"(interactive full-screen apps may not render)\x1b[0m\r\n")

    async def _write(b: bytes):
        try:
            proc.stdin.write(b)
            await proc.stdin.drain()
        except Exception:
            pass

    async def _resize(c, r):
        pass  # no TTY to resize

    async def _pump_out():
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                await ws.send_bytes(chunk)
        except Exception:
            pass

    out_task = asyncio.create_task(_pump_out())
    try:
        await _ws_input_loop(ws, _write, _resize)
    finally:
        out_task.cancel()
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ---- SSH: asyncssh interactive PTY -----------------------------------------
async def _resolve_ssh_kwargs(ssh_host_id: str) -> Optional[Dict[str, Any]]:
    """Build asyncssh.connect kwargs for a stored SSH host id (reusing the exec
    module's credential store + connect-kwargs builder)."""
    ex = _exec_mod()
    if not ex:
        return None
    hosts = await ex._load_hosts()
    rec = hosts.get(ssh_host_id)
    if not rec:
        return None
    password, passphrase = "", ""
    if rec.get("auth", "password") == "password":
        password = ex._deobfuscate(rec.get("password_obf", ""))
    else:
        passphrase = ex._deobfuscate(rec.get("passphrase_obf", ""))
    return await ex._ssh_connect_kwargs(
        rec.get("host", ""), port=int(rec.get("port", 22) or 22),
        user=rec.get("user", ""), password=password,
        key_path=rec.get("key_path", "") or "", passphrase=passphrase)


async def _ssh_pty_bridge(ws: WebSocket, *, ssh_host_id: str, command: str = "",
                          cols: int = 80, rows: int = 24, banner: str = "") -> None:
    """Open an interactive asyncssh PTY (shell, or `command`) and bridge it to
    the WebSocket. `command` empty → a login shell."""
    ex = _exec_mod()
    if not ex or not getattr(ex, "HAS_ASYNCSSH", False):
        await ws.send_bytes(b"\r\n[vera] asyncssh not installed on the server\r\n")
        await ws.close(code=4500)
        return
    import asyncssh
    kw = await _resolve_ssh_kwargs(ssh_host_id)
    if kw is None:
        await ws.send_bytes(f"\r\n[vera] unknown ssh host: {ssh_host_id}\r\n".encode())
        await ws.close(code=4404)
        return
    try:
        conn = await asyncssh.connect(**kw)
    except Exception as e:
        await ws.send_bytes(f"\r\n[vera] ssh connect failed: {e}\r\n".encode())
        await ws.close(code=4502)
        return
    try:
        # A PTY unifies stdout/stderr into one stream, so no stderr redirect.
        proc = await conn.create_process(
            command or None, term_type="xterm-256color",
            term_size=(cols, rows), encoding=None)
    except Exception as e:
        await ws.send_bytes(f"\r\n[vera] shell start failed: {e}\r\n".encode())
        await conn.close()
        await ws.close(code=4502)
        return

    if banner:
        await ws.send_bytes(f"\x1b[90m[vera] {banner}\x1b[0m\r\n".encode())

    async def _write(b: bytes):
        try:
            proc.stdin.write(b)
        except Exception:
            pass

    async def _resize(c, r):
        try:
            proc.change_terminal_size(c, r)
        except Exception:
            pass

    async def _pump_out():
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                await ws.send_bytes(chunk)
        except Exception:
            pass

    out_task = asyncio.create_task(_pump_out())
    try:
        await _ws_input_loop(ws, _write, _resize)
    finally:
        out_task.cancel()
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


@APP.websocket("/remote/ssh/term/ws/{host_id}")
async def remote_ssh_term_ws(ws: WebSocket, host_id: str):
    await ws.accept()
    shell = ws.query_params.get("shell", "") or ""   # blank = login shell
    cols = int(ws.query_params.get("cols", "80") or 80)
    rows = int(ws.query_params.get("rows", "24") or 24)
    if shell and shell != "sh":
        ok, reason = _sandbox_launch_ok(shell)
        if not ok:
            await ws.send_bytes(f"\r\n[vera] sandbox blocked: {reason}\r\n".encode())
            await ws.close(code=4403)
            return
    await emit_event({"type": "remote.term.open", "kind": "ssh", "host_id": host_id})
    await _ssh_pty_bridge(ws, ssh_host_id=host_id,
                          command=shell if shell and shell != "sh" else "",
                          cols=cols, rows=rows)


# ─────────────────────────────────────────────────────────────────────────────
# <vera-terminal> ELEMENT  (served at /ui/vera-terminal.js)
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/ui/vera-terminal.js", include_in_schema=False)
async def _serve_vera_terminal_js():
    p = _HERE / "vera-terminal.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript")
    return Response(content="console.warn('vera-terminal.js not found');",
                    media_type="application/javascript")


# ─────────────────────────────────────────────────────────────────────────────
# PANEL
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/remote/panel", include_in_schema=False)
async def _remote_panel():
    from fastapi.responses import HTMLResponse
    p = _HERE / "remote_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>remote_panel.html not found</p>")


# Standalone top-level tab retired — its features are integrated into the
# workers/Ollama panel instead: docker containers get ⌨ shell buttons (Docker
# pane), proxmox guests get console/SSH/enroll buttons (Proxmox pane) and the
# Exec panel manages SSH hosts. The /remote/panel route above is kept.
register_ui = (lambda *a, **k: None)
register_ui(
    "remote-connections",
    "Remote",
    "⎈",
    html="""<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/remote/panel" style="flex:1;border:none;width:100%;height:100%;
          background:var(--bg0,#0d0f12)" allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    ui_caps=[
        "conn.list", "conn.save", "conn.delete", "conn.targets",
        "conn.open", "conn.exec",
    ],
    mode="tab",
    tab_order=56,
)


log.info("remote_capabilities loaded — connection registry + docker/ssh terminals")
