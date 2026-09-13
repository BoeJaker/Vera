"""
n8n_capabilities.py — Vera ↔ n8n integration
============================================

Wires a self-hosted n8n instance into Vera in both directions:

  Vera → n8n   `n8n.workflow.*` drives n8n's public REST API (`/api/v1`), so
               Vera can list, author, activate and inspect workflows.

  n8n → Vera   `n8n.mcp.connect` opens a **real MCP session** (see
               `vera/mcp/mcp_client.py`) against an n8n *MCP Server Trigger* and
               registers every tool it exposes as a live Vera capability named
               `n8n.<tool>`. Because Vera DAG nodes are just capability names,
               those workflows become usable DAG nodes the moment they are
               registered — that is the whole interop story, and it needs no
               DAG-format translation.

  Vera DAG → n8n  `n8n.dag.export` converts a Vera DAG into an importable n8n
               workflow whose nodes call back into Vera's own `/mcp/call`.

The reverse direction (n8n workflow → Vera DAG) is deliberately *not* a
node-by-node translation; see `n8n_core.py` for why.

Config (Redis hash `vera:n8n:config`, secrets sealed with vera/security/secrets)
────────────────────────────────────────────────────────────────────────────────
  base_url    e.g. https://n8n.corp.local   (also the webhook/MCP URL base)
  api_key     n8n public-API key            (sealed)
  mcp_path    the MCP Server Trigger's path parameter
  mcp_token   bearer token if the trigger uses Bearer Auth (sealed)
  verify_tls  False for an internal-CA cert
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, new_id, now_iso,
    register_ui,
)
from Vera.vera.mcp.mcp_client import MCPClient, MCPError, probe as mcp_probe
from Vera.vera.mcp import mcp_client_core as mcc
from Vera.vera.n8n import n8n_core as core
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.n8n")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "n8n_panel.html"

KEY_CONFIG = "vera:n8n:config"
KEY_TOOLS = "vera:n8n:registered_tools"

_SECRET_FIELDS = ("api_key", "mcp_token")

DEFAULTS: Dict[str, Any] = {
    "base_url": "",
    "api_key": "",
    "mcp_path": "",
    "mcp_token": "",
    "verify_tls": False,
    "auto_connect": True,
    "updated": "",
}


def _redis():
    return getattr(_orch, "REDIS", None)


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════

async def _load_config(open_secrets: bool = True) -> Dict[str, Any]:
    r = _redis()
    cfg = dict(DEFAULTS)
    if not r:
        return cfg
    raw = await r.hgetall(KEY_CONFIG)
    for k, v in (raw or {}).items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        if key in ("verify_tls", "auto_connect"):
            cfg[key] = str(val).lower() in ("1", "true", "yes", "on")
        else:
            cfg[key] = val
    if open_secrets:
        for f in _SECRET_FIELDS:
            if cfg.get(f):
                try:
                    cfg[f] = vsecrets.open_secret(cfg[f])
                except Exception:
                    log.warning("n8n: could not unseal %s", f)
                    cfg[f] = ""
    return cfg


def _redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    for f in _SECRET_FIELDS:
        out[f] = "••••••••" if cfg.get(f) else ""
    return out


def _mcp_url(cfg: Dict[str, Any], test: bool = False) -> str:
    if not cfg.get("base_url") or not cfg.get("mcp_path"):
        return ""
    urls = core.mcp_urls(cfg["base_url"], cfg["mcp_path"])
    return urls["test"] if test else urls["production"]


@capability(
    "n8n.config.get", http_method="GET", http_path="/n8n/config",
    http_tags=["n8n"], memory="off",
    description="Read the n8n connection config (secrets redacted). "
                "Output: {base_url, api_key, mcp_path, mcp_url, verify_tls, configured}.",
)
async def cap_config_get(trace_id=None):
    cfg = await _load_config()
    out = _redact(cfg)
    out["mcp_url"] = _mcp_url(cfg)
    out["mcp_test_url"] = _mcp_url(cfg, test=True)
    out["configured"] = bool(cfg.get("base_url"))
    out["api_configured"] = bool(cfg.get("base_url") and cfg.get("api_key"))
    out["mcp_configured"] = bool(_mcp_url(cfg))
    return out


@capability(
    "n8n.config.set", http_method="POST", http_path="/n8n/config",
    http_tags=["n8n"], memory="on",
    description="Set the n8n connection config. Secrets are sealed at rest and "
                "an empty string leaves an existing secret unchanged. "
                "Input: base_url (str), api_key (str), mcp_path (str), "
                "mcp_token (str), verify_tls (bool), auto_connect (bool). "
                "Output: {ok, config}.",
)
async def cap_config_set(base_url: str = "", api_key: str = "", mcp_path: str = "",
                         mcp_token: str = "", verify_tls: Optional[bool] = None,
                         auto_connect: Optional[bool] = None, trace_id=None):
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    cur = await _load_config()
    if base_url:
        cur["base_url"] = base_url.rstrip("/")
    if mcp_path:
        cur["mcp_path"] = mcp_path.strip("/")
    # Empty means "keep what's stored" — otherwise every partial save from the
    # panel would silently wipe the credentials it doesn't re-send.
    if api_key:
        cur["api_key"] = api_key
    if mcp_token:
        cur["mcp_token"] = mcp_token
    if verify_tls is not None:
        cur["verify_tls"] = bool(verify_tls)
    if auto_connect is not None:
        cur["auto_connect"] = bool(auto_connect)
    cur["updated"] = now_iso()

    stored = dict(cur)
    for f in _SECRET_FIELDS:
        if stored.get(f):
            stored[f] = vsecrets.seal(stored[f])
    await r.hset(KEY_CONFIG, mapping={
        k: (json.dumps(v) if isinstance(v, (dict, list))
            else ("1" if v is True else "0" if v is False else str(v)))
        for k, v in stored.items()})
    out = _redact(cur)
    out["mcp_url"] = _mcp_url(cur)
    return {"ok": True, "config": out}


# ═════════════════════════════════════════════════════════════════════════════
#  n8n PUBLIC REST API  (Vera → n8n)
# ═════════════════════════════════════════════════════════════════════════════

async def _api(method: str, path: str, *, params: Optional[Dict] = None,
               body: Optional[Dict] = None) -> Any:
    cfg = await _load_config()
    if not cfg.get("base_url"):
        raise RuntimeError("n8n base_url not configured — call n8n.config.set")
    if not cfg.get("api_key"):
        raise RuntimeError("n8n api_key not configured — call n8n.config.set")
    url = core.join_url(cfg["base_url"], "api/v1", path)
    headers = {"X-N8N-API-KEY": cfg["api_key"], "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(verify=bool(cfg.get("verify_tls")),
                                 timeout=45, follow_redirects=True) as c:
        r = await c.request(method, url, headers=headers, params=params,
                            content=json.dumps(body) if body is not None else None)
    if r.status_code >= 400:
        raise RuntimeError(f"n8n API {method} {path} → HTTP {r.status_code}: "
                           f"{r.text[:400]}")
    if not r.text.strip():
        return {}
    return r.json()


@capability(
    "n8n.workflow.list", http_method="GET", http_path="/n8n/workflows",
    http_tags=["n8n"], memory="off",
    description="List n8n workflows with their triggers and live webhook/MCP "
                "URLs. Input: active_only (bool), limit (int=100). "
                "Output: {workflows:[{id,name,active,triggers,...}], count}.",
)
async def cap_workflow_list(active_only: bool = False, limit: int = 100,
                            trace_id=None):
    try:
        cfg = await _load_config()
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 100), 250))}
        if active_only:
            params["active"] = "true"
        data = await _api("GET", "workflows", params=params)
        items = data.get("data") or []
        return {"workflows": [core.workflow_summary(w, cfg.get("base_url", ""))
                              for w in items],
                "count": len(items)}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "n8n.workflow.get", http_method="GET", http_path="/n8n/workflow",
    http_tags=["n8n"], memory="off",
    description="Fetch one n8n workflow. Input: id (str!), full (bool — include "
                "the raw node graph). Output: {workflow, raw?}.",
)
async def cap_workflow_get(id: str = "", full: bool = False, trace_id=None):
    if not id:
        return {"error": "id required"}
    try:
        cfg = await _load_config()
        wf = await _api("GET", f"workflows/{id}")
        wf = wf.get("data", wf)
        out = {"workflow": core.workflow_summary(wf, cfg.get("base_url", ""))}
        if full:
            out["raw"] = wf
        return out
    except Exception as e:
        return {"error": str(e)}


@capability(
    "n8n.workflow.create", http_method="POST", http_path="/n8n/workflow/create",
    http_tags=["n8n"], memory="on",
    description="Create an n8n workflow from a node graph. Input: name (str!), "
                "nodes (list!), connections (dict), settings (dict), "
                "activate (bool). Output: {ok, id, workflow}.",
)
async def cap_workflow_create(name: str = "", nodes: Optional[List] = None,
                              connections: Optional[Dict] = None,
                              settings: Optional[Dict] = None,
                              activate: bool = False, trace_id=None):
    if not name or not nodes:
        return {"error": "name and nodes are required"}
    try:
        body = {"name": name, "nodes": nodes,
                "connections": connections or {},
                "settings": settings or {"executionOrder": "v1"}}
        wf = await _api("POST", "workflows", body=body)
        wf = wf.get("data", wf)
        wid = wf.get("id", "")
        if activate and wid:
            await _api("POST", f"workflows/{wid}/activate")
            wf["active"] = True
        await emit_event({"type": "n8n.workflow.created", "id": wid, "name": name})
        cfg = await _load_config()
        return {"ok": True, "id": wid,
                "workflow": core.workflow_summary(wf, cfg.get("base_url", ""))}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "n8n.workflow.activate", http_method="POST", http_path="/n8n/workflow/activate",
    http_tags=["n8n"], memory="on",
    description="Activate or deactivate an n8n workflow — an inactive workflow's "
                "production webhook/MCP URL returns 404. Input: id (str!), "
                "active (bool=True). Output: {ok, id, active}.",
)
async def cap_workflow_activate(id: str = "", active: bool = True, trace_id=None):
    if not id:
        return {"error": "id required"}
    try:
        await _api("POST", f"workflows/{id}/{'activate' if active else 'deactivate'}")
        return {"ok": True, "id": id, "active": bool(active)}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "n8n.workflow.delete", http_method="POST", http_path="/n8n/workflow/delete",
    http_tags=["n8n"], memory="on",
    description="Delete an n8n workflow. Input: id (str!). Output: {ok, id}.",
)
async def cap_workflow_delete(id: str = "", trace_id=None):
    if not id:
        return {"error": "id required"}
    try:
        await _api("DELETE", f"workflows/{id}")
        return {"ok": True, "id": id}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "n8n.execution.list", http_method="GET", http_path="/n8n/executions",
    http_tags=["n8n"], memory="off",
    description="Recent n8n executions, for checking whether a triggered "
                "workflow actually ran. Input: workflow_id (str), status (str — "
                "success|error|waiting), limit (int=20). Output: {executions, count}.",
)
async def cap_execution_list(workflow_id: str = "", status: str = "",
                             limit: int = 20, trace_id=None):
    try:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 20), 100))}
        if workflow_id:
            params["workflowId"] = workflow_id
        if status:
            params["status"] = status
        data = await _api("GET", "executions", params=params)
        items = data.get("data") or []
        return {"executions": [
            {"id": e.get("id"), "workflowId": e.get("workflowId"),
             "status": e.get("status"), "mode": e.get("mode"),
             "startedAt": e.get("startedAt"), "stoppedAt": e.get("stoppedAt")}
            for e in items], "count": len(items)}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "n8n.webhook.call", http_method="POST", http_path="/n8n/webhook/call",
    http_tags=["n8n"], memory="on",
    description="Call an n8n webhook by path — the general way to trigger a "
                "workflow that has no MCP trigger. Input: path (str!), "
                "method (str=POST), body (dict), query (dict), test (bool — use "
                "the editor test URL). Output: {ok, status, response}.",
)
async def cap_webhook_call(path: str = "", method: str = "POST",
                           body: Optional[Dict] = None,
                           query: Optional[Dict] = None,
                           test: bool = False, trace_id=None):
    if not path:
        return {"error": "path required"}
    try:
        cfg = await _load_config()
        if not cfg.get("base_url"):
            return {"error": "n8n base_url not configured"}
        urls = core.webhook_urls(cfg["base_url"], path)
        url = urls["test"] if test else urls["production"]
        async with httpx.AsyncClient(verify=bool(cfg.get("verify_tls")),
                                     timeout=120, follow_redirects=True) as c:
            r = await c.request(method.upper(), url, params=query or None,
                                json=body if body is not None else None)
        try:
            payload = r.json()
        except Exception:
            payload = r.text[:4000]
        return {"ok": r.status_code < 400, "status": r.status_code,
                "url": url, "response": payload}
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
#  MCP  (n8n → Vera capabilities)
# ═════════════════════════════════════════════════════════════════════════════

async def _mcp_params(test: bool = False) -> Dict[str, Any]:
    cfg = await _load_config()
    url = _mcp_url(cfg, test=test)
    if not url:
        raise RuntimeError("MCP not configured — set base_url and mcp_path")
    return {"url": url, "token": cfg.get("mcp_token", ""),
            "verify": bool(cfg.get("verify_tls"))}


@capability(
    "n8n.mcp.probe", http_method="POST", http_path="/n8n/mcp/probe",
    http_tags=["n8n", "mcp"], memory="off",
    description="Open a real MCP session to the configured n8n MCP Server "
                "Trigger and list its tools, WITHOUT registering anything. The "
                "'does this actually work' check. Input: test (bool — use the "
                "editor test URL). Output: {ok, transport, protocol, tools, tool_count}.",
)
async def cap_mcp_probe(test: bool = False, trace_id=None):
    try:
        p = await _mcp_params(test)
        return await mcp_probe(p["url"], token=p["token"], verify=p["verify"])
    except Exception as e:
        return {"error": str(e)}


def _register_tool_cap(cap: str, tool: Dict[str, Any], conn: Dict[str, Any],
                       server_label: str) -> None:
    """Install one MCP tool into Vera's capability registry.

    A fresh MCP session is opened per call rather than holding one open: n8n
    restarts, workflow re-activations and tunnel flaps all invalidate a
    long-lived session, and a stale session fails the call rather than
    reconnecting. Connect-per-call costs a round trip and always works.
    """
    tool_name = tool.get("name", "")
    schema = mcc.tool_input_schema(tool)

    async def _proxy(_tool=tool_name, _conn=dict(conn), **kwargs):
        kwargs.pop("trace_id", None)
        try:
            async with MCPClient(_conn["url"], token=_conn.get("token", ""),
                                 verify=_conn.get("verify", False),
                                 timeout=float(_conn.get("timeout", 120))) as c:
                return await c.call_tool(_tool, kwargs)
        except MCPError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"n8n MCP call failed: {e}"}

    CAPABILITY_REGISTRY[cap] = {
        "func": _proxy, "raw": _proxy,
        "schema": schema,
        "description": mcc.tool_description(tool, server_label),
        "streams": [], "mode": "proxy", "source": "n8n_mcp",
        "server": server_label, "server_url": conn["url"],
        "tags": ["n8n", "mcp", "proxy"], "mcp_expose": True,
        "http_method": None, "http_path": None, "http_tags": ["n8n"],
    }


@capability(
    "n8n.mcp.connect", http_method="POST", http_path="/n8n/mcp/connect",
    http_tags=["n8n", "mcp"], memory="on",
    description="Connect to the n8n MCP Server Trigger and register every tool "
                "it exposes as a live Vera capability named n8n.<tool>. Those "
                "names are valid DAG nodes immediately, so n8n workflows become "
                "usable inside Vera DAGs. Input: test (bool), prefix (str=n8n). "
                "Output: {ok, registered:[cap names], count, transport}.",
)
async def cap_mcp_connect(test: bool = False, prefix: str = "n8n", trace_id=None):
    try:
        p = await _mcp_params(test)
    except Exception as e:
        return {"error": str(e)}

    try:
        async with MCPClient(p["url"], token=p["token"], verify=p["verify"]) as c:
            tools = await c.list_tools()
            transport = c.transport
            server_label = (c.server_info or {}).get("name") or "n8n"
    except Exception as e:
        return {"error": f"could not connect to n8n MCP server: {e}"}

    conn = {"url": p["url"], "token": p["token"], "verify": p["verify"]}
    registered: List[str] = []
    for t in tools:
        if not t.get("name"):
            continue
        cap = mcc.cap_name(prefix, t["name"])
        _register_tool_cap(cap, t, conn, server_label)
        registered.append(cap)

    r = _redis()
    if r:
        await r.set(KEY_TOOLS, json.dumps(registered))

    await emit_event({"type": "n8n.mcp.connected", "tools": registered,
                      "url": p["url"], "transport": transport})
    log.info("n8n MCP: registered %d tools from %s", len(registered), p["url"])
    return {"ok": True, "registered": registered, "count": len(registered),
            "transport": transport, "server": server_label, "url": p["url"]}


@capability(
    "n8n.mcp.disconnect", http_method="POST", http_path="/n8n/mcp/disconnect",
    http_tags=["n8n", "mcp"], memory="on",
    description="Remove the capabilities registered by n8n.mcp.connect. "
                "Output: {ok, removed}.",
)
async def cap_mcp_disconnect(trace_id=None):
    r = _redis()
    names: List[str] = []
    if r:
        raw = await r.get(KEY_TOOLS)
        if raw:
            try:
                names = json.loads(raw if isinstance(raw, str) else raw.decode())
            except Exception:
                names = []
    removed = 0
    for n in names:
        entry = CAPABILITY_REGISTRY.get(n)
        # Only reclaim what this module installed — never a same-named native cap.
        if entry and entry.get("source") == "n8n_mcp":
            CAPABILITY_REGISTRY.pop(n, None)
            removed += 1
    if r:
        await r.delete(KEY_TOOLS)
    return {"ok": True, "removed": removed}


# ═════════════════════════════════════════════════════════════════════════════
#  DAG INTEROP
# ═════════════════════════════════════════════════════════════════════════════

@capability(
    "n8n.dag.export", http_method="POST", http_path="/n8n/dag/export",
    http_tags=["n8n", "dag"], memory="on",
    description="Convert a Vera DAG into an n8n workflow whose nodes call back "
                "into Vera's /mcp/call, optionally creating it in n8n. Reports "
                "anything that could not be represented exactly (callable "
                "conditions, parallel re-join). Input: dag (list!), name (str!), "
                "vera_url (str), create (bool), activate (bool). "
                "Output: {ok, workflow, notes, id?}.",
)
async def cap_dag_export(dag: Optional[List] = None, name: str = "",
                         vera_url: str = "", create: bool = False,
                         activate: bool = False, trace_id=None):
    if not dag or not isinstance(dag, list):
        return {"error": "dag (list) required"}
    if not name:
        return {"error": "name required"}
    base = vera_url or getattr(_orch, "SELF_URL", "") or "https://llm.int:8999"
    built = core.vera_dag_to_n8n(dag, name, base)
    out: Dict[str, Any] = {"ok": True, "workflow": built["workflow"],
                           "notes": built["notes"],
                           "node_count": built["node_count"]}
    if create:
        res = await cap_workflow_create(
            name=name, nodes=built["workflow"]["nodes"],
            connections=built["workflow"]["connections"],
            settings=built["workflow"]["settings"], activate=activate)
        if res.get("error"):
            out["ok"] = False
            out["create_error"] = res["error"]
        else:
            out["id"] = res.get("id")
            out["created"] = True
    return out


@capability(
    "n8n.dag.node", http_method="POST", http_path="/n8n/dag/node",
    http_tags=["n8n", "dag"], memory="off",
    description="Describe how to use an n8n workflow as a node inside a Vera "
                "DAG — returns the literal [cap, out_key] node to paste in. "
                "Input: id (str!), out_key (str). "
                "Output: {dag_node, capability, usable, note}.",
)
async def cap_dag_node(id: str = "", out_key: str = "", trace_id=None):
    if not id:
        return {"error": "id required"}
    try:
        wf = await _api("GET", f"workflows/{id}")
        wf = wf.get("data", wf)
        return core.n8n_workflow_as_dag_node(wf, out_key)
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
#  BOOT + PANEL
# ═════════════════════════════════════════════════════════════════════════════

async def _autoconnect() -> None:
    """Re-register n8n MCP tools after a Vera restart.

    Registered caps live in the process registry, so without this every restart
    silently loses them and DAGs referencing n8n.* start failing with
    unknown_cap.
    """
    try:
        cfg = await _load_config()
        if not cfg.get("auto_connect") or not _mcp_url(cfg):
            return
        await asyncio.sleep(5)          # let the rest of the app finish booting
        res = await cap_mcp_connect()
        if res.get("error"):
            log.warning("n8n MCP autoconnect failed: %s", res["error"])
        else:
            log.info("n8n MCP autoconnect: %d tools", res.get("count", 0))
    except Exception:
        log.debug("n8n autoconnect skipped", exc_info=True)


@APP.on_event("startup")
async def _n8n_startup() -> None:
    asyncio.create_task(_autoconnect())


@capability(
    "n8n.panel.html", http_method="GET", http_path="/n8n/panel",
    http_tags=["n8n", "ui"], memory="off", silent=True,
    description="Serve the n8n integration panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        return HTMLResponse(_PANEL_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style='background:#0d0f12;color:#ef5b5b;"
            "font-family:monospace;padding:40px'><h2>n8n_panel.html not found</h2>"
            f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")


@APP.get("/n8n/panel", include_in_schema=False)
async def _n8n_panel_route():
    p = _HERE / "n8n_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>n8n_panel.html not found</p>")


register_ui(
    "n8n-panel", "n8n", "🔗",
    """<div id="n8n-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/n8n/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
    "",
    ui_caps=["n8n.config.get", "n8n.config.set", "n8n.workflow.list",
             "n8n.workflow.get", "n8n.workflow.activate", "n8n.mcp.probe",
             "n8n.mcp.connect", "n8n.mcp.disconnect", "n8n.dag.node",
             "n8n.execution.list"],
    # "element", not "tab": n8n is reached through the Automations hub, which
    # embeds /n8n/panel as its own sub-tab. Registering it here as a top-level
    # tab as well put the same panel in two places. Still registered (so the
    # dashboard-widget loader, custom tabs and solo popout can find it) —
    # just not auto-rendered as a tab of its own.
    mode="element",
    tab_order=73,
)

log.info("n8n_capabilities: ready")
