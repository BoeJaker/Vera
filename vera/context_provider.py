"""Portable, cited and explicitly budgeted context assembly contracts."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True, slots=True)
class ContextCitation:
    source_id: str
    locator: str
    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip()
                   for value in (self.source_id, self.locator)):
            raise ValueError("context citations require source_id and locator")

@dataclass(frozen=True, slots=True)
class ContextItem:
    item_id: str
    text: str
    source: str
    revision: str
    provider: str
    score: float
    token_count: int
    citations: tuple[ContextCitation, ...]
    def __post_init__(self) -> None:
        identity = (self.item_id, self.text, self.source, self.revision, self.provider)
        if not all(isinstance(value, str) and value.strip() for value in identity):
            raise ValueError("context identity, text, source, revision and provider are required")
        if isinstance(self.token_count, bool) or not isinstance(self.token_count, int) \
                or self.token_count <= 0:
            raise ValueError("context token_count must be positive")
        if not self.citations or not all(
                isinstance(citation, ContextCitation) for citation in self.citations):
            raise ValueError("uncited context is not admissible")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) \
                or not math.isfinite(self.score) or not 0 <= self.score <= 1:
            raise ValueError("context score must be between zero and one")


@runtime_checkable
class ContextProvider(Protocol):
    provider_id: str
    async def search(self, query: str, *, limit: int,
                     cancellation: "ContextCancellation | None" = None
                     ) -> Sequence[ContextItem]: ...


class ContextCancellation(Protocol):
    def checkpoint(self) -> None: ...

@dataclass(frozen=True, slots=True)
class ContextAssembly:
    items: tuple[ContextItem, ...]
    used_tokens: int
    budget_tokens: int
    omitted_items: int


@dataclass(frozen=True, slots=True)
class ContextProviderFailure:
    provider: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextCollection:
    assembly: ContextAssembly
    failures: tuple[ContextProviderFailure, ...]


def assemble_context(items: Sequence[ContextItem], *, budget_tokens: int) -> ContextAssembly:
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) \
            or budget_tokens < 0:
        raise ValueError("context budget must be non-negative")
    unique = {}
    for item in items:
        key = (item.provider, item.item_id, item.revision)
        if key not in unique or item.score > unique[key].score:
            unique[key] = item
    ordered = sorted(unique.values(), key=lambda x: (-x.score, x.token_count,
                                                      x.provider, x.item_id, x.revision))
    selected, used = [], 0
    for item in ordered:
        if used + item.token_count <= budget_tokens:
            selected.append(item)
            used += item.token_count
    return ContextAssembly(
        tuple(selected), used, budget_tokens, len(ordered) - len(selected))


async def collect_context(providers: Sequence[ContextProvider], query: str, *,
                          limit_per_provider: int,
                          budget_tokens: int,
                          cancellation: ContextCancellation | None = None
                          ) -> ContextCollection:
    """Query providers concurrently and isolate ordinary provider failures."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("context query is required")
    if isinstance(limit_per_provider, bool) or not isinstance(limit_per_provider, int) \
            or limit_per_provider <= 0:
        raise ValueError("context provider limit must be positive")
    provider_ids = [str(provider.provider_id).strip() for provider in providers]
    if any(not provider_id for provider_id in provider_ids):
        raise ValueError("context provider_id is required")
    if len(set(provider_ids)) != len(provider_ids):
        raise ValueError("context provider_id must be unique")

    if cancellation is not None:
        cancellation.checkpoint()
    results = await asyncio.gather(*(
        provider.search(query, limit=limit_per_provider, cancellation=cancellation)
        for provider in providers),
        return_exceptions=True)
    if cancellation is not None:
        cancellation.checkpoint()
    items: list[ContextItem] = []
    failures: list[ContextProviderFailure] = []
    for provider_id, result in zip(provider_ids, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            failures.append(ContextProviderFailure(provider_id,
                                                    type(result).__name__))
            continue
        if isinstance(result, BaseException):
            raise result
        try:
            batch = tuple(result)
            if any(not isinstance(item, ContextItem) for item in batch):
                raise ValueError("provider returned invalid context item")
            if any(item.provider != provider_id for item in batch):
                raise ValueError("provider returned foreign context identity")
            items.extend(batch)
        except (TypeError, ValueError) as exc:
            failures.append(ContextProviderFailure(provider_id,
                                                    type(exc).__name__))
    failures.sort(key=lambda failure: failure.provider)
    return ContextCollection(
        assemble_context(items, budget_tokens=budget_tokens), tuple(failures))
