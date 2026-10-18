"""Authority-preserving adapters for Vera's existing web capabilities.

The adapters are inert until ``scout`` or ``collect`` is called. Callables,
clock evidence and worker identity are injected by the owning runtime; this
module does not import the application registry, infer capacity, retry, or
persist provider payloads.
"""
from __future__ import annotations

import hashlib
import inspect
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

try:
    from vera.context_provider import ContextCitation, ContextItem
    from vera.discovery_contract import (
        CollectionOption, CollectionReceipt, DiscoveredContext,
        DiscoveryRequest, SourceCandidate,
    )
    from vera.discovery_orchestration import CollectedBatch
    from vera.discovery_routing import DiscoveryAssignment
except ImportError:  # pragma: no cover - installed application spelling
    from Vera.vera.context_provider import ContextCitation, ContextItem
    from Vera.vera.discovery_contract import (
        CollectionOption, CollectionReceipt, DiscoveredContext,
        DiscoveryRequest, SourceCandidate,
    )
    from Vera.vera.discovery_orchestration import CollectedBatch
    from Vera.vera.discovery_routing import DiscoveryAssignment


MAX_TARGETS = 1_024
MAX_NATIVE_RESULTS = 256
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _revision(value: str, field: str) -> str:
    value = str(value or "").strip()
    if not _REVISION.fullmatch(value):
        raise ValueError(f"{field} must be a bounded identifier")
    return value


