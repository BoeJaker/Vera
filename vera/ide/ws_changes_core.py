"""
ws_changes_core.py — clobber-safety primitives for Workspace Changes accept
===========================================================================

Pure, stdlib-only, no app/redis dependency (so it is unit-testable without
booting Vera — same pattern as dag/planner_core.py and fabric/curation_core.py).

`ide.workspace.changes.accept` writes a reviewed proposal's file versions back
into a target checkout (for Loop Lab's sandbox review, that target is the base
repo). The danger: if the live target changed AFTER the proposal was built — a
newer edit, a dirty working tree, a proposal made from a stale branch — a blind
write silently CLOBBERS that newer work.

The guard is a compare-and-swap: the proposal records each file's `base_sha`
(hash of the target's bytes at propose time); accept re-hashes the live target
and refuses to write any file that no longer matches. A proposal can then only
ever apply cleanly onto exactly the state it was reviewed against.
"""

from __future__ import annotations

import hashlib


def sha256_file(path: str):
    """sha256 hex of a file's bytes, or None if unreadable/absent."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        return None


def accept_conflict(entry: dict, cur_sha) -> bool:
    """True when writing this proposal file would CLOBBER work and accept must
    therefore SKIP it (never overwrite).

    `cur_sha` = sha256_file(target) at accept time — None if the target is
    currently absent. `entry["base_sha"]` was captured at propose time — None
    for an added file (target absent then), so an add still applies iff the
    target is still absent (None == None).

    A proposal built before base-hash tracking has no `base_sha` key: its base
    cannot be verified, so it is treated as a conflict (refuse) rather than
    risk a clobber — regenerate the proposal to get a verifiable base.
    """
    if "base_sha" not in entry:
        return True
    return entry.get("base_sha") != cur_sha
