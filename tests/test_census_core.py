"""The census history must not present step counts as a verdict.

The census is the measuring instrument for the agentic loop, and the panel built
on it is what people will read to decide "did that fix work". Two ways that goes
wrong, both pinned here:

  * treating a COUNT as a score - fewer steps can mean a tighter plan or a plan
    that dropped half the request (run 12's build-multifile planned ONE step for
    a multi-file package), and `status: done` is a coarse harness outcome that
    L8 exists because a run could report without reaching its goal;
  * believing a delta drawn from a run whose own counters do not reconcile -
    before L9 a completion-gate follow-up executed without being counted as an
    insertion, so `executed > planned` with `inserted: 0` was routine.

Pure: no Redis, no app import - so it runs anywhere, including the host venv.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.census.census_core import (          # noqa: E402
    CURRENT, FIXED_PREFIX, FOUND_PREFIX, board_links_by_run, compare_runs,
    goal_evidence, history, run_id_from_filename, run_sort_key, summarise_run,
)


def _rec(gid, status="done", **kw):
    r = {"id": gid, "status": status, "wall_s": 100.0, "planned": 2,
         "executed": 2, "inserted": 0, "unaccounted": 0, "warnings": []}
    r.update(kw)
    return r


# ── run files → run ids ─────────────────────────────────────────────────────
def test_run_ids_come_from_the_filename():
    assert run_id_from_filename("census.jsonl") == CURRENT
    assert run_id_from_filename("census.run11.jsonl") == "run11"
    assert run_id_from_filename("census.run10-prefixes.jsonl") == "run10-prefixes"
    assert run_id_from_filename("notes.txt") is None


def test_runs_order_by_their_number_with_current_last():
    """The files carry no timestamp and mtime lies the moment one is copied, so
    the run number in the name is the only honest ordering signal."""
    ids = ["run11", CURRENT, "run2", "run10-prefixes"]
    assert sorted(ids, key=run_sort_key) == ["run2", "run10-prefixes", "run11", CURRENT]


# ── a run's counters may not be trustworthy, and that must show ────────────
def test_a_clean_run_reconciles():
    s = summarise_run("run11", [_rec("a"), _rec("b")])
    assert s["goals"] == 2 and s["done"] == 2
    assert s["counters_reconcile"] is True


def test_an_unaccounted_step_makes_the_counters_suspect():
    s = summarise_run("r", [_rec("a"), _rec("b", unaccounted=1)])
    assert s["unaccounted_total"] == 1
    assert s["counters_reconcile"] is False


def test_a_run_predating_the_counter_is_suspect_not_clean():
    """Absent is not zero: it means nobody was counting, which is its own
    reason for suspicion. Older runs have no `unaccounted` key at all."""
    old = {"id": "a", "status": "done", "wall_s": 10.0}
    s = summarise_run("run1", [old])
    assert s["accounting_measured"] == 0
    assert s["counters_reconcile"] is False


def test_wall_capped_runs_are_counted_separately_from_failures():
    s = summarise_run("r", [_rec("a", status="wall-cap"), _rec("b", status="interrupted")])
    assert s["wall_capped"] == 1 and s["other"] == 1 and s["done"] == 0


# ── comparison is per goal, and is a transition not a score ────────────────
def test_comparison_is_per_goal_and_worst_news_first():
    base = [_rec("g1"), _rec("g2", status="wall-cap"), _rec("g3")]
    head = [_rec("g1", status="wall-cap"), _rec("g2"), _rec("g3")]
    out = compare_runs(base, head)
    assert [g["outcome_change"] for g in out] == ["regressed", "held", "improved"]
    assert [g["id"] for g in out] == ["g1", "g3", "g2"]


def test_the_verdict_field_is_named_as_a_transition_not_a_score():
    """`outcome_change` must not be called something like `result` or `score` -
    a goal can reach done having dropped half the request."""
    out = compare_runs([_rec("g")], [_rec("g")])
    assert "outcome_change" in out[0]
    assert "score" not in out[0] and "result" not in out[0]


def test_a_wall_cap_outranks_a_failure():
    """It was still working when the instrument stopped it. Treating those alike
    would hide real progress."""
    out = compare_runs([_rec("g", status="interrupted")], [_rec("g", status="wall-cap")])
    assert out[0]["outcome_change"] == "improved"


def test_a_goal_missing_from_either_run_is_not_scored():
    out = compare_runs([_rec("g1")], [_rec("g1"), _rec("g2")])
    g2 = [g for g in out if g["id"] == "g2"][0]
    assert g2["outcome_change"] == "missing" and "not comparable" in g2["note"]


def test_an_unaccounted_head_run_is_called_out_on_the_goal():
    out = compare_runs([_rec("g")], [_rec("g", unaccounted=1)])
    assert "cannot settle" in out[0]["note"]


def test_wall_delta_is_reported_when_both_sides_have_it():
    out = compare_runs([_rec("g", wall_s=100.0)], [_rec("g", wall_s=60.0)])
    assert out[0]["wall_delta_s"] == -40.0


# ── the trend is coarse, and trusted runs are kept separate ────────────────
def test_the_trend_over_reconciling_runs_is_reported_separately():
    """An improvement measured across a run whose accounting did not add up is
    not evidence, and averaging them would launder it into one number."""
    s = [summarise_run("run1", [_rec("a", status="wall-cap")]),
         summarise_run("run2", [_rec("a", unaccounted=1)]),
         summarise_run("run3", [_rec("a")])]
    h = history(s)
    assert [x["run_id"] for x in h["runs"]] == ["run1", "run2", "run3"]
    assert h["trusted_count"] == 2                       # run2 excluded
    assert h["trend_trusted"]["from"] == "run1" and h["trend_trusted"]["to"] == "run3"
    assert h["trend_trusted"]["delta"] == 1


def test_a_single_run_has_no_trend():
    assert history([summarise_run("run1", [_rec("a")])])["trend_all"] is None


# ── goal evidence: the material you actually judge by ──────────────────────
TRACE = {
    "plan": {"done_when": "statkit importable with mean/median",
             "tier": "simple", "intent": "build",
             "steps": [{"id": 1, "title": "Create the package",
                        "caps": ["code.author"], "success": "files exist"}]},
    "steps": [
        {"step_id": 1, "title": "Create the package", "ok": True,
         "calls": [{"tool": "code.author", "ok": True},
                   {"tool": "code.author", "ok": False}]},
        {"step_id": 7, "title": "Write and save the report", "ok": True,
         "calls": [{"tool": "prose.author", "ok": True}]},
    ],
    "gates": [{"round": 1, "complete": False, "missing": ["no document exists"],
               "follow_up": [{"id": 7, "title": "Write and save the report"}]},
              {"round": 2, "complete": True, "missing": [], "follow_up": []}],
    "warnings": ["step 1: code.author called 2x"],
}


def test_goal_evidence_gives_what_the_goal_asked_for_and_what_ran():
    e = goal_evidence(_rec("build-multifile", status="wall-cap"), TRACE)
    assert e["done_when"] == "statkit importable with mean/median"
    assert e["goal_status"] == "wall-cap"
    assert [s["title"] for s in e["steps"]] == ["Create the package",
                                                "Write and save the report"]
    assert e["steps"][0]["caps"] == ["code.author"]
    assert e["steps"][0]["tools"] == ["code.author"]
    assert e["steps"][0]["failed_tools"] == ["code.author"]
    assert e["steps"][0]["cycles"] == 2


def test_goal_evidence_names_steps_the_planner_never_listed():
    """A step nothing planned is the one to look at hardest - either a gate
    follow-up doing real work, or an undeclared producer."""
    e = goal_evidence(_rec("g"), TRACE)
    assert e["unplanned_steps"] == [7]
    assert e["steps"][1]["planned"] is False and e["steps"][0]["planned"] is True


def test_goal_evidence_surfaces_the_gate_verdict():
    e = goal_evidence(_rec("g"), TRACE)
    assert e["gate_rounds"] == 2
    assert e["gate_complete"] is True          # the LAST gate is the verdict


def test_goal_evidence_offers_no_verdict_of_its_own():
    """It assembles the material; it must not decide. A 'passed'/'score' field
    here would recreate exactly the count-as-verdict problem."""
    e = goal_evidence(_rec("g"), TRACE)
    for banned in ("passed", "score", "verdict", "success"):
        assert banned not in e
    assert "aimed at the goal" in e["how_to_read"]


def test_goal_evidence_survives_a_missing_trace():
    """Event logs age out; an old run has a record but no trace."""
    e = goal_evidence(_rec("g"), {})
    assert e["steps"] == [] and e["done_when"] == "" and e["gate_missing"] == []


# ── board items ↔ runs ─────────────────────────────────────────────────────
ITEMS = [
    {"id": "loop-o1", "title": "counters", "lane": "done",
     "labels": ["plan-child", FOUND_PREFIX + "run10", FIXED_PREFIX + "run12"]},
    {"id": "loop-o3", "title": "thrash", "lane": "ready",
     "labels": [FOUND_PREFIX + "run10"]},
    {"id": "unrelated", "title": "x", "lane": "inbox", "labels": ["plan"]},
]


def test_board_items_map_to_the_run_that_found_and_fixed_them():
    by = board_links_by_run(ITEMS)
    assert [i["id"] for i in by["run10"]["found"]] == ["loop-o1", "loop-o3"]
    assert [i["id"] for i in by["run12"]["fixed"]] == ["loop-o1"]
    assert by["run12"]["found"] == []
    assert "unrelated" not in str(by)


def test_an_item_can_be_found_in_one_run_and_fixed_in_another():
    by = board_links_by_run(ITEMS)
    assert "loop-o1" in str(by["run10"]["found"])
    assert "loop-o1" in str(by["run12"]["fixed"])


def test_malformed_items_and_labels_do_not_explode():
    assert board_links_by_run([None, {}, {"labels": None},
                               {"labels": [FOUND_PREFIX]}]) == {}
    assert board_links_by_run([]) == {}
