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

    def _send(self, row: Dict[str, Any]) -> None:
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

    def controller(self, step_id: Any, action: str, ctrl: Dict[str, Any], model: str = "") -> None:
        try:
            d = LD.controller_decision(self.sid, step_id, action, ctrl or {}, model=model,
                                       minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger controller: %s", e)
            return
        self.decisions.append(d)
        self._send(d)

    def verify(self, step_id: Any, met: Any, attempt: int, model: str = "") -> None:
        try:
            d = LD.verify_decision(self.sid, step_id, met, attempt, model=model,
                                   minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger verify: %s", e)
            return
        if d is None:
            return
        self.decisions.append(d)
        self._send(d)

    def step_done(self, step_id: Any, done_by: str, attempt: int) -> None:
        try:
            d = LD.step_done_decision(self.sid, step_id, done_by, attempt,
                                      minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger step_done: %s", e)
            return
        if d is None:
            return
        self.decisions.append(d)
        self._send(d)

    def prestep(self, step_id: Any, gaps: List[Any], known_steps: int, model: str = "") -> None:
        try:
            d = LD.prestep_decision(self.sid, step_id, gaps or [], known_steps, model=model,
                                    minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger prestep: %s", e)
            return
        self.decisions.append(d)
        self._send(d)

    def tier(self, tier: str, info: Dict[str, Any], model: str = "") -> None:
        self._decide(LD.tier_decision, tier, info or {}, model)

    def intent(self, intent: str, info: Dict[str, Any], model: str = "") -> None:
        self._decide(LD.intent_decision, intent, info or {}, model)

    def _decide(self, build, answer, info, model) -> None:
        try:
            d = build(self.sid, answer, info, model=model, minds_digest=self._digest_now(), origin=self.origin)
        except Exception as e:
            log.debug("loop ledger %s: %s", getattr(build, "__name__", "decision"), e)
            return
        self.decisions.append(d)
        self._send(d)

    def finish(self, gate: Optional[Dict[str, Any]], steps_run: Optional[int] = None,
               fast_path: Optional[bool] = None) -> int:
        """Send the outcomes this run can judge. Returns how many."""
        try:
            outs = LD.run_outcomes(self.decisions, gate or {}, steps_run=steps_run, fast_path=fast_path)
        except Exception as e:
            log.debug("loop ledger outcomes: %s", e)
            return 0
        for o in outs:
            self._send(o)
        if self._opened:
            self._mark(False)
        return len(outs)
