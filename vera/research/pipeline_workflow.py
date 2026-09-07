"""Deterministic Workflow IR projection of native research pipelines."""
from __future__ import annotations

import re
from typing import Any

from Vera.vera.execution.workflow_ir import IR_VERSION, normalize_workflow


SCHEMA = "vera.research-pipeline-workflow/v1"
_KINDS = {
    "research": "research.acquire-and-synthesize",
    "transform": "research.transform",
    "synthesis": "research.synthesize",
}
_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def _step_id(index: int, name: Any) -> str:
    stem = _ID.sub("-", str(name or "stage").strip()).strip("-.").lower()
    return f"stage-{index + 1}-{stem[:48] or 'stage'}"


def project_pipeline_workflow(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Return a validated definition projection; never execute the pipeline."""
    if not isinstance(pipeline, dict):
        raise TypeError("pipeline must be an object")
    stages = pipeline.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("pipeline stages must be a non-empty array")
    if len(stages) > 100:
        raise ValueError("pipeline stages exceed 100")

    steps = []
    gaps = []
    previous = "topic"
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"stage {index} must be an object")
        kind = str(stage.get("kind") or "research").strip().lower()
        if kind not in _KINDS:
            raise ValueError(f"stage {index} has unsupported kind: {kind}")
        sid = _step_id(index, stage.get("name"))
        output = f"stage_{index + 1}_result"
        native = {
            "kind": kind,
            "mode": str(stage.get("mode") or "single"),
            "output_mode": str(stage.get("output_mode") or "report"),
            "model_tier": str(stage.get("model_tier") or "auto"),
            "sources": list(stage.get("sources") or []),
            "nlp_tools": list(stage.get("nlp_tools") or []),
            "query_template": str(stage.get("query_template") or "{topic}"),
            "prompt": str(stage.get("prompt") or ""),
        }
        steps.append({
            "id": sid,
            "type": "task",
            "task": _KINDS[kind],
            "output": output,
            "bindings": {
                "topic": {"kind": "state", "value": "topic"},
                "input": {"kind": "state", "value": previous},
            },
            "extensions": {"vera.research": native},
        })
        previous = output
        gaps.append({
            "step_id": sid,
            "feature": "native_stage_execution",
            "reason": "Workflow IR describes the stage; the native research runner remains authoritative",
        })

    workflow = normalize_workflow({
        "ir_version": IR_VERSION,
        "name": str(pipeline.get("name") or "Research pipeline")[:256],
        "description": str(pipeline.get("description") or "")[:2000],
        "inputs": {"topic": {"schema": {"type": "string"}, "required": True}},
        "outputs": {"result": {"schema": {"type": "string"}}},
        "steps": steps,
        "extensions": {
            "vera.research": {
                "pipeline_id": str(pipeline.get("id") or "")[:128],
                "execution_authority": "native_research_pipeline",
            },
        },
    })
    return {
        "schema": SCHEMA,
        "workflow": workflow,
        "workflow_id": workflow["content_hash"],
        "authoritative": False,
        "execution_authority": "native_research_pipeline",
        "executes": False,
        "gaps": gaps,
    }
