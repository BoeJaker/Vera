"""
ide_remote_capabilities.py  —  Vera Remote IDE / Claude Code driver
====================================================================
Extends the Vera IDE from local-only work to *driving remote machines*:

  • Register remote VSCode instances — Vera-managed **code-server** (installed
    over SSH, embeddable in the panel iframe) OR a pre-existing **tunnel URL**
    (VS Code Remote Tunnel / any code-server), selectable per host.
  • Autonomously work on their projects with **Claude Code** (the `claude` CLI,
    run headless over SSH) OR Vera's own **Ollama agents** (thinker/writer/
    analyser editing the remote FS over SSH) — engine chosen per task.
  • Let remote Claude Code call back into Vera's capabilities via an **MCP
    bridge** (see vera_mcp_bridge.py + ide.remote.bridge.*).
  • An **autonomous work queue** + scheduled dispatcher picks up tasks and runs
    them on free instances (dream/project goals can enqueue work).

Everything is recorded through the same `_record()` engine the local IDE uses
(memory graph + data fabric), so remote work flows into dreaming and direct
user queries exactly like local IDE activity. Fabric datasets:
  ide.remote_instances · ide.remote_runs · ide.remote_queue

This module is deliberately thin — it reuses:
  • exec.ssh.run + the **exec** SSH credential store (dual-ssh-host-stores)
  • the provision.* "install over SSH ending in VERA_PROVISION_DONE" pattern
  • ide_capabilities._record / _ide_get_session_id / PROJECT_ROOT
  • the AGENT_RUNNER (ide-thinker/writer/analyser) for the vera-agent engine
  • schedule() for the dispatcher, register_ui() for the panel
  • security.secrets (Fernet) to seal the code-server token + per-instance key

Capabilities (group `ide.remote.*`)
────────────────────────────────────
  instances / register / delete          — instance registry CRUD
  provision / status / start / stop / open — code-server lifecycle over SSH
  run                                    — run a coding task (engine=claude|vera-agent)
  queue.add / queue.list / queue.cancel / queue.clear
  autopilot                              — enable/disable the dispatcher + concurrency
  summary                                — summarise remote work (memory graph)
  bridge.install / bridge.status         — wire Vera into remote Claude Code (MCP)

HTTP-only (raw StreamingResponse / HTML):
  POST /ide-api/remote/run               — SSE live stream of a Claude Code run
  GET  /ide/remote/panel                 — the Remote IDE panel HTML
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
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, StreamingResponse

from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY,
    capability, emit_event, enum_schema, now_iso,
    record_stream_activity, register_ui, schedule,
)
from Vera.vera.ide.ide_capabilities import (  # type: ignore
    PROJECT_ROOT, _record, _ide_get_session_id,
)

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                    # pragma: no cover
    vsecrets = None  # type: ignore

log = logging.getLogger("vera.ide.remote")
_HERE = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE  (small JSON files next to the module — mirrors .vera_workspaces)
# ─────────────────────────────────────────────────────────────────────────────
_INSTANCES_FILE = _HERE / ".vera_remote_instances.json"
_QUEUE_FILE     = _HERE / ".vera_remote_queue.json"
_SETTINGS_FILE  = _HERE / ".vera_remote_settings.json"

_DEFAULT_SETTINGS = {"autopilot": False, "max_concurrency": 1}


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("remote: could not read %s: %s", path.name, e)
    return default


def _save_json(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("remote: could not write %s: %s", path.name, e)


def _load_instances() -> List[dict]:
    return _load_json(_INSTANCES_FILE, [])


def _save_instances(rows: List[dict]) -> None:
    _save_json(_INSTANCES_FILE, rows)


def _get_instance(instance_id: str) -> Optional[dict]:
    for r in _load_instances():
        if r.get("id") == instance_id:
            return r
    return None


def _load_queue() -> List[dict]:
    return _load_json(_QUEUE_FILE, [])


def _save_queue(rows: List[dict]) -> None:
    _save_json(_QUEUE_FILE, rows)


def _load_settings() -> dict:
    s = dict(_DEFAULT_SETTINGS)
    s.update(_load_json(_SETTINGS_FILE, {}) or {})
    return s


def _save_settings(s: dict) -> None:
    _save_json(_SETTINGS_FILE, s)


# ─────────────────────────────────────────────────────────────────────────────
# SECRET HELPERS  (seal on write, redact on read)
# ─────────────────────────────────────────────────────────────────────────────
def _seal(plaintext: str) -> str:
    if not plaintext:
        return ""
    if vsecrets:
        try:
            return vsecrets.seal(plaintext)
        except Exception as e:
            log.error("remote: seal failed (%s) — value NOT stored", e)
            return ""
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


def _public_instance(rec: dict) -> dict:
    """UI-safe copy — never leak sealed secrets, expose has_* flags instead."""
    out = {k: v for k, v in rec.items()
           if k not in ("token_sealed", "api_key_sealed", "oauth_sealed")}
    out["has_token"]   = bool(rec.get("token_sealed"))
    out["has_api_key"] = bool(rec.get("api_key_sealed"))
    out["has_oauth"]   = bool(rec.get("oauth_sealed"))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LAZY BACKEND ACCESS
# ─────────────────────────────────────────────────────────────────────────────
def _cap(name: str):
    c = CAPABILITY_REGISTRY.get(name)
    return c.get("func") if c else None


def _exec_mod():
    return sys.modules.get("exec_capabilities")


async def _ssh(host_id: str, command: str, timeout: int = 120) -> dict:
    """Run a command on a host from the **exec** SSH store (blocking, captured)."""
    fn = _cap("exec.ssh.run")
    if not fn:
        return {"ok": False, "error": "exec.ssh.run unavailable",
                "rc": -1, "stdout": "", "stderr": ""}
    try:
        return await fn(command=command, host_id=host_id, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "rc": -1, "stdout": "", "stderr": ""}


async def _anthropic_key(inst: dict) -> str:
    """Resolve an Anthropic key: per-instance sealed → providers store → env."""
    k = _open(inst.get("api_key_sealed", ""))
    if k:
        return k
    prov = sys.modules.get("providers_capabilities")
    if prov:
        try:
            rec = await prov._get("anthropic")          # type: ignore[attr-defined]
            if rec:
                k = prov._open_key(rec)                  # type: ignore[attr-defined]
                if k:
                    return k
        except Exception as e:
            log.debug("remote: providers key lookup: %s", e)
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


async def _claude_auth(inst: dict) -> "tuple[str, str]":
    """(api_key, oauth_token) per the instance's auth mode.

    auth='subscription' → never export an API key: exported keys OVERRIDE the
    host's `claude login` (claude.ai plan) credentials, so a signed-in host
    must run bare. A sealed per-instance OAuth token (`claude setup-token`)
    is exported as CLAUDE_CODE_OAUTH_TOKEN instead, when present."""
    if (inst.get("auth") or "api-key") == "subscription":
        return "", _open(inst.get("oauth_sealed", ""))
    return await _anthropic_key(inst), ""


# ─────────────────────────────────────────────────────────────────────────────
# INSTALL / DETECT COMMAND TEMPLATES  (mirrors software_capabilities pattern —
# every script ends in VERA_PROVISION_DONE so we can confirm it reached the end)
# ─────────────────────────────────────────────────────────────────────────────
_DONE = "VERA_PROVISION_DONE"

# code-server: official installer, then a per-user systemd service bound to
# 0.0.0.0:{port} with password auth. Falls back to nohup if systemd --user is
# unavailable. {token}/{port} are shlex-safe (hex/int).
_CODE_SERVER_INSTALL = (
    "curl -fsSL https://code-server.dev/install.sh | sh && "
    "mkdir -p ~/.config/code-server && "
    "printf 'bind-addr: 0.0.0.0:{port}\\nauth: password\\npassword: {token}\\ncert: false\\n' "
    "> ~/.config/code-server/config.yaml && "
    "( systemctl --user enable --now code-server 2>/dev/null "
    "|| ( (systemctl --user daemon-reload 2>/dev/null; "
    "systemctl --user restart code-server 2>/dev/null) "
    "|| nohup code-server >/tmp/code-server.log 2>&1 & ) ) ; "
    "sleep 1 ; echo " + _DONE
)

# Claude Code CLI (needs Node >=18 / npm on the host).
_CLAUDE_INSTALL = (
    "(command -v npm >/dev/null 2>&1 || (echo 'ERR: npm/Node>=18 required'; exit 3)) && "
    "npm install -g @anthropic-ai/claude-code && "
    "claude --version && echo " + _DONE
)

# What's already there? (OS, node, claude, code-server, GPU) — one round trip.
_DETECT = (
    "echo \"os=$(. /etc/os-release 2>/dev/null; echo ${ID:-unknown} ${VERSION_ID:-})\"; "
    "echo \"node=$(node --version 2>/dev/null || echo none)\"; "
    "echo \"npm=$(npm --version 2>/dev/null || echo none)\"; "
    "echo \"claude=$(claude --version 2>/dev/null || echo none)\"; "
    "echo \"code_server=$(code-server --version 2>/dev/null | head -1 || echo none)\"; "
    "echo \"bridge=$(test -f ~/.vera/vera_mcp_bridge.py && echo yes || echo no)\"; "
    "echo \"claude_login=$(test -f ~/.claude/.credentials.json && echo yes || echo no)\""
)

_STATUS_PROBE = (
    "echo \"code_server=$( (pgrep -x code-server >/dev/null 2>&1 "
    "|| pgrep -f 'code-server' >/dev/null 2>&1) && echo up || echo down)\"; "
    "echo \"claude=$(claude --version 2>/dev/null || echo none)\"; "
    "echo \"bridge=$(test -f ~/.vera/vera_mcp_bridge.py && echo yes || echo no)\"; "
    "echo \"claude_login=$(test -f ~/.claude/.credentials.json && echo yes || echo no)\""
)


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  INSTANCE REGISTRY
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "ide.remote.instances",
    http_method="GET", http_path="/ide/remote/instances", http_tags=["ide", "remote"],
    memory="off", silent=True,
    description="List registered remote IDE instances (code-server / tunnel / ssh). "
                "Secrets are redacted. Output: {instances: [{id, label, kind, host_id, "
                "url, port, workdir, status, has_token, has_api_key}], count}.",
)
async def ide_remote_instances(trace_id=None):
    rows = [_public_instance(r) for r in _load_instances()]
    return {"instances": rows, "count": len(rows)}


@capability(
    "ide.remote.register",
    http_method="POST", http_path="/ide/remote/register", http_tags=["ide", "remote"],
    memory="on",
    description="Register or update a remote IDE instance. "
                "Input: id (str — update if given), label (str), "
                "kind (code-server|tunnel|ssh|vscode-client), host_id (str — from the "
                "exec SSH store, required for code-server/ssh), url (str — required for "
                "tunnel), port (int=8080), workdir (str — remote project root), "
                "token (str — code-server password / client secret, sealed), "
                "api_key (str — per-instance Anthropic key, sealed), "
                "auth (api-key|subscription — subscription = the host's own "
                "`claude login`, no key exported), oauth_token (str — from "
                "`claude setup-token`, sealed, used with auth=subscription). "
                "kind=vscode-client is a user's live VS Code window running the Vera "
                "extension in client mode; it self-registers and receives work via "
                "the client dispatch channel. Output: {ok, instance}.",
    schema=enum_schema(kind=["code-server", "tunnel", "ssh", "vscode-client"],
                       auth=["api-key", "subscription"]),
)
async def ide_remote_register(
    id:          str = "",
    label:       str = "",
    kind:        str = "code-server",
    host_id:     str = "",
    url:         str = "",
    port:        int = 8080,
    workdir:     str = "",
    token:       str = "",
    api_key:     str = "",
    auth:        str = "",
    oauth_token: str = "",
    trace_id=None,
):
    kind = (kind or "code-server").lower()
    if kind not in ("code-server", "tunnel", "ssh", "vscode-client"):
        return {"ok": False, "error": f"unknown kind: {kind}"}
    if kind in ("code-server", "ssh") and not host_id:
        return {"ok": False, "error": f"host_id required for kind={kind}"}
    if kind == "tunnel" and not url:
        return {"ok": False, "error": "url required for kind=tunnel"}
    if auth and auth not in ("api-key", "subscription"):
        return {"ok": False, "error": f"unknown auth: {auth}"}

    rows = _load_instances()
    rec = next((r for r in rows if r.get("id") == id), None) if id else None
    created = rec is None
    if rec is None:
        rec = {"id": id or str(uuid.uuid4()), "created_at": now_iso()}
        rows.append(rec)

    rec.update({
        "label":   label or rec.get("label") or (url or host_id or "remote"),
        "kind":    kind,
        "host_id": host_id or rec.get("host_id", ""),
        "url":     url or rec.get("url", ""),
        "port":    int(port or rec.get("port", 8080) or 8080),
        "workdir": workdir or rec.get("workdir", ""),
        "status":  rec.get("status", "unknown"),
        "updated_at": now_iso(),
    })
    if auth:
        rec["auth"] = auth
    if token:
        rec["token_sealed"] = _seal(token)
    if api_key:
        rec["api_key_sealed"] = _seal(api_key)
    if oauth_token:
        rec["oauth_sealed"] = _seal(oauth_token)

    _save_instances(rows)
    await emit_event({"type": "ide.remote.instance", "action": "saved",
                      "id": rec["id"], "kind": kind, "label": rec["label"]})
    asyncio.ensure_future(_record(
        session_id=_ide_get_session_id(), category="ide.remote.instance",
        text=f"Remote IDE instance {'created' if created else 'updated'}: {rec['label']} ({kind})",
        full_text=f"Instance: {rec['label']}\nKind: {kind}\nHost: {host_id}\nWorkdir: {workdir}\nURL: {url}",
        tags=["ide", "remote", "instance", kind],
        importance=0.5, source_type="tool", record_type="event",
        capability_name="ide.remote.register", broadcast_type="ide.remote.instance",
        fabric_dataset="ide.remote_instances",
        metadata={"id": rec["id"], "kind": kind, "host_id": host_id},
        fabric_data={"label": rec["label"], "kind": kind, "host_id": host_id,
                     "workdir": workdir, "url": url},
        dedup_key="remote_inst:" + rec["id"] + ":" + rec["updated_at"],
    ))
    return {"ok": True, "instance": _public_instance(rec), "created": created}


@capability(
    "ide.remote.delete",
    http_method="POST", http_path="/ide/remote/delete", http_tags=["ide", "remote"],
    memory="on",
    description="Delete a remote IDE instance by id. Input: id (str!). Output: {ok, deleted}.",
)
async def ide_remote_delete(id: str = "", trace_id=None):
    if not id:
        return {"ok": False, "error": "id required"}
    rows = _load_instances()
    keep = [r for r in rows if r.get("id") != id]
    if len(keep) == len(rows):
        return {"ok": False, "error": f"instance not found: {id}"}
    _save_instances(keep)
    await emit_event({"type": "ide.remote.instance", "action": "deleted", "id": id})
    return {"ok": True, "deleted": id}


# ═════════════════════════════════════════════════════════════════════════════
#  PROVISION / STATUS / LIFECYCLE  (code-server + claude over SSH)
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "ide.remote.detect",
    http_method="POST", http_path="/ide/remote/detect", http_tags=["ide", "remote"],
    memory="off",
    description="SSH-probe a host for what's installed (OS, node, npm, claude, "
                "code-server, MCP bridge). Input: host_id (str!) OR instance_id (str). "
                "Output: {ok, detected: {...}, raw}.",
)
async def ide_remote_detect(host_id: str = "", instance_id: str = "", trace_id=None):
    if instance_id and not host_id:
        inst = _get_instance(instance_id) or {}
        host_id = inst.get("host_id", "")
    if not host_id:
        return {"ok": False, "error": "host_id (or a host-backed instance_id) required"}
    res = await _ssh(host_id, _DETECT, timeout=60)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or res.get("stderr") or "ssh failed",
                "raw": res.get("stdout", "")}
    return {"ok": True, "detected": _parse_kv(res.get("stdout", "")),
            "raw": res.get("stdout", "")}


@capability(
    "ide.remote.provision",
    http_method="POST", http_path="/ide/remote/provision", http_tags=["ide", "remote"],
    memory="on",
    description="Install code-server (and optionally the Claude Code CLI) on a host "
                "over SSH, start code-server bound to 0.0.0.0:{port} with a generated "
                "password, and register the instance. "
                "Input: host_id (str!), port (int=8080), workdir (str), label (str), "
                "install_claude (bool=True), instance_id (str — update existing). "
                "Output: {ok, instance, url, steps}.",
)
async def ide_remote_provision(
    host_id:        str  = "",
    port:           int  = 8080,
    workdir:        str  = "",
    label:          str  = "",
    install_claude: bool = True,
    instance_id:    str  = "",
    trace_id=None,
):
    if not host_id:
        return {"ok": False, "error": "host_id required"}
    port = int(port or 8080)
    token = uuid.uuid4().hex        # code-server password
    steps: List[dict] = []

    await emit_event({"type": "ide.remote.provision", "stage": "code-server",
                      "host_id": host_id, "message": "installing code-server"})
    cmd = _CODE_SERVER_INSTALL.format(port=port, token=token)
    res = await _ssh(host_id, cmd, timeout=600)
    ok_cs = res.get("ok") and _DONE in (res.get("stdout", "") or "")
    steps.append({"step": "code-server", "ok": bool(ok_cs),
                  "tail": (res.get("stdout", "") or res.get("stderr", ""))[-400:]})
    if not ok_cs:
        return {"ok": False, "error": "code-server install did not complete",
                "steps": steps}

    ok_claude = None
    if install_claude:
        await emit_event({"type": "ide.remote.provision", "stage": "claude",
                          "host_id": host_id, "message": "installing Claude Code CLI"})
        cres = await _ssh(host_id, _CLAUDE_INSTALL, timeout=600)
        ok_claude = bool(cres.get("ok") and _DONE in (cres.get("stdout", "") or ""))
        steps.append({"step": "claude", "ok": ok_claude,
                      "tail": (cres.get("stdout", "") or cres.get("stderr", ""))[-400:]})

    # Derive the reachable URL from the exec host record's hostname.
    host_addr = ""
    ex = _exec_mod()
    if ex and hasattr(ex, "_resolve_host_record"):
        try:
            rec = await ex._resolve_host_record(host_id)
            host_addr = (rec or {}).get("host", "")
        except Exception:
            pass
    url = f"http://{host_addr}:{port}" if host_addr else ""

    reg = await ide_remote_register(
        id=instance_id, label=label, kind="code-server", host_id=host_id,
        url=url, port=port, workdir=workdir, token=token,
    )
    inst = reg.get("instance", {})
    if inst.get("id"):
        rows = _load_instances()
        for r in rows:
            if r.get("id") == inst["id"]:
                r["status"] = "running"
                r["claude_installed"] = bool(ok_claude)
                r["updated_at"] = now_iso()
        _save_instances(rows)

    await emit_event({"type": "ide.remote.provision", "stage": "done",
                      "host_id": host_id, "url": url, "message": "provisioned"})
    return {"ok": True, "instance": _public_instance(_get_instance(inst.get("id", "")) or inst),
            "url": url, "password": token, "claude_installed": ok_claude, "steps": steps}


@capability(
    "ide.remote.status",
    http_method="POST", http_path="/ide/remote/status", http_tags=["ide", "remote"],
    memory="off",
    description="Check a remote IDE instance: code-server up?, claude present?, "
                "claude signed in (subscription login)?, bridge installed?, URL "
                "reachable? Input: instance_id (str!). Output: {ok, instance_id, "
                "code_server, claude, claude_login, bridge, url_reachable}.",
)
async def ide_remote_status(instance_id: str = "", trace_id=None):
    inst = _get_instance(instance_id)
    if not inst:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    out: Dict[str, Any] = {"ok": True, "instance_id": instance_id, "kind": inst.get("kind")}

    # VS Code client windows are liveness-checked by their poll loop, not SSH.
    if inst.get("kind") == "vscode-client":
        out["client_connected"] = _client_alive(instance_id)
        out["last_seen"] = inst.get("last_seen", "")
        rows = _load_instances()
        for r in rows:
            if r.get("id") == instance_id:
                r["status"] = "running" if out["client_connected"] else "down"
                r["updated_at"] = now_iso()
        _save_instances(rows)
        return out

    # SSH probe (code-server / claude / bridge) when host-backed.
    if inst.get("host_id"):
        res = await _ssh(inst["host_id"], _STATUS_PROBE, timeout=30)
        kv = _parse_kv(res.get("stdout", "")) if res.get("ok") else {}
        out["code_server"]  = kv.get("code_server", "unknown")
        out["claude"]       = kv.get("claude", "none")
        out["claude_login"] = kv.get("claude_login", "no") == "yes"
        out["bridge"]       = kv.get("bridge", "no") == "yes"
        if not res.get("ok"):
            out["ssh_error"] = res.get("error") or res.get("stderr", "")

    # HTTP ping the URL (works for tunnel + code-server).
    url = inst.get("url", "")
    out["url"] = url
    if url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=6, verify=False) as c:
                r = await c.get(url)
                out["url_reachable"] = r.status_code < 500
                out["url_status"] = r.status_code
        except Exception as e:
            out["url_reachable"] = False
            out["url_error"] = str(e)[:160]

    # Persist a coarse status label.
    rows = _load_instances()
    for r in rows:
        if r.get("id") == instance_id:
            r["status"] = ("running" if out.get("code_server") == "up"
                           or out.get("url_reachable") else "down")
            r["updated_at"] = now_iso()
    _save_instances(rows)
    return out


async def _codeserver_ctl(instance_id: str, action: str) -> dict:
    inst = _get_instance(instance_id)
    if not inst:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    if not inst.get("host_id"):
        return {"ok": False, "error": "instance has no host_id (tunnel instances are self-managed)"}
    if action == "start":
        fallback = "(nohup code-server >/tmp/code-server.log 2>&1 &)"
        cmd = f"systemctl --user restart code-server 2>/dev/null || {fallback} ; echo {_DONE}"
    else:
        cmd = f"systemctl --user stop code-server 2>/dev/null || pkill -f code-server ; echo {_DONE}"
    res = await _ssh(inst["host_id"], cmd, timeout=60)
    return {"ok": bool(res.get("ok")), "action": action,
            "tail": (res.get("stdout", "") or res.get("stderr", ""))[-300:]}


@capability(
    "ide.remote.start",
    http_method="POST", http_path="/ide/remote/start", http_tags=["ide", "remote"],
    memory="on",
    description="Start/restart code-server on a host-backed instance. Input: instance_id (str!).",
)
async def ide_remote_start(instance_id: str = "", trace_id=None):
    return await _codeserver_ctl(instance_id, "start")


@capability(
    "ide.remote.stop",
    http_method="POST", http_path="/ide/remote/stop", http_tags=["ide", "remote"],
    memory="on",
    description="Stop code-server on a host-backed instance. Input: instance_id (str!).",
)
async def ide_remote_stop(instance_id: str = "", trace_id=None):
    return await _codeserver_ctl(instance_id, "stop")


@capability(
    "ide.remote.open",
    http_method="GET", http_path="/ide/remote/open", http_tags=["ide", "remote"],
    memory="off", silent=True,
    description="Return the embeddable URL for a remote IDE instance (for the panel "
                "iframe). proxy_url ('/vscode/<id>/') is the same-origin reverse proxy "
                "— prefer it for iframes: the code-server session cookie is first-party "
                "through it, so login works embedded (raw url logins fail in iframes). "
                "Input: instance_id (str!). Output: {ok, url, proxy_url, kind, needs_password}.",
)
async def ide_remote_open(instance_id: str = "", trace_id=None):
    inst = _get_instance(instance_id)
    if not inst:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    return {"ok": True, "url": inst.get("url", ""),
            "proxy_url": f"/vscode/{instance_id}/" if inst.get("url") else "",
            "kind": inst.get("kind"),
            "needs_password": bool(inst.get("token_sealed"))}


# ═════════════════════════════════════════════════════════════════════════════
#  REMOTE FILESYSTEM HELPERS  (for the vera-agent engine — thin exec.ssh wrappers)
# ═════════════════════════════════════════════════════════════════════════════

async def _remote_list(host_id: str, workdir: str, limit: int = 300) -> str:
    cmd = (f"cd {shlex.quote(workdir)} 2>/dev/null && "
           f"(git ls-files 2>/dev/null | head -n {limit} "
           f"|| find . -type f -not -path '*/.git/*' | head -n {limit})")
    res = await _ssh(host_id, cmd, timeout=45)
    return res.get("stdout", "") or res.get("error", "")


async def _remote_read(host_id: str, workdir: str, path: str, max_bytes: int = 60000) -> str:
    full = f"{workdir.rstrip('/')}/{path}" if not path.startswith("/") else path
    cmd = f"head -c {max_bytes} {shlex.quote(full)}"
    res = await _ssh(host_id, cmd, timeout=45)
    return res.get("stdout", "") if res.get("ok") else f"[error reading {path}: {res.get('stderr') or res.get('error')}]"


async def _remote_grep(host_id: str, workdir: str, pattern: str, limit: int = 80) -> str:
    cmd = (f"cd {shlex.quote(workdir)} 2>/dev/null && "
           f"grep -rnI --exclude-dir=.git -e {shlex.quote(pattern)} . 2>/dev/null | head -n {limit}")
    res = await _ssh(host_id, cmd, timeout=45)
    return res.get("stdout", "") or "[no matches]"


async def _remote_write(host_id: str, workdir: str, path: str, content: str) -> dict:
    """Write a file on the remote host via a base64 here-doc (binary-safe)."""
    import base64
    full = f"{workdir.rstrip('/')}/{path}" if not path.startswith("/") else path
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = (f"mkdir -p \"$(dirname {shlex.quote(full)})\" && "
           f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(full)} && "
           f"echo {_DONE}")
    res = await _ssh(host_id, cmd, timeout=60)
    ok = bool(res.get("ok") and _DONE in (res.get("stdout", "") or ""))
    return {"ok": ok, "path": full,
            "error": None if ok else (res.get("stderr") or res.get("error"))}


async def _remote_run_cmd(host_id: str, workdir: str, command: str, timeout: int = 120) -> str:
    cmd = f"cd {shlex.quote(workdir)} 2>/dev/null; {command}"
    res = await _ssh(host_id, cmd, timeout=timeout)
    out = (res.get("stdout", "") + ("\n" + res.get("stderr", "") if res.get("stderr") else ""))
    return out[:8000] if out else f"[rc={res.get('rc')}]"


# ═════════════════════════════════════════════════════════════════════════════
#  RUN ENGINES  (claude CLI  |  vera-agent)
# ═════════════════════════════════════════════════════════════════════════════

# Configurable per CLI version — override via env if the flags drift.
_CLAUDE_PERMISSION_MODE = os.getenv("VERA_CLAUDE_PERMISSION_MODE", "acceptEdits")


def _build_claude_cmd(workdir: str, task: str, api_key: str,
                      stream: bool, permission_mode: str = "",
                      oauth_token: str = "", model: str = "") -> str:
    """Compose the remote shell command that runs Claude Code headless.

    Credentials (if any) are written to a 0600 env file and sourced, so they
    are not visible in the process list. With neither api_key nor oauth_token
    the CLI runs bare and uses the host's own `claude login` (subscription)
    credentials. Flags are configurable because the CLI's surface changes
    between versions.
    """
    fmt = "stream-json --verbose" if stream else "json"
    pm = permission_mode or _CLAUDE_PERMISSION_MODE
    parts = []
    cred_var = ("ANTHROPIC_API_KEY" if api_key
                else "CLAUDE_CODE_OAUTH_TOKEN" if oauth_token else "")
    cred_val = api_key or oauth_token
    if cred_var:
        parts.append("umask 077; mkdir -p ~/.vera; "
                      f"printf 'export {cred_var}=%s\\n' {shlex.quote(cred_val)} > ~/.vera/claude.env; "
                      ". ~/.vera/claude.env;")
    parts.append(f"cd {shlex.quote(workdir)} &&")
    parts.append("claude -p " + shlex.quote(task)
                 + f" --output-format {fmt}"
                 + (f" --permission-mode {shlex.quote(pm)}" if pm else "")
                 + (f" --model {shlex.quote(model)}" if model else ""))
    return " ".join(parts)


async def _run_claude(inst: dict, task: str, permission_mode: str = "",
                      timeout: int = 1800, workdir: str = "", model: str = "") -> dict:
    """Blocking Claude Code run (used by the cap + dispatcher). Streaming lives
    in the /ide-api/remote/run SSE endpoint. `workdir` overrides the instance's
    project root for THIS run (e.g. Loop Lab pins edits to a branch worktree)."""
    host_id = inst.get("host_id", "")
    workdir = workdir or inst.get("workdir", "") or "."
    if not host_id:
        return {"ok": False, "error": "instance has no host_id"}
    key, oauth = await _claude_auth(inst)
    cmd = _build_claude_cmd(workdir, task, key, stream=False,
                            permission_mode=permission_mode, oauth_token=oauth,
                            model=model)
    res = await _ssh(host_id, cmd, timeout=timeout)
    raw = res.get("stdout", "") or ""
    summary, result_obj = "", None
    # claude --output-format json prints a JSON object; be tolerant of extra lines.
    for line in reversed(raw.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result_obj = json.loads(line)
                summary = (result_obj.get("result")
                           or result_obj.get("text") or "")
                break
            except Exception:
                continue
    if not summary:
        summary = raw[-1200:] or res.get("stderr", "")[-600:]
    return {"ok": bool(res.get("ok")), "engine": "claude",
            "summary": summary[:4000], "rc": res.get("rc"),
            "result": result_obj, "raw_tail": raw[-1500:]}


# ── vera-agent engine ─────────────────────────────────────────────────────────
_AGENT_MAP = {"thinker": "ide-thinker", "writer": "ide-writer", "analyser": "ide-analyser"}

_VERA_AGENT_SYSTEM = (
    "You are Vera's remote coding agent working on a project on a REMOTE host. "
    "You act by emitting EXACTLY ONE tool call per turn as a single JSON object on "
    "its own line, nothing else. Tools:\n"
    '  {"tool":"list"}                         — list project files\n'
    '  {"tool":"read","path":"rel/path"}       — read a file\n'
    '  {"tool":"grep","pattern":"text"}        — search the project\n'
    '  {"tool":"write","path":"rel/path","content":"...full file..."} — write a file\n'
    '  {"tool":"run","command":"shell cmd"}    — run a shell command in the project dir\n'
    '  {"tool":"done","summary":"what you changed"} — finish\n'
    "Rules: paths are relative to the project root. When writing, output the COMPLETE "
    "file. Keep going until the task is complete, then emit done. Do not wrap the JSON "
    "in markdown fences."
)


async def _agent_generate(agent_name: str, prompt: str, system: str, history: list) -> str:
    """Run an IDE Ollama agent, falling back to ide.agent.chat if the runner is absent."""
    try:
        from Vera.Agents.agents import AGENT_REGISTRY, AGENT_RUNNER  # type: ignore
        agent = await AGENT_REGISTRY.get_by_name(agent_name)
        if agent and AGENT_RUNNER:
            import copy
            ag = copy.copy(agent)
            if system:
                ag.system_prompt = system
            result = await AGENT_RUNNER.run(ag, prompt, history or [], "")
            return result.get("text", "") if isinstance(result, dict) else str(result)
    except Exception as e:
        log.debug("remote vera-agent runner fallback: %s", e)
    fn = _cap("ide.agent.chat")
    if fn:
        r = await fn(agent=agent_name.replace("ide-", ""), prompt=prompt, system=system)
        return r.get("text", "") if isinstance(r, dict) else str(r)
    return ""


def _extract_json_action(text: str) -> Optional[dict]:
    """Pull the first JSON object out of an agent turn (tolerant of fences/prose)."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s.split("\n", 1)[-1] if "\n" in s else s
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[start:i + 1])
                        except Exception:
                            break
        start = s.find("{", start + 1)
    return None


