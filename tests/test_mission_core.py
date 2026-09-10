"""Mission control is one table whose rows are events - audit actions,
errors in the work-queue, gates - built from the pages it replaced. These
pin the row shape, who an action is by, the record it names, the order
(open errors first, then newest), the filters and the counts the strip
shows.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import mission_core as mc  # noqa: E402


def audit(action, ts="2026-09-10T10:00:00Z", ok=True, **kw):
    e = {"ts": ts, "action": action, "summary": "did " + action, "ok": ok,
         "by": {"host": "LLM", "pid": 1, "dev": False, "ver": "8edc264"}}
    e.update(kw)
    return e


def err(eid, state="new", ts="2026-09-10T09:00:00Z", **kw):
    it = {"id": eid, "state": state, "source": "sandbox", "title": "ERROR something " + eid, "count": 2,
          "meta": {"severity": "warn"}, "suggestion": {}, "last_seen": ts}
    it.update(kw)
    return it


def gate(branch, ts="2026-09-10T08:00:00Z", ok=True, **kw):
    r = {"ts": ts, "branch": branch, "pipeline_id": "p-" + branch[-1], "markers": "critical", "ok": ok,
         "passed": 3800, "failed": 0 if ok else 1, "total": 3801, "summary": "3800 passed"}
    r.update(kw)
    return r


def test_an_action_is_an_event_with_who_and_what_it_names():
    rows = mc.event_rows([audit("pipeline.promote", id="e08045cc", kind="code", branch="feat/x", repo="vera")])
    r = rows[0]
    assert r["kind"] == "action" and r["action"] == "pipeline.promote" and r["family"] == "pipeline"
    assert r["ok"] is True and r["problem"] is False and r["who"] == "LLM" and r["ver"] == "8edc264"
    assert r["ref"] == {"kind": "pipeline", "id": "e08045cc"} and r["branch"] == "feat/x"
    dev = mc.event_rows([audit("sandbox.exec", by={"host": "LLM", "dev": True})])[0]
    assert dev["who"] == "LLM (dev)" and dev["family"] == "sandbox" and dev["ref"] is None
    ctrl = mc.event_rows([audit("unittest.run", controller="claude_code", branch="feat/y")])[0]
    assert ctrl["who"] == "claude_code" and ctrl["ref"] == {"kind": "branch", "id": "feat/y"}
    bad = mc.event_rows([audit("sandbox.down", ok=False)])[0]
    assert bad["ok"] is False and bad["problem"] is True
    unknown = mc.event_rows([audit("sandbox.exec", ok=None)])[0]
    assert unknown["ok"] is None and unknown["problem"] is False


def test_an_error_is_an_event_with_its_state_and_fix():
    r = mc.event_rows([], [err("x1", state="suggested", suggestion={"suggestion": "restart it", "remediation_id": "r1"},
                                pipeline_id="", meta={"severity": "crit", "run_id": "run-9"})])[0]
    assert r["kind"] == "error" and r["state"] == "suggested" and r["problem"] is True and r["ok"] is False
    assert r["action"] == "error.sandbox" and r["family"] == "errors" and r["level"] == "crit"
    assert r["suggestion"] == "restart it" and r["remediation"] is True and r["count"] == 2
    assert r["ref"] == {"kind": "run", "id": "run-9"} and r["error_id"] == "x1"
    done = mc.event_rows([], [err("x2", state="applied", pipeline_id="p9")])[0]
    assert done["ok"] is True and done["problem"] is False and done["ref"] == {"kind": "pipeline", "id": "p9"}
    gone = mc.event_rows([], [err("x3", state="dismissed")])[0]
    assert gone["ok"] is True and gone["problem"] is False


def test_a_gate_is_an_event():
    r = mc.event_rows([], [], [gate("feat/a", ok=False, failed=1)])[0]
    assert r["kind"] == "gate" and r["action"] == "gate.critical" and r["family"] == "unittest"
    assert r["ok"] is False and r["problem"] is True and r["failed"] == 1 and r["total"] == 3801
    assert r["ref"] == {"kind": "pipeline", "id": "p-a"} and r["branch"] == "feat/a"
    no_pipe = mc.event_rows([], [], [gate("feat/b", pipeline_id="")])[0]
    assert no_pipe["ref"] == {"kind": "branch", "id": "feat/b"}


def test_the_order_is_open_errors_first_then_newest():
    rows = mc.event_rows([audit("a.old", ts="2026-09-01T00:00:00Z"), audit("a.new", ts="2026-09-10T00:00:00Z")],
                         [err("open", state="new", ts="2026-08-01T00:00:00Z"), err("closed", state="applied", ts="2026-09-05T00:00:00Z")],
                         [gate("feat/g", ts="2026-09-07T00:00:00Z")])
    assert [r["id"] for r in rows] == ["e:open", "a:2026-09-10T00:00:00Z:a.new", "g:2026-09-07T00:00:00Z:feat/g",
                                       "e:closed", "a:2026-09-01T00:00:00Z:a.old"]


def test_the_filters():
    rows = mc.event_rows([audit("sandbox.exec"), audit("pipeline.adopt", ok=False, controller="claude_code"), audit("config.set")],
                         [err("e1", state="new"), err("e2", state="dismissed")], [gate("feat/x", ok=False), gate("feat/y")])
    ids = lambda rs: [r["id"] for r in rs]  # noqa: E731
    assert len(mc.filter_events(rows, kind="gate")) == 2 and len(mc.filter_events(rows, kind="error")) == 2
    assert ids(mc.filter_events(rows, family="pipeline")) == ["a:2026-09-10T10:00:00Z:pipeline.adopt"]
    assert set(ids(mc.filter_events(rows, problems=True))) == {"e:e1", "a:2026-09-10T10:00:00Z:pipeline.adopt", "g:2026-09-10T08:00:00Z:feat/x"}
    assert ids(mc.filter_events(rows, who="claude")) == ["a:2026-09-10T10:00:00Z:pipeline.adopt"]
    assert "a:2026-09-10T10:00:00Z:sandbox.exec" not in ids(mc.filter_events(rows, hide_exec=True))
    assert ids(mc.filter_events(rows, text="feat/y")) == ["g:2026-09-10T08:00:00Z:feat/y"]
    assert ids(mc.filter_events(rows, text="SOMETHING E2")) == ["e:e2"], "case-insensitive, over the summary"


def test_the_counts_are_what_master_counted():
    pipes = [{"id": "p1", "decision": "pending", "gate_passed": True, "live": False},
             {"id": "p2", "decision": "promoted", "gate_passed": True, "live": False},
             {"id": "p3", "decision": "pending", "gate_passed": None, "review_requested": True, "live": True},
             {"id": "p4", "decision": "held", "gate_passed": False, "live": False}]
    items = [{"lane": "in_progress"}, {"lane": "done"}, {"lane": "blocked"}, {"lane": "inbox"}]
    errors = [err("a"), err("b", state="suggested"), err("c", state="applied"), err("d", state="dismissed")]
    tests = [gate("feat/1", ok=False, ts="2026-09-10T08:00:00Z"), gate("feat/2", ts="2026-09-10T07:00:00Z")]
    c = mc.counts(pipes, items, errors, tests)
    assert c["needs_promotion"] == 2 and c["needs_promotion_ids"] == ["p1", "p3"]
    assert c["live_pipelines"] == 1 and c["active_items"] == 2
    assert c["errors"] == {"new": 1, "suggested": 1, "applied": 1, "dismissed": 1} and c["errors_open"] == 2
    assert c["gates_recent"] == 2 and c["gates_red"] == 1
    assert c["last_gate"] == {"ok": False, "branch": "feat/1", "ts": "2026-09-10T08:00:00Z", "summary": "3800 passed"}
    assert mc.counts([], [], [], [])["last_gate"] is None


def test_summary_counts_kinds_families_and_problems():
    rows = mc.event_rows([audit("sandbox.exec"), audit("pipeline.adopt", ok=False)], [err("e1")], [gate("feat/x")])
    s = mc.summary(rows)
    assert s["count"] == 4 and s["kinds"] == {"error": 1, "action": 2, "gate": 1}
    assert s["families"] == {"errors": 1, "sandbox": 1, "pipeline": 1, "unittest": 1} and s["problems"] == 2


def test_family_of_and_who_of():
    assert mc.family_of("bleeding_edge.promote_to_main") == "bleeding_edge" and mc.family_of("unittest.run") == "unittest"
    assert mc.family_of("census.control.set") == "census" and mc.family_of("weird") == "weird" and mc.family_of("") == ""
    assert mc.who_of({"by": "someone"}) == "someone" and mc.who_of({}) == ""
