import pytest

import Vera.vera.n8n.n8n_capabilities as n8n
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.integrations.effect_observation_context import attributed
from Vera.vera.integrations.effect_shadow_evidence import (
    EVIDENCE_FAMILIES, ExternalEffectShadowEvidence)
from Vera.vera.n8n.n8n_effects import (
    observe_n8n_workflow_effect, plan_n8n_workflow_effect)


pytestmark = pytest.mark.critical


@pytest.mark.parametrize("operation,method", [
    ("create", "POST"), ("activate", "POST"), ("deactivate", "POST"),
    ("delete", "DELETE"),
])
def test_workflow_plan_uses_provider_method_and_retains_no_identity(operation, method):
    result = plan_n8n_workflow_effect(
        operation=operation, workflow_ref="private-workflow-42",
        idempotency_key="workflow:42", approval_receipt_ref="approval:42")
    assert result["plan"]["method"] == method
    assert result["enforcement"] == "observe_only"
    assert result["blocks_current_call"] is False
    assert result["workflow"]["provider_idempotency_forwarded"] is False
    encoded = str(result)
    assert "private-workflow-42" not in encoded
    assert "workflow:42" not in encoded
    assert "approval:42" not in encoded


def test_create_requires_idempotency_and_approval_but_delete_only_approval():
    create = plan_n8n_workflow_effect(operation="create", workflow_ref="wf")
    delete = plan_n8n_workflow_effect(operation="delete", workflow_ref="wf")
    assert set(create["decision"]["reasons"]) == {
        "approval_receipt_required", "idempotency_key_required"}
    assert delete["decision"]["reasons"] == ["approval_receipt_required"]
    assert create["executes"] is False
    assert delete["executes"] is False


def test_n8n_is_a_separate_evidence_family():
    assert "n8n" in EVIDENCE_FAMILIES


def test_observer_retains_intel_attribution_without_payload(monkeypatch, tmp_path):
    evidence = ExternalEffectShadowEvidence(tmp_path / "n8n.sqlite3")
    monkeypatch.setattr(
        "Vera.vera.n8n.n8n_effects.default_external_effect_shadow_evidence",
        lambda family="": evidence)
    with attributed("intel"):
        observe_n8n_workflow_effect(operation="create", workflow_ref="private-name")
    result = evidence.summary(source="intel")
    assert result["totals"]["observations"] == 1
    assert result["recent"][0]["source"] == "intel"
    assert "private-name" not in str(result)


def test_mutation_capabilities_redact_payload_identity_and_controls():
    expected = {
        "n8n.workflow.create": {"name", "nodes", "connections", "settings",
                                "idempotency_key", "approval_receipt_ref"},
        "n8n.workflow.activate": {"id", "idempotency_key", "approval_receipt_ref"},
        "n8n.workflow.delete": {"id", "idempotency_key", "approval_receipt_ref"},
    }
    for name, redacted in expected.items():
        cap = orchestration.CAPABILITY_REGISTRY[name]
        assert set(cap["redact_args"]) == redacted
        assert cap["redact_result"] is True


@pytest.mark.asyncio
async def test_create_observes_create_and_optional_activation_at_provider_boundaries(monkeypatch):
    order = []
    provider = []
    observed = []

    def observe(**kwargs):
        order.append("observe:" + kwargs["operation"])
        observed.append(kwargs)
        return {"workflow": {"operation": kwargs["operation"]},
                "enforcement": "observe_only"}

    async def api(method, path, **kwargs):
        order.append("provider:" + path)
        provider.append((method, path, kwargs))
        return {"id": "wf-1", "name": "private-name", "nodes": []}

    async def config(*_args, **_kwargs):
        return {"base_url": "https://private.invalid"}

    monkeypatch.setattr(n8n, "observe_n8n_workflow_effect", observe)
    monkeypatch.setattr(n8n, "_api", api)
    monkeypatch.setattr(n8n, "_load_config", config)
    result = await n8n.cap_workflow_create.__wrapped__(
        name="private-name", nodes=[{"private": "node"}], activate=True,
        idempotency_key="workflow:1", approval_receipt_ref="approval:1", retry=True)
    assert order == [
        "observe:create", "provider:workflows", "observe:activate",
        "provider:workflows/wf-1/activate"]
    assert [row["operation"] for row in observed] == ["create", "activate"]
    assert all(row["idempotency_key"] == "workflow:1" for row in observed)
    assert provider[0] == (
        "POST", "workflows",
        {"body": {"name": "private-name", "nodes": [{"private": "node"}],
                  "connections": {}, "settings": {"executionOrder": "v1"}}})
    assert provider[1] == ("POST", "workflows/wf-1/activate", {})
    assert "idempotency_key" not in str(provider)
    assert "approval:1" not in str(provider)
    assert [s["workflow"]["operation"] for s in result["effect_shadows"]] == [
        "create", "activate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("active,operation,path", [
    (True, "activate", "workflows/wf-1/activate"),
    (False, "deactivate", "workflows/wf-1/deactivate"),
])
async def test_activate_observes_before_exact_provider_call(
        monkeypatch, active, operation, path):
    order = []

    def observe(**kwargs):
        order.append(("observe", kwargs))
        return {"enforcement": "observe_only"}

    async def api(method, actual_path, **kwargs):
        order.append(("provider", method, actual_path, kwargs))
        return {}

    monkeypatch.setattr(n8n, "observe_n8n_workflow_effect", observe)
    monkeypatch.setattr(n8n, "_api", api)
    result = await n8n.cap_workflow_activate.__wrapped__(
        id="wf-1", active=active, idempotency_key="workflow:1",
        approval_receipt_ref="approval:1", retry=True)
    assert order[0][0] == "observe"
    assert order[0][1]["operation"] == operation
    assert order[1] == ("provider", "POST", path, {})
    assert result["effect_shadow"]["enforcement"] == "observe_only"


@pytest.mark.asyncio
async def test_delete_observes_before_delete_and_shadow_failure_never_blocks(monkeypatch):
    order = []
    monkeypatch.setattr(
        n8n, "observe_n8n_workflow_effect",
        lambda **_kwargs: (order.append("observe") or
                           {"error": "shadow_unavailable", "enforcement": "observe_only"}))

    async def api(method, path, **kwargs):
        order.append((method, path, kwargs))
        return {}

    monkeypatch.setattr(n8n, "_api", api)
    result = await n8n.cap_workflow_delete.__wrapped__(id="wf-1")
    assert order == ["observe", ("DELETE", "workflows/wf-1", {})]
    assert result["ok"] is True
    assert result["effect_shadow"]["error"] == "shadow_unavailable"
