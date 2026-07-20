"""
docker_capabilities.py  —  Vera Docker subsystem
=================================================

Gives Vera a real, server-side Docker backend so the UI talks to the Docker
Engine through Vera (no browser→daemon CORS / unix-socket problems) and so an
agent can drive containers as sandboxed execution environments. Also lets Vera
spin up **Docker workers** — Vera-image containers that join the cluster.

Connection model (matches the rest of Vera — local CLI / SSH host / DOCKER_HOST):
  • local  — the orchestrator's own Docker (unix socket, or $DOCKER_HOST)
  • tcp    — a remote daemon over the Engine HTTP API (http://host:2375, tcp://…)
  • ssh    — a daemon reached over a stored SSH host (curl-over-ssh for the API,
             `docker -H ssh://user@host` for CLI actions)

Host registry  (docker.hosts.*)
  • docker.hosts.list / save / delete   — persisted to ~/.vera_docker_hosts.json

Monitoring (Engine API — used by the Workers panel's Docker pane via the proxy)
  • GET/POST/DELETE /workers/docker/engine/{host_id}/{api_path}  — reverse proxy
  • docker.ping / docker.ps / docker.images

Lifecycle (docker CLI, gated by the SAME exec sandbox policy as exec.* / IDE Run)
  • docker.build  (SSE)   • docker.run  (SSE)   • docker.logs (SSE)
  • docker.exec           • docker.stop         • docker.rm

Docker workers — run Vera (docker.worker.*)
  • docker.worker.spawn   — `docker run -d` the Vera image wired to the shared
                            REDIS_URL etc.; it auto-registers in WORKER_REGISTRY
                            and consumes the task stream (every Vera process is a
                            worker — see capability_orchestration.worker_loop).
  • docker.worker.list / stop / logs

Sandbox: all CLI paths run through exec_capabilities._sandbox_check, so the
<vera-sandbox-controls> editor (Exec / IDE / Workers panels) governs them too.

Env:
  VERA_DOCKER_HOSTS    — path to the host-registry JSON (default ~/.vera_docker_hosts.json)
  VERA_WORKER_IMAGE    — image used by docker.worker.spawn (default "vera:latest")
  DOCKER_SOCK          — local unix socket path (default /var/run/docker.sock)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shlex
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse, JSONResponse

from Vera.vera.config import cfg
import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso,
)

log = logging.getLogger("vera.docker")

_LOCAL_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")


# ─────────────────────────────────────────────────────────────────────────────
# Lazy bridge to exec_capabilities (sandbox gate + local runner + ssh hosts)
# ─────────────────────────────────────────────────────────────────────────────
def _exec_mod():
    """Return the loaded exec_capabilities module, or None."""
    m = sys.modules.get("exec_capabilities")
    if m is not None and hasattr(m, "_sandbox_check"):
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("exec_capabilities") \
                and hasattr(mod, "_sandbox_check"):
            return mod
    return None


def _sandbox_gate(text: str, cwd: str = "") -> Tuple[bool, str]:
    """(allowed, reason). Allows (with a warning) if exec module isn't loaded."""
    sb = _exec_mod()
    if sb is None:
        log.warning("docker: exec sandbox module not loaded — running ungated")
        return True, ""
    try:
        return sb._sandbox_check(text, cwd=cwd)
    except Exception as e:
        log.warning("docker sandbox check errored (allowing): %s", e)
        return True, ""


async def _run_local(argv: List[str], *, timeout: int = 60, cwd: str = "") -> Dict[str, Any]:
    """Run a captured local command via exec's runner (falls back to asyncio)."""
    sb = _exec_mod()
    if sb is not None and hasattr(sb, "_run_local"):
        return await sb._run_local(argv, timeout=timeout, cwd=cwd or None)
    # Minimal fallback
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"ok": proc.returncode == 0, "rc": proc.returncode,
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": str(e), "error": str(e)}


async def _ssh_user_host(ssh_host_id: str) -> str:
    """Resolve a stored SSH host id → 'user@host' (for `docker -H ssh://…`)."""
    sb = _exec_mod()
    if not sb or not ssh_host_id or not hasattr(sb, "_load_hosts"):
        return ""
    try:
        hosts = await sb._load_hosts()
        rec = hosts.get(ssh_host_id)
        if rec:
            u, h = rec.get("user", ""), rec.get("host", "")
            return f"{u}@{h}" if u else h
    except Exception as e:
        log.debug("ssh host resolve failed: %s", e)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# HOST REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
_STORE_PATH = Path(os.getenv(
    "VERA_DOCKER_HOSTS",
    os.path.join(os.path.expanduser("~"), ".vera_docker_hosts.json"),
))


def _load_hosts() -> Dict[str, dict]:
    if not _STORE_PATH.exists():
        return {}
    try:
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        log.warning("docker host store corrupt: %s", e)
    return {}


def _save_hosts(hosts: Dict[str, dict]) -> None:
    try:
        _STORE_PATH.write_text(json.dumps(hosts, indent=2, sort_keys=True),
                               encoding="utf-8")
        try: os.chmod(_STORE_PATH, 0o600)
        except Exception: pass
    except Exception as e:
        log.error("failed to persist docker host store: %s", e)


def _ensure_local_host(hosts: Dict[str, dict]) -> Dict[str, dict]:
    """Guarantee a 'local' host exists so the panel always has a default."""
    if "local" not in hosts:
        hosts["local"] = {
            "id": "local", "label": "local", "kind": "local",
            "url": "", "ssh_host_id": "", "socket": _LOCAL_SOCK,
            "default": not any(h.get("default") for h in hosts.values()),
            "created_at": now_iso(), "updated_at": now_iso(),
        }
    return hosts


def _get_host(host_id: str) -> Optional[dict]:
    hosts = _ensure_local_host(_load_hosts())
    if not host_id:
        for h in hosts.values():
            if h.get("default"):
                return h
        return hosts.get("local")
    return hosts.get(host_id)


def _normalize_tcp(url: str) -> str:
    """tcp://h:2375 → http://h:2375 ; bare host:port → http://host:port."""
    u = (url or "").strip()
    if u.startswith("tcp://"):
        u = "http://" + u[len("tcp://"):]
    elif not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u.rstrip("/")


