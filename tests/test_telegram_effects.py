import pytest

import Vera.vera.telegram.telegram_capabilities as telegram
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.telegram.telegram_effects import (
    apply_replay_evidence, plan_telegram_send_effect)


pytestmark = pytest.mark.critical


def test_telegram_plan_is_recipient_specific_payload_free_and_observe_only():
    result = plan_telegram_send_effect(
        chat_id="private-chat-42", mode="plain", idempotency_key="message:42",
        approval_receipt_ref="approval:42", retry=True)
    assert result["plan"]["admission"]["allowed"] is True
    assert result["delivery"]["provider_idempotency_forwarded"] is False
    assert result["enforcement"] == "observe_only"
    assert result["blocks_current_call"] is False
    encoded = str(result)
    assert "private-chat-42" not in encoded
    assert "approval:42" not in encoded
    assert "message:42" not in encoded


def test_telegram_plan_requires_control_evidence_but_does_not_enforce():
    result = plan_telegram_send_effect(chat_id="chat-1", mode="notification")
    assert result["decision"]["would_admit"] is False
    assert set(result["decision"]["reasons"]) == {
        "approval_receipt_required", "idempotency_key_required"}
    assert result["blocks_current_call"] is False


def test_replay_evidence_suppresses_only_the_projected_decision():
    original = plan_telegram_send_effect(
        chat_id="chat-1", mode="markdown", idempotency_key="message:1",
        approval_receipt_ref="approval:1")
    result = apply_replay_evidence(
        original, {"already_succeeded": True, "successful_receipt_id": "a" * 64})
    assert result["decision"]["would_execute"] is False
    assert result["replay"]["would_suppress"] is True
    assert original["decision"]["would_execute"] is True


def test_telegram_send_capabilities_redact_payloads_and_policy_references():
    expected = {
        "tg.send": {"chat_id", "text", "idempotency_key", "approval_receipt_ref"},
        "tg.send_markdown": {"chat_id", "text", "idempotency_key", "approval_receipt_ref"},
        "tg.notify": {"text", "idempotency_key", "approval_receipt_ref"},
        "tg.broadcast": {"text", "idempotency_key", "approval_receipt_ref"},
    }
    for name, redacted in expected.items():
        cap = orchestration.CAPABILITY_REGISTRY[name]
        assert set(cap["redact_args"]) == redacted
        assert cap["redact_result"] is True


@pytest.mark.asyncio
async def test_direct_send_observes_without_forwarding_control_references(monkeypatch):
    observed = {}
    sent = {}

    def observe(chat_id, **kwargs):
        observed.update({"chat_id": chat_id, **kwargs})
        return {"decision": {"would_execute": True}, "enforcement": "observe_only"}

    async def send(chat_id, text, **kwargs):
        sent.update({"chat_id": chat_id, "text": text, **kwargs})
        return {"ok": True, "effect_shadow": observe(chat_id, mode=kwargs["effect_mode"],
                    idempotency_key=kwargs["idempotency_key"],
                    approval_receipt_ref=kwargs["approval_receipt_ref"],
                    retry=kwargs["retry"])}

    monkeypatch.setattr(telegram, "_observe_send_effect", observe)
    monkeypatch.setattr(telegram, "_send_message", send)
    result = await telegram.tg_send.__wrapped__(
        chat_id="chat-1", text="private body", idempotency_key="message:1",
        approval_receipt_ref="approval:1", retry=True)
    assert observed["idempotency_key"] == "message:1"
    assert observed["approval_receipt_ref"] == "approval:1"
    assert sent["chat_id"] == "chat-1"
    assert sent["text"] == "private body"
    assert sent["effect_mode"] == "plain"
    assert result["effect_shadow"]["enforcement"] == "observe_only"


@pytest.mark.asyncio
async def test_broadcast_records_each_allowed_recipient_and_returns_aggregate(monkeypatch):
    async def chats():
        return [{"chat_id": "one", "allowed": True},
                {"chat_id": "two", "allowed": False},
                {"chat_id": "three", "allowed": True}]

    observed = []
    sent = []

    def observe(chat_id, **_kwargs):
        observed.append(chat_id)
        return {"decision": {"would_execute": chat_id == "one"}}

    async def send(chat_id, _text, **_kwargs):
        sent.append(chat_id)
        return {"ok": True, "effect_shadow": observe(chat_id)}

    monkeypatch.setattr(telegram, "_list_chats", chats)
    monkeypatch.setattr(telegram, "_observe_send_effect", observe)
    monkeypatch.setattr(telegram, "_send_message", send)
    result = await telegram.tg_broadcast.__wrapped__(text="private body")
    assert observed == ["one", "three"]
    assert sent == ["one", "three"]
    assert result["effect_evidence"] == {
        "observed": 2, "would_execute": 1, "enforcement": "observe_only"}


@pytest.mark.asyncio
async def test_low_level_sender_observes_internal_sends_before_provider_call(monkeypatch):
    order = []
    provider = {}

    def observe(chat_id, **kwargs):
        order.append("observe")
        return {"decision": {"would_execute": True}, "delivery": kwargs}

    async def config():
        order.append("open_config")
        return {"token": "provider-secret", "max_reply_chars": 3800}

    async def provider_call(token, method, params):
        order.append("provider")
        provider.update({"token": token, "method": method, "params": params})
        return {"ok": True}

    monkeypatch.setattr(telegram, "_observe_send_effect", observe)
    monkeypatch.setattr(telegram, "_get_config", config)
    monkeypatch.setattr(telegram, "_tg_api", provider_call)
    result = await telegram._send_message(
        "private-chat", "private body", effect_mode="notification",
        idempotency_key="message:1", approval_receipt_ref="approval:1", retry=True)
    assert order == ["observe", "open_config", "provider"]
    assert provider["params"] == {
        "chat_id": "private-chat", "text": "private body",
        "disable_web_page_preview": True}
    assert "idempotency_key" not in str(provider["params"])
    assert "approval:1" not in str(provider["params"])
    assert result["effect_shadow"]["delivery"]["mode"] == "notification"


@pytest.mark.asyncio
async def test_shadow_failure_preserves_existing_delivery(monkeypatch):
    monkeypatch.setattr(
        telegram, "plan_telegram_send_effect",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("private planning detail")))
    monkeypatch.setattr(
        telegram, "default_external_effect_shadow_evidence",
        lambda: (_ for _ in ()).throw(OSError("private storage detail")))
    monkeypatch.setattr(
        telegram, "_get_config",
        lambda: _async_value({"token": "provider-secret", "max_reply_chars": 3800}))
    monkeypatch.setattr(
        telegram, "_tg_api",
        lambda *_args, **_kwargs: _async_value({"ok": True}))
    result = await telegram._send_message("chat-1", "body")
    assert result["ok"] is True
    assert result["effect_shadow"]["error"] == "shadow_unavailable"
    assert "private planning detail" not in str(result)
    assert "private storage detail" not in str(result)


async def _async_value(value):
    return value
