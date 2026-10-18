"""Default-off runtime wiring for Vera's shared native Memory write seam.

``HybridMemoryStore.store`` calls this seam after the sandbox guard, so public
and internal producers receive the same observational coverage.  When the
switch is off, no revision, policy, SQLite path or receipt sink is constructed.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import json
from typing import Any, Awaitable, Callable, Mapping

from .memory_dual_write_canary import (
    MemoryDualWriteCanaryResult,
    execute_memory_dual_write_canary,
)
from .memory_dual_write_store import MemoryDualWriteReceiptStore
from .record_revision import create_record_revision
from .revision_state_paths import (
    default_memory_dual_write_receipt_path,
    default_memory_revision_path,
)
from .revision_path import RevisionPath
from .revision_policy import RevisionPolicy
from .revision_store import RevisionStore


NativeWriter = Callable[[], Awaitable[Mapping[str, bool | str]]]
CANARY_ENV = "VERA_MEMORY_REVISION_CANARY"
REVISION_PATH_ENV = "FABRIC_REVISION_SQLITE"
RECEIPT_PATH_ENV = "VERA_MEMORY_DUAL_WRITE_RECEIPTS"
POLICY_ENV = "FABRIC_REVISION_POLICY"


def memory_revision_canary_enabled(environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(CANARY_ENV, "0")).strip() == "1"


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _bounded_json(value: Any, *, limit: int = 65_536, fallback: Any) -> Any:
    """Return a detached, bounded JSON value suitable for immutable evidence."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > limit:
            return fallback
        return json.loads(encoded)
    except (TypeError, ValueError):
        return fallback


def native_memory_content_hash(snapshot: Mapping[str, Any]) -> str:
    """Return the exact hash used by ``MemoryRecord.compute_hash``."""
    text = str(snapshot.get("full_text") or snapshot.get("text") or "")
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def revision_from_native_memory_snapshot(snapshot: Mapping[str, Any]):
    """Build the canonical revision for one public ``memory.store`` input."""
    native_id = _bounded_text(snapshot.get("id"), 256).strip()
    if not native_id:
        raise ValueError("native memory snapshot requires an id")
    created_at = _bounded_text(snapshot.get("created_at"), 64).strip()
    text = _bounded_text(
        snapshot.get("full_text") or snapshot.get("text") or snapshot.get("summary"),
        1_048_576,
    )
    tags = snapshot.get("tags") or []
    if not isinstance(tags, (list, tuple)):
        tags = []
    try:
        importance = float(snapshot.get("importance", 0.5))
    except (TypeError, ValueError):
        importance = 0.5
    if not math.isfinite(importance):
        importance = 0.5
    content = {
        "text": text,
        "summary": _bounded_text(snapshot.get("summary"), 65_536),
        "native_record_type": _bounded_text(snapshot.get("record_type"), 256),
        "source_type": _bounded_text(snapshot.get("source_type"), 256),
        "category": _bounded_text(snapshot.get("category"), 256),
        "tags": [_bounded_text(item, 256) for item in tags[:256]],
        "importance": importance,
        "parent_id": _bounded_text(snapshot.get("parent_id"), 256),
        "session_id": _bounded_text(snapshot.get("session_id"), 256),
        "trace_id": _bounded_text(snapshot.get("trace_id"), 256),
        "ttl_seconds": int(snapshot.get("ttl_seconds") or 0),
        "archived": bool(snapshot.get("archived", False)),
        "language": _bounded_text(snapshot.get("language"), 64),
        "human_text": bool(snapshot.get("human_text", False)),
        "ai_output": bool(snapshot.get("ai_output", False)),
        "model": _bounded_text(snapshot.get("model"), 512),
        "capability": _bounded_text(snapshot.get("capability"), 512),
        "source_url": _bounded_text(snapshot.get("source_url"), 4096),
        "metadata": _bounded_json(snapshot.get("metadata") or {}, fallback={}),
        "keywords": _bounded_json(snapshot.get("keywords") or [], fallback=[]),
        "embedding_model": _bounded_text(snapshot.get("embedding_model"), 512),
        "graph_id": _bounded_text(snapshot.get("graph_id"), 256),
        "relations": _bounded_json(snapshot.get("relations") or [], fallback=[]),
    }
    return create_record_revision(
        namespace="memory.native", record_type="memory", logical_key=native_id,
        created_at=created_at, content=content,
        content_schema={"media_type": "application/vnd.vera.memory+json"},
        source={"kind": "vera.native-memory", "native_memory_id": native_id},
        policy={}, metadata={"native_memory_id": native_id},
    )


async def store_public_memory_with_revision_canary(
    snapshot: Mapping[str, Any], *, native_write: NativeWriter, actor: str,
    observed_at: str, producer_kind: str = "internal",
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, bool | str]:
    """Store through the native path, optionally shadowing canonical authority.

    The returned mapping is always the native writer's exact result.  Disabled
    operation is deliberately a single direct await with no setup side effects.
    """
    env = os.environ if environ is None else environ
    if not memory_revision_canary_enabled(env):
        return await native_write()

    try:
        revision = revision_from_native_memory_snapshot(snapshot)
        native_id = str(snapshot["id"])
        native_hash = _bounded_text(snapshot.get("content_hash"), 256).strip()
        if not native_hash:
            native_hash = native_memory_content_hash(snapshot)
        policy = RevisionPolicy.from_json(str(env.get(POLICY_ENV, "")))
        path = RevisionPath(
            RevisionStore(str(env.get(REVISION_PATH_ENV) or default_memory_revision_path())),
            policy.allows,
        )
        receipt_sink = MemoryDualWriteReceiptStore(
            str(env.get(RECEIPT_PATH_ENV) or default_memory_dual_write_receipt_path()))
    except Exception:
        # The canary is observational.  Invalid policy, state paths, or
        # snapshot data must preserve the exact native memory.store contract.
        return await native_write()

    async def revision_write(value):
        return await asyncio.to_thread(
            path.put, value, actor=actor, projections=("memory",))

    result: MemoryDualWriteCanaryResult = await execute_memory_dual_write_canary(
        enabled=True,
        operation_id=f"memory.store:{native_id}",
        native_memory_id=native_id,
        native_content_hash=native_hash,
        revision=revision,
        observed_at=observed_at,
        native_write=native_write,
        revision_write=revision_write,
        receipt_sink=receipt_sink,
        # The native path owns its backend timeouts.  RevisionStore is a
        # synchronous transaction: cancelling its worker thread could report a
        # timeout while the commit continues.  Do not add ambiguous outer caps.
        native_timeout_s=None,
        revision_timeout_s=None,
        revision_factory=lambda: revision_from_native_memory_snapshot(snapshot),
        producer_kind=producer_kind,
    )
    # ``MemoryDualWriteCanaryResult`` freezes its internal mapping with a
    # ``MappingProxyType``.  Capability results cross a JSON boundary, so do
    # not leak that implementation detail to ``memory.store`` callers.
    return dict(result.native_result)