async def _run_vera_agent(inst: dict, task: str, agent: str = "writer",
                          max_turns: int = 8, session_id: str = "",
                          workdir: str = "") -> dict:
    """Bounded tool-calling loop: the IDE agent drives edits on the remote FS.
    `workdir` overrides the instance's project root for THIS run."""
    host_id = inst.get("host_id", "")
    workdir = workdir or inst.get("workdir", "") or "."
    if not host_id:
        return {"ok": False, "error": "instance has no host_id"}
    agent_name = _AGENT_MAP.get((agent or "writer").lower(), "ide-writer")
    sid = session_id or _ide_get_session_id()

    listing = await _remote_list(host_id, workdir)
    history: list = []
    transcript = (f"TASK: {task}\n\nProject root: {workdir} on remote host "
                  f"'{inst.get('label')}'.\nFiles:\n{listing[:4000]}\n\n"
                  "Begin. Emit one JSON tool call.")
    steps, changed = [], []
    for turn in range(max_turns):
        reply = await _agent_generate(agent_name, transcript, _VERA_AGENT_SYSTEM, history)
        history.append({"role": "assistant", "content": reply})
        action = _extract_json_action(reply)
        if not action:
            steps.append({"turn": turn, "tool": "none", "note": "no JSON action — stopping"})
            return {"ok": True, "engine": "vera-agent", "summary": reply[:2000],
                    "steps": steps, "changed": changed, "turns": turn + 1}
        tool = str(action.get("tool", "")).lower()
        await emit_event({"type": "ide.remote.agent_step", "instance_id": inst.get("id"),
                          "turn": turn, "tool": tool, "session_id": sid})
        obs = ""
        if tool == "done":
            summary = action.get("summary", "")
            asyncio.ensure_future(_record(
                session_id=sid, category="ide.remote.agent_step",
                text=f"[vera-agent] done: {summary[:120]}",
                full_text=summary, tags=["ide", "remote", "vera-agent", "done"],
                importance=0.6, source_type="ai", record_type="observation",
                capability_name="ide.remote.run", broadcast_type="ide.remote.agent_step",
                fabric_dataset="ide.remote_runs",
                metadata={"instance_id": inst.get("id"), "turns": turn + 1},
                fabric_data={"summary": summary, "changed": changed},
            ))
            return {"ok": True, "engine": "vera-agent", "summary": summary[:2000],
                    "steps": steps, "changed": changed, "turns": turn + 1}
        elif tool == "list":
            obs = await _remote_list(host_id, workdir)
        elif tool == "read":
            obs = await _remote_read(host_id, workdir, action.get("path", ""))
        elif tool == "grep":
            obs = await _remote_grep(host_id, workdir, action.get("pattern", ""))
        elif tool == "write":
            w = await _remote_write(host_id, workdir, action.get("path", ""),
                                    action.get("content", ""))
            obs = f"write ok={w['ok']} path={w['path']}" + (f" error={w['error']}" if not w["ok"] else "")
            if w["ok"]:
                changed.append(action.get("path", ""))
        elif tool == "run":
            obs = await _remote_run_cmd(host_id, workdir, action.get("command", ""))
        else:
            obs = f"[unknown tool: {tool}]"
        steps.append({"turn": turn, "tool": tool, "obs_preview": obs[:200]})
        history.append({"role": "user", "content": f"OBSERVATION:\n{obs[:6000]}"})
        transcript = "Continue. Emit the next JSON tool call (or done)."
    return {"ok": True, "engine": "vera-agent", "summary": "max turns reached",
            "steps": steps, "changed": changed, "turns": max_turns}


