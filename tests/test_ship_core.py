"""The Ship page is one table whose rows are branches, built from the five
pages it replaced. These pin the row shape, how the five stores meet on a
branch, the stage word, the superseded-pending rule, the order (trunk, then
what needs a person, then newest first) and the filters.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import ship_core as sc  # noqa: E402


def pipe(pid, branch, decision="promoted", created="2026-09-10T10:00:00Z", **kw):
    p = {"id": pid, "kind": "code", "profile": "adopted", "status": "adopted", "decision": decision,
         "created_at": created, "ended_at": "", "branch": branch, "gate_passed": True, "gate_delta": None,
         "repo": "vera", "controller": "claude_code", "review_requested": False, "adopted": True,
         "session_id": "sess-1", "live": False}
    p.update(kw)
    return p


def sbx(name, branch, **kw):
    s = {"role": "spawned", "branch": branch, "name": name, "port": 8983, "redis_db": 15, "running": True,
         "paused": False, "pinned": False, "owner": "claude_code", "session_id": "sess-1",
         "last_activity": "2026-09-10T12:00:00Z", "head_commit": "d8604e66066871b859cf", "dirty": False,
         "merged_to_bleeding_edge": False, "state": "healthy", "worktree": "/wt/" + name,
         "workplan": {"title": "Plan", "status": "planned", "step_count": 3, "completed_steps": 1}}
    s.update(kw)
    return s


def trun(branch, ts, ok=True, total=3800, **kw):
    r = {"ts": ts, "branch": branch, "pipeline_id": "", "markers": "critical", "ok": ok,
         "passed": total - (0 if ok else 3), "failed": 0 if ok else 3, "total": total, "summary": "s"}
    r.update(kw)
    return r


EDGES = [{"name": "bleeding-edge", "branch": "bleeding-edge", "default": True, "exists": True, "head": "b72749e1234",
          "main_state": "released", "description": "the staging trunk",
          "container": {"name": "vera-dev-mirror", "running": True, "port": 8982, "redis_db": 3, "pinned": True}},
         {"name": "bleeding-edge-design", "branch": "bleeding-edge-design", "exists": True, "head": "5199321abcd",
          "main_state": "diverged", "container": {}}]
GIT = {"branch": "main", "dirty": False, "dirty_files": [], "branches": ["loop-lab/bleeding-edge-mirror"], "repo": "vera"}


def test_a_branch_is_one_row_from_every_store():
    rows = sc.branch_rows([pipe("b2af2123", "feat/x")], [sbx("vera-dev-feat-x", "feat/x")],
                          [trun("feat/x", "2026-09-10T10:05:00Z", total=3831),
                           trun("feat/x", "2026-09-10T09:00:00Z", ok=False, total=3800)], EDGES, GIT)
    r = next(x for x in rows if x["branch"] == "feat/x")
    assert r["role"] == "feature" and r["repo"] == "vera"
    assert r["pipeline"]["id"] == "b2af2123" and r["pipelines_n"] == 1 and r["pending_n"] == 0
    assert r["sandbox"]["name"] == "vera-dev-feat-x" and r["sandbox"]["running"] is True
    assert r["sandbox"]["workplan"] == {"title": "Plan", "status": "planned", "step_count": 3, "completed_steps": 1}
    assert r["sandbox"]["worktree"] == "/wt/vera-dev-feat-x", "the registry's record rides whole: the detail draws every control from it"
    assert r["tests"]["ok"] is True and r["tests"]["total"] == 3831 and r["tests"]["runs_n"] == 2
    assert r["tests"]["red_runs"] == 1 and r["tests"]["delta"] == 31 and r["tests"]["fewer"] is False
    assert r["head"] == "d8604e660668", "the sandbox's head, twelve chars"
    assert r["owner"] == "claude_code" and r["session_id"] == "sess-1"
    assert r["merged"] is True and r["stage"] == "merged"
    assert r["last_activity"] == "2026-09-10T12:00:00Z", "the newest of pipeline, sandbox and test times"


def test_the_trunk_rows_come_from_git_and_the_edges():
    rows = sc.branch_rows([], [], [], EDGES, GIT)
    names = [r["branch"] for r in rows]
    assert names[:3] == ["main", "bleeding-edge", "bleeding-edge-design"], "main, then the edges, before anything"
    main = rows[0]
    assert main["role"] == "main" and main["dirty"] is False and main["stage"] == "idle"
    be = rows[1]
    assert be["role"] == "edge" and be["edge"]["main_state"] == "released" and be["edge"]["container"]["port"] == 8982
    assert be["merged"] is True and be["stage"] == "merged", "released = main is at this edge"
    assert be["head"] == "b72749e1234"
    mirror = next(r for r in rows if r["branch"] == "loop-lab/bleeding-edge-mirror")
    assert mirror["role"] == "mirror"


def one(rows, branch):
    return next(r for r in rows if r["branch"] == branch)


def test_the_stage_word():
    live = one(sc.branch_rows([pipe("p1", "feat/live", decision="pending", live=True)], [], [], [], {}), "feat/live")
    assert live["stage"] == "live" and live["live"] is True and live["pending_n"] == 0, "a live pipeline is not yet a decision"
    review = one(sc.branch_rows([pipe("p2", "feat/rv", decision="pending")], [], [], [], {}), "feat/rv")
    assert review["stage"] == "review" and review["pending_n"] == 1
    held = one(sc.branch_rows([pipe("p3", "feat/h", decision="held")], [], [], [], {}), "feat/h")
    assert held["stage"] == "review"
    asked = one(sc.branch_rows([pipe("p4", "feat/a", decision="promoted", review_requested=True)], [], [], [], {}), "feat/a")
    assert asked["stage"] == "merged" and asked["review_requested"] is False, "a decided pipeline's review flag is history"
    gate = one(sc.branch_rows([pipe("p5", "feat/g", decision="pending", gate_passed=False)], [], [], [], {}), "feat/g")
    assert gate["stage"] == "review", "holding for a call outranks its failed gate"
    failed = one(sc.branch_rows([pipe("p6", "feat/f", decision="", gate_passed=False)], [], [], [], {}), "feat/f")
    assert failed["stage"] == "red"
    red = one(sc.branch_rows([], [], [trun("feat/t", "2026-09-10T09:00:00Z", ok=False)], [], {}), "feat/t")
    assert red["stage"] == "red" and red["tests"]["ok"] is False
    landed = one(sc.branch_rows([pipe("p7", "feat/t2", decision="promoted", created="2026-09-10T10:00:00Z")], [],
                                [trun("feat/t2", "2026-09-10T09:00:00Z", ok=False)], [], {}), "feat/t2")
    assert landed["stage"] == "merged", "a promote after the red run is what counts"
    idle = one(sc.branch_rows([], [sbx("s", "feat/i")], [], [], {}), "feat/i")
    assert idle["stage"] == "idle" and idle["sandbox"]["name"] == "s"
    merged_sbx = one(sc.branch_rows([], [sbx("s2", "feat/m", merged_to_bleeding_edge=True)], [], [], {}), "feat/m")
    assert merged_sbx["stage"] == "merged"


def test_a_pending_pipeline_older_than_the_promote_is_superseded():
    """Found on prod: feat/agent-registry was adopted twice three minutes
    apart; the first stayed 'pending' forever and the branch read as
    awaiting a decision after its second pipeline had been promoted."""
    rows = sc.branch_rows([pipe("old", "feat/ar", decision="pending", created="2026-09-09T15:28:56Z"),
                           pipe("new", "feat/ar", decision="promoted", created="2026-09-09T15:31:28Z")], [], [], [], {})
    r = one(rows, "feat/ar")
    assert r["pipeline"]["id"] == "new", "newest first"
    assert r["pending_n"] == 0 and r["stage"] == "merged"
    assert [p["superseded"] for p in r["pipelines"]] == [False, True]
    again = one(sc.branch_rows([pipe("old", "feat/ar", decision="promoted", created="2026-09-09T15:28:56Z"),
                            pipe("new", "feat/ar", decision="pending", created="2026-09-09T15:31:28Z")], [], [], [], {}), "feat/ar")
    assert again["pending_n"] == 1 and again["stage"] == "review", "a pending pipeline newer than the promote is owed"


def test_several_containers_on_one_branch_pick_the_running_primary():
    rows = sc.branch_rows([], [sbx("down", "feat/x", running=False, role="spawned"),
                               sbx("up", "feat/x", running=True, role="spawned"),
                               sbx("prime", "feat/x", running=True, role="primary")], [], [], {})
    assert one(rows, "feat/x")["sandbox"]["name"] == "prime"
    rows = sc.branch_rows([], [sbx("down", "feat/x", running=False), sbx("up", "feat/x", running=True)], [], [], {})
    assert one(rows, "feat/x")["sandbox"]["name"] == "up"


def test_the_order_is_trunk_then_who_needs_a_person_then_newest():
    rows = sc.branch_rows(
        [pipe("a", "feat/old-merged", created="2026-09-01T00:00:00Z"),
         pipe("b", "feat/new-merged", created="2026-09-10T00:00:00Z"),
         pipe("c", "feat/review", decision="pending", created="2026-09-02T00:00:00Z"),
         pipe("d", "feat/live", decision="pending", live=True, created="2026-09-03T00:00:00Z")],
        [sbx("s", "feat/idle", last_activity="2026-09-05T00:00:00Z")],
        [trun("feat/red", "2026-09-04T00:00:00Z", ok=False)], EDGES, GIT)
    assert [r["branch"] for r in rows] == [
        "main", "bleeding-edge", "bleeding-edge-design",
        "feat/live", "feat/review", "feat/red",
        "feat/new-merged", "feat/idle", "feat/old-merged", "loop-lab/bleeding-edge-mirror"]


def test_the_filters_and_the_summary():
    rows = sc.branch_rows(
        [pipe("a", "feat/merged"), pipe("c", "feat/review", decision="pending", controller="codex")],
        [sbx("vera-dev-idle", "feat/idle")], [trun("feat/red", "2026-09-04T00:00:00Z", ok=False)], EDGES, GIT)
    assert [r["branch"] for r in sc.filter_rows(rows, stage="review")] == ["feat/review"]
    assert [r["branch"] for r in sc.filter_rows(rows, role="edge")] == ["bleeding-edge", "bleeding-edge-design"]
    assert [r["branch"] for r in sc.filter_rows(rows, text="codex")] == ["feat/review"]
    assert [r["branch"] for r in sc.filter_rows(rows, text="vera-dev-idle")] == ["feat/idle"], "a sandbox name finds its branch"
    assert [r["branch"] for r in sc.filter_rows(rows, text="  A ")] != [], "case-insensitive, trimmed (pipeline id)"
    assert "feat/merged" not in [r["branch"] for r in sc.filter_rows(rows, hide_merged=True)]
    assert "bleeding-edge" not in [r["branch"] for r in sc.filter_rows(rows, hide_merged=True)], "released is merged"
    s = sc.summary(rows)
    assert s["count"] == len(rows) and s["stages"]["review"] == 1 and s["stages"]["red"] == 1
    assert s["roles"]["main"] == 1 and s["roles"]["edge"] == 2 and s["roles"]["mirror"] == 1
    assert s["sandboxes"] == 1 and s["sandboxes_running"] == 1


def test_a_pipeline_without_a_branch_is_not_a_row():
    rows = sc.branch_rows([pipe("v1", "", kind="variant")], [], [], [], {})
    assert [r["branch"] for r in rows] == ["main"], "main is always a row; the variant pipeline is not"
