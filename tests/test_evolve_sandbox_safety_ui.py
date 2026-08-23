from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


def _panel() -> str:
    return (Path(__file__).parents[1] / "vera" / "evolve" /
            "evolve_panel.html").read_text(encoding="utf-8")


def test_sandbox_panel_consumes_detailed_ownership_and_capacity_read_model():
    panel = _panel()
    assert "/evolve/sandbox/list?detail=true" in panel
    assert "renderSbxCapacity" in panel
    assert "s.owner||'unknown'" in panel
    assert "s.session_id" in panel
    assert "s.head_commit" in panel
    assert "s.bleeding_edge_commit" in panel
    assert "s.git_worktree.state" in panel
    assert "s.merged_to_bleeding_edge" in panel


def test_sandbox_safety_view_uses_read_only_preflight_for_each_action():
    panel = _panel()
    assert "const actions=['restart','stop','reuse','remove']" in panel
    assert "/evolve/sandbox/preflight?name=" in panel
    assert "All decisions are read-only dry runs" in panel
    assert "Refusal reasons" in panel


def test_spawned_down_requires_preflight_and_dry_run_and_preserves_worktree():
    panel = _panel()
    start = panel.index("async function sandboxDownNamed(name)")
    end = panel.index("let _sbxTarget", start)
    function = panel[start:end]
    assert "action=stop" in function
    assert "dry_run:true,remove_worktree:false" in function
    assert "dry.mutated!==false" in function
    assert "remove_worktree:false" in function
    assert "Worktree: PRESERVE" in function
    assert function.index("action=stop") < function.index("dry_run:true")
    assert function.index("dry_run:true") < function.index("confirm(")
    assert function.index("confirm(") < function.rindex("/evolve/sandbox/down")
