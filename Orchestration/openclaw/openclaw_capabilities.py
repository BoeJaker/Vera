"""
openclaw_capabilities.py  —  OpenClaw Agentic Loop Integration for Vera
========================================================================
Optional module. Load this alongside capability_orchestration.py to bridge
Vera and a running OpenClaw gateway.

Integration points
──────────────────
  OpenClaw → Vera   OpenClaw can call any Vera capability as an HTTP tool.
                    A lightweight /openclaw/tools and /openclaw/call REST
                    surface is mounted so skills can be pointed at Vera.

  Vera → OpenClaw   Vera connects to the OpenClaw WS gateway (protocol v3),
                    creates/resumes sessions, and streams agent turns. Results
                    are surfaced through Vera's event stream.

Capabilities registered
────────────────────────
  openclaw.status         — connection status + gateway info
  openclaw.connect        — (re)connect to OpenClaw gateway
  openclaw.disconnect     — disconnect gracefully
  openclaw.prompt         — send a prompt to OpenClaw, stream response back
  openclaw.sessions.list  — list active OpenClaw sessions
  openclaw.sessions.reset — reset (clear) an OpenClaw session
  openclaw.config.get     — get current OpenClaw connection config
  openclaw.config.set     — update OpenClaw connection config

Configuration (env or runtime)
────────────────────────────────
  OPENCLAW_ENABLED        "0" | "1"   (default "0" — opt-in)
  OPENCLAW_WS_URL         ws://localhost:18789
  OPENCLAW_TOKEN          shared secret / gateway password
  OPENCLAW_AGENT_ID       "main"       default agent to address
  OPENCLAW_VERA_BASE_URL  http://localhost:8000  (Vera's own URL for tool bridge)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ── Vera imports ──────────────────────────────────────────────────────────────
import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
    register_ui,
    schedule,
)
from Vera.Orchestration.config import cfg

log = logging.getLogger("openclaw")

# ── Optional websockets dep ───────────────────────────────────────────────────
try:
    import websockets
    import websockets.exceptions
    HAS_WS = True
except ImportError:
    websockets = None
    HAS_WS = False
    log.warning("openclaw: 'websockets' package not installed — install with: pip install websockets")

# ══════════════════════════════════════════════════════════════════════════════
# Runtime config / state
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OpenClawConfig:
    enabled: bool = False
    ws_url: str = "ws://localhost:18789"
    token: str = ""
    agent_id: str = "main"
    vera_base_url: str = "http://localhost:8000"
    auto_reconnect: bool = True
    reconnect_interval: int = 10          # seconds between reconnect attempts
    session_key: str = "vera-bridge"      # OpenClaw session key to use

_CONFIG = OpenClawConfig(
    enabled=os.environ.get("OPENCLAW_ENABLED", "0") == "1",
    ws_url=os.environ.get("OPENCLAW_WS_URL", "ws://localhost:18789"),
    token=os.environ.get("OPENCLAW_TOKEN", ""),
    agent_id=os.environ.get("OPENCLAW_AGENT_ID", "main"),
    vera_base_url=os.environ.get("OPENCLAW_VERA_BASE_URL", "http://localhost:8000"),
)

@dataclass
class OpenClawState:
    connected: bool = False
    connecting: bool = False
    last_error: str = ""
    last_connected_at: str = ""
    gateway_version: str = ""
    gateway_conn_id: str = ""
    active_sessions: Dict[str, dict] = field(default_factory=dict)
    # pending responses keyed by request id
    _pending: Dict[str, asyncio.Future] = field(default_factory=dict)
    # streaming message accumulator keyed by session_key
    _stream_bufs: Dict[str, list] = field(default_factory=dict)

_STATE = OpenClawState()
_WS_CONN = None           # active websocket connection
_WS_TASK: Optional[asyncio.Task] = None
_DEVICE_ID = f"vera-bridge-{uuid.uuid4().hex[:8]}"

# ══════════════════════════════════════════════════════════════════════════════
# OpenClaw WebSocket client
# ══════════════════════════════════════════════════════════════════════════════

async def _send(msg: dict) -> None:
    """Send a JSON frame to the OpenClaw gateway."""
    global _WS_CONN
    if _WS_CONN is None:
        raise RuntimeError("Not connected to OpenClaw gateway")
    await _WS_CONN.send(json.dumps(msg))


async def _rpc(method: str, params: dict, timeout: float = 30.0) -> dict:
    """Send a request and wait for the matching response."""
    req_id = uuid.uuid4().hex
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _STATE._pending[req_id] = fut
    try:
        await _send({"type": "req", "id": req_id, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        _STATE._pending.pop(req_id, None)


async def _ws_reader(ws) -> None:
    """Background task: read frames from OpenClaw and dispatch."""
    global _STATE
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            mtype = msg.get("type")

            # Resolve pending RPC futures
            if mtype == "res":
                req_id = msg.get("id")
                if req_id and req_id in _STATE._pending:
                    fut = _STATE._pending[req_id]
                    if not fut.done():
                        if msg.get("ok"):
                            fut.set_result(msg.get("payload", {}))
                        else:
                            err = msg.get("error", {})
                            fut.set_exception(
                                RuntimeError(err.get("message", "OpenClaw RPC error"))
                            )
                continue

            if mtype == "event":
                event = msg.get("event", "")
                payload = msg.get("payload", {})

                # Stream agent output tokens back into Vera's event bus
                if event in ("agent", "chat"):
                    session_key = payload.get("sessionKey", _CONFIG.session_key)
                    delta = payload.get("delta", "") or payload.get("text", "")
                    done = payload.get("done", False)

                    if session_key not in _STATE._stream_bufs:
                        _STATE._stream_bufs[session_key] = []

                    if delta:
                        _STATE._stream_bufs[session_key].append(delta)
                        await emit_event("openclaw.stream", {
                            "session_key": session_key,
                            "delta": delta,
                            "done": False,
                            "ts": now_iso(),
                        })

                    if done:
                        full_text = "".join(_STATE._stream_bufs.pop(session_key, []))
                        await emit_event("openclaw.response", {
                            "session_key": session_key,
                            "text": full_text,
                            "done": True,
                            "ts": now_iso(),
                        })

                    continue

                # Forward other notable events to Vera's event bus
                if event in ("sessions.changed", "health", "presence"):
                    await emit_event(f"openclaw.gw.{event}", {"payload": payload, "ts": now_iso()})

    except Exception as exc:
        log.warning("openclaw ws reader: %s", exc)
    finally:
        _STATE.connected = False
        await emit_event("openclaw.disconnected", {"ts": now_iso()})
        log.info("openclaw: WS reader exited")


async def _do_connect() -> None:
    """Establish a WS connection and perform the OpenClaw protocol v3 handshake."""
    global _WS_CONN, _STATE

    if not HAS_WS:
        _STATE.last_error = "websockets package not installed"
        return

    _STATE.connecting = True
    _STATE.last_error = ""

    try:
        log.info("openclaw: connecting to %s", _CONFIG.ws_url)
        ws = await websockets.connect(
            _CONFIG.ws_url,
            open_timeout=10,
            ping_interval=15,
            ping_timeout=30,
        )
        _WS_CONN = ws

        # Wait for the connect.challenge event
        challenge_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        challenge_msg = json.loads(challenge_raw)
        nonce = challenge_msg.get("payload", {}).get("nonce", "")

        # Send connect request — we use the "operator" role with read+write scopes
        req_id = uuid.uuid4().hex
        connect_params = {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "vera-bridge",
                "version": "1.0.0",
                "platform": "linux",
                "mode": "operator",
            },
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {"token": _CONFIG.token},
            "locale": "en-US",
            "userAgent": "vera-openclaw-bridge/1.0.0",
            "device": {
                "id": _DEVICE_ID,
                "publicKey": "",
                "signature": "",
                "signedAt": int(time.time() * 1000),
                "nonce": nonce,
            },
        }
        await ws.send(json.dumps({"type": "req", "id": req_id, "method": "connect", "params": connect_params}))

        # Read hello-ok (or error)
        hello_raw = await asyncio.wait_for(ws.recv(), timeout=15)
        hello = json.loads(hello_raw)

        if hello.get("type") != "res" or not hello.get("ok"):
            err_msg = hello.get("error", {}).get("message", "handshake failed")
            raise RuntimeError(f"OpenClaw handshake error: {err_msg}")

        payload = hello.get("payload", {})
        _STATE.connected = True
        _STATE.connecting = False
        _STATE.last_connected_at = now_iso()
        _STATE.gateway_version = payload.get("server", {}).get("version", "")
        _STATE.gateway_conn_id = payload.get("server", {}).get("connId", "")

        await emit_event("openclaw.connected", {
            "gateway_version": _STATE.gateway_version,
            "conn_id": _STATE.gateway_conn_id,
            "ts": now_iso(),
        })
        log.info("openclaw: connected (gateway %s)", _STATE.gateway_version)

        # Start the reader (blocks until disconnected)
        await _ws_reader(ws)

    except Exception as exc:
        _STATE.last_error = str(exc)
        _STATE.connecting = False
        _STATE.connected = False
        _WS_CONN = None
        log.warning("openclaw: connection failed: %s", exc)
        await emit_event("openclaw.error", {"error": str(exc), "ts": now_iso()})


async def _reconnect_loop() -> None:
    """Background task: keep the OpenClaw connection alive."""
    while True:
        if _CONFIG.enabled and not _STATE.connected and not _STATE.connecting:
            await _do_connect()
        if _CONFIG.enabled and not _STATE.connected:
            await asyncio.sleep(_CONFIG.reconnect_interval)
        else:
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
# Vera capability bridge — let OpenClaw call Vera capabilities as HTTP tools
# ══════════════════════════════════════════════════════════════════════════════

from fastapi.responses import JSONResponse as _JSONResponse
from fastapi import Request as _Request

@APP.get("/openclaw/tools")
async def _oc_tools_list():
    """Expose Vera capabilities as OpenClaw-compatible tool schemas."""
    tools = []
    for name, cap in _orch.CAPABILITY_REGISTRY.items():
        schema = cap.get("schema", {})
        tools.append({
            "name": name,
            "description": cap.get("doc", "")[:200],
            "parameters": {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            },
        })
    return {"tools": tools}


@APP.post("/openclaw/call/{capability_name}")
async def _oc_tool_call(capability_name: str, request: _Request):
    """Execute a Vera capability on behalf of OpenClaw."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    cap = _orch.CAPABILITY_REGISTRY.get(capability_name)
    if not cap:
        return _JSONResponse({"error": f"Capability '{capability_name}' not found"}, status_code=404)

    try:
        fn = cap["fn"]
        result = await fn(**body) if asyncio.iscoroutinefunction(fn) else fn(**body)
        return {"result": result, "capability": capability_name}
    except Exception as exc:
        return _JSONResponse({"error": str(exc)}, status_code=500)


