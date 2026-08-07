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
