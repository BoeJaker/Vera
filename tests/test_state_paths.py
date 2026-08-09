"""Tests for the out-of-tree state/output boundary (vera/state_paths.py).

The one invariant that matters: machine-cadence output never lands inside the
tracked repo tree, because a dirty prod checkout makes the safe promote refuse
the merge and blocks EVERY promote (dev-lifecycle §8.2 #7). These tests pin that
invariant, the env override, and the sibling-prefix edge case that a naive
string check would get wrong.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import state_paths  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    """Point VERA_STATE_DIR at a temp dir so tests never create ~/vera-state or
    collide with a real state root."""
    monkeypatch.setenv("VERA_STATE_DIR", str(tmp_path / "state"))
    yield


def test_state_root_is_outside_the_repo():
    root = state_paths.state_root()
    assert root.exists()
    assert not state_paths.is_under_repo(root)


def test_default_state_root_is_outside_the_repo():
    # Even the built-in default (no env) is a sibling of the repo, never inside.
    assert not state_paths.is_under_repo(state_paths._DEFAULT_STATE_ROOT)


def test_state_subdirs_are_created_and_out_of_tree():
    for d in (
        state_paths.build_output_dir(),
        state_paths.render_output_dir(),
        state_paths.board_dir(),
        state_paths.notebook_dir(),
        state_paths.media_dir(),
    ):
        assert d.exists() and d.is_dir()
        assert not state_paths.is_under_repo(d)
        assert d.is_relative_to(state_paths.state_root())


def test_build_output_dir_out_of_tree_regression():
    # This is the hole this change closed: build output used to be vera/build/output
    # (inside the repo, un-gitignored). It must now resolve outside the tree.
    out = state_paths.build_output_dir()
    assert not state_paths.would_dirty_tree(out)


def test_guard_passes_for_state_paths():
    p = state_paths.build_output_dir() / "firmware.bin"
    assert state_paths.guard_out_of_tree(p) == p.resolve()


def test_guard_raises_for_in_tree_path():
    in_tree = state_paths.repo_root() / "vera" / "build" / "output" / "x.bin"
    with pytest.raises(ValueError):
        state_paths.guard_out_of_tree(in_tree)


def test_guard_raises_when_state_dir_is_misconfigured_inside_repo(monkeypatch):
    # A VERA_STATE_DIR pointed inside the repo is itself a misconfiguration; the
    # guard must catch a write there rather than silently dirtying the tree.
    monkeypatch.setenv("VERA_STATE_DIR", str(state_paths.repo_root() / "vera" / "_bad_state"))
    with pytest.raises(ValueError):
        state_paths.guard_out_of_tree(state_paths.state_root() / "out.bin")


def test_is_under_repo_is_component_aware_not_string_prefix():
    # A sibling like "<repo>-state" shares a string prefix with the repo root but
    # is NOT inside it — a naive startswith() check would wrongly flag it.
    sibling = state_paths.repo_root().parent / (state_paths.repo_root().name + "-state")
    assert not state_paths.is_under_repo(sibling)
    inside = state_paths.repo_root() / "vera" / "state_paths.py"
    assert state_paths.is_under_repo(inside)


def test_env_override_respected():
    root = state_paths.state_root()
    assert root.name == "state"  # the tmp_path/"state" set by the fixture
    assert not state_paths.is_under_repo(root)