async def _run_task(instance_id: str, task: str, engine: str = "claude",
                    agent: str = "writer", permission_mode: str = "",
                    session_id: str = "", source: str = "user",
                    workdir: str = "", model: str = "") -> dict:
    """Shared entry used by ide.remote.run AND the queue dispatcher. `workdir`
    (optional) overrides the instance's project root for this run — used by
    Loop Lab to pin edits to a branch WORKTREE so the real source is never
    touched."""
    inst = _get_instance(instance_id)
    if not inst:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    sid = session_id or _ide_get_session_id()
    t0 = time.monotonic()
    engine = (engine or "claude").lower()
    await emit_event({"type": "ide.remote.run", "stage": "start", "engine": engine,
                      "instance_id": instance_id, "task": task[:160], "session_id": sid})
    if engine == "vera-agent":
        out = await _run_vera_agent(inst, task, agent=agent, session_id=sid,
                                    workdir=workdir)
    elif inst.get("kind") == "vscode-client":
        out = await _run_claude_client(inst, task, permission_mode=permission_mode,
                                       model=model)
    else:
        out = await _run_claude(inst, task, permission_mode=permission_mode,
                                workdir=workdir, model=model)
    elapsed_ms = round((time.monotonic() - t0) * 1000)
    out["elapsed_ms"] = elapsed_ms
    out["instance_id"] = instance_id

    asyncio.ensure_future(_record(
        session_id=sid, category="ide.remote.run",
        text=f"[remote/{engine}] {inst.get('label')}: {task[:140]}",
        full_text=(f"Instance: {inst.get('label')} ({instance_id})\nEngine: {engine}\n"
                   f"Task: {task}\n\nSummary:\n{out.get('summary', '')[:8000]}"),
        tags=["ide", "remote", "run", engine] + ([source] if source else []),
        importance=0.7, source_type="ai", record_type="observation",
        capability_name="ide.remote.run", broadcast_type="ide.remote.run_done",
        fabric_dataset="ide.remote_runs",
        metadata={"instance_id": instance_id, "engine": engine, "ok": out.get("ok"),
                  "elapsed_ms": elapsed_ms, "source": source},
        fabric_data={"instance": inst.get("label"), "engine": engine, "task": task[:4000],
                     "summary": out.get("summary", "")[:20000],
                     "changed": out.get("changed", []), "source": source},
    ))
    await emit_event({"type": "ide.remote.run", "stage": "done", "engine": engine,
                      "instance_id": instance_id, "ok": out.get("ok"),
                      "elapsed_ms": elapsed_ms, "session_id": sid})
    return out


