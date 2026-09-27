"""ci_view_core: the Loop Lab pictures, computed once, never silently short."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.evolve import ci_view_core as cv  # noqa: E402
from vera.evolve import unittest_history as UH  # noqa: E402


def run(ts, branch, ok, failed=(), total=10, controller="", pipeline_id="", truncated=False, running=False):
    r = {"ts": ts, "branch": branch, "ok": ok, "passed": total - len(failed),
         "failed": len(failed), "errors": 0, "skipped": 0, "total": total,
         "controller": controller, "pipeline_id": pipeline_id,
         "failures": [{"node_id": f, "kind": "failure", "description": f + " broke"} for f in failed],
         "failures_truncated": truncated}
    if running:
        r["status"] = "running"
    return r


RUNS = [
    run("2026-09-01T10:00:00Z", "feat/a", False, ["t::x"], controller="claude_code", pipeline_id="p1"),
    run("2026-09-01T10:05:00Z", "feat/a", False, ["t::x", "t::y"], controller="claude_code"),
    run("2026-09-01T10:20:00Z", "feat/a", True, controller="claude"),
    run("2026-09-01T11:00:00Z", "feat/b", True, controller="codex"),
    run("2026-09-01T12:00:00Z", "feat/b", False, ["t::y"], controller="codex"),
]


def test_controller_names_are_one_spelling():
    assert cv.controller_of("claude_code") == "claude"
    assert cv.controller_of("", "codex") == "codex"
    assert cv.controller_of("census") == "vera"
    assert cv.controller_of("newbot") == "newbot"
    assert cv.controller_of("", None) == ""


def test_race_counts_the_red_runs_and_the_time_to_green():
    lanes = {ln["name"]: ln for ln in cv.lanes_of(RUNS)}
    a = lanes["feat/a"]["race"]
    assert a["state"] == "green" and a["streak"] == 1
    assert a["laps"] == [{"went_red_at": "2026-09-01T10:00:00Z", "went_green_at": "2026-09-01T10:20:00Z",
                          "red_runs": 2, "attempts": 3, "seconds": 1200.0,
                          "from": "p1", "to": "2026-09-01T10:20:00Z"}]
    b = lanes["feat/b"]["race"]
    assert b["state"] == "red" and b["red_runs"] == 1 and b["laps"] == []


def test_a_running_red_lane_is_racing():
    rows = RUNS + [run("2026-09-01T12:30:00Z", "feat/b", None, running=True)]
    lanes = {ln["name"]: ln for ln in cv.lanes_of(rows)}
    assert lanes["feat/b"]["state"] == "racing"


def test_cols_keeps_the_newest_and_counts_the_rest():
    lanes = {ln["name"]: ln for ln in cv.lanes_of(RUNS, cols=1)}
    a = lanes["feat/a"]
    assert len(a["cells"]) == 1 and a["hidden"] == 2 and a["cells"][0]["o"] == "pass"
    # the race is computed over the WHOLE lane, not the visible cells
    assert a["race"]["laps"][0]["attempts"] == 3
    m = cv.matrix(RUNS, cols=1)
    assert m["summary"]["runs"] == 5 and m["summary"]["shown"] == 2


def test_no_cols_means_every_run():
    m = cv.matrix(RUNS)
    assert sum(len(ln["cells"]) for ln in m["lanes"]) == len(RUNS)
    assert all(ln["hidden"] == 0 for ln in m["lanes"])


def test_red_order_puts_red_lanes_first():
    names = [ln["name"] for ln in cv.lanes_of(RUNS, order="red")]
    assert names[0] == "feat/b"


def test_filters():
    assert {r["branch"] for r in cv.filter_rows(RUNS, controller="claude")} == {"feat/a"}
    assert len(cv.filter_rows(RUNS, branch="feat/")) == 5
    assert len(cv.filter_rows(RUNS, branch="feat/a")) == 3
    assert len(cv.filter_rows(RUNS, status="red")) == 3
    assert len(cv.filter_rows(RUNS, q="t::y")) == 2
    assert len(cv.filter_rows(RUNS, since="2026-09-01T11:00:00Z")) == 2
    assert len(cv.filter_rows(RUNS, until="2026-09-01T10:05:00Z")) == 2


def test_summary_splits_by_agent():
    s = cv.matrix(RUNS)["summary"]
    assert s["controllers"]["claude"] == {"runs": 3, "pass": 1, "red": 2}
    assert s["controllers"]["codex"] == {"runs": 2, "pass": 1, "red": 1}
    assert s["pass_rate"] == 0.4 and s["median_attempts"] == 3


def test_test_grid_classifies_and_never_invents_a_pass():
    rows = [run("1", "b", False, ["t::x"]), run("2", "b", True), run("3", "b", False, ["t::x"]),
            run("4", "b", True), run("5", "b", False, ["t::z"], truncated=True)]
    g = cv.test_grid(rows)
    t = {x["id"]: x for x in g["tests"]}
    assert t["t::x"]["cells"] == ["fail", "pass", "fail", "pass", "unknown"]
    assert t["t::x"]["class"] == "flaky"
    assert t["t::z"]["cells"] == ["-", "-", "-", "-", "fail"] and t["t::z"]["class"] == "broken"
    assert g["tests"][0]["id"] == "t::z"   # broken first


def test_test_grid_old_rows_without_failures_are_unknown():
    rows = [{"ts": "1", "branch": "b", "ok": False, "failed": 1, "total": 3,
             "failures": [{"node_id": "t::x"}]},
            {"ts": "2", "branch": "b", "ok": True, "total": 3}]   # recorded before failures were kept
    g = cv.test_grid(rows)
    assert g["tests"][0]["cells"] == ["fail", "unknown"]


def test_compare():
    c = cv.compare(RUNS[1], RUNS[2])
    assert [x["id"] for x in c["fixed"]] == ["t::x", "t::y"] and c["broken"] == [] and c["still"] == []
    assert c["delta"]["failed"] == -2 and c["seconds"] == 900.0
    c2 = cv.compare(RUNS[0], RUNS[1])
    assert [x["id"] for x in c2["broken"]] == ["t::y"] and [x["id"] for x in c2["still"]] == ["t::x"]


def test_pulse_buckets_by_day():
    p = cv.pulse(RUNS)
    assert p["series"] == [{"t": "2026-09-01", "runs": 5, "pass": 2, "red": 3, "tests": 10, "rate": 0.4}]


def test_track_keeps_full_step_detail_and_orders_stages():
    long = "x" * 3000
    p = {"id": "p1", "branch": "feat/a", "decision": "promoted", "gate_passed": True,
         "controller": "claude_code",
         "steps": [{"stage": "begin", "ok": True, "detail": "b", "ts": "1"},
                   {"stage": "critical-tests", "ok": False, "detail": long, "ts": "2"},
                   {"stage": "critical-tests", "ok": True, "detail": "ok", "ts": "3"}]}
    t = cv.track(p)
    names = [s["name"] for s in t["stages"]]
    assert names == list(cv.TRACK)
    tests = t["stages"][3]
    assert tests["status"] == "done" and tests["steps"][0]["detail"] == long
    assert t["stages"][5]["status"] == "done" and t["controller"] == "claude"


def test_board_joins_the_gate():
    items = [{"id": "i1", "title": "x", "lane": "in_progress", "pipeline": "p1", "agent": "claude"},
             {"id": "i2", "title": "y", "lane": "done", "branch": "feat/b"}]
    pipes = [{"id": "p1", "gate_passed": False}, {"id": "p2", "branch": "feat/b", "gate_passed": True}]
    b = cv.board(items, pipes)
    cols = {c["name"]: c["items"] for c in b["columns"]}
    assert cols["in_progress"][0]["gate"] == "fail" and cols["done"][0]["gate"] == "pass"
    assert [c["name"] for c in b["columns"]] == ["in_progress", "done"]
    assert [c["name"] for c in cv.board(items, pipes, include_done=False)["columns"]] == ["in_progress"]


TRACE = {"session_id": "s1", "run": {"goal": "do it", "status": "done"},
         "plan": {"steps": [{"id": 1, "title": "one"}, {"id": 2, "title": "two"}, {"id": 3, "title": "three"}]},
         "steps": [{"step_id": 1, "title": "one", "ok": True,
                    "calls": [{"cycle": 1, "tool": "a", "ok": False, "ms": 5}, {"cycle": 2, "tool": "a", "ok": True, "ms": 7}]},
                   {"step_id": 2, "title": "two", "ok": False, "calls": [{"cycle": 1, "tool": "b", "ok": False}]}],
         "gates": [{"round": 1, "complete": False, "missing": ["x"]}, {"round": 2, "complete": True}]}


def test_loop_matrix_lane_per_step_cell_per_call():
    m = cv.loop_matrix(TRACE)
    assert [ln["name"] for ln in m["lanes"]] == ["one", "two", "three"]
    assert [c["o"] for c in m["lanes"][0]["cells"]] == ["fail", "pass"]
    assert [ln["state"] for ln in m["lanes"]] == ["green", "red", "none"]


def test_loop_race_leads_with_the_gate():
    r = cv.loop_race(TRACE)
    assert r["lanes"][0]["name"] == "completion gate"
    assert r["lanes"][0]["race"]["laps"][0]["attempts"] == 2


def test_loop_board():
    b = cv.loop_board(TRACE)
    cols = {c["name"]: [i["title"] for i in c["items"]] for c in b["columns"]}
    assert cols == {"planned": ["three"], "running": [], "done": ["one"], "failed": ["two"]}


def test_history_row_keeps_failures_and_marks_truncation():
    parsed = {"ok": False, "passed": 5, "failed": 2, "errors": 0, "skipped": 0,
              "failure_details": [{"node_id": "t::x", "kind": "failure", "description": "boom"}]}
    row = UH.record(parsed, branch="b", controller="claude", session_id="s")
    assert row["failures"] == [{"node_id": "t::x", "kind": "failure", "description": "boom"}]
    assert row["failures_truncated"] is True          # 2 failed, 1 listed
    assert row["controller"] == "claude" and row["session_id"] == "s"
    lanes = UH.lanes([row])
    assert lanes[0]["failing"] == ["t::x"]


def test_parse_merges_reads_loop_lab_and_git_merges():
    log = "\n".join([
        "a1\x1f2026-09-27T20:00:00Z\x1fBoeJaker\x1fLoop Lab: merge feat/x (pipeline 1a2b3c4d)",
        "b2\x1f2026-09-27T19:00:00Z\x1fBoeJaker\x1fMerge branch 'bleeding-edge-design' into loop-lab/mirror",
        "c3\x1f2026-09-26T19:00:00Z\x1fBoeJaker\x1fLoop Lab: merge feat/x (pipeline 9f9f9f9f)",
        "d4\x1f2026-09-25T19:00:00Z\x1fBoeJaker\x1fMerge remote-tracking branch 'origin/main'",
        "e5\x1f2026-09-24T19:00:00Z\x1fBoeJaker\x1fsomething else",
    ])
    rows = {m["branch"]: m for m in cv.parse_merges(log)}
    assert set(rows) == {"feat/x", "bleeding-edge-design", "main"}
    assert rows["feat/x"]["pipeline_id"] == "1a2b3c4d" and rows["feat/x"]["merges"] == 2
    assert rows["bleeding-edge-design"]["pipeline_id"] == ""


def test_test_grid_says_when_runs_recorded_counts_only():
    g = cv.test_grid([{"ts": "1", "branch": "b", "ok": False, "failed": 1, "total": 3}])
    assert g["tests"] == [] and g["summary"]["listed"] == 0 and g["summary"]["red_runs"] == 1
