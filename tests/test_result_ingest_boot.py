"""evolve.result.ingest must load with the app, store a census goal in the
suite's own store, and read back through every view that store feeds.

Runs in-container (the merge gate's runner, or a sandbox), where Vera.vera
resolves to THIS checkout; skips on a host venv where it resolves elsewhere.
The store test needs a Redis of its own: it connects to REDIS_URL the way the
app would, refuses database 0 (prod's), and removes everything it writes.
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


def _rd(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def _store():
    """A live client for the store the app would use HERE, or None: the Redis
    the process was given (REDIS_URL - a sandbox's own database), when a
    server answers on it and it is not database 0, which is prod's. Under
    pytest the app never connected, so the test connects the way it would."""
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


def _row(gid, sid, status="done", wall=321.0, ended="2026-09-10T15:47:18Z"):
    return {"id": gid, "session_id": sid, "status": status, "wall_s": wall, "template": "exec-family",
            "wall_cap_s": 1200, "quality": {"passed": 1, "total": 2, "score": 0.5,
                                            "results": [{"ok": True, "label": "ran"}, {"ok": False, "label": "value", "detail": "42 missing"}]},
            "code": {"sha": "b2864f724a4aa6cee36b29ea6017194942c0f6be", "sha_short": "b2864f724a", "branch": "main"},
            "routing": {"at_start": {"roles": {"coder": {"model": "jaahas/qwen3.5-uncensored"}}},
                        "calls": {"calls": [], "reroutes": [], "nodes": {"gpu-250": 9}, "spill_calls": 0, "n": 9}},
            "started_at": "2026-09-10T15:26:04Z", "ended_at": ended,
            "provenance": {"operator": "claude"}}


async def _cleanup(r, run_ids, suite_id):
    for raw in await r.lrange(EV.KEY_RUNS, 0, EV.RUNS_CAP - 1) or []:
        try:
            rec = json.loads(_rd(raw))
        except Exception:
            continue
        if rec.get("run_id") in run_ids:
            await r.lrem(EV.KEY_RUNS, 0, raw)
    for rid in run_ids:
        await r.delete(EV.KEY_RUN + rid)
    for raw in await r.lrange(EV.KEY_SUITES, 0, EV.SUITES_CAP - 1) or []:
        try:
            rec = json.loads(_rd(raw))
        except Exception:
            continue
        if rec.get("suite_id") == suite_id:
            await r.lrem(EV.KEY_SUITES, 0, raw)


def test_the_cap_is_registered():
    assert "evolve.result.ingest" in ORCH.CAPABILITY_REGISTRY
    assert "evolve.runs" in ORCH.CAPABILITY_REGISTRY


async def _ingest_round_trip(tmp_path, monkeypatch):
    import sys
    r = await _store()
    if r is None:
        pytest.skip("no Redis of our own here (REDIS_URL unset, unreachable, or database 0)")
    monkeypatch.setitem(sys.modules, "census_capabilities", CC)
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setattr(EV, "_redis", lambda: r)
    monkeypatch.setattr(CC, "CENSUS_DIR", pathlib.Path(tmp_path))
    CC._CACHE.clear()
    tag = uuid.uuid4().hex[:6]
    census_run = "exec-family-run9%s" % tag
    sid1, sid2 = "s1-" + tag, "s2-" + tag
    try:
        # one goal posted live by the harness
        out = await TH.cap_evolve_result_ingest(template="exec-family", census_run=census_run,
                                                row=_row("exec-write-then-run", sid1))
        assert out["ok"] and out["added"] == 1 and out["run_ids"] == [sid1], out
        assert out["tag"] == "census-exec-family" and out["suite_id"] == ""
        # ... posted again: replaced, not duplicated
        out2 = await TH.cap_evolve_result_ingest(template="exec-family", census_run=census_run,
                                                 row=json.dumps(_row("exec-write-then-run", sid1, wall=333.0)))
        assert out2["replaced"] == 1 and out2["added"] == 0
        runs = (await EV.evolve_runs(limit=400, source="census", session=census_run))["runs"]
        assert [x["run_id"] for x in runs] == [sid1]
        assert runs[0]["elapsed_s"] == 333.0 and runs[0]["task"] == "census-exec-family-exec-write-then-run"
        assert runs[0]["census_run"] == census_run and runs[0]["code"]["sha_short"] == "b2864f724a"
        det = (await EV.evolve_run_get(run_id=sid1))["run"]
        assert det["census_row"]["quality"]["total"] == 2 and det["checks"][1]["note"] == "42 missing"
        assert det["source"] == "census" and det["routing"]["coder"] == "jaahas/qwen3.5-uncensored"

        # the whole run from its archive: rows + scoreboard
        out3 = await TH.cap_evolve_result_ingest(
            template="exec-family", census_run=census_run, suite=True, archive="census.%s.jsonl" % census_run,
            rows=[_row("exec-write-then-run", sid1, wall=333.0),
                  _row("exec-inline-snippet", sid2, status="wall-cap", wall=1214.3, ended="2026-09-10T16:10:00Z")])
        assert out3["replaced"] == 1 and out3["added"] == 1 and out3["suite_id"] == census_run and out3["tasks_n"] == 2
        suites = (await EV.evolve_suites(limit=5, tag="census-exec-family"))["suites"]
        s = next(x for x in suites if x["suite_id"] == census_run)
        assert s["source"] == "census" and s["tasks_n"] == 2 and s["capped"] == 1 and s["ts"] == "2026-09-10T16:10:00Z"
        assert {x["run_id"] for x in s["results"]} == {sid1, sid2}
        # posted again: the scoreboard is replaced, not duplicated
        await TH.cap_evolve_result_ingest(template="exec-family", census_run=census_run, suite=True,
                                          rows=[_row("exec-write-then-run", sid1, wall=333.0)])
        suites = (await EV.evolve_suites(limit=60, tag="census-exec-family"))["suites"]
        assert sum(1 for x in suites if x["suite_id"] == census_run) == 1

        # the index: an ingested-only goal is a census result of its census run
        h = await TH.cap_evolve_task_history(id="census-exec-family-exec-inline-snippet")
        mine = [x for x in h["results"] if x["run_id"] == sid2]
        assert len(mine) == 1 and mine[0]["ingested"] is True and mine[0]["hit_cap"] is True
        assert mine[0]["driver"] == {"kind": "census", "id": census_run}
        # ... and once the archive holds the same session, the archive row is the result
        (tmp_path / ("census.%s.jsonl" % census_run)).write_text(
            json.dumps(_row("exec-inline-snippet", sid2, status="wall-cap", wall=1214.3)) + "\n")
        CC._CACHE.clear()
        h = await TH.cap_evolve_task_history(id="census-exec-family-exec-inline-snippet")
        mine = [x for x in h["results"] if x["run_id"] == sid2]
        assert len(mine) == 1 and "ingested" not in mine[0], "the archive row and its ingested copy are ONE result"
        assert mine[0]["driver"]["id"] == census_run
        # ... and a marker on the archive's name excludes the ingested record too
        os.rename(tmp_path / ("census.%s.jsonl" % census_run), tmp_path / ("census.%s-failed-x.jsonl" % census_run))
        CC._CACHE.clear()
        h = await TH.cap_evolve_task_history(id="census-exec-family-exec-inline-snippet")
        assert not [x for x in h["results"] if x["run_id"] == sid2], "marked unusable: out of the history"
        h = await TH.cap_evolve_task_history(id="census-exec-family-exec-inline-snippet", include_excluded=True)
        mine = [x for x in h["results"] if x["run_id"] == sid2]
        assert len(mine) == 1 and mine[0]["excluded"] == "failed"
    finally:
        await _cleanup(r, {sid1, sid2}, census_run)
    assert not (await EV.evolve_runs(limit=400, session=census_run))["runs"]
    await r.aclose()


def test_a_goal_lands_in_the_suite_store_and_every_reader_sees_it(tmp_path, monkeypatch):
    run(_ingest_round_trip(tmp_path, monkeypatch))


def test_bad_calls_are_refused_without_touching_the_store():
    assert "error" in run(TH.cap_evolve_result_ingest(template="", census_run="run1", row={"id": "x"}))
    assert "error" in run(TH.cap_evolve_result_ingest(template="default", census_run="", row={"id": "x"}))
    assert "error" in run(TH.cap_evolve_result_ingest(template="default", census_run="run1"))
    assert "error" in run(TH.cap_evolve_result_ingest(template="default", census_run="run1", row={"id": "x"}, source="suite"))


def test_no_store_is_an_error_not_a_silent_ok(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setattr(EV, "_redis", lambda: None)
    out = run(TH.cap_evolve_result_ingest(template="default", census_run="run1", row={"id": "x", "status": "done"}))
    assert out.get("error") and not out.get("ok"), out