# ══════════════════════════════════════════════════════════════════════════════
# Capabilities
# ══════════════════════════════════════════════════════════════════════════════

@capability(
    name="openclaw.status",
    description="Get the current status of the OpenClaw gateway connection",
    http_method="GET",
    http_path="/openclaw/status",
)
async def openclaw_status() -> dict:
    return {
        "enabled": _CONFIG.enabled,
        "connected": _STATE.connected,
        "connecting": _STATE.connecting,
        "last_error": _STATE.last_error,
        "last_connected_at": _STATE.last_connected_at,
        "gateway_version": _STATE.gateway_version,
        "gateway_conn_id": _STATE.gateway_conn_id,
        "ws_url": _CONFIG.ws_url,
        "agent_id": _CONFIG.agent_id,
        "session_key": _CONFIG.session_key,
        "active_sessions": list(_STATE.active_sessions.keys()),
        "has_websockets_lib": HAS_WS,
    }


@capability(
    name="openclaw.config.get",
    description="Get the current OpenClaw connection configuration",
    http_method="GET",
    http_path="/openclaw/config",
)
async def openclaw_config_get() -> dict:
    d = asdict(_CONFIG)
    d.pop("token", None)   # never expose token over API
    return d


@capability(
    name="openclaw.config.set",
    description="Update OpenClaw connection configuration",
    http_method="POST",
    http_path="/openclaw/config",
    schema={
        "properties": {
            "enabled":           {"type": "boolean"},
            "ws_url":            {"type": "string"},
            "token":             {"type": "string"},
            "agent_id":          {"type": "string"},
            "vera_base_url":     {"type": "string"},
            "auto_reconnect":    {"type": "boolean"},
            "reconnect_interval":{"type": "integer"},
            "session_key":       {"type": "string"},
        }
    },
)
async def openclaw_config_set(**kwargs) -> dict:
    for k, v in kwargs.items():
        if hasattr(_CONFIG, k):
            setattr(_CONFIG, k, v)
    await emit_event("openclaw.config.changed", {"ts": now_iso()})
    return {"ok": True, "config": asdict(_CONFIG) | {"token": "***"}}


