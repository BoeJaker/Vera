"""stepwise-reviewed: stepwise plus a critic on the long-horizon CPU node that
reviews every finished step while the GPU carries on (user, 2026-09-28:
stepwise variations that use the nodes in parallel for more comprehensive
results). One review at a time, the latest step only when steps outpace it,
nothing ever waits for it, and a critique reaches the controller once and the
completion gate at the end.
"""

import asyncio
import inspect
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


def test_the_style_is_stepwise_plus_a_critic():
    s = PS.LOOP_STYLES["stepwise-reviewed"]
    base = PS.LOOP_STYLES["stepwise"]
    assert s["step_critic"] and not base.get("step_critic")
    for k in ("run_planner", "master_plan", "recon", "shape_guards", "stepwise_controller"):
        assert s[k] == base[k], k
    assert PS.controller_note(s) == PS.STEPWISE_CONTROLLER_NOTE


def test_a_critique_is_bounded_bullets_and_OK_means_nothing_to_add():
    assert PS.critic_note("OK") == "" and PS.critic_note("ok.") == "" and PS.critic_note("") == ""
    note = PS.critic_note("MISSING: the countdown still starts at 60, not 90\n"
                          "- NEXT: edit timer.html so the displayed value reads 90\nOK")
    assert note.splitlines() == ["  - MISSING: the countdown still starts at 60, not 90",
                                 "  - NEXT: edit timer.html so the displayed value reads 90"]
    many = PS.critic_note("\n".join("line %d" % i for i in range(20)))
    assert len(many.splitlines()) == 8 and len(many) <= PS.MAX_CRITIQUE_CHARS


def test_the_prompt_carries_the_goal_the_step_and_its_result():
    p = PS.critic_prompt("Make a 90 s timer", "timer.html shows 90",
                         {"id": 2, "title": "Change to 90", "success": "timer reads 90"},
                         {"summary": "edited timer.html", "ok": True, "met": True},
                         [{"id": 1, "title": "Create timer", "ok": True}])
    for part in ("GOAL: Make a 90 s timer", "DONE WHEN: timer.html shows 90",
                 "STEP JUST FINISHED: 2. Change to 90", "ITS SUCCESS CRITERION: timer reads 90",
                 "IT REPORTED: met", "edited timer.html", "step 1: Create timer -> ok"):
        assert part in p, part


def test_the_block_names_its_source_and_is_empty_without_notes():
    assert PS.critic_block([]) == "" and PS.critic_block([{"step": 1, "note": ""}]) == ""
    b = PS.critic_block([{"step": 2, "title": "Change to 90", "note": "  - MISSING: x"}])
    assert "REVIEWER NOTES" in b and "evidence, not orders" in b and "after step 2" in b


def _wire(monkeypatch, delay=0.0, fail=False, answer="MISSING: the delete button"):
    seen = {"calls": [], "in_flight": 0, "max": 0, "events": []}

    async def fake_gen(prompt, **kw):
        seen["calls"].append({"prompt": prompt, "role": kw.get("role"), "gpu": kw.get("prefer_gpu"),
                              "profile": kw.get("profile")})
        seen["in_flight"] += 1
        seen["max"] = max(seen["max"], seen["in_flight"])
        try:
            await asyncio.sleep(delay)
            if fail:
                raise RuntimeError("cpu-247 down")
            return answer
        finally:
            seen["in_flight"] -= 1

    async def fake_emit(ev):
        seen["events"].append(ev)

    monkeypatch.setattr(M, "_safe_ollama_generate_dw", fake_gen)
    monkeypatch.setattr(M, "emit_event", fake_emit)
    return seen


def _step(i):
    return {"id": i, "title": "step %d" % i, "success": "s%d" % i}


@needs_app
def test_a_finished_step_is_reviewed_on_the_cpu_route_and_delivered_once(monkeypatch):
    seen = _wire(monkeypatch)

    async def go():
        c = M._V6StepCritic("build a todo app", sid="s", stream_id="")
        c.submit(_step(1), {"id": 1, "summary": "wrote index.html", "ok": True}, [], "add+delete work")
        await asyncio.sleep(0.05)
        first, again = c.new_block(), c.new_block()
        c.cancel()
        return c, first, again

    c, first, again = asyncio.run(go())
    (call,) = seen["calls"]
    assert call["role"] == PS.ENRICH_ROLE and call["profile"] == "planning_style" and call["gpu"] is False
    assert "delete button" in first and again == ""                 # once to the controller
    assert "delete button" in c.recent_block()                      # still there for the gate
    (ev,) = [e for e in seen["events"] if e["type"] == "agent_loop_v6.step_critique"]
    assert ev["step"] == 1 and ev["ok"] is False and "delete" in ev["note"]


