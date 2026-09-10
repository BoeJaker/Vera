import pytest

from Vera.vera.integrations.external_effects import (
    plan_api_effect_shadow, plan_external_effect,
)


pytestmark = pytest.mark.critical


def test_read_plan_is_admissible_without_write_authority():
    plan = plan_external_effect(
        connection_id="integration:weather", operation="forecast.read", method="GET")
    assert plan["schema"] == "vera.external-effect-plan/v1"
    assert plan["classification"] == "read"
    assert plan["admission"] == {"allowed": True, "reasons": []}
    assert plan["executes"] is False


def test_non_idempotent_write_requires_key_and_approval_receipt():
    plan = plan_external_effect(
        connection_id="integration:mail", operation="message.send", method="POST")
    assert plan["admission"]["allowed"] is False
    assert plan["admission"]["reasons"] == [
        "approval_receipt_required", "idempotency_key_required"]


def test_authorised_write_echoes_only_digests_and_is_stable():
    kwargs = dict(connection_id="integration:mail", operation="message.send",
                  method="POST", idempotency_key="send:abc-123",
                  approval_receipt_ref="approval:receipt-456", retry=True)
    first = plan_external_effect(**kwargs)
    second = plan_external_effect(**kwargs)
    assert first == second
    assert first["admission"]["allowed"] is True
    assert first["retry"]["allowed"] is True
    rendered = str(first)
    assert kwargs["idempotency_key"] not in rendered
    assert kwargs["approval_receipt_ref"] not in rendered


@pytest.mark.parametrize("field,value", [
    ("connection_id", ""),
    ("connection_id", "contains a space"),
    ("operation", ""),
    ("method", "TRACE"),
    ("idempotency_key", "secret\nvalue"),
])
def test_invalid_or_unsafe_identifiers_fail_closed(field, value):
    kwargs = dict(connection_id="integration:test", operation="thing.write",
                  method="POST", idempotency_key="key", approval_receipt_ref="receipt")
    kwargs[field] = value
    with pytest.raises(ValueError):
        plan_external_effect(**kwargs)


def test_api_shadow_reports_legacy_write_gap_without_blocking():
    shadow = plan_api_effect_shadow(
        integration_id="service-1", method="POST", path="/orders?token=secret")
    assert shadow["enforcement"] == "observe_only"
    assert shadow["decision"]["would_admit"] is False
    assert shadow["blocks_current_call"] is False
    assert shadow["decision"]["reasons"] == [
        "approval_receipt_required", "idempotency_key_required"]
    assert "/orders" not in str(shadow)
    assert "token=secret" not in str(shadow)


def test_api_shadow_would_suppress_a_successful_replay():
    shadow = plan_api_effect_shadow(
        integration_id="service-1", method="POST", path="/orders",
        idempotency_key="order:42", approval_receipt_ref="approval:42",
        retry=True, prior_success=True)
    assert shadow["decision"]["would_admit"] is True
    assert shadow["decision"]["would_execute"] is False
    assert shadow["replay"]["would_suppress"] is True
    assert shadow["records_completion"] is False


def test_api_operation_identity_ignores_query_and_fragment():
    first = plan_api_effect_shadow(
        integration_id="service-1", method="GET", path="/items?page=1#top")
    second = plan_api_effect_shadow(
        integration_id="service-1", method="GET", path="/items?page=2#bottom")
    assert first["plan"]["plan_id"] == second["plan"]["plan_id"]
