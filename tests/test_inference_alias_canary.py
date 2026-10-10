import pytest

from vera.models.admission import ModelDeploymentTarget
from vera.models.inference_alias_canary import (
    InferenceAliasCanaryPaths,
    InferenceAliasCanarySession,
)
from vera.models.inference_contracts import (
    InferenceEvent,
    InferenceRequest,
    InferenceValue,
)
from vera.models.inference_health import InferenceProviderHealth
from vera.models.inference_registry import ProviderProfile
from vera.models.model_package import (
    ModelArtifact,
    ModelCompatibility,
    ModelPackage,
)


pytestmark = pytest.mark.critical
COMMIT = "a" * 40


class Provider:
    def __init__(self, package_id, *, outage=False):
        self.package_id = package_id
        self.outage = outage
        self.calls = 0

    def profile(self):
        return ProviderProfile("ollama:cpu-a", "inference", ("generate",))

    async def infer(self, request, *, cancellation=None):
        self.calls += 1
        if cancellation is not None:
            cancellation.checkpoint()
        if self.outage:
            yield InferenceEvent(
                request.request_id, request.model_package_id,
                "ollama:cpu-a", 0, "failed",
                error_code="provider_unavailable")
            return
        values = ("left", "right") if request.stream else ("whole",)
        for sequence, value in enumerate(values):
            yield InferenceEvent(
                request.request_id, request.model_package_id,
                "ollama:cpu-a", sequence, "output",
                InferenceValue.from_json("text", value,
                                         media_type="text/plain"))
        yield InferenceEvent(
            request.request_id, request.model_package_id,
            "ollama:cpu-a", len(values), "completed",
            usage=(("output_tokens", len(values)),))


def package():
    return ModelPackage(
        "canary-model", "1", "transformer", "gguf",
        (ModelArtifact("manifest", "ollama://cpu-a/canary", "b" * 64, 10),),
        ModelCompatibility(("generate",), "prompt/v1", "text/v1",
                           accelerators=("cpu",)),
        framework="ollama", framework_version="1")


def request(value, *, stream=False):
    return InferenceRequest(
        value.package_id, "generate", "prompt/v1", "text/v1",
        (InferenceValue.from_json("prompt", "bounded"),), stream=stream)


def target():
    return ModelDeploymentTarget(
        "cpu-a", "generate", "prompt/v1", "text/v1", 1_000_000,
        accelerators=("cpu",), frameworks=("ollama",))


def health(value, *, revision=1):
    return InferenceProviderHealth(
        "ollama:cpu-a", "ready", 100, 10_000, "runtime-probe",
        (value.package_id,), concurrency_limit=1, revision=revision)


def prepared(tmp_path, *, provider=None):
    value = package()
    provider = provider or Provider(value.package_id)
    session = InferenceAliasCanarySession(
        InferenceAliasCanaryPaths.under(tmp_path))
    binding = session.prepare(
        alias="canary", package=value, provider=provider,
        health=health(value), request=request(value), target=target(),
        source_operation="validation-1", as_of_ms=150,
        placements=("cpu",))
    return session, binding, provider, value


def test_prepare_persists_exact_join_and_replays(tmp_path):
    session, binding, provider, value = prepared(tmp_path)
    assert provider.calls == 0
    assert session.packages.get("canary") == value
    assert session.deployments.current(
        binding.deployment.deployment_id) == binding.observation
    assert session.routes.current("canary") == binding.route
    replay = session.prepare(
        alias="canary", package=value, provider=provider,
        health=health(value), request=request(value), target=target(),
        source_operation="validation-1", as_of_ms=150,
        placements=("cpu",))
    assert replay.route == binding.route
    assert replay.activation == binding.activation


@pytest.mark.asyncio
@pytest.mark.parametrize(("scenario", "stream"),
                         (("nonstream", False), ("stream", True)))
async def test_compare_journals_direct_portable_parity(
        tmp_path, scenario, stream):
    session, binding, provider, value = prepared(tmp_path)
    evidence = await session.compare(
        binding, request(value, stream=stream), scenario=scenario,
        source_commit=COMMIT, observed_at_ms=150)
    assert evidence.passed
    assert provider.calls == 2
    assert len(evidence.evidence_refs) == 2


@pytest.mark.asyncio
async def test_outage_is_compared_without_hidden_retry(tmp_path):
    value = package()
    provider = Provider(value.package_id, outage=True)
    session, binding, _, _ = prepared(tmp_path, provider=provider)
    evidence = await session.compare(
        binding, request(value), scenario="outage",
        source_commit=COMMIT, observed_at_ms=150)
    assert evidence.passed
    assert provider.calls == 2


def test_rollback_removes_exact_route_and_alias_and_is_evidence(tmp_path):
    session, binding, _, _ = prepared(tmp_path)
    evidence = session.rollback(
        binding, source_operation="validation-1",
        source_commit=COMMIT, observed_at_ms=160)
    assert evidence.passed
    assert session.routes.current("canary") is None
    assert session.packages.get("canary") is None


def test_rollback_rehearsal_preserves_source_authority(tmp_path):
    session, binding, _, value = prepared(tmp_path)
    evidence = session.rehearse_rollback(
        binding, source_commit=COMMIT, observed_at_ms=160)
    assert evidence.passed
    assert session.routes.current("canary") == binding.route
    assert session.packages.get("canary") == value


