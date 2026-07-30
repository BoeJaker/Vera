"""operator_loop.py — the dedicated observe→think→act driver (``operator.run``).

A bounded loop: each step *observes* (screenshot + a11y refs), *thinks* (LLM
picks one action), checks the *safety* policy, then *acts*. Every step is
recorded and (optionally) emitted so the Operator Studio timeline shows the run
live. Stops on ``done``, an unrecoverable think error, too many consecutive act
errors, a safety block, or ``max_steps``.

The three phase functions are injected (``observe_fn`` / ``think_fn`` /
``act_fn``) so the loop is unit-testable with mocks; the module also provides the
real defaults wired to :mod:`perception`, :mod:`thinker`, :mod:`actions` and
:mod:`safety`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import actions as _actions
from . import perception as _perception
from . import safety as _safety
from . import thinker as _thinker

log = logging.getLogger("vera.operator.loop")


def _make_default_observe(shots_dir: str) -> Callable:
    async def _observe(session, step_i: int):
        shot = ""
        if shots_dir:
            os.makedirs(shots_dir, exist_ok=True)
            shot = os.path.join(shots_dir, f"step-{step_i:02d}.png")
        obs = await _perception.observe_page(session.page, screenshot_path=shot)
        session.ref_map = obs.ref_map()
        session.meta["last_url"] = obs.url
        return obs
    return _observe


def _make_default_think(call_cap, provider: str, model: str) -> Callable:
    async def _think(goal, observation, history, canvas):
        return await _thinker.decide(goal, observation, history, call_cap,
                                     provider=provider, model=model, canvas=canvas)
    return _think


async def _default_act(session, action: str, args: Dict[str, Any]):
    return await _actions.perform(session, action, args)


async def run_loop(goal: str, session, *,
                   call_cap: Optional[Callable[..., Awaitable[Any]]] = None,
                   policy: Optional[_safety.SafetyPolicy] = None,
                   provider: str = "ollama", model: str = "",
                   max_steps: int = 15, canvas: bool = False,
                   shots_dir: str = "",
                   on_step: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
                   observe_fn: Optional[Callable] = None,
                   think_fn: Optional[Callable] = None,
                   act_fn: Optional[Callable] = None) -> Dict[str, Any]:
    """Drive ``session`` toward ``goal``. Returns
    {ok, done, steps, reason, summary, screenshots, error}."""
    if not (goal or "").strip():
        return {"error": "goal required", "steps": []}

    policy = policy or _safety.SafetyPolicy()
    observe = observe_fn or _make_default_observe(shots_dir)
    think = think_fn or _make_default_think(call_cap, provider, model)
    act = act_fn or _default_act

    history: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    screenshots: List[str] = []
    consecutive_errors = 0
    reason = "max_steps"
    done = False
    summary = ""

    async def _emit(rec: Dict[str, Any]):
        if on_step:
            try:
                await on_step(rec)
            except Exception as e:
                log.debug("operator on_step callback failed: %s", e)

    for i in range(1, max_steps + 1):
        t0 = time.time()
        try:
            obs = await observe(session, i)
        except Exception as e:
            reason = "observe_error"
            steps.append({"i": i, "phase": "observe", "error": str(e)})
            await _emit(steps[-1])
            break
        if getattr(obs, "screenshot_path", ""):
            screenshots.append(obs.screenshot_path)

        decision = await think(goal, obs, history, canvas)
        if isinstance(decision, dict) and decision.get("error"):
            reason = "think_error"
            steps.append({"i": i, "phase": "think", "url": getattr(obs, "url", ""),
                          "error": decision["error"],
                          "screenshot": getattr(obs, "screenshot_path", "")})
            await _emit(steps[-1])
            break

        action = decision.get("action", "")
        args = decision.get("args") or {}
        thought = decision.get("thought", "")

        if decision.get("done") or action == "done":
            done = True
            reason = "done"
            summary = args.get("summary") or thought or "goal complete"
            rec = {"i": i, "phase": "done", "thought": thought, "summary": summary,
                   "url": getattr(obs, "url", ""),
                   "screenshot": getattr(obs, "screenshot_path", "")}
            steps.append(rec)
            await _emit(rec)
            break

        if decision.get("invalid"):
            # illegal action — record and let the model try again next step
            rec = {"i": i, "phase": "invalid", "thought": thought, "action": action,
                   "args": args, "error": decision["invalid"],
                   "screenshot": getattr(obs, "screenshot_path", "")}
            steps.append(rec)
            history.append({"action": action, "args": args,
                            "result": {"error": decision["invalid"]}})
            await _emit(rec)
            consecutive_errors += 1
            if consecutive_errors >= 4:
                reason = "too_many_errors"
                break
            continue

        gate = _safety.evaluate(policy, getattr(obs, "url", ""), action, args)
        if not gate["allowed"]:
            reason = "blocked"
            rec = {"i": i, "phase": "blocked", "thought": thought, "action": action,
                   "args": args, "reason": gate["reason"],
                   "screenshot": getattr(obs, "screenshot_path", "")}
            steps.append(rec)
            await _emit(rec)
            break

        if gate["dry_run"]:
            result = {"ok": True, "dry_run": True, "note": gate["reason"]}
        else:
            result = await act(session, action, args)

        rec = {"i": i, "phase": "act", "thought": thought, "action": action,
               "args": {k: (v if k != "text" else str(v)[:60]) for k, v in args.items()},
               "result": result, "url": getattr(obs, "url", ""),
               "screenshot": getattr(obs, "screenshot_path", ""),
               "ms": int((time.time() - t0) * 1000)}
        steps.append(rec)
        history.append({"action": action, "args": args, "result": result})
        session.history.append(rec)
        await _emit(rec)

        if isinstance(result, dict) and result.get("error"):
            consecutive_errors += 1
            if consecutive_errors >= 4:
                reason = "too_many_errors"
                break
        else:
            consecutive_errors = 0

    return {"ok": True, "done": done, "reason": reason, "summary": summary,
            "steps": steps, "step_count": len(steps), "screenshots": screenshots}
