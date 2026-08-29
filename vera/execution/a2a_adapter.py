"""Deterministic A2A client/server adapter plans; performs no network or SDK I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from Vera.vera.execution.a2a_mapping import (
    A2A_PROTOCOL_VERSION,
    A2ACardAnalysis,
    analyze_agent_card,
)


A2A_ADAPTER_SCHEMA = "vera.a2a-adapter-plan/v1"
_CLIENT_OPERATIONS = frozenset({
    "agentCard/get", "message/send", "message/stream", "tasks/get",
    "tasks/list", "tasks/cancel", "tasks/resubscribe",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _bounded(value: Any, name: str, maximum: int = 512) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


@dataclass(frozen=True)
class A2AClientPlan:
    operation: str
    endpoint_origin: str
    local_run_id: str
    canonical_task: str
    message_id: str
    auth_reference: str
    effects: tuple[str, ...]
    policy_decision: str
    remote_task_id: str = ""
    remote_context_id: str = ""
    schema: str = A2A_ADAPTER_SCHEMA
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.operation not in _CLIENT_OPERATIONS:
            raise ValueError("unsupported A2A client operation")
        for name in ("endpoint_origin", "local_run_id", "canonical_task",
                     "message_id", "auth_reference", "policy_decision"):
            object.__setattr__(self, name, _bounded(getattr(self, name), name))
        if not self.endpoint_origin.startswith("https://"):
            raise ValueError("A2A client endpoint must be HTTPS")
        if self.policy_decision != "allow_non_mutating":
            raise ValueError("only explicitly allowed non-mutating plans are supported")
        effects = tuple(sorted({_bounded(item, "effect", 128) for item in self.effects}))
        if effects != ("none",):
            raise ValueError("the first A2A task lane must declare effects=['none']")
        object.__setattr__(self, "effects", effects)
        if self.operation.startswith("tasks/") and not self.remote_task_id:
            raise ValueError("task operations require the server-assigned task id")
        identity = self.identity_dict()
        object.__setattr__(self, "plan_id", "a2aclient_" + hashlib.sha256(
            _canonical(identity).encode()).hexdigest())

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "operation": self.operation,
            "endpoint_origin": self.endpoint_origin,
            "local_run_id": self.local_run_id,
            "canonical_task": self.canonical_task,
            "message_id": self.message_id,
            "auth_reference": self.auth_reference,
            "effects": list(self.effects),
            "policy_decision": self.policy_decision,
            "remote_task_id": self.remote_task_id,
            "remote_context_id": self.remote_context_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, **self.identity_dict(),
            "transport_status": "queued_live",
            "credentials_resolved": False,
            "request_sent": False,
            "executes": False,
        }


@dataclass(frozen=True)
class A2AServerPlan:
    service_name: str
    service_version: str
    endpoint_origin: str
    exposed_tasks: tuple[str, ...]
    auth_schemes: tuple[str, ...]
    schema: str = A2A_ADAPTER_SCHEMA
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("service_name", "service_version", "endpoint_origin"):
            object.__setattr__(self, name, _bounded(getattr(self, name), name))
        if not self.endpoint_origin.startswith("https://"):
            raise ValueError("A2A server endpoint must be HTTPS")
        tasks = tuple(sorted({_bounded(item, "canonical task", 256)
                              for item in self.exposed_tasks}))
        schemes = tuple(sorted({_bounded(item, "auth scheme", 128)
                                for item in self.auth_schemes}))
        if not tasks or not schemes:
            raise ValueError("server plans require approved tasks and authentication")
        object.__setattr__(self, "exposed_tasks", tasks)
        object.__setattr__(self, "auth_schemes", schemes)
        object.__setattr__(self, "plan_id", "a2aserver_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode()).hexdigest())

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "protocol_version": A2A_PROTOCOL_VERSION,
            "service_name": self.service_name, "service_version": self.service_version,
            "endpoint_origin": self.endpoint_origin,
            "exposed_tasks": list(self.exposed_tasks),
            "auth_schemes": list(self.auth_schemes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, **self.identity_dict(),
            "listener_status": "queued_live", "listener_started": False,
            "registers_capabilities": False, "executes": False,
        }


def plan_non_mutating_task(
        card: Mapping[str, Any], *, skill_id: str, local_run_id: str,
        message_id: str, auth_reference: str,
) -> tuple[A2ACardAnalysis, A2AClientPlan]:
    """Validate a supplied card and plan one reviewed no-effect task without sending it."""
    analysis = analyze_agent_card(card)
    if not analysis.accepted:
        raise ValueError("Agent Card is not accepted: " + ", ".join(analysis.issues))
    matches = [item for item in analysis.projections if item.skill_id == skill_id]
    if len(matches) != 1:
        raise ValueError("skill_id must identify exactly one projected skill")
    projection = matches[0]
    plan = A2AClientPlan(
        operation="message/send", endpoint_origin=analysis.interface_url_origin,
        local_run_id=local_run_id, canonical_task=projection.canonical_task,
        message_id=message_id, auth_reference=auth_reference,
        effects=("none",), policy_decision="allow_non_mutating")
    return analysis, plan


def compile_a2a_adapter_status() -> dict[str, Any]:
    """Honest implementation/verification status for UI and conformance tooling."""
    return {
        "schema": A2A_ADAPTER_SCHEMA,
        "protocol_version": A2A_PROTOCOL_VERSION,
        "foundation": "implemented",
        "client_plan_contract": "implemented",
        "server_plan_contract": "implemented",
        "first_non_mutating_task_plan": "implemented",
        "client_transport": "queued_live",
        "server_listener": "queued_live",
        "authentication": "contract_only",
        "policy": "non_mutating_only",
        "artifact_verification": "queued_live",
        "cancel_resume": "contract_only_queued_live",
        "imports_runtime": False,
        "network_io": False,
        "executes": False,
    }
