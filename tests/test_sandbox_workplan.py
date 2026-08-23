import json

import pytest

from vera.evolve.sandbox_workplan import load_plan, plan_summary, update_plan


pytestmark = pytest.mark.critical


def test_workplan_is_local_revision_guarded_and_linkable(tmp_path):
    created = update_plan(
        tmp_path, branch="feat/example", expected_revision=0,
        title="Improve the thing", description="Observable handoff context",
        status="in_progress", current_step="step-1", owner="codex",
        board_item_ids=["board-1"], notes_refs=["notes:loop-lab/example"],
        steps=[{"id": "step-1", "title": "Inspect", "status": "in_progress"},
               {"id": "step-2", "title": "Verify", "status": "pending"}],
        update_note="Started safely")

    assert created["revision"] == 1
    assert created["path"] == ".vera-work/work-plan.json"
    assert load_plan(tmp_path)["board_item_ids"] == ["board-1"]
    assert plan_summary(created)["step_count"] == 2
    assert plan_summary(created)["coordination_linked"] is True
    assert json.loads((tmp_path / ".vera-work/work-plan.json").read_text())["owner"] == "codex"

    with pytest.raises(ValueError, match="revision_conflict"):
        update_plan(tmp_path, branch="feat/example", expected_revision=0,
                    description="stale overwrite")


def test_workplan_validates_status_and_bounds_history(tmp_path):
    with pytest.raises(ValueError, match="invalid step status"):
        update_plan(tmp_path, branch="feat/example", expected_revision=0,
                    steps=[{"title": "Unsafe", "status": "invented"}])

    plan = update_plan(tmp_path, branch="feat/example", expected_revision=0,
                       update_note="first")
    for revision in range(1, 55):
        plan = update_plan(tmp_path, branch="feat/example", expected_revision=revision,
                           update_note=f"update {revision}")
    assert plan["revision"] == 55
    assert len(plan["updates"]) == 50


def test_workplan_rejects_ambiguous_or_false_completion(tmp_path):
    with pytest.raises(ValueError, match="duplicate plan step id"):
        update_plan(tmp_path, branch="feat/example", expected_revision=0,
                    steps=[{"id": "same", "title": "One"},
                           {"id": "same", "title": "Two"}])
    with pytest.raises(ValueError, match="current_step_not_found"):
        update_plan(tmp_path, branch="feat/example", expected_revision=0,
                    current_step="missing",
                    steps=[{"id": "real", "title": "Real"}])
    with pytest.raises(ValueError, match="complete_plan_has_unfinished_steps"):
        update_plan(tmp_path, branch="feat/example", expected_revision=0,
                    status="complete", current_step="real",
                    steps=[{"id": "real", "title": "Real", "status": "pending"}])
