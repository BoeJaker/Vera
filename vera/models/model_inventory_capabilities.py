"""Read-only capability exposing Vera's portable model inventory."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from Vera.vera.capability_orchestration import capability
from Vera.vera.models.inference_deployment import SQLiteInferenceDeploymentRegistry
from Vera.vera.models.model_inventory import project_model_inventory
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
        store = SQLiteModelPackageRegistry(package_path)
        result.update({
            "packages": store.list(),
            "aliases": store.aliases(),
            "admissions": store.admission_history(),
            "activations": store.activation_history(),
            "legacy_bindings": store.legacy_capability_bindings(),
        })
        result["sources"]["registry"] = {"status": "available"}
    else:
        result["sources"]["registry"] = {"status": "not_configured"}

    if deployment_path.is_file():
        store = SQLiteInferenceDeploymentRegistry(deployment_path)
        deployments = store.list()
        result["deployments"] = deployments
        result["observations"] = tuple(
            observation for deployment in deployments
            if (observation := store.current(deployment.deployment_id)) is not None
        )
        result["sources"]["deployments"] = {"status": "available"}
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
            "reason": f"{type(exc).__name__}:{exc}",
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
    try:
        records = _read_persisted_records()
    except Exception as exc:
        records = {
            "packages": (), "aliases": (), "admissions": (), "activations": (),
            "legacy_bindings": (), "deployments": (), "observations": (),
            "sources": {"registry": {
                "status": "error", "reason": f"{type(exc).__name__}:{exc}"}},
        }
    nlp_inventory, nlp_status = await _read_nlp_inventory()
    sources = dict(records.pop("sources"))
    sources["nlp"] = nlp_status
    external = (("nlp", nlp_inventory),) if nlp_inventory is not None else ()
    return project_model_inventory(
        **records,
        external_inventories=external,
        source_status=sources,
    )
