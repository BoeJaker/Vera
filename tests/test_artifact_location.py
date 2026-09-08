"""A bare filename must not create a second copy at the workspace root.

The registry below is the shape census 22's build-multifile actually held after
cyc14: key = basename, rel flattened to that key, fs_path carrying the truth.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import artifact_location as al  # noqa: E402

pytestmark = pytest.mark.critical


# What _v5_register_artifact stores: rel is the KEY (basename), fs_path is real.
ARTS = {
    "stats.py": {"rel": "stats.py", "fs_path": "/workspace/statkit/stats.py"},
    "__init__.py": {"rel": "__init__.py", "fs_path": "/workspace/statkit/__init__.py"},
    "clock.html": {"rel": "clock.html", "fs_path": "/workspace/clock.html"},
}


# ── the census 22 case ──────────────────────────────────────────────────────

def test_the_bare_filename_is_corrected_to_where_the_run_put_it():
    """cyc15: code.edit('stats.py') saved /workspace/stats.py beside the real
    module, and ten cycles of mv/cp/ls followed."""
    assert al.known_location("stats.py", ARTS) == "statkit/stats.py"


def test_a_path_that_is_already_right_is_left_alone():
    """Correcting a correct path would churn the args for nothing."""
    assert al.known_location("statkit/stats.py", ARTS) == ""


def test_a_file_that_really_does_live_at_the_root_is_left_alone():
    assert al.known_location("clock.html", ARTS) == ""


def test_a_leading_slash_does_not_defeat_the_comparison():
    assert al.known_location("/stats.py", ARTS) == "statkit/stats.py"
    assert al.known_location("/statkit/stats.py", ARTS) == ""


def test_the_note_tells_the_executor_what_to_do_next_time():
    note = al.relocation_note("stats.py", "statkit/stats.py")
    assert "statkit/stats.py" in note
    assert "SECOND copy" in note        # why it matters, not just what happened


# ── it must never invent a location ─────────────────────────────────────────

def test_an_unknown_file_is_never_relocated():
    """Inventing a directory would write over something real."""
    assert al.known_location("brand_new.py", ARTS) == ""


def test_a_record_with_no_fs_path_decides_nothing():
    arts = {"x.py": {"rel": "x.py", "fs_path": ""}}
    assert al.known_location("x.py", arts) == ""


def test_a_record_about_a_different_file_is_ignored():
    """A stale fs_path whose basename no longer matches the key cannot be
    trusted to describe this file."""
    arts = {"x.py": {"rel": "x.py", "fs_path": "/workspace/pkg/y.py"}}
    assert al.known_location("x.py", arts) == ""


def test_an_fs_path_outside_the_workspace_is_ignored():
    """The basename MATCHES here on purpose - otherwise the later basename
    check rejects it and the workspace-root guard is never exercised. A
    relocation to /etc would be a write outside the run."""
    arts = {"x.py": {"rel": "x.py", "fs_path": "/etc/x.py"}}
    assert al.known_location("x.py", arts) == ""


def test_junk_does_not_raise():
    for path in (None, "", "   ", "/", "///"):
        assert al.known_location(path, ARTS) == ""
    for arts in (None, {}, {"stats.py": None}, {"stats.py": "not a dict"}):
        assert al.known_location("stats.py", arts) == ""


def test_workspace_rel_strips_only_the_root():
    assert al.workspace_rel("/workspace/a/b.py") == "a/b.py"
    assert al.workspace_rel("/workspace/b.py") == "b.py"
    assert al.workspace_rel("/other/b.py") == ""
    assert al.workspace_rel("") == ""


def test_a_nested_workspace_directory_survives():
    """A real directory called `workspace` under the root is not the root."""
    assert al.workspace_rel("/workspace/workspace/b.py") == "workspace/b.py"


# ── the call site ───────────────────────────────────────────────────────────

def _src():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "vera", "dag", "dag_workshop_capabilities.py"),
              encoding="utf-8") as fh:
        return fh.read()


def test_the_rule_runs_before_the_others():
    """It must correct the path BEFORE the proven-file and code-write rules
    judge it, or they decide about a path that does not exist."""
    src = _src()
    body = src[src.index("def _v5_route_write_call("):]
    body = body[:body.index("\ndef ", 10)]
    assert body.index("known_location(") < body.index("RULE 1")


def test_the_relocation_is_reported_even_when_no_other_rule_fires():
    """Silently rewriting a path would leave the executor naming the wrong one
    forever - it has to be told."""
    src = _src()
    assert '"kind": "path_relocated"' in src
    assert "relocation_note(" in src


def test_the_caller_state_is_not_mutated():
    """_v5_route_write_call documents that it never mutates caller state; the
    correction copies the args dict."""
    src = _src()
    body = src[src.index("def _v5_route_write_call("):]
    body = body[:body.index("RULE 1")]
    assert "args = dict(args)" in body


def test_the_module_is_imported_before_it_is_used():
    src = _src()
    assert (src.index("import artifact_location as _artifact_location")
            < src.index("def _v5_route_write_call("))


# â”€â”€ one missing file must not become two remediation steps â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Census 45, build-multifile. The goal named the file twice - once with its
# directory, once bare - so the completion gate probed both, found both
# missing, and appended "Create the missing file: /workspace/statkit/stats.py"
# AND "Create the missing file: stats.py". Same file. That goal then spent 17
# cycles on one step, the worst in the run.

def test_a_bare_name_is_dropped_when_the_located_one_is_present():
    got = al.dedupe_named_paths(["/workspace/statkit/stats.py", "stats.py"])
    assert got == ["/workspace/statkit/stats.py"]


def test_order_does_not_matter():
    got = al.dedupe_named_paths(["stats.py", "/workspace/statkit/stats.py"])
    assert got == ["/workspace/statkit/stats.py"]


def test_two_real_files_sharing_a_basename_are_BOTH_kept():
    """The confusion this module exists to fix. a/stats.py and b/stats.py are
    two files; collapsing them by basename would lose one."""
    got = al.dedupe_named_paths(["a/stats.py", "b/stats.py"])
    assert got == ["a/stats.py", "b/stats.py"]


def test_a_bare_name_with_no_located_twin_survives():
    got = al.dedupe_named_paths(["clock.html", "notes.md"])
    assert got == ["clock.html", "notes.md"]


def test_exact_duplicates_collapse():
    assert al.dedupe_named_paths(["stats.py", "stats.py"]) == ["stats.py"]


def test_empty_input():
    assert al.dedupe_named_paths([]) == []
    assert al.dedupe_named_paths(None) == []
