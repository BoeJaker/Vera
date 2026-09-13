import asyncio
import json

import pytest

import Vera.vera.email.email_capabilities as mail
import Vera.vera.integrations.integrations_capabilities as integrations
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.email.email_effects import apply_replay_evidence, plan_email_send_effect


pytestmark = pytest.mark.critical


def test_email_plan_is_payload_free_and_destination_specific():
    result = plan_email_send_effect(
        account_ref="account:private", destination_ref="person@example.test",
        mode="send", idempotency_key="message:42",
        approval_receipt_ref="approval:42", retry=True)
    assert result["plan"]["admission"]["allowed"] is True
    assert result["delivery"]["provider_idempotency_forwarded"] is False
    assert result["enforcement"] == "observe_only"
    encoded = str(result)
    for private in ("account:private", "person@example.test", "message:42", "approval:42"):
        assert private not in encoded


def test_email_plan_requires_evidence_without_blocking_legacy_delivery():
    result = plan_email_send_effect(
        account_ref="default", destination_ref="thread:private", mode="reply")
    assert result["decision"]["would_admit"] is False
    assert set(result["decision"]["reasons"]) == {
        "approval_receipt_required", "idempotency_key_required"}
    assert result["blocks_current_call"] is False


def test_email_replay_evidence_does_not_mutate_original():
    original = plan_email_send_effect(
        destination_ref="person@example.test", mode="event",
        idempotency_key="event:1", approval_receipt_ref="approval:1")
    result = apply_replay_evidence(
        original, {"already_succeeded": True, "successful_receipt_id": "a" * 64})
    assert result["decision"]["would_execute"] is False
    assert result["replay"]["would_suppress"] is True
    assert original["decision"]["would_execute"] is True


def test_mail_send_capabilities_redact_content_and_policy_references():
    expected = {
        "mail.send": {"to", "subject", "body", "account", "cc", "bcc",
                      "idempotency_key", "approval_receipt_ref"},
        "mail.reply": {"uid", "body", "account", "idempotency_key",
                       "approval_receipt_ref"},
    }
    for name, redacted in expected.items():
        cap = orchestration.CAPABILITY_REGISTRY[name]
        assert set(cap["redact_args"]) == redacted
        assert cap["redact_result"] is True


def test_email_observations_use_family_isolated_ledger(monkeypatch):
    recorded = {}

    class Receipts:
        def replay_status(self, _plan):
            return {"already_succeeded": False, "successful_receipt_id": ""}

    class Evidence:
        def record(self, shadow):
            recorded["shadow"] = shadow

    def evidence(*, family):
        recorded["family"] = family
        return Evidence()

    monkeypatch.setattr(mail, "default_external_effect_receipt_ledger",
                        lambda: Receipts())
    monkeypatch.setattr(mail, "default_external_effect_shadow_evidence", evidence)
    result = mail._observe_email_effect(
        account_ref="account:1", destination_ref="person@example.test", mode="send",
        idempotency_key="message:1", approval_receipt_ref="approval:1")
    assert recorded["family"] == "email"
    assert recorded["shadow"]["plan"]["plan_id"] == result["plan"]["plan_id"]


@pytest.mark.asyncio
async def test_evidence_inspection_selects_one_family(monkeypatch):
    selected = {}

    class Evidence:
        def summary(self, *, limit):
            return {"totals": {}, "window": {"requested": limit, "returned": 0}}

    def evidence(*, family):
        selected["family"] = family
        return Evidence()

    monkeypatch.setattr(integrations, "default_external_effect_shadow_evidence", evidence)
    result = await integrations.integration_effect_shadow_evidence.__wrapped__(
        family="email", limit=12)
    assert selected["family"] == "email"
    assert result["family"] == "email"


@pytest.mark.asyncio
async def test_send_observes_before_transport_and_does_not_forward_controls(monkeypatch):
    order = []
    observed = {}
    delivered = {}

    def observe(**kwargs):
        order.append("observe")
        observed.update(kwargs)
        return {"enforcement": "observe_only", "decision": {"would_execute": True}}

    class Transport:
        async def send(self, to, subject, body, **kwargs):
            order.append("smtp")
            delivered.update({"to": to, "subject": subject, "body": body, **kwargs})
            return {"ok": True, "message_id": "provider:1"}

    async def transport(_account=""):
        order.append("transport")
        return Transport(), {"email": "sender@example.test"}

    async def settings():
        return {"signature": ""}

    async def emit(_event):
        return None

    monkeypatch.setattr(mail, "_observe_email_effect", observe)
    monkeypatch.setattr(mail, "_transport", transport)
    monkeypatch.setattr(mail, "_get_settings", settings)
    monkeypatch.setattr(mail, "emit_event", emit)
    result = await mail.cap_send.__wrapped__(
        to="person@example.test", subject="private", body="private body",
        account="account:1", idempotency_key="message:1",
        approval_receipt_ref="approval:1", retry=True)
    assert order == ["observe", "transport", "smtp"]
    assert observed["idempotency_key"] == "message:1"
    assert observed["approval_receipt_ref"] == "approval:1"
    assert "idempotency_key" not in delivered
    assert "approval_receipt_ref" not in delivered
    assert result["effect_shadow"]["enforcement"] == "observe_only"


@pytest.mark.asyncio
async def test_reply_plans_against_thread_before_opening_transport(monkeypatch):
    order = []

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["destination_ref"] == "thread:private-uid"
        return {"enforcement": "observe_only"}

    class Transport:
        async def get_message(self, _uid):
            order.append("read")
            return {"from": "Person <person@example.test>", "subject": "Hi",
                    "message_id": "provider:original", "references": ""}

        async def send(self, *_args, **_kwargs):
            order.append("smtp")
            return {"ok": True}

    async def transport(_account=""):
        order.append("transport")
        return Transport(), {}

    monkeypatch.setattr(mail, "_observe_email_effect", observe)
    monkeypatch.setattr(mail, "_transport", transport)
    monkeypatch.setattr(mail, "_get_settings", lambda: _async_value({"signature": ""}))
    monkeypatch.setattr(mail, "emit_event", lambda _event: _async_value(None))
    result = await mail.cap_reply.__wrapped__(
        uid="private-uid", body="private body", idempotency_key="reply:1",
        approval_receipt_ref="approval:1")
    assert order == ["observe", "transport", "read", "smtp"]
    assert result["effect_shadow"]["enforcement"] == "observe_only"


@pytest.mark.asyncio
async def test_event_bridge_observes_before_default_transport(monkeypatch):
    order = []

    class PubSub:
        async def subscribe(self, _channel):
            return None

        async def listen(self):
            yield {"type": "message", "data": json.dumps(
                {"type": "dag.completed", "message": "private"})}
            raise asyncio.CancelledError

    class Redis:
        def pubsub(self):
            return PubSub()

    class Transport:
        async def send(self, *_args, **_kwargs):
            order.append("smtp")
            return {"ok": True}

    def observe(**kwargs):
        order.append("observe")
        assert kwargs["mode"] == "event"
        return {"enforcement": "observe_only"}

    async def transport():
        order.append("transport")
        return Transport(), {}

    monkeypatch.setattr(mail, "_redis", lambda: Redis())
    monkeypatch.setattr(mail, "_get_events_cfg", lambda: _async_value(
        {"enabled": True, "to_addr": "private@example.test", "types": ["dag.completed"]}))
    monkeypatch.setattr(mail, "_observe_email_effect", observe)
    monkeypatch.setattr(mail, "_transport", transport)
    await mail._event_bridge_loop()
    assert order == ["observe", "transport", "smtp"]


async def _async_value(value):
    return value
