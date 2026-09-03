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

try:
    from Vera.vera.operator import operator_budget as _budget
except Exception:                                     # pragma: no cover
    try:
        from vera.operator import operator_budget as _budget
    except Exception:
        _budget = None

# The signature default is evaluated at import, so it cannot reach through a
# failed import. 480 mirrors operator_budget.DEFAULT_MAX_SECONDS.
_OP_MAX_SECONDS = getattr(_budget, "DEFAULT_MAX_SECONDS", 480)

try:
    from Vera.vera.operator import stop_explanation as _stop_explanation
except Exception:                                     # pragma: no cover
    try:
        from vera.operator import stop_explanation as _stop_explanation
    except Exception:
        _stop_explanation = None

import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import actions as _actions
from . import perception as _perception
from . import safety as _safety
from . import operator_progress as _progress
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


def _make_default_think(call_cap, provider: str, model: str,
                        think: Optional[bool] = None) -> Callable:
    """`think=False` turns OFF a reasoning model's <think> pass.

    Measured in census run 18: each operator.think spent 35-60s generating
    560-946 tokens to decide ONE browser action, and every one of the fifteen
    steps was phase="act" - the reasoning was not buying observation. Off by
    default (None = the model's own default) since this trades decision quality
    for speed and that is the caller's call, not ours.
    """
    async def _think(goal, observation, history, canvas):
        return await _thinker.decide(goal, observation, history, call_cap,
                                     provider=provider, model=model, canvas=canvas,
                                     think=think)
    return _think


async def _default_act(session, action: str, args: Dict[str, Any]):
    return await _actions.perform(session, action, args)


# How many times the identical action may repeat on the identical page before
# the run stops. Deliberately generous: a browser legitimately repeats itself a
# few times (retrying a slow click, dismissing a re-appearing banner). Five in a
# row with nothing changing is not that. Env-tunable so a real counter-example
# can be accommodated without a code change.
_REPEAT_LIMIT = max(2, int(os.getenv("VERA_OPERATOR_REPEAT_LIMIT", "5") or 5))


def _repeat_signature(action: str, args: Optional[Dict[str, Any]], url: str) -> str:
    """Identity of an attempt, for the repeat guard.

    Includes the ARGS — the element and text, not just the verb — because
    "click" repeated says nothing on its own: eleven clicks on eleven different
    elements is progress. And includes the URL, so navigating between repeats
    (pagination, a wizard) never accumulates.
    """
    try:
        a = json.dumps(args or {}, sort_keys=True, default=str)[:400]
    except Exception:
        a = str(args)[:400]
    return "%s|%s|%s" % (str(action or ""), a, str(url or ""))


#: How much of a page's text to remember per step. Long enough to carry a
#: clock, a countdown, a total or a validation message; short enough that eight
#: of them do not crowd out the current page.
SEEN_CHARS = 120


def _observed_text(obs) -> str:
    """A one-line digest of what the page showed, for the history line."""
    text = " ".join(str(getattr(obs, "text", "") or "").split())
    if not text:
        title = str(getattr(obs, "title", "") or "").strip()
        return title[:SEEN_CHARS]
    return text[:SEEN_CHARS]


