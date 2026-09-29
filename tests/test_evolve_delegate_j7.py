"""J7 (user, 2026-09-29): a delegated job keeps the delegator's task and the brief,
leaves a trajectory when it ends, is rated by the delegating agent, and only a
rated job is written to memory."""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import delegate_core as D  # noqa: E402

try:
    from Vera.vera.evolve import delegate_capabilities as DC
    import Vera.vera.fabric.context  # noqa: F401
except Exception:                                    # pragma: no cover
    DC = None

needs_app = pytest.mark.skipif(DC is None or not hasattr(DC, "cap_evolve_delegate_rate"),
                               reason="app module (with J7) not importable here")


def test_record_fields_carry_the_why_and_the_verdict():
    f = D.record_fields({"id": "dg1", "parent_task": "G: intent core " * 40,
                         "delegator": "claude:s1", "rating": "useful"})
    assert f["parent_task"].startswith("G: intent core") and len(f["parent_task"]) == 200
    assert f["delegator"] == "claude:s1" and f["verdict"] == "useful"
    assert D.record_fields({})["verdict"] == ""


@needs_app
def test_start_keeps_the_parent_task_and_the_brief(monkeypatch):
    store = {}

    async def fake_make(job_id, ref):
        return {"ok": True, "path": "/wt/" + job_id, "head": "abc"}

    async def fake_save(job):
        store[job["id"]] = dict(job)

    async def fake_run(job, goal):
        return None
    monkeypatch.setattr(DC, "_make_worktree", fake_make)
    monkeypatch.setattr(DC, "_save", fake_save)
    monkeypatch.setattr(DC, "_run", fake_run)
    monkeypatch.setattr(DC._orch, "is_dev_sandbox", lambda: False)
    monkeypatch.setattr(DC, "_redis", lambda: object())

    async def go():
        out = await DC.cap_evolve_delegate_start(
            title="where", brief="Find the builder.", plan=["grep", "read"],
            suggest_caps="evolve.delegate.fs.grep", parent_task="Roadmap G", delegator="claude:s1")
        await asyncio.sleep(0)
        return out
    out = asyncio.run(go())
    job = store[out["job_id"]]
    assert job["parent_task"] == "Roadmap G" and job["delegator"] == "claude:s1"
    assert job["brief"] == "Find the builder." and job["plan"] == ["grep", "read"]
    assert job["suggest_caps"] == ["evolve.delegate.fs.grep"]
    assert job["goal"].startswith("DELEGATED TASK") and job["goal_chars"] == len(job["goal"])
    DC._WORKTREES.pop(out["job_id"], None)
    DC._TASKS.pop(out["job_id"], None)


def _rate_env(monkeypatch, traj, job=None, memory_id="m1"):
    seen = {"traj": None, "job": None, "memory": [], "marked": [], "recorded": []}

    async def load_traj(jid):
        return dict(traj) if traj else None

    async def save_traj(t):
        seen["traj"] = t

    async def load(jid):
        return dict(job) if job else None

    async def save(j):
        seen["job"] = j

    async def mark(j):
        seen["marked"].append(j.get("rating"))

    async def record(sid):
        seen["recorded"].append(sid)

    async def memory_store(text="", **kw):
        seen["memory"].append((text, kw))
        return {"id": memory_id}
    monkeypatch.setattr(DC, "_load_traj", load_traj)
    monkeypatch.setattr(DC, "_save_traj", save_traj)
    monkeypatch.setattr(DC, "_load", load)
    monkeypatch.setattr(DC, "_save", save)
    monkeypatch.setattr(DC, "_mark_run", mark)
    monkeypatch.setattr(DC, "_record_run", record)
    monkeypatch.setitem(DC.CAPABILITY_REGISTRY, DC.MEMORY_CAP, {"raw": memory_store})
    return seen


DONE = {"job_id": "dgX", "session_id": "delegate:dgX", "title": "t", "status": "done",
        "parent_task": "Roadmap G", "brief": "Find it.", "report": "## Summary\nIn a.py.",
        "counts": {"steps": 1, "calls": 2}, "loop": {"intent": "research"}, "mode": "report",
        "rating": None, "memory_ids": []}


