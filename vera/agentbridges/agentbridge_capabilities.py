"""
agentbridge_capabilities.py — Agent Bridge Catalog
============================================================================
The visible/queryable half of the external-agentic-loop bridge system:
iterates agentbridge_registry.BRIDGES generically (no per-bridge code here —
a new registry entry shows up in this catalog automatically) to report live
status (docker image built? enabled?) and, on request, check each bridge's
pinned pip package versions against the latest published on PyPI.

Deliberately does NOT auto-upgrade a pin. These bridges run LLM-generated
code from third-party libraries in throwaway containers — a version bump is
a real, reviewed, tested change (rebuild the image, re-run the same live
verification every bridge in this codebase has gone through), landed through
the normal bleeding-edge pipeline like any other code change, never a
background job silently editing a Dockerfile. check_updates only REPORTS
drift; a human decides whether/when to act on it.

Capabilities
------------
  agentbridge.catalog        - list every registered bridge + live status
  agentbridge.check_updates  - compare pinned versions against PyPI's latest
  agentbridge.image.ensure   - build one bridge's image (dispatches to its
                                own <name>.image.ensure capability)
  agentbridge.panel.html     - serve the catalog panel
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

from Vera.vera.agentbridges.agentbridge_registry import BRIDGES, BY_ID
from Vera.vera.agentbridges.agentbridge_runtime import image_present
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, register_ui,
)
import os

log = logging.getLogger("vera.agentbridges.catalog")

_HERE = Path(__file__).parent
_PANEL_HTML_PATH = _HERE / "agentbridge_catalog_panel.html"

_PYPI_TIMEOUT_S = 8.0


async def _pypi_latest(package: str) -> Optional[str]:
    """Latest published version of a PyPI package, or None on any failure
    (network down, package renamed, PyPI itself unreachable) — check_updates
    treats that as 'unknown', never as 'no update available'."""
    try:
        async with httpx.AsyncClient(timeout=_PYPI_TIMEOUT_S) as client:
            r = await client.get(f"https://pypi.org/pypi/{package}/json")
            if r.status_code != 200:
                return None
            return r.json().get("info", {}).get("version")
    except Exception as e:
        log.debug("agentbridge: PyPI lookup failed for %s: %s", package, e)
        return None


@capability(
    "agentbridge.catalog",
    http_method="GET", http_path="/agentbridge/catalog", http_tags=["agentbridge"],
    memory="off", silent=True,
    description="List every registered external agent-loop bridge (smolagents, "
                "LangGraph, PydanticAI, ...) with live status: enabled (env "
                "var set), docker image built, paradigm, pinned pip package "
                "versions. Output: {bridges:[{id, label, icon, paradigm, "
                "description, docs_url, pip_packages, image, image_present, "
                "enabled, run_cap}]}.",
)
async def agentbridge_catalog(trace_id=None) -> Dict[str, Any]:
    out: List[Dict[str, Any]] = []
    for b in BRIDGES:
        enabled = os.environ.get(b.enabled_env, "0") == "1"
        present = await image_present(b.image)
        out.append({
            "id": b.id, "label": b.label, "icon": b.icon,
            "paradigm": b.paradigm, "description": b.description,
            "docs_url": b.docs_url, "pip_packages": b.pip_packages,
            "image": b.image, "image_present": present,
            "enabled": enabled, "enabled_env": b.enabled_env,
            "run_cap": b.run_cap, "status_cap": b.status_cap,
            "run_cap_registered": b.run_cap in CAPABILITY_REGISTRY,
        })
    return {"bridges": out, "count": len(out)}


@capability(
    "agentbridge.check_updates",
    http_method="POST", http_path="/agentbridge/check_updates", http_tags=["agentbridge"],
    memory="off",
    description="Compare each registered bridge's PINNED pip package versions "
                "(agentbridge_registry.py) against the latest published on "
                "PyPI. Reports drift only — never modifies a pin or rebuilds "
                "an image; bumping a version is a deliberate, tested code "
                "change like any other. Input: bridge (str, optional — limit "
                "to one bridge id). Output: {results:[{bridge, package, "
                "pinned, latest, drift}], checked_at}.",
)
async def agentbridge_check_updates(bridge: str = "", trace_id=None) -> Dict[str, Any]:
    from Vera.vera.capability_orchestration import now_iso
    targets = [BY_ID[bridge]] if bridge and bridge in BY_ID else BRIDGES
    results = []
    for b in targets:
        for pkg, pinned in b.pip_packages.items():
            latest = await _pypi_latest(pkg)
            results.append({
                "bridge": b.id, "package": pkg, "pinned": pinned,
                "latest": latest,
                "drift": bool(latest and latest != pinned),
            })
    return {"results": results, "checked_at": now_iso()}


@capability(
    "agentbridge.image.ensure",
    http_method="POST", http_path="/agentbridge/image/ensure", http_tags=["agentbridge"],
    memory="off",
    description="Build one bridge's image by id — dispatches to its own "
                "<name>.image.ensure capability (kept per-bridge since the "
                "Dockerfile build itself is genuinely bridge-specific). "
                "Input: bridge (str!), force (bool). "
                "Output: whatever the bridge's own image.ensure returns.",
)
async def agentbridge_image_ensure(bridge: str = "", force: bool = False,
                                   trace_id=None) -> Dict[str, Any]:
    if bridge not in BY_ID:
        return {"error": f"unknown bridge {bridge!r}. Known: {sorted(BY_ID)}"}
    cap_name = f"{bridge}.image.ensure"
    fn = CAPABILITY_REGISTRY.get(cap_name)
    if not fn:
        return {"error": f"{cap_name} not registered (bridge module not loaded)"}
    return await fn(force=force)


@capability(
    "agentbridge.panel.html", http_method="GET", http_path="/agentbridge/panel",
    http_tags=["agentbridge", "ui"], memory="off", silent=True,
    description="Serve the Agent Bridge Catalog panel HTML.",
)
async def cap_panel_html(trace_id=None):
    try:
        html = _PANEL_HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = ("<!DOCTYPE html><html><body style='background:#0d0f12;"
                "color:#ef5b5b;font-family:monospace;padding:40px'>"
                "<h2>agentbridge_catalog_panel.html not found</h2>"
                f"<p>Expected at: {_PANEL_HTML_PATH}</p></body></html>")
    return HTMLResponse(html)


@APP.get("/agentbridge/panel", include_in_schema=False)
async def _agentbridge_panel_route():
    p = _HERE / "agentbridge_catalog_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>agentbridge_catalog_panel.html not found</p>")


register_ui(
    "agentbridge-catalog-panel",
    "Agent Bridges",
    "🧩",
    """<div id="agentbridge-catalog-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/agentbridge/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["agentbridge.catalog", "agentbridge.check_updates", "agentbridge.image.ensure"],
    # mode="tab" (2026-08-16 fix, was "inject" — invisible by default: see
    # the identical fix + rationale in mcp_catalog_capabilities.py, same day
    # this panel was reported not showing up anywhere).
    mode="tab",
    tab_order=73,
)

log.info("agentbridge_capabilities: ready — %d bridges registered", len(BRIDGES))
