"""
evolve_git_core.py — pure git helpers for Loop Lab (unit-testable, no app boot)
==============================================================================

Stdlib-only parsing/decision helpers so the safety-critical logic behind
`evolve.sandbox.approve` can be tested without booting Vera (same pattern as
dag/planner_core.py, ide/ws_changes_core.py).
"""

from __future__ import annotations


def branches_checked_out(worktree_list_porcelain: str) -> set:
    """The set of branch names currently checked out by SOME git worktree, parsed
    from `git worktree list --porcelain`.

    Used as a hard safety gate: approval must never advance a branch that a live
    worktree (e.g. prod's running checkout) has checked out, because moving that
    ref changes files under a running process. Detached worktrees contribute no
    branch (a `detached` line, no `branch` line) and are correctly ignored.
    """
    out: set = set()
    for line in (worktree_list_porcelain or "").splitlines():
        line = line.strip()
        if line.startswith("branch refs/heads/"):
            name = line[len("branch refs/heads/"):].strip()
            if name:
                out.add(name)
    return out
