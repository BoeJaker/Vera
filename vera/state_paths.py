# ============================================================================
# state_paths.py — the out-of-tree state/output boundary for Vera
# ============================================================================
#
# Machine-cadence outputs (build artifacts, render exports, board/notebook live
# state, dream/media outputs, …) MUST NOT be written inside the tracked repo
# tree. Prod runs from a live checkout, and a dirty working tree makes the safe
# promote correctly REFUSE the merge — which blocks EVERY promote, not just the
# author's (documentation/specs/dev-lifecycle-and-repo-hygiene.md §8.2 #7; the
# `docs.build` incident that found this the hard way). The Agent Boards & Comms
# plan calls a live agent board "the same hazard, worse" (machine cadence) and
# its Stage 0 (§9.0) is blocked on exactly this boundary existing.
#
# This module centralises the one place such output goes — a single state root
# OUTSIDE the repo (VERA_STATE_DIR) — plus `guard_out_of_tree()`, which makes a
# mis-pointed output path fail LOUDLY at the write instead of silently dirtying
# prod. A silently-dirtied tree looks identical to a healthy one until the next
# promote is refused, so the guard converts a latent, shared-blast-radius hazard
# into an immediate, local error.
#
# NOT for versioned artifacts Vera legitimately authors (docs / skills / notes /
# plans). Those are a SEPARATE concern — a content-edit surface that stages them
# via a branch + commit, never a raw write into prod's live checkout. Do not
# route those through here.
# ============================================================================

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "repo_root", "state_root", "state_dir",
    "build_output_dir", "render_output_dir", "board_dir", "notebook_dir",
    "media_dir", "is_under_repo", "would_dirty_tree", "guard_out_of_tree",
]

# vera/state_paths.py → parents[0] is the `vera` package dir, parents[1] the repo
# root (the dir that holds `.git`). Computed from __file__ so it is correct for a
# main checkout AND for a per-branch worktree under .loop-lab-worktrees/.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Default state root: a sibling of the repo, never inside it. Override with
# VERA_STATE_DIR (e.g. object-store mount, a different disk, the SMB share).
_DEFAULT_STATE_ROOT = Path.home() / "vera-state"


def repo_root() -> Path:
    """Absolute path to the tracked repo root (holds .git)."""
    return _REPO_ROOT


def state_root() -> Path:
    """The out-of-tree state root, created if missing. VERA_STATE_DIR wins;
    otherwise ~/vera-state. Resolved lazily so tests + runtime env changes both
    take effect."""
    raw = os.getenv("VERA_STATE_DIR", "").strip()
    root = Path(raw).expanduser() if raw else _DEFAULT_STATE_ROOT
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_dir(*parts: str) -> Path:
    """A subdirectory under the state root, created if missing.
    `state_dir("build")` → <state_root>/build."""
    d = state_root().joinpath(*parts) if parts else state_root()
    d.mkdir(parents=True, exist_ok=True)
    return d


# Well-known output areas. Callers use these instead of inventing an in-tree dir.
def build_output_dir() -> Path:  return state_dir("build")
def render_output_dir() -> Path: return state_dir("render")
def board_dir() -> Path:         return state_dir("board")
def notebook_dir() -> Path:      return state_dir("notebook")
def media_dir() -> Path:         return state_dir("media")


def is_under_repo(path) -> bool:
    """True iff `path` resolves to somewhere inside the tracked repo tree.
    Component-aware (a sibling like `<repo>-state` is NOT under `<repo>`)."""
    try:
        Path(path).resolve().relative_to(_REPO_ROOT)
        return True
    except ValueError:
        return False


def would_dirty_tree(path) -> bool:
    """True iff writing `path` would land inside the tracked repo tree. This is
    the hazard §8.2 #7 describes — the moment a machine writer's target is inside
    the checkout, a stray/mistakenly-tracked file blocks every promote."""
    return is_under_repo(path)


def guard_out_of_tree(path):
    """Refuse a machine-output path that would land inside the tracked repo tree.
    Call this in a writer's path BEFORE writing, so a mis-pointed output fails
    loudly here instead of silently dirtying prod. Returns the resolved path when
    it is safe (outside the repo)."""
    p = Path(path).resolve()
    if is_under_repo(p):
        raise ValueError(
            "refusing to write machine output inside the tracked repo tree: "
            f"{p} (repo root {repo_root()}). Write under VERA_STATE_DIR "
            f"({state_root()}) instead — see dev-lifecycle-and-repo-hygiene.md "
            "§8.2 #7 (a dirty tree blocks every promote)."
        )
    return p
