import pytest

from Vera.vera.integrations.external_effects import plan_external_effect


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
