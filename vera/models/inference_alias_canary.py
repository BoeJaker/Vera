"""Bounded lifecycle for validating one portable inference alias route.

This module composes the existing package, deployment, route, dispatch and
parity stores.  It does not register a capability or move ordinary inference
traffic.  Callers must inject the exact provider and explicit deployment facts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from .admission import (
    ModelDeploymentTarget,
    ModelPackageAdmissionRejected,
    ModelTrustPolicy,
    evaluate_model_admission,
)
from .inference_alias_readiness import (
    InferenceAliasParityEvidence,
    InferenceAliasReadiness,
    SQLiteInferenceAliasParityStore,
    evaluate_inference_alias_readiness,
)
from .inference_alias_route import (
    InferenceAliasRoute,
    SQLiteInferenceAliasRouteRegistry,
    define_inference_alias_route,
)
from .inference_alias_runtime import execute_inference_alias_dispatch
from .inference_contracts import (
    InferenceContractConflict,
    InferenceProvider,
    InferenceRequest,
    InferenceTranscript,
    consume_inference,
)
from .inference_deployment import (
    InferenceDeployment,
    InferenceDeploymentObservation,
    SQLiteInferenceDeploymentRegistry,
    define_inference_deployment,
)
from .inference_dispatch import InferenceDispatchPolicy, plan_inference_dispatch
from .inference_health import InferenceProviderHealth
from .inference_registry import InferenceProviderRegistry
from .model_package import ModelPackage, _identifier
from .model_package_store import (
    AdmittedModelActivation,
    SQLiteModelPackageRegistry,
)


@dataclass(frozen=True, slots=True)
class InferenceAliasCanaryPaths:
    """The four durable stores used by one bounded canary programme."""

    packages: Path
    deployments: Path
    routes: Path
    parity: Path

    @classmethod
    def under(cls, root: str | Path) -> "InferenceAliasCanaryPaths":
        base = Path(root)
        return cls(
            base / "packages.sqlite3",
            base / "deployments.sqlite3",
            base / "alias-routes.sqlite3",
            base / "alias-parity.sqlite3",
        )


@dataclass(frozen=True, slots=True)
class InferenceAliasCanaryBinding:
    activation: AdmittedModelActivation
    deployment: InferenceDeployment
    observation: InferenceDeploymentObservation
    route: InferenceAliasRoute
    activation_operation_id: str
    route_operation_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "activation": {
                "receipt": self.activation.activation.to_dict(),
                "admission": self.activation.admission.to_dict(),
            },
            "deployment": self.deployment.to_dict(),
            "observation": self.observation.to_dict(),
            "route": self.route.to_dict(),
            "activation_operation_id": self.activation_operation_id,
            "route_operation_id": self.route_operation_id,
            "traffic_activation_allowed": False,
        }


def _result_ref(prefix: str, result: InferenceTranscript) -> str:
    encoded = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))
    return _identifier(
        f"{prefix}-{hashlib.sha256(encoded.encode()).hexdigest()}",
        "evidence reference",
    )


def _sha256(value: Any, field: str) -> str:
    value = str(value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return value


class InferenceAliasCanarySession:
    """Prepare, execute, evidence and roll back one explicit alias route."""

    def __init__(self, paths: InferenceAliasCanaryPaths) -> None:
        if not isinstance(paths, InferenceAliasCanaryPaths):
            raise TypeError("paths must be InferenceAliasCanaryPaths")
        self.paths = paths
        self.packages = SQLiteModelPackageRegistry(paths.packages)
        self.deployments = SQLiteInferenceDeploymentRegistry(paths.deployments)
        self.routes = SQLiteInferenceAliasRouteRegistry(paths.routes)
        self.parity = SQLiteInferenceAliasParityStore(paths.parity)
        self.providers = InferenceProviderRegistry()

    def prepare(
        self, *, alias: str, package: ModelPackage, provider: InferenceProvider,
        health: InferenceProviderHealth, request: InferenceRequest,
        target: ModelDeploymentTarget, source_operation: str,
        as_of_ms: int, placements: tuple[str, ...],
        retry_owner: str = "inference-router",
        trust_policy: ModelTrustPolicy | None = None,
        dispatch_policy: InferenceDispatchPolicy | None = None,
    ) -> InferenceAliasCanaryBinding:
        """Persist one fully joined route without changing a legacy route."""
        alias = _identifier(alias, "alias")
        source_operation = _identifier(source_operation, "source operation")
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        if not isinstance(request, InferenceRequest) \
                or request.model_package_id != package.package_id:
            raise ValueError("request must target the canary package")
        profile = provider.profile()
        if health.provider_id != profile.provider_id:
            raise ValueError("health evidence belongs to another provider")
        if package.package_id not in health.available_package_ids:
            raise ValueError("provider health omits the canary package")
        trust_policy = trust_policy or ModelTrustPolicy("portable-canary")
        dispatch_policy = dispatch_policy or InferenceDispatchPolicy(
            "portable-canary", retry_limit=0)
        activation_operation = f"{source_operation}.activate"
        route_operation = f"{source_operation}.bind"

        # Validate all immutable definitions before moving the alias. Durable
        # package/deployment observations are safe partial progress; the alias
        # and route are the authority pair and are written last.
        admission_preview = evaluate_model_admission(
            package, trust_policy, target)
        if not admission_preview.accepted:
            raise ModelPackageAdmissionRejected(admission_preview)
        deployment = define_inference_deployment(
            package, admission_preview, provider_id=profile.provider_id,
            runtime_kind=package.framework or "inference",
            runtime_version=package.framework_version or "unspecified",
            placements=placements, retry_owner=retry_owner)
        current_provider = self.providers.get(profile.provider_id)
        if current_provider is None:
            self.providers.register(
                provider, package_ids=(package.package_id,),
                placements=placements, health=health)
        elif (current_provider.package_ids != (package.package_id,)
              or current_provider.placements != tuple(sorted(set(placements)))
              or current_provider.health != health):
            raise ValueError("provider registration conflicts with canary evidence")
        self.packages.register(package)
        self.deployments.register(deployment)
        observation = self.deployments.observe(
            deployment.deployment_id, expected_revision=0,
            desired_state="active", health=health)
        dispatch = plan_inference_dispatch(
            request, profile.provider_id, deployment.deployment_id,
            as_of_ms=as_of_ms, policy=dispatch_policy,
            providers=self.providers, deployments=self.deployments)
        activation = self.packages.activate_admitted(
            alias, package.package_id, expected_package_id="",
            operation_id=activation_operation, policy=trust_policy,
            target=target)
        active = self.packages.get(alias)
        if active is None or active.package_id != package.package_id:
            raise RuntimeError(
                "activation operation was previously rolled back; use a new operation")
        try:
            route = define_inference_alias_route(
                alias, activation.activation, activation.admission,
                deployment, observation, dispatch)
            self.routes.bind(
                route, expected_route_id="", operation_id=route_operation)
        except Exception:
            # Cross-database atomicity is impossible. Restore the alias if the
            # final route bind loses a race or fails validation.
            self.packages.rollback(
                activation_operation,
                operation_id=f"{source_operation}.activate-compensation")
            raise
        return InferenceAliasCanaryBinding(
            activation, deployment, observation, route,
            activation_operation, route_operation)

    async def compare(
        self, binding: InferenceAliasCanaryBinding,
        request: InferenceRequest, *, scenario: str,
        source_commit: str, observed_at_ms: int,
        cancellation=None,
    ) -> InferenceAliasParityEvidence:
        """Execute direct and guarded paths once each and journal their parity."""
        if scenario not in {"nonstream", "stream", "cancellation", "outage"}:
            raise ValueError("unsupported executable parity scenario")
        provider = self.providers.resolve(
            request, binding.route.provider_id,
            placements=binding.deployment.placements,
            as_of_ms=observed_at_ms)
        direct = await consume_inference(
            provider, request, cancellation=cancellation)
        portable = await execute_inference_alias_dispatch(
            binding.route.alias, request, enabled=True,
            retry_owner=binding.route.retry_owner,
            packages=self.packages, routes=self.routes,
            providers=self.providers, deployments=self.deployments,
            as_of_ms=observed_at_ms, cancellation=cancellation)
        passed = direct == portable
        failures = () if passed else ("direct_portable_mismatch",)
        evidence = InferenceAliasParityEvidence(
            binding.route.route_id, scenario, source_commit, observed_at_ms,
            passed, (_result_ref("direct", direct),
                     _result_ref("portable", portable)), failures)
        return self.parity.record(evidence)

    def record_live_report(
        self, binding: InferenceAliasCanaryBinding, report: dict[str, Any], *,
        source_commit: str, observed_at_ms: int,
    ) -> tuple[InferenceAliasParityEvidence, ...]:
        """Journal bounded validator evidence without retaining model payloads."""
        if not isinstance(report, dict) \
                or report.get("schema") != "vera.live-inference-validation/v2":
            raise ValueError("unsupported live inference report")
        if report.get("privacy") != "prompt_and_output_omitted":
            raise ValueError("live inference report does not prove payload omission")
        if report.get("model_package_id") != binding.route.package_id \
                or report.get("provider") != binding.route.provider_id:
            raise ValueError("live inference report belongs to another route")
        cases = report.get("cases")
        if not isinstance(cases, list):
            raise ValueError("live inference report cases are malformed")
        by_name = {item.get("case"): item for item in cases
                   if isinstance(item, dict)}
        if len(by_name) != len(cases) \
                or not {"non_stream", "stream"} <= set(by_name):
            raise ValueError("live inference report lacks unique stream cases")
        specs = []
        for report_name, scenario in (("non_stream", "nonstream"),
                                      ("stream", "stream")):
            item = by_name[report_name]
            output_hash = _sha256(item.get("output_sha256"), "output hash")
            expected_hash = _sha256(
                item.get("expected_output_sha256"), "expected output hash")
            passed = item.get("passed") is True
            specs.append((
                scenario, passed,
                (f"live-output-{output_hash}",
                 f"expected-output-{expected_hash}"),
                () if passed else ("live_content_mismatch",),
            ))
        cancellation = report.get("cancellation")
        if not isinstance(cancellation, dict) \
                or not isinstance(cancellation.get("status"), str):
            raise ValueError("live inference report lacks cancellation evidence")
        cancellation_passed = cancellation.get("passed") is True \
            and cancellation.get("cancellation_observed") is True \
            and cancellation.get("task_reaped") is True
        specs.append((
            "cancellation", cancellation_passed,
            (f"live-cancellation-{cancellation['status']}",),
            () if cancellation_passed else ("live_cancellation_failed",),
        ))
        # Validate the entire report before the first durable evidence write.
        evidence = tuple(InferenceAliasParityEvidence(
            binding.route.route_id, scenario, source_commit, observed_at_ms,
            passed, refs, failures)
            for scenario, passed, refs, failures in specs)
        return tuple(self.parity.record(item) for item in evidence)

    def record_scenario(
        self, binding: InferenceAliasCanaryBinding, *, scenario: str,
        source_commit: str, observed_at_ms: int, passed: bool,
        evidence_refs: tuple[str, ...], failure_codes: tuple[str, ...] = (),
    ) -> InferenceAliasParityEvidence:
        """Persist a payload-free externally observed scenario for this route."""
        evidence = InferenceAliasParityEvidence(
            binding.route.route_id, scenario, source_commit, observed_at_ms,
            passed, evidence_refs, failure_codes)
        return self.parity.record(evidence)

    def rehearse_rollback(
        self, binding: InferenceAliasCanaryBinding, *, source_commit: str,
        observed_at_ms: int,
    ) -> InferenceAliasParityEvidence:
        """Prove exact rollback against cloned stores, retaining the live route."""
        for path in (self.paths.packages, self.paths.routes):
            if not path.is_file():
                raise RuntimeError("canary store is missing before rollback rehearsal")
        with tempfile.TemporaryDirectory(prefix="vera-alias-rollback-") as root:
            clone_paths = InferenceAliasCanaryPaths.under(root)
            shutil.copy2(self.paths.packages, clone_paths.packages)
            shutil.copy2(self.paths.routes, clone_paths.routes)
            clone = InferenceAliasCanarySession(clone_paths)
            clone.routes.rollback(
                binding.route_operation_id,
                operation_id="rollback-rehearsal.route")
            clone.packages.rollback(
                binding.activation_operation_id,
                operation_id="rollback-rehearsal.alias")
            route_absent = clone.routes.current(binding.route.alias) is None
            alias_absent = clone.packages.get(binding.route.alias) is None
        retained = self.routes.current(binding.route.alias) == binding.route \
            and self.packages.get(binding.route.alias) is not None
        passed = route_absent and alias_absent and retained
        return self.record_scenario(
            binding, scenario="rollback", source_commit=source_commit,
            observed_at_ms=observed_at_ms, passed=passed,
            evidence_refs=(
                f"clone-route-absent-{str(route_absent).lower()}",
                f"clone-alias-absent-{str(alias_absent).lower()}",
                f"source-route-retained-{str(retained).lower()}",
            ), failure_codes=() if passed else ("rollback_rehearsal_failed",))

    async def rehearse_provider_outage(
        self, binding: InferenceAliasCanaryBinding,
        request: InferenceRequest, *, provider: InferenceProvider,
        health: InferenceProviderHealth, source_commit: str,
        observed_at_ms: int,
    ) -> InferenceAliasParityEvidence:
        """Remove the exact provider and prove portable dispatch fails closed."""
        descriptor = self.providers.get(binding.route.provider_id)
        if descriptor is None or descriptor.revision != binding.route.provider_revision:
            raise RuntimeError("route provider is not at the bound revision")
        if provider.profile().provider_id != binding.route.provider_id \
                or health != descriptor.health:
            raise ValueError("outage restoration facts do not match the route")
        calls_before = getattr(provider, "calls", None)
        self.providers.remove(
            binding.route.provider_id,
            expected_revision=binding.route.provider_revision)
        failed_closed = False
        try:
            try:
                await execute_inference_alias_dispatch(
                    binding.route.alias, request, enabled=True,
                    retry_owner=binding.route.retry_owner,
                    packages=self.packages, routes=self.routes,
                    providers=self.providers, deployments=self.deployments,
                    as_of_ms=observed_at_ms)
            except (KeyError, InferenceContractConflict):
                failed_closed = True
        finally:
            restored = self.providers.register(
                provider, package_ids=descriptor.package_ids,
                placements=descriptor.placements, health=health)
        no_provider_call = calls_before is None \
            or getattr(provider, "calls", None) == calls_before
        restored_exactly = restored.revision == binding.route.provider_revision \
            and restored.health == health
        passed = failed_closed and no_provider_call and restored_exactly
        return self.record_scenario(
            binding, scenario="outage", source_commit=source_commit,
            observed_at_ms=observed_at_ms, passed=passed,
            evidence_refs=(
                f"portable-failed-closed-{str(failed_closed).lower()}",
                f"provider-not-called-{str(no_provider_call).lower()}",
                f"provider-restored-{str(restored_exactly).lower()}",
            ), failure_codes=() if passed else ("outage_rehearsal_failed",))

    def rollback(
        self, binding: InferenceAliasCanaryBinding, *, source_operation: str,
        source_commit: str, observed_at_ms: int,
    ) -> InferenceAliasParityEvidence:
        """Remove the exact route and alias, then journal verified rollback."""
        source_operation = _identifier(source_operation, "source operation")
        self.routes.rollback(
            binding.route_operation_id,
            operation_id=f"{source_operation}.route-rollback")
        self.packages.rollback(
            binding.activation_operation_id,
            operation_id=f"{source_operation}.alias-rollback")
        route_absent = self.routes.current(binding.route.alias) is None
        alias_absent = self.packages.get(binding.route.alias) is None
        passed = route_absent and alias_absent
        evidence = InferenceAliasParityEvidence(
            binding.route.route_id, "rollback", source_commit,
            observed_at_ms, passed,
            (f"route-absent-{str(route_absent).lower()}",
             f"alias-absent-{str(alias_absent).lower()}"),
            () if passed else ("rollback_incomplete",))
        return self.parity.record(evidence)

    def readiness(
        self, route: InferenceAliasRoute, *, source_commit: str,
        as_of_ms: int, max_age_ms: int,
    ) -> InferenceAliasReadiness:
        return evaluate_inference_alias_readiness(
            route, self.parity, source_commit=source_commit,
            as_of_ms=as_of_ms, max_age_ms=max_age_ms)
