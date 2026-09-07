import copy
from pathlib import Path

import pytest

from Vera.vera.research.pipeline_workflow import project_pipeline_workflow


pytestmark = pytest.mark.critical


def _pipeline():
    return {
        "id": "pipeline-1",
        "name": "Evidence brief",
        "description": "Acquire, edit, and synthesize evidence.",
        "stages": [
            {"name": "Acquire", "kind": "research", "mode": "parallel",
             "sources": ["web"], "query_template": "{topic}"},
            {"name": "Edit", "kind": "transform", "prompt": "Preserve citations"},
            {"name": "Finish", "kind": "synthesis"},
        ],
    }


def test_projection_is_deterministic_valid_and_non_executing():
    source = _pipeline()
    before = copy.deepcopy(source)
    first = project_pipeline_workflow(source)
    second = project_pipeline_workflow(source)
    assert first == second
    assert source == before
    assert first["executes"] is False
    assert first["authoritative"] is False
    assert first["workflow_id"].startswith("sha256:")
    assert [step["task"] for step in first["workflow"]["steps"]] == [
        "research.acquire-and-synthesize", "research.transform", "research.synthesize"]


def test_projection_preserves_native_definition_without_claiming_execution():
    result = project_pipeline_workflow(_pipeline())
    stages = result["workflow"]["steps"]
    assert stages[0]["extensions"]["vera.research"]["sources"] == ["web"]
    assert stages[1]["extensions"]["vera.research"]["prompt"] == "Preserve citations"
    assert len(result["gaps"]) == 3
    assert {gap["feature"] for gap in result["gaps"]} == {"native_stage_execution"}


@pytest.mark.parametrize("stages", [[], ["bad"], [{"kind": "unknown"}]])
def test_malformed_or_unknown_stages_fail_closed(stages):
    pipeline = _pipeline()
    pipeline["stages"] = stages
    with pytest.raises((TypeError, ValueError)):
        project_pipeline_workflow(pipeline)


def test_native_pipeline_get_is_opt_in_and_default_shape_is_unchanged():
    source = (Path(__file__).parents[1] / "vera" / "research" /
              "researcher_api.py").read_text(encoding="utf-8")
    assert "async def get_pipeline(pl_id: str, include_workflow_ir: bool = False)" in source
    assert 'return {**pl, "workflow_ir": project_pipeline_workflow(pl)}' in source
