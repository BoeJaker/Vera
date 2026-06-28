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
import json
import logging
import os
import shlex
import sys
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
    try:
        rows = json.loads(body or b"[]")
    except Exception:
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
    try:
        rows = json.loads(body or b"[]")
    except Exception:
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
                "and consumes the task stream. "
                "Input: host_id (str — Docker host), name (str), image (str), "
                "redis_url (str — default this orchestrator's), network (str), "
                "gpus (str — e.g. 'all'), env (dict — extra -e vars), "
                "inherit_backends (bool=true — copy POSTGRES/NEO4J/OLLAMA env), "
                "extra_args (str). Output: {ok, container_id, name, image, host_id}.",
)
async def cap_docker_worker_spawn(
    host_id: str = "", name: str = "", image: str = "", redis_url: str = "",
    network: str = "", gpus: str = "", env: Optional[Dict[str, str]] = None,
    inherit_backends: bool = True, extra_args: str = "", trace_id=None,
) -> Dict:
    rec = _get_host(host_id)
    if not rec:
        return {"ok": False, "error": f"unknown host: {host_id}"}
    image = image or os.getenv("VERA_WORKER_IMAGE", "vera:latest")
    name = name or f"vera-worker-{uuid.uuid4().hex[:8]}"
    redis_url = redis_url or os.getenv("REDIS_URL", "") or getattr(cfg, "REDIS_URL", "redis://localhost:6379")

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
                "(str='unless-stopped'), extra_args (str), pull (bool=False — pull "
                "the image first). Output: {ok, container_id, name, image, host_id}.",
)
async def cap_docker_run(
    host_id: str = "", image: str = "", name: str = "", ports: str = "",
    env: Optional[Dict[str, str]] = None, volumes: str = "", network: str = "",
    restart: str = "unless-stopped", extra_args: str = "", pull: bool = False,
    trace_id=None,
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
