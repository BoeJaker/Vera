"""One task through time (vera/evolve/task_history_core.py).

A census goal is a task; its results lived in a JSONL archive while the
suite's lived in Redis, so no view could ask how ONE task had done across
runs. These pin the shared result shape, the join key, the dedupe rule, and
the stats a per-task dashboard shows.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import task_history_core as th  # noqa: E402


def crow(gid, status="done", wall=100, template="default", sid="s1", **kw):
    r = {"id": gid, "status": status, "wall_s": wall, "template": template, "session_id": sid,
         "quality": {"passed": 3, "total": 4}, "wall_cap_s": 1800}
    r.update(kw)
    return r


def test_the_join_key_is_the_seeded_task_id():
    assert th.census_task_id("default", "build-multifile") == "census-default-build-multifile"
    # An unlabelled row belongs to the default series.
    assert th.census_task_id("", "trivial-chat") == "census-default-trivial-chat"
    # Template names carry hyphens: the store's names decide the split.
    tpls = ["default", "exec-family", "model-compare", "specialist-loops"]
    assert th.split_task_id("census-exec-family-exec-write-then-run", tpls) == {"template": "exec-family", "goal": "exec-write-then-run"}
    assert th.split_task_id("census-default-build-multifile", tpls) == {"template": "default", "goal": "build-multifile"}
    assert th.split_task_id("census-default-build-multifile") == {"template": "default", "goal": "build-multifile"}
    assert th.split_task_id("core-something") == {}
    r = th.result_from_run_record({"run_id": "x", "task": "census-exec-family-exec-inline-snippet", "ts": "t"}, templates=tpls)
    assert r["template"] == "exec-family" and r["goal"] == "exec-inline-snippet"


def test_a_census_row_becomes_a_result_with_its_provenance():
    r = th.result_from_census_row(crow("build-multifile", started_at="2026-09-10T11:00:00Z",
                                       ended_at="2026-09-10T11:20:00Z",
                                       code={"sha": "b2864f724a", "sha_short": "b2864f724a", "branch": "main"},
                                       routing={"at_start": {"roles": {"coder": {"model": "m9b"}}},
                                                "calls": {"n": 5, "nodes": {"gpu-250": 5}, "spill_calls": 0, "reroutes": []}}),
                                  "run52")
    assert r["task_id"] == "census-default-build-multifile"
    assert r["source"] == "census" and r["driver"] == {"kind": "census", "id": "run52"}
    assert r["run_id"] == "s1" and r["session"] == "s1"
    assert r["ok"] is True and r["status"] == "done" and r["hit_cap"] is False
    assert r["ts"] == "2026-09-10T11:20:00Z"
    assert r["checks_ok"] == 3 and r["checks_n"] == 4 and r["pass_rate"] == 0.75
    assert r["code"] == {"sha_short": "b2864f724a", "branch": "main", "changed": False}
    assert r["routing"]["coder"] == "m9b" and r["routing"]["nodes"] == {"gpu-250": 5}


def test_a_capped_row_is_not_ok_and_says_it_hit_the_cap():
    r = th.result_from_census_row(crow("author-then-edit", status="wall-cap", wall=1804), "run52")
    assert r["ok"] is False and r["hit_cap"] is True and r["status"] == "wall-cap"


def test_code_version_backfill_is_read_when_code_is_absent():
    r = th.result_from_census_row(crow("g", code_version=["04c38bfca2@main"]), "run50")
    assert r["code"] == {"sha_short": "04c38bfca2", "branch": "main", "changed": False}


def test_a_row_without_timestamp_takes_the_fallback():
    r = th.result_from_census_row(crow("g"), "run49", ts_fallback="2026-09-09T08:43:51Z")
    assert r["ts"] == "2026-09-09T08:43:51Z"


def test_a_skipped_or_sessionless_row_is_still_a_result():
    assert th.result_from_census_row({"id": "g", "skipped": "box busy"}, "run1")["status"] == "skipped"
    r = th.result_from_census_row({"id": "g", "no_session": True, "wall_s": 100}, "run1")
    assert r["status"] == "no-session" and r["run_id"] == "run1:g"


def test_a_run_record_becomes_a_result():
    rec = {"run_id": "7653d5ab30", "task": "census-default-underspecified", "ts": "2026-09-08T08:48:53Z",
           "where": "in-process", "pass_rate": 0.0, "elapsed_s": 0.1,
           "error": "sandbox_mode=require but the sandbox is unusable", "profile": "planning"}
    r = th.result_from_run_record(rec, suite_id="0f618255", suite_tag="census-default")
    assert r["task_id"] == "census-default-underspecified" and r["source"] == "suite"
    assert r["driver"] == {"kind": "suite", "id": "0f618255"}
    assert r["ok"] is False and r["status"] == "error"
    good = th.result_from_run_record({"run_id": "x", "task": "t", "ts": "2026-09-01T00:00:00Z",
                                      "pass_rate": 1.0, "checks_n": 3, "checks_ok": 3, "elapsed_s": 40})
    assert good["ok"] and good["status"] == "pass" and good["source"] == "task"
    imp = th.result_from_run_record({"run_id": "y", "task": "t", "ts": "2026-09-02T00:00:00Z",
                                     "pass_rate": 1.0, "variant": "v3"})
    assert imp["source"] == "improve" and imp["driver"]["id"] == "v3"


def test_merge_dedupes_on_task_and_run_and_the_census_row_wins():
    c = th.result_from_census_row(crow("g", sid="sess-1"), "run52")
    s = th.result_from_run_record({"run_id": "sess-1", "task": "census-default-g", "ts": "t", "pass_rate": 1.0})
    merged = th.merge_results([s], [c])
    assert len(merged) == 1 and merged[0]["source"] == "census"
    merged2 = th.merge_results([c], [s])
    assert len(merged2) == 1 and merged2[0]["source"] == "census"


def test_task_history_orders_newest_first_and_computes_stats():
    rows = [th.result_from_census_row(crow("g", status=st, wall=w, sid="s%d" % i, ended_at="2026-09-%02dT00:00:00Z" % (i + 1)), "run%d" % (40 + i))
            for i, (st, w) in enumerate([("done", 100), ("wall-cap", 1800), ("done", 120), ("done", 90)])]
    h = th.task_history(rows, "census-default-g")
    assert [r["driver"]["id"] for r in h["results"]] == ["run43", "run42", "run41", "run40"]
    st = h["stats"]
    assert st["runs"] == 4 and st["ok"] == 3 and st["capped"] == 1 and st["ok_rate"] == 0.75
    assert st["wall_median_s"] == 110 and st["wall_min_s"] == 90 and st["wall_max_s"] == 1800
    assert st["wall_spread"] == 20.0
    assert st["streak"] == {"n": 2, "ok": True}
    assert h["census_stats"]["runs"] == 4 and h["by_driver"]["census"]["runs"] == 4
    assert h["stats"]["trend_ok_rate"] is None            # fewer than ten results


def test_code_changes_in_a_tasks_history_are_named():
    rows = [th.result_from_census_row(crow("g", sid="s1", ended_at="2026-09-09T00:00:00Z", code_version=["04c38bfca2@main"]), "run50"),
            th.result_from_census_row(crow("g", sid="s2", ended_at="2026-09-10T00:00:00Z", code={"sha": "b2864f724a", "sha_short": "b2864f724a", "branch": "main"}), "run52")]
    h = th.task_history(rows, "census-default-g")
    assert h["code_changes"] == [{"at": "s2", "driver": "run52", "ts": "2026-09-10T00:00:00Z",
                                  "from": "04c38bfca2@main", "to": "b2864f724a@main"}]


def test_tasks_overview_gives_one_line_per_task_with_a_series():
    rows = [th.result_from_census_row(crow("a", sid="s1", ended_at="2026-09-01T00:00:00Z"), "run1"),
            th.result_from_census_row(crow("a", status="wall-cap", sid="s2", ended_at="2026-09-02T00:00:00Z"), "run2"),
            th.result_from_census_row(crow("b", sid="s3", ended_at="2026-09-02T00:00:00Z"), "run2")]
    ov = th.tasks_overview(rows)
    a = next(x for x in ov if x["task_id"] == "census-default-a")
    assert a["runs"] == 2 and a["last"]["status"] == "wall-cap" and a["stats"]["ok_rate"] == 0.5
    assert [p["driver"] for p in a["series"]] == ["run1", "run2"]     # oldest first for a sparkline
    ov2 = th.tasks_overview(rows, task_ids=["census-default-zzz"])
    assert ov2[0]["runs"] == 0 and ov2[0]["last"] is None


def test_tasks_overview_series_is_the_newest_24_oldest_first():
    rows = [{"task_id": "t", "ts": "2026-09-%02dT10:00:00Z" % (d + 1), "ok": True, "status": "done",
             "driver": {"id": "r%02d" % d}} for d in range(30)]
    a = th.tasks_overview(rows)[0]
    got = [p["driver"] for p in a["series"]]
    assert got[0] == "r06" and got[-1] == "r29" and len(got) == 24, got
