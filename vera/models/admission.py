"""Deterministic, non-probing ModelPackage admission contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from .model_package import ModelPackage, _identifier, _string


ADMISSION_SCHEMA = "vera.model-admission/v1"


@dataclass(frozen=True)
class ModelDeploymentTarget:
    target_id: str
    task: str
    input_contract: str
    output_contract: str
    available_memory_bytes: int
    accelerators: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    max_opset: int | None = None

    def __post_init__(self) -> None:
        for name in ("target_id", "task", "input_contract", "output_contract"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if isinstance(self.available_memory_bytes, bool) or int(self.available_memory_bytes) < 0:
            raise ValueError("available_memory_bytes must be non-negative")
        object.__setattr__(self, "available_memory_bytes", int(self.available_memory_bytes))
        for name in ("accelerators", "frameworks"):
            object.__setattr__(self, name, tuple(sorted({
                _identifier(item, name) for item in getattr(self, name)})))
        if self.max_opset is not None:
            if isinstance(self.max_opset, bool) or int(self.max_opset) < 1:
                raise ValueError("max_opset must be positive")
            object.__setattr__(self, "max_opset", int(self.max_opset))

    def to_dict(self) -> dict:
        return {**self.__dict__, "accelerators": list(self.accelerators),
                "frameworks": list(self.frameworks)}


@dataclass(frozen=True)
class ModelTrustPolicy:
    policy_id: str
    require_signature: bool = False
    trusted_signatures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy ID"))
        if not isinstance(self.require_signature, bool):
            raise TypeError("require_signature must be boolean")
        object.__setattr__(self, "trusted_signatures", tuple(sorted({
            _string(item, "trusted signature", required=True, limit=4096)
            for item in self.trusted_signatures})))

    def to_dict(self) -> dict:
        return {**self.__dict__, "trusted_signatures": list(self.trusted_signatures)}


@dataclass(frozen=True)
class ModelAdmissionReceipt:
    package_id: str
    policy: ModelTrustPolicy
    target: ModelDeploymentTarget
    reasons: tuple[str, ...]
    admission_id: str = field(init=False)
    schema: str = ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "package_id", _identifier(self.package_id, "package ID"))
        if not isinstance(self.policy, ModelTrustPolicy):
            raise TypeError("policy must be ModelTrustPolicy")
        if not isinstance(self.target, ModelDeploymentTarget):
            raise TypeError("target must be ModelDeploymentTarget")
        reasons = tuple(sorted({_identifier(item, "admission reason")
                                for item in self.reasons}))
        object.__setattr__(self, "reasons", reasons)
        payload = self.evidence_dict()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "admission_id", "madm_" + hashlib.sha256(
            encoded.encode()).hexdigest())

    @property
    def accepted(self) -> bool:
        return not self.reasons

    def evidence_dict(self) -> dict:
        return {"schema": self.schema, "package_id": self.package_id,
                "policy": self.policy.to_dict(), "target": self.target.to_dict(),
                "reasons": list(self.reasons)}

    def to_dict(self) -> dict:
        return {"admission_id": self.admission_id, "accepted": self.accepted,
                **self.evidence_dict()}


class ModelPackageAdmissionRejected(ValueError):
    def __init__(self, receipt: ModelAdmissionReceipt) -> None:
        self.receipt = receipt
        super().__init__("model package admission rejected: " + ", ".join(receipt.reasons))


def evaluate_model_admission(
        package: ModelPackage, policy: ModelTrustPolicy,
        target: ModelDeploymentTarget) -> ModelAdmissionReceipt:
    """Compare declared package requirements to declared target facts only."""
    if not isinstance(package, ModelPackage):
        raise TypeError("package must be ModelPackage")
    reasons: list[str] = []
    compatibility = package.compatibility
    if target.task not in compatibility.tasks:
        reasons.append("task_unsupported")
    if target.input_contract != compatibility.input_contract:
        reasons.append("input_contract_mismatch")
    if target.output_contract != compatibility.output_contract:
        reasons.append("output_contract_mismatch")
    if target.available_memory_bytes < compatibility.min_memory_bytes:
        reasons.append("memory_insufficient")
    if not set(compatibility.accelerators).issubset(target.accelerators):
        reasons.append("accelerator_unavailable")
    if package.framework and package.framework not in target.frameworks:
        reasons.append("framework_unsupported")
    if package.opset is not None:
        if target.max_opset is None:
            reasons.append("opset_limit_unspecified")
        elif package.opset > target.max_opset:
            reasons.append("opset_unsupported")
    if policy.require_signature and not package.signature:
        reasons.append("signature_missing")
    elif (package.signature and (policy.require_signature or policy.trusted_signatures) and
          package.signature not in policy.trusted_signatures):
        reasons.append("signature_untrusted")
    return ModelAdmissionReceipt(package.package_id, policy, target, tuple(reasons))
