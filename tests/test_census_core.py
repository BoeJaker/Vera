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
    goal_evidence, history, live_progress, provenance_line, run_id_from_filename,
    run_outputs, run_provenance, run_sort_key, run_template, summarise_run,
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
    assert history([summarise_run("run1", [_rec("a")])])["trend_complete"] is None


# ── a partial run is not comparable, and must not drive the trend ──────────
def test_a_partial_run_is_excluded_from_the_trend():
    """Measured against the real files this once reported run1 -> current,
    done 2 -> 1, delta -1 — 'we got worse' — while run 12 was three goals into
    twelve. Half the archived files are partial, and the live census.jsonl is
    partial for the hours a run takes."""
    full_a = summarise_run("run10", [_rec(f"g{i}") for i in range(12)])
    full_b = summarise_run("run11", [_rec(f"g{i}") for i in range(11)]
                           + [_rec("g11", status="wall-cap")])
    in_flight = summarise_run(CURRENT, [_rec("g0")])          # 1 of 12 so far
    h = history([full_a, full_b, in_flight])
    assert h["full_goal_count"] == 12
    assert h["partial_count"] == 1 and h["complete_count"] == 2
    t = h["trend_complete"]
    assert t["from"] == "run10" and t["to"] == "run11"        # NOT `current`
    assert t["done_from"] == 12 and t["done_to"] == 11 and t["of_goals"] == 12


def test_partial_runs_are_flagged_on_the_run_itself():
    h = history([summarise_run("run10", [_rec(f"g{i}") for i in range(12)]),
                 summarise_run("run4-partial", [_rec("g0")])])
    by = {s["run_id"]: s for s in h["runs"]}
    assert by["run10"]["partial"] is False
    assert by["run4-partial"]["partial"] is True


def test_there_is_no_trend_over_every_run():
    """A number spanning partial and complete runs could only ever mislead, so
    it must not exist for the UI to reach for."""
    h = history([summarise_run("run10", [_rec(f"g{i}") for i in range(12)]),
                 summarise_run(CURRENT, [_rec("g0")])])
    assert "trend_all" not in h
    assert h["trend_complete"] is None       # only one complete run


def test_all_runs_equal_size_means_none_are_partial():
    h = history([summarise_run("run1", [_rec("a")]), summarise_run("run2", [_rec("a")])])
    assert h["partial_count"] == 0 and h["complete_count"] == 2


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


# ── the run's OUTPUT — the thing you actually judge by ─────────────────────
EVENTS = [
    {"type": "agent_loop_v5.step_done", "step_id": 1, "summary": "made the dir"},
    {"type": "agent_loop_v5.tool_done", "step_id": 2, "tool": "code.author",
     "ok": True, "args": {"path": "/workspace/statkit/stats.py"},
     "preview": "wrote 42 lines"},
    {"type": "agent_loop_v5.step_done", "step_id": 2, "summary": "raw tool dump"},
    {"type": "agent_loop_v6.step_finalized", "step_id": 2,
     "summary": "Authored stats.py; syntax VERIFIED by the code author."},
    {"type": "agent_loop_v5.proven_code_protected", "path": "stats.py"},
    {"type": "agent_loop_v5.proven_code_protected", "path": "stats.py"},
    {"type": "agent_loop_v6.done", "summary": "package created with mean/median"},
]


def test_the_run_output_is_recovered_from_its_events():
    """Neither the census record nor the trace digest carries a line of what the
    loop actually wrote."""
    o = run_outputs(EVENTS)
    assert o["has_final"] is True
    assert o["final"] == "package created with mean/median"


def test_a_finalised_step_summary_wins_over_the_raw_one():
    """step_finalized is the considered version; step_done is the tool dump."""
    o = run_outputs(EVENTS)
    s2 = [s for s in o["steps"] if s["step_id"] == 2][0]
    assert s2["final_summary"].startswith("Authored stats.py")
    assert s2["summary"] == "raw tool dump"        # kept, not discarded


def test_authored_files_are_listed_as_the_deliverable():
    o = run_outputs(EVENTS)
    assert [a["path"] for a in o["artifacts"]] == ["/workspace/statkit/stats.py"]
    assert o["artifacts"][0]["tool"] == "code.author" and o["artifacts"][0]["ok"] is True


def test_self_correction_events_are_counted():
    """The loop fighting itself is invisible in the step shape: build-multifile
    fired proven_code_protected EIGHT times while failing to persist one file."""
    assert run_outputs(EVENTS)["notable"]["proven_code_protected"] == 2


def test_a_cancelled_run_reports_no_final_rather_than_pretending():
    o = run_outputs([e for e in EVENTS if not str(e["type"]).endswith(".done")])
    assert o["has_final"] is False and o["final"] == ""


