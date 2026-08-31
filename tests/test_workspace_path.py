"""A write must land where the read looked.

The case below is the one observed live in census run 21, goal build-multifile,
step 3: code.edit read /workspace/statkit/stats.py and wrote
/workspace/workspace/statkit/stats.py, reported ok, and left the container with
two files whose calculate_mode bodies had diverged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import workspace_path as wp  # noqa: E402

pytestmark = pytest.mark.critical


LIVE_PATH = "/workspace/statkit/stats.py"
LIVE_REPO = "statkit"


# ── the live failure ────────────────────────────────────────────────────────

def test_the_run21_call_now_collapses():
    """cycle 12: path absolute, repo given. This is the whole bug."""
    assert wp.should_collapse(LIVE_PATH, LIVE_REPO) is True
    assert wp.collapse_workspace_prefix(LIVE_PATH) == "statkit/stats.py"


def test_the_same_call_without_a_repo_was_always_fine():
    """cycle 13 worked, which is why the two cycles disagreed about the file."""
    assert wp.should_collapse(LIVE_PATH, "") is True


def test_a_repo_that_is_really_a_path_still_collapses():
    """cycle 14 passed repo='/workspace/statkit'. Whatever that means for the
    store scope, it must not change WHERE the file is written."""
    assert wp.should_collapse(LIVE_PATH, "/workspace/statkit") is True


def test_read_and_write_agree_on_the_live_path():
    """The invariant the shadow file broke. The read side collapses
    unconditionally; the write side must reach the same string for this input."""
    read_side = wp.collapse_workspace_prefix(LIVE_PATH)
    write_side = (wp.collapse_workspace_prefix(LIVE_PATH)
                  if wp.should_collapse(LIVE_PATH, LIVE_REPO) else LIVE_PATH)
    assert read_side == write_side == "statkit/stats.py"


# ── the reason the repo guard exists, which must survive ────────────────────

def test_a_relative_workspace_dir_inside_a_repo_is_left_alone():
    """`workspace/` CAN be a real top-level directory in a repo - collapsing it
    there would corrupt the path. This is why the guard was added, and it stays
    for relative paths."""
    assert wp.should_collapse("workspace/main.py", "myrepo") is False


def test_a_relative_workspace_dir_with_no_repo_still_collapses():
    """Without a repo there is no directory it could legitimately be."""
    assert wp.should_collapse("workspace/main.py", "") is True


def test_only_the_leading_segment_matters():
    for p in ("src/workspace/x.py", "a/b/workspace/c.py"):
        assert wp.should_collapse(p, "") is False
        assert wp.collapse_workspace_prefix(p) == p


def test_a_path_that_merely_starts_with_the_word_is_not_a_prefix():
    """`workspaces/` and `workspace.py` are not the workspace root."""
    for p in ("workspaces/x.py", "/workspaces/x.py", "workspace.py"):
        assert wp.should_collapse(p, "") is False
        assert wp.collapse_workspace_prefix(p) == p


# ── absoluteness ────────────────────────────────────────────────────────────

def test_absoluteness_is_judged_on_the_raw_path():
    assert wp.is_absolute_workspace("/workspace/a.py") is True
    assert wp.is_absolute_workspace("workspace/a.py") is False


def test_backslashes_are_normalised_before_judging():
    assert wp.is_absolute_workspace("\\workspace\\a.py") is True
    assert wp.collapse_workspace_prefix("\\workspace\\a.py") == "a.py"


def test_only_one_level_is_collapsed():
    """Collapsing repeatedly would eat a real `workspace/` directory that
    happens to sit under the root."""
    assert wp.collapse_workspace_prefix("/workspace/workspace/x.py") == "workspace/x.py"


def test_junk_does_not_raise():
    for bad in (None, "", 0):
        assert wp.collapse_workspace_prefix(bad) == ""
        assert wp.should_collapse(bad, "") is False
        assert wp.is_absolute_workspace(bad) is False


# ── the call sites ──────────────────────────────────────────────────────────

def _src(*parts):
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_write_side_uses_the_shared_definition():
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    assert "_workspace_path.should_collapse(raw, repo)" in src
    # and the import must come first in the file, not merely before use at runtime
    assert src.index("import workspace_path as _workspace_path") < src.index(
        "def _code_workspace_path")


def test_the_read_side_uses_the_shared_definition():
    """Both read sites - the existence probe and the read itself. If either
    keeps its own regex the two can drift apart again, which is the whole
    failure this fixes."""
    src = _src("vera", "execution", "exec_capabilities.py")
    assert src.count("_collapse_workspace_prefix(pnorm)") == 2
    assert 're.sub(r"^/?workspace/", "", pnorm)' not in src


def test_the_write_side_no_longer_has_its_own_collapse():
    """The old inline version, which is what disagreed with the read."""
    src = _src("vera", "dag", "dag_workshop_capabilities.py")
    body = src[src.index("def _code_workspace_path"):]
    body = body[:body.index("def _code_scope")]
    assert 'parts[0] == "workspace"' not in body.split("# pragma: no cover")[0]
