"""Inspect snapshot retention: the newest keep_min always stay, anything younger
than keep_days stays, a snapshot a dream review names stays, and the rest go."""
import ast
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "vera", "ide", "ide_inspect_capabilities.py")

pytestmark = pytest.mark.critical

DAY = 86400
NOW = 1_789_300_000


def candidates():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_prune_candidates")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["_prune_candidates"]


def snaps(ages_days):
    return [{"id": f"s{i:03d}", "mtime": NOW - age * DAY} for i, age in enumerate(ages_days)]


def test_old_snapshots_beyond_the_newest_go():
    doomed = candidates()(snaps([1, 2, 30, 40, 50]), NOW, 14, 2, set())
    assert doomed == ["s002", "s003", "s004"]


def test_the_newest_keep_min_stay_even_when_old():
    assert candidates()(snaps([100, 200, 300]), NOW, 14, 3, set()) == []
    assert candidates()(snaps([100, 200, 300]), NOW, 14, 2, set()) == ["s002"]


def test_young_and_protected_snapshots_stay():
    doomed = candidates()(snaps([1, 3, 5, 60, 90]), NOW, 14, 1, {"s004"})
    assert doomed == ["s003"]


def test_the_snapshot_capability_prunes_after_each_new_snapshot():
    src = open(SRC, encoding="utf-8").read()
    assert "asyncio.ensure_future(_auto_prune_snapshots())" in src
    assert '"ide.inspect.prune_snapshots"' in src
