"""evolve.delegate: hand a code-reporting task to a Vera loop (user, 2026-09-28).
Read-only, in its own worktree, async, board-updating. The safety properties
are the point of these tests: the loop may only use the jailed read tools, the
jail cannot be escaped, and the guard/worktree never outlive the job.
"""

import asyncio
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import delegate_core as D  # noqa: E402

try:
    from Vera.vera.evolve import delegate_capabilities as DC
    # What the loader registers in prod: the session cap guard and the
    # ide.code.* caps the jailed tools wrap.
    import Vera.vera.fabric.context  # noqa: F401
    import Vera.vera.ide.ide_code_capabilities  # noqa: F401
except Exception:                                    # pragma: no cover
    DC = None

needs_app = pytest.mark.skipif(DC is None, reason="app module not importable here")


def test_the_guard_admits_only_the_jailed_tools_pinned_to_the_job():
    g = D.guard_spec("dg1")
    assert g["allow"] == list(D.FS_CAPS)
    assert g["pin_args"] == {"evolve.delegate.fs.*": {"job": "dg1"}}
    gb = D.guard_spec("dg1", board_item="plan-x-3")
    assert D.BOARD_CAP in gb["allow"]
    assert gb["pin_args"][D.BOARD_CAP] == {"id": "plan-x-3", "frm": D.BOARD_FROM}
    assert not any(c.startswith(("ide.", "exec.", "code.", "fs.")) for c in gb["allow"])


def test_the_goal_carries_the_handover_the_tools_and_the_report_shape():
    g = D.compose_goal(title="Map the census harness", brief="How are rows ingested?",
                       plan="find the ingest cap\nfollow it to the store",
                       suggest_caps=["evolve.result.ingest"], suggest_commands="grep run_record_from",
                       ref="bleeding-edge", paths="vera/evolve", board_item="plan-a-1")
    for part in ("Map the census harness", "read-only checkout of vera @ bleeding-edge",
                 "How are rows ingested?", "1. find the ingest cap", "2. follow it to the store",
                 "START IN THESE PATHS: vera/evolve", "evolve.delegate.fs.grep",
                 "board.comment(kind='progress'", "evolve.result.ingest",
                 "- grep run_record_from", "## Findings", "path:line",
                 "Never state anything you did not read"):
        assert part in g, part


def test_the_jail_refuses_every_escape(tmp_path):
    root = tmp_path / "wt"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    os.symlink(str(outside), str(root / "link.txt"))
    assert D.jail_path(str(root), "pkg/a.py") == os.path.realpath(str(root / "pkg" / "a.py"))
    assert D.jail_path(str(root), "/pkg/a.py") == os.path.realpath(str(root / "pkg" / "a.py"))
    assert D.jail_path(str(root), "../secret.txt") is None
    assert D.jail_path(str(root), "pkg/../../secret.txt") is None
    assert D.jail_path(str(root), "link.txt") is None             # symlink out of the checkout
    assert D.jail_path("", "pkg/a.py") is None


def test_reports_and_paths():
    assert D.report_from({"deliverable": "## Summary\nx", "final": "y"}).startswith("## Summary")
    assert D.report_from({"final": "just final"}) == "just final"
    assert D.report_from(None) == ""
    assert D.relativise("/wt/delegate-dg1", "/wt/delegate-dg1/vera/a.py:3") == "vera/a.py:3"
    assert D.session_for("dg1") == "delegate:dg1" and D.worktree_name("dg1") == "delegate-dg1"
    assert D.terminal("done") and not D.terminal("running")


@needs_app
def test_the_jailed_tools_read_only_inside_the_checkout(tmp_path):
    root = tmp_path / "wt"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def alpha():\n    return 1\n\nclass Beta:\n    pass\n")
    (tmp_path / "secret.txt").write_text("TOKEN")
    DC._WORKTREES["dgT"] = str(root)
    try:
        async def go():
            ok = await DC.cap_evolve_delegate_fs_read(job="dgT", path="pkg/mod.py")
            bad = await DC.cap_evolve_delegate_fs_read(job="dgT", path="../secret.txt")
            absolute = await DC.cap_evolve_delegate_fs_read(job="dgT", path=str(tmp_path / "secret.txt"))
            grep = await DC.cap_evolve_delegate_fs_grep(job="dgT", pattern="alpha")
            unknown = await DC.cap_evolve_delegate_fs_read(job="nope", path="pkg/mod.py")
            return ok, bad, absolute, grep, unknown
        ok, bad, absolute, grep, unknown = asyncio.run(go())
        assert "def alpha" in str(ok)
        assert "error" in bad and "error" in absolute and "TOKEN" not in str(absolute)
        assert "pkg/mod.py" in str(grep) and str(root) not in str(grep)   # repo-relative
        assert unknown.get("error") == "unknown job"
    finally:
        DC._WORKTREES.pop("dgT", None)


