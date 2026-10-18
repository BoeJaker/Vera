"""The money rule (pure): only a capability's own HTTP route, at the top level and
outside background work, may run a money capability."""
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vera import money_guard_core as mg  # noqa: E402

N = "markets.broker.order"


def test_own_route_at_top_level_is_allowed():
    assert mg.allowed(name=N, http_cap=N) == (True, "")


@pytest.mark.parametrize("kw, words", [
    ({}, "not a direct request"),                                            # schedule, trigger, widget
    ({"http_cap": "markets.studio.panel"}, "not a direct request"),          # another route
    ({"http_cap": N, "running_caps": ("dag.agent_loop_v8",)}, "dag.agent_loop_v8"),
    ({"http_cap": N, "running_caps": ("chat.stream", "tool.run")}, "tool.run"),
    ({"http_cap": N, "background": "dream:c1"}, "background"),
    ({"mcp_call": True}, "/mcp/call"),                                       # chat cap-in-chat, copilots
    ({"mcp_call": True, "caller_kind": "claude"}, "'claude'"),
    ({"mcp_call": True, "caller_kind": "codex"}, "'codex'"),
])
def test_everything_else_is_refused(kw, words):
    ok, why = mg.allowed(name=N, **kw)
    assert not ok and words in why
