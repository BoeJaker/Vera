"""Policy boundary for the canonical Fabric revision authority."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .record_revision import RecordRevision
from .revision_store import RevisionStore


class RevisionAccessDenied(PermissionError):
    pass


Authorizer = Callable[[str, str, dict[str, Any]], bool]


class RevisionPath:
    """One explicit read/write seam over RevisionStore.

    Authorization is injected by the caller so the authority stays independent
    of HTTP, agent, tenant, or deployment identity systems. Denials happen
    before storage is consulted or mutated.
    """

    def __init__(self, store: RevisionStore, authorize: Authorizer):
        if not callable(authorize):
            raise TypeError("authorize must be callable")
        self.store = store
        self.authorize = authorize

    def _require(self, action: str, actor: str, resource: dict[str, Any]) -> None:
        actor = str(actor or "").strip()
        if not actor or not self.authorize(action, actor, resource):
            raise RevisionAccessDenied(f"{action} denied")

    def put(self, revision: RecordRevision, *, actor: str,
            projections: Iterable[str], expected_head: str | None = None
            ) -> dict[str, Any]:
        if not isinstance(revision, RecordRevision):
            raise TypeError("revision must be a RecordRevision")
        envelope = revision.to_dict()
        self._require("revision.write", actor, {
            "record_id": revision.record_id,
            "revision_id": revision.revision_id,
            "namespace": revision.namespace,
            "record_type": revision.record_type,
            "tombstone": revision.tombstone,
            "policy": envelope["policy"],
        })
        return self.store.put(revision, projections=projections,
                              expected_head=expected_head)

    def get(self, record_id: str, *, actor: str,
            revision_id: str = "") -> dict[str, Any] | None:
        resource = {"record_id": str(record_id),
                    "revision_id": str(revision_id or "")}
        self._require("revision.read", actor, resource)
        if revision_id:
            value = self.store.revision(revision_id)
            if value["record_id"] != record_id:
                raise KeyError("revision does not belong to record")
            return value
        return self.store.current(record_id)