def _socket_from_url(url: str) -> str:
    """If `url` denotes a unix socket (unix:///… or a bare /…/*.sock path),
    return the socket path, else "". Tolerates a stray scheme/slash prefix the UI
    may prepend (e.g. 'https://unix:///var/run/docker.sock'), so a host saved as
    tcp by mistake still resolves to the local socket instead of httpx trying to
    dial host 'unix' → HTTP 502."""
    u = (url or "").strip()
    low = u.lower()
    if low.startswith("https://"):
        u = u[8:]
    elif low.startswith("http://"):
        u = u[7:]
    s = u.lstrip("/")  # tolerate stray leading slashes before the scheme
    if s.lower().startswith("unix://"):
        p = s[len("unix://"):]
        return p if p.startswith("/") else "/" + p
    if u.startswith("/") and u.endswith(".sock"):
        return u
    return ""


@capability(
    "docker.hosts.list",
    http_method="GET", http_path="/workers/docker/hosts", http_tags=["docker"],
    memory="off", silent=True,
    description="List registered Docker hosts (local / tcp / ssh). "
                "Output: {hosts: [{id, label, kind, url, ssh_host_id, default}], count}.",
)
async def cap_docker_hosts_list(trace_id=None) -> Dict:
    hosts = _ensure_local_host(_load_hosts())
    return {"hosts": list(hosts.values()), "count": len(hosts)}


@capability(
    "docker.hosts.save",
    http_method="POST", http_path="/workers/docker/hosts/save", http_tags=["docker"],
    description="Add or update a Docker host. "
                "Input: kind ('local'|'tcp'|'ssh'), label (str), url (str — for tcp, "
                "e.g. http://host:2375), ssh_host_id (str — for ssh, a stored SSH cred), "
                "socket (str — local/ssh unix socket, default /var/run/docker.sock), "
                "id (str — update if given), make_default (bool). Output: {ok, host}.",
)
async def cap_docker_hosts_save(
    kind: str = "tcp", label: str = "", url: str = "", ssh_host_id: str = "",
    socket: str = "", id: str = "", make_default: bool = False, trace_id=None,
) -> Dict:
    kind = (kind or "tcp").strip().lower()
    if kind not in ("local", "tcp", "ssh"):
        return {"ok": False, "error": "kind must be local|tcp|ssh"}
    # A unix-socket given as a tcp url is really a local host (dialing it as tcp
    # would 502). Coerce it so it's stored — and dialed — correctly.
    if kind == "tcp":
        _sock = _socket_from_url(url)
        if _sock:
            kind, socket, url = "local", (socket or _sock), ""
    if kind == "tcp" and not url:
        return {"ok": False, "error": "url required for tcp host"}
    if kind == "ssh" and not ssh_host_id:
        return {"ok": False, "error": "ssh_host_id required for ssh host"}

    hosts = _ensure_local_host(_load_hosts())
    hid = id or (label or kind).strip().lower().replace(" ", "-") or uuid.uuid4().hex[:8]
    existing = hosts.get(hid, {})
    rec = {
        "id": hid,
        "label": label or existing.get("label") or hid,
        "kind": kind,
        "url": _normalize_tcp(url) if (kind == "tcp" and url) else (url or existing.get("url", "")),
        "ssh_host_id": ssh_host_id or existing.get("ssh_host_id", ""),
        "socket": socket or existing.get("socket", _LOCAL_SOCK),
        "default": bool(make_default) or existing.get("default", False),
        "created_at": existing.get("created_at", now_iso()),
        "updated_at": now_iso(),
    }
    hosts[hid] = rec
    if make_default:
        for k, h in hosts.items():
            h["default"] = (k == hid)
    _save_hosts(hosts)
    await emit_event({"type": "docker.host.saved", "id": hid, "kind": kind})
    return {"ok": True, "host": rec}


@capability(
    "docker.hosts.delete",
    http_method="POST", http_path="/workers/docker/hosts/delete", http_tags=["docker"],
    description="Delete a registered Docker host by id (the built-in 'local' "
                "host cannot be removed). Input: id (str!). Output: {ok}.",
)
async def cap_docker_hosts_delete(id: str = "", trace_id=None) -> Dict:
    if not id:
        return {"ok": False, "error": "id required"}
    if id == "local":
        return {"ok": False, "error": "cannot delete the built-in local host"}
    hosts = _load_hosts()
    if id not in hosts:
        return {"ok": False, "error": f"unknown host: {id}"}
    hosts.pop(id, None)
    _save_hosts(hosts)
    await emit_event({"type": "docker.host.deleted", "id": id})
    return {"ok": True, "deleted": id}


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE API  (reverse proxy + ping/ps/images)
# ─────────────────────────────────────────────────────────────────────────────
async def _engine_request(rec: dict, method: str, api_path: str,
                          *, timeout: float = 15.0) -> Tuple[int, bytes, str]:
    """Hit the Docker Engine API for a host. Returns (status, body, content_type)."""
    kind = rec.get("kind", "local")
    if not api_path.startswith("/"):
        api_path = "/" + api_path

    if kind == "tcp":
        # Recover hosts mistakenly saved as tcp with a unix-socket url.
        sock = _socket_from_url(rec.get("url", ""))
        if sock and os.path.exists(sock):
            transport = httpx.AsyncHTTPTransport(uds=sock)
            async with httpx.AsyncClient(transport=transport, timeout=timeout) as c:
                r = await c.request(method, "http://localhost" + api_path)
                return r.status_code, r.content, r.headers.get("content-type", "application/json")
        base = _normalize_tcp(rec.get("url", ""))
        if not base:
            return 503, b'{"message":"tcp host has no url"}', "application/json"
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, base + api_path)
            return r.status_code, r.content, r.headers.get("content-type", "application/json")

    if kind == "ssh":
        sock = rec.get("socket") or _LOCAL_SOCK
        # URL-safe enough: api_path may contain ? and & — keep it single-quoted.
        cmd = f"curl -s -m {int(timeout)} -X {method} --unix-socket {shlex.quote(sock)} 'http://localhost{api_path}'"
        sb = _exec_mod()
        if not sb or not hasattr(sb, "cap_ssh_run"):
            return 502, b'{"message":"ssh exec unavailable"}', "application/json"
        res = await sb.cap_ssh_run(command=cmd, host_id=rec.get("ssh_host_id", ""),
                                   timeout=int(timeout) + 4)
        if not res.get("ok"):
            msg = res.get("stderr") or res.get("error") or "ssh curl failed"
            return 502, json.dumps({"message": msg}).encode(), "application/json"
        return 200, (res.get("stdout", "") or "").encode("utf-8"), "application/json"

    # local
    sock = rec.get("socket") or _LOCAL_SOCK
    if os.path.exists(sock):
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as c:
            r = await c.request(method, "http://localhost" + api_path)
            return r.status_code, r.content, r.headers.get("content-type", "application/json")
    dh = os.getenv("DOCKER_HOST", "")
    if dh.startswith(("tcp://", "http://", "https://")):
        base = _normalize_tcp(dh)
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, base + api_path)
            return r.status_code, r.content, r.headers.get("content-type", "application/json")
    return (503, b'{"message":"no local docker socket; set DOCKER_HOST or register a tcp/ssh host"}',
            "application/json")


