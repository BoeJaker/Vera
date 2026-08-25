import asyncio

import pytest

from vera import capability_orchestration as orchestration
from vera.capability_policy_core import evaluate_policy_shadow


pytestmark = pytest.mark.critical


def test_legacy_contract_is_indeterminate_and_never_authorized():
    result = evaluate_policy_shadow("legacy.cap", None)
    assert result["verdict"] == "indeterminate"
    assert result["authorized"] is False and result["executed"] is False
    assert [reason["code"] for reason in result["reasons"]] == [
        "contract_unknown", "effects_unknown"]


def test_read_only_contract_is_allowed_by_the_shadow_baseline():
    result = evaluate_policy_shadow("records.read", {
        "effects": ["read"],
        "approval": {"status": "not_required"},
        "tenant": {"status": "global_read_only"},
        "secrets": {"status": "not_required"},
    })
    assert result["verdict"] == "allow"
    assert result["authorized"] is False
    assert result["reasons"] == []


def test_sensitive_effect_requires_explicit_grant_and_known_posture():
    result = evaluate_policy_shadow("records.write", {"effects": ["write"]})
    assert result["verdict"] == "deny"
    assert {reason["code"] for reason in result["reasons"]} == {
        "effect_grant_missing", "approval_posture_unknown", "tenant_posture_unknown"}


def test_required_approval_and_session_scope_fail_closed_then_preview_allow():
    contract = {
        "effects": ["write"],
        "approval": {"status": "required"},
        "tenant": {"status": "session_scoped"},
        "secrets": {"status": "not_required"},
    }
    denied = evaluate_policy_shadow("records.write", contract, {
        "allowed_effects": ["write"]})
    assert denied["verdict"] == "deny"
    assert {reason["code"] for reason in denied["reasons"]} == {
        "approval_missing", "session_scope_missing"}

    preview = evaluate_policy_shadow("records.write", contract, {
        "allowed_effects": ["write"], "approval_present": True,
        "session_id": "session-a"})
    assert preview["verdict"] == "allow"
    assert preview["authorized"] is False and preview["executed"] is False


def test_secret_effect_requires_contract_and_confirmed_opaque_references():
    contract = {
        "effects": ["secrets"],
        "approval": {"status": "not_required"},
        "tenant": {"status": "session_scoped"},
        "secrets": {"status": "opaque_references"},
    }
    denied = evaluate_policy_shadow("secret.use", contract, {
        "allowed_effects": ["secrets"], "session_id": "s"})
    assert denied["verdict"] == "deny"
    assert denied["reasons"] == [{"code": "opaque_secret_refs_unconfirmed"}]
    preview = evaluate_policy_shadow("secret.use", contract, {
        "allowed_effects": ["secrets"], "session_id": "s",
        "opaque_secret_refs": True})
    assert preview["verdict"] == "allow" and preview["authorized"] is False


def test_central_wrapper_attaches_content_free_shadow_policy_without_blocking(monkeypatch):
    events = []

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(orchestration, "emit_event", emit)
    name = "test.policy.shadow.wrapper"
    try:
        @orchestration.capability(name, memory="off", contract={
            "effects": ["write"],
            "approval": {"status": "required"},
            "tenant": {"status": "session_scoped"},
            "secrets": {"status": "not_required"},
        })
        async def sample(value: str, trace_id=None):
            return {"value": value}

        result = asyncio.run(sample(value="payload-must-not-enter-policy", trace_id="t"))
        assert result == {"value": "payload-must-not-enter-policy"}
        call = next(event for event in events if event["type"] == "cap.call")
        assert call["policy"]["verdict"] == "deny"
        assert call["policy"]["authorized"] is False
        assert "payload-must-not-enter-policy" not in repr(call["policy"])
    finally:
        orchestration.CAPABILITY_REGISTRY.pop(name, None)


def test_policy_inspection_capability_never_treats_simulation_as_authority():
    result = asyncio.run(orchestration.cap_policy_shadow.__wrapped__(
        name="llm.generate",
        allowed_effects=["filesystem", "model", "network"],
        session_id="session-a",
        approval_present=True,
    ))
    assert result["name"] == "llm.generate"
    assert result["authorized"] is False and result["executed"] is False

    missing = asyncio.run(orchestration.cap_policy_shadow.__wrapped__(
        name="does.not.exist"))
    assert missing == {"error": "capability_unknown", "name": "does.not.exist",
                       "authorized": False, "executed": False}
