"""
comms_inbox.py — pending outbound questions awaiting a reply from a comms channel
=================================================================================

The routing twin of :mod:`vera.delivery` for the RETURN path. ``delivery`` sends
a finished report OUT to a channel (telegram / email / chat). This module tracks
a question we sent out that is still WAITING for the human's reply to come back
IN — so an agentic loop can ask a clarifying / steering question over Telegram
(or any comms channel) and block on the answer, exactly as it would over the
in-UI HITL channel.

Why a standalone helper (mirrors delivery.py):
  * Nothing here imports the orchestrator or any capability module, so it is safe
    to import from BOTH the loop engine (dag_workshop_capabilities) and the
    inbound side (telegram_capabilities) without circular-import risk.
  * The loop engine REGISTERS a pending question keyed by the channel address it
    was sent to (e.g. a Telegram chat_id), together with the loop's HITL
    coordinates (session_id + step). The inbound channel, on the next human
    message from that address, POPs the pending entry and resolves the loop's
    waiting future through the loop engine's own HITL resolver — so there is ONE
    wait path (the existing ``_await_hitl_decision``) for both UI and comms.

Everything is in-process (the loop engine and the comms poller run in the same
FastAPI app). Entries carry a TTL so an abandoned question is garbage-collected
rather than swallowing an unrelated later message.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# address (str, e.g. a telegram chat_id) -> pending question record:
#   {session_id, step, question, channel, kind, created, expires}
_PENDING_BY_ADDRESS: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _sweep() -> None:
    """Drop expired entries (lazy GC on every access)."""
    now = _now()
    for addr in [a for a, r in _PENDING_BY_ADDRESS.items()
                 if r.get("expires", 0) and r["expires"] < now]:
        _PENDING_BY_ADDRESS.pop(addr, None)


def register(address: str, *, session_id: str = "", step: int = 0,
             question: str = "", channel: str = "telegram", kind: str = "clarify",
             ttl_secs: float = 86400.0, meta: Optional[Dict[str, Any]] = None) -> None:
    """Record that we sent `question` to `address` and are awaiting a reply.

    Two consumers share this store, distinguished by `kind`:
      • kind="clarify" (default) — a loop HITL question; the inbound channel
        resolves the loop's future via (session_id, step).
      • kind="schedule" — a long-term-scheduler user notification; the inbound
        channel routes the reply to the scheduler via meta["action_id"].

    `meta` carries channel-agnostic routing data (e.g. the scheduler action id).
    A later reply from `address` is matched by :func:`take`. Re-registering the
    same address overwrites (the newest question wins)."""
    addr = str(address or "").strip()
    # A clarify entry needs a session to resolve; a schedule entry needs meta.
    if not addr or (kind != "schedule" and not session_id):
        return
    _sweep()
    _PENDING_BY_ADDRESS[addr] = {
        "session_id": str(session_id or ""),
        "step":       int(step),
        "question":   str(question or "")[:2000],
        "channel":    channel or "telegram",
        "kind":       kind or "clarify",
        "meta":       dict(meta or {}),
        "created":    _now(),
        "expires":    _now() + max(30.0, float(ttl_secs or 0)),
    }


def take(address: str) -> Optional[Dict[str, Any]]:
    """POP and return the pending question for `address` (a reply just arrived),
    or None if there is no live pending question for it."""
    _sweep()
    return _PENDING_BY_ADDRESS.pop(str(address or "").strip(), None)


def peek(address: str) -> Optional[Dict[str, Any]]:
    """Return (without removing) the pending question for `address`, or None."""
    _sweep()
    return _PENDING_BY_ADDRESS.get(str(address or "").strip())


def clear_session(session_id: str) -> int:
    """Remove any pending questions belonging to `session_id` (e.g. the loop
    got its answer via the UI channel instead, or was cancelled). Returns the
    number removed."""
    sid = str(session_id or "")
    if not sid:
        return 0
    victims = [a for a, r in _PENDING_BY_ADDRESS.items()
               if r.get("session_id") == sid]
    for a in victims:
        _PENDING_BY_ADDRESS.pop(a, None)
    return len(victims)


def has_pending(session_id: str) -> bool:
    sid = str(session_id or "")
    _sweep()
    return any(r.get("session_id") == sid for r in _PENDING_BY_ADDRESS.values())


def list_pending() -> List[Dict[str, Any]]:
    """Snapshot of all live pending questions (for status/debug UIs)."""
    _sweep()
    return [{"address": a, **r} for a, r in _PENDING_BY_ADDRESS.items()]