def test_long_output_is_clipped_for_a_panel_not_an_archive():
    o = run_outputs([{"type": "agent_loop_v6.done", "summary": "x" * 9000}], limit=100)
    assert len(o["final"]) == 101 and o["final"].endswith("…")


def test_a_non_authoring_tool_is_not_an_artifact():
    o = run_outputs([{"type": "agent_loop_v5.tool_done", "tool": "exec.bash.run",
                      "ok": True, "args": {"path": "/tmp/x"}, "preview": "ls"}])
    assert o["artifacts"] == []


def test_the_same_file_authored_twice_is_listed_once():
    ev = [{"type": "agent_loop_v5.tool_done", "tool": "code.author", "ok": True,
           "args": {"path": "a.py"}, "preview": "v1"},
          {"type": "agent_loop_v5.tool_done", "tool": "code.author", "ok": True,
           "args": {"path": "a.py"}, "preview": "v2"}]
    assert [a["path"] for a in run_outputs(ev)["artifacts"]] == ["a.py"]


def test_outputs_survive_an_empty_or_malformed_event_list():
    o = run_outputs([])
    assert o["final"] == "" and o["steps"] == [] and o["artifacts"] == []
    assert run_outputs([{"type": "x"}, {}])["notable"] == {}


# ── an in-flight run must be visible while it is in flight ─────────────────
GOAL_IDS = ["build-simple-code", "build-multifile", "research-web", "trivial-chat"]


def test_live_progress_shows_where_an_unfinished_run_has_got_to():
    """A goal only lands in census.jsonl when it FINISHES, so without this a
    census is invisible for the one-to-three hours it takes."""
    p = live_progress(GOAL_IDS, [_rec("build-simple-code")], active_goal="build-multifile")
    assert p["goals_total"] == 4 and p["completed"] == 1
    assert p["active_goal"] == "build-multifile" and p["position"] == 2
    assert p["remaining"] == ["research-web", "trivial-chat"]


def test_remaining_follows_the_harness_queue_order_not_the_records():
    p = live_progress(GOAL_IDS, [_rec("research-web")], active_goal="build-simple-code")
    assert p["remaining"] == ["build-multifile", "trivial-chat"]


def test_live_progress_with_nothing_running():
    p = live_progress(GOAL_IDS, [_rec("build-simple-code")])
    assert p["active_goal"] == "" and p["position"] is None
    assert len(p["remaining"]) == 3


def test_an_unmatched_active_goal_does_not_fake_a_position():
    p = live_progress(GOAL_IDS, [], active_goal="something-else")
    assert p["position"] is None and len(p["remaining"]) == 4


# ── liveness must defer to the loop's own rule ─────────────────────────────
_CAPS_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "vera", "census", "census_capabilities.py")


def _caps_src():
    with open(_CAPS_SRC, encoding="utf-8") as fh:
        return fh.read()


def test_live_defers_to_the_loops_own_staleness_rule():
    """A run orphaned by a restart keeps status:running forever - nothing is left
    alive to write a terminal status. census.live read Redis directly and skipped
    that correction, so it reported an 11-hour-old cancelled chat session as the
    live census run. Reuse the shared helper; never re-derive the threshold."""
    src = _caps_src()
    # The IMPORT specifically: a local rebinding of the same name would satisfy a
    # bare substring check while quietly reintroducing the bug.
    assert ("from Vera.vera.dag.dag_workshop_capabilities import _loop_run_is_stale"
            in src), "census.live must import the loop's own staleness helper"
    assert "await _loop_run_is_stale(" in src
    # A local threshold would drift from the loop's own definition.
    assert "_LOOP_STALE_SECS" not in src
    for invented in ("stale_secs", "STALE_SECONDS", "MAX_IDLE"):
        assert invented not in src


def test_live_reads_the_same_indexes_the_sessions_route_does():
    """The route prefers the durable history index and falls back to the resume
    index; reading only one silently misses runs."""
    src = _caps_src()
    assert "vera:loop:history:index" in src and "vera:loop:sessions" in src
    assert "vera:loop:history:run:" in src


def test_malformed_items_and_labels_do_not_explode():
    assert board_links_by_run([None, {}, {"labels": None},
                               {"labels": [FOUND_PREFIX]}]) == {}
    assert board_links_by_run([]) == {}


# â”€â”€ who operated the run â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# A run in the archive was an orphan the moment its chat log scrolled away. The
# risk in fixing that is not that the field is missing; it is that a run gets
# credited to somebody who did not drive it.
def _row(op="claude", skill="improve-vera-sandboxed", tool="/loop",
         session="22e34f10", helpers=None, **extra):
    return dict({"status": "done",
                 "provenance": {"operator": op, "skill": skill, "tool": tool,
                                "caller_session": session,
                                "helpers": helpers or []}}, **extra)


