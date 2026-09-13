"""A census row must say which code it ran on, and a run that crossed a
restart must show the goal where the code changed (census_core.code_of /
code_rollup).

Asked 2026-09-10: "we need to make sure census are tagged with the branches
they were running on and where the change happened (in the case of restarts)".
The default run of that day was stopped at goal 8, prod promoted from ab57820
to 1d7e978 and restarted, and the run resumed - two instruments in one file.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.census import census_core as cc  # noqa: E402


def row(gid, sha=None, branch="main", at_end=None, code_version=None):
    r = {"id": gid, "status": "done", "wall_s": 100}
    if sha:
        r["code"] = {"sha": sha, "sha_short": sha[:10], "branch": branch, "dirty": False,
                     "process_started_at": "t", "changed_during_goal": bool(at_end)}
        if at_end:
            r["code"]["at_end"] = {"sha": at_end, "sha_short": at_end[:10], "branch": branch}
    if code_version:
        r["code_version"] = code_version
    return r


A = "ab578205581feec15435bb357253a92d6c1a2da7"
B = "1d7e978d48a12a7c7bf369ebb9210c7af33fe204"


def test_code_of_prefers_provenance_then_event_stamps_then_unknown():
    assert cc.code_of(row("g", A))["sha_short"] == "ab57820558"
    assert cc.code_of(row("g", A))["source"] == "provenance"
    ev = cc.code_of(row("g", code_version=["ab57820558@main"]))
    assert ev["sha_short"] == "ab57820558" and ev["branch"] == "main" and ev["source"] == "events"
    assert cc.code_of({"id": "g"})["source"] == "none"
    assert cc.code_of({"id": "g"})["sha"] == ""


def test_a_single_code_run_is_one_segment_and_not_changed():
    r = cc.code_rollup([row("a", A), row("b", A), row("c", A)])
    assert r["codes"] == ["ab57820558@main"]
    assert r["changed"] is False and r["changes"] == []
    assert r["recorded_goals"] == 3 and r["unknown_goals"] == 0


def test_a_restart_between_goals_names_the_boundary_goal():
    # The 2026-09-10 default run: goals 1-7 on ab57820, then a promote+restart,
    # goals 8-12 on 1d7e978.
    recs = [row(g, A) for g in ("g1", "g2", "g3")] + [row(g, B) for g in ("g4", "g5")]
    r = cc.code_rollup(recs)
    assert r["codes"] == ["ab57820558@main", "1d7e978d48@main"]
    assert r["changed"] is True
    assert r["changes"] == [{"goal": "g4", "from": "ab57820558@main", "to": "1d7e978d48@main",
                             "kind": "between_goals"}]
    assert [s["first_goal"] for s in r["segments"]] == ["g1", "g4"]
    assert [s["goals"] for s in r["segments"]] == [3, 2]


def test_a_restart_during_a_goal_is_named_on_that_goal():
    recs = [row("g1", A), row("g2", A, at_end=B), row("g3", B)]
    r = cc.code_rollup(recs)
    assert r["changed"] is True
    kinds = [(c["goal"], c["kind"]) for c in r["changes"]]
    assert ("g2", "during_goal") in kinds
    # g3 continues the post-restart segment; it is not a second change.
    assert r["codes"] == ["ab57820558@main", "1d7e978d48@main"]
    assert [s["goals"] for s in r["segments"]] == [2, 1]


def test_a_branch_change_is_a_code_change_even_on_the_same_sha():
    recs = [row("g1", A, branch="main"), row("g2", A, branch="bleeding-edge")]
    r = cc.code_rollup(recs)
    assert r["changed"] and r["branches"] == ["bleeding-edge", "main"]


def test_rows_without_code_are_counted_not_guessed():
    r = cc.code_rollup([{"id": "old1"}, {"id": "old2"}, row("g3", B)])
    assert r["unknown_goals"] == 2 and r["recorded_goals"] == 1
    assert r["changed"] is False


def test_summarise_run_carries_the_rollup():
    s = cc.summarise_run("run52", [row("g1", A), row("g2", B)])
    assert s["code"]["changed"] is True
    assert s["code"]["codes"] == ["ab57820558@main", "1d7e978d48@main"]
