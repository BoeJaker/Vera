from types import SimpleNamespace

import pytest

import Vera.vera.integrations.integrations_capabilities as integrations
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.capabilities import cap_tracking


pytestmark = pytest.mark.critical


def test_enforcement_decision_redacts_operator_and_approval_references():
    cap = orchestration.CAPABILITY_REGISTRY["integration.effect.enforcement.decide"]
    assert set(cap["redact_args"]) == {"actor_ref", "approval_receipt_ref"}


@pytest.mark.asyncio
async def test_enforcement_decision_fails_closed_when_evidence_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        integrations, "default_external_effect_shadow_evidence",
        lambda: (_ for _ in ()).throw(OSError("private storage detail")))
    result = await integrations.integration_effect_enforcement_decide(
        decision="continue_observing", expected_revision=0, actor_ref="operator:alice")
    assert result == {"error": "evidence_unavailable", "code": "evidence_unavailable",
                      "effective_mode": "observe_only", "enforcement_enabled": False}


@pytest.mark.asyncio
async def test_enforcement_readiness_fails_closed_when_evidence_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        integrations, "default_external_effect_shadow_evidence",
        lambda: (_ for _ in ()).throw(OSError("private storage detail")))
    result = await integrations.integration_effect_enforcement_readiness()
    assert result["eligible_for_operator_review"] is False
    assert result["enforcement_enabled"] is False
    assert result["error"] == "evidence_unavailable"
    assert "private storage detail" not in str(result)


def test_api_call_declares_payload_redaction_for_all_activity_channels():
    cap = orchestration.CAPABILITY_REGISTRY["integration.api.call"]
    assert cap["redact_result"] is True
    assert set(cap["redact_args"]) == {
        "path", "query", "body", "headers", "idempotency_key",
        "approval_receipt_ref",
    }

    secret_path = "/orders?customer=private"
    kw = {"id": "service-1", "path": secret_path,
          "body": {"private": "payload"}, "method": "POST"}
    preview = orchestration._args_preview(kw, redact=cap["redact_args"])
    compact = orchestration._args_compact(kw, redact=cap["redact_args"])
    stored = orchestration._act_safe_params(kw, redact=cap["redact_args"])

    assert secret_path not in preview
    assert compact["path"] == "***"
    assert stored["path"] == "[redacted]"
    assert stored["body"] == "[redacted]"


def test_tracking_gate_forwards_redaction_metadata_to_activity_enqueue(monkeypatch):
    """Exercise the installed tracking wrapper, not just either side in isolation."""
    forwarded = {}

    def enqueue(*args, **kwargs):
        forwarded.update({"args": args, "kwargs": kwargs})

    orch = SimpleNamespace(
        _act_enqueue=enqueue,
        _ACT_QUEUE=object(),
        _ACT_SESSION_CURSOR={},
        _cap_tracking_installed=False,
    )
    monkeypatch.setattr(cap_tracking, "is_tracked", lambda *_args: True)
    monkeypatch.setattr(cap_tracking, "_INSTALLED_ORCH", None)

    cap_tracking.install(orch)
    orch._act_enqueue(
        "integration.api.call", "integrations", "session-1", "trace-1",
        {"path": "/private"}, {"token": "private"}, 12.5,
        redact_args=frozenset({"path"}), redact_result=True,
    )

    assert forwarded["kwargs"]["redact_args"] == frozenset({"path"})
    assert forwarded["kwargs"]["redact_result"] is True


@pytest.mark.asyncio
async def test_api_call_observes_policy_without_forwarding_or_enforcing(monkeypatch):
    async def get_record(_id):
        return {"id": _id, "label": "Test", "kind": "generic",
                "base_url": "https://service.test", "scheme": "https",
                "verify_tls": True, "access": {"api": True}, "api": {}}

    audit = {}

    async def capture_audit(event, record, **extra):
        audit.update({"event": event, **extra})

    class Ledger:
        def replay_status(self, plan):
            return {"already_succeeded": False, "successful_receipt_id": ""}

    recorded = {}

    class ShadowEvidence:
        def record(self, value):
            recorded.update(value)

    class Response:
        status_code = 200
        text = '{"ok":true}'

        @staticmethod
        def json():
            return {"ok": True}

    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return Response()

    monkeypatch.setattr(integrations, "_get", get_record)
    monkeypatch.setattr(integrations, "_audit", capture_audit)
    monkeypatch.setattr(integrations, "default_external_effect_receipt_ledger",
                        lambda: Ledger())
    monkeypatch.setattr(integrations, "default_external_effect_shadow_evidence",
                        lambda: ShadowEvidence())
    monkeypatch.setattr(integrations.httpx, "AsyncClient", Client)

    result = await integrations.cap_api_call(
        id="service-1", method="POST", path="/orders?private=secret",
        body={"value": 1}, idempotency_key="order:42",
        approval_receipt_ref="approval:42", retry=True)

    assert result["ok"] is True
    assert result["effect_shadow"]["decision"]["would_execute"] is True
    assert recorded["decision"]["would_execute"] is True
    assert captured["url"].endswith("/orders?private=secret")
    assert "idempotency_key" not in captured
    assert "approval_receipt_ref" not in captured
    assert audit["event"] == "api_call"
    assert "path" not in audit
    assert "private=secret" not in str(audit)


@pytest.mark.asyncio
async def test_denied_shadow_does_not_block_current_compatibility_call(monkeypatch):
    async def get_record(_id):
        return {"id": _id, "label": "Test", "kind": "generic",
                "base_url": "https://service.test", "scheme": "https",
                "verify_tls": True, "access": {"api": True}, "api": {}}

    async def no_audit(*_args, **_kwargs):
        return None

    class Response:
        status_code = 204
        text = ""

        @staticmethod
        def json():
            raise ValueError("no body")

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(integrations, "_get", get_record)
    monkeypatch.setattr(integrations, "_audit", no_audit)
    class BrokenEvidence:
        def record(self, _value):
            raise OSError("evidence store unavailable")

    monkeypatch.setattr(integrations, "default_external_effect_shadow_evidence",
                        lambda: BrokenEvidence())
    monkeypatch.setattr(integrations.httpx, "AsyncClient", Client)
    result = await integrations.cap_api_call(
        id="service-1", method="POST", path="/legacy-write")
    assert result["status"] == 204
    assert result["effect_shadow"]["decision"]["would_admit"] is False
    assert result["effect_shadow"]["blocks_current_call"] is False
