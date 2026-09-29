"""Fail-closed projection of native Agent RAG hits into portable context."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from urllib.parse import quote

from vera.context_provider import ContextCitation, ContextItem


MAX_HITS = 100
MAX_AUTHORITY_CHARS = 512
MAX_TEXT_CHARS = 20_000
RECEIPT_SCHEMA = "vera.agent-rag-context-projection/v1"


@dataclass(frozen=True, slots=True)
class AgentRagProjectionReceipt:
    projection_id: str
    provider_id: str
    input_count: int
    item_identities: tuple[tuple[str, str, str], ...]
    schema: str = RECEIPT_SCHEMA

    def to_dict(self) -> dict:
        return {"schema": self.schema, "projection_id": self.projection_id,
                "provider_id": self.provider_id, "input_count": self.input_count,
                "item_identities": [list(value) for value in self.item_identities]}


@dataclass(frozen=True, slots=True)
class AgentRagContextProjection:
    items: tuple[ContextItem, ...]
    receipt: AgentRagProjectionReceipt


def _authority(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > MAX_AUTHORITY_CHARS \
            or any(ord(char) < 32 for char in result):
        raise ValueError(f"Agent RAG {name} must be present and bounded")
    return result


def project_agent_rag_results(
    rows: Sequence[Mapping[str, object]], *, provider_id: str,
    token_counter: Callable[[str], int],
) -> tuple[ContextItem, ...]:
    """Project already-retrieved hits; never query Fabric or infer authority."""
    return project_agent_rag_batch(
        rows, provider_id=provider_id, token_counter=token_counter).items


def project_agent_rag_batch(
    rows: Sequence[Mapping[str, object]], *, provider_id: str,
    token_counter: Callable[[str], int],
) -> AgentRagContextProjection:
    """Project hits and return payload-free lifecycle/provenance evidence."""
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        raise ValueError("provider_id is required")
    if not callable(token_counter):
        raise TypeError("token_counter must be callable")
    if isinstance(rows, (str, bytes)) or len(rows) > MAX_HITS:
        raise ValueError(f"rows must contain at most {MAX_HITS} hits")

    items: list[ContextItem] = []
    identities: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Agent RAG hits must be objects")
        dataset = _authority(row.get("dataset") or row.get("dataset_id"), "dataset")
        record_id = _authority(row.get("record_id") or row.get("id"), "record")
        revision_id = _authority(row.get("revision_id"), "revision")
        text = str(row.get("text") or "").strip()
        if not text or len(text) > MAX_TEXT_CHARS:
            raise ValueError("portable Agent RAG hits require bounded text")
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) \
                or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Agent RAG score must be between zero and one")
        identity = (dataset, record_id, revision_id)
        if identity in identities:
            raise ValueError("Agent RAG hit identities must be unique")
        identities.add(identity)
        locator = (f"fabric://{quote(dataset, safe='')}/{quote(record_id, safe='')}"
                   f"?revision={quote(revision_id, safe='')}")
        tokens = token_counter(text)
        items.append(ContextItem(
            item_id=f"{dataset}:{record_id}", text=text, source=record_id,
            revision=revision_id, provider=provider_id, score=float(score),
            token_count=tokens,
            citations=(ContextCitation(record_id, locator),),
        ))
    item_identities = tuple(sorted(identities))
    identity = {"schema": RECEIPT_SCHEMA, "provider_id": provider_id,
                "input_count": len(rows), "item_identities": item_identities}
    projection_id = "arp_" + hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return AgentRagContextProjection(
        tuple(items), AgentRagProjectionReceipt(
            projection_id, provider_id, len(rows), item_identities))
