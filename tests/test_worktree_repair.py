import subprocess
import shutil

import pytest
import vera.evolve.worktree_repair as repair_module

from vera.evolve.worktree_repair import (
    plan_severed_worktree_repair, repair_severed_worktree)


pytestmark = pytest.mark.critical


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, text=True,
                          capture_output=True, check=True).stdout.strip()


def severed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "keep.txt").write_text("base", encoding="utf-8")
    (root / "delete.txt").write_text("delete", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "branch", "feat/repair-test")
    worktree = root / ".loop-lab-worktrees" / "feat-repair-test"
    worktree.parent.mkdir()
    git(root, "worktree", "add", str(worktree), "feat/repair-test")
    (worktree / "keep.txt").write_text("edited", encoding="utf-8")
    (worktree / "delete.txt").unlink()
    (worktree / "untracked.txt").write_text("untracked", encoding="utf-8")
    (worktree / "ignored.bin").write_bytes(b"ignored-but-important")
    admin = root / ".git" / "worktrees" / "feat-repair-test"
    shutil.rmtree(admin)
    return root, worktree


def test_dry_run_is_exact_and_non_mutating(tmp_path):
    root, worktree = severed(tmp_path)
    plan = plan_severed_worktree_repair(root, "feat/repair-test")
    assert plan["worktree"] == str(worktree.resolve())
    assert plan["dry_run"] is True and plan["mutated"] is False
    assert (worktree / "untracked.txt").read_text() == "untracked"
    assert (worktree / "ignored.bin").read_bytes() == b"ignored-but-important"


def test_repair_preserves_edits_deletions_and_untracked_files(tmp_path):
    root, worktree = severed(tmp_path)
    result = repair_severed_worktree(root, "feat/repair-test")
    assert result["ok"] is True and result["quarantine_retained"] is True
    assert (worktree / "keep.txt").read_text() == "edited"
    assert not (worktree / "delete.txt").exists()
    assert (worktree / "untracked.txt").read_text() == "untracked"
    assert [line.strip() for line in
            git(worktree, "status", "--porcelain").splitlines()] == [
        "D delete.txt", "M keep.txt", "?? untracked.txt"]


def test_refuses_healthy_registered_or_unknown_targets(tmp_path):
    root, worktree = severed(tmp_path)
    repair_severed_worktree(root, "feat/repair-test")
    with pytest.raises(ValueError, match="already registered"):
        plan_severed_worktree_repair(root, "feat/repair-test")
    with pytest.raises(ValueError, match="does not exist"):
        plan_severed_worktree_repair(root, "feat/missing")


def test_failed_overlay_restores_original_source_and_retains_failed_copy(
        tmp_path, monkeypatch):
    root, worktree = severed(tmp_path)
    def fail_overlay(source, target):
        (target / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected overlay failure")
    monkeypatch.setattr(repair_module, "_copy_overlay", fail_overlay)
    with pytest.raises(RuntimeError, match="injected overlay"):
        repair_severed_worktree(root, "feat/repair-test")
    assert (worktree / "keep.txt").read_text() == "edited"
    assert (worktree / "untracked.txt").read_text() == "untracked"
    assert git(root, "worktree", "list", "--porcelain").count(
        "branch refs/heads/feat/repair-test") == 0
    probe = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                           text=True, capture_output=True)
    assert probe.returncode != 0
    failed = list((worktree.parent / ".quarantine").glob("*-failed-recreate-*"))
    assert len(failed) == 1 and (failed[0] / "partial.txt").read_text() == "partial"


# The mount check (2026-09-28): one batched inspect over every container.
FULL_A = "a" * 64
FULL_B = "b" * 64


def test_mount_check_reads_one_batched_inspect():
    out = "%s\t/repo/.loop-lab-worktrees/feat-x\t/data\n%s\n" % (FULL_A, FULL_B)
    mounts, why = repair_module.mounts_from_inspect(
        [FULL_A[:12], FULL_B[:12]], out, "", 0)
    assert why == ""
    assert "/repo/.loop-lab-worktrees/feat-x" in mounts[FULL_A]
    assert mounts[FULL_B] == set()


def test_a_container_gone_since_ps_mounts_nothing():
    out = "%s\t/data\n" % FULL_A
    err = "Error: No such object: %s\n" % FULL_B[:12]
    mounts, why = repair_module.mounts_from_inspect(
        [FULL_A[:12], FULL_B[:12]], out, err, 1)
    assert why == "" and set(mounts) == {FULL_A}


def test_any_other_docker_error_still_refuses_and_says_why():
    mounts, why = repair_module.mounts_from_inspect(
        [FULL_A[:12]], "", "Cannot connect to the Docker daemon", 1)
    assert mounts is None and "Cannot connect" in why
    # A listed container the inspect never mentions is unknown ownership too.
    mounts, why = repair_module.mounts_from_inspect(
        [FULL_A[:12], FULL_B[:12]], "%s\n" % FULL_A, "", 0)
    assert mounts is None and FULL_B[:12] in why
    # A failing exit with no stderr at all is not a pass.
    mounts, why = repair_module.mounts_from_inspect([FULL_A[:12]], "%s\n" % FULL_A, "", 1)
    assert mounts is None
