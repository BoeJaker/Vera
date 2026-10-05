"""Offline, SDK-neutral interchange records for MLflow interoperability.

The module deliberately does not import MLflow.  It prepares bounded,
payload-safe export records and validates already-produced candidate projections.
Vera identities remain authoritative and imported candidates remain proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from ..execution.run_protocol import ArtifactRef, Run
from .model_package import (
    ModelPackage, _identifier, _string, model_package_from_dict)
from .training_contracts import (
    EvaluationReport, PromptPackage, TrainingRun,
    prompt_package_from_dict)


MLFLOW_EXPORT_SCHEMA = "vera.mlflow-export-plan/v1"
MLFLOW_CANDIDATE_SCHEMA = "vera.mlflow-candidate-projection/v1"
MLFLOW_IMPORT_SCHEMA = "vera.mlflow-import-proposal/v1"
MAX_EXPORT_ENTITIES = 1_000
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_IMPORT_KEYS = {
    "schema", "source_revision", "mlflow_run_id", "candidate_kind",
    "candidate", "candidate_digest", "artifact_digests",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256(value: Any, label: str) -> str:
    digest = str(value or "").lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} must be a canonical SHA-256 digest")
    return "sha256:" + digest.removeprefix("sha256:")


def _scalars(value: Mapping[str, Any], label: str,
             *, limit: int = 128) -> dict[str, Any]:
    if not isinstance(value, Mapping) or len(value) > limit:
        raise ValueError(f"{label} must be a bounded mapping")
    out: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = _identifier(raw_key, f"{label} key")
        if item is None or isinstance(item, bool):
            normalized = item
        elif isinstance(item, int):
            normalized = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{label} values must be finite")
            normalized = item
        elif isinstance(item, str):
            normalized = _string(item, f"{label} value", limit=2_048)
        else:
            raise TypeError(f"{label} values must be JSON scalars")
        out[key] = normalized
    return dict(sorted(out.items()))


def _artifact_projection(value: ArtifactRef) -> dict[str, Any]:
    if not isinstance(value, ArtifactRef):
        raise TypeError("run artifacts must contain ArtifactRef values")
    checksum = _sha256(value.checksum, "artifact checksum")
    return {
        "artifact_id": _identifier(value.id, "artifact ID"),
        "kind": _identifier(value.kind, "artifact kind"),
        "uri_digest": _digest(value.uri),
        "checksum": checksum,
        "media_type": _string(value.media_type, "artifact media type", limit=255),
        "size_bytes": value.size_bytes,
    }


def _optional_identifier(value: Any, label: str) -> str:
    return _identifier(value, label) if str(value or "").strip() else ""


def _run_projection(value: Run) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Run):
        raise TypeError("runs must contain Run values")
    projection = {
        "protocol": "vera.run.v1",
        "run_id": _identifier(value.id, "run ID"),
        "kind": _identifier(value.kind, "run kind"),
        "status": value.status.value,
        "parent_run_id": _optional_identifier(value.parent_run_id, "parent run ID"),
        "workflow_id": _optional_identifier(value.workflow_id, "workflow ID"),
        "task_id": _optional_identifier(value.task_id, "task ID"),
        "attempt": value.attempt,
        "created_at": _string(value.created_at, "created timestamp", limit=128),
        "started_at": _string(value.started_at, "started timestamp", limit=128),
        "ended_at": _string(value.ended_at, "ended timestamp", limit=128),
        "progress": value.progress,
        "usage": _scalars(value.usage, "run usage"),
        "cost": _scalars(value.cost, "run cost"),
        "policy_keys": sorted(_identifier(key, "policy key") for key in value.policy),
        "retry_owner": _optional_identifier(value.retry_owner, "retry owner"),
        "error_code": (_identifier(value.error.code, "error code")
                       if value.error else ""),
        "artifacts": [_artifact_projection(item) for item in value.artifacts],
        "event_count": len(value.events),
    }
    return projection["run_id"], projection


def _evaluation_projection(
        value: EvaluationReport) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, EvaluationReport):
        raise TypeError("evaluations must contain EvaluationReport values")
    projection = value.to_dict()
    return value.evaluation_report_id, projection


def _prompt_projection(value: PromptPackage) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, PromptPackage):
        raise TypeError("prompts must contain PromptPackage values")
    projection = {
        "schema": value.schema,
        "prompt_package_id": value.prompt_package_id,
        "name": value.name,
        "version": value.version,
        "messages": [{"role": item.role,
                       "template_digest": _digest(item.template),
                       "template_bytes": len(item.template.encode("utf-8"))}
                      for item in value.messages],
        "variables": list(value.variables),
        "output_contract": value.output_contract,
        "metadata_keys": [key for key, _ in value.metadata],
    }
    return value.prompt_package_id, projection


def _model_projection(value: ModelPackage) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, ModelPackage):
        raise TypeError("models must contain ModelPackage values")
    projection = {
        "schema": value.schema,
        "package_id": value.package_id,
        "name": value.name,
        "version": value.version,
        "architecture": value.architecture,
        "format": value.format,
        "artifacts": [{"role": item.role,
                       "uri_digest": _digest(item.uri),
                       "sha256": "sha256:" + item.sha256,
                       "size_bytes": item.size_bytes}
                      for item in value.artifacts],
        "compatibility": value.compatibility.to_dict(),
        "framework": value.framework,
        "framework_version": value.framework_version,
        "opset": value.opset,
        "tokenizer_digest": _digest(value.tokenizer),
        "preprocessing_digest": _digest(value.preprocessing),
        "source_uri_digest": _digest(value.source_uri),
        "source_revision": _optional_identifier(
            value.source_revision, "model source revision"),
        "license": value.license,
        "training_run_id": value.training_run_id,
        "evaluation_report_ids": list(value.evaluation_report_ids),
        "metadata_keys": [key for key, _ in value.metadata],
    }
    return value.package_id, projection


def _training_projection(value: TrainingRun) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, TrainingRun):
        raise TypeError("training runs must contain TrainingRun values")
    projection = value.to_dict()
    return value.training_run_id, projection


@dataclass(frozen=True)
class MlflowExportEntity:
    kind: str
    vera_id: str
    mlflow_type: str
    identity_tag: str
    record_digest: str
    projection_json: str = field(repr=False)

    @property
    def projection(self) -> dict[str, Any]:
        return json.loads(self.projection_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "vera_id": self.vera_id,
            "mlflow_type": self.mlflow_type,
            "identity_tag": self.identity_tag,
            "record_digest": self.record_digest,
            "projection": self.projection,
        }


@dataclass(frozen=True)
class MlflowExportPlan:
    source_revision: str
    experiment_name: str
    entities: tuple[MlflowExportEntity, ...]
    plan_id: str = field(init=False)
    schema: str = MLFLOW_EXPORT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_revision", _identifier(
            self.source_revision, "source revision"))
        object.__setattr__(self, "experiment_name", _identifier(
            self.experiment_name, "experiment name"))
        if not self.entities or len(self.entities) > MAX_EXPORT_ENTITIES:
            raise ValueError("export plan must contain 1..1000 entities")
        keys = {(item.kind, item.vera_id) for item in self.entities}
        if len(keys) != len(self.entities):
            raise ValueError("export entity identities must be unique")
        object.__setattr__(self, "plan_id", "mlfx_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_revision": self.source_revision,
            "experiment_name": self.experiment_name,
            "entities": [item.to_dict() for item in self.entities],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, **self.identity_dict(),
            "effect": "none", "mlflow_invoked": False,
            "payloads_retained": False, "activation_authority": False,
        }


def _entity(kind: str, value: Any) -> MlflowExportEntity:
    handlers = {
        "run": (_run_projection, "run", "vera.run.id"),
        "evaluation_report": (
            _evaluation_projection, "metric", "vera.evaluation_report.id"),
        "prompt_package": (
            _prompt_projection, "prompt", "vera.prompt_package.id"),
        "model_package": (
            _model_projection, "model", "vera.model_package.id"),
        "training_run": (
            _training_projection, "run", "vera.training_run.id"),
    }
    if kind not in handlers:
        raise ValueError("unsupported MLflow export entity kind")
    projector, mlflow_type, identity_tag = handlers[kind]
    vera_id, projection = projector(value)
    return MlflowExportEntity(
        kind, vera_id, mlflow_type, identity_tag, _digest(projection),
        _canonical(projection))


def plan_mlflow_export(*, source_revision: str, experiment_name: str,
                       runs: Sequence[Run] = (),
                       evaluations: Sequence[EvaluationReport] = (),
                       prompts: Sequence[PromptPackage] = (),
                       models: Sequence[ModelPackage] = (),
                       training_runs: Sequence[TrainingRun] = (),
                       ) -> MlflowExportPlan:
    """Build a deterministic plan; never imports or calls the MLflow SDK."""
    groups = (
        ("run", runs), ("evaluation_report", evaluations),
        ("prompt_package", prompts), ("model_package", models),
        ("training_run", training_runs),
    )
    for kind, values in groups:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TypeError(f"{kind} values must be a sequence")
    total = sum(len(values) for _, values in groups)
    if not 0 < total <= MAX_EXPORT_ENTITIES:
        raise ValueError("export plan must contain 1..1000 entities")
    entities = []
    for kind, values in groups:
        entities.extend(_entity(kind, value) for value in values)
    entities.sort(key=lambda item: (item.kind, item.vera_id))
    plan = MlflowExportPlan(source_revision, experiment_name, tuple(entities))
    if len(_canonical(plan.to_dict()).encode("utf-8")) > MAX_EXPORT_BYTES:
        raise ValueError("MLflow export plan exceeds byte limit")
    return plan


@dataclass(frozen=True)
class MlflowImportProposal:
    source_revision: str
    mlflow_run_id: str
    candidate_kind: str
    candidate_id: str
    candidate_digest: str
    artifact_digests: tuple[tuple[str, str], ...]
    candidate: ModelPackage | PromptPackage = field(repr=False)
    proposal_id: str = field(init=False)
    schema: str = MLFLOW_IMPORT_SCHEMA

    def __post_init__(self) -> None:
        identity = self.identity_dict()
        object.__setattr__(self, "proposal_id", "mlfi_" + _digest(
            identity).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "source_revision": self.source_revision,
            "mlflow_run_id": self.mlflow_run_id,
            "candidate_kind": self.candidate_kind,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "artifact_digests": dict(self.artifact_digests),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id, **self.identity_dict(),
            "status": "untrusted_proposal", "effect": "none",
            "mlflow_invoked": False, "artifact_downloaded": False,
            "registered": False, "alias_mutated": False,
            "activation_authority": False,
        }


def import_mlflow_candidate(
        projection: Mapping[str, Any]) -> MlflowImportProposal:
    """Validate a pinned projection without fetching, registering, or activating."""
    if not isinstance(projection, Mapping):
        raise TypeError("MLflow candidate projection must be an object")
    unknown = set(projection) - _IMPORT_KEYS
    if unknown:
        raise ValueError(f"projection contains unsupported fields: {sorted(unknown)}")
    encoded = _canonical(projection).encode("utf-8")
    if len(encoded) > MAX_CANDIDATE_BYTES:
        raise ValueError("MLflow candidate projection exceeds byte limit")
    if projection.get("schema") != MLFLOW_CANDIDATE_SCHEMA:
        raise ValueError("unsupported MLflow candidate projection schema")
    source_revision = _identifier(
        projection.get("source_revision"), "source revision")
    mlflow_run_id = _identifier(projection.get("mlflow_run_id"), "MLflow run ID")
    kind = projection.get("candidate_kind")
    candidate_value = projection.get("candidate")
    if not isinstance(candidate_value, Mapping):
        raise ValueError("candidate must be an object")
    expected_digest = _sha256(
        projection.get("candidate_digest"), "candidate digest")
    actual_digest = _digest(candidate_value)
    if expected_digest != actual_digest:
        raise ValueError("candidate digest does not match candidate content")
    raw_artifacts = projection.get("artifact_digests") or {}
    if not isinstance(raw_artifacts, Mapping) or len(raw_artifacts) > 64:
        raise ValueError("artifact digests must be a bounded mapping")
    artifacts = tuple(sorted(
        (_identifier(role, "artifact role"), _sha256(digest, "artifact digest"))
        for role, digest in raw_artifacts.items()))
    if len({role for role, _ in artifacts}) != len(artifacts):
        raise ValueError("artifact digest roles must be unique")
    if kind == "model_package":
        candidate = model_package_from_dict(candidate_value)
        if dict(candidate_value) != candidate.to_dict():
            raise ValueError("model candidate must use its canonical schema")
        expected_artifacts = tuple(sorted(
            (item.role, "sha256:" + item.sha256) for item in candidate.artifacts))
        if artifacts != expected_artifacts:
            raise ValueError("artifact digest evidence does not match ModelPackage")
        candidate_id = candidate.package_id
    elif kind == "prompt_package":
        candidate = prompt_package_from_dict(candidate_value)
        if dict(candidate_value) != candidate.to_dict():
            raise ValueError("prompt candidate must use its canonical schema")
        if artifacts:
            raise ValueError("prompt candidates cannot claim model artifacts")
        candidate_id = candidate.prompt_package_id
    else:
        raise ValueError("unsupported MLflow candidate kind")
    return MlflowImportProposal(
        source_revision, mlflow_run_id, kind, candidate_id,
        actual_digest, artifacts, candidate)
