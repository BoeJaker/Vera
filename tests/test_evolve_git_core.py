"""evolve.sandbox.approve safety gate (vera/evolve/evolve_git_core.py).

branches_checked_out parses `git worktree list --porcelain`; approve refuses to
merge into any branch it returns, so a live checkout (e.g. prod's) is never
advanced under a running process. Pure — no git/app needed.
"""

from vera.evolve import evolve_git_core as core


PORCELAIN = """worktree /home/boejaker/Vera
HEAD 1111111111111111111111111111111111111111
branch refs/heads/agentic-loop-improvements-3

worktree /home/boejaker/Vera/.loop-lab-worktrees/phase-a-test-safetynet
HEAD 2222222222222222222222222222222222222222
branch refs/heads/loop-lab/phase-a-test-safetynet

worktree /home/boejaker/Vera/.loop-lab-worktrees/_merge-abc
HEAD 3333333333333333333333333333333333333333
detached
"""


def test_parses_checked_out_branches_including_slashes():
    got = core.branches_checked_out(PORCELAIN)
    assert got == {"agentic-loop-improvements-3", "loop-lab/phase-a-test-safetynet"}


def test_detached_worktree_contributes_no_branch():
    # the _merge-abc worktree is detached → must NOT appear (it has no branch)
    assert not any(b.startswith("_merge") for b in core.branches_checked_out(PORCELAIN))


def test_live_prod_branch_is_flagged_so_approve_refuses_it():
    # the guard's actual use: is prod's branch checked out? -> yes -> approve refuses
    assert "agentic-loop-improvements-3" in core.branches_checked_out(PORCELAIN)
    # an integration branch not checked out anywhere -> safe to merge into
    assert "main" not in core.branches_checked_out(PORCELAIN)


def test_empty_and_blank_are_empty_set():
    assert core.branches_checked_out("") == set()
    assert core.branches_checked_out("\n \n") == set()


def test_worktree_paths_by_branch():
    m = core.worktree_paths_by_branch(PORCELAIN)
    assert m["agentic-loop-improvements-3"] == "/home/boejaker/Vera"
    assert m["loop-lab/phase-a-test-safetynet"] == \
        "/home/boejaker/Vera/.loop-lab-worktrees/phase-a-test-safetynet"
    # detached worktree contributes no branch → its path is not mapped
    assert not any("_merge" in p for p in m.values())
    # a branch NOT in the map is free for an isolated merge; one IN it is a live
    # checkout that promote must merge into in-place (guarded), never git-checkout.
    assert "main" not in m
    assert core.worktree_paths_by_branch("") == {}


# ── tracked_dirty_lines: the in-checkout promote must refuse only on TRACKED WIP,
# never on an unrelated untracked scratch/spec doc left in the merge-target worktree.

def test_untracked_only_is_not_dirty():
    # a lone untracked file (e.g. a user's open spec doc) must NOT block a promote
    porcelain = '?? "documentation/specs/External Agentic-Loop Integration.md"\n'
    assert core.tracked_dirty_lines(porcelain) == []


def test_tracked_modification_is_dirty():
    assert core.tracked_dirty_lines(" M vera/evolve/evolve_capabilities.py\n")
    assert core.tracked_dirty_lines("M  tests/conftest.py\n")   # staged
    assert core.tracked_dirty_lines("A  tests/new_test.py\n")   # staged add
    assert core.tracked_dirty_lines(" D vera/gone.py\n")        # deletion


def test_mixed_keeps_only_tracked():
    porcelain = (
        " M vera/evolve/evolve_capabilities.py\n"
        '?? "docs/scratch.md"\n'
        "?? untracked_dir/\n"
    )
    got = core.tracked_dirty_lines(porcelain)
    assert got == [" M vera/evolve/evolve_capabilities.py"]


def test_ignored_entries_are_not_dirty():
    assert core.tracked_dirty_lines("!! build/artifact.o\n") == []


def test_empty_and_blank_are_not_dirty():
    assert core.tracked_dirty_lines("") == []
    assert core.tracked_dirty_lines("\n  \n") == []


# ── worktree_is_severed: detect the T10 broken-link state from git status stderr

def test_severed_detected_from_status_stderr():
    err = "fatal: not a git repository: /home/x/Vera/.git/worktrees/bleeding-edge-mirror"
    assert core.worktree_is_severed(err) is True


def test_severed_is_case_insensitive():
    assert core.worktree_is_severed("Fatal: NOT A GIT REPOSITORY: ...") is True


def test_healthy_status_is_not_severed():
    assert core.worktree_is_severed("") is False
    assert core.worktree_is_severed(None) is False
    # a normal non-fatal stderr (e.g. a hint) must not read as severed
    assert core.worktree_is_severed("warning: LF will be replaced by CRLF") is False


def test_release_preflight_allows_same_tip_without_a_commit():
    got = core.release_preflight("abc", "abc", True, True)
    assert got == {"ok": True, "action": "already-up-to-date", "error": ""}


def test_release_preflight_requires_fast_forward_history():
    got = core.release_preflight("main", "bleeding", True, False)
    assert got == {"ok": True, "action": "fast-forward", "error": ""}


def test_release_preflight_refuses_main_ahead_without_rewriting_it():
    got = core.release_preflight("main", "bleeding", False, True)
    assert not got["ok"]
    assert got["action"] == "refuse"
    assert "main is ahead" in got["error"]
    assert "Preserve all changes" in got["error"]


def test_release_preflight_refuses_diverged_history_without_auto_merge():
    got = core.release_preflight("main", "bleeding", False, False)
    assert not got["ok"]
    assert "diverged" in got["error"]
    assert "isolated reviewed pipeline" in got["error"]
