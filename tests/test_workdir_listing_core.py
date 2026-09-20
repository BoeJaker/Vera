"""The success-criterion extension gate must see files BELOW the top level.

Census runs 54 and 55 (2026-09-19/20): `build-multifile` writes
`statkit/__init__.py` + `statkit/stats.py`; the gate listed only `/workspace`
(`["statkit"]`), saw no `.py`, and hard-failed every step until the wall cap.
These tests pin the walk that fixes it and the caps that keep it cheap.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.execution.workdir_listing_core import (  # noqa: E402
    files_with_extensions, is_dir_kind, walk_listing,
)


def _lister(tree):
    """tree: {relpath: [(name, is_dir), ...]}; '' is the root. Unknown -> None."""
    calls = []

    async def list_dir(rel):
        calls.append(rel)
        return tree.get(rel)

    list_dir.calls = calls
    return list_dir


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_package_files_one_level_down_are_listed():
    tree = {
        "": [("statkit", True), ("README.md", False)],
        "statkit": [("__init__.py", False), ("stats.py", False), ("tests", True)],
        "statkit/tests": [("test_stats.py", False)],
    }
    got = _run(walk_listing(_lister(tree)))
    # Breadth-first, directories first within a directory.
    assert got == ["statkit/", "README.md", "statkit/tests/", "statkit/__init__.py",
                   "statkit/stats.py", "statkit/tests/test_stats.py"]
    assert files_with_extensions(got, {"py"}) == [
        "statkit/__init__.py", "statkit/stats.py", "statkit/tests/test_stats.py"]


def test_the_census_case_top_level_only_would_have_failed():
    # The exact shape run 55 saw: the top level names ONLY the package dir.
    top_only = ["statkit/"]
    assert files_with_extensions(top_only, {"py"}) == []
    tree = {"": [("statkit", True)], "statkit": [("stats.py", False)]}
    deep = _run(walk_listing(_lister(tree)))
    assert files_with_extensions(deep, {".py"}) == ["statkit/stats.py"]


def test_root_unreadable_is_none_but_a_bad_subdir_is_skipped():
    assert _run(walk_listing(_lister({}))) is None
    tree = {"": [("ok", True), ("broken", True)], "ok": [("a.txt", False)]}
    got = _run(walk_listing(_lister(tree)))
    assert got == ["broken/", "ok/", "ok/a.txt"]


def test_depth_dir_and_name_caps_hold():
    # A chain deeper than max_depth stops; node_modules is never opened;
    # max_dirs bounds how many directories get listed at all.
    tree = {"": [("a", True), ("node_modules", True)],
            "a": [("b", True)], "a/b": [("c", True)], "a/b/c": [("deep.py", False)],
            "node_modules": [("x.js", False)]}
    lst = _lister(tree)
    # max_depth = how many directory levels below the root get OPENED:
    # 2 opens a/ and a/b/, so a/b/c/ is named but never listed.
    got = _run(walk_listing(lst, max_depth=2))
    assert "a/b/" in got and "a/b/c/" in got and "a/b/c/deep.py" not in got
    assert "a/b/c/deep.py" in _run(walk_listing(_lister(tree), max_depth=3))
    assert "node_modules/x.js" not in got
    assert "node_modules" not in lst.calls
    wide = {"": [(f"d{i}", True) for i in range(20)]}
    wide.update({f"d{i}": [(f"f{i}.py", False)] for i in range(20)})
    lst2 = _lister(wide)
    got2 = _run(walk_listing(lst2, max_dirs=4))
    assert len(lst2.calls) == 4                      # root + 3 subdirs
    assert len(files_with_extensions(got2, ["py"])) == 3
    many = {"": [(f"f{i}.txt", False) for i in range(50)]}
    assert len(_run(walk_listing(_lister(many), limit=10))) == 10


def test_hidden_entries_and_extension_matching():
    tree = {"": [(".git", True), (".env", False), ("Doc.MD", False)],
            ".git": [("HEAD", False)]}
    got = _run(walk_listing(_lister(tree)))
    assert got == ["Doc.MD"]
    assert files_with_extensions(got, ["md"]) == ["Doc.MD"]
    assert files_with_extensions(got, []) == []
    assert files_with_extensions(["a/", "a/b.py"], ["py"]) == ["a/b.py"]


def test_is_dir_kind_accepts_both_spellings():
    # The sandbox ls reports 'directory'; the top-level lister was testing
    # for 'dir', so it never marked a directory as one.
    assert is_dir_kind("directory") and is_dir_kind("dir") and is_dir_kind("D")
    assert not is_dir_kind("file") and not is_dir_kind("") and not is_dir_kind(None)