@capability(
    "ide.remote.run",
    http_method="POST", http_path="/ide/remote/run", http_tags=["ide", "remote"],
    memory="on",
    description="Run a coding task on a remote instance/project. "
                "engine='claude' runs the Claude Code CLI headless over SSH; "
                "engine='vera-agent' drives an IDE Ollama agent (thinker|writer|analyser) "
                "editing the remote FS over SSH. Every run is recorded to memory + fabric "
                "(dataset ide.remote_runs) for dreaming/queries. "
                "Input: instance_id (str!), task (str!), engine (claude|vera-agent), "
                "agent (thinker|writer|analyser), permission_mode (str), model (str — "
                "e.g. 'opus'/'sonnet'/'haiku' or a full model id; engine=claude only, "
                "blank lets the CLI's own default decide), session_id (str). "
                "Output: {ok, engine, summary, changed, elapsed_ms}. "
                "For live token streaming use POST /ide-api/remote/run instead.",
    schema=enum_schema(engine=["claude", "vera-agent"],
                       agent=["writer", "thinker", "analyser"]),
)
async def ide_remote_run(
    instance_id:     str = "",
    task:            str = "",
    engine:          str = "claude",
    agent:           str = "writer",
    permission_mode: str = "",
    model:           str = "",
    session_id:      str = "",
    trace_id=None,
):
    if not instance_id:
        return {"ok": False, "error": "instance_id required"}
    if not task.strip():
        return {"ok": False, "error": "task required"}
    return await _run_task(instance_id, task, engine=engine, agent=agent,
                           permission_mode=permission_mode, session_id=session_id,
                           model=model)


