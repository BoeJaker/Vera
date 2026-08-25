import asyncio
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from vera import capability_orchestration as orchestration
from vera.approval_receipts import (
    RedisNonceReplayLedger, activate_trusted_policy_context,
    consume_approval_receipt_durable, issue_approval_receipt,
)
from vera.capability_enforcement import (
    PolicyEnforcementDenied, enforcement_projection, enforcement_status)
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


def test_wrapper_uses_only_dispatcher_context_not_similar_arguments(monkeypatch):
    events = []

    async def emit(event):
        events.append(event)

    class Redis:
        async def set(self, key, value, *, nx, ex):
            return True

    monkeypatch.setattr(orchestration, "emit_event", emit)
    name = "test.policy.trusted.context"
    contract = {"effects": ["write"], "approval": {"status": "required"},
                "tenant": {"status": "session_scoped"},
                "secrets": {"status": "not_required"}}
    try:
        @orchestration.capability(name, memory="off", contract=contract)
        async def sample(session_id: str = "", approval_present: bool = False,
                         allowed_effects=None, trace_id=None):
            return {"ok": True}

        asyncio.run(sample(session_id="session-a", approval_present=True,
                           allowed_effects=["write"]))
        untrusted = events[-2]["policy"]
        assert untrusted["verdict"] == "deny"
        assert untrusted["trusted_context"] == {"present": False}

        key = b"z" * 32
        issued_at = int(time.time())
        receipt = issue_approval_receipt(
            signing_key=key, capability=name, effects=["write"],
            session_id="session-a", now=issued_at,
            nonce="raw-nonce-must-not-appear")
        consumed = asyncio.run(consume_approval_receipt_durable(
            receipt, signing_key=key, capability=name, effects=["write"],
            session_id="session-a", now=issued_at + 1,
            replay_ledger=RedisNonceReplayLedger(Redis())))
        with activate_trusted_policy_context(consumed["context"]):
            asyncio.run(sample(session_id="session-a"))
        trusted = events[-2]["policy"]
        assert trusted["verdict"] == "allow"
        assert trusted["authorized"] is False and trusted["executed"] is False
        assert trusted["trusted_context"]["present"] is True
        assert "raw-nonce-must-not-appear" not in repr(trusted["trusted_context"])
    finally:
        orchestration.CAPABILITY_REGISTRY.pop(name, None)


def test_enforcement_projection_requires_both_flags_and_supported_family():
    denied = {"verdict": "deny"}
    assert enforcement_projection(
        "run.shadow.get", denied, mode_value="shadow",
        families_value="run.shadow")["blocked"] is False
    assert enforcement_projection(
        "run.shadow.get", denied, mode_value="enforce",
        families_value="")["blocked"] is False
    active = enforcement_projection(
        "run.shadow.get", denied, mode_value="enforce",
        families_value="run.shadow")
    assert active["selected"] is True and active["blocked"] is True
    unsupported = enforcement_projection(
        "llm.generate", denied, mode_value="enforce",
        families_value="llm")
    assert unsupported["blocked"] is False
    assert unsupported["config_valid"] is False

    status = enforcement_status(mode_value="enforce", families_value="run.shadow")
    assert status["enabled"] is True and status["config_valid"] is True
    assert status["selected_capabilities"] == [
        "run.shadow.export", "run.shadow.get", "run.shadow.graph", "run.shadow.list"]


def test_enforcement_status_capability_is_bounded(monkeypatch):
    monkeypatch.setenv("VERA_POLICY_MODE", "enforce")
    monkeypatch.setenv("VERA_POLICY_ENFORCE_FAMILIES", "run.shadow,unknown")
    result = asyncio.run(orchestration.cap_policy_enforcement_status.__wrapped__())
    assert result["enabled"] is True
    assert result["config_valid"] is False
    assert result["unsupported_families"] == ["unknown"]
    assert "VERA_POLICY_MODE=enforce" not in repr(result)


