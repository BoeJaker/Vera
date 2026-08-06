"""
vscode_capabilities.py  —  Central VS Code (code-server) + same-origin proxy
=============================================================================
Makes a **central code-server running next to Vera** the primary IDE surface,
and folds the old Remote-IDE panel into the IDE tab:

  • A `vscode` compose service (or `ide.vscode.central.ensure` on any registered
    Docker host) runs code-server with the shared `vera-projects` volume mounted,
    so the central IDE edits the exact tree the classic IDE / agents work on.
  • **Same-origin reverse proxy** at `/vscode/{instance_id}/…` (HTTP + WebSocket).
    code-server's session cookie is first-party through the proxy, which fixes
    the "can log in standalone but not inside the Vera page" iframe bug — the
    cookie was third-party (SameSite) when the iframe pointed at the raw URL.
    Works for the central instance AND every registered remote code-server.
  • **Interactive workers**: start a code-server sidecar on any session sandbox
    (`sandbox.session.*`) sharing its /workspace volume — work alongside Vera in
    a project's filespace while her exec/code calls run in the same container
    filesystem. The central container also gets the docker socket (compose) so
    `docker exec -it vera-sbx-… sh` works from its integrated terminal.
  • **Password management** for every instance (central / sidecars / remote
    code-server hosts): sealed with security.secrets, revealed on demand for
    the login form, rotated via `ide.vscode.password.set`. Surfaced in the
    Workers → Connections pane next to the SSH credential store.

Instance records live in the SAME registry the Remote-IDE driver uses
(.vera_remote_instances.json) — central is id "central" (kind "central"),
sandbox sidecars are id "sbxw-<session>" (kind "sandbox-worker"), so the
work-queue / autopilot / bridge machinery sees one flat list.

Capabilities (group `ide.vscode.*`)
────────────────────────────────────
  instances                    — union list with proxy paths + status hints
  central.ensure / central.status — deploy/adopt + inspect the central container
  password.set / password.reveal  — rotate / read an instance's code-server password
  sandbox.workers / sandbox.attach / sandbox.detach — interactive-worker sidecars
  panel.html                   — the merged IDE wrapper (GET /ide/vscode/panel)

HTTP-only:
  ANY  /vscode/{iid}/{path}    — reverse proxy (strips frame headers, scopes
                                 cookies to the instance path)
  WS   /vscode/{iid}/{path}    — websocket bridge for the code-server transport
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shlex
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xesc

import httpx
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse,
)

try:
    from Vera.vera.config import cfg as _vcfg
except Exception:                                    # pragma: no cover
    _vcfg = None  # type: ignore

from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso,
)
import Vera.vera.capability_orchestration as _orch

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                    # pragma: no cover
    vsecrets = None  # type: ignore

log = logging.getLogger("vera.ide.vscode")
_HERE = Path(__file__).parent

CENTRAL_ID = "central"
_CENTRAL_CONTAINER = os.getenv("VSCODE_CENTRAL_CONTAINER", "vera-vscode")
_CENTRAL_IMAGE = os.getenv("VSCODE_CENTRAL_IMAGE", "codercom/code-server:latest")
_WORKER_IMAGE = os.getenv("VSCODE_WORKER_IMAGE", _CENTRAL_IMAGE)
_WORKER_PORT_BASE = int(os.getenv("VSCODE_WORKER_PORT_BASE", "8860") or 8860)


# ─────────────────────────────────────────────────────────────────────────────
# SHARED REGISTRY  (delegates to ide_remote_capabilities when loaded, so both
# modules read/write the same in-flight state; falls back to direct file IO)
# ─────────────────────────────────────────────────────────────────────────────
_INSTANCES_FILE = _HERE / ".vera_remote_instances.json"


def _rmod():
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("ide_remote_capabilities") \
                and hasattr(mod, "_load_instances"):
            return mod
    return None


def _load_instances() -> List[dict]:
    m = _rmod()
    if m:
        return m._load_instances()
    try:
        if _INSTANCES_FILE.exists():
            return json.loads(_INSTANCES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("vscode: could not read instances: %s", e)
    return []


def _save_instances(rows: List[dict]) -> None:
    m = _rmod()
    if m:
        m._save_instances(rows)
        return
    try:
        _INSTANCES_FILE.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("vscode: could not write instances: %s", e)


def _get_instance(iid: str) -> Optional[dict]:
    for r in _load_instances():
        if r.get("id") == iid:
            return r
    return None


def _upsert_instance(rec: dict) -> dict:
    rows = _load_instances()
    for r in rows:
        if r.get("id") == rec["id"]:
            r.update(rec)
            r["updated_at"] = now_iso()
            _save_instances(rows)
            return r
    rec.setdefault("created_at", now_iso())
    rec["updated_at"] = now_iso()
    rows.append(rec)
    _save_instances(rows)
    return rec


def _delete_instance(iid: str) -> bool:
    rows = _load_instances()
    keep = [r for r in rows if r.get("id") != iid]
    if len(keep) == len(rows):
        return False
    _save_instances(keep)
    return True


def _seal(plaintext: str) -> str:
    if not plaintext:
        return ""
    if vsecrets:
        try:
            return vsecrets.seal(plaintext)
        except Exception as e:
            log.error("vscode: seal failed (%s) — value NOT stored", e)
    return ""


def _open(sealed: str) -> str:
    if not sealed:
        return ""
    if vsecrets:
        try:
            return vsecrets.open_secret(sealed)
        except Exception:
            return ""
    return ""


def _public(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if k not in ("token_sealed", "api_key_sealed")}
    out["has_token"] = bool(rec.get("token_sealed"))
    out["has_api_key"] = bool(rec.get("api_key_sealed"))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LAZY BACKENDS  (docker engine + session sandboxes — same style as elsewhere)
# ─────────────────────────────────────────────────────────────────────────────
def _dk():
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("docker_capabilities") and hasattr(mod, "_get_host"):
            return mod
    return None


def _sbx_mod():
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("session_sandbox_capabilities") \
                and hasattr(mod, "_get_rec"):
            return mod
    return None


async def _docker_host_addr(rec: dict) -> str:
    """Orchestrator-reachable address of a docker host (for published ports)."""
    kind = rec.get("kind", "local")
    if kind == "tcp":
        url = rec.get("url", "")
        for pre in ("tcp://", "http://", "https://"):
            if url.startswith(pre):
                url = url[len(pre):]
        return url.split("/")[0].split(":")[0] or "127.0.0.1"
    if kind == "ssh":
        # docker host record points at an exec-store SSH host (user@host)
        dk = _dk()
        if dk and hasattr(dk, "_ssh_user_host"):
            try:
                uh = await dk._ssh_user_host(rec.get("ssh_host_id", ""))
                if uh and "@" in uh:
                    return uh.split("@", 1)[1]
                if uh:
                    return uh
            except Exception:
                pass
        return rec.get("host", "") or "127.0.0.1"
    return os.getenv("VSCODE_PUBLIC_HOST", "").strip() \
        or os.getenv("VERA_HOST", "").strip() or "127.0.0.1"


async def _docker_exec(host: dict, container: str, script: str,
                       timeout: int = 300, user: str = "") -> dict:
    dk = _dk()
    if dk is None:
        return {"ok": False, "stderr": "docker module not loaded", "stdout": "", "rc": -1}
    args = ["exec"]
    if user:
        args += ["-u", user]
    args += [container, "sh", "-lc", script]
    return await dk._run_local(await dk._docker_argv(host, args), timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# TARGET RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────
def _central_env_url() -> str:
    return os.getenv("VSCODE_CENTRAL_URL", "").strip().rstrip("/")


def _resolve_target(iid: str) -> Tuple[str, Optional[dict]]:
    """(upstream base url, instance record|None) for a proxy id."""
    inst = _get_instance(iid)
    if inst and inst.get("url"):
        return inst["url"].rstrip("/"), inst
    if iid == CENTRAL_ID:
        env = _central_env_url()
        if env:
            return env, inst
    return "", inst


def _known_password(inst: Optional[dict], iid: str) -> str:
    if inst and inst.get("token_sealed"):
        pw = _open(inst["token_sealed"])
        if pw:
            return pw
    if iid == CENTRAL_ID:
        return os.getenv("VSCODE_PASSWORD", "").strip()
    return ""


# ═════════════════════════════════════════════════════════════════════════════
#  SAME-ORIGIN REVERSE PROXY  —  /vscode/{iid}/{path}
#  Fixes in-iframe login: through the proxy the code-server session cookie is
#  first-party (and scoped to this instance's path), so the login form works
#  embedded in the Vera page exactly like it does standalone.
# ═════════════════════════════════════════════════════════════════════════════
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}
_STRIP_RESP = _HOP_HEADERS | {
    "x-frame-options", "content-security-policy",
    "content-security-policy-report-only", "content-length", "set-cookie",
}


def _rewrite_cookie(value: str, prefix: str) -> str:
    """Scope an upstream Set-Cookie to this instance's proxy path and drop any
    Domain attr so two proxied instances can't clobber each other's session."""
    parts = [p.strip() for p in value.split(";")]
    out = []
    saw_path = False
    for p in parts:
        low = p.lower()
        if low.startswith("domain="):
            continue
        if low.startswith("path="):
            out.append(f"Path={prefix}")
            saw_path = True
            continue
        out.append(p)
    if not saw_path:
        out.append(f"Path={prefix}")
    return "; ".join(out)


def _rewrite_location(value: str, base: str, prefix: str) -> str:
    if value.startswith(base):
        return prefix.rstrip("/") + value[len(base):]
    if value.startswith("/") and not value.startswith(prefix):
        return prefix.rstrip("/") + value
    return value


# ═════════════════════════════════════════════════════════════════════════════
#  QUICK-CONNECT DOWNLOAD ROUTES — registered BEFORE the /vscode/{iid} proxy so
#  the literal /vscode/connect* paths win over the catch-all (Starlette matches
#  in registration order, NOT by specificity; otherwise /vscode/connect resolves
#  to proxy instance id "connect" → 502). Handlers reference helpers + templates
#  defined further down in the QUICK-CONNECT section; those resolve at REQUEST
#  time, so the forward references are fine.
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/vscode/connect/extension.vsix", include_in_schema=False)
async def vscode_connect_vsix():
    pkg = _ext_manifest()
    fname = f"{pkg.get('name', 'vera-vscode')}-{pkg.get('version', '0.0.1')}.vsix"
    try:
        data = _build_vsix_bytes()
    except Exception as e:
        return Response(f"vsix build failed: {e}", status_code=500, media_type="text/plain")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-store"})


@APP.get("/vscode/connect/cert", include_in_schema=False)
async def vscode_connect_cert():
    path = _tls_cert_path()
    if not path:
        return Response("no TLS certificate on this server — TLS is probably "
                        "disabled, so the service-worker SSL error only applies "
                        "to self-signed-HTTPS deployments.",
                        status_code=404, media_type="text/plain")
    try:
        pem = Path(path).read_bytes()
    except Exception as e:
        return Response(f"cert unavailable: {e}", status_code=500, media_type="text/plain")
    # Hand out the ISSUING CA, not the leaf, so trust survives cert rotation and a
    # migration onto FreeIPA's CA without re-distributing per-cert. Self-signed →
    # the cert is its own CA (unchanged behaviour). CA-signed (e.g. after the
    # FreeIPA cutover) → serve the issuing CA (via identity.ca.cert), so every
    # future FreeIPA-issued Vera cert is already trusted and clients auto-update.
    ca_pem = pem
    try:
        from cryptography import x509
        crt = x509.load_pem_x509_certificate(pem)
        if crt.issuer.rfc4514_string() != crt.subject.rfc4514_string():
            reg = _orch.CAPABILITY_REGISTRY.get("identity.ca.cert")
            fn = reg.get("raw") if reg else None
            if fn:
                r = await fn()
                cp = (r or {}).get("ca_pem") if isinstance(r, dict) else None
                if cp and "BEGIN CERTIFICATE" in cp:
                    ca_pem = cp.encode() if isinstance(cp, str) else cp
    except Exception:
        pass  # any failure → fall back to serving the cert file itself
    return Response(content=ca_pem, media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="vera-ca.crt"',
                             "Cache-Control": "no-store"})


@APP.get("/vscode/connect/connect.ps1", include_in_schema=False)
async def vscode_connect_ps1(request: Request, token: str = ""):
    body = (_CONNECT_PS1.replace("__BASE__", _req_base(request))
                        .replace("__TOKEN__", _sanitize_token(token)))
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@APP.get("/vscode/connect/connect.sh", include_in_schema=False)
async def vscode_connect_sh(request: Request, token: str = ""):
    body = (_CONNECT_SH.replace("__BASE__", _req_base(request))
                       .replace("__TOKEN__", _sanitize_token(token)))
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@APP.get("/vscode/connect", include_in_schema=False)
@APP.get("/vscode/connect/", include_in_schema=False)
async def vscode_connect_landing(request: Request):
    return HTMLResponse(_CONNECT_HTML.replace("__BASE__", _req_base(request)))


@APP.get("/vscode/{iid}", include_in_schema=False)
async def vscode_proxy_root(iid: str):
    return RedirectResponse(f"/vscode/{iid}/")


@APP.api_route("/vscode/{iid}/{path:path}", include_in_schema=False,
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def vscode_proxy(iid: str, path: str, request: Request):
    base, _inst = _resolve_target(iid)
    if not base:
        return Response(f"vscode proxy: unknown instance '{iid}'", status_code=502,
                        media_type="text/plain")
    prefix = f"/vscode/{iid}/"
    url = f"{base}/{path}"
    if request.url.query:
        url += "?" + request.url.query

    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    # code-server rejects requests whose Origin host differs from the Host
    # header (cross-site protection) — make Origin agree with the upstream.
    if "origin" in {k.lower() for k in fwd}:
        fwd = {k: v for k, v in fwd.items() if k.lower() != "origin"}
        fwd["origin"] = base
    # keep body bytes exactly as sent (aiter_raw below), so let upstream pick
    # its own encoding and pass content-encoding straight through.
    body = request.stream() if request.method not in ("GET", "HEAD") else None
    client = httpx.AsyncClient(verify=False, follow_redirects=False,
                               timeout=httpx.Timeout(300.0, connect=10.0))
    try:
        req = client.build_request(request.method, url, headers=fwd,
                                   content=body)
        upstream = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        return Response(f"vscode proxy: upstream unreachable ({e})",
                        status_code=502, media_type="text/plain")

    async def _body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    resp = StreamingResponse(_body(), status_code=upstream.status_code)
    for k, v in upstream.headers.items():
        lk = k.lower()
        if lk in _STRIP_RESP:
            continue
        if lk == "location":
            v = _rewrite_location(v, base, prefix)
        resp.headers.append(k, v)
    for c in upstream.headers.get_list("set-cookie"):
        resp.headers.append("set-cookie", _rewrite_cookie(c, prefix))
    return resp


@APP.websocket("/vscode/{iid}/{path:path}")
async def vscode_proxy_ws(ws: WebSocket, iid: str, path: str):
    base, _inst = _resolve_target(iid)
    if not base:
        await ws.close(code=4404)
        return
    try:
        import websockets
    except Exception:
        log.error("vscode ws proxy: `websockets` package unavailable")
        await ws.close(code=1011)
        return

    scheme = "wss" if base.startswith("https") else "ws"
    hostpart = base.split("://", 1)[1]
    url = f"{scheme}://{hostpart}/{path}"
    if ws.url.query:
        url += "?" + ws.url.query

    # Forward auth-bearing headers but REWRITE Origin to the upstream — code-server
    # rejects the upgrade (403 → client sees a bare 1006) when Origin's host differs
    # from its own Host. websockets sets Host itself from the URL; don't pass it on.
    hdrs = [(k, v) for k, v in ws.headers.items()
            if k.lower() in ("cookie", "authorization", "user-agent")]
    hdrs.append(("Origin", base))
    subprotos = [p.strip() for p in
                 ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]

    connect_kw: Dict[str, Any] = {"max_size": None, "open_timeout": 15, "ssl": None}
    if scheme == "wss":
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        connect_kw["ssl"] = ctx
    else:
        connect_kw.pop("ssl")
    if subprotos:
        connect_kw["subprotocols"] = subprotos

    try:
        try:
            upstream = await websockets.connect(url, additional_headers=hdrs, **connect_kw)
        except TypeError:   # websockets < 13 uses extra_headers
            upstream = await websockets.connect(url, extra_headers=hdrs, **connect_kw)
    except Exception as e:
        # Surface the real reason (403 origin-check, 502 upstream down, …) — a
        # bare 1006 in the browser is otherwise undiagnosable.
        log.warning("vscode ws proxy connect failed for %s (%s): %s",
                    iid, url, e)
        try:
            await ws.accept()          # accept so we can send a close reason
            await ws.close(code=1011, reason=f"upstream ws refused: {type(e).__name__}")
        except Exception:
            pass
        return

    accepted = getattr(upstream, "subprotocol", None)
    await ws.accept(subprotocol=accepted)

    async def _client_to_upstream():
        while True:
            msg = await ws.receive()
            t = msg.get("type")
            if t == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def _upstream_to_client():
        async for data in upstream:
            if isinstance(data, (bytes, bytearray)):
                await ws.send_bytes(bytes(data))
            else:
                await ws.send_text(data)

    t1 = asyncio.ensure_future(_client_to_upstream())
    t2 = asyncio.ensure_future(_upstream_to_client())
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        for t in (t1, t2):
            t.cancel()
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  INSTANCE UNION LIST
# ═════════════════════════════════════════════════════════════════════════════
_KIND_ORDER = {"central": 0, "sandbox-worker": 1, "container": 2,
               "code-server": 3, "tunnel": 4, "ssh": 5}


@capability(
    "ide.vscode.instances",
    http_method="GET", http_path="/ide/vscode/instances", http_tags=["ide", "vscode"],
    memory="off", silent=True,
    description="List every VS Code surface Vera can embed: the central "
                "code-server, sandbox interactive workers, and all Remote-IDE "
                "instances. Each row carries proxy ('/vscode/<id>/' — same-origin, "
                "iframe-safe) when the upstream is resolvable. Secrets redacted. "
                "Output: {instances: [{id, label, kind, url, proxy, status, "
                "has_token, ...}], count}.",
)
async def ide_vscode_instances(trace_id=None):
    rows = [_public(r) for r in _load_instances()]
    if not any(r.get("id") == CENTRAL_ID for r in rows) and _central_env_url():
        rows.insert(0, {
            "id": CENTRAL_ID, "label": "Central VS Code (vera)", "kind": "central",
            "url": _central_env_url(), "status": "unknown",
            "has_token": bool(os.getenv("VSCODE_PASSWORD", "").strip()),
        })
    for r in rows:
        base, _ = _resolve_target(r.get("id", ""))
        r["proxy"] = f"/vscode/{r['id']}/" if base else ""
    rows.sort(key=lambda r: (_KIND_ORDER.get(r.get("kind", ""), 9),
                             r.get("label", "") or ""))
    return {"instances": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  CENTRAL INSTANCE  —  deploy / adopt / inspect / integrations
# ═════════════════════════════════════════════════════════════════════════════
def _vera_base_url() -> str:
    m = _rmod()
    if m and hasattr(m, "_vera_base_url"):
        return m._vera_base_url()
    port = os.getenv("VERA_PORT", "").strip() or os.getenv("ORCHESTRATOR_PORT", "8999")
    host = os.getenv("VERA_HOST", "").strip() or "127.0.0.1"
    return f"http://{host}:{port}"


async def _central_integrations(host: dict, container: str,
                                projects_dir: str) -> List[dict]:
    """Best-effort in-container setup: Claude Code CLI, extensions, MCP bridge,
    docker CLI (when the socket is mounted). Every step is non-fatal."""
    steps: List[dict] = []

    r = await _docker_exec(host, container,
                           "command -v claude >/dev/null 2>&1 && claude --version "
                           "|| (curl -fsSL https://claude.ai/install.sh | bash "
                           "&& ~/.local/bin/claude --version)", timeout=420)
    steps.append({"step": "claude-cli", "ok": bool(r.get("ok")),
                  "tail": (r.get("stdout", "") or r.get("stderr", ""))[-200:]})

    exts = [e.strip() for e in
            os.getenv("VSCODE_CENTRAL_EXTENSIONS", "").split(",") if e.strip()]
    for ext in exts:
        r = await _docker_exec(host, container,
                               f"code-server --install-extension {shlex.quote(ext)}",
                               timeout=300)
        steps.append({"step": f"ext:{ext}", "ok": bool(r.get("ok"))})

    # MCP bridge: lets Claude Code inside the container call Vera capabilities.
    try:
        src = (_HERE / "vera_mcp_bridge.py").read_text(encoding="utf-8")
        b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
        vera_url = _vera_base_url()
        allow = "ide.,fabric.,memory.,dream.,project.,web."
        mcp_json = json.dumps({"mcpServers": {"vera": {
            "command": "python3",
            "args": ["/home/coder/.vera/vera_mcp_bridge.py",
                     "--url", vera_url, "--allow", allow]}}})
        script = (f"mkdir -p ~/.vera && printf %s {shlex.quote(b64)} | base64 -d "
                  f"> ~/.vera/vera_mcp_bridge.py && "
                  f"printf %s {shlex.quote(mcp_json)} > {shlex.quote(projects_dir)}/.mcp.json "
                  f"&& echo BRIDGE_OK")
        r = await _docker_exec(host, container, script, timeout=60)
        steps.append({"step": "mcp-bridge",
                      "ok": "BRIDGE_OK" in (r.get("stdout", "") or "")})
    except Exception as e:
        steps.append({"step": "mcp-bridge", "ok": False, "tail": str(e)[:160]})

    # docker CLI so the central terminal can `docker exec` into session sandboxes
    # (only when the compose file mounted /var/run/docker.sock).
    r = await _docker_exec(host, container,
                           "test -S /var/run/docker.sock && echo SOCK || echo NOSOCK",
                           timeout=20)
    if "SOCK" in (r.get("stdout", "") or "") and "NOSOCK" not in (r.get("stdout") or ""):
        cli = ("command -v docker >/dev/null 2>&1 || "
               "(curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz "
               "| tar xz -C /tmp && (sudo cp /tmp/docker/docker /usr/local/bin/ 2>/dev/null "
               "|| (mkdir -p ~/.local/bin && cp /tmp/docker/docker ~/.local/bin/))); "
               "docker version --format ok 2>/dev/null || true")
        r = await _docker_exec(host, container, cli, timeout=300)
        steps.append({"step": "docker-cli", "ok": bool(r.get("ok"))})
    return steps


@capability(
    "ide.vscode.central.ensure",
    http_method="POST", http_path="/ide/vscode/central/ensure", http_tags=["ide", "vscode"],
    memory="on",
    description="Deploy (or adopt) the central code-server container `vera-vscode` "
                "on a registered Docker host, mount the shared vera-projects volume, "
                "run the integration steps (Claude Code CLI, extensions from "
                "VSCODE_CENTRAL_EXTENSIONS, Vera MCP bridge, docker CLI), and "
                "register it as instance 'central'. If the compose service already "
                "runs it, the container is adopted as-is. "
                "Input: docker_host_id (str='local'), port (int=8843 host port), "
                "password (str — blank keeps/derives one), image (str), "
                "projects_volume (str), recreate (bool=False). "
                "Output: {ok, instance, url, proxy, password, steps}.",
)
async def ide_vscode_central_ensure(
    docker_host_id:  str  = "local",
    port:            int  = 0,
    password:        str  = "",
    image:           str  = "",
    projects_volume: str  = "",
    recreate:        bool = False,
    trace_id=None,
):
    dk = _dk()
    if dk is None:
        return {"ok": False, "error": "docker module not loaded"}
    host = dk._get_host(docker_host_id or "local")
    if not host:
        return {"ok": False, "error": f"unknown docker host: {docker_host_id}"}

    port = int(port or os.getenv("VSCODE_CENTRAL_PORT", "8843") or 8843)
    image = image or _CENTRAL_IMAGE
    projects_volume = projects_volume \
        or os.getenv("VSCODE_PROJECTS_VOLUME", "").strip() or "vera_vera-projects"
    existing = _get_instance(CENTRAL_ID)
    pw = password or _known_password(existing, CENTRAL_ID) or uuid.uuid4().hex
    steps: List[dict] = []

    sbx = _sbx_mod()
    state = await sbx._container_running(dk, host, _CENTRAL_CONTAINER) if sbx else None
    adopted = False
    if state == "running" and not recreate and not password:
        adopted = True
        steps.append({"step": "adopt", "ok": True,
                      "tail": "container already running — adopted"})
    else:
        if state is not None:
            await dk._run_local(
                await dk._docker_argv(host, ["rm", "-f", _CENTRAL_CONTAINER]), timeout=60)
        network = os.getenv("VSCODE_CENTRAL_NETWORK", "").strip()

        async def _run(net: str):
            # keep the compose DNS name alive when re-created onto vera-net,
            # so VSCODE_CENTRAL_URL=http://vscode:8080 keeps resolving.
            return await dk.cap_docker_run(
                host_id=host["id"], image=image, name=_CENTRAL_CONTAINER,
                ports=f"{port}:8080",
                env={"PASSWORD": pw},
                volumes=f"vera-vscode-data:/home/coder,{projects_volume}:/home/coder/projects",
                network=net,
                extra_args="--network-alias vscode" if net else "",
                command="--bind-addr 0.0.0.0:8080 --auth password /home/coder/projects",
                pull=True)

        run = await _run(network)
        if not run.get("ok") and network and "network" in (run.get("error") or "").lower():
            run = await _run("")
        steps.append({"step": "run", "ok": bool(run.get("ok")),
                      "tail": (run.get("error") or run.get("container_id", ""))[:200]})
        if not run.get("ok"):
            return {"ok": False, "error": run.get("error", "docker run failed"),
                    "steps": steps}
        await asyncio.sleep(2)

    steps += await _central_integrations(host, _CENTRAL_CONTAINER, "/home/coder/projects")

    url = _central_env_url() or f"http://{await _docker_host_addr(host)}:{port}"
    rec = _upsert_instance({
        "id": CENTRAL_ID, "label": "Central VS Code (vera)", "kind": "central",
        "url": url, "port": port, "image": image,
        "docker_host_id": host["id"], "container": _CENTRAL_CONTAINER,
        "projects_volume": projects_volume,
        "workdir": "/home/coder/projects",
        "status": "running",
        "token_sealed": _seal(pw),
    })
    await emit_event({"type": "ide.vscode.central", "action": "ensured",
                      "url": url, "adopted": adopted})
    return {"ok": True, "instance": _public(rec), "url": url,
            "proxy": f"/vscode/{CENTRAL_ID}/", "password": pw,
            "adopted": adopted, "steps": steps}


@capability(
    "ide.vscode.central.status",
    http_method="GET", http_path="/ide/vscode/central/status", http_tags=["ide", "vscode"],
    memory="off", silent=True,
    description="Central code-server health: container state (when docker-managed) "
                "+ HTTP reachability of the upstream. "
                "Output: {ok, exists, running, reachable, url, proxy, has_token}.",
)
async def ide_vscode_central_status(trace_id=None):
    inst = _get_instance(CENTRAL_ID)
    base, _ = _resolve_target(CENTRAL_ID)
    out: Dict[str, Any] = {
        "ok": True, "exists": bool(inst or base), "url": base,
        "proxy": f"/vscode/{CENTRAL_ID}/" if base else "",
        "has_token": bool((inst or {}).get("token_sealed")
                          or os.getenv("VSCODE_PASSWORD", "").strip()),
        "running": None, "reachable": False,
    }
    dk, sbx = _dk(), _sbx_mod()
    if dk and sbx and inst and inst.get("container"):
        host = dk._get_host(inst.get("docker_host_id", "local"))
        if host:
            state = await sbx._container_running(dk, host, inst["container"])
            out["running"] = (state == "running")
    if base:
        try:
            async with httpx.AsyncClient(timeout=6, verify=False) as c:
                r = await c.get(base + "/healthz")
                out["reachable"] = r.status_code < 500
        except Exception as e:
            out["reach_error"] = str(e)[:160]
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  PASSWORD MANAGEMENT  (central / sandbox workers / remote code-server hosts)
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "ide.vscode.password.reveal",
    http_method="GET", http_path="/ide/vscode/password/reveal", http_tags=["ide", "vscode"],
    memory="off", silent=True,
    description="Reveal the code-server login password for an instance (for the "
                "login form / clipboard). Input: instance_id (str!). "
                "Output: {ok, instance_id, password}.",
)
async def ide_vscode_password_reveal(instance_id: str = "", trace_id=None):
    inst = _get_instance(instance_id)
    pw = _known_password(inst, instance_id)
    if not pw:
        return {"ok": False, "error": "no stored password for this instance"}
    return {"ok": True, "instance_id": instance_id, "password": pw}


@capability(
    "ide.vscode.password.set",
    http_method="POST", http_path="/ide/vscode/password/set", http_tags=["ide", "vscode"],
    memory="on",
    description="Set/rotate an instance's code-server password. central and "
                "sandbox-worker instances are redeployed with the new PASSWORD; "
                "kind=code-server hosts get config.yaml rewritten + restarted over "
                "SSH; tunnel instances just store it. Blank password generates one. "
                "Input: instance_id (str!), password (str). "
                "Output: {ok, instance_id, password}.",
)
async def ide_vscode_password_set(instance_id: str = "", password: str = "",
                                  trace_id=None):
    inst = _get_instance(instance_id)
    if not inst and instance_id != CENTRAL_ID:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    pw = password or uuid.uuid4().hex
    kind = (inst or {}).get("kind", "central" if instance_id == CENTRAL_ID else "")

    if kind == "central" or instance_id == CENTRAL_ID:
        res = await ide_vscode_central_ensure(
            docker_host_id=(inst or {}).get("docker_host_id", "local"),
            port=int((inst or {}).get("port", 0) or 0),
            password=pw, recreate=True)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error", "redeploy failed")}
        return {"ok": True, "instance_id": CENTRAL_ID, "password": pw,
                "redeployed": True}

    if kind == "sandbox-worker":
        sid = inst.get("session_id", "")
        res = await ide_vscode_sandbox_attach(
            session_id=sid, password=pw, port=int(inst.get("port", 0) or 0),
            recreate=True)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error", "redeploy failed")}
        return {"ok": True, "instance_id": instance_id, "password": pw,
                "redeployed": True}

    if kind == "code-server" and inst.get("host_id"):
        m = _rmod()
        if not m:
            return {"ok": False, "error": "remote IDE module not loaded"}
        port = int(inst.get("port", 8080) or 8080)
        cmd = ("mkdir -p ~/.config/code-server && "
               f"printf 'bind-addr: 0.0.0.0:{port}\\nauth: password\\npassword: {pw}\\ncert: false\\n' "
               "> ~/.config/code-server/config.yaml && "
               "(systemctl --user restart code-server 2>/dev/null "
               "|| (pkill -f code-server; nohup code-server >/tmp/code-server.log 2>&1 &)) ; "
               "echo PW_SET")
        res = await m._ssh(inst["host_id"], cmd, timeout=90)
        if "PW_SET" not in (res.get("stdout", "") or ""):
            return {"ok": False,
                    "error": res.get("error") or res.get("stderr") or "remote update failed"}

    rows = _load_instances()
    for r in rows:
        if r.get("id") == instance_id:
            r["token_sealed"] = _seal(pw)
            r["updated_at"] = now_iso()
    _save_instances(rows)
    await emit_event({"type": "ide.vscode.password", "action": "rotated",
                      "instance_id": instance_id})
    return {"ok": True, "instance_id": instance_id, "password": pw}


# ═════════════════════════════════════════════════════════════════════════════
#  SANDBOX INTERACTIVE WORKERS  —  code-server sidecar on a session sandbox
# ═════════════════════════════════════════════════════════════════════════════
def _worker_id(session_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "_.-") else "-" for c in session_id)[:48]
    return f"sbxw-{safe}"


def _pick_port() -> int:
    used = set()
    for r in _load_instances():
        try:
            used.add(int(r.get("port", 0) or 0))
        except Exception:
            pass
    p = _WORKER_PORT_BASE
    while p in used:
        p += 1
    return p


@capability(
    "ide.vscode.sandbox.workers",
    http_method="GET", http_path="/ide/vscode/sandbox/workers", http_tags=["ide", "vscode"],
    memory="off", silent=True,
    description="Interactive-worker overview: session sandboxes (from "
                "sandbox.session.list) joined with any code-server sidecars already "
                "attached. Output: {sandboxes: [{session_id, container, image, "
                "active, worker: {id, url, proxy}|null}], count}.",
)
async def ide_vscode_sandbox_workers(trace_id=None):
    sbx = _sbx_mod()
    sandboxes: List[dict] = []
    if sbx:
        try:
            res = await sbx.cap_sbx_list()
            sandboxes = res.get("sandboxes", [])
        except Exception as e:
            log.debug("vscode: sandbox list failed: %s", e)
    workers = {r.get("session_id"): r for r in _load_instances()
               if r.get("kind") == "sandbox-worker"}
    out = []
    for s in sandboxes:
        w = workers.get(s.get("session_id"))
        out.append({**s, "worker": ({"id": w["id"], "url": w.get("url", ""),
                                     "proxy": f"/vscode/{w['id']}/"} if w else None)})
    # sidecars whose sandbox record has vanished still deserve a row
    for sid, w in workers.items():
        if not any(s.get("session_id") == sid for s in sandboxes):
            out.append({"session_id": sid, "container": None, "orphan": True,
                        "worker": {"id": w["id"], "url": w.get("url", ""),
                                   "proxy": f"/vscode/{w['id']}/"}})
    return {"sandboxes": out, "count": len(out)}


@capability(
    "ide.vscode.sandbox.attach",
    http_method="POST", http_path="/ide/vscode/sandbox/attach", http_tags=["ide", "vscode"],
    memory="on",
    description="Start an interactive worker: a code-server sidecar sharing a "
                "session sandbox's /workspace volume, so you can work alongside "
                "Vera in that session's filespace. Registers instance "
                "'sbxw-<session>' (kind sandbox-worker) behind the same-origin "
                "proxy. Input: session_id (str!), port (int — auto), password "
                "(str — generated), image (str), recreate (bool=False). "
                "Output: {ok, instance, url, proxy, password}.",
)
async def ide_vscode_sandbox_attach(
    session_id: str = "", port: int = 0, password: str = "",
    image: str = "", recreate: bool = False, trace_id=None,
):
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    dk, sbx = _dk(), _sbx_mod()
    if dk is None or sbx is None:
        return {"ok": False, "error": "docker/sandbox modules not loaded"}
    srec = await sbx._get_rec(session_id)
    if not srec or not srec.get("container"):
        return {"ok": False, "error": "no sandbox for this session — start it first "
                                      "(sandbox.session.start)"}
    host = dk._get_host(srec.get("docker_host_id", "local"))
    if not host:
        return {"ok": False, "error": "sandbox's docker host unavailable"}

    iid = _worker_id(session_id)
    existing = _get_instance(iid)
    cname = srec["container"] + "-code"
    vol = srec.get("volume") or sbx._volname(session_id)
    port = int(port or (existing or {}).get("port", 0) or 0) or _pick_port()
    pw = password or _known_password(existing, iid) or uuid.uuid4().hex

    state = await sbx._container_running(dk, host, cname)
    if state == "running" and not recreate and not password:
        pass    # keep the running sidecar; just refresh the record below
    else:
        if state is not None:
            await dk._run_local(await dk._docker_argv(host, ["rm", "-f", cname]),
                                timeout=60)
        # -u 0 + direct binary: sandbox files are usually root-owned (the sandbox
        # base images run as root), so the sidecar edits them as root too.
        run = await dk.cap_docker_run(
            host_id=host["id"], image=image or _WORKER_IMAGE, name=cname,
            ports=f"{port}:8080", env={"PASSWORD": pw},
            volumes=f"{vol}:/home/coder/workspace",
            extra_args=f"-u 0 --entrypoint /usr/bin/code-server "
                       f"--label vera.sandbox.worker={session_id}",
            command="--bind-addr 0.0.0.0:8080 --auth password /home/coder/workspace",
            pull=False)
        if not run.get("ok"):
            return {"ok": False, "error": run.get("error", "docker run failed")}

    url = f"http://{await _docker_host_addr(host)}:{port}"
    rec = _upsert_instance({
        "id": iid, "label": f"Sandbox worker · {session_id[:20]}",
        "kind": "sandbox-worker", "session_id": session_id,
        "url": url, "port": port, "container": cname,
        "docker_host_id": host["id"], "workdir": "/home/coder/workspace",
        "status": "running", "token_sealed": _seal(pw),
    })
    await emit_event({"type": "ide.vscode.worker", "action": "attached",
                      "session_id": session_id, "url": url})
    return {"ok": True, "instance": _public(rec), "url": url,
            "proxy": f"/vscode/{iid}/", "password": pw}


@capability(
    "ide.vscode.sandbox.detach",
    http_method="POST", http_path="/ide/vscode/sandbox/detach", http_tags=["ide", "vscode"],
    memory="on",
    description="Stop and remove a sandbox's interactive-worker sidecar (the "
                "sandbox container + its volume are untouched). "
                "Input: session_id (str!) OR instance_id (str). Output: {ok}.",
)
async def ide_vscode_sandbox_detach(session_id: str = "", instance_id: str = "",
                                    trace_id=None):
    iid = instance_id or _worker_id(session_id)
    inst = _get_instance(iid)
    if not inst:
        return {"ok": False, "error": f"worker not found: {iid}"}
    dk = _dk()
    if dk and inst.get("container"):
        host = dk._get_host(inst.get("docker_host_id", "local"))
        if host:
            await dk._run_local(
                await dk._docker_argv(host, ["rm", "-f", inst["container"]]), timeout=60)
    _delete_instance(iid)
    await emit_event({"type": "ide.vscode.worker", "action": "detached", "id": iid})
    return {"ok": True, "removed": iid}


# ═════════════════════════════════════════════════════════════════════════════
#  QUICK-CONNECT  —  package the extension + one-shot client wiring + cert trust
#  ---------------------------------------------------------------------------
#  Everything a user's editor needs to talk to this Vera, served for download:
#    GET /vscode/connect                 → landing page (one-liners + downloads)
#    GET /vscode/connect/extension.vsix  → the vera-vscode extension, packaged
#                                          in-process (a .vsix is just an OPC zip,
#                                          so no vsce / Node / container needed)
#    GET /vscode/connect/connect.ps1     → Windows quick-connect script
#    GET /vscode/connect/connect.sh      → macOS/Linux quick-connect script
#    GET /vscode/connect/cert            → Vera's TLS cert (PEM). Trusting it on
#                                          the client is the fix for the
#                                          code-server webview "service worker
#                                          SSL error" behind self-signed HTTPS.
#  The scripts install the extension, turn on CLIENT MODE (so Vera can queue
#  Claude Code tasks straight into that window — kind=vscode-client), and trust
#  the cert in one shot.
# ═════════════════════════════════════════════════════════════════════════════
_EXT_DIR = _HERE.parent.parent / "tools" / "vera-vscode"
_VSIX_SKIP_DIRS = {"node_modules", ".vscode-test", ".git", "dist-test"}
_VSIX_SKIP_EXT = {".vsix"}


def _public_base() -> str:
    """Best public origin of this Vera server (scheme + host + port)."""
    scheme = "https" if getattr(_vcfg, "TLS_ENABLED", False) else "http"
    host = (getattr(_vcfg, "BACKEND_HOST", "") or os.getenv("BACKEND_HOST", "")
            or os.getenv("VERA_HOST", "") or "127.0.0.1")
    port = (os.getenv("ORCHESTRATOR_PORT", "") or os.getenv("VERA_PORT", "") or "8999")
    return f"{scheme}://{host}:{port}"


def _req_base(request: Request) -> str:
    """Origin the client actually reached (honours reverse-proxy headers) so the
    generated scripts point back at the same host the browser used."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme
             or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host")
            or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}" if host else _public_base()


