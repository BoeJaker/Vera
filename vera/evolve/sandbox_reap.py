"""Pure classification for sandbox/worktree cleanup (evolve.sandbox.prune).

Kept free of I/O so it is unit-testable: the capability layer gathers the
live facts (git worktree list, which containers are running, which branches
are fully merged) and hands them here; this module decides — with no side
effects — what may be safely reaped, what must be kept, and what needs a
human decision. Nothing here ever deletes anything.

Safety model (why each bucket exists):
  • keep   — the main/primary repo checkout, or any worktree whose sandbox
             container is CURRENTLY RUNNING (deleting it would pull the rug
             from under a live sandbox), or an explicitly protected branch.
  • reap   — a `.loop-lab-worktrees/<x>` worktree with NO live container whose
             branch is fully merged into base (unique-commit count 0): removing
             it loses no work. Safe to auto-remove.
  • review — a leftover worktree with UNMERGED commits: it may hold work that
             was never landed, so it is NEVER auto-removed — surfaced for the
             user to decide. This is the prove-redundant-before-delete rule.
"""
from typing import Any, Dict, Iterable, List, Optional

WORKTREE_MARK = ".loop-lab-worktrees"

# Trunk / mirror refs the sweep must NEVER reap or delete, regardless of merge
# status. After a release, `bleeding-edge` has 0 unique commits vs `main` (it's an
# ancestor) and therefore looks "merged" — which once let the hourly scaffolding
# sweep force-delete the ENTIRE trunk: the bleeding-edge branch (via its reaped
# worktree + delete_branches) and the loop-lab mirror (via delete_merged_branches).
# That silently broke every agent's pipeline, because _default_pipeline_base falls
# back to `main` when refs/heads/bleeding-edge is gone. These are infrastructure
# refs, not disposable feature worktrees; protect them unconditionally, here in the
# pure core, so the guarantee holds no matter what the caller passes.
TRUNK_PROTECTED_BRANCHES = frozenset({
    "bleeding-edge", "main", "master",
    "loop-lab/bleeding-edge-mirror", "loop-lab/mainline-mirror",
})


def is_trunk_protected(branch: str) -> bool:
    """True if `branch` is a trunk/mirror ref the sweep must never reap or delete."""
    return (branch or "").strip() in TRUNK_PROTECTED_BRANCHES


def _norm(p: Optional[str]) -> str:
    return (p or "").replace("\\", "/").rstrip("/")


def plan_reap(
    *,
    worktrees: Iterable[Dict[str, Any]],
    protected_paths: Iterable[str] = (),
    merged_branches: Iterable[str] = (),
    protected_branches: Iterable[str] = (),
    dirty_paths: Iterable[str] = (),
    base_branch: str = "main",
) -> Dict[str, List[Dict[str, Any]]]:
    """Classify git worktrees into keep / reap / review.

    worktrees:          [{path, branch, is_main}] from `git worktree list`.
    protected_paths:    worktree paths with a LIVE sandbox container — never touched.
    merged_branches:    branches proven to have 0 unique commits vs base.
    protected_branches: extra branch names to always keep (e.g. an active dev branch).
    dirty_paths:        worktree paths with UNCOMMITTED changes — routed to review,
                        never reaped, even when the branch is fully merged. This is
                        the guard that stops a "merged" verdict (which only sees
                        COMMITTED history) from clobbering work-in-progress a user
                        left uncommitted in a worktree.

    Returns {"keep": [...], "reap": [...], "review": [...]} where each entry is
    {path, branch, reason}. Only `reap` entries are safe to auto-remove.
    """
    protected = {_norm(p) for p in protected_paths}
    merged = {(b or "").strip() for b in merged_branches}
    prot_branches = {(b or "").strip() for b in protected_branches}
    dirty = {_norm(p) for p in dirty_paths}
    keep: List[Dict[str, Any]] = []
    reap: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []

    for w in worktrees:
        path = _norm(w.get("path"))
        branch = (w.get("branch") or "").strip()
        entry = {"path": w.get("path"), "branch": branch}
        # never in scope: the main repo checkout or anything outside loop-lab
        if w.get("is_main") or WORKTREE_MARK not in path:
            keep.append({**entry, "reason": "main/primary repo checkout"})
            continue
        if path in protected:
            keep.append({**entry, "reason": "live sandbox container"})
            continue
        if branch and (branch in prot_branches or is_trunk_protected(branch)):
            keep.append({**entry, "reason": "protected branch"})
            continue
        # uncommitted changes trump a merged verdict — never silently discard WIP
        if path in dirty:
            review.append({**entry,
                           "reason": "uncommitted changes — manual decision required"})
            continue
        if branch and branch in merged:
            reap.append({**entry,
                         "reason": f"merged into {base_branch} — 0 unique commits"})
        else:
            review.append({**entry,
                           "reason": "unmerged commits — manual decision required"})
    return {"keep": keep, "reap": reap, "review": review}


def orphan_composes(
    *,
    compose_files: Iterable[str],
    live_composes: Iterable[str],
) -> List[str]:
    """Auto-generated per-branch compose files (docker-compose.dev-<slug>.yml)
    with no matching live pool entry — safe to remove. `live_composes` is the
    set of compose filenames referenced by current pool descriptors."""
    live = {(c or "").strip() for c in live_composes}
    out = []
    for f in compose_files:
        name = (f or "").strip()
        if name and name not in live:
            out.append(name)
    return out