@capability(
    name="openclaw.connect",
    description="Connect (or reconnect) to the OpenClaw gateway",
    http_method="POST",
    http_path="/openclaw/connect",
)
async def openclaw_connect() -> dict:
    global _WS_TASK, _STATE, _WS_CONN

    if not HAS_WS:
        return {"ok": False, "error": "websockets package not installed — pip install websockets"}

    if _STATE.connected:
        return {"ok": True, "status": "already_connected", "gateway_version": _STATE.gateway_version}

    if _STATE.connecting:
        return {"ok": True, "status": "connecting"}

    _CONFIG.enabled = True

    # Cancel old task if any
    if _WS_TASK and not _WS_TASK.done():
        _WS_TASK.cancel()
        with contextlib.suppress(Exception):
            await _WS_TASK

    _WS_TASK = asyncio.create_task(_reconnect_loop(), name="openclaw_reconnect")
    return {"ok": True, "status": "connecting", "ws_url": _CONFIG.ws_url}


@capability(
    name="openclaw.disconnect",
    description="Disconnect from the OpenClaw gateway",
    http_method="POST",
    http_path="/openclaw/disconnect",
)
async def openclaw_disconnect() -> dict:
    global _WS_CONN, _STATE

    _CONFIG.enabled = False
    _STATE.connected = False
    _STATE.connecting = False

    if _WS_CONN:
        try:
            await _WS_CONN.close()
        except Exception:
            pass
        _WS_CONN = None

    await emit_event("openclaw.disconnected", {"manual": True, "ts": now_iso()})
    return {"ok": True, "status": "disconnected"}