# ═════════════════════════════════════════════════════════════════════════════
#  SSE STREAMING RUN  —  POST /ide-api/remote/run   (Claude Code live tokens)
# ═════════════════════════════════════════════════════════════════════════════

def _sse(event: str, data) -> bytes:
    return ("data: " + json.dumps({"event": event, "data": data}) + "\n\n").encode("utf-8")


async def _stream_ssh_lines(host_id: str, command: str):
    """Async-generator of stdout lines from a remote command, using asyncssh's
    streaming process. Falls back to a single blocking capture if asyncssh or
    cred resolution is unavailable."""
    ex = _exec_mod()
    if not (ex and getattr(ex, "HAS_ASYNCSSH", False) and hasattr(ex, "_resolve_host_record")):
        res = await _ssh(host_id, command, timeout=1800)
        for ln in (res.get("stdout", "") + res.get("stderr", "")).splitlines():
            yield ("line", ln)
        yield ("exit", res.get("rc", 0))
        return
    try:
        rec = await ex._resolve_host_record(host_id)
        if not rec:
            yield ("error", f"host_id not found: {host_id}")
            yield ("exit", -1)
            return
        auth = rec.get("auth", "password")
        password = ex._deobfuscate(rec.get("password_obf", "")) if auth == "password" else ""
        passphrase = ex._deobfuscate(rec.get("passphrase_obf", "")) if auth != "password" else ""
        kw = await ex._ssh_connect_kwargs(
            rec.get("host", ""), port=int(rec.get("port", 22) or 22),
            user=rec.get("user", ""), password=password,
            key_path=rec.get("key_path", ""), passphrase=passphrase)
        async with ex.asyncssh.connect(**kw) as conn:
            async with conn.create_process(command) as proc:
                async for line in proc.stdout:
                    yield ("line", line.rstrip("\n"))
                async for line in proc.stderr:
                    yield ("stderr", line.rstrip("\n"))
                await proc.wait()
                yield ("exit", proc.returncode)
    except Exception as e:
        yield ("error", f"{type(e).__name__}: {e}")
        yield ("exit", -1)


