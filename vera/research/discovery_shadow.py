"""Payload-safe shadow adaptation for Research's native web citations.

The adapter consumes values Research already produced. It never searches,
fetches, schedules work, or claims that the portable discovery route executed.
Portable ``ContextItem`` values are ephemeral; only their bounded identities
and coverage summary are suitable for the operator ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

try:
    from vera.context_provider import ContextCitation, ContextItem
except ImportError:  # pragma: no cover - deployed package spelling
    from Vera.vera.context_provider import ContextCitation, ContextItem


SHADOW_SCHEMA = "vera.research-discovery-shadow/v1"
MAX_CITATIONS = 1_000
MAX_TEXT_CHARS = 200_000


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _value(citation: Any, name: str, default: Any = "") -> Any:
    if isinstance(citation, Mapping):
        return citation.get(name, default)
    return getattr(citation, name, default)


def _locator(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 8_192:
        raise ValueError("missing_or_oversized_url")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported_url")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentialed_url")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                       parsed.query, ""))


def _text(citation: Any) -> str:
    for name in ("full_text", "snippet", "title"):
        value = str(_value(citation, name, "") or "").strip()
        if value:
            return value[:MAX_TEXT_CHARS]
    raise ValueError("missing_text")


def _score(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def adapt_research_citations(citations: Sequence[Any]) -> tuple[ContextItem, ...]:
    """Map existing native citations to portable context without side effects."""
    try:
        values = tuple(citations)
    except TypeError as exc:
        raise ValueError("citations must be a sequence") from exc
    if len(values) > MAX_CITATIONS:
        raise ValueError("citation limit exceeded")

    items: list[ContextItem] = []
    seen: set[tuple[str, str]] = set()
    for citation in values:
        try:
            locator = _locator(_value(citation, "url"))
            text = _text(citation)
        except ValueError:
            continue
        revision = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        native_id = str(_value(citation, "id", "") or "").strip()
        identity = native_id or _digest({"locator": locator, "revision": revision})
        item_id = "research_web_" + _digest(identity)[:32]
        key = (item_id, revision)
        if key in seen:
            continue
        seen.add(key)
        items.append(ContextItem(
            item_id=item_id,
            text=text,
            source=locator,
            revision=revision,
            provider="research.native.web",
            score=_score(_value(citation, "rank_score", 0.0)),
            token_count=max(1, len(text.split())),
            citations=(ContextCitation(
                source_id="research.native." + _digest(identity)[:32],
                locator=locator,
            ),),
        ))
    return tuple(items)


@dataclass(frozen=True, slots=True)
class ResearchDiscoveryShadow:
    observation_id: str
    query_digest: str
    native_result_count: int
    portable_context_count: int
    excluded_count: int
    full_text_count: int
    screenshot_count: int
    portable_item_ids: tuple[str, ...]
    portable_revision_digests: tuple[str, ...]
    schema: str = SHADOW_SCHEMA
    producer: str = "research.gather_web_search"
    execution_authority: str = "research.native"
    worker_evidence_available: bool = False
    route_evidence_available: bool = False
    consumer_routing_changed: bool = False
    duplicate_provider_calls: bool = False
    payloads_included: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "observation_id": self.observation_id,
            "producer": self.producer, "query_digest": self.query_digest,
            "native_result_count": self.native_result_count,
            "portable_context_count": self.portable_context_count,
            "excluded_count": self.excluded_count,
            "full_text_count": self.full_text_count,
            "screenshot_count": self.screenshot_count,
            "portable_item_ids": list(self.portable_item_ids),
            "portable_revision_digests": list(self.portable_revision_digests),
            "execution_authority": self.execution_authority,
            "worker_evidence_available": self.worker_evidence_available,
            "route_evidence_available": self.route_evidence_available,
            "consumer_routing_changed": self.consumer_routing_changed,
            "duplicate_provider_calls": self.duplicate_provider_calls,
            "payloads_included": self.payloads_included,
        }


def observe_research_citations(query: str, citations: Sequence[Any]
                               ) -> tuple[tuple[ContextItem, ...], ResearchDiscoveryShadow]:
    """Return ephemeral portable items and a payload-free shadow observation."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is required")
    try:
        native = tuple(citations)
    except TypeError as exc:
        raise ValueError("citations must be a sequence") from exc
    items = adapt_research_citations(native)
    identities = tuple(item.item_id for item in items)
    revisions = tuple(item.revision for item in items)
    core = {
        "schema": SHADOW_SCHEMA, "producer": "research.gather_web_search",
        "query_digest": _digest(query), "native_result_count": len(native),
        "portable_context_count": len(items),
        "excluded_count": len(native) - len(items),
        "full_text_count": sum(bool(str(_value(c, "full_text", "") or "").strip())
                               for c in native),
        "screenshot_count": sum(bool(str(_value(c, "screenshot_path", "") or "").strip())
                                for c in native),
        "portable_item_ids": identities, "portable_revision_digests": revisions,
        "execution_authority": "research.native",
        "worker_evidence_available": False, "route_evidence_available": False,
        "consumer_routing_changed": False, "duplicate_provider_calls": False,
        "payloads_included": False,
    }
    return items, ResearchDiscoveryShadow(
        observation_id="rdso_" + _digest(core),
        query_digest=core["query_digest"], native_result_count=len(native),
        portable_context_count=len(items), excluded_count=len(native) - len(items),
        full_text_count=core["full_text_count"],
        screenshot_count=core["screenshot_count"],
        portable_item_ids=identities, portable_revision_digests=revisions,
    )


def record_research_citations_shadow(
        query: str, citations: Sequence[Any],
        recorder: Callable[[ResearchDiscoveryShadow], Any], *,
        on_error: Callable[[Exception], Any] | None = None) -> Sequence[Any]:
    """Best-effort hook that always returns the original native result object."""
    try:
        _items, observation = observe_research_citations(query, citations)
        recorder(observation)
    except Exception as exc:  # shadow evidence may never fail native Research
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
    return citations
