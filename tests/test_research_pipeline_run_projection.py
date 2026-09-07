import pytest
from pathlib import Path

from Vera.vera.research import pipeline_run_projection as projection


pytestmark = pytest.mark.critical


def test_parent_and_children_follow_shared_run_lifecycle_without_content():
    observer = projection.ResearchPipelineRunProjection(
        "run-1", "sha256:abc", ["stage-1-acquire", "stage-2-finish"])
    observer.start()
    observer.stage_start(0, "research")
    observer.stage_done(0, ok=True, citation_count=3, native_job_id="job-1")
    observer.stage_start(1, "synthesis")
    observer.stage_done(1, ok=False)
    observer.finish("error")
    value = observer.to_dict()
    assert value["run"]["status"] == "failed"
    assert [child["status"] for child in value["children"]] == ["completed", "failed"]
    assert value["run"]["workflow_id"] == "sha256:abc"
    assert value["children"][0]["task_id"] == "stage-1-acquire"
    assert "citation_count" in str(value)
    assert "topic" not in str(value)
    assert "prompt" not in str(value)
    assert "result" not in str(value)


def test_cancellation_closes_running_child_and_parent():
    observer = projection.ResearchPipelineRunProjection(
        "run-2", "sha256:def", ["stage-1-transform"])
    observer.stage_start(0, "transform")
    observer.finish("cancelled")
    value = observer.to_dict()
    assert value["run"]["status"] == "cancelled"
    assert value["children"][0]["status"] == "cancelled"


def test_duplicate_or_out_of_range_stages_fail_closed():
    observer = projection.ResearchPipelineRunProjection(
        "run-3", "sha256:ghi", ["stage-1-research"])
    with pytest.raises(ValueError):
        observer.stage_start(2, "research")
    observer.stage_start(0, "research")
    with pytest.raises(ValueError):
        observer.stage_start(0, "research")


def test_registry_snapshot_and_drop_are_explicit():
    projection.create("run-4", "sha256:jkl", ["stage-1-research"])
    assert projection.get("run-4")["run"]["status"] == "queued"
    projection.drop("run-4")
    assert projection.get("run-4") is None


def test_untrusted_native_job_id_is_not_copied_into_metadata():
    observer = projection.ResearchPipelineRunProjection(
        "run-5", "sha256:mno", ["stage-1-research"])
    observer.stage_start(0, "research")
    observer.stage_done(0, ok=True, native_job_id="secret payload with spaces")
    assert "secret payload" not in str(observer.to_dict())


def test_native_runner_wires_projection_without_changing_default_run_shape():
    source = (Path(__file__).parents[1] / "vera" / "research" /
              "researcher_api.py").read_text(encoding="utf-8")
    assert "_pipeline_run_observer.create(" in source
    assert "_pipeline_run_observer.stage_start(" in source
    assert "_pipeline_run_observer.stage_done(" in source
    assert "include_run_protocol: bool = False" in source
