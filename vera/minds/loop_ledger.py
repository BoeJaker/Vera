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
                 sink: Optional[Callable[[Dict[str, Any]], Any]] = None):
        self.sid = str(sid or "")
        self.origin = origin
        self._digest = digest
        self._sink = sink                      # tests pass a sink; the loop uses the ledger
        self.decisions: List[Dict[str, Any]] = []
        self.sent = 0
        self.on = enabled()

    def _send(self, row: Dict[str, Any]) -> None:
        if not self.on or row is None:
            return
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

    def finish(self, gate: Optional[Dict[str, Any]]) -> int:
        """Send the outcomes this run can judge. Returns how many."""
        try:
            outs = LD.run_outcomes(self.decisions, gate or {})
        except Exception as e:
            log.debug("loop ledger outcomes: %s", e)
            return 0
        for o in outs:
            self._send(o)
        return len(outs)
