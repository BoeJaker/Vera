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


# ── M3.6 main-merge guardrail (2026-08-16, after the direct-to-main incident) ──
# The ONE sanctioned path to the real mainline is
# evolve.bleeding_edge.promote_to_main, and only on the user's explicit,
# unambiguous go-ahead. A per-feature evolve.pipeline.adopt/promote with
# to="main" is exactly the accident that broke the "bleeding-edge is a superset
# of main" invariant once. This makes such a merge a DELIBERATE two-key action,
# never a default or a habit.

MAIN_MERGE_SENTINEL = "I-HAVE-EXPLICIT-USER-GO-AHEAD"


def protected_mainline_names(mainline: str) -> set:
    """Branch names a per-feature adopt/promote must refuse to target directly:
    the repo's resolved mainline plus the conventional 'main'/'master', lower-cased.
    'bleeding-edge' and feature branches are never in this set (they land freely)."""
    names = {(mainline or "").strip().lower(), "main", "master"}
    names.discard("")
    return names


def main_merge_refusal(to: str, mainline: str, authorize: str) -> str:
    """Pure guard for evolve.pipeline.adopt/promote. Returns a non-empty refusal
    message when `to` targets the protected mainline WITHOUT the explicit
    authorization sentinel, else '' (the merge is allowed to proceed).

      to='bleeding-edge' / any feature branch          -> ''            (allowed)
      to='main'/'master'/<mainline>, blank/wrong auth   -> loud refusal  (blocked)
      to='main' + authorize==MAIN_MERGE_SENTINEL        -> ''            (deliberate, allowed)

    The sentinel must be passed per-call and deliberately; it is never a default,
    so a habitual or fat-fingered to='main' is refused, not silently honoured.
    Case- and whitespace-insensitive on the target name."""
    target = (to or "").strip().lower()
    if target not in protected_mainline_names(mainline):
        return ""
    if (authorize or "").strip() == MAIN_MERGE_SENTINEL:
        return ""
    return (
        f"REFUSED: '{to}' is the protected mainline. ALL new code lands on "
        f"'bleeding-edge'; main advances ONLY on the user's explicit, unambiguous "
        f"go-ahead, via evolve.bleeding_edge.promote_to_main - never a per-feature "
        f"adopt/promote to main. If you genuinely have that go-ahead, re-call with "
        f"authorize_main='{MAIN_MERGE_SENTINEL}'. "
        f"(HARD RULE / M3.6 - documentation/specs/consolidated-route-forward.md)"
    )


def tracked_dirty_lines(porcelain: str) -> list:
    """Lines from `git status --porcelain` that represent TRACKED changes (staged
    or unstaged) — the work an in-checkout deploy merge could clobber and must
    therefore refuse to run over.

    Untracked files ('??' lines) are deliberately EXCLUDED: `git merge` refuses on
    its own to overwrite an untracked file the merge would touch (so a genuine
    collision still fails safely), while an UNRELATED untracked file — e.g. a
    scratch/spec doc a user left open in the standing bleeding-edge worktree — must
    not block a promote it has nothing to do with. Ignored (' !!') entries, if ever
    surfaced, are treated like untracked. Pure — unit-tested in test_evolve_git_core."""
    out: list = []
    for line in (porcelain or "").splitlines():
        if not line.strip():
            continue
        if line.startswith("??") or line.startswith("!!"):
            continue
        out.append(line)
    return out


def worktree_is_severed(git_status_stderr: str) -> bool:
    """True if a `git status` inside a worktree reports the T10 SEVERED-link
    failure — the worktree's admin dir (.git/worktrees/<name>) was removed while the
    worktree DIR + its .git file survived (e.g. the standing bleeding-edge container
    keeps the bind-mount alive while an unrelated worktree prune/remove strips the
    admin entry). git then errors 'not a git repository: …/.git/worktrees/<name>'.

    A plain `wt.exists() and (wt/.git).exists()` check MISSES this (both still
    exist), so callers must inspect the status stderr. `git worktree repair` cannot
    fix it (the admin dir is gone) — the worktree must be removed + re-added. Pure."""
    return "not a git repository" in (git_status_stderr or "").lower()


def release_preflight(main_sha: str, bleeding_edge_sha: str,
                      main_is_ancestor: bool, bleeding_edge_is_ancestor: bool) -> dict:
    """Choose the only history-preserving bleeding-edge release action.

    A release may be a no-op or a fast-forward. It must never manufacture a
    main-only merge commit, and it must never guess how to combine diverged
    histories. Divergence is reconciled into bleeding-edge through a normal,
    reviewed pipeline before this release is retried.
    """
    if main_sha and main_sha == bleeding_edge_sha:
        return {"ok": True, "action": "already-up-to-date", "error": ""}
    if main_is_ancestor:
        return {"ok": True, "action": "fast-forward", "error": ""}
    reason = ("main is ahead of bleeding-edge" if bleeding_edge_is_ancestor
              else "main and bleeding-edge have diverged")
    return {
        "ok": False,
        "action": "refuse",
        "error": (
            f"REFUSED: {reason}. Preserve all changes by reconciling main into "
            "bleeding-edge through an isolated reviewed pipeline, then retry the "
            "fast-forward-only release."
        ),
    }
