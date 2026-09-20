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
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi.responses import HTMLResponse

from Vera.vera.agentbridges.agentbridge_registry import BRIDGES, BY_ID
from Vera.vera.agentbridges.agentbridge_runtime import image_present
from Vera.vera.agentbridges.runtime_registry import RUNTIME_ADAPTERS
from Vera.vera.agentbridges.runtime_matrix import (
    compile_runtime_matrix, evaluate_live_evidence,
)
from Vera.vera.execution.a2a_adapter import compile_a2a_adapter_status
from Vera.vera.execution.a2a_mapping import compile_a2a_protocol_mapping
from Vera.vera.integrations.source_intake import lifecycle_contract
from Vera.vera.integrations.source_build_plan import build_plan_contract
from Vera.vera.providers.structured_generation import structured_generation_status
from Vera.vera.providers.document_parser import document_parser_status
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, register_ui,
)
log = logging.getLogger("vera.agentbridges.catalog")
_RUNTIME_ADAPTERS = RUNTIME_ADAPTERS


@capability(
    "agentbridge.runtime_matrix", http_method="GET",
    http_path="/agentbridge/runtime_matrix", http_tags=["agentbridge"],
    memory="off", silent=True,
    description="Return the deterministic agent-runtime compatibility comparison. "
                "Separates upstream claims from Vera-verified bridge coverage; "
                "imports and executes no optional runtime; live evidence is "
                "validated separately.",
)
async def agentbridge_runtime_matrix(trace_id=None) -> Dict[str, Any]:
    return compile_runtime_matrix().to_dict()


@capability(
    "agentbridge.runtime_matrix.evaluate", http_method="POST",
    http_path="/agentbridge/runtime-matrix/evaluate",
    http_tags=["agentbridge", "interop"], memory="off", silent=True,
    description="Validate payload-free live conformance observations for selected "
                "runtime candidates. Does not execute a runtime, retain prompts or "
                "outputs, rank candidates, or choose a winner. Inputs: observations "
                "(list of runtime_id/case/state/reason_code plus bounded lifecycle "
                "facts), selected_runtime_ids (list).",
)
async def agentbridge_runtime_matrix_evaluate(
        observations: Optional[List[Dict[str, Any]]] = None,
        selected_runtime_ids: Optional[List[str]] = None,
        trace_id=None) -> Dict[str, Any]:
    try:
        return evaluate_live_evidence(
            observations or [], selected_runtime_ids or [])
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@capability(
    "agentbridge.interoperability", http_method="GET",
    http_path="/agentbridge/interoperability", http_tags=["agentbridge", "interop"],
    memory="off", silent=True,
    description="Return an inspection-only summary of the A2A adapter, external "
                "runtime matrix, and shared Vera contracts visible to Agent Bridge. "
                "Does not import optional runtimes, contact remote agents, or execute.",
)
async def agentbridge_interoperability(trace_id=None) -> Dict[str, Any]:
    matrix = compile_runtime_matrix().to_dict()
    mapping = compile_a2a_protocol_mapping().to_dict()
    adapter = compile_a2a_adapter_status()
    intake = lifecycle_contract()
    build_plan = build_plan_contract()
    structured = structured_generation_status()
    documents = document_parser_status()
    runtime_adapters = [adapter.inspect()
                        for adapter in _RUNTIME_ADAPTERS.values()]
    shared = (
        ("capability_v2", "cap.contract.manifest"),
        ("resolver_shadow", "cap.resolve.shadow"),
        ("policy_shadow", "cap.policy.shadow"),
        ("run_protocol", "run.shadow.list"),
        ("workflow_ir", "workflow.ir.validate"),
        ("portable_telemetry", "run.telemetry.status"),
        ("durability_fixture", "workflow.durability.fixture"),
        ("runtime_matrix", "agentbridge.runtime_matrix"),
        ("runtime_matrix_evidence", "agentbridge.runtime_matrix.evaluate"),
        ("runtime_cancel", "agentbridge.run.cancel"),
        ("runtime_version", "agentbridge.runtime.version"),
        ("a2a_conformance", "interop.a2a.conformance"),
        ("source_lifecycle", "integration.source.lifecycle"),
        ("source_inspection", "integration.source.inspect"),
        ("source_build_status", "integration.source.build.status"),
        ("source_build_plan", "integration.source.build.plan"),
        ("structured_status", "providers.structured.status"),
        ("structured_plan", "providers.structured.plan"),
        ("structured_validate", "providers.structured.validate"),
        ("structured_retry", "providers.structured.retry.plan"),
        ("document_status", "providers.document.status"),
        ("document_plan", "providers.document.plan"),
        ("document_validate", "providers.document.validate"),
        ("document_corpus", "providers.document.corpus.evaluate"),
        ("document_teardown", "providers.document.teardown.plan"),
        # The other half of the estate: what external agents drive Vera
        # WITH. Everything above describes how Vera reaches out; this is
        # the only row that describes what is reaching in.
        ("agent_registry", "registry.interop"),
    )
    return {
        "schema": "vera.agentbridge-interoperability/v1",
        "a2a": {
            "mapping_id": mapping["mapping_id"],
            "foundation": adapter["foundation"],
            "client_plan_contract": adapter["client_plan_contract"],
            "server_plan_contract": adapter["server_plan_contract"],
            "client_transport": adapter["client_transport"],
            "server_listener": adapter["server_listener"],
            "deterministic_cases": mapping["lanes"]["deterministic"],
            "queued_live_cases": mapping["lanes"]["queued_live"],
            "ready_for_execution": mapping["ready_for_execution"],
        },
        "runtime_matrix": {
            "matrix_id": matrix.get("matrix_id"),
            "candidate_count": matrix.get("candidate_count", 0),
            "dimension_count": len(matrix.get("dimensions", [])),
            "queued_live_cases": len(matrix.get("required_live_cases", [])),
            "required_live_cases": len(matrix.get("required_live_cases", [])),
            "evidence_evaluator_registered": (
                "agentbridge.runtime_matrix.evaluate" in CAPABILITY_REGISTRY),
            "execution_lane": matrix.get("execution_lane"),
            "ready_for_selection": matrix.get("ready_for_selection", False),
        },
        "runtime_adapters": runtime_adapters,
        "source_intake": {
            "schema": intake["schema"],
            "implemented_states": intake["implemented_states"],
            "queued_states": intake["queued_states"],
            "supported_kinds": intake["supported_kinds"],
            "network_io": intake["network_io"],
            "executes": intake["executes"],
            "build_plan_contract": build_plan["proposal_contract"],
            "build_source_kinds": build_plan["supported_kinds"],
            "build_execution": build_plan["build_execution"],
            "activation_execution": build_plan["activation_execution"],
        },
        "structured_generation": {
            "contract": structured["contract"],
            "deterministic_validation": structured["deterministic_validation"],
            "provider_profiles": structured["provider_profiles"],
            "provider_execution": structured["provider_execution"],
            "model_execution": structured["model_execution"],
            "model_called": structured["model_called"],
            "executes": structured["executes"],
        },
        "document_parser": {
            "contract": documents["contract"],
            "provider_profiles": documents["provider_profiles"],
            "portable_media_types": documents["portable_media_types"],
            "frozen_corpus_contract": documents["frozen_corpus_contract"],
            "stable_element_ids": documents["stable_element_ids"],
            "citations": documents["citations"],
            "parser_execution": documents["parser_execution"],
            "ocr_execution": documents["ocr_execution"],
            "queued_live_gates": documents["queued_live_gates"],
            "provider_imported": documents["optional_provider_imported"],
            "executes": documents["executes"],
        },
        "shared_contracts": [
            {"id": key, "capability": cap, "registered": cap in CAPABILITY_REGISTRY}
            for key, cap in shared
        ],
        "imports_optional_runtimes": False,
        "network_io": False,
        "executes": False,
    }