def _tls_cert_path() -> str:
    for cand in (getattr(_vcfg, "TLS_CERTFILE", "") or "",
                 os.getenv("TLS_CERTFILE", ""), "~/.vera/tls/cert.pem"):
        if not cand:
            continue
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return ""


# ── VSIX packaging (a .vsix is an OPC zip; `code --install-extension` accepts it
#    without vsce ever running) ────────────────────────────────────────────────
def _ext_manifest() -> dict:
    try:
        return json.loads((_EXT_DIR / "package.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _vsix_content_types() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="json" ContentType="application/json"/>'
        '<Default Extension="js" ContentType="application/javascript"/>'
        '<Default Extension="md" ContentType="text/markdown"/>'
        '<Default Extension="svg" ContentType="image/svg+xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="vsixmanifest" ContentType="text/xml"/>'
        '</Types>'
    )


def _vsix_manifest(pkg: dict) -> str:
    e = _xesc
    ident = pkg.get("name", "vera-vscode")
    version = pkg.get("version", "0.0.1")
    publisher = pkg.get("publisher", "vera")
    display = pkg.get("displayName", ident)
    desc = pkg.get("description", "")
    engine = str((pkg.get("engines", {}) or {}).get("vscode", "^1.85.0")).lstrip("^")
    cats = pkg.get("categories") or ["Other"]
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PackageManifest Version="2.0.0" '
        'xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" '
        'xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">'
        '<Metadata>'
        f'<Identity Language="en-US" Id="{e(ident)}" Version="{e(version)}" '
        f'Publisher="{e(publisher)}"/>'
        f'<DisplayName>{e(display)}</DisplayName>'
        f'<Description xml:space="preserve">{e(desc)}</Description>'
        '<Tags>vera,claude,mcp,ai</Tags>'
        f'<Categories>{e(",".join(cats))}</Categories>'
        '<GalleryFlags>Public</GalleryFlags>'
        '<Properties>'
        f'<Property Id="Microsoft.VisualStudio.Code.Engine" Value="^{e(engine)}"/>'
        '<Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace,ui"/>'
        '</Properties>'
        '</Metadata>'
        '<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>'
        '<Dependencies/>'
        '<Assets>'
        '<Asset Type="Microsoft.VisualStudio.Code.Manifest" '
        'Path="extension/package.json" Addressable="true"/>'
        '<Asset Type="Microsoft.VisualStudio.Services.Content.Details" '
        'Path="extension/README.md" Addressable="true"/>'
        '</Assets>'
        '</PackageManifest>'
    )


