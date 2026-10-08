from concurrent.futures import ThreadPoolExecutor

import pytest

from vera.models import (
    InferenceAliasRoute, InferenceContractConflict, InferenceDispatchPolicy,
    InferenceProviderRegistry, SQLiteInferenceAliasRouteRegistry,
    SQLiteInferenceDeploymentRegistry, SQLiteModelPackageRegistry,
    define_inference_alias_route, inference_alias_route_from_dict,
    plan_inference_dispatch, resolve_inference_alias_dispatch)
from tests.test_inference_dispatch import Provider, health, request
from tests.test_inference_deployment import admitted, deployment

pytestmark = pytest.mark.critical


def evidence(tmp_path, *, alias="canary"):
    package, admission = admitted()
    deployed = deployment(package)
    packages = SQLiteModelPackageRegistry(tmp_path / "packages.sqlite")
    packages.register(package)
    activated = packages.activate_admitted(
        alias, package.package_id, operation_id="activate-canary",
        policy=admission.policy, target=admission.target)
    observed_health = health(deployed)
    providers = InferenceProviderRegistry()
    provider = Provider()
    providers.register(provider, package_ids=(deployed.package_id,),
                       placements=deployed.placements, health=observed_health)
    deployments = SQLiteInferenceDeploymentRegistry(tmp_path / "deployments.sqlite")
    deployments.register(deployed)
    observation = deployments.observe(
        deployed.deployment_id, expected_revision=0, desired_state="active",
        health=observed_health)
    dispatch = plan_inference_dispatch(
        request(deployed), deployed.provider_id, deployed.deployment_id,
        as_of_ms=150, policy=InferenceDispatchPolicy("canary", retry_limit=1),
        providers=providers, deployments=deployments)
    route = define_inference_alias_route(
        alias, activated.activation, activated.admission, deployed,
        observation, dispatch)
    return route, provider, packages, providers, deployments


def test_route_binds_exact_evidence_without_invoking_provider(tmp_path):
    route, provider, *_ = evidence(tmp_path)
    assert route.alias == "canary"
    assert route.deployment_revision == 1
    assert route.dispatch_policy.policy_id == "canary"
    assert route.dispatch_policy.retry_limit == 1
    assert route.retry_owner == "inference-router"
    assert provider.calls == 0
    assert inference_alias_route_from_dict(route.to_dict()) == route


def test_cross_package_or_non_ready_evidence_fails_before_persistence(tmp_path):
    route, *_ = evidence(tmp_path)
    changed = route.to_dict()
    changed["package_id"] = "mpkg_foreign"
    with pytest.raises(ValueError, match="identity"):
        inference_alias_route_from_dict(changed)


def test_binding_is_durable_replay_safe_and_compare_and_set_guarded(tmp_path):
    route, *_ = evidence(tmp_path)
    path = tmp_path / "routes.sqlite"
    store = SQLiteInferenceAliasRouteRegistry(path)
    assert store.bind(route, operation_id="bind-1") == route
    assert SQLiteInferenceAliasRouteRegistry(path).bind(
        route, operation_id="bind-1") == route
    assert SQLiteInferenceAliasRouteRegistry(path).current("canary") == route
    with pytest.raises(InferenceContractConflict, match="compare-and-set"):
        store.bind(route, operation_id="bind-2")


def test_rollback_removes_initial_binding_and_replays(tmp_path):
    route, *_ = evidence(tmp_path)
    path = tmp_path / "routes.sqlite"
    store = SQLiteInferenceAliasRouteRegistry(path)
    store.bind(route, operation_id="bind-1")
    assert store.rollback("bind-1", operation_id="rollback-1") is None
    assert SQLiteInferenceAliasRouteRegistry(path).rollback(
        "bind-1", operation_id="rollback-1") is None
    assert store.current("canary") is None


def test_stale_rollback_cannot_overwrite_newer_route(tmp_path):
    route, *_ = evidence(tmp_path)
    values = route.identity_dict()
    values["provider_revision"] = 2
    values["dispatch_policy"] = route.dispatch_policy
    values.pop("schema")
    replacement = InferenceAliasRoute(**values)
    store = SQLiteInferenceAliasRouteRegistry(tmp_path / "routes.sqlite")
    store.bind(route, operation_id="bind-1")
    store.bind(replacement, expected_route_id=route.route_id,
               operation_id="bind-2")
    with pytest.raises(InferenceContractConflict, match="compare-and-set"):
        store.rollback("bind-1", operation_id="rollback-stale")
    assert store.current("canary") == replacement


def test_concurrent_compare_and_set_has_one_winner(tmp_path):
    route, *_ = evidence(tmp_path)
    values = route.identity_dict()
    values["provider_revision"] = 2
    values["dispatch_policy"] = route.dispatch_policy
    values.pop("schema")
    other = InferenceAliasRoute(**values)
    path = tmp_path / "routes.sqlite"

    def bind(item):
        candidate, operation_id = item
        try:
            return SQLiteInferenceAliasRouteRegistry(path).bind(
                candidate, operation_id=operation_id)
        except InferenceContractConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, ((route, "bind-a"), (other, "bind-b"))))
    assert sum(value is not None for value in results) == 1


def test_complete_policy_is_identity_bound_not_just_its_name(tmp_path):
    route, *_ = evidence(tmp_path)
    changed = route.to_dict()
    changed["dispatch_policy"]["retry_limit"] = 0
    with pytest.raises(ValueError, match="identity"):
        inference_alias_route_from_dict(changed)


def test_disabled_runtime_resolution_is_store_and_provider_inert():
    class Unusable:
        def __getattr__(self, name):
            raise AssertionError(f"disabled resolver touched {name}")

    assert resolve_inference_alias_dispatch(
        "not-even-validated", object(), enabled=False, packages=Unusable(),
        routes=Unusable(), providers=Unusable(), deployments=Unusable(),
        as_of_ms=-1) is None


def test_runtime_resolution_revalidates_and_builds_fresh_plan(tmp_path):
    route, provider, packages, providers, deployments = evidence(tmp_path)
    route_store = SQLiteInferenceAliasRouteRegistry(tmp_path / "routes.sqlite")
    route_store.bind(route, operation_id="bind-1")
    deployed = deployments.get(route.deployment_id)
    result = resolve_inference_alias_dispatch(
        "canary", request(deployed), enabled=True, packages=packages,
        routes=route_store, providers=providers, deployments=deployments,
        as_of_ms=150)
    assert result is not None
    assert result.plan_id == route.sample_dispatch_plan_id
    assert result.policy == route.dispatch_policy
    assert provider.calls == 0


def test_runtime_resolution_fails_closed_after_observation_drift(tmp_path):
    route, _, packages, providers, deployments = evidence(tmp_path)
    route_store = SQLiteInferenceAliasRouteRegistry(tmp_path / "routes.sqlite")
    route_store.bind(route, operation_id="bind-1")
    deployed = deployments.get(route.deployment_id)
    deployments.observe(
        route.deployment_id, expected_revision=1, desired_state="stopped",
        observed_state="stopped", observed_at_ms=160)
    with pytest.raises(InferenceContractConflict, match="observation has changed"):
        resolve_inference_alias_dispatch(
            "canary", request(deployed), enabled=True, packages=packages,
            routes=route_store, providers=providers, deployments=deployments,
            as_of_ms=160)
