"""Deterministic inventory of Vera's native Memory authority routes.

The inventory is deliberately source-bound and offline.  It identifies the
in-repository writers and consumers that must be migrated before authority can
move from ``MemoryRecord`` to ``RecordRevision``.  It does not import the
side-effectful Memory runtime, inspect stored records, or contact a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence


SCHEMA = "vera.memory-authority-inventory/v1"
MAX_SOURCE_BYTES = 3_000_000
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_KINDS = {"writer", "mutator", "reader", "stored_reference"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryAuthorityRoute:
    route: str
    source: str
    symbol: str
    kind: str
    operations: tuple[str, ...]
    identity: str
    notes: tuple[str, ...] = ()
    route_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(str(self.route or "")):
            raise ValueError("route must be a bounded identifier")
        if self.kind not in _KINDS:
            raise ValueError("unsupported memory route kind")
        if not self.source.startswith("vera/") or not self.source.endswith(".py"):
            raise ValueError("source must name a Vera Python module")
        if not str(self.symbol or "").strip():
            raise ValueError("symbol is required")
        operations = tuple(sorted(set(str(value).strip() for value in self.operations)))
        if not operations or any(not _NAME.fullmatch(value) for value in operations):
            raise ValueError("operations must be bounded identifiers")
        if not str(self.identity or "").strip():
            raise ValueError("identity is required")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "notes", tuple(sorted(set(self.notes))))
        object.__setattr__(self, "route_id", _identity("mari_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "source": self.source,
            "symbol": self.symbol,
            "kind": self.kind,
            "operations": list(self.operations),
            "identity": self.identity,
            "notes": list(self.notes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"route_id": self.route_id, **self.identity_dict()}


ROUTES = (
    MemoryAuthorityRoute(
        "memory.cap.store", "vera/fabric/memory.py", "memory_store", "writer",
        ("memory.store",), "new MemoryRecord.id",
        ("public_capability", "hybrid_fanout")),
    MemoryAuthorityRoute(
        "memory.cap.promote", "vera/fabric/memory.py", "memory_promote_cap", "writer",
        ("memory.promote",), "new MemoryRecord.id",
        ("public_capability", "hybrid_fanout")),
    MemoryAuthorityRoute(
        "memory.event.promoter", "vera/fabric/memory.py", "_maybe_promote", "writer",
        ("memory.store", "memory.promote", "llm.auto_promote"),
        "event-derived MemoryRecord.id", ("redis_event_consumer", "hybrid_fanout")),
    MemoryAuthorityRoute(
        "memory.hook.agent_turn", "vera/fabric/memory_hooks.py", "record_agent_turn",
        "writer", ("agent.human", "agent.response"), "deterministic turn MemoryRecord.id",
        ("two_native_records", "session_graph_links")),
    MemoryAuthorityRoute(
        "memory.hook.dag_execution", "vera/fabric/memory_hooks.py",
        "record_dag_execution", "writer", ("dag.root", "dag.step", "dag.trigger"),
        "deterministic DAG MemoryRecord.id", ("multiple_native_records", "graph_links")),
    MemoryAuthorityRoute(
        "memory.cap.lifecycle", "vera/fabric/memory.py", "memory_forget", "mutator",
        ("memory.forget",), "existing MemoryRecord.id",
        ("soft_delete", "backend_updates")),
    MemoryAuthorityRoute(
        "memory.cap.relationship", "vera/fabric/memory.py", "memory_relate", "mutator",
        ("memory.relate",), "source and target MemoryRecord.id",
        ("neo4j_relationship",)),
    MemoryAuthorityRoute(
        "memory.cap.read", "vera/fabric/memory.py", "memory_search/get/recall", "reader",
        ("memory.get", "memory.recall", "memory.search", "memory.session_history",
         "memory.similar", "memory.traverse"),
        "MemoryRecord.id returned from first-success/merged native reads",
        ("public_capabilities", "native_read_authority")),
    MemoryAuthorityRoute(
        "memory.hook.context", "vera/fabric/memory_hooks.py",
        "get_agent_memory_context/get_agent_memory_context_fast", "reader",
        ("memory.context",), "session-scoped native MemoryRecord.id",
        ("prompt_context_consumer", "fast_and_slow_paths")),
    MemoryAuthorityRoute(
        "memory.consumer.agents", "vera/agents/agents.py", "agent capability policy",
        "reader", ("memory.recall", "memory.search", "memory.store"),
        "capability name and returned MemoryRecord.id", ("tool_using_models",)),
    MemoryAuthorityRoute(
        "memory.consumer.delivery", "vera/delivery.py", "_build_memory", "writer",
        ("memory.store",), "capability result MemoryRecord.id", ("delivery_task_adapter",)),
    MemoryAuthorityRoute(
        "memory.consumer.chat", "vera/chat/chat_panels_capabilities.py",
        "chat memory capability policy", "reader", ("memory.recall", "memory.search"),
        "session-scoped returned MemoryRecord.id", ("chat_context",)),
    MemoryAuthorityRoute(
        "memory.consumer.project", "vera/dream/project_capabilities.py", "project_upsert",
        "stored_reference", ("memory.pin",), "project.memory_ids -> MemoryRecord.id",
        ("stored_definition", "must_remain_resolvable")),
)


SOURCE_ASSERTIONS = {
    "vera/fabric/memory.py": (
        "async def _maybe_promote(event: dict):",
        "async def memory_store(",
        "async def memory_promote_cap(",
        "async def memory_forget(",
        "async def memory_relate(",
        "result = await MEMORY.store(rec, producer_kind=\"public_capability\")",
        "async def memory_search(",
        "async def memory_get(",
        "async def memory_recall(",
    ),
    "vera/fabric/memory_hooks.py": (
        "async def record_agent_turn(",
        "await MEMORY.store(human_rec, producer_kind=\"conversation\")",
        "await MEMORY.store(ai_rec, producer_kind=\"conversation\")",
        "async def record_dag_execution(",
        "await MEMORY.store(dag_rec, producer_kind=\"workflow\")",
        "await MEMORY.store(step_rec, producer_kind=\"workflow\")",
        "async def get_agent_memory_context(",
        "async def get_agent_memory_context_fast(",
    ),
    "vera/agents/agents.py": ("\"memory.recall\", \"memory.search\", \"memory.session_history\", \"memory.store\"",),
    "vera/delivery.py": ('"memory.store":     _build_memory',),
    "vera/chat/chat_panels_capabilities.py": ('"memory.search", "memory.recall"',),
    "vera/dream/project_capabilities.py": ('"memory_ids":         kw.get("memory_ids", [])',),
}


def _source_evidence(repo_root: Path) -> dict[str, Any]:
    root = repo_root.resolve()
    result: dict[str, Any] = {}
    for relative, assertions in sorted(SOURCE_ASSERTIONS.items()):
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"source is unavailable: {relative}")
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError(f"source is too large: {relative}")
        text = raw.decode("utf-8")
        missing = [assertion for assertion in assertions if assertion not in text]
        if missing:
            raise ValueError(f"source assertions are missing from {relative}: {missing}")
        result[relative] = {
            "content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "assertion_digests": tuple(
                "sha256:" + hashlib.sha256(value.encode()).hexdigest()
                for value in assertions
            ),
        }
    return result


def build_memory_authority_inventory(
    repo_root: Path, routes: Sequence[MemoryAuthorityRoute] = ROUTES,
) -> dict[str, Any]:
    """Return the bounded in-repository migration inventory.

    External and dynamically generated callers are explicitly not inferred;
    runtime observation remains a separate pre-cutover gate.
    """
    ordered = tuple(sorted(routes, key=lambda item: item.route_id))
    if not ordered or len({item.route for item in ordered}) != len(ordered):
        raise ValueError("memory authority routes must be non-empty and unique")
    by_kind: dict[str, list[str]] = {}
    for item in ordered:
        by_kind.setdefault(item.kind, []).append(item.route)
    for values in by_kind.values():
        values.sort()
    payload = {
        "schema": SCHEMA,
        "routes": [item.to_dict() for item in ordered],
        "routes_by_kind": dict(sorted(by_kind.items())),
        "stored_identities": {
            "native_record": "MemoryRecord.id",
            "postgres": "vera_memories.id",
            "chroma": "MemoryRecord.id within model-scoped collection",
            "neo4j": "Memory.id",
            "canonical_record": "RecordRevision.record_id",
            "canonical_revision": "RecordRevision.revision_id",
            "portable_projection": "MemoryProjection.memory_id",
            "stored_project_reference": "project.memory_ids -> MemoryRecord.id",
        },
        "source_evidence": _source_evidence(Path(repo_root)),
        "coverage": {
            "in_repository_direct_routes": "complete_for_bound_sources",
            "stored_identity_shapes": "complete_for_declared_surfaces",
            "dynamic_python_callers": "not_observed",
            "external_consumers": "not_observed",
            "stored_record_instances": "not_read",
            "live_backends": "not_contacted",
        },
        "required_before_authority_switch": (
            "runtime_caller_observation",
            "stored_identity_cardinality_and_drift_scan",
            "external_consumer_evidence",
            "revision_bound_dual_write_receipts",
            "projection_reconciliation",
        ),
        "changes_write_path": False,
        "changes_read_path": False,
        "mutates": False,
    }
    return {"inventory_id": _identity("mai_", payload), **payload}
