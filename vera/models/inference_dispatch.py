"""Deterministic, non-executing inference dispatch decisions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from .inference_contracts import InferenceContractConflict, InferenceRequest
from .inference_deployment import SQLiteInferenceDeploymentRegistry
from .inference_registry import InferenceProviderRegistry
from .model_package import _identifier

INFERENCE_DISPATCH_POLICY_SCHEMA = "vera.inference-dispatch-policy/v1"
INFERENCE_DISPATCH_PLAN_SCHEMA = "vera.inference-dispatch-plan/v1"


def _canonical_hash(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InferenceDispatchPolicy:
    policy_id: str
    retry_limit: int = 0
    require_available_slot: bool = True
    max_queue_depth: int | None = None
    schema: str = INFERENCE_DISPATCH_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INFERENCE_DISPATCH_POLICY_SCHEMA:
            raise ValueError("unsupported inference dispatch policy schema")
        object.__setattr__(self, "policy_id", _identifier(
            self.policy_id, "dispatch policy ID"))
        if isinstance(self.retry_limit, bool) or not isinstance(
                self.retry_limit, int) or not 0 <= self.retry_limit <= 3:
            raise ValueError("retry_limit must be between zero and three")
        if not isinstance(self.require_available_slot, bool):
            raise TypeError("require_available_slot must be boolean")
        if self.max_queue_depth is not None and (
                isinstance(self.max_queue_depth, bool)
                or not isinstance(self.max_queue_depth, int)
                or not 0 <= self.max_queue_depth <= 1_000_000):
            raise ValueError("max_queue_depth must be a bounded non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "policy_id": self.policy_id,
                "retry_limit": self.retry_limit,
                "require_available_slot": self.require_available_slot,
                "max_queue_depth": self.max_queue_depth}


@dataclass(frozen=True, slots=True)
class InferenceDispatchPlan:
    request_id: str
    model_package_id: str
    deployment_id: str
    provider_id: str
    provider_revision: int
    deployment_revision: int
    health_evidence_id: str
    policy: InferenceDispatchPolicy
    retry_owner: str
    as_of_ms: int
    plan_id: str = field(init=False)
    schema: str = INFERENCE_DISPATCH_PLAN_SCHEMA

    def __post_init__(self) -> None:
        for name in ("request_id", "model_package_id", "deployment_id",
                     "provider_id", "health_evidence_id", "retry_owner"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in ("provider_revision", "deployment_revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be positive")
        if not isinstance(self.policy, InferenceDispatchPolicy):
            raise TypeError("policy must be InferenceDispatchPolicy")
        if isinstance(self.as_of_ms, bool) or not isinstance(self.as_of_ms, int) \
                or self.as_of_ms < 0:
            raise ValueError("as_of_ms must be a non-negative Unix millisecond")
        object.__setattr__(self, "plan_id", _canonical_hash(
            "idisp_", self.identity_dict()))

    @property
    def max_attempts(self) -> int:
        return self.policy.retry_limit + 1

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "request_id": self.request_id,
            "model_package_id": self.model_package_id,
            "deployment_id": self.deployment_id,
            "provider_id": self.provider_id,
            "provider_revision": self.provider_revision,
            "deployment_revision": self.deployment_revision,
            "health_evidence_id": self.health_evidence_id,
            "policy": self.policy.to_dict(), "retry_owner": self.retry_owner,
            "as_of_ms": self.as_of_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "max_attempts": self.max_attempts,
                **self.identity_dict()}


def inference_dispatch_plan_from_dict(
        value: Mapping[str, Any]) -> InferenceDispatchPlan:
    if not isinstance(value, Mapping) or value.get("schema") != \
            INFERENCE_DISPATCH_PLAN_SCHEMA:
        raise ValueError("unsupported inference dispatch plan schema")
    try:
        policy = InferenceDispatchPolicy(**dict(value["policy"]))
        plan = InferenceDispatchPlan(
            request_id=value["request_id"],
            model_package_id=value["model_package_id"],
            deployment_id=value["deployment_id"],
            provider_id=value["provider_id"],
            provider_revision=value["provider_revision"],
            deployment_revision=value["deployment_revision"],
            health_evidence_id=value["health_evidence_id"], policy=policy,
            retry_owner=value["retry_owner"], as_of_ms=value["as_of_ms"])
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference dispatch plan") from exc
    if value.get("plan_id") != plan.plan_id:
        raise ValueError("inference dispatch plan identity does not match content")
    if "max_attempts" in value and value["max_attempts"] != plan.max_attempts:
        raise ValueError("inference dispatch attempts do not match policy")
    return plan


def plan_inference_dispatch(
        request: InferenceRequest, provider_id: str, deployment_id: str, *,
        as_of_ms: int, policy: InferenceDispatchPolicy,
        providers: InferenceProviderRegistry,
        deployments: SQLiteInferenceDeploymentRegistry) -> InferenceDispatchPlan:
    """Validate one explicit route; never choose, execute, retry, or fail over."""
    if not isinstance(request, InferenceRequest):
        raise TypeError("request must be InferenceRequest")
    if not isinstance(policy, InferenceDispatchPolicy):
        raise TypeError("policy must be InferenceDispatchPolicy")
    if not isinstance(providers, InferenceProviderRegistry):
        raise TypeError("providers must be InferenceProviderRegistry")
    if not isinstance(deployments, SQLiteInferenceDeploymentRegistry):
        raise TypeError("deployments must be SQLiteInferenceDeploymentRegistry")
    provider_id = _identifier(provider_id, "provider ID")
    deployment_id = _identifier(deployment_id, "deployment ID")
    deployment = deployments.get(deployment_id)
    if deployment is None:
        raise KeyError("inference deployment is not registered")
    if deployment.provider_id != provider_id:
        raise InferenceContractConflict(
            "deployment belongs to another inference provider")
    if deployment.package_id != request.model_package_id:
        raise InferenceContractConflict(
            "deployment belongs to another model package")
    descriptor = providers.get(provider_id)
    if descriptor is None:
        raise KeyError("inference provider is not registered")
    eligible = providers.candidates(
        request, placements=deployment.placements, as_of_ms=as_of_ms)
    if descriptor not in eligible:
        raise InferenceContractConflict(
            "selected provider is not eligible for dispatch")
    observation = deployments.current(deployment_id)
    if observation is None:
        raise InferenceContractConflict(
            "deployment has no lifecycle observation")
    if observation.desired_state != "active" or observation.observed_state != "ready":
        raise InferenceContractConflict("deployment is not active and ready")
    health = observation.health
    if health is None or not health.is_current(as_of_ms):
        raise InferenceContractConflict(
            "deployment health evidence is absent or stale")
    if descriptor.health is None \
            or descriptor.health.evidence_id != health.evidence_id:
        raise InferenceContractConflict(
            "provider and deployment health evidence do not match")
    if policy.require_available_slot and health.available_slots < 1:
        raise InferenceContractConflict("provider has no available inference slot")
    if policy.max_queue_depth is not None \
            and health.queue_depth > policy.max_queue_depth:
        raise InferenceContractConflict("provider queue exceeds dispatch policy")
    return InferenceDispatchPlan(
        request.request_id, request.model_package_id, deployment.deployment_id,
        provider_id, descriptor.revision, observation.revision,
        health.evidence_id, policy, deployment.retry_owner, as_of_ms)
