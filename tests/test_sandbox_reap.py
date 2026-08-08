"""Unit tests for the pure sandbox-reap classification (evolve.sandbox.prune).

These guard the prove-redundant-before-delete rule: an unmerged worktree must
NEVER land in `reap`, and a live-container worktree must NEVER be removed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve.sandbox_reap import plan_reap, orphan_composes  # noqa: E402


WT = ".loop-lab-worktrees"


def _wt(path, branch, is_main=False):
    return {"path": path, "branch": branch, "is_main": is_main}


def test_main_checkout_always_kept():
    res = plan_reap(worktrees=[_wt("/home/x/Vera", "main", is_main=True)])
    assert res["reap"] == [] and res["review"] == []
    assert res["keep"][0]["reason"].startswith("main")


def test_merged_worktree_is_reapable():
    res = plan_reap(
        worktrees=[_wt(f"/home/x/Vera/{WT}/feat-done", "feat/done")],
        merged_branches=["feat/done"],
    )
    assert [e["branch"] for e in res["reap"]] == ["feat/done"]
    assert res["review"] == []


def test_unmerged_worktree_goes_to_review_not_reap():
    res = plan_reap(
        worktrees=[_wt(f"/home/x/Vera/{WT}/feat-wip", "feat/wip")],
        merged_branches=[],  # NOT merged
    )
    assert res["reap"] == []
    assert [e["branch"] for e in res["review"]] == ["feat/wip"]


def test_live_container_worktree_never_reaped_even_if_merged():
    p = f"/home/x/Vera/{WT}/feat-live"
    res = plan_reap(
        worktrees=[_wt(p, "feat/live")],
        protected_paths=[p],
        merged_branches=["feat/live"],  # merged, but container is LIVE
    )
    assert res["reap"] == [] and res["review"] == []
    assert res["keep"][0]["reason"] == "live sandbox container"


def test_dirty_worktree_never_reaped_even_if_merged():
    # A merged branch whose worktree holds uncommitted changes must NOT be
    # reaped — rev-list only sees committed history; WIP would be lost.
    p = f"/home/x/Vera/{WT}/feat-wip-uncommitted"
    res = plan_reap(
        worktrees=[_wt(p, "feat/wip")],
        merged_branches=["feat/wip"],   # committed history IS merged
        dirty_paths=[p],                # but the worktree is dirty
    )
    assert res["reap"] == []
    assert [e["branch"] for e in res["review"]] == ["feat/wip"]
    assert "uncommitted" in res["review"][0]["reason"]


def test_protected_branch_kept_even_if_merged():
    res = plan_reap(
        worktrees=[_wt(f"/home/x/Vera/{WT}/agentic-2", "agentic-loop-improvements-2")],
        merged_branches=["agentic-loop-improvements-2"],
        protected_branches=["agentic-loop-improvements-2"],
    )
    assert res["reap"] == []
    assert res["keep"][0]["reason"] == "protected branch"


def test_path_normalization_matches_protected_windows_slashes():
    # protected path given with backslashes + trailing slash still matches
    res = plan_reap(
        worktrees=[_wt(f"/home/x/Vera/{WT}/feat-live", "feat/live")],
        protected_paths=[f"\\home\\x\\Vera\\{WT}\\feat-live\\"],
        merged_branches=["feat/live"],
    )
    assert res["reap"] == []
    assert res["keep"][0]["reason"] == "live sandbox container"


def test_mixed_batch_partitions_correctly():
    res = plan_reap(
        worktrees=[
            _wt("/home/x/Vera", "main", is_main=True),
            _wt(f"/home/x/Vera/{WT}/a", "feat/a"),          # merged -> reap
            _wt(f"/home/x/Vera/{WT}/b", "feat/b"),          # unmerged -> review
            _wt(f"/home/x/Vera/{WT}/c", "feat/c"),          # live -> keep
        ],
        protected_paths=[f"/home/x/Vera/{WT}/c"],
        merged_branches=["feat/a", "feat/c"],
    )
    assert [e["branch"] for e in res["reap"]] == ["feat/a"]
    assert [e["branch"] for e in res["review"]] == ["feat/b"]
    assert {e["branch"] for e in res["keep"]} == {"main", "feat/c"}


def test_orphan_composes_flags_only_unreferenced():
    got = orphan_composes(
        compose_files=[
            "docker-compose.dev-feat-a.yml",
            "docker-compose.dev-feat-b.yml",
        ],
        live_composes=["docker-compose.dev-feat-b.yml"],
    )
    assert got == ["docker-compose.dev-feat-a.yml"]