@needs_app
def test_the_review_can_be_routed_to_the_gpu(monkeypatch):
    """User 2026-09-28: allow routing the review to the GPU. It then uses the
    GPU planning route and the GPU's own model (the CPU brief model does not
    apply); an unknown route falls back to the CPU."""
    seen = _wire(monkeypatch)

    async def go(route):
        c = M._V6StepCritic("g", sid="s", stream_id="", model="qwen3.6:35b-a3b", route=route)
        c.submit(_step(1), {"id": 1, "summary": "r1"}, [], "")
        await asyncio.sleep(0.05)
        c.cancel()
        return c

    gpu = asyncio.run(go("gpu"))
    assert gpu.route == "gpu" and gpu.notes[0]["route"] == "gpu"
    call = seen["calls"][-1]
    assert call["role"] == PS.PLAN_ROLE and call["gpu"] is True
    assert asyncio.run(go("sideways")).route == "cpu"
    assert seen["calls"][-1]["role"] == PS.ENRICH_ROLE and seen["calls"][-1]["gpu"] is False
    assert inspect.signature(M.cap_dag_agent_loop_v6).parameters["critic_route"].default == "cpu"


@needs_app
def test_one_review_at_a_time_and_only_the_latest_when_steps_outpace_it(monkeypatch):
    seen = _wire(monkeypatch, delay=0.05)

    async def go():
        c = M._V6StepCritic("g", sid="s", stream_id="")
        c.submit(_step(1), {"id": 1, "summary": "r1"}, [], "")
        await asyncio.sleep(0.01)                     # step 1 under review
        for i in (2, 3, 4):                           # three more finish meanwhile
            c.submit(_step(i), {"id": i, "summary": "r%d" % i}, [], "")
        await asyncio.sleep(0.2)
        c.cancel()
        return c

    c = asyncio.run(go())
    assert seen["max"] == 1
    assert [n["step"] for n in c.notes] == [1, 4]                   # 2 and 3 were stale


@needs_app
def test_a_failed_review_changes_nothing(monkeypatch):
    seen = _wire(monkeypatch, fail=True)

    async def go():
        c = M._V6StepCritic("g", sid="s", stream_id="")
        c.submit(_step(1), {"id": 1, "summary": "r1"}, [], "")
        await asyncio.sleep(0.05)
        blk = c.new_block()
        c.cancel()
        return blk

    assert asyncio.run(go()) == ""
    (ev,) = [e for e in seen["events"] if e["type"] == "agent_loop_v6.step_critique"]
    assert "cpu-247 down" in ev["error"]


@needs_app
def test_the_loop_wires_the_critic_without_waiting_for_it():
    src = inspect.getsource(M.cap_dag_agent_loop_v6)
    assert 'if (_plan_styles is not None and _pstyle.get("step_critic")) else None' in src
    assert "_critic.submit(step, res, results, done_when)" in src
    assert "style_note=_ctrl_style_note + (_critic.new_block() if _critic is not None else \"\")" in src
    assert "_gate_goal = _gate_goal + _critic.recent_block()" in src
    assert "_critic.cancel()" in src
    # the ONLY wait: the final gate, bounded, for the last step's critique
    assert src.count("await _critic.") == 1 and "_cw = await _critic.wait_for_last()" in src
    assert src.index("_cw = await _critic.wait_for_last()") < src.index("_gate_goal = _gate_goal + _critic.recent_block()")
    assert "_critic._runner" not in src
    assert "agent_loop_v6.step_critique" in inspect.getsource(M)            # forwarded to the UI


@needs_app
def test_the_final_gate_waits_for_the_last_steps_critique(monkeypatch):
    """User 2026-09-28: the first live run's critique landed ~30 s after the
    gate passed a misread goal. The gate now waits (bounded) for it."""
    _wire(monkeypatch, delay=0.1)

    async def go():
        c = M._V6StepCritic("g", sid="s", stream_id="")
        nothing = await c.wait_for_last(timeout=1)             # nothing submitted: no wait
        c.submit(_step(1), {"id": 1, "summary": "r1"}, [], "")
        got = await c.wait_for_last(timeout=5)
        c.cancel()
        return nothing, got, c

    nothing, got, c = asyncio.run(go())
    assert nothing["landed"] and nothing["waited_s"] < 0.05
    assert got["landed"] and got["step"] == 1 and 0.05 <= got["waited_s"] < 2
    assert "delete button" in c.recent_block()


@needs_app
def test_the_gate_wait_is_bounded(monkeypatch):
    _wire(monkeypatch, delay=5.0)

    async def go():
        c = M._V6StepCritic("g", sid="s", stream_id="")
        c.submit(_step(1), {"id": 1, "summary": "r1"}, [], "")
        got = await c.wait_for_last(timeout=0.2)
        c.cancel()
        return got

    got = asyncio.run(go())
    assert got["landed"] is False and got["waited_s"] < 1.0
