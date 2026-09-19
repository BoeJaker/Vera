"""Pure routing helpers for the research subsystem.

App-free on purpose: the escalation decision is the part that was silently
wrong for the whole life of the feature, so it needs to be testable without
importing researcher_api (which pulls httpx, the capability app and the rest
of the research stack with it).

Background. A research role rule may carry a length escalation:

    "verifier": {"job_type": "research_reader", "deny_gpu": True,
                 "escalate_chars": 12000,
                 "escalate": {"deny_gpu": False, "prefer_gpu": True}}

meaning "digest on a CPU node, but send a big digest to the GPU". The research
subsystem resolved its node in get_instance(), which runs BEFORE the prompt is
composed, so the router was always asked with prompt_chars=0 and the escalation
could never fire -- a 68-citation analyst digest stayed on a CPU node. The
decision below is applied again at generation time, where the real size is
known.
"""

from typing import Optional

__all__ = ["escalation_threshold", "should_escalate"]


def escalation_threshold(rule: Optional[dict]) -> int:
    """The prompt size at which `rule` switches to its escalated routing.

    Returns 0 when the rule has no usable length escalation -- no threshold, no
    escalate block, or a malformed one. 0 means no prompt size can ever change
    where this rule routes, so callers should not re-resolve at all.
    """
    if not isinstance(rule, dict):
        return 0
    escalate = rule.get("escalate")
    # An empty or non-dict escalate block has nothing to apply. The orchestrator
    # iterates it with .items(), so anything else is malformed config rather
    # than an escalation we should announce.
    if not isinstance(escalate, dict) or not escalate:
        return 0
    try:
        at = int(rule.get("escalate_chars") or 0)
    except (TypeError, ValueError):
        return 0
    return at if at > 0 else 0


def should_escalate(rule: Optional[dict], prompt_chars: int) -> bool:
    """True when a prompt of `prompt_chars` crosses `rule`'s escalation point.

    Deliberately mirrors the orchestrator's own threshold test in
    _merge_rule_over_base (`esc_at > 0 and prompt_chars >= esc_at and
    rule.get("escalate")`) -- inclusive at the boundary. This is the one place
    the research path decides to re-ask the router, so it must not disagree
    with the rule the router will then apply; test_research_route_core pins
    both halves of that agreement.
    """
    at = escalation_threshold(rule)
    if at <= 0:
        return False
    try:
        chars = int(prompt_chars or 0)
    except (TypeError, ValueError):
        return False
    return chars >= at
