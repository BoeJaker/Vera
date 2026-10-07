"""Default-off runtime wiring for one public native Memory ingress.

Only ``memory.store`` calls this seam.  Internal ``HybridMemoryStore`` callers
remain untouched.  When the switch is off, no revision, policy, SQLite path or
receipt sink is constructed.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .memory_dual_write_canary import (
    MemoryDualWriteCanaryResult,
    execute_memory_dual_write_canary,
)
from .memory_dual_write_store import MemoryDualWriteReceiptStore
from .record_revision import create_record_revision
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


def _default_revision_path() -> str:
    return str(Path(__file__).with_name("vera_fabric_revisions.db"))


def _default_receipt_path() -> str:
    return str(Path(__file__).with_name("memory_dual_write_receipts.db"))


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _revision(snapshot: Mapping[str, Any]):
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
        "human_text": bool(snapshot.get("human_text", False)),
        "ai_output": bool(snapshot.get("ai_output", False)),
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
    observed_at: str, environ: Mapping[str, str] | None = None,
) -> Mapping[str, bool | str]:
    """Store through the native path, optionally shadowing canonical authority.

    The returned mapping is always the native writer's exact result.  Disabled
    operation is deliberately a single direct await with no setup side effects.
    """
    env = os.environ if environ is None else environ
    if not memory_revision_canary_enabled(env):
        return await native_write()

    revision = _revision(snapshot)
    native_id = str(snapshot["id"])
    native_hash = _bounded_text(snapshot.get("content_hash"), 256).strip()
    if not native_hash:
        # MemoryRecord normally derives this inside HybridMemoryStore.store.
        # The canary snapshot is intentionally captured before that native
        # call, so derive the same value without mutating the record or forcing
        # different behavior on the disabled path.
        text = str(snapshot.get("full_text") or snapshot.get("text") or "")
        native_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    policy = RevisionPolicy.from_json(str(env.get(POLICY_ENV, "")))
    path = RevisionPath(
        RevisionStore(str(env.get(REVISION_PATH_ENV) or _default_revision_path())),
        policy.allows,
    )
    receipt_sink = MemoryDualWriteReceiptStore(
        str(env.get(RECEIPT_PATH_ENV) or _default_receipt_path()))

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
    )
    return result.native_result
