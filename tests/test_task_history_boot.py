"""The task-history caps must load with the app and read the census archive.

Runs in-container (the merge gate's runner), where Vera.vera resolves to THIS
checkout; skips on a host venv where it resolves elsewhere.
"""
import asyncio
import json
import os
import pathlib

import pytest

try:
    from Vera.vera import capability_orchestration as ORCH
    from Vera.vera.census import census_capabilities as CC
    from Vera.vera.evolve import task_history_capabilities as TH
except Exception:                                    # pragma: no cover
    ORCH = CC = TH = None

_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(TH is not None and
                  os.path.realpath(getattr(TH, "__file__", "")).startswith(_HERE_ROOT))
pytestmark = pytest.mark.skipif(not _SAME_TREE, reason="app module not importable from THIS checkout here")


def run(coro):
    return asyncio.run(coro)


def test_caps_are_registered():
    for name in ("evolve.results", "evolve.task.history", "evolve.tasks.overview"):
        assert name in ORCH.CAPABILITY_REGISTRY, name


def test_history_reads_the_archive_and_survives_no_redis(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(CC, "CENSUS_DIR", pathlib.Path(tmp_path))
    CC._CACHE.clear()
    rows = [{"id": "build-multifile", "session_id": "s-%d" % i, "status": st, "wall_s": w,
             "template": "default", "quality": {"passed": 2, "total": 3},
             "ended_at": "2026-09-%02dT00:00:00Z" % (i + 1)}
            for i, (st, w) in enumerate([("done", 700), ("wall-cap", 1800), ("done", 650)])]
    for i, r in enumerate(rows):
        (tmp_path / ("census.run%d.jsonl" % (47 + i))).write_text(json.dumps(r) + "\n")
    (tmp_path / "census.run46-failed-x.jsonl").write_text(json.dumps(dict(rows[0], session_id="bad")) + "\n")
    # The loader registers modules by bare filename; point the module lookup at
    # the real objects and make the suite store unavailable.
    monkeypatch.setitem(sys.modules, "census_capabilities", CC)
    ev = sys.modules.get("evolve_capabilities")
    if ev is not None:
        monkeypatch.setattr(ev, "_redis", lambda: None)

    h = run(TH.cap_evolve_task_history(id="census-default-build-multifile"))
    assert h["stats"]["runs"] == 3, h["stats"]           # the failed archive is excluded
    assert h["stats"]["capped"] == 1 and h["stats"]["ok"] == 2
    assert [r["driver"]["id"] for r in h["results"]] == ["run49", "run48", "run47"]
    h2 = run(TH.cap_evolve_task_history(id="census-default-build-multifile", include_excluded=True))
    assert h2["stats"]["runs"] == 4

    res = run(TH.cap_evolve_results(template="default"))
    assert res["count"] == 3 and res["sources"]["census"] == 4

    ov = run(TH.cap_evolve_tasks_overview(template="default"))
    ids = [o["task_id"] for o in ov["tasks"]]
    assert "census-default-build-multifile" in ids
    o = next(x for x in ov["tasks"] if x["task_id"] == "census-default-build-multifile")
    assert o["runs"] == 3 and o["last"]["status"] == "done"