@needs_app
def test_rating_labels_the_trajectory_the_record_and_writes_one_memory(monkeypatch):
    seen = _rate_env(monkeypatch, DONE, job={"id": "dgX", "session_id": "delegate:dgX"})
    out = asyncio.run(DC.cap_evolve_delegate_rate(job_id="dgX", verdict="useful",
                                                  notes="exact refs", by="claude:s1"))
    assert out == {"ok": True, "verdict": "useful", "memory_id": "m1"}
    t = seen["traj"]
    assert t["rating"]["verdict"] == "useful" and t["rating"]["by"] == "claude:s1"
    assert t["memory_ids"] == ["m1"]
    [(text, kw)] = seen["memory"]
    assert "Part of: Roadmap G" in text and kw["category"] == "delegate"
    assert "verdict:useful" in kw["tags"]
    assert seen["job"]["rating"] == "useful" and seen["marked"] == ["useful"]
    assert seen["recorded"] == ["delegate:dgX"]


@needs_app
def test_rate_refuses_a_running_job_and_an_unknown_verdict(monkeypatch):
    seen = _rate_env(monkeypatch, dict(DONE, status="running"))
    DC._TASKS["dgX"] = object()          # still running in this process
    try:
        busy = asyncio.run(DC.cap_evolve_delegate_rate(job_id="dgX", verdict="useful"))
    finally:
        DC._TASKS.pop("dgX", None)
    assert "still running" in busy["error"] and seen["memory"] == []
    _rate_env(monkeypatch, DONE)
    bad = asyncio.run(DC.cap_evolve_delegate_rate(job_id="dgX", verdict="great"))
    assert "verdict must be one of" in bad["error"]


@needs_app
def test_an_unrated_job_is_not_remembered(monkeypatch):
    seen = _rate_env(monkeypatch, DONE)
    assert asyncio.run(DC._remember(dict(DONE))) == ""
    assert seen["memory"] == []


@needs_app
def test_a_rebuilt_trajectory_keeps_its_rating(monkeypatch):
    saved = {}

    async def events(sid):
        return [{"type": "agent_loop_v6.intent", "intent": "research"}]

    async def load_traj(jid):
        return {"rating": {"verdict": "partly"}, "memory_ids": ["m0"]}

    async def save_traj(t):
        saved["t"] = t
    monkeypatch.setattr(DC, "_loop_events", events)
    monkeypatch.setattr(DC, "_load_traj", load_traj)
    monkeypatch.setattr(DC, "_save_traj", save_traj)
    t = asyncio.run(DC._keep_trajectory({"id": "dgX", "session_id": "delegate:dgX",
                                         "status": "done", "report": "## Findings\n- a.py:1"}))
    assert t["loop"]["intent"] == "research" and t["metrics"]["findings"] == 1
    assert saved["t"]["rating"]["verdict"] == "partly" and saved["t"]["memory_ids"] == ["m0"]


def test_the_run_keeps_the_trajectory_after_its_record():
    src = (ROOT / "vera" / "evolve" / "delegate_capabilities.py").read_text(encoding="utf-8")
    run = src[src.index("async def _run("):src.index("@capability(\n    \"evolve.delegate.start\"")]
    assert run.index("await _record_run(sid)") < run.index("await _keep_trajectory(job)")


@needs_app
def test_a_running_jobs_trajectory_is_rebuilt_not_a_stale_snapshot(monkeypatch):
    built = []

    async def load_traj(jid):
        return {"job_id": jid, "status": "running", "counts": {"calls": 1}}

    async def load(jid):
        return {"id": jid, "session_id": "delegate:" + jid, "status": "running"}

    async def keep(job):
        built.append(job["status"])
        return {"job_id": job["id"], "status": job["status"], "counts": {"calls": 9}}
    monkeypatch.setattr(DC, "_load_traj", load_traj)
    monkeypatch.setattr(DC, "_load", load)
    monkeypatch.setattr(DC, "_keep_trajectory", keep)
    DC._TASKS["dgR2"] = object()
    try:
        t = asyncio.run(DC._trajectory_for("dgR2"))
    finally:
        DC._TASKS.pop("dgR2", None)
    assert built == ["running"] and t["counts"]["calls"] == 9

    async def load_done(jid):
        return {"job_id": jid, "status": "done"}
    monkeypatch.setattr(DC, "_load_traj", load_done)
    assert asyncio.run(DC._trajectory_for("dgR2"))["status"] == "done" and built == ["running"]