@capability(
    name="openclaw.prompt",
    description="Send a prompt to OpenClaw's agentic loop and receive the response",
    http_method="POST",
    http_path="/openclaw/prompt",
    schema={
        "properties": {
            "message":     {"type": "string", "description": "The prompt to send"},
            "session_key": {"type": "string", "description": "OpenClaw session key (default: vera-bridge)"},
            "agent_id":    {"type": "string", "description": "OpenClaw agent ID (default from config)"},
            "thinking":    {"type": "string", "enum": ["low", "medium", "high"], "description": "Thinking level"},
        },
        "required": ["message"],
    },
)
async def openclaw_prompt(
    message: str,
    session_key: str = None,
    agent_id: str = None,
    thinking: str = None,
) -> dict:
    if not _STATE.connected:
        return {"ok": False, "error": "Not connected to OpenClaw gateway. Call openclaw.connect first."}

    session_key = session_key or _CONFIG.session_key
    agent_id = agent_id or _CONFIG.agent_id

    # Build the chat.send params per OpenClaw protocol
    params: dict = {
        "sessionKey": session_key,
        "agentId": agent_id,
        "message": message,
    }
    if thinking:
        params["thinking"] = thinking

    # Clear any existing stream buffer for this session
    _STATE._stream_bufs[session_key] = []

    try:
        result = await _rpc("chat.send", params, timeout=120)
        return {
            "ok": True,
            "session_key": session_key,
            "run_id": result.get("runId"),
            "status": "streaming",
            "note": "Response is streaming via openclaw.stream events",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@capability(
    name="openclaw.sessions.list",
    description="List active OpenClaw sessions",
    http_method="GET",
    http_path="/openclaw/sessions",
)
async def openclaw_sessions_list() -> dict:
    if not _STATE.connected:
        return {"ok": False, "error": "Not connected", "sessions": []}
    try:
        result = await _rpc("sessions.list", {}, timeout=10)
        sessions = result.get("sessions", [])
        _STATE.active_sessions = {s.get("key", s.get("id", "")): s for s in sessions}
        return {"ok": True, "sessions": sessions}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "sessions": []}


