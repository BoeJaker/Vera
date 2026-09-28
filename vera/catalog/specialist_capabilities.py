"""Specialist models - the non-LLM models the estate runs, in one view.

    specialist.status   NLP on each node (nlp_server version vs the host's, each
                        task's model: in the store? loaded?), the GPU media
                        server(s) (STT/TTS/diffusion), and the host's entity NER
                        (spaCy / GLiNER). deep=true adds the media server's SD
                        model, LoRA and voice counts, and each media node's
                        deployed gpu_inference version over SSH.

Read-only. Deploying (re-provisioning nodes to the host's version) is
`nodes.provision` / `provision.deploy`; this module only says who is behind.
Everything is reached through other capabilities, so the module never imports
a sibling capability module (see the Vera namespace trap: a second import runs
the module body again).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from fastapi.responses import HTMLResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, register_ui
from Vera.vera.catalog import specialist_core as _core
from Vera.vera.provisioning.components_core import compare_versions
from Vera.vera.research.nlp_dispatch_core import DEFAULT_MODELS, TASK_KIND

log = logging.getLogger("vera.catalog.specialist")


def _rawcap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


async def _call(name: str, **kw) -> Dict[str, Any]:
    fn = _rawcap(name)
    if not fn:
        return {"error": f"{name} unavailable"}
    try:
        return await fn(**kw) or {}
    except Exception as e:
        log.debug("specialist: %s failed: %s", name, e)
        return {"error": f"{name}: {type(e).__name__}: {e}"}


async def _host_versions() -> Dict[str, Dict[str, Any]]:
    out = {}
    for comp in ("nlp_server", "gpu_inference"):
        res = await _call("provision.component.version", component=comp)
        out[comp] = res.get("host") or {}
    return out


def _host_ner() -> Dict[str, Any]:
    """The host's in-process entity NER, from its config and what is importable.
    Deliberately NOT fabric.entity_graph.ner: that runs a self-test extraction,
    which on this 2-core host is a real inference, not a status read."""
    return {"backend": os.getenv("FABRIC_NER_BACKEND", "auto"),
            "spacy_model": os.getenv("FABRIC_NER_MODEL", "en_core_web_sm"),
            "gliner_model": os.getenv("FABRIC_GLINER_MODEL", "urchade/gliner_medium-v2.1"),
            "spacy_installed": importlib.util.find_spec("spacy") is not None,
            "gliner_installed": importlib.util.find_spec("gliner") is not None,
            "runs_on": "host (in-process, fabric entity graph)"}


async def _media_deep(row: Dict[str, Any]) -> Dict[str, Any]:
    """SD model + LoRA count and voice count from an online media server."""
    import httpx
    out: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{row['url']}/sd/capabilities")
            if r.status_code == 200:
                d = r.json() or {}
                out["sd_model"] = d.get("model", "")
                out["loras"] = d.get("loras")
                out["image_tiers"] = [k for k, v in d.items() if v is True]
            r = await c.get(f"{row['url']}/tts/voices")
            if r.status_code == 200:
                d = r.json() or {}
                out["tts_engine"] = d.get("engine", "")
                out["voices"] = len(d.get("voices") or [])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


async def _ssh_host_ids() -> Dict[str, str]:
    """LAN address -> stored SSH host id, to read a node's deployed version."""
    res = await _call("exec.ssh.hosts.list")
    return {str(h.get("host")): str(h.get("id")) for h in (res.get("hosts") or [])
            if h.get("host") and h.get("id")}


