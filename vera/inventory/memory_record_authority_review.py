"""Source-bound review of Vera's overlapping Memory record authorities.

This module reads declarations only.  It does not import Memory, connect to a
backend, inspect stored content, embed text, or change a write/read path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence

SCHEMA = "vera.memory-record-authority-review/v1"
MAX_SOURCE_BYTES = 3_000_000
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_ROLES = {
    "native_record", "native_archive", "vector_projection",
    "graph_projection", "fanout_coordinator", "canonical_revision",
    "provider_projection", "compatibility_adapter", "session_cursor",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryAuthoritySurface:
    surface: str
    source: str
    role: str
    stores_content: bool
    accepts_writes: bool
    authority_state: str
    record_identity: str
    notes: tuple[str, ...] = ()
    surface_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(str(self.surface or "")):
            raise ValueError("surface must be a bounded identifier")
        if self.role not in _ROLES:
            raise ValueError("unsupported memory authority role")
        if self.authority_state not in {
            "current_native_claim", "derived_projection", "coordinator",
            "target_canonical", "compatibility", "ephemeral",
        }:
            raise ValueError("unsupported memory authority state")
        if not self.source.startswith("vera/") or not self.source.endswith(".py"):
            raise ValueError("source must name a Vera Python module")
        if not self.record_identity.strip():
            raise ValueError("record identity is required")
        object.__setattr__(self, "notes", tuple(sorted(set(self.notes))))
        object.__setattr__(self, "surface_id", _identity("mras_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface, "source": self.source, "role": self.role,
            "stores_content": self.stores_content, "accepts_writes": self.accepts_writes,
            "authority_state": self.authority_state,
            "record_identity": self.record_identity, "notes": list(self.notes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, **self.identity_dict()}


CURRENT_SURFACES = (
    MemoryAuthoritySurface(
        "memory.record", "vera/fabric/memory.py", "native_record", True, True,
        "current_native_claim", "MemoryRecord.id",
        ("mutable_record_shape", "compatibility_ingress")),
    MemoryAuthoritySurface(
        "memory.postgres", "vera/fabric/memory.py", "native_archive", True, True,
        "current_native_claim", "vera_memories.id",
        ("registered_first", "described_as_append_only_but_supports_update")),
    MemoryAuthoritySurface(
        "memory.chroma", "vera/fabric/memory.py", "vector_projection", True, True,
        "derived_projection", "MemoryRecord.id",
        ("embedding_model_scoped_collection", "rebuildable_from_postgres")),
    MemoryAuthoritySurface(
        "memory.neo4j", "vera/fabric/memory.py", "graph_projection", True, True,
        "derived_projection", "MemoryRecord.id",
        ("owns_graph_edges", "session_graph_read_surface")),
    MemoryAuthoritySurface(
        "memory.hybrid_store", "vera/fabric/memory.py", "fanout_coordinator", False,
        True, "coordinator", "delegated MemoryRecord.id",
        ("best_effort_fanout", "first_successful_backend_read")),
    MemoryAuthoritySurface(
        "fabric.record_revision", "vera/fabric/record_revision.py",
        "canonical_revision", True, True, "target_canonical",
        "RecordRevision.record_id plus revision_id",
        ("immutable_revision_identity", "content_hash_and_valid_time")),
    MemoryAuthoritySurface(
        "memory.provider_projection", "vera/fabric/memory_provider.py",
        "provider_projection", True, True, "derived_projection",
        "MemoryProjection.memory_id bound to record_id and revision_id",
        ("citation_required", "policy_filtered", "not_content_authority")),
    MemoryAuthoritySurface(
        "memory.native_adapter", "vera/fabric/native_memory_adapter.py",
        "compatibility_adapter", False, False, "compatibility",
        "trusted native id binding to RecordRevision",
        ("read_only", "payload_bounded", "receipt_available")),
    MemoryAuthoritySurface(
        "memory.session_cursors", "vera/fabric/memory_hooks.py", "session_cursor",
        False, True, "ephemeral", "session_id to last node id",
        ("causal_linking_only", "not_a_memory_record_store")),
)

SOURCE_ASSERTIONS = {
    "vera/fabric/memory.py": (
        "class MemoryRecord:", "class PostgresBackend(MemoryBackend):",
        "class ChromaBackend(MemoryBackend):", "class Neo4jBackend(MemoryBackend):",
        "class HybridMemoryStore:", "MEMORY.register(PostgresBackend())",
        "MEMORY.register(ChromaBackend())", "MEMORY.register(Neo4jBackend())",
        "tasks = {name: b.store(record) for name, b in self._backends.items()}",
        "for b in self._backends.values():",
    ),
    "vera/fabric/record_revision.py": ("class RecordRevision:",),
    "vera/fabric/memory_provider.py": (
        "class MemoryProjection:",
        'raise ValueError("a citation must bind the authoritative revision")',
    ),
    "vera/fabric/native_memory_adapter.py": (
        "class NativeMemoryBinding:", "def project_native_memory(",
        "def project_native_memory_with_receipt(",
    ),
    "vera/fabric/memory_hooks.py": (
        "async def record_agent_turn(", "await MEMORY.store(human_rec)",
        "await MEMORY.store(ai_rec)",
    ),
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
        missing = [value for value in assertions if value not in text]
        if missing:
            raise ValueError(f"source assertions are missing from {relative}: {missing}")
        result[relative] = {
            "content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "assertion_digests": [
                "sha256:" + hashlib.sha256(value.encode()).hexdigest()
                for value in assertions],
        }
    return result


def build_memory_record_authority_review(
    repo_root: Path,
    surfaces: Sequence[MemoryAuthoritySurface] = CURRENT_SURFACES,
) -> dict[str, Any]:
    """Describe current and target authority without reading or migrating data."""
    surfaces = tuple(sorted(surfaces, key=lambda item: item.surface_id))
    if not surfaces or len({item.surface for item in surfaces}) != len(surfaces):
        raise ValueError("memory authority surfaces must be non-empty and unique")
    states: dict[str, list[str]] = {}
    for item in surfaces:
        states.setdefault(item.authority_state, []).append(item.surface)
    for values in states.values():
        values.sort()
    payload = {
        "schema": SCHEMA,
        "surfaces": [item.to_dict() for item in surfaces],
        "surfaces_by_authority_state": dict(sorted(states.items())),
        "source_evidence": _source_evidence(Path(repo_root)),
        "current_contract": {
            "native_record_shape": "MemoryRecord",
            "native_content_authority_claim": "memory.postgres",
            "derived_indexes": ["memory.chroma", "memory.neo4j"],
            "write_coordinator": "memory.hybrid_store",
            "read_behavior": "first_successful_backend_in_registration_order",
            "atomic_fanout": False,
            "drift_possible": True,
        },
        "target_contract": {
            "content_authority": "fabric.record_revision",
            "retrieval_contract": "memory.provider_projection",
            "legacy_ingress": "memory.record",
            "compatibility_boundary": "memory.native_adapter",
            "projection_rebuild_required": True,
        },
        "findings": [
            "three_native_backends_accept_the_same_mutable_record",
            "hybrid_reads_do_not_prove_backend_agreement",
            "postgres_is_documented_as_archive_but_exposes_updates",
            "neo4j_owns_graph_edges_while_chroma_owns_vector_indexing",
            "provider_projection_is_revision_bound_but_not_runtime_traffic_authority",
            "session_cursors_and_hook_helpers_are_not_record_authorities",
        ],
        "recommendations": [
            "make_current_postgres_priority_and_projection_roles_executable_contracts",
            "retain_chroma_and_neo4j_as_rebuildable_projections",
            "route_new_content_authority_through_fabric_record_revisions",
            "retain_memory_record_as_compatibility_ingress_during_migration",
            "require_dual_write_receipts_and_reconciliation_before_cutover",
            "prove_success_error_timeout_cancel_restart_and_recovery_parity",
            "measure_all_direct_writers_stored_ids_and_external_consumers",
            "do_not_delete_or_rewrite_stored_memory_in_this_review",
        ],
        "coverage": {
            "declared_runtime_surfaces": "complete_for_bound_sources",
            "stored_records": "not_examined", "runtime_drift": "not_examined",
            "external_consumers": "not_examined", "live_backends": "not_run",
        },
        "removal_authority": False,
        "changes_write_path": False,
        "changes_read_path": False,
        "reads_stored_memory": False,
        "contacts_backends": False,
        "contacts_models": False,
        "mutates": False,
    }
    return {"review_id": _identity("mrar_", payload), **payload}
