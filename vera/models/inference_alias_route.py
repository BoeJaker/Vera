"""Durable, non-executing bindings from model aliases to portable routes."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .admission import ModelAdmissionReceipt
from .inference_contracts import InferenceContractConflict
from .inference_deployment import InferenceDeployment, InferenceDeploymentObservation
from .inference_dispatch import InferenceDispatchPlan, InferenceDispatchPolicy
from .model_package import _identifier
from .model_package_store import ModelActivationReceipt

INFERENCE_ALIAS_ROUTE_SCHEMA = "vera.inference-alias-route/v2"


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return "iaroute_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class InferenceAliasRoute:
    alias: str
    package_id: str
    activation_operation_id: str
    admission_id: str
    deployment_id: str
    deployment_revision: int
    observation_id: str
    provider_id: str
    provider_revision: int
    health_evidence_id: str
    sample_dispatch_plan_id: str
    dispatch_policy: InferenceDispatchPolicy
    retry_owner: str
    route_id: str = field(init=False)
    schema: str = INFERENCE_ALIAS_ROUTE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INFERENCE_ALIAS_ROUTE_SCHEMA:
            raise ValueError("unsupported inference alias route schema")
        for name in (
                "alias", "package_id", "activation_operation_id", "admission_id",
                "deployment_id", "observation_id", "provider_id",
                "health_evidence_id", "sample_dispatch_plan_id",
                "retry_owner"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if not isinstance(self.dispatch_policy, InferenceDispatchPolicy):
            raise TypeError("dispatch_policy must be InferenceDispatchPolicy")
        for name in ("deployment_revision", "provider_revision"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be positive")
        object.__setattr__(self, "route_id", _hash(self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "alias": self.alias,
            "package_id": self.package_id,
            "activation_operation_id": self.activation_operation_id,
            "admission_id": self.admission_id,
            "deployment_id": self.deployment_id,
            "deployment_revision": self.deployment_revision,
            "observation_id": self.observation_id,
            "provider_id": self.provider_id,
            "provider_revision": self.provider_revision,
            "health_evidence_id": self.health_evidence_id,
            "sample_dispatch_plan_id": self.sample_dispatch_plan_id,
            "dispatch_policy": self.dispatch_policy.to_dict(),
            "retry_owner": self.retry_owner,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"route_id": self.route_id, **self.identity_dict()}


def define_inference_alias_route(
        alias: str, activation: ModelActivationReceipt,
        admission: ModelAdmissionReceipt, deployment: InferenceDeployment,
        observation: InferenceDeploymentObservation,
        dispatch: InferenceDispatchPlan) -> InferenceAliasRoute:
    """Join exact existing evidence without resolving or invoking a provider."""
    alias = _identifier(alias, "alias")
    if not isinstance(activation, ModelActivationReceipt) \
            or activation.kind != "activate" or activation.alias != alias:
        raise InferenceContractConflict("route requires the alias activation receipt")
    if not isinstance(admission, ModelAdmissionReceipt) or not admission.accepted:
        raise InferenceContractConflict("route requires accepted admission evidence")
    if not isinstance(deployment, InferenceDeployment):
        raise TypeError("deployment must be InferenceDeployment")
    if not isinstance(observation, InferenceDeploymentObservation):
        raise TypeError("observation must be InferenceDeploymentObservation")
    if not isinstance(dispatch, InferenceDispatchPlan):
        raise TypeError("dispatch must be InferenceDispatchPlan")
    package_id = activation.package_id
    if admission.package_id != package_id or deployment.package_id != package_id \
            or dispatch.model_package_id != package_id:
        raise InferenceContractConflict("route evidence belongs to another model package")
    if deployment.admission_id != admission.admission_id:
        raise InferenceContractConflict("deployment does not bind the admission evidence")
    if observation.deployment_id != deployment.deployment_id \
            or dispatch.deployment_id != deployment.deployment_id:
        raise InferenceContractConflict("route evidence belongs to another deployment")
    if observation.desired_state != "active" or observation.observed_state != "ready" \
            or observation.health is None:
        raise InferenceContractConflict("route requires an active ready deployment")
    health = observation.health
    if deployment.provider_id != dispatch.provider_id \
            or health.provider_id != dispatch.provider_id:
        raise InferenceContractConflict("route evidence belongs to another provider")
    if observation.revision != dispatch.deployment_revision \
            or health.evidence_id != dispatch.health_evidence_id:
        raise InferenceContractConflict("dispatch does not bind the deployment observation")
    if deployment.retry_owner != dispatch.retry_owner:
        raise InferenceContractConflict("dispatch changes deployment retry ownership")
    return InferenceAliasRoute(
        alias, package_id, activation.operation_id, admission.admission_id,
        deployment.deployment_id, observation.revision,
        observation.observation_id, dispatch.provider_id,
        dispatch.provider_revision, health.evidence_id, dispatch.plan_id,
        dispatch.policy, dispatch.retry_owner)


def inference_alias_route_from_dict(value: Mapping[str, Any]) -> InferenceAliasRoute:
    if not isinstance(value, Mapping) or value.get("schema") != INFERENCE_ALIAS_ROUTE_SCHEMA:
        raise ValueError("unsupported inference alias route schema")
    try:
        values = {
            key: value[key] for key in (
                "alias", "package_id", "activation_operation_id", "admission_id",
                "deployment_id", "deployment_revision", "observation_id",
                "provider_id", "provider_revision", "health_evidence_id",
                "sample_dispatch_plan_id", "retry_owner")}
        values["dispatch_policy"] = InferenceDispatchPolicy(
            **dict(value["dispatch_policy"]))
        route = InferenceAliasRoute(**values)
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference alias route") from exc
    if value.get("route_id") != route.route_id:
        raise ValueError("inference alias route identity does not match content")
    return route


class SQLiteInferenceAliasRouteRegistry:
    """CAS route selection and rollback journal; never dispatches inference."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inference_alias_routes (
                    route_id TEXT PRIMARY KEY, route_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inference_alias_route_current (
                    alias TEXT PRIMARY KEY, route_id TEXT NOT NULL,
                    FOREIGN KEY(route_id) REFERENCES inference_alias_routes(route_id));
                CREATE TABLE IF NOT EXISTS inference_alias_route_operations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ('bind','rollback')),
                    alias TEXT NOT NULL, previous_route_id TEXT NOT NULL,
                    route_id TEXT NOT NULL, reverted_operation_id TEXT UNIQUE);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def current(self, alias: str) -> InferenceAliasRoute | None:
        alias = _identifier(alias, "alias")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.route_json FROM inference_alias_route_current c "
                "JOIN inference_alias_routes r USING(route_id) WHERE c.alias=?",
                (alias,)).fetchone()
        return inference_alias_route_from_dict(json.loads(row[0])) if row else None

    def bind(self, route: InferenceAliasRoute, *, expected_route_id: str = "",
             operation_id: str) -> InferenceAliasRoute:
        if not isinstance(route, InferenceAliasRoute):
            raise TypeError("route must be InferenceAliasRoute")
        operation_id = _identifier(operation_id, "operation ID")
        expected_route_id = str(expected_route_id or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT kind,alias,previous_route_id,route_id FROM "
                "inference_alias_route_operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if prior:
                if prior != ("bind", route.alias, expected_route_id, route.route_id):
                    raise InferenceContractConflict(
                        "route operation ID was already used for another request")
                return route
            current = conn.execute(
                "SELECT route_id FROM inference_alias_route_current WHERE alias=?",
                (route.alias,)).fetchone()
            current_id = current[0] if current else ""
            if current_id != expected_route_id:
                raise InferenceContractConflict("route compare-and-set failed")
            encoded = json.dumps(route.to_dict(), sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT OR IGNORE INTO inference_alias_routes VALUES (?,?)",
                         (route.route_id, encoded))
            conn.execute(
                "INSERT INTO inference_alias_route_current VALUES (?,?) "
                "ON CONFLICT(alias) DO UPDATE SET route_id=excluded.route_id",
                (route.alias, route.route_id))
            conn.execute(
                "INSERT INTO inference_alias_route_operations "
                "(operation_id,kind,alias,previous_route_id,route_id,reverted_operation_id) "
                "VALUES (?,?,?,?,?,NULL)",
                (operation_id, "bind", route.alias, current_id, route.route_id))
        return route

    def rollback(self, bind_operation_id: str, *, operation_id: str) -> InferenceAliasRoute | None:
        bind_operation_id = _identifier(bind_operation_id, "bind operation ID")
        operation_id = _identifier(operation_id, "operation ID")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT kind,reverted_operation_id,alias,route_id FROM "
                "inference_alias_route_operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if prior:
                if prior[0:2] != ("rollback", bind_operation_id):
                    raise InferenceContractConflict(
                        "route operation ID was already used for another request")
                return self._route(conn, prior[3]) if prior[3] else None
            binding = conn.execute(
                "SELECT alias,previous_route_id,route_id FROM "
                "inference_alias_route_operations WHERE operation_id=? AND kind='bind'",
                (bind_operation_id,)).fetchone()
            if not binding:
                raise KeyError("route binding operation was not found")
            if conn.execute(
                    "SELECT 1 FROM inference_alias_route_operations "
                    "WHERE reverted_operation_id=?", (bind_operation_id,)).fetchone():
                raise InferenceContractConflict("route binding was already rolled back")
            alias, previous_id, bound_id = binding
            current = conn.execute(
                "SELECT route_id FROM inference_alias_route_current WHERE alias=?",
                (alias,)).fetchone()
            if not current or current[0] != bound_id:
                raise InferenceContractConflict("route rollback compare-and-set failed")
            if previous_id:
                conn.execute("UPDATE inference_alias_route_current SET route_id=? WHERE alias=?",
                             (previous_id, alias))
            else:
                conn.execute("DELETE FROM inference_alias_route_current WHERE alias=?", (alias,))
            conn.execute(
                "INSERT INTO inference_alias_route_operations "
                "(operation_id,kind,alias,previous_route_id,route_id,reverted_operation_id) "
                "VALUES (?,?,?,?,?,?)",
                (operation_id, "rollback", alias, bound_id, previous_id,
                 bind_operation_id))
            return self._route(conn, previous_id) if previous_id else None

    @staticmethod
    def _route(conn: sqlite3.Connection, route_id: str) -> InferenceAliasRoute:
        row = conn.execute("SELECT route_json FROM inference_alias_routes WHERE route_id=?",
                           (route_id,)).fetchone()
        if not row:
            raise RuntimeError("stored inference alias route is missing")
        return inference_alias_route_from_dict(json.loads(row[0]))