@APP.post("/ide-api/remote/run", include_in_schema=False)
async def ide_remote_run_stream(request: Request):
    """SSE-stream a Claude Code run on a remote instance. Sandbox-gated like the
    IDE Run terminal. Frames: {event: 'meta'|'assistant'|'tool'|'result'|
    'stderr'|'exit'|'error', data}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    instance_id = body.get("instance_id", "")
    task        = (body.get("task") or "").strip()
    permission  = body.get("permission_mode", "")
    model       = (body.get("model") or "").strip()
    session_id  = body.get("session_id", "") or _ide_get_session_id()

    inst = _get_instance(instance_id)

    def _err(msg: str):
        async def _g():
            yield _sse("error", msg)
            yield _sse("exit", -1)
        return StreamingResponse(_g(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if not inst:
        return _err(f"instance not found: {instance_id}")
    if not task:
        return _err("task required")
    if not inst.get("host_id"):
        return _err("instance has no host_id")

    workdir = inst.get("workdir", "") or "."
    key, oauth = await _claude_auth(inst)
    cmd = _build_claude_cmd(workdir, task, key, stream=True,
                            permission_mode=permission, oauth_token=oauth, model=model)

    # Gate the remote command through the shared exec sandbox (same as ide.run).
    sb = None
    for _n, _m in list(sys.modules.items()):
        if _m is not None and _n.endswith("exec_capabilities") and hasattr(_m, "_sandbox_check"):
            sb = _m
            break
    if sb is not None:
        try:
            ok, reason = sb._sandbox_check("claude -p ...", cwd=workdir)
        except Exception:
            ok, reason = True, ""
        if not ok:
            await emit_event({"type": "exec.sandbox.blocked", "shell": "ide.remote.run",
                              "reason": reason, "session_id": session_id})
            return _err(f"sandbox blocked: {reason}")

    async def _gen():
        t0 = time.monotonic()
        yield _sse("meta", {"instance_id": instance_id, "engine": "claude",
                            "workdir": workdir, "label": inst.get("label")})
        await emit_event({"type": "ide.remote.run", "stage": "start", "engine": "claude",
                          "instance_id": instance_id, "task": task[:160],
                          "streaming": True, "session_id": session_id})
        assembled: List[str] = []
        rc = -1
        async for kind, payload in _stream_ssh_lines(inst["host_id"], cmd):
            if kind == "exit":
                rc = payload
                continue
            if kind in ("error", "stderr"):
                yield _sse(kind, payload)
                continue
            # stream-json: one JSON event per line — classify for the UI.
            line = payload.strip()
            if not line:
                continue
            obj = None
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except Exception:
                    obj = None
            if obj is None:
                yield _sse("stdout", line)
                continue
            etype = obj.get("type", "")
            if etype == "assistant":
                txt = _text_of(obj)
                if txt:
                    assembled.append(txt)
                yield _sse("assistant", txt or obj)
            elif etype in ("tool_use", "tool_result", "user"):
                yield _sse("tool", obj)
            elif etype == "result":
                assembled.append(obj.get("result", "") or "")
                yield _sse("result", obj)
            else:
                yield _sse("stdout", obj)

        full = "\n".join(t for t in assembled if t).strip()
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        # Record like every other IDE stream (syslog + cap_tracking + graph/fabric).
        try:
            await record_stream_activity(
                cap_name="ide.remote.run", session_id=session_id,
                params={"instance_id": instance_id, "engine": "claude",
                        "task": task, "workdir": workdir},
                result={"ok": rc == 0, "preview": full[:800], "elapsed_ms": elapsed_ms, "rc": rc},
                elapsed_ms=elapsed_ms, group="ide")
        except Exception as e:
            log.debug("remote stream record_stream_activity: %s", e)
        asyncio.ensure_future(_record(
            session_id=session_id, category="ide.remote.run",
            text=f"[remote/claude stream] {inst.get('label')}: {task[:140]}",
            full_text=f"Task: {task}\n\n{full[:12000]}",
            tags=["ide", "remote", "run", "claude", "stream"],
            importance=0.7, source_type="ai", record_type="observation",
            capability_name="ide.remote.run", broadcast_type="ide.remote.run_done",
            fabric_dataset="ide.remote_runs",
            metadata={"instance_id": instance_id, "engine": "claude", "rc": rc,
                      "elapsed_ms": elapsed_ms},
            fabric_data={"instance": inst.get("label"), "engine": "claude",
                         "task": task[:4000], "summary": full[:20000]},
        ))
        await emit_event({"type": "ide.remote.run", "stage": "done", "engine": "claude",
                          "instance_id": instance_id, "rc": rc, "streaming": True,
                          "elapsed_ms": elapsed_ms, "session_id": session_id})
        yield _sse("exit", rc)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


def _text_of(obj: dict) -> str:
    """Extract assistant text from a stream-json assistant event."""
    msg = obj.get("message", obj)
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return ""


# ═════════════════════════════════════════════════════════════════════════════
#  CLIENT DISPATCH CHANNEL  —  kind=vscode-client (a user's live VS Code window)
# ═════════════════════════════════════════════════════════════════════════════
# The vera-vscode extension, in client mode, registers itself as an instance
# (kind=vscode-client) and long-polls POST /ide-api/remote/client/poll. Vera
# pushes actions into the window — open a file, run a VS Code command, drive
# the terminal, type text, or run a Claude Code task (which uses the CLIENT's
# own `claude` sign-in, so subscription auth comes for free). Results are
# posted back to /ide-api/remote/client/result and resolve the dispatcher's
# future, which lets the work queue treat a live editor like any other host.

_CLIENT_ACTION_KINDS = ("open_file", "run_command", "terminal", "type_text",
                        "claude_task", "notify",
                        "claude_sessions_scan", "claude_sessions_read")
_CLIENT_ACTIONS: Dict[str, List[dict]] = {}      # instance_id -> pending actions
_CLIENT_WAKE:    Dict[str, asyncio.Event] = {}   # instance_id -> poll waker
_CLIENT_RESULTS: Dict[str, asyncio.Future] = {}  # action_id -> result future
_CLIENT_LAST_POLL: Dict[str, float] = {}         # instance_id -> monotonic-ish ts


def _client_alive(instance_id: str, horizon: float = 120.0) -> bool:
    """True when the client window has polled recently enough to receive work."""
    return (time.time() - _CLIENT_LAST_POLL.get(instance_id, 0)) < horizon


def _client_token_ok(inst: dict, token: str) -> bool:
    sealed = inst.get("token_sealed", "")
    if not sealed:
        return True                 # no secret registered — LAN-trusted
    return bool(token) and _open(sealed) == token


async def _client_dispatch(instance_id: str, action: str, args: dict,
                           wait: float = 0) -> dict:
    """Queue an action for a client window; optionally await its result."""
    aid = str(uuid.uuid4())
    item = {"id": aid, "action": action, "args": args or {},
            "created_at": now_iso()}
    fut: Optional[asyncio.Future] = None
    if wait > 0:
        fut = asyncio.get_event_loop().create_future()
        _CLIENT_RESULTS[aid] = fut
    _CLIENT_ACTIONS.setdefault(instance_id, []).append(item)
    _CLIENT_WAKE.setdefault(instance_id, asyncio.Event()).set()
    await emit_event({"type": "ide.remote.client", "stage": "dispatched",
                      "instance_id": instance_id, "action": action,
                      "action_id": aid, "awaited": wait > 0})
    if fut is None:
        return {"ok": True, "action_id": aid, "queued": True}
    try:
        res = await asyncio.wait_for(fut, timeout=wait)
        return {"action_id": aid, **(res if isinstance(res, dict) else {"result": res})}
    except asyncio.TimeoutError:
        return {"ok": False, "action_id": aid,
                "error": f"client did not report a result within {int(wait)}s"}
    finally:
        _CLIENT_RESULTS.pop(aid, None)


@capability(
    "ide.remote.client.dispatch",
    http_method="POST", http_path="/ide/remote/client/dispatch", http_tags=["ide", "remote"],
    memory="on",
    description="Push an action into a connected VS Code client window "
                "(kind=vscode-client — the vera-vscode extension in client mode). "
                "Actions: open_file {path} · run_command {command, args} · "
                "terminal {text, enter} · type_text {text} · "
                "claude_task {task, permission_mode} (runs the CLIENT's Claude Code, "
                "using its local sign-in) · notify {message} · "
                "claude_sessions_scan {} (list the client's local "
                "~/.claude/projects/**/*.jsonl transcripts) · "
                "claude_sessions_read {path, offset} (tail one transcript from a "
                "byte offset) — used by ide.claude_sessions.* to ingest this "
                "machine's Claude Code conversations. "
                "Input: instance_id (str!), action (str!), args (dict), "
                "wait (int seconds — 0 = fire-and-forget, >0 = await the client's "
                "result). Output: {ok, action_id, queued|result…}.",
    schema=enum_schema(action=list(_CLIENT_ACTION_KINDS)),
)
async def ide_remote_client_dispatch(
    instance_id: str = "", action: str = "", args: Optional[dict] = None,
    wait: int = 0, trace_id=None,
):
    inst = _get_instance(instance_id)
    if not inst:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    if inst.get("kind") != "vscode-client":
        return {"ok": False, "error": "instance is not a vscode-client "
                                      "(use ide.remote.run for host-backed instances)"}
    if action not in _CLIENT_ACTION_KINDS:
        return {"ok": False, "error": f"unknown action: {action} "
                                      f"(one of {', '.join(_CLIENT_ACTION_KINDS)})"}
    out = await _client_dispatch(instance_id, action, args or {}, wait=float(wait or 0))
    if not _client_alive(instance_id):
        out["warning"] = "client has not polled recently — action will run when it reconnects"
    return out


@APP.post("/ide-api/remote/client/poll", include_in_schema=False)
async def ide_remote_client_poll(request: Request):
    """Long-poll endpoint the extension's client mode calls in a loop.
    Body: {instance_id, token, wait=25}. Returns {ok, actions: [...]}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    iid = body.get("instance_id", "")
    inst = _get_instance(iid)
    if not inst or inst.get("kind") != "vscode-client":
        return {"ok": False, "error": f"vscode-client instance not found: {iid}"}
    if not _client_token_ok(inst, body.get("token", "")):
        return {"ok": False, "error": "bad client token"}
    wait = min(max(float(body.get("wait", 25) or 25), 0), 55)

    first_poll = not _client_alive(iid)
    _CLIENT_LAST_POLL[iid] = time.time()
    rows = _load_instances()
    for r in rows:
        if r.get("id") == iid:
            r["status"] = "running"
            r["last_seen"] = now_iso()
    _save_instances(rows)
    if first_poll:
        await emit_event({"type": "ide.remote.client", "stage": "connected",
                          "instance_id": iid, "label": inst.get("label", "")})

    ev = _CLIENT_WAKE.setdefault(iid, asyncio.Event())
    if not _CLIENT_ACTIONS.get(iid):
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
    _CLIENT_LAST_POLL[iid] = time.time()
    actions = _CLIENT_ACTIONS.pop(iid, [])
    return {"ok": True, "actions": actions}


@APP.post("/ide-api/remote/client/result", include_in_schema=False)
async def ide_remote_client_result(request: Request):
    """The extension posts each executed action's outcome here.
    Body: {instance_id, token, action_id, ok, summary, result, error}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    iid = body.get("instance_id", "")
    inst = _get_instance(iid)
    if not inst or inst.get("kind") != "vscode-client":
        return {"ok": False, "error": f"vscode-client instance not found: {iid}"}
    if not _client_token_ok(inst, body.get("token", "")):
        return {"ok": False, "error": "bad client token"}
    aid = body.get("action_id", "")
    payload = {"ok": bool(body.get("ok", True)),
               "summary": (body.get("summary") or "")[:4000],
               "result": body.get("result"),
               "error": (body.get("error") or "")[:2000]}
    fut = _CLIENT_RESULTS.get(aid)
    if fut is not None and not fut.done():
        fut.set_result(payload)
    await emit_event({"type": "ide.remote.client", "stage": "result",
                      "instance_id": iid, "action_id": aid, "ok": payload["ok"]})
    return {"ok": True, "delivered": fut is not None}


async def _run_claude_client(inst: dict, task: str, permission_mode: str = "",
                             model: str = "", timeout: int = 1800) -> dict:
    """Run a Claude Code task inside a connected VS Code client window. The
    client spawns `claude -p` itself, so it authenticates with whatever the
    user is signed in as there (subscription or key). `model`, when given,
    overrides the extension's own vera.clientDefaultModel for this task."""
    if not _client_alive(inst.get("id", "")):
        return {"ok": False, "engine": "claude",
                "error": "vscode client is not connected (no recent poll)"}
    args = {"task": task, "permission_mode": permission_mode or _CLAUDE_PERMISSION_MODE}
    if model:
        args["model"] = model
    r = await _client_dispatch(inst["id"], "claude_task", args, wait=float(timeout))
    return {"ok": bool(r.get("ok")), "engine": "claude", "via": "vscode-client",
            "summary": (r.get("summary") or r.get("error") or "")[:4000],
            "result": r.get("result"), "error": r.get("error", "")}


