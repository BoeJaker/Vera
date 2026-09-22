# -*- coding: utf-8 -*-
"""code.sources (EXPLODE.md §8.4, S6g): the repo's own tree, so a reader picks a thing to explode instead of
typing its path -- "a drop selector ... for veras source in-whole or in-part (i.e. 1 folder or file or the entire
thing)" (owner, 2026-09-22). Every choice carries its SIZE, because "the entire thing" is over a thousand files.
"""
import asyncio

from vera.research import explode_capabilities as X


def run(**kw):
    return asyncio.get_event_loop().run_until_complete(X.cap_code_sources(**kw))


def test_the_whole_tree_is_folders_and_files_each_with_its_size():
    d = run()
    assert d["ok"] and d["counts"]["files"] > 200 and d["counts"]["folders"] > 20
    assert not d["counts"]["capped"]
    by = {f["path"]: f for f in d["folders"]}
    assert "vera/research" in by and by["vera/research"]["files"] >= 8
    assert by["vera/research"]["label"].startswith("vera/research")
    assert "(" in by["vera/research"]["label"]                 # the count is in the label the picker shows
    one = next(f for f in d["files"] if f["path"] == "vera/research/assess_core.py")
    assert one["lang"] == "python" and one["bytes"] > 1000


def test_a_folder_lists_only_what_is_under_it():
    d = run(path="vera/research")
    assert d["ok"] and d["root"] == "vera/research"
    assert all(f["path"].startswith("vera/research/") for f in d["files"])
    assert any(f["path"].endswith("code_explode_core.py") for f in d["files"])
    assert len(d["files"]) < run()["counts"]["files"]


def test_the_noise_of_a_working_tree_is_not_source():
    paths = [f["path"] for f in run()["files"]]
    for junk in ("node_modules", "__pycache__", "/.git/", ".loop-lab-worktrees", "site-packages"):
        assert not any(junk in p for p in paths), junk
    assert all(not p.startswith(".") for p in paths)


def test_it_never_leaves_the_repo_and_says_so():
    for bad in ("../../etc", "/etc", "nope/nope"):
        d = run(path=bad)
        assert d.get("error") and "repo" in d["error"]


def test_the_cap_is_a_cap_and_is_reported():
    d = run(max_files=5)
    assert d["counts"]["files"] == 5 and d["counts"]["capped"] is True


def test_every_file_it_offers_can_actually_be_exploded():
    """The picker's whole job is that what it offers works: each path must pass the reader code.explode uses."""
    d = run(path="vera/research")
    read = X._read_repo_files([f["path"] for f in d["files"]], 40, 2000000)
    assert len(read["sources"]) == len(d["files"]) and not read["skipped"]
