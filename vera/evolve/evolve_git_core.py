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


def worktree_paths_by_branch(worktree_list_porcelain: str) -> dict:
    """Map {branch name -> its worktree path} from `git worktree list --porcelain`.

    Lets a promote/deploy route the merge: a target NOT in this map is free for an
    isolated-worktree merge; a target that IS here is checked out live (e.g. prod
    on main at that path), so it takes the guarded in-checkout deploy path instead
    of the blind `git checkout` the old promote used."""
    out: dict = {}
    path = None
    for line in (worktree_list_porcelain or "").splitlines():
        line = line.rstrip()
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip().startswith("branch refs/heads/"):
            name = line.strip()[len("branch refs/heads/"):].strip()
            if name and path:
                out[name] = path
    return out
