import pytest

import Vera.vera.integrations.integrations_capabilities as integrations
from Vera.vera import capability_orchestration as orchestration


pytestmark = pytest.mark.critical


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
    monkeypatch.setattr(integrations.httpx, "AsyncClient", Client)

    result = await integrations.cap_api_call(
        id="service-1", method="POST", path="/orders?private=secret",
        body={"value": 1}, idempotency_key="order:42",
        approval_receipt_ref="approval:42", retry=True)

    assert result["ok"] is True
    assert result["effect_shadow"]["decision"]["would_execute"] is True
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
    monkeypatch.setattr(integrations.httpx, "AsyncClient", Client)
    result = await integrations.cap_api_call(
        id="service-1", method="POST", path="/legacy-write")
    assert result["status"] == 204
    assert result["effect_shadow"]["decision"]["would_admit"] is False
    assert result["effect_shadow"]["blocks_current_call"] is False