# ═════════════════════════════════════════════════════════════════════════════
#  AUTONOMOUS WORK QUEUE  +  DISPATCHER
# ═════════════════════════════════════════════════════════════════════════════
_BUSY: set = set()          # instance_ids currently running a task


@capability(
    "ide.remote.queue.add",
    http_method="POST", http_path="/ide/remote/queue/add", http_tags=["ide", "remote"],
    memory="on",
    description="Enqueue a coding task for autonomous execution on a remote instance. "
                "Input: task (str!), instance_id (str — or 'any'), engine (claude|vera-agent), "
                "agent (writer|thinker|analyser), model (str — e.g. 'opus'/'sonnet'/'haiku' "
                "or a full model id; engine=claude only, blank lets the CLI's own default "
                "decide), priority (int=5, lower=sooner), "
                "source (user|dream|project), session_id (str), workdir (str — "
                "override the instance's project root for this task, e.g. a Loop "
                "Lab branch worktree). Output: {ok, item}.",
    schema=enum_schema(engine=["claude", "vera-agent"],
                       agent=["writer", "thinker", "analyser"],
                       source=["user", "dream", "project"]),
)
async def ide_remote_queue_add(
    task:        str = "",
    instance_id: str = "any",
    engine:      str = "claude",
    agent:       str = "writer",
    model:       str = "",
    priority:    int = 5,
    source:      str = "user",
    session_id:  str = "",
    workdir:     str = "",
    trace_id=None,
):
    if not task.strip():
        return {"ok": False, "error": "task required"}
    item = {
        "id": str(uuid.uuid4()), "task": task, "instance_id": instance_id or "any",
        "engine": (engine or "claude").lower(), "agent": agent, "model": model or "",
        "priority": int(priority),
        "source": source or "user", "session_id": session_id or _ide_get_session_id(),
        "workdir": workdir or "",
        "status": "queued", "created_at": now_iso(),
        "started_at": "", "finished_at": "", "summary": "", "ok": None,
    }
    q = _load_queue()
    q.append(item)
    _save_queue(q)
    await emit_event({"type": "ide.remote.queue", "action": "added", "id": item["id"],
                      "source": item["source"], "task": task[:120]})
    return {"ok": True, "item": item}


@capability(
    "ide.remote.queue.list",
    http_method="GET", http_path="/ide/remote/queue/list", http_tags=["ide", "remote"],
    memory="off", silent=True,
    description="List work-queue items. Input: status (str — filter, optional). "
                "Output: {items: [...], counts: {queued, running, done, error}, autopilot}.",
)
async def ide_remote_queue_list(status: str = "", trace_id=None):
    q = _load_queue()
    if status:
        q = [i for i in q if i.get("status") == status]
    counts: Dict[str, int] = {}
    for i in _load_queue():
        counts[i.get("status", "?")] = counts.get(i.get("status", "?"), 0) + 1
    s = _load_settings()
    return {"items": q, "counts": counts, "autopilot": s.get("autopilot"),
            "max_concurrency": s.get("max_concurrency"), "busy": list(_BUSY)}


@capability(
    "ide.remote.queue.cancel",
    http_method="POST", http_path="/ide/remote/queue/cancel", http_tags=["ide", "remote"],
    memory="on",
    description="Cancel a queued item (running items are marked cancelled but may finish). "
                "Input: id (str!). Output: {ok, item}.",
)
async def ide_remote_queue_cancel(id: str = "", trace_id=None):
    if not id:
        return {"ok": False, "error": "id required"}
    q = _load_queue()
    for i in q:
        if i.get("id") == id:
            if i.get("status") in ("queued", "running"):
                i["status"] = "cancelled"
                i["finished_at"] = now_iso()
            _save_queue(q)
            await emit_event({"type": "ide.remote.queue", "action": "cancelled", "id": id})
            return {"ok": True, "item": i}
    return {"ok": False, "error": f"item not found: {id}"}


@capability(
    "ide.remote.queue.clear",
    http_method="POST", http_path="/ide/remote/queue/clear", http_tags=["ide", "remote"],
    memory="on",
    description="Clear finished items (done/error/cancelled), or all with which='all'. "
                "Input: which (finished|all). Output: {ok, removed}.",
    schema=enum_schema(which=["finished", "all"]),
)
async def ide_remote_queue_clear(which: str = "finished", trace_id=None):
    q = _load_queue()
    if which == "all":
        removed = len(q)
        _save_queue([])
    else:
        keep = [i for i in q if i.get("status") in ("queued", "running")]
        removed = len(q) - len(keep)
        _save_queue(keep)
    return {"ok": True, "removed": removed}


@capability(
    "ide.remote.autopilot",
    http_method="POST", http_path="/ide/remote/autopilot", http_tags=["ide", "remote"],
    memory="on",
    description="Enable/disable the autonomous dispatcher and set max concurrency. "
                "Input: enabled (bool), max_concurrency (int). "
                "Call with no args to just read current settings. Output: {ok, autopilot, max_concurrency}.",
)
async def ide_remote_autopilot(enabled: Optional[bool] = None,
                               max_concurrency: int = 0, trace_id=None):
    s = _load_settings()
    if enabled is not None:
        s["autopilot"] = bool(enabled)
    if max_concurrency and max_concurrency > 0:
        s["max_concurrency"] = int(max_concurrency)
    _save_settings(s)
    await emit_event({"type": "ide.remote.autopilot", "autopilot": s["autopilot"],
                      "max_concurrency": s["max_concurrency"]})
    return {"ok": True, "autopilot": s["autopilot"], "max_concurrency": s["max_concurrency"]}


def _pick_instance_for(item: dict, instances: List[dict]) -> Optional[dict]:
    want = item.get("instance_id", "any")
    if want and want != "any":
        inst = next((i for i in instances if i.get("id") == want), None)
        return inst if (inst and inst["id"] not in _BUSY) else None
    # 'any' — first free host-backed instance, or a live VS Code client window
    for i in instances:
        if i["id"] in _BUSY:
            continue
        if i.get("host_id"):
            return i
        if i.get("kind") == "vscode-client" and _client_alive(i["id"]):
            return i
    return None


async def _run_queue_item(item: dict, inst: dict):
    """Execute one queued item end-to-end, updating its status in the file."""
    iid = inst["id"]
    _BUSY.add(iid)

    def _update(**fields):
        q = _load_queue()
        for it in q:
            if it.get("id") == item["id"]:
                it.update(fields)
        _save_queue(q)

    _update(status="running", started_at=now_iso(), instance_id=iid)
    await emit_event({"type": "ide.remote.queue", "action": "running",
                      "id": item["id"], "instance_id": iid})
    try:
        out = await _run_task(iid, item["task"], engine=item.get("engine", "claude"),
                              agent=item.get("agent", "writer"),
                              session_id=item.get("session_id", ""),
                              source=item.get("source", "user"),
                              workdir=item.get("workdir", ""),
                              model=item.get("model", ""))
        # Respect a cancel that landed mid-run.
        cur = next((i for i in _load_queue() if i.get("id") == item["id"]), {})
        if cur.get("status") == "cancelled":
            _update(finished_at=now_iso(), summary=out.get("summary", "")[:2000], ok=out.get("ok"))
        else:
            _update(status="done" if out.get("ok") else "error",
                    finished_at=now_iso(), summary=out.get("summary", "")[:2000],
                    ok=bool(out.get("ok")))
        await emit_event({"type": "ide.remote.queue", "action": "finished",
                          "id": item["id"], "ok": out.get("ok")})
    except Exception as e:
        log.exception("queue item %s failed", item["id"])
        _update(status="error", finished_at=now_iso(), summary=f"{type(e).__name__}: {e}", ok=False)
    finally:
        _BUSY.discard(iid)


async def _queue_tick():
    """Scheduler tick: dispatch queued items to free instances (autopilot on)."""
    s = _load_settings()
    if not s.get("autopilot"):
        return
    max_conc = int(s.get("max_concurrency", 1) or 1)
    if len(_BUSY) >= max_conc:
        return
    q = _load_queue()
    queued = sorted([i for i in q if i.get("status") == "queued"],
                    key=lambda i: (i.get("priority", 5), i.get("created_at", "")))
    if not queued:
        return
    instances = _load_instances()
    for item in queued:
        if len(_BUSY) >= max_conc:
            break
        inst = _pick_instance_for(item, instances)
        if inst:
            asyncio.ensure_future(_run_queue_item(item, inst))


