"""Canonical out-of-tree paths for Fabric revision and Memory evidence state."""

from __future__ import annotations


def _state_paths():
    try:
        from Vera.vera import state_paths
    except ImportError:                              # pragma: no cover
        from .. import state_paths
    return state_paths


def default_memory_revision_path() -> str:
    """One canonical revision journal shared by Fabric and Memory canaries."""
    state_paths = _state_paths()
    path = state_paths.state_dir("fabric") / "record-revisions.sqlite3"
    return str(state_paths.guard_out_of_tree(path))


def default_memory_dual_write_receipt_path() -> str:
    """One payload-free dual-write evidence journal for all import topologies."""
    state_paths = _state_paths()
    path = state_paths.state_dir("fabric") / "memory-dual-write-receipts.sqlite3"
    return str(state_paths.guard_out_of_tree(path))
