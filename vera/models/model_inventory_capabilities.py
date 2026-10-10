"""Read-only capability exposing Vera's portable model inventory."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from Vera.vera.capability_orchestration import capability
from Vera.vera.models.inference_deployment import SQLiteInferenceDeploymentRegistry
from Vera.vera.models.model_inventory import (
    project_model_inventory,
    project_persisted_inference_providers,
)
from Vera.vera.models.model_package_store import SQLiteModelPackageRegistry
from Vera.vera.models.nlp_inventory import project_nlp_inventory
from Vera.vera.state_paths import guard_out_of_tree, state_dir


def _state_path(env_name: str, filename: str) -> Path:
    configured = os.getenv(env_name, "").strip()
    path = Path(configured).expanduser() if configured else state_dir("models") / filename
    return guard_out_of_tree(path)


def _read_persisted_records() -> dict[str, Any]:
    package_path = _state_path("VERA_MODEL_PACKAGE_DB", "packages.sqlite3")
    deployment_path = _state_path(
        "VERA_INFERENCE_DEPLOYMENT_DB", "deployments.sqlite3")
    result: dict[str, Any] = {
        "packages": (), "aliases": (), "admissions": (), "activations": (),
        "legacy_bindings": (), "deployments": (), "observations": (),
        "sources": {},
    }
    if package_path.is_file():
        try:
            store = SQLiteModelPackageRegistry(package_path)
            result.update({
                "packages": store.list(),
                "aliases": store.aliases(),
                "admissions": store.admission_history(),
                "activations": store.activation_history(),
                "legacy_bindings": store.legacy_capability_bindings(),
            })
            result["sources"]["registry"] = {"status": "available"}
        except Exception as exc:
            result["sources"]["registry"] = {
                "status": "error", "reason": type(exc).__name__}
    else:
        result["sources"]["registry"] = {"status": "not_configured"}

    if deployment_path.is_file():
        try:
            store = SQLiteInferenceDeploymentRegistry(deployment_path)
            deployments = store.list()
            observations = tuple(
                observation for deployment in deployments
                if (observation := store.current(deployment.deployment_id)) is not None
            )
            result["deployments"] = deployments
            result["observations"] = observations
            result["providers"] = project_persisted_inference_providers(
                deployments, observations)
            result["sources"]["deployments"] = {"status": "available"}
        except Exception as exc:
            result["sources"]["deployments"] = {
                "status": "error", "reason": type(exc).__name__}
    else:
        result["sources"]["deployments"] = {"status": "not_configured"}
    return result


async def _read_nlp_inventory() -> tuple[dict[str, Any] | None, dict[str, str]]:
    dispatch = sys.modules.get("nlp_dispatch")
    if dispatch is None:
        return None, {"status": "unavailable", "reason": "dispatch_not_loaded"}
    try:
        nodes = await dispatch.discover()
        return project_nlp_inventory(nodes), {"status": "available"}
    except Exception as exc:  # source outage is inventory data, not a 500
        return None, {
            "status": "unavailable",
            "reason": type(exc).__name__,
        }


@capability(
    "model.inventory",
    http_method="GET",
    http_path="/models/inventory",
    http_tags=["models", "inventory"],
    memory="off",
    silent=True,
    description=(
        "Return a deterministic read-only inventory of portable ModelPackages, "
        "aliases, admission receipts, deployments, provider evidence, and "
        "verified or unresolved source-owned candidates. This capability does "
        "not download, hash, load, execute, activate, or route a model."
    ),
)
async def model_inventory(trace_id=None) -> dict[str, Any]:
    records = _read_persisted_records()
    nlp_inventory, nlp_status = await _read_nlp_inventory()
    sources = dict(records.pop("sources"))
    sources["nlp"] = nlp_status
    external = (("nlp", nlp_inventory),) if nlp_inventory is not None else ()
    return project_model_inventory(
        **records,
        external_inventories=external,
        source_status=sources,
    )


# ── callable models: the capability registry's model bindings, joined ──────────
@capability(
    "model.callable",
    http_method="GET",
    http_path="/models/callable",
    http_tags=["models", "inventory"],
    memory="off",
    silent=True,
    description=(
        "Every model Vera can call, and what each is FOR - pick a model for a task "
        "here. Specialist models (NLP, NER, GLiNER, spaCy, zero-shot, QA, embeddings, "
        "reranker, decision models) come from the capabilities that serve them "
        "(@capability serves=); LLMs come from the routing system (call llm.generate "
        "with model=<name>; purposes are the operator's model tags). Each row: id, cap "
        "and selector (the call that reaches it), task, family, uses, limits, io, "
        "labels, questions (decision ids), tier, placement, model, loaded_on / "
        "present_on (nodes), installable (specialist catalog id). Inputs: task, "
        "family, question (str filters), search (str - in id, uses, model), "
        "include_llms (bool=true). Output: {ok, count, models:[...], errors}."
    ),
)
async def model_callable(task: str = "", family: str = "", question: str = "",
                         search: str = "", include_llms: bool = True,
                         trace_id: str | None = None) -> dict[str, Any]:
    import Vera.vera.capability_orchestration as _orch
    from Vera.vera import model_binding_core as _mb
    from Vera.vera.catalog.specialist_catalog import CURATED
    from Vera.vera.research.nlp_dispatch_core import DEFAULT_MODELS

    nodes, sources = [], {}
    # cap modules load under bare names in prod; the package name elsewhere
    dispatch = (sys.modules.get("nlp_dispatch")
                or sys.modules.get("Vera.vera.research.nlp_dispatch"))
    if dispatch is None:
        sources["nlp_nodes"] = "unavailable: dispatch not loaded"
    else:
        try:
            nodes = await dispatch.discover()
            sources["nlp_nodes"] = f"{len(nodes)} node(s)"
        except Exception as exc:          # an outage is data, not a 500
            sources["nlp_nodes"] = f"unavailable: {type(exc).__name__}"
    rows = _mb.callable_view(_orch.CAPABILITY_REGISTRY, defaults=DEFAULT_MODELS,
                             nodes=nodes, catalog=CURATED,
                             task=task, family=family, question=question)
    if include_llms and not (family and family != "ollama") and not question \
            and (not task or task == "generate"):
        try:
            await _orch._model_tags_hydrate()
        except Exception:
            pass
        rows += _mb.llm_rows(getattr(_orch, "OLLAMA_INSTANCES", {}) or {},
                             getattr(_orch, "MODEL_TAGS", {}) or {})
    if search:
        q = search.lower()
        rows = [r for r in rows if q in " ".join(
            str(r.get(k) or "") for k in ("id", "uses", "model", "task")).lower()]
    return {"ok": True, "count": len(rows), "models": rows, "sources": sources,
            "errors": list(getattr(_orch, "MODEL_BINDING_ERRORS", []) or [])}
