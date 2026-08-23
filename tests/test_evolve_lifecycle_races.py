import asyncio

import pytest

from vera.evolve import evolve_capabilities as evolve


pytestmark = pytest.mark.critical


def test_release_ref_change_after_preflight_refuses_before_merge(monkeypatch):
    shell_calls = []
    git_tips = iter(["branch-moved", "into-original"])

    async def shell(argv, cwd=None, timeout=60):
        shell_calls.append(argv)
        joined = " ".join(argv)
        if "symbolic-ref" in joined:
            return {"ok": True, "out": "main", "err": "", "code": 0}
        if "status --porcelain" in joined:
            return {"ok": True, "out": "", "err": "", "code": 0}
        raise AssertionError(f"mutation crossed the race guard: {argv}")

    async def git(*_args, **_kwargs):
        return {"ok": True, "out": next(git_tips), "err": "", "code": 0}

    monkeypatch.setattr(evolve, "_sh", shell)
    monkeypatch.setattr(evolve, "_git", git)

    result = asyncio.run(evolve._release_fast_forward_in_checkout(
        "/repo", "bleeding-edge", "main", "/repo",
        "branch-original", "into-original"))

    assert result["ok"] is False
    assert result["ref_changed"] is True
    assert "nothing was changed" in result["error"]
    assert all(" merge " not in f" {' '.join(call)} " for call in shell_calls)


def test_mirror_refresh_race_fails_without_forcing_ref_or_removing_worktree(
        monkeypatch, tmp_path):
    mirror_path = (tmp_path / ".loop-lab-worktrees" /
                   "bleeding-edge-mirror")
    mirror_path.mkdir(parents=True)
    (mirror_path / ".git").write_text("gitdir: preserved", encoding="utf-8")
    git_calls = []
    shell_calls = []

    async def git(*args, **_kwargs):
        git_calls.append(args)
        return {"ok": True, "out": "present", "err": "", "code": 0}

    async def shell(argv, cwd=None, timeout=60):
        shell_calls.append(argv)
        joined = " ".join(argv)
        if "status --porcelain" in joined:
            return {"ok": True, "out": "", "err": "", "code": 0}
        if "merge --ff-only" in joined:
            return {"ok": False, "out": "", "err": "not fast-forwardable", "code": 1}
        raise AssertionError(f"unexpected command: {argv}")

    async def must_not_remove(*_args, **_kwargs):
        raise AssertionError("race failure attempted worktree removal")

    monkeypatch.setattr(evolve, "_git", git)
    monkeypatch.setattr(evolve, "_sh", shell)
    monkeypatch.setattr(evolve, "_remove_worktree_robust", must_not_remove)

    result = asyncio.run(evolve._refresh_loop_lab_mirror(
        "loop-lab/bleeding-edge-mirror", "bleeding-edge", repo_root=tmp_path))

    assert result == {"ok": False, "reason": "not fast-forwardable"}
    assert mirror_path.exists()
    assert not any(call and call[0] == "branch" for call in git_calls)
    assert len(shell_calls) == 2
