"""A census goal becomes a run record (vera/evolve/result_ingest_core.py) and
the index reads it back as the census result it is (task_history_core).

Slice 2 of the Loop Lab flattening: the write side of "one result, many
drivers". These pin the record shape the suite's readers expect, the join key
that lets the index dedupe an ingested record against its archive row, the
archive-over-ingested preference, the suite scoreboard a census run becomes,
and the archive naming the harness reserves at start.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import census_seed  # noqa: E402
from vera.evolve import result_ingest_core as ric  # noqa: E402
from vera.evolve import task_history_core as th  # noqa: E402


def row(gid="exec-inline-snippet", status="wall-cap", sid="b6acd2ce", template="exec-family", **kw):
    r = {"id": gid, "session_id": sid, "status": status, "wall_s": 1214.3, "cancelled": status != "done",
         "intent": "analyse", "tier": "simple", "output": "answer", "shape": "inline",
         "planned": 1, "executed": 2, "tool_calls": 17, "template": template, "wall_cap_s": 1200,
         "model": "(routing default)",
         "quality": {"score": 0.0, "passed": 0, "failed": 1, "total": 1,
                     "results": [{"ok": False, "label": "the arithmetic is correct", "detail": "no match"}],
                     "files_found": [], "files_missing": []},
         "code": {"sha": "b2864f724a4aa6cee36b29ea6017194942c0f6be", "sha_short": "b2864f724a",
                  "branch": "main", "dirty": False, "changed_during_goal": False},
         "routing": {"at_start": {"profile": "loop", "roles": {"coder": {"model": "jaahas/qwen3.5-uncensored"},
                                                                "planner": {"model": "qwen3:8b"}}},
                     "calls": {"calls": [{"node": "gpu-250"}] * 40, "reroutes": [], "nodes": {"gpu-250": 40},
                               "models": {}, "spill_calls": 0, "n": 40, "truncated": False}},
         "started_at": "2026-09-10T15:26:04Z", "ended_at": "2026-09-10T15:47:18Z",
         "provenance": {"operator": "claude", "skill": "improve-vera-sandboxed", "tool": "census_all.sh"},
         "warnings": ["a", "b"], "stages": {"executor": 19}}
    r.update(kw)
    return r


# ── the run record ───────────────────────────────────────────────────────────
def test_a_goal_row_is_a_run_record_the_suite_readers_can_read():
    compact, detail = ric.run_record_from_census_row(row(), template="exec-family", census_run="exec-family-run3",
                                                     goal_text="Compute 6*7 and run it", profile="planning",
                                                     ingested_at="2026-09-10T16:00:00Z")
    # what every evolve.runs consumer reads
    assert compact["run_id"] == "b6acd2ce"                      # the loop session: the archive's key too
    assert compact["task"] == "census-exec-family-exec-inline-snippet"
    assert compact["source"] == "census" and compact["session"] == "exec-family-run3"
    assert compact["task_type"] == "loop" and compact["profile"] == "planning"
    assert compact["ts"] == "2026-09-10T15:47:18Z" and compact["elapsed_s"] == 1214.3
    assert compact["pass_rate"] == 0.0 and compact["combined"] == 0.0
    assert compact["checks_ok"] == 0 and compact["checks_n"] == 1
    assert compact["error"] == "wall-cap", "a capped goal is a problem; its status is the error text"
    assert compact["where"] == "prod-loop" and compact["triggered_by"] == "census:claude"
    # the census facts
    assert compact["status"] == "wall-cap" and compact["hit_cap"] is True and compact["ok"] is False
    assert compact["wall_cap_s"] == 1200 and compact["census_run"] == "exec-family-run3"
    assert compact["loop_session"] == "b6acd2ce" and compact["goal_id"] == "exec-inline-snippet"
    assert compact["code"] == {"sha_short": "b2864f724a", "branch": "main", "changed": False}
    assert compact["routing"] == {"coder": "jaahas/qwen3.5-uncensored", "nodes": {"gpu-250": 40},
                                  "spill_calls": 0, "reroutes": 0}
    assert compact["warnings"] == 2 and compact["tool_calls"] == 17 and compact["ingested_at"] == "2026-09-10T16:00:00Z"
    # the detail is the compact record plus the checks in the modal's shape and the whole row
    assert detail["goal"] == "Compute 6*7 and run it"
    assert detail["checks"] == [{"type": "quality", "value": "the arithmetic is correct", "note": "no match", "ok": False}]
    assert detail["census_row"]["routing"]["calls"]["n"] == 40
    assert detail["final"] == "" and detail["steps"] == [] and detail["assessment"] is None


def test_the_compact_record_stays_small_however_fat_the_row_is():
    """The run list is read whole by every runs view; the 44 KB of routing
    calls a real row carries must not ride along."""
    fat = row()
    fat["routing"]["calls"]["calls"] = [{"node": "gpu-250", "model": "x" * 200, "prompt": "y" * 800}] * 60
    compact, detail = ric.run_record_from_census_row(fat, template="exec-family", census_run="exec-family-run3")
    assert ric.compact_bytes(compact) < 2000, ric.compact_bytes(compact)
    assert ric.compact_bytes(detail) > 50000


def test_a_clean_goal_with_no_checks_passes_the_suite_way():
    compact, _ = ric.run_record_from_census_row(row(status="done", quality={}), template="default", census_run="run52")
    assert compact["pass_rate"] == 1.0 and compact["combined"] == 10.0 and compact["checks_n"] == 0
    assert compact["error"] == "" and compact["ok"] is True and compact["hit_cap"] is False
    bad, _ = ric.run_record_from_census_row(row(status="abandoned", quality={}, session_id=""),
                                            template="default", census_run="run52")
    assert bad["pass_rate"] == 0.0 and bad["error"] == "abandoned"
    assert bad["run_id"] == "run52:exec-inline-snippet", "no session: keyed by run and goal, as the archive row is"


def test_the_join_key_matches_the_archive_rows_key():
    for r in (row(), row(session_id=""), row(sid="")):
        compact, _ = ric.run_record_from_census_row(r, template="exec-family", census_run="exec-family-run3")
        archive = th.result_from_census_row(r, "exec-family-run3")
        assert compact["run_id"] == archive["run_id"] and compact["task"] == archive["task_id"]


def test_task_ids_use_the_task_stores_own_slug_rule():
    assert th.census_task_id("exec-family", "exec-inline-snippet") == census_seed.task_id_for("exec-family", "exec-inline-snippet")
    assert th.census_task_id("Model Compare", "Goal_One") == census_seed.task_id_for("Model Compare", "Goal_One") == "census-model-compare-goal-one"
    assert th.census_task_id("", "trivial-chat") == "census-default-trivial-chat"
    assert ric.census_tag("exec-family") == census_seed.tag_for("exec-family") == "census-exec-family"


def test_bad_rows_are_refused():
    assert ric.run_record_from_census_row({}, template="default", census_run="run52") is None
    assert ric.run_record_from_census_row({"status": "done"}, template="default", census_run="run52") is None
    assert ric.suite_record_from_rows([{}], template="default", census_run="run52") is None


# ── the suite scoreboard ─────────────────────────────────────────────────────
def test_a_census_run_is_a_suite_scoreboard_tagged_by_template():
    rows = [row("exec-write-then-run", status="done", sid="s1", quality={"passed": 2, "total": 2, "results": []},
                started_at="2026-09-10T15:12:40Z", ended_at="2026-09-10T15:25:00Z"),
            row("exec-inline-snippet", sid="s2"),
            row("exec-shell-artifact", status="done", sid="s3", quality={"passed": 1, "total": 2, "results": []},
                started_at="2026-09-10T15:48:00Z", ended_at="2026-09-10T16:02:00Z")]
    s = ric.suite_record_from_rows(rows, template="exec-family", census_run="exec-family-run3",
                                   archive="census.exec-family-run3.jsonl", ingested_at="t")
    assert s["suite_id"] == "exec-family-run3" and s["tag"] == "census-exec-family"
    assert s["source"] == "census" and s["template"] == "exec-family" and s["census_run"] == "exec-family-run3"
    assert s["tasks_n"] == 3 and s["done"] == 2 and s["capped"] == 1
    assert s["ts"] == "2026-09-10T16:02:00Z" and s["started_at"] == "2026-09-10T15:12:40Z"
    assert s["pass_rate"] == round((1.0 + 0.0 + 0.5) / 3, 3) and s["avg_combined"] == 5.0
    assert s["variant"] == "", "routing-default runs pin no model"
    assert s["code_versions"] == ["b2864f724a@main"] and s["archive"] == "census.exec-family-run3.jsonl"
    assert s["critic"] == "" and s["excluded"] == ""
    r0 = s["results"][0]
    assert r0["task"] == "census-exec-family-exec-write-then-run" and r0["run_id"] == "s1"
    assert r0["checks"] == "2/2" and r0["type"] == "loop" and r0["status"] == "done" and r0["hit_cap"] is False
    assert s["results"][1]["error"] == "wall-cap" and s["results"][1]["hit_cap"] is True
    pinned = ric.suite_record_from_rows(rows, template="model-compare", census_run="model-compare-run2",
                                        excluded="failed")
    assert pinned["excluded"] == "failed"
    one_model = ric.suite_record_from_rows([row(model="qwen3:8b")], template="model-compare", census_run="model-compare-run2")
    assert one_model["variant"] == "qwen3:8b"


# ── the archive name the harness reserves at start ───────────────────────────
def test_run_numbering_mirrors_the_driver_markers_included():
    names = ["census.run52.jsonl", "census.run51-failed-coder-cpu-spill.jsonl", "census.run50.jsonl",
             "census.exec-family-run2-failed-coder-cpu-spill.jsonl", "census.exec-family-run1.jsonl",
             "census.jsonl", "census.run12-partial.jsonl", "backfill_code.py"]
    assert ric.next_run_number(names, "default") == 53
    assert ric.next_run_number(names, "exec-family") == 3
    assert ric.next_run_number(names, "prose-family") == 1
    assert ric.next_run_number([], "default") == 51, "the driver's own floor for the default series"
    assert ric.census_run_name("default", 53) == "run53"
    assert ric.census_run_name("exec-family", 3) == "exec-family-run3"
    assert ric.census_run_name("", 7) == "run7"


# ── reading an ingested record back through the index ────────────────────────
def test_an_ingested_record_reads_as_the_census_result_its_archive_row_would_be():
    r = row()
    compact, _ = ric.run_record_from_census_row(r, template="exec-family", census_run="exec-family-run3")
    back = th.result_from_run_record(json.loads(json.dumps(compact)), templates=["exec-family", "default"])
    archive = th.result_from_census_row(r, "exec-family-run3")
    assert back["ingested"] is True and "ingested" not in archive
    for k in ("task_id", "run_id", "source", "driver", "ts", "status", "ok", "wall_s", "checks_ok", "checks_n",
              "pass_rate", "wall_cap_s", "hit_cap", "code", "routing", "session", "error", "reruns",
              "planned", "executed", "tool_calls", "warnings", "goal", "template"):
        assert back[k] == archive[k], (k, back[k], archive[k])
    assert back["driver"] == {"kind": "census", "id": "exec-family-run3"}


def test_a_census_suites_scoreboard_row_reads_as_a_census_result_too():
    s = ric.suite_record_from_rows([row()], template="exec-family", census_run="exec-family-run3")
    rec = dict(s["results"][0], ts=s["ts"], source="census", census_run=s["census_run"], template=s["template"])
    back = th.result_from_run_record(rec, suite_id=s["suite_id"], suite_tag=s["tag"])
    assert back["source"] == "census" and back["driver"] == {"kind": "census", "id": "exec-family-run3"}
    assert back["run_id"] == "b6acd2ce" and back["hit_cap"] is True and back["tag"] == "census-exec-family"
    assert back["goal"] == "exec-inline-snippet" and back["template"] == "exec-family"


def test_the_archive_row_outranks_its_ingested_copy_which_outranks_a_suite_record():
    r = row()
    archive = th.result_from_census_row(r, "exec-family-run3")
    compact, _ = ric.run_record_from_census_row(r, template="exec-family", census_run="exec-family-run3")
    ingested = th.result_from_run_record(compact, templates=["exec-family"])
    suite = th.result_from_run_record({"run_id": "b6acd2ce", "task": "census-exec-family-exec-inline-snippet",
                                       "ts": "t", "source": "suite"}, suite_id="abc", templates=["exec-family"])
    for order in ([suite, ingested, archive], [archive, ingested, suite], [ingested, archive, suite],
                  [ingested, suite, archive]):
        merged = th.merge_results(order)
        assert len(merged) == 1 and "ingested" not in merged[0] and merged[0]["source"] == "census", order
    assert th.merge_results([suite, ingested])[0].get("ingested") is True
    assert th.merge_results([ingested, suite])[0].get("ingested") is True
    # one result, and only one, whichever order the stores are read in
    assert [x["run_id"] for x in th.merge_results([suite], [ingested], [archive])] == ["b6acd2ce"]


def test_an_ingested_record_learns_its_archive_was_marked_unusable():
    marks = th.archive_marks(["run52", "run51-failed-coder-cpu-spill", "exec-family-run2-failed-coder-cpu-spill",
                              "run12-partial", "current", "exec-family-run3"],
                             lambda rid: next((m for m in ("partial", "failed") if m in rid), ""))
    assert marks == {"run51": "failed", "exec-family-run2": "failed", "run12": "partial"}


def test_task_history_counts_an_ingested_only_result_in_the_census_series():
    compact, _ = ric.run_record_from_census_row(row(status="done"), template="exec-family", census_run="exec-family-run3")
    ingested = th.result_from_run_record(compact, templates=["exec-family"])
    h = th.task_history([ingested], "census-exec-family-exec-inline-snippet")
    assert h["census_stats"]["runs"] == 1 and h["by_driver"]["census"]["runs"] == 1
    assert h["results"][0]["driver"]["id"] == "exec-family-run3"
