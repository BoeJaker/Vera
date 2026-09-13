"""One shape for every driver run (vera/evolve/work_core.py).

The Work page (Loop Lab flattening slice 3) is one table whose rows are tasks
or driver runs; these pin the row shape, the dedupe of a census run against
the scoreboard the harness posted for it, which single runs are rows, the
order (live first, then newest), and the filters.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import work_core as wc  # noqa: E402


def census(run_id, **kw):
    s = {"run_id": run_id, "goals": 12, "done": 8, "wall_capped": 4, "other": 0, "warnings_total": 44,
         "wall_total_s": 12630.8, "quality_mean": 0.84, "template": "default", "reruns": 1,
         "ended_at": 1789058111.9, "partial": False, "excluded": False, "exclude_reason": "",
         "code": {"segments": [{"code": "ab57820558@main", "sha": "ab57820558", "branch": "main", "goals": 7},
                               {"code": "b2864f724a@main", "sha": "b2864f724a", "branch": "main", "goals": 5}],
                  "changed": True},
         "routing": {"coders": ["jaahas/qwen3.5-uncensored"], "nodes": {"gpu-250": 30}},
         "provenance": {"operator": "claude", "tool": "census_all.sh"}}
    s.update(kw)
    return s


def suite(sid, ts, **kw):
    s = {"suite_id": sid, "ts": ts, "tasks_n": 2, "avg_combined": 7.5, "pass_rate": 0.75, "tag": "core",
         "profile": "", "critic": "ollama", "variant": "",
         "results": [{"task": "a", "run_id": "r1", "pass_rate": 1.0, "elapsed_s": 10, "error": "", "combined": 10},
                     {"task": "b", "run_id": "r2", "pass_rate": 0.5, "elapsed_s": 20, "error": "", "combined": 5}]}
    s.update(kw)
    return s


def test_a_census_run_is_a_row_with_its_rollups():
    r = wc.census_row(census("run52"))
    assert r["kind"] == "census" and r["id"] == "run52" and r["template"] == "default" and r["tag"] == "census-default"
    assert r["tasks_n"] == 12 and r["done"] == 8 and r["capped"] == 4 and r["pass_rate"] == 0.667
    assert r["status"] == "done" and r["live"] is False and r["excluded"] == ""
    assert r["code"] == {"sha_short": "b2864f724a", "branch": "main", "changed": True}, "the code the run ENDED on"
    assert r["routing"]["coders"] == ["jaahas/qwen3.5-uncensored"] and r["provenance"]["operator"] == "claude"
    assert r["ts"].startswith("2026-09-10T") and r["ended_at"] == 1789058111.9
    assert r["open"] == {"kind": "census", "id": "run52"} and r["census"]["goals"] == 12
    bad = wc.census_row(census("run51-failed-coder-cpu-spill", excluded=True, exclude_reason="the run is named 'failed'"))
    assert bad["status"] == "excluded" and bad["excluded"] == "failed", "the marker, not the sentence"
    assert bad["excluded_reason"] == "the run is named 'failed'"
    short = wc.census_row(census("run30", excluded=True, partial=True, exclude_reason="4 of 12 goals"))
    assert short["excluded"] == "partial"
    live = wc.census_row(census("current", partial=True), live=True)
    assert live["status"] == "running" and live["live"] is True
    part = wc.census_row(census("run12-partial", partial=True))
    assert part["status"] == "partial"


def test_suite_improve_and_single_runs_are_rows_too():
    s = wc.suite_row(suite("0f618255", "2026-09-08T08:48:51Z"))
    assert s["kind"] == "suite" and s["id"] == "0f618255" and s["tag"] == "core" and s["tasks_n"] == 2
    assert s["done"] == 1 and s["failed"] == 1 and s["pass_rate"] == 0.75 and s["avg_combined"] == 7.5
    assert s["wall_total_s"] == 30 and s["ts"] == "2026-09-08T08:48:51Z" and s["ended_at"] > 0
    assert s["open"] == {"kind": "suite", "id": "0f618255"} and len(s["results"]) == 2
    i = wc.improve_row({"id": "abc", "profile": "planning", "status": "running", "live": True, "started_at": "2026-09-10T10:00:00Z",
                        "rounds_done": 2, "max_rounds": 4, "best_score": 8.1, "best_variant": "v2", "current": "round 3"})
    assert i["kind"] == "improve" and i["status"] == "running" and i["live"] is True and i["rounds"] == "2/4"
    assert i["avg_combined"] == 8.1 and i["best_variant"] == "v2" and i["open"] == {"kind": "improve", "id": "abc"}
    r = wc.run_row({"run_id": "x1", "task": "echo", "ts": "2026-09-09T12:00:00Z", "source": "manual", "pass_rate": 1.0,
                    "combined": 10, "elapsed_s": 3.2, "where": "sandbox"})
    assert r["kind"] == "run" and r["status"] == "done" and r["source"] == "manual" and r["task"] == "echo"
    assert r["tasks_n"] == 1 and r["done"] == 1 and r["wall_total_s"] == 3.2 and r["open"] == {"kind": "run", "id": "x1"}
    e = wc.run_row({"run_id": "x2", "task": "echo", "ts": "t", "source": "goal", "error": "boom"})
    assert e["status"] == "error" and e["failed"] == 1


def test_driver_rows_dedupe_census_scoreboards_and_drop_child_runs():
    rows = wc.driver_rows(
        [census("run52"), census("run51-failed-coder-cpu-spill", excluded=True, exclude_reason="failed", ended_at=1789040000.0),
         census("current", partial=True, ended_at=1789060000.0)],
        [suite("run52", "2026-09-10T15:11:38Z", source="census", census_run="run52", template="default"),
         suite("run51", "2026-09-10T07:25:00Z", source="census", census_run="run51", template="default"),
         suite("run40", "2026-09-06T00:00:00Z", source="census", census_run="run40", template="default"),
         suite("0f618255", "2026-09-08T08:48:51Z")],
        [{"id": "s9", "profile": "planning", "status": "done", "started_at": "2026-09-01T00:00:00Z", "rounds_done": 4, "max_rounds": 4}],
        [{"run_id": "r1", "task": "a", "ts": "2026-09-08T08:48:52Z", "source": "suite"},
         {"run_id": "c1", "task": "census-default-x", "ts": "2026-09-10T15:00:00Z", "source": "census"},
         {"run_id": "m1", "task": "echo", "ts": "2026-09-09T12:00:00Z", "source": "manual", "pass_rate": 1.0}],
        live_census=True)
    ids = [(r["kind"], r["id"]) for r in rows]
    assert ids[0] == ("census", "current"), "the live run first"
    assert ("census", "run52") in ids and ("census", "run51-failed-coder-cpu-spill") in ids
    assert ("suite", "run52") not in ids and ("suite", "run51") not in ids, "the scoreboard of an archived census run is that run"
    assert ("census", "run40") in ids, "a census scoreboard with no archive here still shows as a census run"
    assert ("suite", "0f618255") in ids and ("improve", "s9") in ids and ("run", "m1") in ids
    assert ("run", "r1") not in ids and ("run", "c1") not in ids, "child runs belong to their driver"
    r52 = next(r for r in rows if r["id"] == "run52")
    assert r52.get("also_in_store") is True
    r51 = next(r for r in rows if r["id"].startswith("run51"))
    assert r51.get("also_in_store") is True, "matched by the bare id the harness reserved"
    # newest first after the live one
    rest = [r["ended_at"] for r in rows[1:]]
    assert rest == sorted(rest, reverse=True)


def test_filters():
    rows = wc.driver_rows([census("run52"), census("run51-x", excluded=True, exclude_reason="failed", template="exec-family")],
                          [suite("0f618255", "2026-09-08T08:48:51Z")], [], [{"run_id": "m1", "task": "echo", "ts": "t", "source": "manual"}])
    assert [r["id"] for r in wc.filter_rows(rows, kind="census")] == ["run52", "run51-x"]
    assert [r["id"] for r in wc.filter_rows(rows, kind="census", include_excluded=False)] == ["run52"]
    assert [r["id"] for r in wc.filter_rows(rows, template="exec-family")] == ["run51-x"]
    assert [r["id"] for r in wc.filter_rows(rows, source="manual")] == ["m1"]
    assert [r["id"] for r in wc.filter_rows(rows, text="claude")] == ["run52", "run51-x"], "provenance is searchable"
    assert [r["id"] for r in wc.filter_rows(rows, text="gpu-250")] == ["run52", "run51-x"], "so are the nodes"
    assert [r["id"] for r in wc.filter_rows(rows, text="echo")] == ["m1"]


def test_a_task_row_carries_definition_and_history():
    t = {"id": "census-default-build-multifile", "label": "build multifile", "type": "loop", "profile": "planning",
         "enabled": True, "tags": ["census", "census-default"], "checks": [{}, {}], "goal": "Build three files",
         "census": {"template": "default", "goal_id": "build-multifile"}, "overrides": {"model": "qwen3:8b"}}
    o = {"runs": 28, "stats": {"ok_rate": 0.9, "wall_median_s": 640, "trend_ok_rate": 0.05, "streak": {"kind": "ok", "n": 3}},
         "last": {"status": "done"}, "series": [{"ok": True}], "template": "default"}
    r = wc.task_row(t, o)
    assert r["id"] == t["id"] and r["type"] == "loop" and r["checks_n"] == 2 and r["template"] == "default"
    assert r["seeded"] is True and r["overrides"] == {"model": "qwen3:8b"} and r["enabled"] is True
    assert r["runs"] == 28 and r["ok_rate"] == 0.9 and r["wall_median_s"] == 640 and r["last"]["status"] == "done"
    bare = wc.task_row({"id": "echo", "type": "cap", "cap": "echo"}, None)
    assert bare["seeded"] is False and bare["runs"] == 0 and bare["cap"] == "echo" and bare["stats"] == {}