async def run_loop(goal: str, session, *,
                   call_cap: Optional[Callable[..., Awaitable[Any]]] = None,
                   policy: Optional[_safety.SafetyPolicy] = None,
                   provider: str = "ollama", model: str = "",
                   max_steps: int = 15, canvas: bool = False,
                   max_seconds: float = _OP_MAX_SECONDS,
                   progress_tolerance: int = _progress.DEFAULT_TOLERANCE,
                   think: Optional[bool] = None,
                   shots_dir: str = "",
                   on_step: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
                   observe_fn: Optional[Callable] = None,
                   think_fn: Optional[Callable] = None,
                   act_fn: Optional[Callable] = None,
                   should_cancel: Optional[Callable[[], Awaitable[bool]]] = None
                   ) -> Dict[str, Any]:
    """Drive ``session`` toward ``goal``. Returns
    {ok, done, steps, reason, summary, screenshots, error}.

    ``should_cancel`` is polled once per step, BEFORE the step observes or
    thinks. Without it a cancelled operator run kept driving the browser and
    kept issuing LLM calls: observed 2026-08-30, a census run was cancelled and
    its operator generation ran on for 1211s against the shared GPU. The agentic
    loop has had a cooperative-cancel check for a long time; this is the same
    idea in the same place.

    It stops the run issuing ANY further work. It cannot abort the single
    generation already in flight — that ends on its own timeout — so the honest
    claim is "no new work after cancel", not "instantly free"."""
    if not (goal or "").strip():
        return {"error": "goal required", "steps": []}

    policy = policy or _safety.SafetyPolicy()
    observe = observe_fn or _make_default_observe(shots_dir)
    think_llm = think
    think = think_fn or _make_default_think(call_cap, provider, model, think_llm)
    act = act_fn or _default_act

    history: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    screenshots: List[str] = []
    consecutive_errors = 0
    # Repeat guard state: the last (action, args, url) and how many times running.
    # See the check below for why the URL is part of the key.
    last_sig: Optional[str] = None
    same_sig_count = 0
    # Progress state: what the PAGE looked like, not what was done to it. A run
    # doing real work may take as many turns as it needs; what is worth stopping
    # is one whose actions stop changing anything. See operator_progress.
    prog_state: Optional[Dict[str, Any]] = None
    reason = "max_steps"
    done = False
    summary = ""

    async def _emit(rec: Dict[str, Any]):
        if on_step:
            try:
                await on_step(rec)
            except Exception as e:
                log.debug("operator on_step callback failed: %s", e)

    _t_start = time.time()
    for i in range(1, max_steps + 1):
        # Checked BEFORE the step, so the budget is a promise about when this
        # RETURNS rather than a limit it may overshoot by a whole generation.
        # max_steps alone could not express this: 15 quick steps and 15 steps at
        # 90s each are the same count. Census 23 had a single call run 22 min.
        if _budget is not None and _budget.exhausted(_t_start, time.time(), max_seconds):
            reason = _budget.STOP_REASON
            rec = {"i": i, "phase": _budget.STOP_REASON,
                   "reason": _budget.describe(time.time() - _t_start, max_seconds, i - 1)}
            steps.append(rec)
            await _emit(rec)
            break
        # Checked BEFORE observing or thinking, so a cancel stops the run without
        # buying one more browser action and one more LLM call.
        if should_cancel is not None:
            try:
                if await should_cancel():
                    reason = "cancelled"
                    rec = {"i": i, "phase": "cancelled",
                           "reason": "run cancelled before step %d" % i}
                    steps.append(rec)
                    await _emit(rec)
                    break
            except Exception:
                # A cancel check that errors must not stop a healthy run; the
                # worst case is the old behaviour.
                pass

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

        # Judged BEFORE thinking: a page that has not moved in several acts is
        # not worth another 40-second decision. The observation reflects the
        # page AFTER the previous act, so consecutive identical observations
        # are consecutive acts that changed nothing.
        prog_state = _progress.update(
            prog_state,
            _progress.page_signature(
                url=getattr(obs, "url", ""), title=getattr(obs, "title", ""),
                refs=[getattr(e, "ref", "") for e in (getattr(obs, "elements", None) or [])],
                # The observation has always carried the page text; the
                # signature just never looked at it, so a countdown was invisible.
                text=getattr(obs, "text", "")),
            last_action=(history[-1].get("action") if history else None))
        if _progress.should_stop(prog_state, progress_tolerance):
            reason = _progress.STOP_REASON
            rec = {"i": i, "phase": "no_progress", "url": getattr(obs, "url", ""),
                   "reason": _progress.describe(
                       prog_state, progress_tolerance,
                       [str(h.get("action") or "") for h in history]),
                   "screenshot": getattr(obs, "screenshot_path", "")}
            steps.append(rec)
            await _emit(rec)
            break

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
        # The THOUGHT is kept because the run's own conclusion is the best
        # signal that it has finished - operator/completion reads it.
        history.append({"action": action, "args": args, "result": result,
                        "thought": thought,
                        # WHAT THE PAGE SHOWED. Without it a goal phrased as
                        # "observe X count down from 01:30 to 00:00" is
                        # impossible: the model gets one snapshot per turn and
                        # no record of the previous ones, so it cannot see the
                        # change it is being asked to confirm. Census 30 run
                        # 00f154eb0e watched 01:30 -> 00:54 -> 00:00 and then
                        # said "I need to start by clicking the Start button".
                        "seen": _observed_text(obs)})
        session.history.append(rec)
        await _emit(rec)

        if isinstance(result, dict) and result.get("error"):
            consecutive_errors += 1
            if consecutive_errors >= 4:
                reason = "too_many_errors"
                break
        else:
            consecutive_errors = 0

        # REPEAT GUARD. Census run 15: a verification step clicked Start ELEVEN
        # times, ran the timer out, then spent its remaining steps unable to tell
        # whether "Time's up!" was success or the residue of its own clicks. It
        # burned 437s — 29% of that goal's entire budget — and hit its ceiling.
        #
        # Keyed on action + args + URL, not on the action alone. "click" repeated
        # is not evidence of anything: eleven clicks on eleven different elements
        # is progress. The same element, same args, same page, over and over, is
        # not. Including the URL is what keeps legitimate repetition safe — a
        # paginating "Next" changes the page, so it never accumulates.
        sig = _repeat_signature(action, args, getattr(obs, "url", ""))
        same_sig_count = same_sig_count + 1 if sig == last_sig else 1
        last_sig = sig
        if same_sig_count >= _REPEAT_LIMIT:
            reason = "repeating_action"
            rec = {"i": i, "phase": "repeating",
                   "action": action, "args": args,
                   "reason": ("the same action was repeated %d times on the same "
                              "page with no change — stopping rather than spending "
                              "the rest of the budget on it" % same_sig_count)}
            steps.append(rec)
            await _emit(rec)
            break

    # A run that ended WITHOUT reaching its goal is not a success. Reporting
    # ok=True for reason="max_steps" told the caller nothing had gone wrong, so
    # the agentic loop simply called operator.run again: census
    # build-browser-verified (2026-08-29) made four calls, two of which spent
    # 9m16s and 7m45s hitting the step ceiling and still reporting ok, together
    # eating 86% of the step's wall budget. `reason` already carried the truth;
    # only `ok` disagreed with it.
    # The explanation the branches above already wrote lives in steps[-1]; the
    # caller only ever saw `reason`. Census run 21, author-then-edit step 3:
    # three operator.run calls on the SAME url, 735s in total, each reported to
    # the agentic loop as the single word "no_progress".
    # The url matters: for a static file the caller has a far cheaper option
    # than a browser, and the explanation is where it can be told so.
    _last_url = ""
    for _r in reversed(steps):
        if isinstance(_r, dict) and _r.get("url"):
            _last_url = str(_r["url"]); break
    _explanation = _stop_explanation.explain(reason, steps) if _stop_explanation else ""
    # Appended by the CALLER, not passed into explain(): this module is imported
    # as Vera.vera.operator.stop_explanation, which resolves to the DEPLOYED
    # checkout, so a changed signature here breaks against the copy already on
    # main until it catches up. getattr keeps a mixed-version estate working.
    _hint = getattr(_stop_explanation, "static_check_hint", None) if _stop_explanation else None
    if _explanation and _hint is not None:
        try:
            _explanation += _hint(_last_url)
        except Exception:                             # pragma: no cover
            pass
    out = {"ok": bool(done), "done": done, "reason": reason, "summary": summary,
           "steps": steps, "step_count": len(steps), "screenshots": screenshots}
    if _explanation:
        out["explanation"] = _explanation
        # `reason` stays the machine-readable code; `error` is what the agentic
        # loop surfaces to the executor that has to decide what to do next.
        out.setdefault("error", _explanation)
    return out
