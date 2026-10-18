"""Who may run a capability tagged ``money`` (pure, no I/O).

A ``money`` capability moves real money: today the real broker caps
(``markets.broker.arm/set_trading/limits.set/order``). It runs only when a
person asks for it directly, through its own HTTP route (the Markets studio
panel's buttons), as the outermost capability and outside any background
driver. Everything else is refused, whatever name it calls:

- loops, agent turns, dreams, DAG steps (they run inside another capability);
- schedules, triggers and widget reads (no route of their own);
- MCP bridges and ``/ws/mcp`` (not the capability's route);
- ``/mcp/call``, even from the browser: the chat's cap-in-chat and the panel
  copilots execute model-chosen calls through it, so it does not prove that a
  person chose this call.

This is the ``money`` row of the B0.11 invocation policy, ahead of the rest of
it; an approval receipt can widen it when B0.11 lands. Known gap until B0.3b
(owner session): a server-side HTTP client calling Vera's own route over
loopback looks like the panel. The arm code (out of band), expiry and limits
still apply to it.

The facts come from the dispatcher's context variables, read in ``wrap()``
before the capability runs:
  running_caps  RUNNING_CAPS: the capabilities this call runs inside
  http_cap      CURRENT_HTTP_CAP: the capability whose own route is serving
  mcp_call      MCP_CALL_ACTIVE: the call came through /mcp/call
  caller_kind   CALLER_KIND: what the /mcp/call body said it was
  background    BACKGROUND_LLM: the background driver's label, if any
"""
from __future__ import annotations

from typing import Sequence, Tuple

TAG = "money"
# Tags this guard applies, with what the refusal says the caller cannot do. "direct" is for capabilities that
# must only run when the owner asks in person, without moving money (issuing and revoking caller tokens).
TAGS = {"money": "move money", "direct": "do this"}


def allowed(*, name: str, running_caps: Sequence[str] = (), http_cap: str = "",
            mcp_call: bool = False, caller_kind: str = "", background: str = "",
            what: str = "move money", where: str = "the Markets panel") -> Tuple[bool, str]:
    """(True, "") when a person asked for ``name`` directly, else (False, reason)."""
    if running_caps:
        return False, (f"called from inside {running_caps[-1]}: this capability runs "
                       "only when a person asks for it directly")
    if background:
        return False, f"background work ({background}) cannot {what}"
    if caller_kind:
        return False, f"caller {caller_kind!r} cannot {what}; the owner does it from {where}"
    if mcp_call:
        return False, ("not through /mcp/call (chat and copilot tool calls use it): "
                       f"use {where}")
    if http_cap == name:
        return True, ""
    return False, ("not a direct request from the UI: schedules, triggers, widgets, loops "
                   f"and agents cannot {what}")