@capability(
    name="openclaw.sessions.reset",
    description="Reset (clear history of) an OpenClaw session",
    http_method="POST",
    http_path="/openclaw/sessions/reset",
    schema={
        "properties": {
            "session_key": {"type": "string", "description": "Session key to reset"},
        },
        "required": ["session_key"],
    },
)
async def openclaw_sessions_reset(session_key: str) -> dict:
    if not _STATE.connected:
        return {"ok": False, "error": "Not connected"}
    try:
        result = await _rpc("sessions.reset", {"sessionKey": session_key}, timeout=10)
        return {"ok": True, "session_key": session_key, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# UI Panel  — iframe-mount pattern (scripts execute correctly in own window)
# ══════════════════════════════════════════════════════════════════════════════
 
import contextlib
from pathlib import Path as _Path
from fastapi.responses import HTMLResponse as _HTMLResp
 
_PANEL_HTML_PATH = _Path(__file__).parent / "openclaw_panel.html"
 
@APP.get("/openclaw/panel", response_class=_HTMLResp)
async def _openclaw_panel_html():
    """Serve the OpenClaw panel as a standalone page (for iframe embedding)."""
    if _PANEL_HTML_PATH.exists():
        return _HTMLResp(content=_PANEL_HTML_PATH.read_text(encoding="utf-8"))
    return _HTMLResp(
        content="<h2 style='font-family:monospace;color:#c96b6b'>openclaw_panel.html not found</h2>",
        status_code=404,
    )
 
 
# JS that runs in the harness window — just mounts an iframe pointing at the route above.
# The iframe has its own window so DOMContentLoaded fires, addEventListener works,
# and no scripts are injected via innerHTML.
try:
    _PANEL_HTML = (
        __file__.replace("openclaw_capabilities.py", "openclaw_panel.html")
    )
    with open(_PANEL_HTML, encoding="utf-8") as _fh:
        _HTML = _fh.read()
except Exception as _e:
    _HTML

_OPENCLAW_MOUNT_JS = r"""
(function mountOpenclawPanel() {
  var mount = document.getElementById('panel-auto-openclaw');
  if (!mount) return;
  if (mount._openclawMounted) return;
  mount._openclawMounted = true;
 
  var base = (document.getElementById('urlInput')?.value
    || localStorage.getItem('vera_base')
    || window.location.origin).replace(/\/$/, '');
 
  var frame = document.createElement('iframe');
  frame.src = base + '/openclaw/panel';
  frame.style.cssText = 'width:100%;height:100%;border:none;display:block;';
  mount.appendChild(frame);
 
  // If the harness URL input changes, reload the iframe against the new base
  var urlInput = document.getElementById('urlInput');
  if (urlInput) {
    urlInput.addEventListener('change', function() {
      var newBase = urlInput.value.replace(/\/$/, '');
      frame.src = newBase + '/openclaw/panel';
    });
  }
})();
"""
 
register_ui(
    panel_id="openclaw",
    label="OpenClaw",
    icon="",
    # Minimal mount point — the iframe is created by the JS above
    html=_HTML,
    js=_OPENCLAW_MOUNT_JS,
    mode="tab",
    tab_order=55,
    ui_caps=[
        "openclaw.status",
        "openclaw.connect",
        "openclaw.disconnect",
        "openclaw.prompt",
        "openclaw.config.get",
        "openclaw.config.set",
        "openclaw.sessions.list",
        "openclaw.sessions.reset",
    ],
)

# ══════════════════════════════════════════════════════════════════════════════
# Auto-start
# ══════════════════════════════════════════════════════════════════════════════

async def _maybe_autostart():
    """If OPENCLAW_ENABLED=1, start the reconnect loop at startup."""
    await asyncio.sleep(3)   # let Vera finish startup
    if _CONFIG.enabled:
        log.info("openclaw: auto-starting (OPENCLAW_ENABLED=1)")
        global _WS_TASK
        _WS_TASK = asyncio.create_task(_reconnect_loop(), name="openclaw_reconnect")

schedule(_maybe_autostart, interval=0)

"""
openclaw_extras.py  —  Extension capabilities for OpenClaw / Vera integration
==============================================================================
Drop this alongside openclaw_capabilities.py (or append to it).

New capabilities:
  openclaw.ollama-models    — list models available on Vera's Ollama proxy
  openclaw.install.discover — find openclaw.json on disk via npm paths
  openclaw.install.write    — write Vera provider + tool bridge into openclaw.json

Config additions (new fields on OpenClawConfig):
  use_vera_ollama: bool     — route OpenClaw LLM calls through Vera's /ollama proxy
  vera_ollama_base: str     — base URL for Vera's Ollama proxy  (http://host:8999/ollama)
  ollama_model: str         — model to advertise/default when using proxy

These are intended to be merged into openclaw_capabilities.py. They are kept
separate here so the patch is reviewable in isolation.
"""


import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import JSONResponse as _JSONResponse

# ── Import from the main openclaw module ─────────────────────────────────────
import Vera.Orchestration.capability_orchestration as _orch
from Vera.Orchestration.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
)
# Pull shared config/state objects from the main module
from Vera.Orchestration.openclaw_capabilities import (   # noqa: E402
    _CONFIG,
    _STATE,
)
from Vera.Orchestration.config import cfg