def test_run_shadow_enforcement_denies_before_execution_and_kill_switch_restores(monkeypatch):
    events = []
    executed = 0

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(orchestration, "emit_event", emit)
    monkeypatch.setenv("VERA_POLICY_MODE", "enforce")
    monkeypatch.setenv("VERA_POLICY_ENFORCE_FAMILIES", "run.shadow")
    name = "run.shadow.get"
    existing = orchestration.CAPABILITY_REGISTRY.get(name)
    try:
        @orchestration.capability(name, memory="off", contract={
            "effects": ["read", "filesystem"],
            "approval": {"status": "not_required"},
            "tenant": {"status": "process_local"},
            "secrets": {"status": "not_required"},
        })
        async def sample(run_id: str, session_id: str = "", trace_id=None):
            nonlocal executed
            executed += 1
            return {"run_id": run_id}

        with pytest.raises(PolicyEnforcementDenied):
            asyncio.run(sample(run_id="must-not-run", session_id="session-a"))
        assert executed == 0
        assert [event["type"] for event in events] == ["cap.call", "cap.denied"]
        assert events[-1]["policy"]["enforcement"]["blocked"] is True

        # Runtime kill switch: no redecorating or restart is required.
        monkeypatch.setenv("VERA_POLICY_MODE", "shadow")
        assert asyncio.run(sample(run_id="allowed", session_id="session-a")) == {
            "run_id": "allowed"}
        assert executed == 1
        assert events[-2]["policy"]["enforcement"]["would_block"] is True
        assert events[-2]["policy"]["enforcement"]["blocked"] is False
    finally:
        if existing is None:
            orchestration.CAPABILITY_REGISTRY.pop(name, None)
        else:
            orchestration.CAPABILITY_REGISTRY[name] = existing


def test_enforced_run_shadow_executes_with_exact_consumed_context(monkeypatch):
    events = []

    async def emit(event):
        events.append(event)

    class Redis:
        async def set(self, key, value, *, nx, ex):
            return True

    monkeypatch.setattr(orchestration, "emit_event", emit)
    monkeypatch.setenv("VERA_POLICY_MODE", "enforce")
    monkeypatch.setenv("VERA_POLICY_ENFORCE_FAMILIES", "run.shadow")
    name = "run.shadow.list"
    existing = orchestration.CAPABILITY_REGISTRY.get(name)
    try:
        @orchestration.capability(name, memory="off", contract={
            "effects": ["read", "filesystem"],
            "approval": {"status": "not_required"},
            "tenant": {"status": "process_local"},
            "secrets": {"status": "not_required"},
        })
        async def sample(session_id: str = "", trace_id=None):
            return {"ok": True}

        key = b"e" * 32
        issued_at = int(time.time())
        receipt = issue_approval_receipt(
            signing_key=key, capability=name, effects=["read", "filesystem"],
            session_id="session-a", now=issued_at, nonce="enforcement-context")
        consumed = asyncio.run(consume_approval_receipt_durable(
            receipt, signing_key=key, capability=name,
            effects=["read", "filesystem"], session_id="session-a",
            now=issued_at + 1, replay_ledger=RedisNonceReplayLedger(Redis())))
        with activate_trusted_policy_context(consumed["context"]):
            assert asyncio.run(sample(session_id="session-a")) == {"ok": True}
        policy = events[-2]["policy"]
        assert policy["verdict"] == "allow"
        assert policy["enforcement"]["selected"] is True
        assert policy["enforcement"]["blocked"] is False
        assert policy["authorized"] is False and policy["executed"] is False
    finally:
        if existing is None:
            orchestration.CAPABILITY_REGISTRY.pop(name, None)
        else:
            orchestration.CAPABILITY_REGISTRY[name] = existing


def test_mcp_transport_maps_policy_denial_to_403(monkeypatch):
    monkeypatch.setenv("VERA_POLICY_MODE", "enforce")
    monkeypatch.setenv("VERA_POLICY_ENFORCE_FAMILIES", "run.shadow")
    name = "run.shadow.export"
    existing = orchestration.CAPABILITY_REGISTRY.get(name)
    try:
        @orchestration.capability(name, memory="off", contract={
            "effects": ["read", "filesystem"],
            "approval": {"status": "not_required"},
            "tenant": {"status": "process_local"},
            "secrets": {"status": "not_required"},
        })
        async def sample(run_id: str, trace_id=None):
            raise AssertionError("denied call executed")

        payload = b'{"name":"run.shadow.export","arguments":{"run_id":"r"}}'
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/mcp/call",
                           "headers": []}, receive)
        with pytest.raises(HTTPException) as denied:
            asyncio.run(orchestration._make_mcp_call_handler()(request))
        assert denied.value.status_code == 403
        assert "run.shadow.export" in str(denied.value.detail)
        assert "run_id" not in str(denied.value.detail)
    finally:
        if existing is None:
            orchestration.CAPABILITY_REGISTRY.pop(name, None)
        else:
            orchestration.CAPABILITY_REGISTRY[name] = existing
