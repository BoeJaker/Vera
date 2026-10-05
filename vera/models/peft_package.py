"""Deterministic PEFT-derived ModelPackage proposals without PEFT execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from .model_package import (
    ModelArtifact, ModelPackage, _identifier, _string)
from .training_contracts import EvaluationReport, TrainingRun


PEFT_PROPOSAL_SCHEMA = "vera.peft-package-proposal/v1"
PEFT_RECEIPT_SCHEMA = "vera.peft-training-receipt/v1"
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_METHODS = {"lora", "adalora", "ia3"}
_TASK_TYPES = {
    "causal_lm", "seq2seq_lm", "sequence_classification",
    "token_classification", "feature_extraction",
}
_PRECISIONS = {"fp32", "fp16", "bf16"}
_ARTIFACT_ROLES = {
    "adapter.weights", "adapter.config", "adapter.modules_to_save",
    "adapter.tokenizer",
}
_TASK_COMPATIBILITY = {
    "causal_lm": "generate",
    "seq2seq_lm": "generate",
    "sequence_classification": "classify",
    "token_classification": "token_classification",
    "feature_extraction": "embed",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _finite_number(value: Any, label: str, *, minimum: float,
                   maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if (not math.isfinite(result) or result < minimum or
            (maximum is not None and result > maximum)):
        raise ValueError(f"{label} is outside its supported range")
    return result


@dataclass(frozen=True)
class PeftMethodConfig:
    method: str
    task_type: str
    target_modules: tuple[str, ...]
    rank: int | None = None
    alpha: float | None = None
    dropout: float = 0.0
    modules_to_save: tuple[str, ...] = ()
    bias: str = "none"

    def __post_init__(self) -> None:
        method = _identifier(self.method, "PEFT method").lower()
        task_type = _identifier(self.task_type, "PEFT task type").lower()
        if method not in _METHODS:
            raise ValueError("unsupported PEFT method")
        if task_type not in _TASK_TYPES:
            raise ValueError("unsupported PEFT task type")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "task_type", task_type)
        if (isinstance(self.target_modules, (str, bytes)) or
                not isinstance(self.target_modules, Sequence) or
                isinstance(self.modules_to_save, (str, bytes)) or
                not isinstance(self.modules_to_save, Sequence)):
            raise TypeError("PEFT module selections must be sequences")
        targets = tuple(sorted({_identifier(item, "target module")
                                for item in self.target_modules}))
        saved = tuple(sorted({_identifier(item, "module to save")
                              for item in self.modules_to_save}))
        if not targets or len(targets) > 128 or len(saved) > 64:
            raise ValueError("PEFT module selections must be bounded and non-empty")
        object.__setattr__(self, "target_modules", targets)
        object.__setattr__(self, "modules_to_save", saved)
        if self.bias not in {"none", "all", "lora_only"}:
            raise ValueError("unsupported PEFT bias mode")
        dropout = _finite_number(
            self.dropout, "dropout", minimum=0, maximum=1)
        object.__setattr__(self, "dropout", dropout)
        if method in {"lora", "adalora"}:
            object.__setattr__(self, "rank", _positive_int(
                self.rank, "adapter rank", 65_536))
            alpha = _finite_number(
                self.alpha, "adapter alpha", minimum=float.fromhex("0x0.0000000000001p-1022"))
            object.__setattr__(self, "alpha", alpha)
        elif self.rank is not None or self.alpha is not None or dropout != 0:
            raise ValueError("IA3 does not accept rank, alpha, or dropout")
        if method == "ia3" and self.bias != "none":
            raise ValueError("IA3 does not accept a LoRA bias mode")

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "task_type": self.task_type,
            "target_modules": list(self.target_modules),
            "rank": self.rank,
            "alpha": self.alpha,
            "dropout": self.dropout,
            "modules_to_save": list(self.modules_to_save),
            "bias": self.bias,
        }


@dataclass(frozen=True)
class PeftPackageProposal:
    base_model_package_id: str
    base_source_revision: str
    dataset_revision_id: str
    runtime_profile_id: str
    method_config: PeftMethodConfig
    precision: str
    package: ModelPackage = field(repr=False)
    proposal_id: str = field(init=False)
    schema: str = PEFT_PROPOSAL_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_id", "peftp_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "base_model_package_id": self.base_model_package_id,
            "base_source_revision": self.base_source_revision,
            "dataset_revision_id": self.dataset_revision_id,
            "runtime_profile_id": self.runtime_profile_id,
            "method_config": self.method_config.to_dict(),
            "precision": self.precision,
            "derived_model_package_id": self.package.package_id,
            "training_run_id": self.package.training_run_id,
            "artifacts": [
                {"role": item.role, "sha256": "sha256:" + item.sha256,
                 "size_bytes": item.size_bytes}
                for item in self.package.artifacts
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            **self.identity_dict(),
            "status": "untrusted_proposal",
            "effect": "none",
            "peft_invoked": False,
            "artifact_downloaded": False,
            "registered": False,
            "alias_mutated": False,
            "deployed": False,
            "activation_authority": False,
        }


def propose_peft_package(
        *, base_package: ModelPackage, method_config: PeftMethodConfig,
        name: str, version: str, artifacts: Sequence[ModelArtifact],
        training_run_id: str, dataset_revision_id: str,
        runtime_profile_id: str, peft_version: str,
        transformers_version: str, precision: str,
        license: str,
        ) -> PeftPackageProposal:
    """Build a derived package proposal; never import PEFT or mutate a registry."""
    if not isinstance(base_package, ModelPackage):
        raise TypeError("base package must be a ModelPackage")
    if not isinstance(method_config, PeftMethodConfig):
        raise TypeError("method config must be a PeftMethodConfig")
    if not base_package.source_revision:
        raise ValueError("base package must declare an exact source revision")
    for item in base_package.artifacts:
        if isinstance(item.size_bytes, bool) or item.size_bytes <= 0:
            raise ValueError("base package artifacts must have positive verified sizes")
    adapter_artifacts = tuple(artifacts)
    if (not adapter_artifacts or len(adapter_artifacts) > 16 or
            not all(isinstance(item, ModelArtifact) for item in adapter_artifacts)):
        raise ValueError("adapter artifacts must contain 1..16 ModelArtifact values")
    roles = {item.role for item in adapter_artifacts}
    if not {"adapter.weights", "adapter.config"}.issubset(roles):
        raise ValueError("adapter weights and config artifacts are required")
    if len(roles) != len(adapter_artifacts) or roles - _ARTIFACT_ROLES:
        raise ValueError("adapter artifact roles must be unique and supported")
    for item in adapter_artifacts:
        if isinstance(item.size_bytes, bool) or item.size_bytes <= 0:
            raise ValueError("adapter artifacts must have positive verified sizes")
    for value, label in ((peft_version, "PEFT version"),
                         (transformers_version, "Transformers version")):
        if not isinstance(value, str) or not _VERSION.fullmatch(value):
            raise ValueError(f"{label} must be a pinned release version")
    precision = _identifier(precision, "precision").lower()
    if precision not in _PRECISIONS:
        raise ValueError("unsupported adapter precision")
    training_run_id = _identifier(training_run_id, "training run ID")
    dataset_revision_id = _identifier(dataset_revision_id, "dataset revision ID")
    runtime_profile_id = _identifier(runtime_profile_id, "runtime profile ID")
    license = _string(license, "license", required=True, limit=1024)
    if not base_package.license or license != base_package.license:
        raise ValueError("derived package must preserve the base package license")
    required_task = _TASK_COMPATIBILITY[method_config.task_type]
    if required_task not in base_package.compatibility.tasks:
        raise ValueError("PEFT task type is incompatible with the base package")
    method_digest = _digest(method_config.to_dict())
    derivation = {
        "base_model_package_id": base_package.package_id,
        "base_source_revision": base_package.source_revision,
        "dataset_revision_id": dataset_revision_id,
        "runtime_profile_id": runtime_profile_id,
        "method_config_digest": method_digest,
        "peft_version": peft_version,
        "transformers_version": transformers_version,
        "precision": precision,
        "training_run_id": training_run_id,
        "artifact_digests": {item.role: "sha256:" + item.sha256
                             for item in adapter_artifacts},
    }
    package = ModelPackage(
        name=name, version=version,
        architecture=f"{base_package.architecture}+peft-{method_config.method}",
        format="safetensors", artifacts=adapter_artifacts,
        compatibility=base_package.compatibility,
        framework="peft", framework_version=peft_version,
        tokenizer=base_package.tokenizer,
        preprocessing=base_package.preprocessing,
        source_uri=f"peft://{base_package.package_id}/{training_run_id}",
        source_revision=_digest(derivation), license=license,
        training_run_id=training_run_id,
        evaluation_report_ids=(),
        metadata=tuple(sorted({
            "base_model_package_id": base_package.package_id,
            "base_source_revision": base_package.source_revision,
            "dataset_revision_id": dataset_revision_id,
            "runtime_profile_id": runtime_profile_id,
            "peft_method": method_config.method,
            "method_config_digest": method_digest,
            "peft_version": peft_version,
            "transformers_version": transformers_version,
            "precision": precision,
        }.items())))
    return PeftPackageProposal(
        base_package.package_id, base_package.source_revision,
        dataset_revision_id, runtime_profile_id, method_config, precision,
        package)


@dataclass(frozen=True)
class PeftTrainingReceipt:
    proposal_id: str
    training_run_id: str
    derived_model_package_id: str
    evaluation_report_ids: tuple[str, ...]
    receipt_id: str = field(init=False)
    schema: str = PEFT_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", "peftr_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "training_run_id": self.training_run_id,
            "derived_model_package_id": self.derived_model_package_id,
            "evaluation_report_ids": list(self.evaluation_report_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            **self.identity_dict(),
            "verified": True,
            "effect": "none",
            "registered": False,
            "alias_mutated": False,
            "deployed": False,
            "activation_authority": False,
        }


def verify_peft_training_result(
        proposal: PeftPackageProposal, training_run: TrainingRun,
        evaluations: Sequence[EvaluationReport]) -> PeftTrainingReceipt:
    """Verify exact terminal evidence without registering or activating output."""
    if not isinstance(proposal, PeftPackageProposal):
        raise TypeError("proposal must be a PeftPackageProposal")
    if not isinstance(training_run, TrainingRun):
        raise TypeError("training run must be a TrainingRun")
    if training_run.status != "succeeded":
        raise ValueError("PEFT package verification requires succeeded training")
    package = proposal.package
    if training_run.training_run_id != package.training_run_id:
        raise ValueError("training run identity does not match proposal")
    if training_run.output_model_package_id != package.package_id:
        raise ValueError("training output package does not match proposal")
    if training_run.request.base_model_package_id != proposal.base_model_package_id:
        raise ValueError("training base package does not match proposal")
    if training_run.request.dataset_revision_id != proposal.dataset_revision_id:
        raise ValueError("training dataset revision does not match proposal")
    if training_run.runtime_id != proposal.runtime_profile_id:
        raise ValueError("training runtime does not match proposal")
    if training_run.request.objective not in {"fine_tune", "adapter_train", "peft"}:
        raise ValueError("training objective is not a PEFT-compatible objective")
    if training_run.attempts < 1:
        raise ValueError("succeeded training must record at least one attempt")
    reports = tuple(evaluations)
    if not reports or len(reports) > 128 or not all(
            isinstance(item, EvaluationReport) for item in reports):
        raise ValueError("bounded evaluation reports are required")
    by_id = {item.evaluation_report_id: item for item in reports}
    expected_reports = tuple(sorted(by_id))
    if len(by_id) != len(reports):
        raise ValueError("evaluation report identities must be unique")
    if training_run.evaluation_report_ids != expected_reports:
        raise ValueError("training evaluation identities do not match evidence")
    for report in reports:
        if not report.passed:
            raise ValueError("PEFT evaluation evidence must pass")
        request = report.request
        if (request.subject_kind != "model_package" or
                request.subject_id != package.package_id or
                request.dataset_revision_id != proposal.dataset_revision_id):
            raise ValueError("evaluation evidence is not bound to the derived package")
    return PeftTrainingReceipt(
        proposal.proposal_id, training_run.training_run_id, package.package_id,
        expected_reports)
