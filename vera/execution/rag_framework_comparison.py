"""Offline, loss-aware comparison boundary for data/RAG frameworks.

The adapter deliberately consumes Vera-owned manifests rather than importing a
framework's executable objects.  LlamaIndex and Haystack remain optional
providers; Workflow IR, DatasetSnapshot, and record/revision citations remain
the portable authorities.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .workflow_ir import IR_VERSION, WorkflowIRValidationError, normalize_workflow
from ..fabric.dataset_provider import DatasetSnapshot
from ..fabric.retrieval_comparison import (
    RetrievalCase,
    RetrievalComparisonFixture,
    RetrievalProviderEvidence,
    compare_retrieval,
)


MANIFEST_SCHEMA = "vera.rag-framework-manifest/v1"
COMPARISON_SCHEMA = "vera.rag-framework-comparison/v1"
FRAMEWORKS = ("llamaindex", "haystack")

_ROLE_TO_TASK = {
    "reader": "data.read",
    "converter": "data.read",
    "parser": "data.chunk",
    "preprocessor": "data.chunk",
    "embedder": "model.embed",
    "store": "data.index",
    "retriever": "data.retrieve",
    "reranker": "data.rerank",
    "synthesizer": "data.synthesize",
    "generator": "data.synthesize",
}
_TASK_TO_ROLE = {
    "data.read": {"llamaindex": "reader", "haystack": "converter"},
    "data.chunk": {"llamaindex": "parser", "haystack": "preprocessor"},
    "model.embed": {"llamaindex": "embedder", "haystack": "embedder"},
    "data.index": {"llamaindex": "store", "haystack": "store"},
    "data.retrieve": {"llamaindex": "retriever", "haystack": "retriever"},
    "data.rerank": {"llamaindex": "reranker", "haystack": "reranker"},
    "data.synthesize": {"llamaindex": "synthesizer", "haystack": "generator"},
}


def _gap(path: str, code: str, detail: str, *, blocking: bool = True) -> dict[str, Any]:
    return {"path": path, "code": code, "detail": detail, "blocking": blocking}


def _framework(value: Any) -> str:
    value = str(value or "").strip().lower().replace("_", "")
    if value not in FRAMEWORKS:
        raise ValueError("framework must be llamaindex or haystack")
    return value


def _bounded_text(value: Any, field: str, limit: int = 256) -> str:
    value = str(value or "").strip()
    if not value or len(value) > limit:
        raise ValueError(f"{field} must contain 1..{limit} characters")
    return value


def _component_rows(manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = manifest.get("components")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise ValueError("components must contain 1..100 entries")
    clean: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(rows):
        path = f"components[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{path} must be an object")
        unknown = set(raw) - {"id", "role", "component_type", "depends_on"}
        if unknown:
            gaps.append(_gap(path, "unsupported_component_fields",
                             "unsupported fields: " + ", ".join(sorted(unknown))))
        component_id = _bounded_text(raw.get("id"), f"{path}.id", 128)
        if component_id in ids:
            raise ValueError(f"duplicate component id: {component_id}")
        ids.add(component_id)
        role = str(raw.get("role") or "").strip().lower()
        if role not in _ROLE_TO_TASK:
            gaps.append(_gap(path + ".role", "unsupported_component_role",
                             f"role {role!r} has no portable task mapping"))
        component_type = _bounded_text(raw.get("component_type"),
                                       f"{path}.component_type", 256)
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list) or any(
                not isinstance(item, str) or not item for item in depends_on):
            raise ValueError(f"{path}.depends_on must be an array of component IDs")
        if len(depends_on) != len(set(depends_on)):
            raise ValueError(f"{path}.depends_on must not contain duplicates")
        clean.append({"id": component_id, "role": role,
                      "component_type": component_type,
                      "depends_on": list(depends_on)})
    seen_before: set[str] = set()
    for index, row in enumerate(clean):
        for dependency in row["depends_on"]:
            if dependency not in ids:
                raise ValueError(f"components[{index}] depends on unknown component {dependency!r}")
            if dependency == row["id"]:
                raise ValueError(f"components[{index}] cannot depend on itself")
            if dependency not in seen_before:
                raise ValueError(
                    f"components[{index}] dependency {dependency!r} must precede the component")
        seen_before.add(row["id"])
    return clean, gaps


def import_framework_manifest(manifest: Mapping[str, Any], *,
                              allow_lossy: bool = False) -> dict[str, Any]:
    """Project a bounded external-framework manifest into Workflow IR.

    The function never imports, constructs, or invokes framework code.
    """
    if not isinstance(manifest, Mapping):
        raise TypeError("manifest must be an object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported framework manifest schema")
    unknown = set(manifest) - {
        "schema", "framework", "framework_version", "pipeline_id", "name",
        "components", "extensions",
    }
    gaps: list[dict[str, Any]] = []
    if unknown:
        gaps.append(_gap("manifest", "unsupported_manifest_fields",
                         "unsupported fields: " + ", ".join(sorted(unknown))))
    framework = _framework(manifest.get("framework"))
    framework_version = _bounded_text(manifest.get("framework_version"),
                                      "framework_version", 128)
    pipeline_id = _bounded_text(manifest.get("pipeline_id"), "pipeline_id", 128)
    name = _bounded_text(manifest.get("name"), "name")
    if manifest.get("extensions") not in (None, {}):
        gaps.append(_gap("extensions", "opaque_framework_extensions",
                         "framework extensions have no portable semantics"))
    components, component_gaps = _component_rows(manifest)
    gaps.extend(component_gaps)

    steps: list[dict[str, Any]] = []
    for index, component in enumerate(components):
        task = _ROLE_TO_TASK.get(component["role"])
        if task is None:
            continue
        bindings = {
            f"dependency_{dep_index + 1}": {"kind": "state", "value": dependency}
            for dep_index, dependency in enumerate(component["depends_on"])
        }
        steps.append({
            "id": component["id"], "type": "task", "task": task,
            "output": component["id"], "bindings": bindings,
            "extensions": {"vera.rag_framework": {
                "framework": framework,
                "component_type": component["component_type"],
                "role": component["role"],
                "order": index,
            }},
        })
    blocking = [gap for gap in gaps if gap["blocking"]]
    workflow = None
    if not blocking or allow_lossy:
        workflow = normalize_workflow({
            "ir_version": IR_VERSION,
            "name": name,
            "description": f"Portable projection of {framework} pipeline {pipeline_id}",
            "steps": steps,
            "extensions": {"vera.rag_framework": {
                "source_framework": framework,
                "source_version": framework_version,
                "source_pipeline_id": pipeline_id,
                "execution_authority": "external_opt_in",
                "record_authority": "vera.fabric",
            }},
        })
    return {
        "schema": "vera.rag-framework-import/v1", "ok": workflow is not None,
        "framework": framework, "workflow": workflow, "gaps": gaps,
        "lossy": bool(blocking), "executes": False, "providers_invoked": False,
        "authority": {"workflow": "vera.workflow_ir", "records": "vera.fabric"},
    }


def export_framework_manifest(workflow: Mapping[str, Any], *, framework: str,
                              framework_version: str,
                              allow_lossy: bool = False) -> dict[str, Any]:
    """Export the supported Workflow IR subset as a non-executable manifest."""
    framework = _framework(framework)
    framework_version = _bounded_text(framework_version, "framework_version", 128)
    try:
        normalized = normalize_workflow(dict(workflow))
    except (TypeError, WorkflowIRValidationError) as exc:
        return {"schema": "vera.rag-framework-export/v1", "ok": False,
                "framework": framework, "manifest": None,
                "gaps": [_gap("workflow", "invalid_workflow", str(exc))],
                "lossy": False, "executes": False, "providers_invoked": False}
    gaps: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    known_outputs: dict[str, str] = {}
    for index, step in enumerate(normalized["steps"]):
        path = f"steps[{index}]"
        if step["type"] != "task":
            gaps.append(_gap(path, "unsupported_structure",
                             f"{step['type']} is outside the portable RAG subset"))
            continue
        roles = _TASK_TO_ROLE.get(step["task"])
        if roles is None:
            gaps.append(_gap(path + ".task", "unsupported_task",
                             f"task {step['task']!r} has no framework component mapping"))
            continue
        dependency_ids: list[str] = []
        for binding_path, binding in sorted((step.get("bindings") or {}).items()):
            if binding.get("kind") != "state" or binding.get("value") not in known_outputs:
                gaps.append(_gap(f"{path}.bindings.{binding_path}",
                                 "unsupported_binding",
                                 "only state references to prior component outputs are portable"))
            else:
                dependency_ids.append(known_outputs[binding["value"]])
        extension = (step.get("extensions") or {}).get("vera.rag_framework", {})
        if extension.get("framework") in (None, framework):
            component_type = str(extension.get("component_type") or roles[framework])
        else:
            component_type = roles[framework]
            gaps.append(_gap(
                path + ".extensions.vera.rag_framework.component_type",
                "framework_specific_component_type",
                "source component type was replaced by the target framework role",
                blocking=False,
            ))
        unknown_extensions = set(step.get("extensions") or {}) - {"vera.rag_framework"}
        for key in sorted(unknown_extensions):
            gaps.append(_gap(f"{path}.extensions.{key}", "unsupported_extension",
                             "extension has no framework-manifest representation"))
        components.append({"id": step["id"], "role": roles[framework],
                           "component_type": component_type,
                           "depends_on": dependency_ids})
        known_outputs[step.get("output", step["id"])] = step["id"]
    for key in sorted(set(normalized.get("extensions") or {}) - {"vera.rag_framework"}):
        gaps.append(_gap(f"extensions.{key}", "unsupported_extension",
                         "workflow extension has no framework-manifest representation"))
    blocking = [gap for gap in gaps if gap["blocking"]]
    manifest = None
    if not blocking or allow_lossy:
        manifest = {
            "schema": MANIFEST_SCHEMA, "framework": framework,
            "framework_version": framework_version,
            "pipeline_id": normalized["content_hash"],
            "name": normalized.get("name") or "RAG pipeline",
            "components": components,
        }
    return {
        "schema": "vera.rag-framework-export/v1", "ok": manifest is not None,
        "framework": framework, "manifest": manifest, "gaps": gaps,
        "lossy": bool(blocking), "executes": False, "providers_invoked": False,
        "authority": {"workflow": "vera.workflow_ir", "records": "vera.fabric"},
    }


@dataclass(frozen=True)
class RAGFrameworkEvidence:
    framework: str
    projection: Mapping[str, Any]
    retrieval: RetrievalProviderEvidence
    import_ms: int
    export_ms: int
    dependency_count: int
    installed_bytes: int
    teardown_ms: int
    residual_resources: tuple[str, ...] = ()
    duplicate_authorities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "framework", _framework(self.framework))
        if not isinstance(self.projection, Mapping):
            raise TypeError("projection must be an import/export result")
        if self.projection.get("framework") != self.framework:
            raise ValueError("projection framework does not match evidence framework")
        if not isinstance(self.retrieval, RetrievalProviderEvidence):
            raise TypeError("retrieval must be RetrievalProviderEvidence")
        for field in ("import_ms", "export_ms", "dependency_count",
                      "installed_bytes", "teardown_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        for field in ("residual_resources", "duplicate_authorities"):
            values = tuple(getattr(self, field))
            if len(values) != len(set(values)) or any(
                    not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{field} must contain unique non-empty strings")
            object.__setattr__(self, field, tuple(sorted(values)))


def compare_rag_frameworks(*, snapshot: DatasetSnapshot,
                           cases: Sequence[RetrievalCase],
                           evidence: Sequence[RAGFrameworkEvidence]) -> dict[str, Any]:
    """Compare supplied evidence; never import or invoke either framework."""
    evidence = tuple(evidence)
    if len(evidence) != 2 or {item.framework for item in evidence} != set(FRAMEWORKS):
        raise ValueError("comparison requires exactly one llamaindex and one haystack evidence set")
    retrieval = compare_retrieval(RetrievalComparisonFixture(
        snapshot=snapshot, cases=tuple(cases),
        evidence=tuple(item.retrieval for item in evidence)))
    frameworks: dict[str, Any] = {}
    for item in sorted(evidence, key=lambda value: value.framework):
        projection_gaps = copy.deepcopy(list(item.projection.get("gaps") or []))
        frameworks[item.framework] = {
            "projection": {
                "ok": bool(item.projection.get("ok")),
                "lossy": bool(item.projection.get("lossy")),
                "gap_count": len(projection_gaps),
                "blocking_gap_count": sum(bool(gap.get("blocking"))
                                          for gap in projection_gaps),
                "gaps": projection_gaps,
            },
            "retrieval": retrieval["providers"][item.retrieval.profile.provider_id],
            "overhead": {
                "import_ms": item.import_ms, "export_ms": item.export_ms,
                "dependency_count": item.dependency_count,
                "installed_bytes": item.installed_bytes,
                "teardown_ms": item.teardown_ms,
                "residual_resources": list(item.residual_resources),
            },
            "authority": {
                "duplicate_authorities": list(item.duplicate_authorities),
                "passes": not item.duplicate_authorities,
            },
        }
    return {
        "schema": COMPARISON_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "case_ids": retrieval["case_ids"],
        "frameworks": frameworks,
        "winner": None,
        "effect": "none",
        "providers_invoked": False,
        "frameworks_imported": False,
    }