@capability(
    "specialist.status",
    http_method="GET", http_path="/specialist/status", http_tags=["models", "nlp", "media"],
    memory="off", silent=True,
    description="The non-LLM ('specialist') models across the estate, per node: NLP "
                "(nlp_server's deployed version against the version this host would "
                "deploy - current|behind|modified|unversioned - and each task's model, "
                "whether it is in the shared store and loaded), the GPU media servers "
                "(STT/TTS/diffusion services), and the host's entity NER (spaCy / "
                "GLiNER). Read-only. Inputs: refresh (bool - re-probe the NLP nodes), "
                "deep (bool - also the media server's SD model/LoRA/voice counts and "
                "each media node's gpu_inference version over SSH). Output: {ok, "
                "versions, nlp:{placement, registry, nodes}, media:{nodes}, host_ner, "
                "summary}.",
)
async def cap_specialist_status(refresh: bool = False, deep: bool = False,
                                trace_id=None) -> Dict[str, Any]:
    versions = await _host_versions()

    nlp = await _call("nlp.nodes", refresh=bool(refresh))
    nlp_rows = [_core.nlp_node_row(n, versions.get("nlp_server") or {}, compare_versions)
                for n in (nlp.get("nodes") or [])]

    media_rows = [_core.media_node_row(iid, inst) for iid, inst in
                  sorted((getattr(_orch, "MEDIA_INSTANCES", {}) or {}).items())]

    if deep:
        online = [r for r in media_rows if r["status"] == "online"]
        for r, extra in zip(online, await asyncio.gather(*(_media_deep(r) for r in online))):
            r["serves"].update(extra)
        ids = await _ssh_host_ids()
        for r in media_rows:
            hid = ids.get(urlparse(r["url"]).hostname or "")
            if not hid:
                r["component"] = {"state": "unknown", "note": "no stored SSH host"}
                continue
            v = await _call("provision.component.version", component="gpu_inference",
                            host_id=hid)
            r["component"] = {"state": v.get("state", "unknown"),
                              "version": (v.get("node") or {}).get("version", ""),
                              "changed": v.get("changed") or [],
                              "error": v.get("error", "")}

    try:
        sandbox = bool(_orch.is_dev_sandbox())
    except Exception:
        sandbox = False
    return {"ok": True, "sandbox": sandbox,
            "versions": {k: v.get("version", "") for k, v in versions.items()},
            "nlp": {"placement": {"where": nlp.get("where"), "node": nlp.get("node"),
                                  "reason": nlp.get("reason"),
                                  "nlp_local": nlp.get("nlp_local")},
                    "registry": _core.registry_rows(DEFAULT_MODELS, TASK_KIND),
                    "nodes": nlp_rows, "error": nlp.get("error", "")},
            "media": {"nodes": media_rows},
            "host_ner": _host_ner(),
            "summary": _core.summarize(nlp_rows, media_rows)}


# ── the element ───────────────────────────────────────────────────────────────
_EL = Path(__file__).resolve().parent / "specialist_models_element.js"


@APP.get("/ui/elements/specialist_models.js", include_in_schema=False)
async def _specialist_element_js():
    try:
        body = _EL.read_text(encoding="utf-8")
    except OSError:
        body = "console.error('specialist_models_element.js not found')"
    return Response(body, media_type="application/javascript")


@APP.get("/specialist/panel", include_in_schema=False)
async def _specialist_panel():
    """The element as a page of its own (the registered panel embeds this)."""
    return HTMLResponse("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<script>(function(){try{var d=document.documentElement,S=window.localStorage;
var t=S.getItem('vera:ui:theme');if(t)d.setAttribute('data-theme',t);
var vf=S.getItem('vera:ui:themeVarsFor');if(t&&vf!==t)return;var v=JSON.parse(S.getItem('vera:ui:themeVars')||'null');
if(v)for(var k in v)d.style.setProperty(k,v[k]);}catch(e){}})();</script>
<title>Vera - Specialist models</title>
<style>:root{--bg:#0d0f12;--bg1:#14181d;--bg2:#1a1f26;--border:#232a33;--border2:#2e3742;--fg:#d8dde3;
--dim:#5f6975;--acc:#4a9eff;--acc2:#28c28a;--warn:#f5b341;--err:#ef5b5b}
html,body{margin:0;background:var(--bg0,var(--bg));color:var(--fg);height:100%}</style></head>
<body><vera-specialist-models></vera-specialist-models>
<script src="/ui/vera-ui.js"></script><script src="/ui/elements/specialist_models.js"></script></body></html>""")


register_ui(
    "specialist-models", "Specialist models", "◇",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/specialist/panel" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"></iframe>
</div>""",
    "",
    ui_caps=["specialist.status", "provision.component.sync"],
    # an element of the Models view (and any dashboard), not a tab of its own
    mode="element",
    tab_order=75,
)