log = logging.getLogger("openclaw.extras")

# ══════════════════════════════════════════════════════════════════════════════
# Config extensions
# (Monkey-patch new fields onto the existing OpenClawConfig instance at runtime)
# ══════════════════════════════════════════════════════════════════════════════

def _patch_config():
    """Add new fields to the shared _CONFIG instance if not already present."""
    if not hasattr(_CONFIG, "use_vera_ollama"):
        object.__setattr__(_CONFIG, "use_vera_ollama",
                           os.environ.get("OPENCLAW_USE_VERA_OLLAMA", "0") == "1")
    if not hasattr(_CONFIG, "vera_ollama_base"):
        default_ollama = f"http://{cfg.BACKEND_HOST}:{cfg.ORCHESTRATOR_PORT}/ollama"
        object.__setattr__(_CONFIG, "vera_ollama_base",
                           os.environ.get("OPENCLAW_VERA_OLLAMA_BASE", default_ollama))
    if not hasattr(_CONFIG, "ollama_model"):
        object.__setattr__(_CONFIG, "ollama_model",
                           os.environ.get("OPENCLAW_OLLAMA_MODEL", cfg.OLLAMA_MODEL))

_patch_config()

# ══════════════════════════════════════════════════════════════════════════════
# config.set extension — accept new fields
# ══════════════════════════════════════════════════════════════════════════════

_EXTRA_CONFIG_FIELDS = {"use_vera_ollama", "vera_ollama_base", "ollama_model"}

# Wrap the existing config-set endpoint's handler to also handle extra fields.
# The original capability already handles arbitrary setattr; we just need the
# fields to exist on _CONFIG (done above) and be documented.

# ══════════════════════════════════════════════════════════════════════════════
# openclaw.ollama-models  — list models on Vera's Ollama proxy
# ══════════════════════════════════════════════════════════════════════════════