@capability(
    "agentbridge.run.cancel", http_method="POST",
    http_path="/agentbridge/run/cancel", http_tags=["agentbridge"],
    memory="off",
    description="Request cancellation of one active isolated agent-runtime run. "
                "Inputs: runtime_id and run_id. The runtime adapter targets only "
                "the exact process handle already owned by that run; it never "
                "searches or kills by a guessed container name. Output reports "
                "whether cancellation was accepted or the run was not active.",
)
async def agentbridge_run_cancel(runtime_id: str, run_id: str,
                                 trace_id=None) -> Dict[str, Any]:
    runtime_id = str(runtime_id or "").strip().lower()
    adapter = _RUNTIME_ADAPTERS.get(runtime_id)
    if adapter is None:
        return {"ok": False, "accepted": False,
                "reason_code": "runtime_adapter_unknown",
                "runtime_id": runtime_id,
                "supported_runtime_ids": sorted(_RUNTIME_ADAPTERS)}
    return await adapter.cancel(run_id)


@capability(
    "agentbridge.runtime.version", http_method="GET",
    http_path="/agentbridge/runtime/version", http_tags=["agentbridge"],
    memory="off", silent=True,
    description="Compare one isolated runtime image's self-declared identity and "
                "pinned package set from read-only OCI labels. The image is never started and "
                "the optional runtime is never imported. Input: runtime_id. "
                "Output distinguishes verified, mismatch, unattested, unavailable, "
                "and unknown-adapter states.",
)
async def agentbridge_runtime_version(runtime_id: str,
                                      trace_id=None) -> Dict[str, Any]:
    runtime_id = str(runtime_id or "").strip().lower()
    adapter = _RUNTIME_ADAPTERS.get(runtime_id)
    if adapter is None:
        return {"ok": False, "verified": False,
                "status": "unknown", "reason_code": "runtime_adapter_unknown",
                "runtime_id": runtime_id,
                "supported_runtime_ids": sorted(_RUNTIME_ADAPTERS),
                "executes_runtime": False}
    return await adapter.version_report()

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
    "⋈",
    """<div id="agentbridge-catalog-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/agentbridge/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["agentbridge.catalog", "agentbridge.runtime_matrix",
             "agentbridge.interoperability", "agentbridge.check_updates",
             "agentbridge.image.ensure"],
    # mode="tab" (2026-08-16 fix, was "inject" — invisible by default: see
    # the identical fix + rationale in mcp_catalog_capabilities.py, same day
    # this panel was reported not showing up anywhere).
    mode="tab",
    tab_order=73,
)

log.info("agentbridge_capabilities: ready — %d bridges registered", len(BRIDGES))