@capability(
    "ide.remote.summary",
    http_method="GET", http_path="/ide/remote/summary", http_tags=["ide", "remote"],
    memory="off",
    description="Summarise remote IDE work for a session from the memory graph "
                "(powers 'what did Vera do on the remote boxes?' queries). "
                "Input: session_id (str! — query param). "
                "Output: {session_id, runs, instances, queue_counts, categories}.",
)
async def ide_remote_summary(session_id: str = "", trace_id=None):
    m = sys.modules.get("memory")
    cats: Dict[str, int] = {}
    if m and getattr(m, "MEMORY", None):
        try:
            results = await m.MEMORY.search("", limit=200, filters={"session_id": session_id})
            for item in (results or []):
                rec = item.get("record", item) if isinstance(item, dict) else item
                c = getattr(rec, "category", None) or ""
                if c.startswith("ide.remote"):
                    cats[c] = cats.get(c, 0) + 1
        except Exception as e:
            log.debug("remote summary search: %s", e)
    qcounts: Dict[str, int] = {}
    for i in _load_queue():
        qcounts[i.get("status", "?")] = qcounts.get(i.get("status", "?"), 0) + 1
    return {"session_id": session_id, "runs": cats.get("ide.remote.run", 0),
            "instances": len(_load_instances()), "queue_counts": qcounts,
            "categories": cats}


# ═════════════════════════════════════════════════════════════════════════════
#  BRIDGE  —  wire Vera into remote Claude Code as an MCP server
# ═════════════════════════════════════════════════════════════════════════════
_BRIDGE_SRC = _HERE / "vera_mcp_bridge.py"
_DEFAULT_ALLOW = "ide.,fabric.,memory.,dream.,project.,web."


def _vera_base_url() -> str:
    """Best-effort self URL the remote bridge should call back to."""
    base = os.getenv("VERA_PUBLIC_URL", "").strip()
    if base:
        return base.rstrip("/")
    host = os.getenv("VERA_HOST", "").strip()
    port = os.getenv("VERA_PORT", "").strip() or "8000"
    if host:
        return f"http://{host}:{port}"
    return f"http://127.0.0.1:{port}"


@capability(
    "ide.remote.bridge.install",
    http_method="POST", http_path="/ide/remote/bridge/install", http_tags=["ide", "remote"],
    memory="on",
    description="Deploy the Vera MCP bridge to a remote host and register it with the "
                "host's Claude Code (`claude mcp add vera ...`), plus write <workdir>/.mcp.json. "
                "Lets remote Claude Code call Vera capabilities. "
                "Input: instance_id (str!), vera_url (str — defaults to auto), "
                "allow (csv cap-name prefixes). Output: {ok, url, registered, steps}.",
)
async def ide_remote_bridge_install(
    instance_id: str = "", vera_url: str = "", allow: str = "", trace_id=None,
):
    inst = _get_instance(instance_id)
    if not inst:
        return {"ok": False, "error": f"instance not found: {instance_id}"}
    host_id = inst.get("host_id", "")
    if not host_id:
        return {"ok": False, "error": "instance has no host_id"}
    base = (vera_url or _vera_base_url()).rstrip("/")
    allow = allow or _DEFAULT_ALLOW
    workdir = inst.get("workdir", "") or "."

    try:
        src = _BRIDGE_SRC.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"bridge source unreadable: {e}"}

    import base64
    b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
    steps: List[dict] = []

    # 1) copy bridge to ~/.vera/vera_mcp_bridge.py
    deploy = (f"mkdir -p ~/.vera && printf %s {shlex.quote(b64)} | base64 -d "
              f"> ~/.vera/vera_mcp_bridge.py && echo {_DONE}")
    r1 = await _ssh(host_id, deploy, timeout=60)
    steps.append({"step": "deploy", "ok": _DONE in (r1.get("stdout", "") or "")})

    # 2) register with claude (idempotent: remove then add)
    add = (f"cd {shlex.quote(workdir)} 2>/dev/null; "
           f"claude mcp remove vera 2>/dev/null; "
           f"claude mcp add vera -- python3 ~/.vera/vera_mcp_bridge.py "
           f"--url {shlex.quote(base)} --allow {shlex.quote(allow)} "
           f"&& claude mcp list 2>/dev/null; echo {_DONE}")
    r2 = await _ssh(host_id, add, timeout=90)
    registered = "vera" in (r2.get("stdout", "") or "").lower()
    steps.append({"step": "claude mcp add", "ok": registered,
                  "tail": (r2.get("stdout", "") or r2.get("stderr", ""))[-300:]})

    # 3) drop a project .mcp.json (works even without the CLI registration)
    mcp_json = json.dumps({"mcpServers": {"vera": {
        "command": "python3",
        "args": ["~/.vera/vera_mcp_bridge.py", "--url", base, "--allow", allow],
    }}}, indent=2)
    w = await _remote_write(host_id, workdir, ".mcp.json", mcp_json)
    steps.append({"step": ".mcp.json", "ok": w.get("ok")})

    ok = all(s.get("ok") for s in steps)
    rows = _load_instances()
    for r in rows:
        if r.get("id") == instance_id:
            r["bridge_installed"] = ok
            r["bridge_url"] = base
            r["updated_at"] = now_iso()
    _save_instances(rows)
    await emit_event({"type": "ide.remote.bridge", "action": "installed",
                      "instance_id": instance_id, "ok": ok, "url": base})
    return {"ok": ok, "url": base, "registered": registered, "steps": steps}


@capability(
    "ide.remote.bridge.status",
    http_method="POST", http_path="/ide/remote/bridge/status", http_tags=["ide", "remote"],
    memory="off",
    description="Check whether the Vera MCP bridge is installed + registered on a host. "
                "Input: instance_id (str!). Output: {ok, bridge_file, claude_registered}.",
)
async def ide_remote_bridge_status(instance_id: str = "", trace_id=None):
    inst = _get_instance(instance_id)
    if not inst or not inst.get("host_id"):
        return {"ok": False, "error": "host-backed instance required"}
    cmd = ("echo \"bridge=$(test -f ~/.vera/vera_mcp_bridge.py && echo yes || echo no)\"; "
           "echo \"registered=$(claude mcp list 2>/dev/null | grep -qi vera && echo yes || echo no)\"")
    res = await _ssh(inst["host_id"], cmd, timeout=30)
    kv = _parse_kv(res.get("stdout", "")) if res.get("ok") else {}
    return {"ok": True, "bridge_file": kv.get("bridge") == "yes",
            "claude_registered": kv.get("registered") == "yes"}


@capability(
    "ide.remote.bridge.source",
    http_method="GET", http_path="/ide/remote/bridge/source", http_tags=["ide", "remote"],
    memory="off", silent=True,
    description="Return the vera_mcp_bridge.py source (text/plain) so the VS Code "
                "extension / a client can deploy it locally. Output: the script text.",
)
async def ide_remote_bridge_source(trace_id=None):
    from fastapi.responses import PlainTextResponse
    try:
        return PlainTextResponse(_BRIDGE_SRC.read_text(encoding="utf-8"))
    except Exception as e:
        return PlainTextResponse(f"# bridge source unavailable: {e}", status_code=500)


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
_PANEL_PATH = _HERE / "ide_remote_panel.html"


@capability(
    "ide.remote.panel.html",
    http_method="GET", http_path="/ide/remote/panel", http_tags=["ide", "remote", "ui"],
    memory="off", silent=True,
    description="Serve the Remote IDE panel HTML (text/html).",
)
async def ide_remote_panel_html(trace_id=None):
    try:
        html = _PANEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef4444;"
                "font-family:monospace;padding:40px'><h2>ide_remote_panel.html not found</h2>"
                f"<p>Expected: {_PANEL_PATH}</p></body></html>")
    return HTMLResponse(html)


register_ui(
    "ide-remote-panel",
    "Remote IDE",
    "🛰",
    """<div id="ide-remote-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/ide/remote/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "ide.remote.instances", "ide.remote.register", "ide.remote.delete",
        "ide.remote.detect", "ide.remote.provision", "ide.remote.status",
        "ide.remote.start", "ide.remote.stop", "ide.remote.open",
        "ide.remote.run",
        "ide.remote.queue.add", "ide.remote.queue.list", "ide.remote.queue.cancel",
        "ide.remote.queue.clear", "ide.remote.autopilot", "ide.remote.summary",
        "ide.remote.client.dispatch",
        "ide.remote.bridge.install", "ide.remote.bridge.status",
        "exec.ssh.hosts.list",
    ],
    # Merged into the IDE tab (vscode_panel.html embeds /ide/remote/panel as its
    # "Remotes & Queue" view). mode="element" keeps it listed for custom tabs /
    # solo pop-out without rendering a second top-level tab.
    mode="element",
    tab_order=51,
)


# ═════════════════════════════════════════════════════════════════════════════
#  DISPATCHER SCHEDULE
# ═════════════════════════════════════════════════════════════════════════════
schedule(_queue_tick, interval=10, name="ide_remote_queue")

log.info("ide_remote_capabilities loaded — %d instances, autopilot=%s",
         len(_load_instances()), _load_settings().get("autopilot"))