@capability(
    name="openclaw.ollama-models",
    description="List models available on Vera's Ollama proxy cluster",
    http_method="GET",
    http_path="/openclaw/ollama-models",
)
async def openclaw_ollama_models() -> dict:
    """
    Hit Vera's own /ollama/api/tags endpoint to enumerate available models.
    Falls back to querying each OLLAMA_INSTANCES node directly.
    """
    vera_ollama = getattr(_CONFIG, "vera_ollama_base",
                          f"http://localhost:{cfg.ORCHESTRATOR_PORT}/ollama")

    # Try the proxy first
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{vera_ollama}/api/tags")
            if r.status_code == 200:
                data = r.json()
                models = [m["name"] for m in data.get("models", [])]
                return {
                    "ok": True,
                    "models": models,
                    "source": "vera_proxy",
                    "proxy_url": vera_ollama,
                }
    except Exception as exc:
        log.debug("openclaw.ollama-models: proxy failed (%s), trying direct", exc)

    # Fallback — poll OLLAMA_INSTANCES directly
    models: list[str] = []
    seen: set[str] = set()
    instances = getattr(_orch, "OLLAMA_INSTANCES", {})
    async with httpx.AsyncClient(timeout=5) as client:
        for iid, inst in instances.items():
            url = inst.get("url", "")
            if not url:
                continue
            try:
                r = await client.get(f"{url}/api/tags")
                if r.status_code == 200:
                    for m in r.json().get("models", []):
                        name = m.get("name", "")
                        if name and name not in seen:
                            seen.add(name)
                            models.append(name)
            except Exception:
                pass

    if models:
        return {"ok": True, "models": sorted(models), "source": "direct_nodes"}
    return {"ok": False, "models": [], "error": "No models found — Ollama may be unreachable"}


# ══════════════════════════════════════════════════════════════════════════════
# Install helpers
# ══════════════════════════════════════════════════════════════════════════════

def _candidate_paths() -> list[Path]:
    """Return ordered list of candidate openclaw.json paths."""
    candidates: list[Path] = []

    # 1. npm global prefix
    npm = shutil.which("npm")
    if npm:
        try:
            result = subprocess.run(
                [npm, "root", "-g"],
                capture_output=True, text=True, timeout=8
            )
            if result.returncode == 0:
                npm_root = Path(result.stdout.strip())
                candidates.append(npm_root / "openclaw" / "openclaw.json")
                candidates.append(npm_root.parent / "etc" / "openclaw.json")
                candidates.append(npm_root.parent / "openclaw.json")
        except Exception:
            pass

    # 2. Common user config paths
    home = Path.home()
    candidates += [
        home / ".config" / "openclaw" / "openclaw.json",
        home / ".openclaw" / "openclaw.json",
        home / "openclaw.json",
        Path("/etc/openclaw/openclaw.json"),
        Path("/usr/local/etc/openclaw/openclaw.json"),
        Path("/opt/openclaw/openclaw.json"),
    ]

    # 3. Working directory
    candidates.append(Path.cwd() / "openclaw.json")

    return candidates


