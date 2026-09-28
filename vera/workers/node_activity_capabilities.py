"""Node activity - one pane for everything the compute nodes are doing.

    nodes.activity          per node: what is running right now, the last calls
                            (model, caller - prod / sandbox / external - job type,
                            duration, tokens, tok/s, prompt/response previews),
                            summaries over a window, the node's own load (node
                            agent) and who holds the GPU gate. Filters.
    nodes.activity.record   one call's FULL prompt and response, fetched from
                            the node's tap (the shared stream keeps previews).

Source: the node-side taps (edge/ollama_tap.py, nodes.ollama.tap) write every
generate/chat/embed call - from Vera, from sandboxes, from anyone - to the
shared Redis stream `vera:node_activity`, and their in-flight calls to
`vera:node_activity:inflight:<node>`. Read-only here.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from fastapi.responses import HTMLResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, register_ui
from Vera.vera.workers import node_activity_core as _core

log = logging.getLogger("vera.nodes.activity")

_AGENT_CACHE: Dict[str, Any] = {"at": 0.0, "data": {}}


def _rawcap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


def _s(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v or "")


async def _records(count: int) -> List[Dict[str, Any]]:
    r = _orch.REDIS
    if r is None:
        return []
    out = []
    try:
        for _id, fields in await r.xrevrange(_core.STREAM, count=count):
            f = {_s(k): v for k, v in (fields or {}).items()}
            try:
                out.append(json.loads(_s(f.get("r"))))
            except ValueError:
                continue
    except Exception as e:
        log.debug("node activity stream: %s", e)
    return out


async def _inflight() -> Dict[str, List[Dict[str, Any]]]:
    r = _orch.REDIS
    out: Dict[str, List[Dict[str, Any]]] = {}
    if r is None:
        return out
    try:
        async for k in r.scan_iter(_core.INFLIGHT_PREFIX + "*"):
            # "<node>" from a tap, "<node>:<service>" from a node service
            node = _s(k)[len(_core.INFLIGHT_PREFIX):].split(":", 1)[0]
            try:
                out.setdefault(node, []).extend(json.loads(_s(await r.get(k)) or "[]"))
            except ValueError:
                out.setdefault(node, [])
    except Exception as e:
        log.debug("node activity inflight: %s", e)
    return out


async def _agents() -> Dict[str, Any]:
    """Node agent facts (load, memory, GPU, runners), cached 10 s."""
    if time.time() - _AGENT_CACHE["at"] < 10:
        return _AGENT_CACHE["data"]
    fn = _rawcap("nodes.agent.status")
    data: Dict[str, Any] = {}
    if fn:
        try:
            for n in ((await fn()) or {}).get("nodes") or []:
                data[str(n.get("node_id"))] = {k: n.get(k) for k in (
                    "cores", "load", "mem_total_mb", "mem_available_mb", "gpu",
                    "runners", "error") if k in n}
        except Exception as e:
            log.debug("agent status: %s", e)
    _AGENT_CACHE.update(at=time.time(), data=data)
    return data


_NAMES_CACHE: Dict[str, Any] = {"at": 0.0, "map": {}}


async def _agent_names() -> Dict[str, str]:
    """Container hostname -> instance id, from the node agents (cached 60 s)."""
    if time.time() - _NAMES_CACHE["at"] < 60:
        return _NAMES_CACHE["map"]
    fn = _rawcap("nodes.agent.status")
    m: Dict[str, str] = {}
    if fn:
        try:
            for n in ((await fn()) or {}).get("nodes") or []:
                if n.get("node") and n.get("node_id"):
                    host, iid = str(n["node"]), str(n["node_id"])
                    # a GPU node and its CPU sibling share a hostname: the
                    # node's own services belong to the node, not the sibling
                    if host not in m or m[host].endswith("-cpu"):
                        m[host] = iid
        except Exception:
            pass
    _NAMES_CACHE.update(at=time.time(), map=m)
    return m


def _instances() -> Dict[str, Dict[str, Any]]:
    return {iid: {"url": i.get("url"), "has_gpu": bool(i.get("has_gpu")),
                  "status": i.get("status"), "in_use": i.get("in_use", 0),
                  "label": i.get("label", iid)}
            for iid, i in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).items()}


@capability(
    "nodes.activity",
    http_method="GET", http_path="/nodes/activity", http_tags=["nodes", "obs"],
    memory="off", silent=True,
    description="Everything the compute nodes are doing, in one view: per node what is "
                "running NOW, the recent calls (model, caller - prod Vera / a Loop Lab "
                "sandbox / external - job type, duration, tokens, tok/s, prompt and "
                "response previews), summaries over the window, the node's own load and "
                "the GPU gate's holders. Includes calls from sandboxes and external callers "
                "(the node-side taps see every request). Filters: node, service (ollama|"
                "nlp|media|worker), caller (prod|sandbox|external), kind (generate|chat|"
                "embed), text (search model/prompt/response/origin), since_s (window, "
                "default 3600), limit (rows, default 200). Output: {ok, now, nodes:{node:"
                "{running[], calls, by_kind, by_caller, tokens_out, mean_tps, busy_pct, "
                "errors, models, agent}}, rows[], gate, instances, taps}.",
)
async def cap_nodes_activity(node: str = "", service: str = "", caller: str = "",
                             kind: str = "", text: str = "", since_s: int = 3600,
                             limit: int = 200, trace_id=None) -> Dict[str, Any]:
    now = time.time()
    recs = await _records(3000)
    infl = await _inflight()
    # Node services name themselves by container hostname ("Ollama-B"); the
    # node agent knows both names, so every record lands under its instance id.
    agents_raw = await _agent_names()
    for r in recs:
        r["node"] = agents_raw.get(r.get("node"), r.get("node"))
    infl = {agents_raw.get(n, n): v for n, v in infl.items()}
    since = now - max(60, int(since_s or 3600))
    summary = _core.summarize(recs, infl, window_s=min(900, max(60, int(since_s or 3600))), now=now)
    agents = await _agents()
    insts = _instances()
    for n in set(summary) | set(insts):
        s = summary.setdefault(n, {"node": n, "calls": 0, "by_kind": {}, "by_caller": {},
                                   "tokens_out": 0, "busy_s": 0.0, "busy_pct": 0.0,
                                   "errors": 0, "mean_tps": None, "models": {}, "running": []})
        s["agent"] = agents.get(n) or agents.get(n.rsplit("-cpu", 1)[0]) or {}
        s["instance"] = insts.get(n) or {}
        s["tapped"] = n in infl
    rows = [_core.row(r) for r in recs
            if _core.matches(r, node=node, service=service, caller=caller, kind=kind,
                             text=text, since=since)][:max(1, min(1000, int(limit or 200)))]
    gate = {}
    g = _rawcap("ollama.gate.status")
    if g:
        try:
            gate = await g() or {}
        except Exception:
            gate = {}
    return {"ok": True, "now": now, "nodes": summary, "rows": rows,
            "gate": gate.get("nodes") or [], "instances": insts,
            "taps": sorted(infl), "records_seen": len(recs)}


@capability(
    "nodes.activity.record",
    http_method="GET", http_path="/nodes/activity/record", http_tags=["nodes", "obs"],
    memory="off", silent=True,
    description="One node call's FULL record - the complete prompt and response and every "
                "stat - fetched from the node's tap (the shared stream keeps 16 KB previews). "
                "Inputs: node (str! - instance id), id (str! - the record id), port (int - "
                "the tap's port; default the instance's). Output: the record, or {error}.",
)
async def cap_nodes_activity_record(node: str = "", id: str = "", port: int = 0,
                                    trace_id=None) -> Dict[str, Any]:
    import httpx
    inst = (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).get(node)
    if not inst or not id or not id.isalnum():
        return {"error": "node (a registered instance) and id are required"}
    u = urlparse(str(inst.get("url") or ""))
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"http://{u.hostname}:{int(port or u.port or 11435)}"
                            f"/vera-tap/record/{id}")
            return r.json() if r.status_code == 200 else {"error": f"tap answered {r.status_code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── the element ───────────────────────────────────────────────────────────────
_EL = Path(__file__).resolve().parent / "node_activity_element.js"


@APP.get("/ui/elements/node_activity.js", include_in_schema=False)
async def _node_activity_js():
    try:
        body = _EL.read_text(encoding="utf-8")
    except OSError:
        body = "console.error('node_activity_element.js not found')"
    return Response(body, media_type="application/javascript")


@APP.get("/nodes/activity/panel", include_in_schema=False)
async def _node_activity_panel():
    return HTMLResponse("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<script>(function(){try{var d=document.documentElement,S=window.localStorage;
var t=S.getItem('vera:ui:theme');if(t)d.setAttribute('data-theme',t);
var vf=S.getItem('vera:ui:themeVarsFor');if(t&&vf!==t)return;var v=JSON.parse(S.getItem('vera:ui:themeVars')||'null');
if(v)for(var k in v)d.style.setProperty(k,v[k]);}catch(e){}})();</script>
<title>Vera - Node activity</title>
<style>:root{--bg:#0d0f12;--bg1:#14181d;--bg2:#1a1f26;--border:#232a33;--border2:#2e3742;--fg:#d8dde3;
--dim:#5f6975;--acc:#4a9eff;--acc2:#28c28a;--warn:#f5b341;--err:#ef5b5b}
html,body{margin:0;background:var(--bg0,var(--bg));color:var(--fg);height:100%}</style></head>
<body><vera-node-activity></vera-node-activity>
<script src="/ui/vera-ui.js"></script><script src="/ui/elements/node_activity.js"></script></body></html>""")


register_ui(
    "node-activity", "Node activity", "◉",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/nodes/activity/panel" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"></iframe>
</div>""",
    "",
    ui_caps=["nodes.activity", "nodes.activity.record"],
    mode="element",
    tab_order=73,
)
