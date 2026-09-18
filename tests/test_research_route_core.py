"""The research role rule's length escalation must be reachable.

The bug this pins: the research subsystem resolved its Ollama node in
get_instance(), which runs BEFORE the prompt is composed, so the cluster router
was always asked with prompt_chars=0. The verifier role is deny_gpu with an
"escalate at 12000 chars to the GPU" rule, and that escalation therefore could
never fire -- observed live 2026-09-18, when a 68-citation analyst digest ran
on cpu-247 while the GPU sat free.

Two things are worth guarding, and they are different:
  * the threshold decision itself (pure, below), and
  * that it AGREES with the orchestrator rule it is standing in for -- the
    research path re-asks the router only when it believes the router will
    escalate, so a drift between the two conditions puts the feature straight
    back to silently doing nothing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.research.research_route_core import (  # noqa: E402
    escalation_threshold,
    should_escalate,
)

# The rule as researcher_api registers it for the verifier role.
VERIFIER_RULE = {
    "job_type": "research_reader",
    "deny_gpu": True,
    "escalate_chars": 12000,
    "escalate": {"deny_gpu": False, "prefer_gpu": True},
    "label": "Research - verifier",
}


# -- the threshold ---------------------------------------------------------

def test_threshold_is_the_rules_own_number():
    assert escalation_threshold(VERIFIER_RULE) == 12000


@pytest.mark.parametrize("rule", [
    None,
    {},
    "not a rule",
    {"job_type": "research_reader", "deny_gpu": True},        # no escalation
    {"escalate_chars": 12000},                                # threshold, nothing to apply
    {"escalate": {"prefer_gpu": True}},                       # block, no threshold
    {"escalate_chars": 0, "escalate": {"prefer_gpu": True}},  # disabled
    {"escalate_chars": -5, "escalate": {"prefer_gpu": True}},
    {"escalate_chars": "big", "escalate": {"prefer_gpu": True}},
    {"escalate_chars": 12000, "escalate": {}},                # empty block
    {"escalate_chars": 12000, "escalate": ["prefer_gpu"]},    # malformed block
])
def test_no_usable_escalation_means_no_threshold(rule):
    assert escalation_threshold(rule) == 0
    # ...and nothing can trigger it, however large the prompt.
    assert should_escalate(rule, 10 ** 9) is False


# -- the decision ----------------------------------------------------------

def test_the_regression_itself_prompt_chars_zero_never_escalates():
    """This is what the caller used to pass, and why the feature was dead."""
    assert should_escalate(VERIFIER_RULE, 0) is False


@pytest.mark.parametrize("chars,expected", [
    (0, False),
    (11999, False),
    (12000, True),      # inclusive at the boundary, like the orchestrator
    (12001, True),
    (250000, True),     # a 68-citation digest
])
def test_threshold_boundary(chars, expected):
    assert should_escalate(VERIFIER_RULE, chars) is expected


@pytest.mark.parametrize("chars", [None, "", "lots"])
def test_unusable_prompt_size_does_not_escalate(chars):
    """A size we cannot read is not evidence that the prompt is big."""
    assert should_escalate(VERIFIER_RULE, chars) is False


# -- agreement with the rule the router will actually apply ----------------

def test_agrees_with_the_orchestrators_own_merge():
    """should_escalate must predict _merge_rule_over_base exactly.

    Skips where the app module is not importable (the pure tier runs in places
    the orchestrator's dependencies are not installed); when it IS importable
    this is the test that stops the two conditions drifting apart.
    """
    try:
        from vera.capability_orchestration import _merge_rule_over_base
    except Exception as e:                                   # pragma: no cover
        pytest.skip(f"capability_orchestration not importable: {e}")

    base = {"job_type": "research_reader"}
    for chars in (0, 11999, 12000, 12001, 250000):
        merged = _merge_rule_over_base(VERIFIER_RULE, base, chars)
        # The escalation's observable effect: the CPU pin is lifted.
        router_escalated = (merged.get("deny_gpu") is False
                            and merged.get("prefer_gpu") is True)
        assert should_escalate(VERIFIER_RULE, chars) is router_escalated, (
            f"disagreement at {chars} chars: research path says "
            f"{should_escalate(VERIFIER_RULE, chars)}, router says "
            f"{router_escalated}")
