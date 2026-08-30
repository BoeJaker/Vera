"""One instance runs the sweeps that mutate shared state.

`scheduler_loop` had no cross-instance gate. The only filter was
`is_dev_sandbox()` (leech boot), so EVERY non-sandbox orchestrator ran EVERY
ambient job on its own timer, against the one Redis every instance shares.

Measured on the host 2026-08-31: **seventeen** `capability_orchestration`
processes, fourteen of them holding no listening port at all and between five
and twelve days old. The restart helper does `fuser -k 8999/tcp`, which kills
whatever holds the PORT; anything that lost the port or never bound it survives
indefinitely and keeps running its schedulers. Together they held ~3.5GB RSS
and kept executing week-old code.

That is not a cosmetic waste. It is how a fixed bug came back: a `sandbox.prune`
at 22:58 emitted the pre-fix audit text minutes after the fixed build was
serving :8999 - a zombie ran the old destructive sweep and deleted the pool
descriptors that the running build had just been taught to protect. Reaping,
un-registering and force-removing are not idempotent when fifteen copies of
different vintages do them concurrently.

The fix is a lease, not a kill: jobs registered with ``singleton=True`` run only
in the instance currently holding a short Redis lease. Everything else keeps its
current behaviour, so per-instance housekeeping is untouched. A crashed leader
loses the lease when it expires and another instance takes over, which is why
the TTL is short and renewal is frequent.

Pure: no Redis, no clock. The caller supplies both and performs the I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: How long a claimed lease stays valid without renewal. Short enough that a
#: crashed leader is replaced promptly, long enough to survive a slow tick.
LEASE_TTL_S = 90.0

#: How often the holder re-asserts it. Comfortably inside the TTL so an ordinary
#: hiccup never costs leadership.
RENEW_EVERY_S = 30.0


def should_check_lease(*, now: float, last_checked: Optional[float],
                       renew_every: float = RENEW_EVERY_S) -> bool:
    """Whether to talk to Redis this tick.

    The scheduler ticks every second; asking Redis every second would add 86400
    round trips a day per instance to answer a question that changes rarely.
    """
    if last_checked is None:
        return True
    return (now - last_checked) >= renew_every


def lease_decision(*, now: float, me: str, holder: Optional[str],
                   expires_at: Optional[float]) -> Dict[str, Any]:
    """May *me* run singleton jobs, and should it write the lease?

    Returns ``{run, claim, reason}``. ``claim`` means "write the lease with a
    fresh expiry" - the caller does that, then trusts ``run``.
    """
    me = str(me or "")
    holder = str(holder or "") or None

    if holder is None:
        return {"run": True, "claim": True, "reason": "no leader; claiming"}
    if holder == me:
        return {"run": True, "claim": True, "reason": "renewing own lease"}
    if expires_at is None or now >= float(expires_at):
        return {"run": True, "claim": True,
                "reason": f"lease from {holder} expired; taking over"}
    return {"run": False, "claim": False,
            "reason": f"{holder} holds the lease until {expires_at:.0f}"}


def may_run(task: Dict[str, Any], *, is_leader: bool) -> bool:
    """Whether this task may run in this instance on this tick.

    A task is unaffected unless it opted in with ``singleton=True``: this must
    never quietly stop ordinary per-instance work from running.
    """
    if not task.get("singleton"):
        return True
    return bool(is_leader)


def describe(state: Dict[str, Any]) -> str:
    """One line for the log/status, so leadership is observable rather than
    inferred from which instance happened to do something."""
    if state.get("is_leader"):
        return "scheduler leadership: HELD by this instance"
    holder = state.get("holder") or "unknown"
    return f"scheduler leadership: held by {holder}; singleton jobs skipped here"
