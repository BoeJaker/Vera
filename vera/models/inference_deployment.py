"""Durable, non-executing lifecycle records for inference deployments."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
from collections.abc import Mapping
from typing import Any

from .admission import ModelAdmissionReceipt, evaluate_model_admission
from .inference_contracts import InferenceContractConflict
from .inference_health import (
    InferenceProviderHealth, inference_provider_health_from_dict)
from .model_package import ModelPackage, _identifier

INFERENCE_DEPLOYMENT_SCHEMA = "vera.inference-deployment/v1"
INFERENCE_DEPLOYMENT_OBSERVATION_SCHEMA = \
    "vera.inference-deployment-observation/v1"
_DESIRED_STATES = {"active", "stopped"}
_OBSERVED_WITH_HEALTH = {"ready", "degraded", "unavailable", "draining", "unknown"}
_OBSERVED_WITHOUT_HEALTH = {"pending", "stopped", "failed"}


def _hash(prefix: str, value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256(value: object, name: str) -> str:
    digest = str(value or "").lower().removeprefix("sha256:")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(f"{name} must be a canonical sha256 digest")
    return digest


@dataclass(frozen=True, slots=True)
class InferenceDeployment:
    package_id: str
    provider_id: str
    target_id: str
    admission_id: str
    runtime_kind: str
    runtime_version: str
    artifact_digests: tuple[tuple[str, str], ...]
    placements: tuple[str, ...]
    retry_owner: str
    deployment_id: str = field(init=False)
    schema: str = INFERENCE_DEPLOYMENT_SCHEMA

    def __post_init__(self) -> None:
        for name in ("package_id", "provider_id", "target_id", "admission_id",
                     "runtime_kind", "runtime_version", "retry_owner"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        try:
            artifacts = tuple(sorted((
                _identifier(role, "artifact role"),
                _sha256(digest, "artifact digest"))
                for role, digest in self.artifact_digests))
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact digests must be role/digest pairs") from exc
        if not artifacts or len(artifacts) > 256 \
                or len({role for role, _ in artifacts}) != len(artifacts):
            raise ValueError("artifact digests must be unique and bounded")
        object.__setattr__(self, "artifact_digests", artifacts)
        placements = tuple(sorted({_identifier(value, "placement")
                                   for value in self.placements}))
        if len(placements) > 64:
            raise ValueError("placements exceed their limit")
        object.__setattr__(self, "placements", placements)
        object.__setattr__(self, "deployment_id", _hash(
            "ideploy_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "package_id": self.package_id,
            "provider_id": self.provider_id, "target_id": self.target_id,
            "admission_id": self.admission_id,
            "runtime_kind": self.runtime_kind,
            "runtime_version": self.runtime_version,
            "artifact_digests": dict(self.artifact_digests),
            "placements": list(self.placements), "retry_owner": self.retry_owner,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"deployment_id": self.deployment_id, **self.identity_dict()}


def define_inference_deployment(
        package: ModelPackage, admission: ModelAdmissionReceipt, *,
        provider_id: str, runtime_kind: str, runtime_version: str,
        placements: tuple[str, ...] = (), retry_owner: str
        ) -> InferenceDeployment:
    """Bind admitted package facts without loading an artifact or runtime."""
    if not isinstance(package, ModelPackage):
        raise TypeError("package must be ModelPackage")
    if not isinstance(admission, ModelAdmissionReceipt):
        raise TypeError("admission must be ModelAdmissionReceipt")
    if not admission.accepted:
        raise InferenceContractConflict("deployment requires accepted admission")
    if admission.package_id != package.package_id:
        raise InferenceContractConflict("admission belongs to another package")
    expected_admission = evaluate_model_admission(
        package, admission.policy, admission.target)
    if expected_admission.admission_id != admission.admission_id:
        raise InferenceContractConflict(
            "admission evidence does not match package and target facts")
    artifacts = tuple((artifact.role, artifact.sha256)
                      for artifact in package.artifacts)
    return InferenceDeployment(
        package.package_id, provider_id, admission.target.target_id,
        admission.admission_id, runtime_kind, runtime_version, artifacts,
        placements, retry_owner)


def inference_deployment_from_dict(value: Mapping[str, Any]) -> InferenceDeployment:
    if not isinstance(value, Mapping) or value.get("schema") != INFERENCE_DEPLOYMENT_SCHEMA:
        raise ValueError("unsupported inference deployment schema")
    try:
        deployment = InferenceDeployment(
            package_id=value["package_id"], provider_id=value["provider_id"],
            target_id=value["target_id"], admission_id=value["admission_id"],
            runtime_kind=value["runtime_kind"],
            runtime_version=value["runtime_version"],
            artifact_digests=tuple(dict(value["artifact_digests"]).items()),
            placements=tuple(value.get("placements") or ()),
            retry_owner=value["retry_owner"])
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference deployment") from exc
    if value.get("deployment_id") != deployment.deployment_id:
        raise ValueError("inference deployment identity does not match content")
    return deployment


@dataclass(frozen=True, slots=True)
class InferenceDeploymentObservation:
    deployment_id: str
    revision: int
    desired_state: str
    observed_state: str
    observed_at_ms: int
    health: InferenceProviderHealth | None = None
    failure_code: str = ""
    observation_id: str = field(init=False)
    schema: str = INFERENCE_DEPLOYMENT_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_id", _identifier(
            self.deployment_id, "deployment ID"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) \
                or self.revision < 1:
            raise ValueError("deployment observation revision must be positive")
        if self.desired_state not in _DESIRED_STATES:
            raise ValueError("unsupported desired deployment state")
        if self.observed_state not in _OBSERVED_WITH_HEALTH | _OBSERVED_WITHOUT_HEALTH:
            raise ValueError("unsupported observed deployment state")
        if isinstance(self.observed_at_ms, bool) or not isinstance(
                self.observed_at_ms, int) or self.observed_at_ms < 0:
            raise ValueError("observed_at_ms must be a non-negative Unix millisecond")
        if self.health is not None:
            if not isinstance(self.health, InferenceProviderHealth):
                raise TypeError("health must be InferenceProviderHealth")
            if self.observed_state != self.health.state:
                raise ValueError("observed state does not match health evidence")
            if self.observed_at_ms != self.health.observed_at_ms:
                raise ValueError("observation time does not match health evidence")
        elif self.observed_state in _OBSERVED_WITH_HEALTH:
            raise ValueError("provider state requires health evidence")
        if self.failure_code:
            object.__setattr__(self, "failure_code", _identifier(
                self.failure_code, "deployment failure code"))
        if (self.observed_state == "failed") != bool(self.failure_code):
            raise ValueError("only failed deployment observations require a failure code")
        if self.desired_state == "stopped" and self.observed_state not in {
                "draining", "stopped", "failed", "unavailable", "unknown"}:
            raise ValueError("stopped deployment desire has an incoherent observed state")
        object.__setattr__(self, "observation_id", _hash(
            "idobs_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "deployment_id": self.deployment_id,
            "revision": self.revision, "desired_state": self.desired_state,
            "observed_state": self.observed_state,
            "observed_at_ms": self.observed_at_ms,
            "health": self.health.to_dict() if self.health else None,
            "failure_code": self.failure_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"observation_id": self.observation_id, **self.identity_dict()}


def inference_deployment_observation_from_dict(
        value: Mapping[str, Any]) -> InferenceDeploymentObservation:
    if not isinstance(value, Mapping) or value.get("schema") != \
            INFERENCE_DEPLOYMENT_OBSERVATION_SCHEMA:
        raise ValueError("unsupported inference deployment observation schema")
    try:
        health_value = value.get("health")
        observation = InferenceDeploymentObservation(
            deployment_id=value["deployment_id"], revision=value["revision"],
            desired_state=value["desired_state"],
            observed_state=value["observed_state"],
            observed_at_ms=value["observed_at_ms"],
            health=(inference_provider_health_from_dict(health_value)
                    if health_value is not None else None),
            failure_code=value.get("failure_code", ""))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference deployment observation") from exc
    if value.get("observation_id") != observation.observation_id:
        raise ValueError("deployment observation identity does not match content")
    return observation


class InferenceDeploymentStoreCorrupt(RuntimeError):
    pass


class SQLiteInferenceDeploymentRegistry:
    """Persist definitions and CAS observations; never execute deployments."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inference_deployments (
                    deployment_id TEXT PRIMARY KEY,
                    deployment_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inference_deployment_observations (
                    deployment_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    observation_id TEXT NOT NULL UNIQUE,
                    observation_json TEXT NOT NULL,
                    PRIMARY KEY(deployment_id, revision),
                    FOREIGN KEY(deployment_id)
                        REFERENCES inference_deployments(deployment_id));
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def register(self, deployment: InferenceDeployment) -> InferenceDeployment:
        if not isinstance(deployment, InferenceDeployment):
            raise TypeError("deployment must be InferenceDeployment")
        encoded = json.dumps(deployment.to_dict(), sort_keys=True,
                             separators=(",", ":"))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT deployment_json FROM inference_deployments "
                "WHERE deployment_id=?", (deployment.deployment_id,)).fetchone()
            if row and row[0] != encoded:
                raise InferenceContractConflict("deployment identity collision")
            conn.execute("INSERT OR IGNORE INTO inference_deployments VALUES (?,?)",
                         (deployment.deployment_id, encoded))
        return deployment

    def get(self, deployment_id: str) -> InferenceDeployment | None:
        deployment_id = _identifier(deployment_id, "deployment ID")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT deployment_json FROM inference_deployments "
                "WHERE deployment_id=?", (deployment_id,)).fetchone()
        return self._deployment(row[0]) if row else None

    def list(self) -> tuple[InferenceDeployment, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT deployment_json FROM inference_deployments "
                "ORDER BY deployment_id").fetchall()
        return tuple(self._deployment(row[0]) for row in rows)

    def observe(self, deployment_id: str, *, expected_revision: int,
                desired_state: str, observed_at_ms: int | None = None,
                health: InferenceProviderHealth | None = None,
                observed_state: str = "", failure_code: str = ""
                ) -> InferenceDeploymentObservation:
        deployment_id = _identifier(deployment_id, "deployment ID")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT deployment_json FROM inference_deployments "
                "WHERE deployment_id=?", (deployment_id,)).fetchone()
            if not row:
                raise KeyError("inference deployment is not registered")
            deployment = self._deployment(row[0])
            latest = conn.execute(
                "SELECT revision,observation_json FROM inference_deployment_observations "
                "WHERE deployment_id=? ORDER BY revision DESC LIMIT 1",
                (deployment_id,)).fetchone()
            current_revision = latest[0] if latest else 0
            if health is not None:
                if health.provider_id != deployment.provider_id:
                    raise InferenceContractConflict(
                        "health evidence belongs to another provider")
                if deployment.package_id not in health.available_package_ids \
                        and health.state == "ready":
                    raise InferenceContractConflict(
                        "ready health evidence omits the deployed package")
                if observed_state and observed_state != health.state:
                    raise InferenceContractConflict(
                        "observed state conflicts with health evidence")
                if observed_at_ms is not None \
                        and observed_at_ms != health.observed_at_ms:
                    raise InferenceContractConflict(
                        "observation time conflicts with health evidence")
                observed_state = health.state
                observed_at_ms = health.observed_at_ms
            elif not observed_state or observed_at_ms is None:
                raise ValueError(
                    "observation state and time are required without health evidence")
            observation = InferenceDeploymentObservation(
                deployment_id, expected_revision + 1, desired_state,
                observed_state, observed_at_ms, health, failure_code)
            if expected_revision != current_revision:
                if current_revision == observation.revision and latest \
                        and self._observation(latest[1]) == observation:
                    return observation
                raise InferenceContractConflict(
                    "deployment observation revision conflict")
            encoded = json.dumps(observation.to_dict(), sort_keys=True,
                                 separators=(",", ":"))
            conn.execute(
                "INSERT INTO inference_deployment_observations VALUES (?,?,?,?)",
                (deployment_id, observation.revision,
                 observation.observation_id, encoded))
        return observation

    def current(self, deployment_id: str) -> InferenceDeploymentObservation | None:
        deployment_id = _identifier(deployment_id, "deployment ID")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT observation_json FROM inference_deployment_observations "
                "WHERE deployment_id=? ORDER BY revision DESC LIMIT 1",
                (deployment_id,)).fetchone()
        return self._observation(row[0]) if row else None

    def history(self, deployment_id: str) -> tuple[InferenceDeploymentObservation, ...]:
        deployment_id = _identifier(deployment_id, "deployment ID")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT observation_json FROM inference_deployment_observations "
                "WHERE deployment_id=? ORDER BY revision", (deployment_id,)).fetchall()
        return tuple(self._observation(row[0]) for row in rows)

    @staticmethod
    def _deployment(encoded: str) -> InferenceDeployment:
        try:
            return inference_deployment_from_dict(json.loads(encoded))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InferenceDeploymentStoreCorrupt(
                "stored inference deployment is corrupt") from exc

    @staticmethod
    def _observation(encoded: str) -> InferenceDeploymentObservation:
        try:
            return inference_deployment_observation_from_dict(json.loads(encoded))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InferenceDeploymentStoreCorrupt(
                "stored deployment observation is corrupt") from exc
