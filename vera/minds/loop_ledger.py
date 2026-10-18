"""One run's decisions, sent to the decision ledger without the loop ever waiting on it
(four-minds FM-0.2a).

Every row goes out as a background task (one Redis XADD each, see ledger_capabilities.append),
so recording adds no await to the runner. A failure to record is logged and dropped; it never
touches the run. VERA_DECIDE_LEDGER_LOOP=0 turns recording off.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

try:
    from Vera.vera.minds import loop_decisions_core as LD
except Exception:                       # worktree / app-free import
    from vera.minds import loop_decisions_core as LD  # type: ignore
try:
    from Vera.vera.minds import judge_core as JC
except Exception:                       # worktree / app-free import
    from vera.minds import judge_core as JC  # type: ignore

log = logging.getLogger("vera.minds.loop_ledger")
_PENDING: set = set()     # strong refs: asyncio keeps only weak ones to bare tasks


def enabled() -> bool:
    return str(os.environ.get("VERA_DECIDE_LEDGER_LOOP", "1")).strip().lower() not in ("0", "false", "no", "off")


def _append():
    try:
        from Vera.vera.minds import ledger_capabilities as L
    except Exception:
        from vera.minds import ledger_capabilities as L  # type: ignore
    return L.append


class RunLedger:
    """Collects a run's decisions; send() fires each to the ledger in the background."""

    def __init__(self, sid: str, *, origin: str = "user", digest: Callable[[], str] = lambda: "default",
                 sink: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 marker: Optional[Callable[[str, bool], Any]] = None):
        self.sid = str(sid or "")
        self.origin = origin
        self._digest = digest
        self._sink = sink                      # tests pass a sink; the loop uses the ledger
        self.decisions: List[Dict[str, Any]] = []
        self.sent = 0
        self.on = enabled()
        self._opened = False
        self._marker = marker                  # tests pass one; the loop uses ledger_capabilities
        self.states: List[Any] = []            # (decision_id, inputs) kept, when a test sink is used

    def _bg(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)
        except Exception as e:
            log.debug("loop ledger task: %s", e)
            coro.close()

    def _mark(self, open_: bool) -> None:
        """Open/close this run for the orphan sweeper. Best effort: never raises."""
        if not self.sid:
            return
        try:
            if self._marker is not None:
                self._bg(self._marker(self.sid, open_))
                return
            if self._sink is not None:
                return
            try:
                from Vera.vera.minds import ledger_capabilities as L
            except Exception:
                from vera.minds import ledger_capabilities as L  # type: ignore
            mark = getattr(L, "mark_run", None)
            if mark is not None:
                self._bg(mark(self.sid, open_))
        except Exception as e:
            log.debug("loop ledger mark: %s", e)

    def _send(self, row: Dict[str, Any], inputs: Optional[Dict[str, Any]] = None) -> None:
        if not self.on or row is None:
            return
        if not self._opened:
            self._opened = True
            self._mark(True)
        try:
            if self._sink is not None:
                self._sink(row)
            else:
                task = asyncio.get_running_loop().create_task(self._safe(row))
                _PENDING.add(task)
                task.add_done_callback(_PENDING.discard)
            self.sent += 1
        except Exception as e:
            log.debug("loop ledger: %s", e)
            return
        if inputs:
            self._keep(row, inputs)

    def _keep(self, row: Dict[str, Any], inputs: Dict[str, Any]) -> None:
        """What the decision saw, to the training store (training_state); never raises or waits."""
        try:
            if self._sink is not None:
                self.states.append((row.get("decision_id"), inputs))
                return
            try:
                from Vera.vera.minds import training_state as TSR
            except Exception:
                from vera.minds import training_state as TSR  # type: ignore
            if row.get("question_id") in getattr(TSR, "FRAME_QUESTIONS", ()) and inputs.get("goal"):
                TSR.put_framed(row, inputs)       # + the goal's Frame (sense.frame), in the background
            else:
                TSR.put(row, inputs)
        except Exception as e:
            log.debug("loop ledger inputs: %s", e)

    @staticmethod
    async def _safe(row: Dict[str, Any]) -> None:
        try:
            res = await _append()(row)
            if not res.get("ok"):
                log.debug("loop ledger refused %s: %s", row.get("decision_id"), res.get("errors"))
        except Exception as e:
            log.debug("loop ledger append: %s", e)

    def _digest_now(self) -> str:
        try:
            return str(self._digest() or "default")
        except Exception:
            return "default"

    def controller(self, step_id: Any, action: str, ctrl: Dict[str, Any], model: str = "",
                   inputs: Optional[Dict[str, Any]] = None, by: str = "llm",
                   student: Optional[Dict[str, Any]] = None) -> None:
        try:
            d = LD.controller_decision(self.sid, step_id, action, ctrl or {}, model=model,
                                       minds_digest=self._digest_now(), origin=self.origin, by=by, student=student)
        except Exception as e:
            log.debug("loop ledger controller: %s", e)
            return
        self.decisions.append(d)
        self._send(d, inputs)
        self._student_shadow(d, by, student)

    def verify(self, step_id: Any, met: Any, attempt: int, model: str = "",
               inputs: Optional[Dict[str, Any]] = None, by: str = "llm",
               student: Optional[Dict[str, Any]] = None) -> None:
        """The verdict and who gave it. A student that answered the same evidence but did not decide (shadow, or
        student_first below its threshold) is recorded as a shadow of this decision."""
        try:
            d = LD.verify_decision(self.sid, step_id, met, attempt, model=model,
                                   minds_digest=self._digest_now(), origin=self.origin, by=by, student=student)
        except Exception as e:
            log.debug("loop ledger verify: %s", e)
            return
        if d is None:
            return
        self.decisions.append(d)
        self._send(d, inputs)
        self._student_shadow(d, by, student)

    def _student_shadow(self, d: Dict[str, Any], by: str, student: Optional[Dict[str, Any]]) -> None:
        """A student that answered the same input but did not decide, as a shadow of the live decision."""
        if not (student and student.get("answer")) or by == "student":
            return
        try:
            s = JC.shadow_decision(d, student["answer"], student.get("p"), student.get("impl") or "student",
                                   mass=float(student.get("mass") or 0.0), ms=int(student.get("ms") or 0),
                                   would_take=student.get("would_take"))
        except Exception as e:
            log.debug("loop ledger student shadow: %s", e)
            return
        self._send(s)

    def step_done(self, step_id: Any, done_by: str, attempt: int,
                  inputs: Optional[Dict[str, Any]] = None) -> None:
        try:
            d = LD.step_done_decision(self.sid, step_id, done_by, attempt,
                                      minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger step_done: %s", e)
            return
        if d is None:
            return
        self.decisions.append(d)
        self._send(d, inputs)

    def prestep(self, step_id: Any, gaps: List[Any], known_steps: int, model: str = "",
                inputs: Optional[Dict[str, Any]] = None) -> None:
        try:
            d = LD.prestep_decision(self.sid, step_id, gaps or [], known_steps, model=model,
                                    minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger prestep: %s", e)
            return
        self.decisions.append(d)
        self._send(d, inputs)

    def tier(self, tier: str, info: Dict[str, Any], model: str = "",
             inputs: Optional[Dict[str, Any]] = None, by: str = "",
             student: Optional[Dict[str, Any]] = None) -> None:
        """The tier, and a System 1 student's answer to the same goal as its shadow (compute-roles, 18 Oct
        2026: the loop.tier hook - kNN 0.979 vs live 0.873 on the first matrix night)."""
        n = len(self.decisions)
        self._decide(LD.tier_decision, tier, info or {}, model, inputs)
        if len(self.decisions) > n:
            self._student_shadow(self.decisions[-1], by or ("llm" if (info or {}).get("llm") else "rule"), student)

    def intent(self, intent: str, info: Dict[str, Any], model: str = "",
               inputs: Optional[Dict[str, Any]] = None) -> None:
        self._decide(LD.intent_decision, intent, info or {}, model, inputs)

    def intent_shadow(self, rec: Dict[str, Any], inputs: Optional[Dict[str, Any]] = None) -> None:
        """The zero-shot intent beside the live one (mode=shadow); never judged by run_outcomes."""
        live = next((x for x in reversed(self.decisions)
                     if x.get("question_id") == "loop.intent" and x.get("mode") != "shadow"), None)
        try:
            d = LD.intent_shadow_decision(self.sid, rec or {}, minds_digest=self._digest_now(), origin=self.origin,
                                          for_id=(live or {}).get("decision_id") or "")
        except Exception as e:
            log.debug("loop ledger intent shadow: %s", e)
            return
        if d is None:
            return
        self.decisions.append(d)
        self._send(d, inputs)

    def _decide(self, build, answer, info, model, inputs: Optional[Dict[str, Any]] = None) -> None:
        try:
            d = build(self.sid, answer, info, model=model, minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger %s: %s", getattr(build, "__name__", "decision"), e)
            return
        self.decisions.append(d)
        self._send(d, inputs)

    def finish(self, gate: Optional[Dict[str, Any]], steps_run: Optional[int] = None,
               fast_path: Optional[bool] = None, families: Optional[Dict[str, Any]] = None) -> int:
        """Send the outcomes this run can judge. Returns how many."""
        try:
            outs = LD.run_outcomes(self.decisions, gate or {}, steps_run=steps_run, fast_path=fast_path,
                                   families=families)
        except Exception as e:
            log.debug("loop ledger outcomes: %s", e)
            return 0
        for o in outs:
            self._send(o)
        if self._opened:
            self._mark(False)
        return len(outs)
