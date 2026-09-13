"""The suite runner honours the census posture: a seeded census task runs on
prod's own loop the way the harness ran every archived goal - no sandbox
whatever sandbox_mode says, no test denylist, the engine's own defaults -
and a run that hits its cap says `capped`, not "failed".

Runs in-container (Vera.vera resolves to THIS checkout); skips on a host
venv. Every side effect of _run_task is stubbed at the module.
"""
import asyncio
import os

import pytest

try:
    from Vera.vera.evolve import evolve_capabilities as EV
    from Vera.vera.evolve import census_seed as CS
except Exception:                                    # pragma: no cover
    EV = CS = None

_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(EV is not None and
                  os.path.realpath(getattr(EV, "__file__", "")).startswith(_HERE_ROOT))
pytestmark = pytest.mark.skipif(not _SAME_TREE, reason="app module not importable from THIS checkout here")

GOAL = {"id": "build-simple-code", "intent": "build", "tier": "simple", "output": "code", "shape": "one-file",
        "goal": "Create clock.html - a live digital clock.",
        "checks": [{"file": "clock.html", "exists": True, "why": "it was written"}]}


def run(coro):
    return asyncio.run(coro)


def _stub(monkeypatch, calls, *, loop_result=None, loop_raises=None):
    async def _cfg():
        return {"sandbox_mode": "require", "test_denylist": ["file.write", "exec."], "run_idle_timeout_s": 300, "run_max_s": 7200}

    async def _resolve(cfg, ttype):
        calls.append(("resolve_sandbox", cfg.get("sandbox_mode")))
        return {"use": False, "blocked": True, "reason": "sandbox_mode=require but the sandbox is unusable: no dev sandbox is up"}

    async def _loop(task, variant, run_id, timeout):
        calls.append(("run_loop_task", dict(task), timeout))
        if loop_raises:
            raise loop_raises
        return loop_result or {"final": "done", "steps": [{"cap": "file.write"}]}

    async def _push(compact, detail):
        calls.append(("push_run", compact, detail))

    async def _emit(ev):
        calls.append(("event", ev.get("type")))

    async def _files(task, sid):
        return {"clock.html": "<html>clock</html>"}

    async def _events(run_id):
        return []
    monkeypatch.setattr(EV, "_get_config", _cfg)
    monkeypatch.setattr(EV, "_resolve_sandbox", _resolve)
    monkeypatch.setattr(EV, "_run_loop_task", _loop)
    monkeypatch.setattr(EV, "_push_run", _push)
    monkeypatch.setattr(EV, "emit_event", _emit)
    monkeypatch.setattr(EV, "_fetch_check_files", _files)
    monkeypatch.setattr(EV, "_steps_from_events", _events)
    monkeypatch.setattr(EV, "_triggered_by", lambda: "user")


def test_a_census_task_runs_on_prods_loop_whatever_sandbox_mode_says(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    task = CS.goal_to_task(GOAL, "default")
    detail = run(EV._run_task(task, source="suite", run_id="r-census"))
    names = [c[0] for c in calls]
    assert "resolve_sandbox" not in names, "the posture never asks the sandbox"
    assert "run_loop_task" in names, "the loop ran"
    ran = next(c for c in calls if c[0] == "run_loop_task")[1]
    assert ran["_sandbox"] is False and ran["_denylist"] == [], "prod's own loop, no test denylist"
    assert detail["where"] == "prod" and detail["posture"] == "census" and detail["blocked"] is False
    assert detail["error"] == "" and detail["capped"] is False
    assert detail["checks_n"] == 1 and detail["checks_ok"] == 1 and detail["combined"] == 10.0, "its declared check, scored"


def test_an_ordinary_task_still_goes_through_the_sandbox_rules(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    task = dict(CS.goal_to_task(GOAL, "default"), posture="")
    detail = run(EV._run_task(task, source="suite", run_id="r-plain"))
    names = [c[0] for c in calls]
    assert "resolve_sandbox" in names and "run_loop_task" not in names, "require + no sandbox: refused, as before"
    assert detail["blocked"] is True and detail["where"] == "in-process" and detail["posture"] == ""
    assert "sandbox_mode=require" in detail["error"]


def test_a_run_that_hits_its_cap_is_capped_not_failed(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, loop_raises=asyncio.TimeoutError())
    task = CS.goal_to_task(GOAL, "default")
    detail = run(EV._run_task(task, source="suite", run_id="r-cap"))
    assert detail["capped"] is True and detail["error"] == "timeout after 1800s", "the seeded task's cap is the harness's"
    compact = next(c for c in calls if c[0] == "push_run")[1]
    assert compact["capped"] is True and compact["posture"] == "census" and compact["where"] == "prod"


def test_the_loop_call_is_the_harness_call():
    """What reaches loops.run for a census task: the goal, the engine's step
    ceiling, no headless override - the harness passed nothing else."""
    import inspect
    src = inspect.getsource(EV._run_loop_task)
    assert 'if not census:' in src and 'kw.setdefault("enable_step_questions", False)' in src
    assert 'kw["enable_dream_persistence"] = False' in src, "a census run never persists a goal or spawns a program"
    assert 'max_steps=int(task.get("max_steps") or (8 if census else 6))' in src
    assert EV._is_census_posture(CS.goal_to_task(GOAL, "default")) is True
    assert EV._is_census_posture({"id": "x", "type": "loop"}) is False