def _iter_ext_files():
    for p in sorted(_EXT_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(_EXT_DIR)
        if any(part in _VSIX_SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in _VSIX_SKIP_EXT:
            continue
        yield p, "/".join(rel.parts)


def _build_vsix_bytes() -> bytes:
    """Package tools/vera-vscode into a valid .vsix entirely in-process."""
    if not _EXT_DIR.exists():
        raise FileNotFoundError(f"extension source not found at {_EXT_DIR}")
    pkg = _ext_manifest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _vsix_content_types())
        z.writestr("extension.vsixmanifest", _vsix_manifest(pkg))
        for p, rel in _iter_ext_files():
            z.writestr("extension/" + rel, p.read_bytes())
    return buf.getvalue()


# ── Quick-connect scripts (tokens filled per-request) ────────────────────────
_CONNECT_PS1 = r'''
param(
  [string]$BaseUrl = "__BASE__",
  [string]$Token   = "__TOKEN__",
  [switch]$NoCert,
  [switch]$NoClientMode
)
$ErrorActionPreference = "Stop"
Write-Host ""
Write-Host "  Vera :: Connect VS Code + Claude Code  ->  $BaseUrl" -ForegroundColor Cyan
Write-Host ""

function Get-VeraFile([string]$Url,[string]$OutFile){
  if($PSVersionTable.PSVersion.Major -ge 6){
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -SkipCertificateCheck -UseBasicParsing
  } else {
    try{
      Add-Type -TypeDefinition @"
using System.Net;using System.Security.Cryptography.X509Certificates;
public class VeraTrustAll : ICertificatePolicy { public bool CheckValidationResult(ServicePoint s,X509Certificate c,WebRequest r,int p){return true;} }
"@ -ErrorAction SilentlyContinue
    }catch{}
    [System.Net.ServicePointManager]::CertificatePolicy = New-Object VeraTrustAll
    [System.Net.ServicePointManager]::SecurityProtocol  = [System.Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
  }
}

function Find-Code {
  foreach($c in @("code","code-insiders","codium","code-server")){
    $g = Get-Command $c -ErrorAction SilentlyContinue
    if($g){ return $g.Source }
  }
  return $null
}

$code = Find-Code
if(-not $code){
  throw "No VS Code CLI on PATH. In VS Code: Ctrl+Shift+P -> 'Shell Command: Install code command in PATH', then re-run."
}
Write-Host "  VS Code CLI : $code"

if(-not $NoCert -and $BaseUrl -like "https:*"){
  try{
    $crt = Join-Path $env:TEMP "vera-ca.crt"
    Get-VeraFile "$BaseUrl/vscode/connect/cert" $crt
    if((Test-Path $crt) -and (Get-Item $crt).Length -gt 0){
      Import-Certificate -FilePath $crt -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
      Write-Host "  Certificate : trusted (CurrentUser\Root) - restart the browser to clear the webview SSL error" -ForegroundColor Green
    }
  }catch{ Write-Warning "cert trust skipped: $($_.Exception.Message)" }
}

$vsix = Join-Path $env:TEMP "vera-vscode.vsix"
Get-VeraFile "$BaseUrl/vscode/connect/extension.vsix" $vsix
& $code --install-extension $vsix --force
Write-Host "  Extension   : installed" -ForegroundColor Green

if(-not $NoClientMode){
  $userDir = Join-Path $env:APPDATA "Code\User"
  if($code -like "*insiders*"){ $userDir = Join-Path $env:APPDATA "Code - Insiders\User" }
  if(-not (Test-Path $userDir)){ New-Item -ItemType Directory -Force -Path $userDir | Out-Null }
  $sp = Join-Path $userDir "settings.json"
  $obj = @{}
  if(Test-Path $sp){
    try{
      $raw = Get-Content -Raw -Path $sp
      $raw = [regex]::Replace($raw,'(?m)^\s*//.*$','')
      $parsed = $raw | ConvertFrom-Json
      if($parsed){ $parsed.PSObject.Properties | ForEach-Object { $obj[$_.Name] = $_.Value } }
    }catch{ Copy-Item $sp "$sp.vera-backup" -Force; Write-Warning "settings.json unparseable - backed up to settings.json.vera-backup" }
  }
  $obj["vera.baseUrl"]    = $BaseUrl
  $obj["vera.clientMode"] = $true
  if($Token){ $obj["vera.clientToken"] = $Token }
  ($obj | ConvertTo-Json -Depth 30) | Set-Content -Path $sp -Encoding UTF8
  Write-Host "  Client mode : ON - Vera can now queue Claude Code tasks to this window" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Done. Reload VS Code (Ctrl+Shift+P -> 'Developer: Reload Window')." -ForegroundColor Cyan
Write-Host "  Queue a task from Vera (IDE -> Remotes & Queue) or via ide.remote.client.dispatch." -ForegroundColor DarkGray
Write-Host ""
'''.lstrip()

_CONNECT_SH = r'''
#!/usr/bin/env bash
set -euo pipefail
BASE="${VERA_BASE:-__BASE__}"
TOKEN="${VERA_TOKEN:-__TOKEN__}"
echo ""
echo "  Vera :: Connect VS Code + Claude Code  ->  $BASE"
echo ""

dl(){ curl -fsSL -k "$1" -o "$2"; }

CODE="$(command -v code || command -v code-insiders || command -v codium || command -v code-server || true)"
if [ -z "$CODE" ]; then
  echo "  ERROR: no VS Code CLI on PATH (code / code-insiders / codium)." >&2
  exit 1
fi
echo "  VS Code CLI : $CODE"

case "$BASE" in https:*) HTTPS=1;; *) HTTPS=0;; esac
if [ "${NO_CERT:-0}" != "1" ] && [ "$HTTPS" = "1" ]; then
  crt="$(mktemp).crt"
  if dl "$BASE/vscode/connect/cert" "$crt" && [ -s "$crt" ]; then
    if [ "$(uname)" = "Darwin" ]; then
      sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$crt" 2>/dev/null \
        && echo "  Certificate : trusted (System keychain)" || echo "  Certificate : skipped (needs admin)"
    elif command -v update-ca-certificates >/dev/null 2>&1; then
      sudo cp "$crt" /usr/local/share/ca-certificates/vera-ca.crt && sudo update-ca-certificates >/dev/null 2>&1 \
        && echo "  Certificate : trusted (update-ca-certificates)" || echo "  Certificate : skipped (needs sudo)"
    else
      echo "  Certificate : skipped (import $crt into your trust store manually)"
    fi
  fi
fi

vsix="$(mktemp).vsix"
dl "$BASE/vscode/connect/extension.vsix" "$vsix"
"$CODE" --install-extension "$vsix" --force
echo "  Extension   : installed"

if [ "${NO_CLIENT_MODE:-0}" != "1" ]; then
  if [ "$(uname)" = "Darwin" ]; then dir="$HOME/Library/Application Support/Code/User"; else dir="$HOME/.config/Code/User"; fi
  mkdir -p "$dir"; sp="$dir/settings.json"
  python3 - "$sp" "$BASE" "$TOKEN" <<'PY'
import json,sys
sp,base,token=sys.argv[1],sys.argv[2],sys.argv[3]
try:
    d=json.load(open(sp)); assert isinstance(d,dict)
except Exception:
    d={}
d["vera.baseUrl"]=base
d["vera.clientMode"]=True
if token: d["vera.clientToken"]=token
json.dump(d,open(sp,"w"),indent=2)
PY
  echo "  Client mode : ON - Vera can now queue Claude Code tasks to this window"
fi

echo ""
echo "  Done. Reload VS Code, then queue a task from Vera (IDE -> Remotes & Queue)."
echo ""
'''.lstrip()


_CONNECT_HTML = r'''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vera · Connect VS Code</title>
<style>
 :root{--bg0:#0d0f12;--bg1:#14171c;--bg2:#1b1f26;--bg3:#232833;--fg:#e6e9ef;
   --dim:#9aa4b2;--line:#2a2f3a;--accent:#5b8cff;--ok:#37c871;--warn:#e0a83a;
   --mono:'SF Mono',ui-monospace,'Cascadia Code',Menlo,Consolas,monospace}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg0);color:var(--fg);font:14px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:32px 18px}
 .wrap{max-width:820px;margin:0 auto}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:26px 0 10px;letter-spacing:.2px}
 p{color:var(--dim);margin:0 0 12px} a{color:var(--accent)}
 .sub{color:var(--dim);font-size:13px;margin-bottom:8px}
 .card{background:var(--bg1);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0}
 .os{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin-bottom:6px}
 .code{position:relative;background:#0a0c0f;border:1px solid var(--line);border-radius:9px;
   padding:12px 44px 12px 12px;font-family:var(--mono);font-size:12.5px;color:#cdd6e6;
   white-space:pre-wrap;word-break:break-all;overflow:auto}
 .cp{position:absolute;top:7px;right:7px;background:var(--bg3);border:1px solid var(--line);
   color:var(--fg);border-radius:7px;padding:3px 8px;font-size:11px;cursor:pointer}
 .cp:hover{border-color:var(--accent)}
 .dls{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
 .dl{display:inline-flex;align-items:center;gap:6px;background:var(--bg2);border:1px solid var(--line);
   border-radius:8px;padding:7px 11px;color:var(--fg);text-decoration:none;font-size:12.5px}
 .dl:hover{border-color:var(--accent)}
 ol{color:var(--dim);padding-left:20px} ol li{margin:4px 0}
 code.i{background:var(--bg2);border:1px solid var(--line);border-radius:5px;padding:1px 5px;
   font-family:var(--mono);font-size:12px;color:#cdd6e6}
 .note{border-left:3px solid var(--warn);background:rgba(224,168,58,.08);padding:10px 14px;border-radius:0 8px 8px 0}
 .foot{color:var(--dim);font-size:12px;margin-top:28px;border-top:1px solid var(--line);padding-top:12px}
</style></head><body><div class="wrap">
 <h1>🔌 Connect VS Code &amp; Claude Code to Vera</h1>
 <p class="sub">Installs the Vera extension, turns on <b>client mode</b> so Vera can queue
   Claude Code tasks straight into your editor, and trusts Vera's TLS certificate
   (which also fixes the browser-IDE <i>“service worker SSL error”</i>).</p>

 <h2>One-line install</h2>
 <div class="card">
   <div class="os">Windows · PowerShell</div>
   <div class="code"><button class="cp" onclick="cp(this)">copy</button><span class="t">if($PSVersionTable.PSVersion.Major -ge 6){$s=irm __BASE__/vscode/connect/connect.ps1 -SkipCertificateCheck}else{if(-not ([Management.Automation.PSTypeName]'VeraTrustAll').Type){Add-Type -TypeDefinition 'using System.Net;using System.Security.Cryptography.X509Certificates;public class VeraTrustAll : ICertificatePolicy { public bool CheckValidationResult(ServicePoint sp,X509Certificate c,WebRequest r,int p){return true;} }'};[Net.ServicePointManager]::CertificatePolicy=New-Object VeraTrustAll;[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;$s=irm __BASE__/vscode/connect/connect.ps1};iex $s</span></div>
 </div>
 <div class="card">
   <div class="os">macOS · Linux</div>
   <div class="code"><button class="cp" onclick="cp(this)">copy</button><span class="t">curl -fsSLk __BASE__/vscode/connect/connect.sh | bash</span></div>
 </div>
 <p class="sub">Run it on the machine with your <b>desktop</b> VS Code. Requires the
   <code class="i">code</code> CLI on PATH (VS Code → <i>Shell Command: Install 'code' command in PATH</i>).</p>

 <h2>Or download the pieces</h2>
 <div class="card">
   <div class="dls">
     <a class="dl" href="__BASE__/vscode/connect/extension.vsix">⬇ extension .vsix</a>
     <a class="dl" href="__BASE__/vscode/connect/connect.ps1">⬇ connect.ps1</a>
     <a class="dl" href="__BASE__/vscode/connect/connect.sh">⬇ connect.sh</a>
     <a class="dl" href="__BASE__/vscode/connect/cert">⬇ TLS cert (vera-ca.crt)</a>
   </div>
   <p class="sub" style="margin-top:12px">Manual: <code class="i">code --install-extension vera-vscode.vsix</code>,
     then set <code class="i">vera.baseUrl</code> = <code class="i">__BASE__</code> and
     <code class="i">vera.clientMode</code> = <code class="i">true</code> in Settings.</p>
 </div>

 <h2>Fix the “service worker SSL error”</h2>
 <div class="card">
   <p>The browser IDE runs over Vera's <b>self-signed HTTPS</b>. Browsers refuse to
     register code-server's webview service worker under an untrusted cert — so
     Claude Code's chat webview (and other webviews) fail to load. The fix is to
     <b>trust Vera's certificate</b> on your machine:</p>
   <ol>
     <li>The one-liner above does this automatically. To do it by hand,
       <a href="__BASE__/vscode/connect/cert">download the cert</a>.</li>
     <li><b>Windows:</b> <code class="i">Import-Certificate -FilePath vera-ca.crt -CertStoreLocation Cert:\CurrentUser\Root</code></li>
     <li><b>macOS:</b> double-click the cert → Keychain → set it to <i>Always Trust</i>.</li>
     <li><b>Linux:</b> <code class="i">sudo cp vera-ca.crt /usr/local/share/ca-certificates/ &amp;&amp; sudo update-ca-certificates</code></li>
     <li><b>Restart the browser</b>, then reopen the IDE.</li>
   </ol>
   <div class="note">A real (CA-signed / <code class="i">mkcert</code>) certificate for
     this host removes the warning permanently and is the recommended long-term fix.</div>
 </div>

 <div class="foot">After connecting, this window shows up in Vera under
   <b>IDE → Remotes &amp; Queue</b>. Queue a task there, or push one with the
   <code class="i">ide.remote.client.dispatch</code> capability
   (<code class="i">action=claude_task</code>).</div>
</div>
<script>
 function cp(btn){
   var t=btn.parentElement.querySelector('.t').textContent;
   navigator.clipboard.writeText(t).then(function(){
     var o=btn.textContent; btn.textContent='copied'; setTimeout(function(){btn.textContent=o;},1400);
   });
 }
</script>
</body></html>'''


def _sanitize_token(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", token or "")[:128]


# ── The download / bootstrap routes (vscode_connect_*) are registered ABOVE the
#    /vscode/{iid} proxy so their literal paths win over the catch-all. ─────────

# ── Agent/UI-callable capabilities ───────────────────────────────────────────
@capability(
    "ide.vscode.connect.info",
    http_method="GET", http_path="/ide/vscode/connect/info", http_tags=["ide", "vscode"],
    memory="off", silent=True,
    description="Quick-connect details for wiring a user's VS Code + Claude Code "
                "to this Vera. Client mode makes that window a controllable "
                "instance (kind=vscode-client) so Vera can queue Claude Code tasks "
                "into it. Returns the landing URL, packaged-extension / script / "
                "cert download URLs, ready-to-paste one-liners, and whether a TLS "
                "cert is available (the fix for the code-server webview "
                "service-worker SSL error). "
                "Output: {ok, base, landing, urls, oneliners, tls_cert}.",
)
async def ide_vscode_connect_info(trace_id=None):
    base = _public_base()
    return {
        "ok": True, "base": base,
        "landing": f"{base}/vscode/connect",
        "urls": {
            "vsix": f"{base}/vscode/connect/extension.vsix",
            "ps1":  f"{base}/vscode/connect/connect.ps1",
            "sh":   f"{base}/vscode/connect/connect.sh",
            "cert": f"{base}/vscode/connect/cert",
        },
        "oneliners": {
            # Avoids a scriptblock ServerCertificateValidationCallback: .NET can
            # invoke that callback on a TLS I/O thread with no PowerShell
            # runspace attached, which throws and aborts the handshake — seen
            # as "The underlying connection was closed: An unexpected error
            # occurred on a send." PS7+ uses -SkipCertificateCheck instead;
            # PS5.1 uses a compiled ICertificatePolicy type (no runspace needed).
            "windows": (
                "if($PSVersionTable.PSVersion.Major -ge 6){$s=irm "
                f"{base}/vscode/connect/connect.ps1 -SkipCertificateCheck}}else{{"
                "if(-not ([Management.Automation.PSTypeName]'VeraTrustAll').Type){Add-Type "
                "-TypeDefinition 'using System.Net;using System.Security.Cryptography.X509Certificates;"
                "public class VeraTrustAll : ICertificatePolicy { public bool CheckValidationResult"
                "(ServicePoint sp,X509Certificate c,WebRequest r,int p){return true;} }'};"
                "[Net.ServicePointManager]::CertificatePolicy=New-Object VeraTrustAll;"
                "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
                f"$s=irm {base}/vscode/connect/connect.ps1}};iex $s"),
            "unix": f"curl -fsSLk {base}/vscode/connect/connect.sh | bash",
        },
        "tls_cert": bool(_tls_cert_path()),
        "hint": "Run the one-liner on the machine with your desktop VS Code. It "
                "installs the Vera extension, turns on client mode, and (over "
                "HTTPS) trusts Vera's cert — which also clears the 'service worker "
                "SSL error' in the browser IDE.",
    }


@capability(
    "ide.vscode.extension.build",
    http_method="POST", http_path="/ide/vscode/extension/build", http_tags=["ide", "vscode"],
    memory="on",
    description="Package the vera-vscode extension into a .vsix. mode='zip' "
                "(default) builds it in-process — a VSIX is just an OPC zip, so no "
                "vsce/Node/container is needed. mode='vsce' spins up a throwaway "
                "node:20-alpine container and runs the official @vscode/vsce "
                "packager (needs a reachable docker host). "
                "Input: mode ('zip'|'vsce'), docker_host_id (str='local', vsce "
                "only), save (bool=False → also write the .vsix next to the "
                "source). Output: {ok, mode, filename, bytes, download, path?}.",
)
async def ide_vscode_extension_build(mode: str = "zip", docker_host_id: str = "local",
                                     save: bool = False, trace_id=None):
    pkg = _ext_manifest()
    fname = f"{pkg.get('name', 'vera-vscode')}-{pkg.get('version', '0.0.1')}.vsix"
    base = _public_base()
    if mode == "vsce":
        return await _build_vsix_via_container(docker_host_id, fname, base)
    try:
        data = _build_vsix_bytes()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    out: Dict[str, Any] = {"ok": True, "mode": "zip", "filename": fname,
                           "bytes": len(data),
                           "download": f"{base}/vscode/connect/extension.vsix"}
    if save:
        try:
            dest = _EXT_DIR.parent / fname
            dest.write_bytes(data)
            out["path"] = str(dest)
        except Exception as e:
            out["save_error"] = str(e)
    return out


async def _build_vsix_via_container(docker_host_id: str, fname: str, base: str) -> dict:
    """Build the .vsix with the official vsce packager inside a throwaway
    node container — the 'container' path (source injected as base64 tar.gz,
    result read back as base64; the extension is tiny so both fit the exec cap)."""
    dk = _dk()
    if dk is None:
        return {"ok": False, "error": "docker module not loaded"}
    host = dk._get_host(docker_host_id or "local")
    if not host:
        return {"ok": False, "error": f"unknown docker host: {docker_host_id}"}
    import tarfile
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as tar:
        for p, rel in _iter_ext_files():
            tar.add(str(p), arcname="ext/" + rel)
    src_b64 = base64.b64encode(tbuf.getvalue()).decode("ascii")
    cname = f"vera-vsce-{uuid.uuid4().hex[:8]}"
    script = (
        "set -e; mkdir -p /w && cd /w && "
        f"printf %s '{src_b64}' | base64 -d | tar xz && cd ext && "
        "npx --yes @vscode/vsce package --allow-missing-repository -o /w/out.vsix 1>&2 && "
        "base64 -w0 /w/out.vsix"
    )
    run = await dk.cap_docker_run(host_id=host["id"], image="node:20-alpine",
                                  name=cname, command="sleep 900", pull=True)
    if not run.get("ok"):
        return {"ok": False, "error": run.get("error", "docker run failed")}
    try:
        r = await _docker_exec(host, cname, script, timeout=600)
        b64out = "".join((r.get("stdout") or "").split())
        if not r.get("ok") or not b64out:
            return {"ok": False,
                    "error": "vsce packaging failed: " + (r.get("stderr", "") or "")[-400:]}
        try:
            data = base64.b64decode(b64out)
        except Exception as e:
            return {"ok": False, "error": f"could not decode vsix: {e}"}
        dest = _EXT_DIR.parent / fname
        try:
            dest.write_bytes(data)
        except Exception as e:
            return {"ok": True, "mode": "vsce", "filename": fname, "bytes": len(data),
                    "save_error": str(e),
                    "download": f"{base}/vscode/connect/extension.vsix"}
        return {"ok": True, "mode": "vsce", "filename": fname, "bytes": len(data),
                "path": str(dest), "download": f"{base}/vscode/connect/extension.vsix"}
    finally:
        try:
            await dk._run_local(await dk._docker_argv(host, ["rm", "-f", cname]), timeout=60)
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL  —  merged IDE wrapper (VS Code first; classic IDE + remotes kept)
# ═════════════════════════════════════════════════════════════════════════════
_PANEL_PATH = _HERE / "vscode_panel.html"


@capability(
    "ide.vscode.panel.html",
    http_method="GET", http_path="/ide/vscode/panel", http_tags=["ide", "vscode", "ui"],
    memory="off", silent=True,
    description="Serve the merged IDE panel (central VS Code + classic workbench "
                "+ remotes) as text/html.",
)
async def ide_vscode_panel_html(trace_id=None):
    try:
        html = _PANEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef4444;"
                "font-family:monospace;padding:40px'><h2>vscode_panel.html not found</h2>"
                f"<p>Expected: {_PANEL_PATH}</p></body></html>")
    return HTMLResponse(html)


log.info("vscode_capabilities loaded — central=%s proxy=/vscode/{id}/",
         _central_env_url() or "(unset)")