def test_live_report_records_payload_free_cases_and_cancellation(tmp_path):
    session, binding, _, _ = prepared(tmp_path)
    report = {
        "schema": "vera.live-inference-validation/v2",
        "privacy": "prompt_and_output_omitted",
        "model_package_id": binding.route.package_id,
        "provider": binding.route.provider_id,
        "cases": [
            {"case": "non_stream", "passed": True,
             "output_sha256": "1" * 64,
             "expected_output_sha256": "1" * 64},
            {"case": "stream", "passed": True,
             "output_sha256": "1" * 64,
             "expected_output_sha256": "1" * 64},
        ],
        "cancellation": {"status": "cancelled", "passed": True,
                         "cancellation_observed": True,
                         "task_reaped": True},
    }
    evidence = session.record_live_report(
        binding, report, source_commit=COMMIT, observed_at_ms=150)
    assert [item.scenario for item in evidence] == [
        "nonstream", "stream", "cancellation"]
    encoded = str([item.to_dict() for item in evidence])
    assert "prompt" not in encoded and "output_sha256" not in encoded


def test_live_report_rejects_foreign_route_before_evidence(tmp_path):
    session, binding, _, _ = prepared(tmp_path)
    with pytest.raises(ValueError, match="another route"):
        session.record_live_report(binding, {
            "schema": "vera.live-inference-validation/v2",
            "privacy": "prompt_and_output_omitted",
            "model_package_id": "mpkg_foreign",
            "provider": binding.route.provider_id,
            "cases": [],
        }, source_commit=COMMIT, observed_at_ms=150)
    assert session.parity.latest(binding.route.route_id, COMMIT) == ()


def test_live_report_is_all_or_nothing_on_malformed_cancellation(tmp_path):
    session, binding, _, _ = prepared(tmp_path)
    with pytest.raises(ValueError, match="cancellation"):
        session.record_live_report(binding, {
            "schema": "vera.live-inference-validation/v2",
            "privacy": "prompt_and_output_omitted",
            "model_package_id": binding.route.package_id,
            "provider": binding.route.provider_id,
            "cases": [
                {"case": "non_stream", "passed": True,
                 "output_sha256": "1" * 64,
                 "expected_output_sha256": "1" * 64},
                {"case": "stream", "passed": True,
                 "output_sha256": "1" * 64,
                 "expected_output_sha256": "1" * 64},
            ],
        }, source_commit=COMMIT, observed_at_ms=150)
    assert session.parity.latest(binding.route.route_id, COMMIT) == ()


def test_readiness_stays_closed_until_every_scenario_exists(tmp_path):
    session, binding, _, _ = prepared(tmp_path)
    session.rollback(
        binding, source_operation="validation-1",
        source_commit=COMMIT, observed_at_ms=160)
    status = session.readiness(
        binding.route, source_commit=COMMIT,
        as_of_ms=170, max_age_ms=1_000)
    assert not status.ready_for_activation_review
    assert not status.traffic_activation_allowed
    assert set(status.reasons) == {
        "missing_nonstream", "missing_stream", "missing_cancellation",
        "missing_outage"}


@pytest.mark.asyncio
async def test_all_scenarios_enable_review_but_never_traffic(tmp_path):
    session, binding, _, value = prepared(tmp_path)
    await session.compare(
        binding, request(value), scenario="nonstream",
        source_commit=COMMIT, observed_at_ms=150)
    await session.compare(
        binding, request(value, stream=True), scenario="stream",
        source_commit=COMMIT, observed_at_ms=151)
    session.record_scenario(
        binding, scenario="cancellation", source_commit=COMMIT,
        observed_at_ms=152, passed=True,
        evidence_refs=("live-cancellation-cancelled",))
    session.record_scenario(
        binding, scenario="outage", source_commit=COMMIT,
        observed_at_ms=153, passed=True,
        evidence_refs=("direct-outage", "portable-outage"))
    session.rehearse_rollback(
        binding, source_commit=COMMIT, observed_at_ms=154)
    status = session.readiness(
        binding.route, source_commit=COMMIT,
        as_of_ms=160, max_age_ms=1_000)
    assert status.ready_for_activation_review
    assert not status.traffic_activation_allowed
    assert not status.reasons


def test_mismatched_health_fails_before_alias_activation(tmp_path):
    value = package()
    session = InferenceAliasCanarySession(
        InferenceAliasCanaryPaths.under(tmp_path))
    wrong = InferenceProviderHealth(
        "ollama:other", "ready", 100, 10_000, "probe",
        (value.package_id,), concurrency_limit=1)
    with pytest.raises(ValueError, match="another provider"):
        session.prepare(
            alias="canary", package=value, provider=Provider(value.package_id),
            health=wrong, request=request(value), target=target(),
            source_operation="validation-1", as_of_ms=150,
            placements=("cpu",))
    assert session.packages.get("canary") is None


def test_route_bind_failure_compensates_alias_activation(tmp_path, monkeypatch):
    value = package()
    session = InferenceAliasCanarySession(
        InferenceAliasCanaryPaths.under(tmp_path))

    def fail(*args, **kwargs):
        raise RuntimeError("route store unavailable")

    monkeypatch.setattr(session.routes, "bind", fail)
    with pytest.raises(RuntimeError, match="route store"):
        session.prepare(
            alias="canary", package=value, provider=Provider(value.package_id),
            health=health(value), request=request(value), target=target(),
            source_operation="validation-1", as_of_ms=150,
            placements=("cpu",))
    assert session.packages.get("canary") is None
    assert [item.kind for item in session.packages.activation_history()] == [
        "activate", "rollback"]
