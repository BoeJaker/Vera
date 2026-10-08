"""Default-off resolution of a portable alias route into a fresh dispatch plan."""
from __future__ import annotations

from .inference_alias_route import SQLiteInferenceAliasRouteRegistry
from .inference_contracts import InferenceContractConflict, InferenceRequest
from .inference_deployment import SQLiteInferenceDeploymentRegistry
from .inference_dispatch import InferenceDispatchPlan, plan_inference_dispatch
from .inference_registry import InferenceProviderRegistry
from .model_package import _identifier
from .model_package_store import SQLiteModelPackageRegistry


def resolve_inference_alias_dispatch(
        alias: str, request: InferenceRequest, *, enabled: bool,
        packages: SQLiteModelPackageRegistry,
        routes: SQLiteInferenceAliasRouteRegistry,
        providers: InferenceProviderRegistry,
        deployments: SQLiteInferenceDeploymentRegistry,
        as_of_ms: int) -> InferenceDispatchPlan | None:
    """Revalidate one bound alias and plan it; never invoke the provider.

    Disabled resolution is deliberately inert: it does not inspect any registry.
    """
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be boolean")
    if not enabled:
        return None
    alias = _identifier(alias, "alias")
    if not isinstance(request, InferenceRequest):
        raise TypeError("request must be InferenceRequest")
    route = routes.current(alias)
    if route is None:
        raise InferenceContractConflict("portable alias route is not bound")
    package = packages.get(alias)
    if package is None or package.package_id != route.package_id \
            or request.model_package_id != route.package_id:
        raise InferenceContractConflict("portable alias package binding has changed")
    history = packages.activation_history(alias)
    if not history or history[-1].kind != "activate" \
            or history[-1].operation_id != route.activation_operation_id \
            or history[-1].package_id != route.package_id:
        raise InferenceContractConflict("portable alias activation evidence is not current")
    admission = packages.admission_for_activation(route.activation_operation_id)
    if admission is None or admission.admission_id != route.admission_id:
        raise InferenceContractConflict("portable alias admission evidence has changed")
    deployment = deployments.get(route.deployment_id)
    observation = deployments.current(route.deployment_id)
    if deployment is None or deployment.package_id != route.package_id \
            or deployment.admission_id != route.admission_id:
        raise InferenceContractConflict("portable alias deployment evidence has changed")
    if observation is None or observation.revision != route.deployment_revision \
            or observation.observation_id != route.observation_id:
        raise InferenceContractConflict("portable alias deployment observation has changed")
    descriptor = providers.get(route.provider_id)
    if descriptor is None or descriptor.revision != route.provider_revision \
            or descriptor.health is None \
            or descriptor.health.evidence_id != route.health_evidence_id:
        raise InferenceContractConflict("portable alias provider evidence has changed")
    plan = plan_inference_dispatch(
        request, route.provider_id, route.deployment_id, as_of_ms=as_of_ms,
        policy=route.dispatch_policy, providers=providers,
        deployments=deployments)
    if plan.retry_owner != route.retry_owner \
            or plan.deployment_revision != route.deployment_revision \
            or plan.provider_revision != route.provider_revision \
            or plan.health_evidence_id != route.health_evidence_id:
        raise InferenceContractConflict("portable alias dispatch evidence has changed")
    return plan