def _score(value: Any, fallback: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return fallback
    return score if math.isfinite(score) and 0 <= score <= 1 else fallback


def _target_url(value: Any) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 2_048 or any(char in value for char in "\r\n\0"):
        raise ValueError("web target URL must be present and bounded")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web target URL must be HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("web target URL must not contain credentials")
    return value


async def _invoke(callable_: Callable[..., Any], **kwargs: Any) -> Any:
    result = callable_(**kwargs)
    return await result if inspect.isawaitable(result) else result


def _bounded_utf8(text: str, maximum: int) -> tuple[str, int]:
    encoded = str(text or "").encode("utf-8")[:maximum]
    while encoded:
        try:
            return encoded.decode("utf-8"), len(encoded)
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return "", 0


@dataclass(frozen=True, slots=True)
class NativeWebTarget:
    source_id: str
    url: str
    title: str
    snippet: str
    relevance: float


class NativeWebTargetStore:
    """Bounded route-local lookup for redacted candidate locators."""

    def __init__(self, maximum: int = MAX_TARGETS):
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_TARGETS:
            raise ValueError("target-store maximum must be between one and 1024")
        self.maximum = maximum
        self._targets: OrderedDict[str, NativeWebTarget] = OrderedDict()

    def put(self, target: NativeWebTarget) -> None:
        if target.source_id in self._targets:
            self._targets.move_to_end(target.source_id)
        self._targets[target.source_id] = target
        while len(self._targets) > self.maximum:
            self._targets.popitem(last=False)

    def get(self, source_id: str) -> NativeWebTarget | None:
        return self._targets.get(source_id)


class CapabilityDiscoveryScout:
    """Adapt one injected ``web.search``-shaped callable to portable candidates."""

    scout_id = "native.web.search"

    def __init__(self, search: Callable[..., Any], targets: NativeWebTargetStore, *,
                 search_revision: str, fetch_revision: str,
                 clock: Callable[[], str] = _utc_now):
        if not callable(search) or not callable(clock):
            raise ValueError("search and clock must be callable")
        self._search = search
        self._targets = targets
        self.revision = _revision(search_revision, "search_revision")
        self._fetch_revision = _revision(fetch_revision, "fetch_revision")
        self._clock = clock

    async def scout(self, request: DiscoveryRequest, *, cancellation=None
                    ) -> tuple[SourceCandidate, ...]:
        if cancellation is not None:
            cancellation.checkpoint()
        limit = min(request.max_sources, MAX_NATIVE_RESULTS)
        response = await _invoke(self._search, query=request.query, limit=limit)
        if cancellation is not None:
            cancellation.checkpoint()
        rows = response.get("results", response.get("items", [])) if isinstance(response, Mapping) else response
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ValueError("web search returned a non-sequence")
        candidates: list[SourceCandidate] = []
        seen_urls: set[str] = set()
        for index, row in enumerate(rows[:limit]):
            if not isinstance(row, Mapping):
                continue
            try:
                url = _target_url(row.get("url") or row.get("link"))
            except ValueError:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            digest = hashlib.sha256(url.encode()).hexdigest()
            source_id = f"web.{digest}"
            relevance = _score(row.get("score"), max(0.0, 1.0 - index / max(1, limit)))
            target = NativeWebTarget(source_id, url,
                                     str(row.get("title") or "")[:500],
                                     str(row.get("snippet") or row.get("description") or "")[:2_000],
                                     relevance)
            option = CollectionOption(
                source_id=source_id, method="capability", provider="web.fetch",
                provider_revision=self._fetch_revision, resource="network",
                output_kinds=("context",), estimated_latency_ms=1_000,
                max_bytes=min(request.max_bytes, 1_000_000), network_required=True)
            candidate = SourceCandidate(
                request_id=request.request_id, source_id=source_id,
                locator=f"capability://web.fetch/{digest}", source_kind="web",
                provider="web.search", revision=self.revision,
                observed_at=self._clock(), authority_score=_score(row.get("authority_score"), .5),
                relevance_score=relevance, freshness_score=_score(row.get("freshness_score"), .5),
                options=(option,))
            self._targets.put(target)
            candidates.append(candidate)
        return tuple(candidates)


class CapabilityDiscoveryCollector:
    """Adapt one injected ``web.fetch``-shaped callable without taking retry authority."""

    provider = "web.fetch"
    methods = ("capability",)

    def __init__(self, fetch: Callable[..., Any], targets: NativeWebTargetStore, *,
                 worker_id: str, provider_revision: str,
                 clock: Callable[[], str] = _utc_now,
                 monotonic: Callable[[], float] = time.monotonic):
        if not callable(fetch) or not callable(clock) or not callable(monotonic):
            raise ValueError("fetch, clock and monotonic must be callable")
        self._fetch = fetch
        self._targets = targets
        self.worker_id = _revision(worker_id, "worker_id")
        self.provider_revision = _revision(provider_revision, "provider_revision")
        self._clock = clock
        self._monotonic = monotonic

    async def collect(self, request: DiscoveryRequest, candidate: SourceCandidate,
                      option: CollectionOption, assignment: DiscoveryAssignment, *,
                      cancellation=None) -> CollectedBatch:
        if (assignment.worker_id != self.worker_id or option.provider != self.provider or
                option.provider_revision != self.provider_revision or
                option.source_id != candidate.source_id or
                assignment.option_id != option.option_id or
                assignment.candidate_id != candidate.candidate_id):
            raise ValueError("collection assignment does not match native web adapter")
        target = self._targets.get(candidate.source_id)
        if target is None:
            raise ValueError("native web target is unavailable")
        if cancellation is not None:
            cancellation.checkpoint()
        started_at = self._clock()
        started = self._monotonic()
        response = await _invoke(self._fetch, url=target.url)
        if cancellation is not None:
            cancellation.checkpoint()
        completed_at = self._clock()
        duration_ms = max(0, min(86_400_000, int((self._monotonic() - started) * 1_000)))
        if not isinstance(response, Mapping):
            raise ValueError("web fetch returned a non-object")
        raw = response.get("text") or response.get("markdown") or response.get("content") or ""
        text, byte_count = _bounded_utf8(str(raw), min(option.max_bytes, request.max_bytes))
        if not text.strip():
            receipt = CollectionReceipt(
                request_id=request.request_id, candidate_id=candidate.candidate_id,
                option_id=option.option_id, provider_revision=self.provider_revision,
                status="failed", started_at=started_at, completed_at=completed_at,
                duration_ms=duration_ms, error_code="empty_result")
            return CollectedBatch(receipt)
        receipt = CollectionReceipt(
            request_id=request.request_id, candidate_id=candidate.candidate_id,
            option_id=option.option_id, provider_revision=self.provider_revision,
            status="succeeded", started_at=started_at, completed_at=completed_at,
            item_count=1, byte_count=byte_count, duration_ms=duration_ms)
        revision = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        item_id = "web." + hashlib.sha256((target.url + revision).encode()).hexdigest()
        item = ContextItem(
            item_id, text, candidate.source_id, revision, self.provider,
            candidate.relevance_score, max(1, math.ceil(len(text) / 4)),
            (ContextCitation(receipt.receipt_id, target.url),))
        return CollectedBatch(receipt, (DiscoveredContext(receipt.receipt_id, item),))