@capability(
    name="openclaw.install.discover",
    description="Discover the openclaw.json config file location via npm paths and common directories",
    http_method="GET",
    http_path="/openclaw/install/discover",
)
async def openclaw_install_discover() -> dict:
    """Search for openclaw.json on disk."""
    searched: list[str] = []
    for candidate in _candidate_paths():
        searched.append(str(candidate))
        if candidate.exists():
            return {
                "ok": True,
                "path": str(candidate),
                "searched": searched,
            }

    return {
        "ok": False,
        "path": None,
        "searched": searched,
        "error": "openclaw.json not found in any standard location. "
                 "Install OpenClaw via npm or provide the path manually.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# openclaw.install.write
# ══════════════════════════════════════════════════════════════════════════════

def _build_vera_snippet(vera_base: str, ollama: bool, tools: bool, model: str) -> dict:
    """Build the Vera sub-keys to merge into openclaw.json."""
    snippet: dict = {}

    if ollama:
        # OpenClaw uses the 'ollama' block to point at an Ollama-compatible API.
        # Vera's /ollama proxy is fully Ollama-API-compatible.
        snippet["ollama"] = {
            "baseUrl": f"{vera_base}/ollama",
            "model": model or "jaahas/qwen3.5-uncensored",
            "_vera": "Vera Ollama cluster proxy — managed by Vera",
        }

    if tools:
        # OpenClaw's tool bridge — a list of HTTP tool sources.
        # The schema follows OpenClaw's skill/tool config convention.
        snippet["skills"] = {
            "vera": {
                "url": f"{vera_base}/openclaw/tools",
                "callUrl": f"{vera_base}/openclaw/call",
                "description": "Vera capability bridge — all Vera capabilities as tools",
                "enabled": True,
                "_vera": "Auto-registered by Vera",
            }
        }

    snippet["_vera_bridge"] = {
        "veraBaseUrl": vera_base,
        "wsUrl": f"{vera_base.replace('http', 'ws')}/openclaw/ws-proxy",
        "installedAt": now_iso(),
        "version": "1.0",
    }

    return snippet


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base (overlay wins on conflicts)."""
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


@capability(
    name="openclaw.install.write",
    description="Write Vera provider and tool bridge configuration into openclaw.json",
    http_method="POST",
    http_path="/openclaw/install/write",
    schema={
        "properties": {
            "config_path":         {"type": "string", "description": "Absolute path to openclaw.json"},
            "vera_base_url":       {"type": "string", "description": "Vera HTTP base URL (e.g. http://host:8999)"},
            "set_ollama_provider": {"type": "boolean", "description": "Register Vera as the Ollama provider"},
            "set_tool_bridge":     {"type": "boolean", "description": "Register Vera tool bridge in skills"},
            "ollama_model":        {"type": "string",  "description": "Default model to advertise"},
        },
        "required": ["config_path"],
    },
)
async def openclaw_install_write(
    config_path: str,
    vera_base_url: str = None,
    set_ollama_provider: bool = True,
    set_tool_bridge: bool = True,
    ollama_model: str = None,
) -> dict:
    """
    Merge Vera configuration into an existing openclaw.json.
    Creates the file (with empty base {}) if it does not exist.
    A backup is written to <path>.vera-bak before modification.
    """
    path = Path(config_path)
    vera_base = (vera_base_url or _CONFIG.vera_base_url or
                 f"http://localhost:{cfg.ORCHESTRATOR_PORT}").rstrip("/")
    model = ollama_model or getattr(_CONFIG, "ollama_model", cfg.OLLAMA_MODEL) or "jaahas/qwen3.5-uncensored"

    # Read existing config
    existing: dict = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as exc:
            return {"ok": False, "error": f"Could not read {path}: {exc}"}
    else:
        # Ensure parent dir exists
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"ok": False, "error": f"Cannot create directory {path.parent}: {exc}"}

    # Build snippet
    snippet = _build_vera_snippet(vera_base, set_ollama_provider, set_tool_bridge, model)

    # Merge
    merged = _deep_merge(existing, snippet)

    # Backup
    if path.exists():
        bak = path.with_suffix(".json.vera-bak")
        try:
            bak.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass   # backup failure is non-fatal

    # Write
    try:
        path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Write failed: {exc}",
            "snippet": snippet,
        }

    wrote = []
    if set_ollama_provider: wrote.append("ollama provider")
    if set_tool_bridge:     wrote.append("skill/tool bridge")
    wrote.append("_vera_bridge metadata")

    await emit_event("openclaw.installed", {
        "config_path": str(path),
        "vera_base_url": vera_base,
        "wrote": wrote,
        "ts": now_iso(),
    })

    log.info("openclaw: wrote Vera config to %s (sections: %s)", path, wrote)

    return {
        "ok": True,
        "message": f"Vera registered in {path} — sections: {', '.join(wrote)}",
        "config_path": str(path),
        "wrote": wrote,
        "snippet": snippet,
        "backup": str(path.with_suffix(".json.vera-bak")) if path.exists() else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Ollama-transparent status extension
# (Adds use_vera_ollama / vera_ollama_base to openclaw.status response)
# ══════════════════════════════════════════════════════════════════════════════

@APP.get("/openclaw/status/extended")
async def _oc_status_extended():
    """Extended status including Ollama proxy routing info."""
    from Vera.Orchestration.openclaw_capabilities import openclaw_status
    base_status = await openclaw_status()
    base_status["use_vera_ollama"]  = getattr(_CONFIG, "use_vera_ollama", False)
    base_status["vera_ollama_base"] = getattr(_CONFIG, "vera_ollama_base", "")
    base_status["ollama_model"]     = getattr(_CONFIG, "ollama_model", "")
    return base_status