def test_a_run_with_no_provenance_reads_as_unattributed_not_as_someone():
    """exec-family run 1 has provenance on no row at all - the field was added
    while it was running. That must read as unattributed, not be credited to
    whoever edited the harness last."""
    p = run_provenance([{"status": "done"}, {"status": "wall-cap"}])
    assert p["attributed"] is False
    assert p["operator"] == ""
    assert provenance_line(p) == "unattributed"


def test_the_operator_is_read_off_the_rows():
    p = run_provenance([_row(), _row()])
    assert p["attributed"] is True
    assert (p["operator"], p["skill"], p["tool"]) == \
           ("claude", "improve-vera-sandboxed", "/loop")
    assert p["session"] == "22e34f10"


def test_the_harness_spelling_of_session_is_translated_once():
    """The harness writes caller_session; everything here says session. If that
    difference leaks, the panel shows a blank session for every real run."""
    assert run_provenance([_row()])["session"] == "22e34f10"


def test_disagreeing_rows_are_reported_not_silently_resolved():
    """A run half-driven by somebody else is not a run driven by whoever
    happened to start it."""
    p = run_provenance([_row(op="claude"), _row(op="codex")])
    assert "operator" in p["mixed"]
    assert p["operator"] == "claude / codex"
    assert "mixed operator" in provenance_line(p)


def test_agreeing_rows_are_not_reported_as_mixed():
    assert run_provenance([_row(), _row(), _row()])["mixed"] == []


def test_helpers_are_a_union_across_rows_deduped_by_name():
    """A script written halfway through a run is still part of what drove it."""
    a = _row(helpers=[{"name": "a.py", "purpose": "counts rows"}])
    b = _row(helpers=[{"name": "a.py", "purpose": "counts rows"},
                      {"name": "b.py", "purpose": "restarts prod"}])
    assert [h["name"] for h in run_provenance([a, b])["helpers"]] == ["a.py", "b.py"]


def test_partial_attribution_still_counts_as_attributed():
    """An operator with no skill recorded is still a named operator."""
    p = run_provenance([_row(skill="", tool="")])
    assert p["attributed"] is True
    assert provenance_line(p) == "claude"


def test_a_provenance_block_of_only_blanks_is_not_attribution():
    p = run_provenance([_row(op="", skill="", tool="", session="")])
    assert p["attributed"] is False


def test_the_line_counts_helpers_rather_than_listing_them():
    p = run_provenance([_row(helpers=[{"name": "a.py"}, {"name": "b.py"}])])
    assert "2 helpers" in provenance_line(p)
    assert "1 helper" in provenance_line(
        run_provenance([_row(helpers=[{"name": "a.py"}])]))


def test_rows_counted_so_a_partly_stamped_run_is_visible():
    p = run_provenance([_row(), {"status": "done"}])
    assert (p["rows_with"], p["rows_total"]) == (1, 2)


def test_the_summary_carries_provenance_so_the_list_need_not_reread_rows():
    s = summarise_run("run50", [_row(), _row()])
    assert s["provenance"]["operator"] == "claude"


def test_provenance_survives_rubbish_rows():
    for junk in (None, [], [None], [{"provenance": "not a dict"}], [{"provenance": []}]):
        assert run_provenance(junk)["attributed"] is False


# â”€â”€ which template a run belongs to â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Every row the harness writes carries `template` and nothing surfaced it, so
# the panel had thirteen templates to choose from and no way to say which one a
# RUN came from. Comparing across templates is the one thing the census must
# never do silently.
def test_a_runs_template_is_read_off_its_rows():
    recs = [{"template": "exec-family"}, {"template": "exec-family"}]
    assert run_template(recs) == "exec-family"


def test_a_run_from_before_the_field_existed_is_unlabelled_not_guessed():
    """They are all default-template runs in practice, but saying so here would
    be a guess dressed as data."""
    assert run_template([{"status": "done"}, {"status": "done"}]) == ""
    assert run_template([]) == ""
    assert run_template(None) == ""


def test_rows_that_disagree_are_reported_as_mixed_not_averaged():
    """A file holding two templates is not a run of either."""
    got = run_template([{"template": "exec-family"}, {"template": "code-family"}])
    assert got == "mixed:code-family+exec-family"


def test_a_partly_labelled_run_takes_the_label_it_has():
    assert run_template([{"template": "exec-family"}, {"status": "done"}]) \
        == "exec-family"


def test_blank_templates_do_not_count_as_a_label():
    assert run_template([{"template": ""}, {"template": "   "}]) == ""


def test_rubbish_rows_do_not_raise():
    for junk in ([None], ["nope"], [{"template": None}], [{}]):
        assert run_template(junk) == "", junk


def test_the_summary_carries_the_template_so_the_panel_can_filter():
    s = summarise_run("run50", [{"status": "done", "template": "exec-family"}])
    assert s["template"] == "exec-family"


def test_an_unlabelled_run_still_summarises():
    assert summarise_run("run1", [{"status": "done"}])["template"] == ""
