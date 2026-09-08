import pytest

from vera.models import (
    InferenceContractConflict, InferenceDispatchPolicy, InferenceEvent,
    InferenceProviderHealth, InferenceProviderRegistry, InferenceRequest,
    InferenceValue, ProviderProfile, SQLiteInferenceDeploymentRegistry,
    inference_dispatch_plan_from_dict, plan_inference_dispatch)
from tests.test_inference_deployment import deployment

pytestmark = pytest.mark.critical


class Provider:
    calls = 0

    def __init__(self, provider_id="onnx:a"):
        self._profile = ProviderProfile(provider_id, "inference", ("inference",))

    def profile(self):
        return self._profile

    async def infer(self, request, *, cancellation=None):
        self.calls += 1
        raise AssertionError("dispatch planning must not invoke inference")
        yield InferenceEvent(  # pragma: no cover
            request.request_id, request.model_package_id,
            self._profile.provider_id, 0, "completed")


def request(value):
    return InferenceRequest(
        value.package_id, "inference", "tensor/v1", "tensor/v1",
        (InferenceValue.from_json("X", [[1, 2]]),))


def health(value, *, source="probe", revision=1, in_flight=1,
           queue_depth=0, observed=100):
    return InferenceProviderHealth(
        "onnx:a", "ready", observed, observed + 100, source,
        (value.package_id,), concurrency_limit=2, in_flight=in_flight,
        queue_depth=queue_depth, latency_ms=4, revision=revision)


def setup(tmp_path, *, provider_health=None):
    deployed = deployment()
    observed_health = health(deployed)
    providers = InferenceProviderRegistry()
    provider = Provider()
    providers.register(
        provider, package_ids=(deployed.package_id,), placements=("gpu", "local"),
        health=provider_health or observed_health)
    deployments = SQLiteInferenceDeploymentRegistry(tmp_path / "deployments.sqlite")
    deployments.register(deployed)
    deployments.observe(
        deployed.deployment_id, expected_revision=0, desired_state="active",
        health=observed_health)
    return deployed, provider, providers, deployments, observed_health


def test_plan_binds_explicit_route_and_current_evidence_without_execution(tmp_path):
    deployed, provider, providers, deployments, observed_health = setup(tmp_path)
    policy = InferenceDispatchPolicy("interactive", retry_limit=1,
                                     max_queue_depth=2)
    plan = plan_inference_dispatch(
        request(deployed), deployed.provider_id, deployed.deployment_id,
        as_of_ms=150, policy=policy, providers=providers,
        deployments=deployments)
    assert plan.provider_id == deployed.provider_id
    assert plan.deployment_id == deployed.deployment_id
    assert plan.health_evidence_id == observed_health.evidence_id
    assert plan.retry_owner == deployed.retry_owner
    assert plan.max_attempts == 2
    assert provider.calls == 0
    assert inference_dispatch_plan_from_dict(plan.to_dict()) == plan


def test_plan_rejects_stale_or_divergent_health_evidence(tmp_path):
    deployed, _, providers, deployments, _ = setup(tmp_path)
    with pytest.raises(InferenceContractConflict, match="eligible"):
        plan_inference_dispatch(
            request(deployed), deployed.provider_id, deployed.deployment_id,
            as_of_ms=201, policy=InferenceDispatchPolicy("default"),
            providers=providers, deployments=deployments)

    other_health = health(deployed, source="new-probe", revision=2)
    other_providers = InferenceProviderRegistry()
    other_providers.register(
        Provider(), package_ids=(deployed.package_id,),
        placements=("gpu", "local"),
        health=other_health)
    with pytest.raises(InferenceContractConflict, match="do not match"):
        plan_inference_dispatch(
            request(deployed), deployed.provider_id, deployed.deployment_id,
            as_of_ms=150, policy=InferenceDispatchPolicy("default"),
            providers=other_providers, deployments=deployments)


def test_capacity_and_queue_are_explicit_policy_not_hidden_ranking(tmp_path):
    deployed = deployment()
    saturated = health(deployed, in_flight=2, queue_depth=5)
    _, _, providers, deployments, _ = setup(
        tmp_path, provider_health=saturated)
    # Make the durable observation cite the same saturated evidence.
    deployments.observe(
        deployed.deployment_id, expected_revision=1, desired_state="active",
        health=saturated)
    with pytest.raises(InferenceContractConflict, match="no available"):
        plan_inference_dispatch(
            request(deployed), deployed.provider_id, deployed.deployment_id,
            as_of_ms=150, policy=InferenceDispatchPolicy("strict"),
            providers=providers, deployments=deployments)
    with pytest.raises(InferenceContractConflict, match="queue"):
        plan_inference_dispatch(
            request(deployed), deployed.provider_id, deployed.deployment_id,
            as_of_ms=150,
            policy=InferenceDispatchPolicy(
                "queued", require_available_slot=False, max_queue_depth=4),
            providers=providers, deployments=deployments)
    assert plan_inference_dispatch(
        request(deployed), deployed.provider_id, deployed.deployment_id,
        as_of_ms=150,
        policy=InferenceDispatchPolicy("allow-queue", require_available_slot=False),
        providers=providers, deployments=deployments).provider_id == "onnx:a"


def test_plan_rejects_inactive_deployment_and_foreign_explicit_identity(tmp_path):
    deployed, _, providers, deployments, _ = setup(tmp_path)
    deployments.observe(
        deployed.deployment_id, expected_revision=1, desired_state="stopped",
        observed_state="stopped", observed_at_ms=160)
    with pytest.raises(InferenceContractConflict, match="active and ready"):
        plan_inference_dispatch(
            request(deployed), deployed.provider_id, deployed.deployment_id,
            as_of_ms=160, policy=InferenceDispatchPolicy("default"),
            providers=providers, deployments=deployments)
    with pytest.raises(InferenceContractConflict, match="another inference provider"):
        plan_inference_dispatch(
            request(deployed), "other", deployed.deployment_id,
            as_of_ms=160, policy=InferenceDispatchPolicy("default"),
            providers=providers, deployments=deployments)


def test_dispatch_policy_and_serialized_derived_fields_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="zero and three"):
        InferenceDispatchPolicy("bad", retry_limit=4)
    with pytest.raises(ValueError, match="policy schema"):
        InferenceDispatchPolicy("bad", schema="foreign")
    deployed, _, providers, deployments, _ = setup(tmp_path)
    plan = plan_inference_dispatch(
        request(deployed), deployed.provider_id, deployed.deployment_id,
        as_of_ms=150, policy=InferenceDispatchPolicy("default"),
        providers=providers, deployments=deployments)
    changed = plan.to_dict()
    changed["max_attempts"] = 99
    with pytest.raises(ValueError, match="attempts"):
        inference_dispatch_plan_from_dict(changed)
    changed = plan.to_dict()
    changed["retry_owner"] = "other"
    with pytest.raises(ValueError, match="identity"):
        inference_dispatch_plan_from_dict(changed)
