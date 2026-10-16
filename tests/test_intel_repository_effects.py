import pytest

import Vera.vera.intel.intel_capabilities as intel
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.integrations.effect_observation_context import attributed
from Vera.vera.integrations.effect_shadow_evidence import (
    EVIDENCE_FAMILIES, ExternalEffectShadowEvidence)
from Vera.vera.intel.intel_repo_effects import (
    observe_repository_effect, plan_repository_effect)


pytestmark = pytest.mark.critical


@pytest.mark.parametrize("operation,method", [
    ("clone", "POST"), ("fetch", "PUT"), ("reset", "PUT")])
def test_repository_plan_is_payload_free_and_uses_operation_semantics(operation, method):
    result = plan_repository_effect(
        operation=operation, repository_ref="https://secret@github.com/private/repo.git",
        provider="github.com", idempotency_key="repo:1",
        approval_receipt_ref="approval:1", retry=True)
    assert result["plan"]["method"] == method
    assert result["enforcement"] == "observe_only"
    assert result["blocks_current_call"] is False
    assert result["repository"]["provider_idempotency_forwarded"] is False
    encoded = str(result)
    assert "https://secret@github.com/private/repo.git" not in encoded
    assert "private/repo.git" not in encoded
    assert "repo:1" not in encoded
    assert "approval:1" not in encoded


def test_clone_requires_idempotency_and_approval_while_update_steps_are_idempotent():
    clone = plan_repository_effect(
        operation="clone", repository_ref="repo", provider="github.com")
    fetch = plan_repository_effect(
        operation="fetch", repository_ref="repo", provider="github.com")
    assert set(clone["decision"]["reasons"]) == {
        "approval_receipt_required", "idempotency_key_required"}
    assert fetch["decision"]["reasons"] == ["approval_receipt_required"]


def test_repository_has_separate_source_attributable_evidence_family(monkeypatch, tmp_path):
    assert "repository" in EVIDENCE_FAMILIES
    evidence = ExternalEffectShadowEvidence(tmp_path / "repository.sqlite3")
    monkeypatch.setattr(
        "Vera.vera.intel.intel_repo_effects.default_external_effect_shadow_evidence",
        lambda family="": evidence)
    with attributed("intel"):
        observe_repository_effect(
            operation="clone", repository_ref="private/repo", provider="github.com")
    summary = evidence.summary(source="intel")
    assert summary["totals"]["observations"] == 1
    assert summary["recent"][0]["source"] == "intel"
    assert "private/repo" not in str(summary)


def test_repo_capability_redacts_repository_identity_and_controls():
    cap = orchestration.CAPABILITY_REGISTRY["intel.repo.get"]
    assert set(cap["redact_args"]) == {
        "url", "topic_id", "idempotency_key", "approval_receipt_ref"}
    assert cap["redact_result"] is True


class _Process:
    returncode = 0

    async def communicate(self):
        return b"", b""


@pytest.mark.asyncio
async def test_clone_observes_immediately_before_git_and_never_forwards_controls(monkeypatch, tmp_path):
    order = []
    observations = []

    def observe(**kwargs):
        order.append("observe:" + kwargs["operation"])
        observations.append(kwargs)
        return {"enforcement": "observe_only"}

    async def subprocess(*args, **kwargs):
        order.append("git:" + args[1])
        assert "repo:1" not in str(args) + str(kwargs)
        assert "approval:1" not in str(args) + str(kwargs)
        (tmp_path / "github.com" / "public" / "example").mkdir(parents=True)
        return _Process()

    monkeypatch.setenv("VERA_INTEL_REPO_DIR", str(tmp_path))
    monkeypatch.setattr(intel.intel_repo_effects, "observe_repository_effect", observe)
    monkeypatch.setattr(intel.asyncio, "create_subprocess_exec", subprocess)
    result = await intel.cap_repo_get.__wrapped__(
        url="https://github.com/public/example", idempotency_key="repo:1",
        approval_receipt_ref="approval:1", retry=True)
    assert order == ["observe:clone", "git:clone"]
    assert observations[0]["provider"] == "github.com"
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_existing_repo_observes_fetch_then_reset_in_provider_order(monkeypatch, tmp_path):
    dest = tmp_path / "github.com" / "public" / "example"
    (dest / ".git").mkdir(parents=True)
    order = []

    def observe(**kwargs):
        order.append("observe:" + kwargs["operation"])
        return {"enforcement": "observe_only"}

    async def subprocess(*args, **_kwargs):
        order.append("git:" + args[3])
        return _Process()

    monkeypatch.setenv("VERA_INTEL_REPO_DIR", str(tmp_path))
    monkeypatch.setattr(intel.intel_repo_effects, "observe_repository_effect", observe)
    monkeypatch.setattr(intel.asyncio, "create_subprocess_exec", subprocess)
    result = await intel.cap_repo_get.__wrapped__(url="https://github.com/public/example")
    assert result["ok"] is True
    assert order == ["observe:fetch", "git:fetch", "observe:reset", "git:reset"]
