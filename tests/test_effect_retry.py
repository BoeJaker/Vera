import pytest

from vera.integrations.effect_retry import describe_retry_policy, plan_effect_retry
from vera.integrations.external_effects import plan_external_effect


pytestmark = pytest.mark.critical


def effect(method="POST", retry=True, approval="approval:1", key="effect:1"):
    return plan_external_effect(
        connection_id="integration:demo", operation="orders.create",
        method=method, idempotency_key=key,
        approval_receipt_ref=approval, retry=retry)


def test_retry_policy_description_is_bounded_and_non_executing():
    policy = describe_retry_policy()
    assert policy["retryable_http_statuses"] == [408, 425, 429, 500, 502, 503, 504]
    assert policy["bounds"] == {"maximum_attempts": 20,
                                "maximum_delay_ms": 86_400_000}
    assert set(policy["reason_descriptions"]) == {
        "effect_not_admitted", "successful_receipt_exists",
        "retry_not_requested", "attempt_budget_exhausted",
        "outcome_not_retryable"}
    assert all(policy[key] is False for key in (
        "executes", "sleeps", "records_receipt", "resolves_secrets",
        "retains_payload"))


def test_retryable_mutation_produces_bounded_window_without_executing():
    result = plan_effect_retry(
        effect(), attempts_completed=1, status_code=429,
        retry_after_ms=4_000, rate_limit=100, rate_remaining=0,
        rate_reset_after_ms=3_000)
    assert result["schedule"] == {
        "allowed": True, "reasons": [], "earliest_delay_ms": 4_000,
        "latest_delay_ms": 4_000, "selection": "caller_jitter_within_window"}
    assert result["outcome"]["retryable"] is True
    assert result["attempts"]["remaining"] == 2
    assert all(result[key] is False for key in (
        "executes", "sleeps", "records_receipt", "resolves_secrets",
        "retains_payload"))


@pytest.mark.parametrize("change,reason", [
    ({"plan": effect(retry=False)}, "retry_not_requested"),
    ({"plan": effect(approval="")}, "effect_not_admitted"),
    ({"attempts_completed": 3}, "attempt_budget_exhausted"),
    ({"status_code": 400}, "outcome_not_retryable"),
    ({"successful_receipt": True}, "successful_receipt_exists"),
])
def test_retry_refusal_has_stable_reason(change, reason):
    args = {"plan": effect(), "attempts_completed": 1, "status_code": 503}
    args.update(change)
    result = plan_effect_retry(**args)
    assert result["schedule"]["allowed"] is False
    assert reason in result["schedule"]["reasons"]
    assert result["schedule"]["earliest_delay_ms"] == 0


def test_connection_failure_uses_exponential_jitter_ceiling():
    result = plan_effect_retry(
        effect(method="GET", approval="", key=""), attempts_completed=2,
        error_code="connection_error", base_delay_ms=250)
    assert result["schedule"]["earliest_delay_ms"] == 0
    assert result["schedule"]["latest_delay_ms"] == 1_000


def test_server_minimum_is_not_shortened_by_local_backoff_cap():
    result = plan_effect_retry(
        effect(), attempts_completed=1, status_code=503,
        retry_after_ms=60_000, backoff_cap_ms=1_000)
    assert result["schedule"]["earliest_delay_ms"] == 60_000
    assert result["schedule"]["latest_delay_ms"] == 60_000


@pytest.mark.parametrize("change", [
    {"attempts_completed": 0}, {"attempts_completed": -1},
    {"max_attempts": 0}, {"status_code": 600},
    {"retry_after_ms": -1}, {"rate_remaining": 1},
    {"rate_limit": 1, "rate_remaining": 2},
    {"status_code": 503, "error_code": "timeout"},
    {"error_code": "provider-private-message"},
])
def test_invalid_or_unbounded_evidence_is_rejected(change):
    args = {"plan": effect(), "attempts_completed": 1, "status_code": 503}
    args.update(change)
    with pytest.raises(ValueError):
        plan_effect_retry(**args)


def test_retry_plan_identity_changes_with_scheduling_evidence_only():
    first = plan_effect_retry(effect(), attempts_completed=1, status_code=503)
    same = plan_effect_retry(effect(), attempts_completed=1, status_code=503)
    later = plan_effect_retry(effect(), attempts_completed=2, status_code=503)
    assert first["retry_plan_id"] == same["retry_plan_id"]
    assert first["retry_plan_id"] != later["retry_plan_id"]
    assert "approval:1" not in str(first)
    assert "effect:1" not in str(first)
