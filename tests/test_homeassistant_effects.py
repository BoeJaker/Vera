import pytest

import Vera.vera.homeassistant.ha_capabilities as ha
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.homeassistant.ha_effects import (
    observe_homeassistant_notification_effect,
    plan_homeassistant_notification_effect,
)
from Vera.vera.integrations.effect_observation_context import attributed
from Vera.vera.integrations.effect_shadow_evidence import ExternalEffectShadowEvidence
from Vera.vera.integrations.effect_shadow_evidence import EVIDENCE_FAMILIES


pytestmark = pytest.mark.critical


def test_notification_plan_is_target_specific_payload_free_and_observe_only():
    result = plan_homeassistant_notification_effect(
        target_ref="notify.private_phone", idempotency_key="message:42",
        approval_receipt_ref="approval:42", retry=True)
    assert result["plan"]["admission"]["allowed"] is True
    assert result["enforcement"] == "observe_only"
    assert result["blocks_current_call"] is False
    assert result["delivery"]["provider_idempotency_forwarded"] is False
    encoded = str(result)
    assert "private_phone" not in encoded
    assert "message:42" not in encoded
    assert "approval:42" not in encoded


def test_notification_plan_requires_control_evidence_but_never_enforces():
    result = plan_homeassistant_notification_effect(target_ref="notify.phone")
    assert result["decision"]["would_admit"] is False
    assert set(result["decision"]["reasons"]) == {
        "approval_receipt_required", "idempotency_key_required"}
    assert result["blocks_current_call"] is False
    assert result["executes"] is False


def test_homeassistant_is_a_separate_evidence_family():
    assert "homeassistant" in EVIDENCE_FAMILIES


def test_observer_records_once_with_source_attribution(monkeypatch):
    calls = []

    class Evidence:
        def record(self, shadow):
            calls.append(shadow)

    monkeypatch.setattr(
        "Vera.vera.homeassistant.ha_effects.default_external_effect_shadow_evidence",
        lambda family="": Evidence())
    result = observe_homeassistant_notification_effect(target_ref="notify.phone")
    assert len(calls) == 1
    assert calls[0] is result


def test_observer_retains_intel_attribution_without_payload(monkeypatch, tmp_path):
    evidence = ExternalEffectShadowEvidence(tmp_path / "ha.sqlite3")
    monkeypatch.setattr(
        "Vera.vera.homeassistant.ha_effects.default_external_effect_shadow_evidence",
        lambda family="": evidence)
    with attributed("intel"):
        observe_homeassistant_notification_effect(target_ref="notify.private_phone")
    result = evidence.summary(source="intel")
    assert result["totals"]["observations"] == 1
    assert result["recent"][0]["source"] == "intel"
    assert "private_phone" not in str(result)


def test_notify_capability_redacts_payload_and_control_references():
    cap = orchestration.CAPABILITY_REGISTRY["ha.notify"]
    assert set(cap["redact_args"]) == {
        "message", "title", "target", "data", "idempotency_key",
        "approval_receipt_ref"}
    assert cap["redact_result"] is True


@pytest.mark.asyncio
async def test_notify_observes_before_provider_and_does_not_forward_controls(monkeypatch):
    order = []
    observed = {}
    provider = {}

    def observe(**kwargs):
        order.append("observe")
        observed.update(kwargs)
        return {"enforcement": "observe_only", "decision": {"would_execute": True}}

    async def call(domain, service, payload):
        order.append("provider")
        provider.update({"domain": domain, "service": service, "payload": payload})
        return {"ok": True, "service": service}

    monkeypatch.setattr(ha, "observe_homeassistant_notification_effect", observe)
    monkeypatch.setattr(ha, "_do_call", call)
    result = await ha.cap_notify.__wrapped__(
        message="private body", title="private title", target="notify.private_phone",
        data={"private": "value"}, idempotency_key="message:1",
        approval_receipt_ref="approval:1", retry=True)
    assert order == ["observe", "provider"]
    assert observed == {
        "target_ref": "notify.private_phone", "idempotency_key": "message:1",
        "approval_receipt_ref": "approval:1", "retry": True}
    assert provider == {
        "domain": "notify", "service": "private_phone",
        "payload": {"message": "private body", "title": "private title",
                    "data": {"private": "value"}}}
    assert "idempotency_key" not in str(provider)
    assert "approval:1" not in str(provider)
    assert result["effect_shadow"]["enforcement"] == "observe_only"


@pytest.mark.asyncio
async def test_shadow_failure_never_blocks_existing_notification(monkeypatch):
    monkeypatch.setattr(
        ha, "observe_homeassistant_notification_effect",
        lambda **_kwargs: {"error": "shadow_unavailable", "enforcement": "observe_only"})

    async def call(_domain, _service, _payload):
        return {"ok": True}

    monkeypatch.setattr(ha, "_do_call", call)
    result = await ha.cap_notify.__wrapped__(message="body", target="notify.phone")
    assert result["ok"] is True
    assert result["effect_shadow"]["error"] == "shadow_unavailable"