@APP.api_route("/workers/docker/engine/{host_id}/{api_path:path}",
               methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def docker_engine_proxy(host_id: str, api_path: str, request: Request):
    """Reverse-proxy the Docker Engine API for a registered host (so the panel
    talks to the daemon through Vera instead of the browser hitting it directly)."""
    rec = _get_host(host_id)
    if not rec:
        return JSONResponse({"message": f"unknown docker host: {host_id}"}, status_code=404)
    qs = request.url.query
    full = "/" + api_path + (("?" + qs) if qs else "")
    try:
        status, content, ctype = await _engine_request(rec, request.method, full)
    except Exception as e:
        return JSONResponse({"message": f"{type(e).__name__}: {e}"}, status_code=502)
    return Response(content=content, status_code=status,
                    media_type=(ctype or "application/json").split(";")[0])


@capability(
    "docker.ping",
    http_method="POST", http_path="/workers/docker/ping", http_tags=["docker"],
    memory="off",
    description="Check a Docker host is reachable (Engine /version). "
                "Input: host_id (str). Output: {ok, version, api_version, host_id}.",
)
async def cap_docker_ping(host_id: str = "", trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    try:
        status, body, _ = await _engine_request(rec, "GET", "/version", timeout=8)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}", "host_id": rec["id"]}
        j = json.loads(body or b"{}")
        return {"ok": True, "version": j.get("Version", ""),
                "api_version": j.get("ApiVersion", ""), "host_id": rec["id"]}
    except Exception as e:
        return {"ok": False, "error": str(e), "host_id": rec["id"]}


async def _parse_engine_json(body: bytes, default):
    """Parse an Engine API response body; big ones (containers/images on a
    busy host — a captured 1.7s loop stall in docker.ps) off the loop."""
    try:
        if body and len(body) > 131072:
            return await asyncio.to_thread(json.loads, body)
        return json.loads(body) if body else default
    except Exception:
        return default


@capability(
    "docker.ps",
    http_method="POST", http_path="/workers/docker/ps", http_tags=["docker"],
    memory="off",
    description="List containers on a host (Engine /containers/json). "
                "Input: host_id (str), all (bool=true). Output: {containers: [...], count}.",
)
async def cap_docker_ps(host_id: str = "", all: bool = True, trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"error": f"unknown host: {host_id}", "containers": []}
    status, body, _ = await _engine_request(rec, "GET", f"/containers/json?all={'true' if all else 'false'}")
    if status != 200:
        return {"error": f"HTTP {status}", "containers": []}
    rows = await _parse_engine_json(body, [])
    if not isinstance(rows, list):
        rows = []
    return {"host_id": rec["id"], "containers": rows, "count": len(rows)}


@capability(
    "docker.images",
    http_method="POST", http_path="/workers/docker/images", http_tags=["docker"],
    memory="off",
    description="List images on a host (Engine /images/json). "
                "Input: host_id (str). Output: {images: [...], count}.",
)
async def cap_docker_images(host_id: str = "", trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"error": f"unknown host: {host_id}", "images": []}
    status, body, _ = await _engine_request(rec, "GET", "/images/json")
    if status != 200:
        return {"error": f"HTTP {status}", "images": []}
    rows = await _parse_engine_json(body, [])
    if not isinstance(rows, list):
        rows = []
    return {"host_id": rec["id"], "images": rows, "count": len(rows)}


# ─────────────────────────────────────────────────────────────────────────────
# CLI BRIDGE  (lifecycle — sandbox-gated)
# ─────────────────────────────────────────────────────────────────────────────
async def _docker_argv(rec: dict, args: List[str]) -> List[str]:
    """Build `docker [-H endpoint] <args>` for a host."""
    flag: List[str] = []
    kind = rec.get("kind", "local")
    if kind == "tcp" and rec.get("url"):
        flag = ["-H", _normalize_tcp(rec["url"])]
    elif kind == "ssh":
        uh = await _ssh_user_host(rec.get("ssh_host_id", ""))
        if uh:
            flag = ["-H", "ssh://" + uh]
    return ["docker", *flag, *args]


def _docker_sse(event: str, data) -> bytes:
    """SSE frame shape shared with the IDE/Workers panels: data:{event,data}."""
    return ("data: " + json.dumps({"event": event, "data": data}) + "\n\n").encode("utf-8")


async def _stream_argv(argv: List[str], *, cwd: str = "", timeout: int = 0,
                       on_first_line_as: str = ""):
    """Async generator → SSE frames (start/stdout/stderr/exit). If
    on_first_line_as is set, the first stdout line is also emitted as that event
    (used by `run -d` to surface the container id as `name`)."""
    yield _docker_sse("start", " ".join(argv))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd or None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        yield _docker_sse("stderr", f"spawn failed: {e}")
        yield _docker_sse("exit", "-1")
        return

    q: "asyncio.Queue" = asyncio.Queue()
    first_done = {"v": False}

    async def _pump(stream, kind):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                await q.put((kind, line.decode("utf-8", "replace").rstrip("\r\n")))
        finally:
            await q.put((kind + ":eof", ""))

    t_out = asyncio.create_task(_pump(proc.stdout, "stdout"))
    t_err = asyncio.create_task(_pump(proc.stderr, "stderr"))
    t0 = time.monotonic()
    eofs = 0
    rc = -1
    try:
        while eofs < 2:
            try:
                kind, text = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if timeout and (time.monotonic() - t0) > timeout:
                    try: proc.kill()
                    except Exception: pass
                    yield _docker_sse("stderr", f"timeout after {timeout}s")
                    break
                continue
            if kind.endswith(":eof"):
                eofs += 1
                continue
            if kind == "stdout" and on_first_line_as and not first_done["v"] and text.strip():
                first_done["v"] = True
                yield _docker_sse(on_first_line_as, text.strip())
            yield _docker_sse(kind, text)
        rc = await proc.wait()
    except asyncio.CancelledError:
        try: proc.kill()
        except Exception: pass
        raise
    finally:
        for _t in (t_out, t_err):
            if not _t.done():
                _t.cancel()
    yield _docker_sse("exit", str(rc))


async def _gated_stream_response(rec: dict, args: List[str], *, cwd: str = "",
                                 timeout: int = 0, on_first_line_as: str = ""):
    """Sandbox-gate then stream a docker CLI command as SSE."""
    argv = await _docker_argv(rec, args)
    ok, reason = _sandbox_gate(" ".join(argv), cwd)
    if not ok:
        await emit_event({"type": "exec.sandbox.blocked", "shell": "docker", "reason": reason})

        async def _blocked():
            yield _docker_sse("stderr", f"⛔ sandbox blocked: {reason}")
            yield _docker_sse("exit", "126")
        return StreamingResponse(_blocked(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return StreamingResponse(
        _stream_argv(argv, cwd=cwd, timeout=timeout, on_first_line_as=on_first_line_as),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@APP.post("/workers/docker/build", include_in_schema=False)
async def docker_build_stream(request: Request):
    """SSE: `docker build -t <tag> <cwd>` (gated). Body: {host_id, cwd, tag, dockerfile?}."""
    try: body = await request.json()
    except Exception: body = {}
    rec = _get_host(body.get("host_id", ""))
    if not rec:
        return JSONResponse({"detail": "unknown docker host"}, status_code=404)
    cwd = body.get("cwd", "") or ""
    tag = body.get("tag", "") or f"vera-build:{uuid.uuid4().hex[:8]}"
    args = ["build", "-t", tag]
    if body.get("dockerfile"):
        args += ["-f", body["dockerfile"]]
    args += [cwd or "."]
    return await _gated_stream_response(rec, args, cwd=cwd, timeout=int(body.get("timeout", 0)))


@APP.post("/workers/docker/run", include_in_schema=False)
async def docker_run_stream(request: Request):
    """SSE: `docker run` an image (gated). Body: {host_id, image, name?, detach?, ports?, env?, volumes?, cmd?, extra_args?}."""
    try: body = await request.json()
    except Exception: body = {}
    rec = _get_host(body.get("host_id", ""))
    if not rec:
        return JSONResponse({"detail": "unknown docker host"}, status_code=404)
    image = (body.get("image") or "").strip()
    if not image:
        return JSONResponse({"detail": "image required"}, status_code=400)
    detach = bool(body.get("detach", False))
    args = ["run", "--rm"] if not detach else ["run", "-d"]
    if body.get("name"):
        args += ["--name", str(body["name"])]
    for p in (body.get("ports") or []):
        args += ["-p", str(p)]
    for k, v in (body.get("env") or {}).items():
        args += ["-e", f"{k}={v}"]
    for vol in (body.get("volumes") or []):
        args += ["-v", str(vol)]
    if body.get("extra_args"):
        args += shlex.split(str(body["extra_args"]))
    args += [image]
    if body.get("cmd"):
        args += shlex.split(str(body["cmd"])) if isinstance(body["cmd"], str) else list(body["cmd"])
    # When detached, the first stdout line is the container id → emit as `name`.
    return await _gated_stream_response(rec, args, timeout=int(body.get("timeout", 0)),
                                        on_first_line_as="name" if detach else "")


@APP.post("/workers/docker/logs", include_in_schema=False)
async def docker_logs_stream(request: Request):
    """SSE: `docker logs -f` a container. Body: {host_id, container, tail?}."""
    try: body = await request.json()
    except Exception: body = {}
    rec = _get_host(body.get("host_id", ""))
    if not rec:
        return JSONResponse({"detail": "unknown docker host"}, status_code=404)
    cid = (body.get("container") or "").strip()
    if not cid:
        return JSONResponse({"detail": "container required"}, status_code=400)
    tail = str(body.get("tail", "200"))
    args = ["logs", "-f", "--tail", tail, cid]
    return await _gated_stream_response(rec, args, timeout=int(body.get("timeout", 0)))


@capability(
    "docker.exec",
    http_method="POST", http_path="/workers/docker/exec", http_tags=["docker"],
    description="Run a command inside a running container (sandboxed execution "
                "environment). Gated by the exec sandbox. "
                "Input: host_id (str), container (str!), command (str!), "
                "workdir (str), timeout (int=120). Output: {ok, rc, stdout, stderr}.",
)
async def cap_docker_exec(host_id: str = "", container: str = "", command: str = "",
                          workdir: str = "", timeout: int = 120, trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    if not container or not command.strip():
        return {"ok": False, "error": "container and command required"}
    ok, reason = _sandbox_gate(command, workdir)
    if not ok:
        await emit_event({"type": "exec.sandbox.blocked", "shell": "docker.exec", "reason": reason})
        return {"ok": False, "blocked": True, "error": f"sandbox: {reason}"}
    args = ["exec"]
    if workdir:
        args += ["-w", workdir]
    args += [container, "sh", "-lc", command]
    argv = await _docker_argv(rec, args)
    res = await _run_local(argv, timeout=int(timeout))
    res["container"] = container
    return res


@capability(
    "docker.stop",
    http_method="POST", http_path="/workers/docker/stop", http_tags=["docker"],
    description="Stop a container. Input: host_id (str), container (str!), timeout (int=10). Output: {ok}.",
)
async def cap_docker_stop(host_id: str = "", container: str = "", timeout: int = 10, trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    if not container:
        return {"ok": False, "error": "container required"}
    argv = await _docker_argv(rec, ["stop", "-t", str(int(timeout)), container])
    res = await _run_local(argv, timeout=int(timeout) + 20)
    await emit_event({"type": "docker.container.stopped", "container": container, "host_id": rec["id"]})
    return {"ok": res.get("ok", False), "rc": res.get("rc"),
            "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}


@capability(
    "docker.rm",
    http_method="POST", http_path="/workers/docker/rm", http_tags=["docker"],
    description="Remove a container. Input: host_id (str), container (str!), force (bool=false). Output: {ok}.",
)
async def cap_docker_rm(host_id: str = "", container: str = "", force: bool = False, trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    if not container:
        return {"ok": False, "error": "container required"}
    args = ["rm"] + (["-f"] if force else []) + [container]
    argv = await _docker_argv(rec, args)
    res = await _run_local(argv, timeout=40)
    await emit_event({"type": "docker.container.removed", "container": container, "host_id": rec["id"]})
    return {"ok": res.get("ok", False), "rc": res.get("rc"),
            "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE ENSURE  —  local-only images (vera:latest is NOT on Docker Hub)
# ─────────────────────────────────────────────────────────────────────────────
# .../Vera/vera/workers/docker_capabilities.py → parents[2] == repo root, where
# the Dockerfile lives. `docker -H <remote> build <repo>` ships this local
# context to the remote daemon, so building works for ssh/tcp hosts too.
_REPO_ROOT = Path(__file__).resolve().parents[2]


async def _image_present(rec: dict, image: str) -> bool:
    try:
        status, _, _ = await _engine_request(rec, "GET", f"/images/{image}/json")
        return status == 200
    except Exception:
        return False


@capability(
    "docker.image.ensure",
    http_method="POST", http_path="/workers/docker/image/ensure", http_tags=["docker"],
    memory="off",
    description="Make sure an image exists on a Docker host WITHOUT pulling from "
                "a registry — for local-only images like the Vera one "
                "('vera:latest' does not exist on Docker Hub; a docker run there "
                "fails with 'pull access denied'). If missing, either BUILDS it "
                "from this repo's Dockerfile (works for local, tcp and ssh hosts "
                "— the build context is sent from this machine) or TRANSFERS an "
                "image the local daemon already has via docker save/load. "
                "Inputs: host_id (str), image (str — default VERA_WORKER_IMAGE/"
                "'vera:latest'), strategy ('auto'|'build'|'transfer'|"
                "'build_transfer' — auto prefers transfer when the local daemon "
                "already has the image; for a remote target with a working local "
                "daemon it builds locally then transfers, since streaming a build "
                "context over ssh is fragile; else builds on the target), context "
                "(str — build dir, default the Vera repo root), dockerfile (str), "
                "force (bool=false — REBUILD from source even if the image already "
                "exists; use to refresh a stale image, e.g. a dev sandbox running "
                "an old vera:latest), timeout (int=1800). "
                "Output: {ok, present, action, image, host_id, log}.",
)
async def cap_docker_image_ensure(host_id: str = "", image: str = "",
                                  strategy: str = "auto", context: str = "",
                                  dockerfile: str = "", timeout: int = 1800,
                                  force: bool = False,
                                  trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    image = image or os.getenv("VERA_WORKER_IMAGE", "vera:latest")
    present = await _image_present(rec, image)
    # force=True rebuilds even when present (refresh a stale image). Without it,
    # an existing image short-circuits — which is why a stale sandbox image is
    # never refreshed by a plain `sandbox up`.
    if present and not force:
        return {"ok": True, "present": True, "action": "none",
                "image": image, "host_id": rec["id"]}

    strategy = (strategy or "auto").strip().lower()
    local_rec = _get_host("local")
    is_remote = rec.get("id") != (local_rec or {}).get("id")
    local_has = (await _image_present(local_rec, image)) \
        if (is_remote and local_rec) else False
    local_up = local_has
    if is_remote and local_rec and not local_has:
        try:
            st, _, _ = await _engine_request(local_rec, "GET", "/_ping", timeout=5)
            local_up = st == 200
        except Exception:
            local_up = False
    if strategy == "auto":
        # A forced refresh must actually REBUILD from source — never satisfy it
        # by transferring a copy that may itself be stale. Build locally (and
        # transfer to a remote target after).
        if force and is_remote and local_up:
            strategy = "build_transfer"
        elif force:
            strategy = "build"
        elif is_remote and local_has:
            strategy = "transfer"
        elif is_remote and local_up:
            # Build on the LOCAL daemon then save/load across. Building directly
            # on the remote (`docker -H ssh://… build <ctx>`) streams the whole
            # context through the ssh connhelper, which breaks on large repos
            # ("write |1: broken pipe" on http://docker.example.com/…/build).
            strategy = "build_transfer"
        else:
            strategy = "build"
    if strategy == "transfer" and not local_has:
        return {"ok": False, "error": f"transfer requested but the local daemon "
                                      f"does not have {image} — build it first "
                                      f"(strategy='build')",
                "image": image, "host_id": rec["id"]}

    log_tail = ""
    if strategy in ("build", "build_transfer"):
        build_rec = local_rec if strategy == "build_transfer" else rec
        ctx = context or str(_REPO_ROOT)
        args = ["build", "-t", image]
        if dockerfile:
            args += ["-f", dockerfile]
        args += [ctx]
        argv = await _docker_argv(build_rec, args)
        ok, reason = _sandbox_gate(" ".join(argv), ctx)
        if not ok:
            await emit_event({"type": "exec.sandbox.blocked",
                              "shell": "docker.image.ensure", "reason": reason})
            return {"ok": False, "blocked": True, "error": f"sandbox: {reason}"}
        await emit_event({"type": "docker.image.build", "image": image,
                          "host_id": build_rec["id"], "context": ctx})
        res = await _run_local(argv, timeout=int(timeout))
        log_tail = ((res.get("stdout", "") or "") + "\n" +
                    (res.get("stderr", "") or ""))[-2500:]
        if not res.get("ok"):
            return {"ok": False, "action": strategy, "image": image,
                    "host_id": rec["id"], "log": log_tail,
                    "error": (res.get("stderr") or "docker build failed")[-800:]}

    if strategy in ("transfer", "build_transfer"):
        # Two-step save/load through a temp tar — portable (no shell pipe),
        # works from Windows-native runs too. Images can be GB-scale.
        tmp = Path(tempfile.gettempdir()) / f"vera-img-{uuid.uuid4().hex[:8]}.tar"
        try:
            save_argv = await _docker_argv(local_rec, ["save", "-o", str(tmp), image])
            load_argv = await _docker_argv(rec, ["load", "-i", str(tmp)])
            for argv in (save_argv, load_argv):
                ok, reason = _sandbox_gate(" ".join(argv))
                if not ok:
                    await emit_event({"type": "exec.sandbox.blocked",
                                      "shell": "docker.image.ensure", "reason": reason})
                    return {"ok": False, "blocked": True, "error": f"sandbox: {reason}"}
            res = await _run_local(save_argv, timeout=int(timeout))
            log_tail += ("\n" + (res.get("stderr", "") or ""))[-1500:]
            if not res.get("ok"):
                return {"ok": False, "action": strategy, "image": image,
                        "host_id": rec["id"], "log": log_tail,
                        "error": res.get("stderr") or "docker save failed"}
            res = await _run_local(load_argv, timeout=int(timeout))
            log_tail += ("\n" + (res.get("stdout", "") or "") +
                         (res.get("stderr", "") or ""))[-1500:]
            if not res.get("ok"):
                return {"ok": False, "action": strategy, "image": image,
                        "host_id": rec["id"], "log": log_tail,
                        "error": res.get("stderr") or "docker load failed"}
        finally:
            try: tmp.unlink()
            except Exception: pass

    present = await _image_present(rec, image)
    await emit_event({"type": "docker.image.ensured", "image": image,
                      "host_id": rec["id"], "action": strategy, "ok": present})
    return {"ok": present, "present": present, "action": strategy,
            "image": image, "host_id": rec["id"], "log": log_tail}


# ─────────────────────────────────────────────────────────────────────────────
# DOCKER WORKERS  —  run Vera in a container (joins the cluster)
# ─────────────────────────────────────────────────────────────────────────────
# Every Vera process auto-registers as a worker (capability_orchestration.
# worker_loop) and consumes vera:tasks. So a "Docker worker" is just the Vera
# image run with the shared REDIS_URL (and the other backends) — it appears in
# WORKER_REGISTRY within seconds.
# ─────────────────────────────────────────────────────────────────────────────
_WORKER_LABEL = "vera.role=worker"
_DEFAULT_BACKEND_ENV = (
    "POSTGRES_URL", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASS",
    "CHROMA_HOST", "CHROMA_PORT",
    "OLLAMA_GPU_URL", "OLLAMA_CPU_A_URL", "OLLAMA_CPU_B_URL",
    "OLLAMA_MODEL", "OLLAMA_EMBED_URL", "OLLAMA_EMBED_MODEL",
)


@capability(
    "docker.worker.spawn",
    http_method="POST", http_path="/workers/docker/worker/spawn", http_tags=["docker", "workers"],
    description="Spin up a Vera worker container that joins the cluster. Runs the "
                "Vera image (VERA_WORKER_IMAGE, default 'vera:latest') detached, "
                "wired to the shared REDIS_URL so it auto-registers in WORKER_REGISTRY "
                "and consumes the task stream. The Vera image is LOCAL-ONLY (not on "
                "Docker Hub): when missing on the host it is built from this repo / "
                "transferred from the local daemon first (docker.image.ensure) "
                "instead of docker attempting a doomed registry pull. "
                "Input: host_id (str — Docker host), name (str), image (str), "
                "redis_url (str — default this orchestrator's), network (str), "
                "gpus (str — e.g. 'all'), env (dict — extra -e vars), "
                "inherit_backends (bool=true — copy POSTGRES/NEO4J/OLLAMA env), "
                "ensure_image (bool=true — build/transfer the image if absent), "
                "extra_args (str). Output: {ok, container_id, name, image, host_id}.",
)
async def cap_docker_worker_spawn(
    host_id: str = "", name: str = "", image: str = "", redis_url: str = "",
    network: str = "", gpus: str = "", env: Optional[Dict[str, str]] = None,
    inherit_backends: bool = True, ensure_image: bool = True,
    extra_args: str = "", trace_id=None,
) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    image = image or os.getenv("VERA_WORKER_IMAGE", "vera:latest")
    name = name or f"vera-worker-{uuid.uuid4().hex[:8]}"
    redis_url = redis_url or os.getenv("REDIS_URL", "") or getattr(cfg, "REDIS_URL", "redis://localhost:6379")

    if ensure_image:
        ens = await cap_docker_image_ensure(host_id=rec["id"], image=image)
        if not ens.get("ok"):
            return {"ok": False, "image": image, "host_id": rec["id"],
                    "error": "image unavailable and could not be built/"
                             "transferred: " + str(ens.get("error", ""))[:400],
                    "ensure": {k: ens.get(k) for k in ("action", "log", "blocked")}}

    args = ["run", "-d", "--name", name,
            "--label", _WORKER_LABEL,
            "--label", f"vera.spawned_at={now_iso()}",
            "--restart", "unless-stopped",
            "-e", f"REDIS_URL={redis_url}",
            "-e", "ORCHESTRATOR_HOST=0.0.0.0"]
    if inherit_backends:
        for k in _DEFAULT_BACKEND_ENV:
            v = os.getenv(k, "")
            if v:
                args += ["-e", f"{k}={v}"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    if network:
        args += ["--network", network]
    if gpus:
        args += ["--gpus", gpus]
    if extra_args:
        args += shlex.split(extra_args)
    args += [image]

    argv = await _docker_argv(rec, args)
    ok, reason = _sandbox_gate(" ".join(argv))
    if not ok:
        await emit_event({"type": "exec.sandbox.blocked", "shell": "docker.worker.spawn", "reason": reason})
        return {"ok": False, "blocked": True, "error": f"sandbox: {reason}"}

    res = await _run_local(argv, timeout=180)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "docker run failed",
                "rc": res.get("rc"), "host_id": rec["id"]}
    cid = (res.get("stdout", "") or "").strip().splitlines()[-1] if res.get("stdout") else ""
    await emit_event({"type": "docker.worker.spawned", "name": name, "image": image,
                      "container_id": cid[:12], "host_id": rec["id"]})
    return {"ok": True, "container_id": cid, "name": name, "image": image,
            "host_id": rec["id"], "redis_url": redis_url}


@capability(
    "docker.run",
    http_method="POST", http_path="/workers/docker/run", http_tags=["docker"],
    description="Deploy (docker run -d) an arbitrary container on a registered "
                "Docker host. Sandbox-gated. Inputs: host_id (str!), image (str!), "
                "name (str), ports (str — 'host:container,…' comma-sep), env (dict), "
                "volumes (str — 'src:dst,…' comma-sep), network (str), restart "
                "(str='unless-stopped'), extra_args (str), command (str — container "
                "command/args appended AFTER the image), pull (bool=False — pull "
                "the image first). Output: {ok, container_id, name, image, host_id}.",
)
async def cap_docker_run(
    host_id: str = "", image: str = "", name: str = "", ports: str = "",
    env: Optional[Dict[str, str]] = None, volumes: str = "", network: str = "",
    restart: str = "unless-stopped", extra_args: str = "", command: str = "",
    pull: bool = False, trace_id=None,
) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    if not image:
        return {"ok": False, "error": "image required"}
    args = ["run", "-d"]
    if name:
        args += ["--name", name]
    if restart:
        args += ["--restart", restart]
    for p in [x.strip() for x in (ports or "").split(",") if x.strip()]:
        args += ["-p", p]
    for v in [x.strip() for x in (volumes or "").split(",") if x.strip()]:
        args += ["-v", v]
    for k, val in (env or {}).items():
        args += ["-e", f"{k}={val}"]
    if network:
        args += ["--network", network]
    if extra_args:
        args += shlex.split(extra_args)
    args += [image]
    if command:
        args += shlex.split(command)

    argv = await _docker_argv(rec, args)
    ok, reason = _sandbox_gate(" ".join(argv))
    if not ok:
        await emit_event({"type": "exec.sandbox.blocked", "shell": "docker.run", "reason": reason})
        return {"ok": False, "blocked": True, "error": f"sandbox: {reason}"}

    if pull:
        await _run_local(await _docker_argv(rec, ["pull", image]), timeout=300)
    res = await _run_local(argv, timeout=180)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error") or "docker run failed",
                "rc": res.get("rc"), "host_id": rec["id"]}
    cid = (res.get("stdout", "") or "").strip().splitlines()[-1] if res.get("stdout") else ""
    await emit_event({"type": "docker.container.run", "name": name, "image": image,
                      "container_id": cid[:12], "host_id": rec["id"]})
    return {"ok": True, "container_id": cid, "name": name, "image": image, "host_id": rec["id"]}


# ─────────────────────────────────────────────────────────────────────────────
# VERA STACK  —  provision the backing services (docker-compose.yml equivalents)
# on any registered Docker host, one service at a time.
# ─────────────────────────────────────────────────────────────────────────────
def _stack_catalog() -> Dict[str, Dict]:
    """Service catalog mirroring docker-compose.yml (images, ports, volumes,
    env) so the whole Vera stack — postgres, redis, chroma, neo4j, garage,
    ollama — can be provisioned onto any registered Docker host."""
    neo_user = os.getenv("NEO4J_USER", "neo4j")
    neo_pass = os.getenv("NEO4J_PASS", "veraneo4j")
    return {
        "redis": {
            "label": "Redis", "image": "redis:7-alpine",
            "container_name": "vera-redis", "ports": "6379:6379",
            "volumes": "vera-redis-data:/data", "env": {},
            "command": "redis-server --appendonly yes --maxmemory 512mb "
                       "--maxmemory-policy allkeys-lru",
            "note": "event streams · task queues · caching",
        },
        "postgres": {
            "label": "PostgreSQL", "image": "postgres:16-alpine",
            "container_name": "vera-postgres", "ports": "5433:5432",
            "volumes": "vera-pg-data:/var/lib/postgresql/data",
            "env": {"POSTGRES_USER": "admin", "POSTGRES_PASSWORD": "admin",
                    "POSTGRES_DB": "postgres"},
            "command": "", "note": "persistent storage",
        },
        "chromadb": {
            "label": "ChromaDB", "image": "chromadb/chroma:0.5.23",
            "container_name": "vera-chromadb", "ports": "8008:8000",
            "volumes": "vera-chroma-data:/chroma/chroma",
            "env": {"IS_PERSISTENT": "TRUE", "ANONYMIZED_TELEMETRY": "FALSE"},
            "command": "", "note": "vector embeddings store",
        },
        "neo4j": {
            "label": "Neo4j", "image": "neo4j:5-community",
            "container_name": "vera-neo4j", "ports": "7474:7474,7687:7687",
            "volumes": "vera-neo4j-data:/data",
            "env": {"NEO4J_AUTH": f"{neo_user}/{neo_pass}",
                    "NEO4J_PLUGINS": '["apoc"]',
                    "NEO4J_server_memory_heap_max__size": "512m"},
            "command": "", "note": "memory graph database",
        },
        "garage": {
            "label": "Garage (S3)", "image": "dxflrs/garage:v1.0.1",
            "container_name": "vera-garage", "ports": "3900:3900,3903:3903",
            "volumes": "vera-garage-etc:/etc/garage,"
                       "vera-garage-meta:/var/lib/garage/meta,"
                       "vera-garage-data:/var/lib/garage/data",
            "env": {"GARAGE_ALLOW_WORLD_READABLE_SECRETS": "true"},
            # config is written into the vera-garage-etc volume by a one-shot
            # busybox helper before this runs (see docker.stack.deploy)
            "command": "/garage -c /etc/garage/garage.toml server",
            "config_file": "garage/garage.toml",
            "note": "S3 blob store for the data fabric · ring needs bootstrap "
                    "after first start (garage-init)",
        },
        "ollama": {
            "label": "Ollama", "image": "ollama/ollama:latest",
            "container_name": "vera-ollama", "ports": "11434:11434",
            "volumes": "vera-ollama:/root/.ollama", "env": {},
            "command": "", "gpus_hint": True,
            "note": "LLM inference — pass gpus='all' on GPU hosts",
        },
    }


@capability(
    "docker.stack.catalog",
    http_method="GET", http_path="/workers/docker/stack/catalog", http_tags=["docker"],
    memory="off", silent=True,
    description="Catalog of Vera's backing services (mirrors docker-compose.yml): "
                "redis, postgres, chromadb, neo4j, garage, ollama — with image, "
                "ports, volumes and env each deploy would use. Output: "
                "{services:[{id,label,image,container_name,ports,note}]}.",
)
async def cap_docker_stack_catalog(trace_id=None) -> Dict:
    cat = _stack_catalog()
    return {"services": [
        {"id": k, "label": v["label"], "image": v["image"],
         "container_name": v["container_name"], "ports": v["ports"],
         "note": v.get("note", "")}
        for k, v in cat.items()]}


@capability(
    "docker.stack.status",
    http_method="GET", http_path="/workers/docker/stack/status", http_tags=["docker"],
    memory="off", silent=True,
    description="State of each Vera stack service on a Docker host (by container "
                "name). Input: host_id (str). Output: {services:[{id,label,image,"
                "container_name,ports,note,exists,running,status}]}.",
)
async def cap_docker_stack_status(host_id: str = "", trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"error": f"unknown host: {host_id}", "services": []}
    out = []
    for k, v in _stack_catalog().items():
        exists = running = False
        status_txt = ""
        try:
            st, body, _ = await _engine_request(
                rec, "GET", f"/containers/{v['container_name']}/json", timeout=8)
            if st == 200:
                info = json.loads(body or b"{}")
                exists = True
                running = bool((info.get("State") or {}).get("Running"))
                status_txt = (info.get("State") or {}).get("Status", "")
        except Exception:
            pass
        out.append({"id": k, "label": v["label"], "image": v["image"],
                    "container_name": v["container_name"], "ports": v["ports"],
                    "note": v.get("note", ""), "exists": exists,
                    "running": running, "status": status_txt})
    return {"host_id": rec["id"], "services": out}


def _stores_mod():
    """The provision stores module, if loaded. It is the canonical stack-deploy
    path (adds garage admin bootstrap + sealed secrets); docker.stack.deploy
    delegates to it so there is ONE implementation rather than two."""
    m = sys.modules.get("stores_capabilities")
    if m is not None:
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("stores_capabilities"):
            return mod
    return None


@capability(
    "docker.stack.deploy",
    http_method="POST", http_path="/workers/docker/stack/deploy", http_tags=["docker"],
    description="Provision one Vera backing service (from docker.stack.catalog) "
                "onto a Docker host. Delegates to provision.store.deploy — the "
                "canonical path that also generates garage config + bootstraps "
                "its ring and seals generated secrets — so there is a single "
                "deploy implementation. Idempotent: an existing container is "
                "started, not recreated. Inputs: host_id (str!), service (str! — "
                "redis|postgres|chromadb|neo4j|garage|ollama), gpus (str — e.g. "
                "'all', ollama only), env (dict), network (str, legacy path only). "
                "Output: {ok, container_id|started|already, name, host_id}.",
)
async def cap_docker_stack_deploy(host_id: str = "", service: str = "",
                                  gpus: str = "", env: Optional[Dict[str, str]] = None,
                                  network: str = "", trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    svc_key = (service or "").strip().lower()

    # Canonical path: delegate to provision.store.deploy when it knows this
    # service (redis/postgres/chromadb/neo4j/garage/ollama all live there now).
    sm = _stores_mod()
    if sm is not None and svc_key in getattr(sm, "_STORES", {}):
        res = await sm.cap_store_deploy(host_id=rec["id"], store=svc_key,
                                        env=env or None, gpus=gpus)
        st = (res.get("stores") or {}).get(svc_key, {}) if isinstance(res, dict) else {}
        if res.get("error"):
            return {"ok": False, "error": res["error"], "host_id": rec["id"]}
        return {"ok": bool(st.get("ok", res.get("ok"))),
                "name": st.get("container", f"vera-{svc_key}"),
                "container_id": st.get("container_id", ""),
                "already": st.get("already", False),
                "bootstrap": st.get("bootstrap"), "secrets": st.get("secrets"),
                "host_id": rec["id"]}

    # Legacy fallback (stores module not loaded) — original inline logic.
    svc = _stack_catalog().get(svc_key)
    if not svc:
        return {"ok": False, "error": f"unknown service: {service} — see docker.stack.catalog"}
    cname = svc["container_name"]

    # Already there? Start it rather than failing on the name collision.
    try:
        st, body, _ = await _engine_request(rec, "GET", f"/containers/{cname}/json", timeout=8)
        if st == 200:
            info = json.loads(body or b"{}")
            if (info.get("State") or {}).get("Running"):
                return {"ok": True, "already": True, "name": cname, "host_id": rec["id"]}
            st2, _, _ = await _engine_request(rec, "POST", f"/containers/{cname}/start", timeout=30)
            return {"ok": st2 in (204, 304), "started": True, "name": cname,
                    "host_id": rec["id"]}
    except Exception:
        pass

    # Garage needs its config written into the vera-garage-etc volume first
    # (compose bind-mounts ./garage/garage.toml; remote hosts have no repo).
    if svc.get("config_file"):
        cfg_path = _REPO_ROOT / svc["config_file"]
        if cfg_path.exists():
            b64 = base64.b64encode(cfg_path.read_bytes()).decode()
            vol = svc["volumes"].split(":", 1)[0]
            writer = await _docker_argv(rec, [
                "run", "--rm", "-v", f"{vol}:/cfg", "busybox", "sh", "-c",
                f"echo {b64} | base64 -d > /cfg/garage.toml"])
            wres = await _run_local(writer, timeout=120)
            if not wres.get("ok"):
                return {"ok": False, "name": cname, "host_id": rec["id"],
                        "error": "config write failed: " +
                                 (wres.get("stderr") or "")[-300:]}
        else:
            return {"ok": False, "error": f"config file missing: {svc['config_file']}"}

    merged_env = dict(svc.get("env") or {})
    merged_env.update(env or {})
    extra = f"--gpus {shlex.quote(gpus)}" if (gpus and svc.get("gpus_hint")) else ""
    res = await cap_docker_run(
        host_id=rec["id"], image=svc["image"], name=cname,
        ports=svc["ports"], env=merged_env, volumes=svc["volumes"],
        network=network, extra_args=extra, command=svc.get("command", ""),
        pull=True)
    if res.get("ok"):
        await emit_event({"type": "docker.stack.deployed", "service": service,
                          "name": cname, "host_id": rec["id"]})
    return res


@capability(
    "docker.worker.list",
    http_method="GET", http_path="/workers/docker/worker/list", http_tags=["docker", "workers"],
    memory="off",
    description="List Vera worker containers (label vera.role=worker) on a host, "
                "cross-referenced with the live cluster WORKER_REGISTRY. "
                "Input: host_id (str). Output: {workers: [...], registered: int}.",
)
async def cap_docker_worker_list(host_id: str = "", trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"error": f"unknown host: {host_id}", "workers": []}
    import urllib.parse
    filt = urllib.parse.quote(json.dumps({"label": [_WORKER_LABEL]}))
    status, body, _ = await _engine_request(
        rec, "GET", f"/containers/json?all=true&filters={filt}")
    try:
        rows = json.loads(body or b"[]") if status == 200 else []
    except Exception:
        rows = []
    registered = 0
    try:
        registered = len(getattr(_orch, "WORKER_REGISTRY", {}) or {})
    except Exception:
        pass
    workers = [{
        "id": (r.get("Id") or "")[:12],
        "name": (r.get("Names") or ["?"])[0].lstrip("/"),
        "image": r.get("Image", ""),
        "state": r.get("State", ""),
        "status": r.get("Status", ""),
    } for r in rows]
    return {"host_id": rec["id"], "workers": workers, "count": len(workers),
            "registered": registered}


@capability(
    "docker.worker.stop",
    http_method="POST", http_path="/workers/docker/worker/stop", http_tags=["docker", "workers"],
    description="Stop and remove a Vera worker container. "
                "Input: host_id (str), container (str!). Output: {ok}.",
)
async def cap_docker_worker_stop(host_id: str = "", container: str = "", trace_id=None) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    if not container:
        return {"ok": False, "error": "container required"}
    argv = await _docker_argv(rec, ["rm", "-f", container])
    res = await _run_local(argv, timeout=40)
    await emit_event({"type": "docker.worker.stopped", "container": container, "host_id": rec["id"]})
    return {"ok": res.get("ok", False), "rc": res.get("rc"),
            "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}


@APP.post("/workers/docker/worker/logs", include_in_schema=False)
async def docker_worker_logs_stream(request: Request):
    """SSE logs for a Vera worker container. Body: {host_id, container, tail?}."""
    return await docker_logs_stream(request)


log.info("docker_capabilities loaded — host store=%s", _STORE_PATH)