def _fake_env(monkeypatch, result=None, raise_exc=None):
    seen = {"saved": [], "board": [], "guard_during": None, "dropped": [], "kwargs": None}
    store = {}

    async def fake_save(job):
        store[job["id"]] = dict(job)
        seen["saved"].append(job["status"])

    async def fake_load(jid):
        return store.get(jid)

    async def fake_drop(path):
        seen["dropped"].append(path)

    async def fake_board(id="", frm="", kind="", body="", **kw):
        seen["board"].append((id, frm, kind, body[:40]))

    async def fake_loop(goal="", trace_id=None, **kw):
        seen["kwargs"] = kw
        ctx = DC._ctx()
        seen["guard_during"] = ctx.get_session_cap_guard(kw["session_id"]) if ctx else None
        if raise_exc:
            raise raise_exc
        return result

    async def fake_emit(ev):
        return None

    monkeypatch.setattr(DC, "_save", fake_save)
    monkeypatch.setattr(DC, "_load", fake_load)
    monkeypatch.setattr(DC, "_drop_worktree", fake_drop)
    monkeypatch.setattr(DC, "emit_event", fake_emit)
    monkeypatch.setitem(DC.CAPABILITY_REGISTRY, "dag.agent_loop_v7", {"func": fake_loop})
    monkeypatch.setitem(DC.CAPABILITY_REGISTRY, "board.comment", {"raw": fake_board})
    return seen, store


def _job(board_item="plan-a-1"):
    return {"id": "dgR", "session_id": "delegate:dgR", "title": "t", "mode": "report",
            "effort": "max", "ref": "bleeding-edge", "head": "abc", "worktree": "/wt/delegate-dgR",
            "board_item": board_item, "plan_style": "stepwise", "max_steps": 8,
            "status": "starting", "report": "", "error": ""}


@needs_app
def test_a_run_is_guarded_reports_and_cleans_up(monkeypatch):
    seen, store = _fake_env(monkeypatch, result={"deliverable": "## Summary\nfound it"})
    asyncio.run(DC._run(_job(), "GOAL"))
    g = seen["guard_during"]
    assert g and g["allow"][:4] == list(D.FS_CAPS) and D.BOARD_CAP in g["allow"]
    assert DC._ctx().get_session_cap_guard("delegate:dgR") is None          # cleared after
    kw = seen["kwargs"]
    assert kw["effort"] == "max" and kw["prefer_terminal_tools"] is False
    assert kw["enable_step_questions"] is False and set(kw["base_toolkit"].split()) == set(D.FS_CAPS)
    assert store["dgR"]["status"] == "done" and store["dgR"]["report"].startswith("## Summary")
    assert seen["dropped"] == ["/wt/delegate-dgR"]
    kinds = [b[2] for b in seen["board"]]
    assert kinds[0] == "progress" and "Delegated to Vera" in seen["board"][0][3]
    assert seen["board"][-1][3].startswith("Report from Vera")


@needs_app
def test_a_failed_or_cancelled_run_says_so_and_still_cleans_up(monkeypatch):
    seen, store = _fake_env(monkeypatch, raise_exc=RuntimeError("model down"))
    asyncio.run(DC._run(_job(board_item=""), "GOAL"))
    assert store["dgR"]["status"] == "error" and "model down" in store["dgR"]["error"]
    assert seen["dropped"] and DC._ctx().get_session_cap_guard("delegate:dgR") is None
    assert seen["board"] == []                                              # no item, no comments

    seen2, store2 = _fake_env(monkeypatch, raise_exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(DC._run(_job(board_item=""), "GOAL"))
    assert store2["dgR"]["status"] == "cancelled" and seen2["dropped"]


@needs_app
def test_start_refuses_what_it_cannot_do_safely():
    async def go():
        return (await DC.cap_evolve_delegate_start(title="x", brief=""),
                await DC.cap_evolve_delegate_start(title="x", brief="b", mode="edit"),
                await DC.cap_evolve_delegate_start(title="x", brief="b", repo="other"))
    no_brief, edit, other = asyncio.run(go())
    assert "brief is required" in no_brief["error"]
    assert "mode must be one of report" in edit["error"]
    assert "only repo=vera" in other["error"]


@needs_app
def test_a_delegated_session_is_recorded_with_its_origin():
    from Vera.vera.evolve import loop_record_core as R
    assert R.origin_of("delegate:dg1") == "delegate"
