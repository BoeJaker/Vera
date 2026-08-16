"""Critical-tier tests for M3.4 test-generation pure helpers (vera/evolve/test_gen_core.py).

The capability makes the git diff + LLM call; these functions decide WHICH modules
get tests, WHERE the test file goes, and how to unwrap the LLM's fenced output — the
parts a regression would silently break (wrong import path, tests written for I/O
modules, code buried in a markdown fence). Pure, no app/LLM.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import test_gen_core as tg  # noqa: E402


# ── strip_code_fence ─────────────────────────────────────────────────────────

def test_strips_python_fence():
    assert tg.strip_code_fence("```python\nx = 1\n```") == "x = 1"


def test_strips_bare_fence():
    assert tg.strip_code_fence("```\nx = 1\n```") == "x = 1"


def test_no_fence_returned_unchanged():
    assert tg.strip_code_fence("import os\nx = 1") == "import os\nx = 1"


def test_open_fence_without_close_is_still_unwrapped():
    assert tg.strip_code_fence("```python\nimport os") == "import os"


def test_empty_and_none():
    assert tg.strip_code_fence("") == ""
    assert tg.strip_code_fence(None) == ""


# ── test_target_for ──────────────────────────────────────────────────────────

def test_target_maps_source_to_lowercase_import_and_test_path():
    t = tg.test_target_for("vera/evolve/foo.py")
    assert t["import_name"] == "vera.evolve.foo"      # lowercase namespace (worktree-safe)
    assert t["suggested_test"] == "tests/test_foo.py"
    assert t["stem"] == "foo"


def test_target_normalizes_backslashes():
    t = tg.test_target_for("vera\\board\\board_core.py")
    assert t["import_name"] == "vera.board.board_core"
    assert t["suggested_test"] == "tests/test_board_core.py"


# ── generatable_modules ──────────────────────────────────────────────────────

def test_keeps_only_vera_source_py():
    got = tg.generatable_modules([
        "vera/evolve/a.py",          # keep
        "vera/board/b.py",           # keep
        "docs/readme.md",            # not .py
        "tests/test_a.py",           # a test file
        "vera/evolve/tests/x.py",    # under a tests/ dir
        "vera/evolve/__init__.py",   # package init
        "scripts/tool.py",           # not under vera/
    ])
    assert got == ["vera/evolve/a.py", "vera/board/b.py"]


def test_excludes_existing_test_files_by_name():
    assert tg.generatable_modules(["vera/x/test_thing.py"]) == []


def test_caps_at_max_n_and_dedupes_preserving_order():
    got = tg.generatable_modules(
        ["vera/a.py", "vera/a.py", "vera/b.py", "vera/c.py", "vera/d.py"], max_n=2)
    assert got == ["vera/a.py", "vera/b.py"]


def test_empty_input():
    assert tg.generatable_modules([]) == []
    assert tg.generatable_modules(None) == []
