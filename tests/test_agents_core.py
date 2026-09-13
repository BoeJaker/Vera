"""The Agents page is one table whose rows are agent sessions (or, in the
other mode, board items), built from the five pages it replaced. These pin
the row shape, how the watch, the board, the pipelines and the sandboxes
meet on a session, the state word, the order, the filters and the swarm's
counts.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import agents_core as ac  # noqa: E402


def watch(sid, state="untracked", **kw):
    s = {"claude_session_id": sid, "title": "Fix the thing", "project_dir": "--llm-int-boejaker-Vera",
         "last_ts": "2026-09-10T10:00:00Z", "turns": 12, "commit_count": 2, "state": state, "action": "none",
         "reason": "why", "age_s": 99999.0, "resume_ok": False, "claims": [], "pipelines": []}
    s.update(kw)
    return s


def item(iid, session, lane="done", **kw):
    i = {"id": iid, "title": "item " + iid, "lane": lane, "labels": ["loop-lab"], "agent": "claude", "repo": "vera",
         "project": "", "plan": "plan-x", "branch": "feat/" + iid, "pipeline": "", "session": session, "executor": "",
         "model": "", "comment_count": 1, "created_at": "2026-09-10T08:00:00Z", "updated_at": "2026-09-10T09:00:00Z"}
    i.update(kw)
    return i


def pipe(pid, session, branch="feat/x", decision="promoted", live=False, created="2026-09-10T09:30:00Z", **kw):
    p = {"id": pid, "branch": branch, "decision": decision, "gate_passed": True, "live": live, "kind": "code",
         "created_at": created, "session_id": session, "controller": "claude_code"}
    p.update(kw)
    return p


def sbx(name, session, branch="feat/x", **kw):
    s = {"name": name, "branch": branch, "running": True, "paused": False, "port": 8983, "session_id": session,
         "owner": "claude_code", "last_activity": "2026-09-10T11:00:00Z"}
    s.update(kw)
    return s


def test_a_session_is_one_row_from_every_store():
    rows = ac.session_rows([watch("s1", claims=[{"id": "i1", "title": "item i1"}])],
                           [item("i1", "s1", lane="in_progress"), item("i2", "s1", lane="done", updated_at="2026-09-10T09:30:00Z")],
                           [pipe("p1", "s1", branch="feat/i1", live=False)], [sbx("vera-dev-feat-i1", "s1", branch="feat/i1")])
    r = next(x for x in rows if x["id"] == "s1")
    assert r["kind"] == "session" and r["agent"] == "claude" and r["chat"] is True and r["title"] == "Fix the thing"
    assert r["turns"] == 12 and r["commit_count"] == 2 and r["watch_state"] == "untracked"
    assert r["items_n"] == 2 and r["items_open"] == 1 and r["by_lane"] == {"in_progress": 1, "done": 1}
    assert [i["id"] for i in r["items"]] == ["i2", "i1"], "newest first"
    assert r["claims"] == [{"id": "i1", "title": "item i1"}]
    assert r["pipelines_n"] == 1 and r["pipelines"][0]["id"] == "p1" and r["live"] is False
    assert r["sandboxes"][0]["name"] == "vera-dev-feat-i1" and r["branches"] == ["feat/i1", "feat/i2"]
    assert r["state"] == "working", "an item in progress"
    assert r["last_activity"] == "2026-09-10T11:00:00Z", "the newest of the session, its items, pipelines and sandboxes"


def test_a_session_the_watch_does_not_know_comes_from_the_board():
    rows = ac.session_rows([], [item("a", "codex-run-1", lane="done", agent="codex", updated_at="2026-09-10T09:00:00Z"),
                                item("b", "codex-run-1", lane="done", agent="codex", updated_at="2026-09-10T09:30:00Z")],
                           [pipe("p9", "codex-run-1")], [])
    r = rows[0]
    assert r["id"] == "codex-run-1" and r["agent"] == "codex" and r["chat"] is False
    assert r["title"] == "item b", "labelled by its newest item"
    assert r["state"] == "done" and r["pipelines_n"] == 1


def test_the_state_word():
    assert ac.session_rows([watch("w", state="resumable")], [], [], [])[0]["state"] == "resumable"
    assert ac.session_rows([watch("w", state="stalled")], [], [], [])[0]["state"] == "stalled"
    assert ac.session_rows([watch("w", state="declared-block")], [], [], [])[0]["state"] == "declared-block"
    assert ac.session_rows([watch("w", state="live")], [], [], [])[0]["state"] == "live"
    assert ac.session_rows([watch("w", state="human")], [], [], [])[0]["state"] == "human"
    assert ac.session_rows([watch("w", state="finished-unreported")], [], [], [])[0]["state"] == "finished-unreported"
    assert ac.session_rows([watch("w")], [], [], [])[0]["state"] == "idle", "untracked with nothing open is idle"
    assert ac.session_rows([watch("w", state="stalled")], [item("i", "w", lane="in_progress")], [], [])[0]["state"] == "stalled", \
        "the watch's silence outranks an item still marked in progress"
    assert ac.session_rows([watch("w")], [item("i", "w", lane="ready")], [], [])[0]["state"] == "working", "an open item is work"
    assert ac.session_rows([watch("w")], [], [pipe("p", "w", decision="pending", live=True)], [])[0]["state"] == "live"
    assert ac.session_rows([], [item("i", "x", lane="done"), item("j", "x", lane="dropped")], [], [])[0]["state"] == "done"


def test_an_untracked_session_that_spoke_recently_is_active():
    """`untracked` means no board claim - not "not running". Every ingested
    session on this instance is untracked (90 of 90, 2026-09-07), so the
    Swarm's list, which tested state alone, was empty by construction and
    could never show your own session. Recency is the test."""
    rows = ac.session_rows([watch("fresh", age_s=120.0), watch("old", age_s=99999.0)], [], [], [], recent_s=2700)
    by = {r["id"]: r for r in rows}
    assert by["fresh"]["state"] == "recent" and by["old"]["state"] == "idle"
    assert [r["id"] for r in rows] == ["fresh", "old"], "the one that spoke leads"
    c = ac.swarm_counts(rows, [], [], [])
    assert c["sessions_active"] == 1 and c["sessions"] == 2
    tight = ac.session_rows([watch("fresh", age_s=120.0)], [], [], [], recent_s=60)
    assert tight[0]["state"] == "idle", "the watch's own stalled window is the threshold"
    board_only = ac.session_rows([], [item("i", "codex-x", lane="done", updated_at="2026-09-10T09:00:00Z")], [], [])
    assert board_only[0]["state"] == "done", "recency is the watch's fact; a board-only session has no age"


def test_loops_and_editors_are_rows_while_they_run():
    rows = ac.session_rows([], [], [], [],
                           loops=[{"id": "imp-1", "status": "running", "profile": "planning", "rounds_done": 2, "max_rounds": 5,
                                   "started_at": "2026-09-10T12:00:00Z"},
                                  {"id": "imp-0", "status": "done"}],
                           editors=[{"id": "ed-1", "status": "queued", "model": "gpt-oss:20b", "ts": "2026-09-10T12:01:00Z"},
                                    {"id": "ed-2", "status": "running", "model": "gpt-oss:20b", "instance": "LLM", "ts": "2026-09-10T12:02:00Z"},
                                    {"id": "ed-0", "status": "done"}])
    by = {r["id"]: r for r in rows}
    assert set(by) == {"imp-1", "ed-1", "ed-2"}, "finished loops and editors are the Work page's, not agents at work"
    assert by["imp-1"]["kind"] == "loop" and by["imp-1"]["agent"] == "vera" and by["imp-1"]["state"] == "live"
    assert by["imp-1"]["title"] == "improve planning" and by["imp-1"]["status"] == "running r2/5"
    assert by["ed-1"]["kind"] == "editor" and by["ed-1"]["state"] == "queued"
    assert by["ed-2"]["state"] == "live" and by["ed-2"]["status"] == "running @LLM"
    assert [r["id"] for r in rows][0] in ("imp-1", "ed-2"), "live first"


def test_the_order_is_live_working_then_who_needs_a_person_then_newest():
    rows = ac.session_rows(
        [watch("old-idle", last_ts="2026-09-01T00:00:00Z"), watch("new-idle", last_ts="2026-09-10T00:00:00Z"),
         watch("stalled", state="stalled", last_ts="2026-09-05T00:00:00Z"),
         watch("human", state="human", last_ts="2026-09-09T00:00:00Z")],
        [item("i", "working", lane="in_progress", updated_at="2026-09-02T00:00:00Z")],
        [pipe("p", "live", live=True, decision="pending", created="2026-09-03T00:00:00Z")], [])
    assert [r["id"] for r in rows] == ["live", "working", "stalled", "human", "new-idle", "old-idle"]


def test_item_rows_and_their_filters():
    items = [item("a", "s1", lane="done", updated_at="2026-09-10T09:00:00Z"),
             item("b", "s2", lane="in_progress", agent="codex", updated_at="2026-09-09T09:00:00Z", repo="other"),
             item("c", "", lane="ready", updated_at="2026-09-10T10:00:00Z", plan="plan-y", labels=["ui"])]
    rows = ac.item_rows(items, {"s2": {"state": "working"}})
    assert [r["id"] for r in rows] == ["b", "c", "a"], "live lanes first, then open, then the rest; newest first within"
    assert rows[0]["agent"] == "codex" and rows[0]["session_state"] == "working" and rows[0]["session"] == "s2"
    assert rows[2]["session_state"] == "", "a session the table does not know says nothing"
    assert [r["id"] for r in ac.filter_items(rows, hide_done=True)] == ["b", "c"]
    assert [r["id"] for r in ac.filter_items(rows, lane="ready")] == ["c"]
    assert [r["id"] for r in ac.filter_items(rows, agent="codex")] == ["b"]
    assert [r["id"] for r in ac.filter_items(rows, repo="other")] == ["b"]
    assert [r["id"] for r in ac.filter_items(rows, plan="plan-y")] == ["c"]
    assert [r["id"] for r in ac.filter_items(rows, text="UI")] == ["c"], "labels count, case-insensitive"
    assert [r["id"] for r in ac.filter_items(rows, branch="feat/a")] == ["a"]


def test_session_filters_and_summary():
    rows = ac.session_rows([watch("s1"), watch("s2", state="stalled", title="Ship it", agent="codex")],
                           [item("i", "s1", lane="in_progress", branch="feat/i")], [], [])
    assert [r["id"] for r in ac.filter_sessions(rows, state="stalled")] == ["s2"]
    assert [r["id"] for r in ac.filter_sessions(rows, agent="codex")] == ["s2"]
    assert [r["id"] for r in ac.filter_sessions(rows, text="ship")] == ["s2"]
    assert [r["id"] for r in ac.filter_sessions(rows, branch="feat/i")] == ["s1"]
    assert [r["id"] for r in ac.filter_sessions(rows, text="item i")] == ["s1"], "an item's title finds its session"
    idle = ac.session_rows([watch("s3")], [], [], [])
    assert ac.filter_sessions(idle, hide_idle=True) == []
    s = ac.summary(rows)
    assert s["count"] == 2 and s["states"] == {"working": 1, "stalled": 1} and s["agents"] == {"claude": 1, "codex": 1}


def test_the_swarm_counts_come_from_the_same_rows():
    items = [item("i", "s1", lane="in_progress"), item("j", "s1", lane="ready"), item("k", "s2", lane="done")]
    pipes = [pipe("p", "s3", live=True, decision="pending")]
    rows = ac.session_rows([watch("s1"), watch("s2"), watch("s9", state="stalled")], items, pipes,
                           [sbx("a", "s1"), sbx("b", "s2", running=False)],
                           loops=[{"id": "L", "status": "running"}], editors=[{"id": "E", "status": "queued"}])
    c = ac.swarm_counts(rows, items, [sbx("a", "s1"), sbx("b", "s2", running=False)], pipes)
    assert c == {"loops": 1, "editors": 1, "pipelines_live": 1, "dispatched": 1, "sessions_active": 3, "sessions": 4,
                 "containers": 2, "containers_running": 1, "items_open": 2, "items": 3}


def test_agent_of_reads_every_naming():
    assert ac.agent_of("claude_code") == "claude" and ac.agent_of("codex-w4") == "codex"
    assert ac.agent_of("", None, "orchestrator") == "orchestrator" and ac.agent_of("vera") == "vera"
    assert ac.agent_of("selftest-b") == "other" and ac.agent_of("", "") == ""
