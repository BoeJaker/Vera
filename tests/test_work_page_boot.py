"""The Work page's caps must load with the app; the task editor's save must
keep what the form does not show.

Runs in-container, where Vera.vera resolves to THIS checkout; skips on a
host venv. The upsert test needs a Redis of its own (REDIS_URL, not db 0 -
see test_result_ingest_boot) and removes what it writes.
"""
import asyncio
import json
import os
import pathlib
import uuid

import pytest

try:
    from Vera.vera import capability_orchestration as ORCH
    from Vera.vera.census import census_capabilities as CC
    from Vera.vera.evolve import evolve_capabilities as EV
    from Vera.vera.evolve import task_history_capabilities as TH
except Exception:                                    # pragma: no cover
    ORCH = CC = EV = TH = None

_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(TH is not None and
                  os.path.realpath(getattr(TH, "__file__", "")).startswith(_HERE_ROOT))
pytestmark = pytest.mark.skipif(not _SAME_TREE, reason="app module not importable from THIS checkout here")


def run(coro):
    return asyncio.run(coro)


async def _store():
    url = os.getenv("REDIS_URL") or ""
    if not url:
        return None
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(url, decode_responses=False, socket_connect_timeout=3, socket_timeout=5)
        if int(r.connection_pool.connection_kwargs.get("db") or 0) == 0:
            return None
        if not await r.ping():
            return None
        return r
    except Exception:
        return None


def test_the_caps_are_registered():
    for name in ("evolve.work.drivers", "evolve.work.live", "evolve.task.upsert", "evolve.tasks.overview"):
        assert name in ORCH.CAPABILITY_REGISTRY, name
    import inspect
    assert "merge" in inspect.signature(EV.evolve_task_upsert).parameters


def test_drivers_and_live_read_the_archive_without_a_store(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "census_capabilities", CC)
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setattr(EV, "_redis", lambda: None)
    monkeypatch.setattr(CC, "CENSUS_DIR", pathlib.Path(tmp_path))
    CC._CACHE.clear()
    rows = [{"id": "build-multifile", "session_id": "s-%d" % i, "status": st, "wall_s": w, "template": "default",
             "quality": {"passed": 2, "total": 3}, "ended_at": "2026-09-%02dT00:00:00Z" % (i + 1)}
            for i, (st, w) in enumerate([("done", 700), ("wall-cap", 1800)])]
    for i, r in enumerate(rows):
        (tmp_path / ("census.run%d.jsonl" % (47 + i))).write_text(json.dumps(r) + "\n")
    (tmp_path / "census.run46-failed-x.jsonl").write_text(json.dumps(dict(rows[0], session_id="bad")) + "\n")
    out = run(TH.cap_evolve_work_drivers())
    ids = [(d["kind"], d["id"]) for d in out["drivers"]]
    assert ("census", "run48") in ids and ("census", "run47") in ids and ("census", "run46-failed-x") in ids
    assert out["kinds"].get("census") == 3 and out["templates"] == ["default"]
    assert [d["id"] for d in out["drivers"] if d["kind"] == "census"][:2] == ["run48", "run47"], "newest first"
    assert next(d for d in out["drivers"] if d["id"] == "run46-failed-x")["excluded"] == "failed"
    only = run(TH.cap_evolve_work_drivers(include_excluded=False, kind="census"))
    assert [d["id"] for d in only["drivers"]] == ["run48", "run47"]
    live = run(TH.cap_evolve_work_live())
    assert set(live) >= {"census", "suite", "improve", "run", "any_live"}
    assert live["any_live"] is False and live["improve"] is None


def test_the_overview_carries_the_definitions_facts(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "census_capabilities", CC)
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setattr(EV, "_redis", lambda: None)
    monkeypatch.setattr(CC, "CENSUS_DIR", pathlib.Path(tmp_path))
    CC._CACHE.clear()

    async def _tasks():
        return [{"id": "census-default-build-multifile", "label": "bm", "type": "loop", "profile": "planning",
                 "checks": [{}, {}], "census": {"template": "default"}, "overrides": {"model": "qwen3:8b"}, "goal": "g"}]
    monkeypatch.setattr(EV, "_get_tasks", _tasks)
    ov = run(TH.cap_evolve_tasks_overview())
    o = next(x for x in ov["tasks"] if x["task_id"] == "census-default-build-multifile")
    assert o["type"] == "loop" and o["profile"] == "planning" and o["checks_n"] == 2 and o["enabled"] is True
    assert o["seeded"] is True and o["overrides"] == {"model": "qwen3:8b"} and o["goal"] == "g"


def test_upsert_merges_over_the_stored_record(monkeypatch):
    async def _go():
        r = await _store()
        if r is None:
            pytest.skip("no Redis of our own here")
        monkeypatch.setattr(EV, "_redis", lambda: r)
        tid = "test-merge-" + uuid.uuid4().hex[:6]
        try:
            seeded = {"id": tid, "label": "seeded", "type": "loop", "goal": "do x", "tags": ["census", "census-t"],
                      "census": {"template": "t", "goal_id": "x"}, "overrides": {"model": "qwen3:8b"},
                      "scenario": "s", "target": "loop", "checks": [{"type": "cap_called", "value": "echo"}]}
            await EV._save_task(seeded)
            # the editor's save: a record rebuilt from form fields only
            out = await EV.evolve_task_upsert(task={"id": tid, "label": "edited", "type": "loop", "goal": "do y",
                                                    "tags": ["census", "census-t"], "checks": [], "enabled": True})
            assert out["ok"] and out["merged"] is True
            got = await EV._get_task(tid)
            assert got["label"] == "edited" and got["goal"] == "do y" and got["checks"] == []
            assert got["census"] == {"template": "t", "goal_id": "x"}, "the template link survives an edit"
            assert got["overrides"] == {"model": "qwen3:8b"} and got["scenario"] == "s" and got["target"] == "loop"
            # null removes a field on purpose
            await EV.evolve_task_upsert(task={"id": tid, "overrides": None})
            assert "overrides" not in (await EV._get_task(tid))
            # merge=false is the old full replace
            await EV.evolve_task_upsert(task={"id": tid, "label": "bare"}, merge=False)
            got = await EV._get_task(tid)
            assert got["label"] == "bare" and "census" not in got and got["type"] == "loop"
            # a new id is simply created
            out = await EV.evolve_task_upsert(task={"id": tid + "-new", "label": "n"})
            assert out["ok"] and out["merged"] is False
        finally:
            await r.hdel(EV.KEY_TASKS, tid, tid + "-new")
            await r.aclose()
    run(_go())
