"""Deterministic discovery and composition for portable context components."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from vera.context_provider import (
    ContextAssembly,
    ContextCancellation,
    ContextItem,
    ContextProvider,
    ContextProviderFailure,
    ContextRanker,
    assemble_context,
    collect_context_candidates,
)


@dataclass(frozen=True, slots=True)
class ContextComponent:
    component_id: str
    role: str


@dataclass(frozen=True, slots=True)
class ContextRankerFailure:
    ranker: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextComposition:
    assembly: ContextAssembly
    provider_failures: tuple[ContextProviderFailure, ...]
    ranker_failures: tuple[ContextRankerFailure, ...]
    providers: tuple[str, ...]
    rankers: tuple[str, ...]


def _component_id(component: object, attribute: str, role: str) -> str:
    value = getattr(component, attribute, None)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"context {role} id is required and must be canonical")
    return value


class ContextRegistry:
    """Explicit registry; registration order never controls execution order."""

    def __init__(self) -> None:
        self._providers: dict[str, ContextProvider] = {}
        self._rankers: dict[str, ContextRanker] = {}

    def register_provider(self, provider: ContextProvider) -> None:
        component_id = _component_id(provider, "provider_id", "provider")
        if component_id in self._providers:
            raise ValueError(f"context provider already registered: {component_id}")
        self._providers[component_id] = provider

    def register_ranker(self, ranker: ContextRanker) -> None:
        component_id = _component_id(ranker, "ranker_id", "ranker")
        if component_id in self._rankers:
            raise ValueError(f"context ranker already registered: {component_id}")
        self._rankers[component_id] = ranker

    def manifest(self) -> tuple[ContextComponent, ...]:
        providers = (ContextComponent(value, "provider")
                     for value in self._providers)
        rankers = (ContextComponent(value, "ranker") for value in self._rankers)
        return tuple(sorted((*providers, *rankers),
                            key=lambda value: (value.role, value.component_id)))

    def _select(self, available: dict[str, object], requested: Sequence[str],
                role: str, *, allow_empty: bool) -> tuple[tuple[str, ...], tuple[object, ...]]:
        if isinstance(requested, (str, bytes)):
            raise ValueError(f"context {role} selection must be a sequence")
        try:
            identifiers = tuple(requested)
        except TypeError as exc:
            raise ValueError(f"context {role} selection must be a sequence") from exc
        if any(not isinstance(value, str) or not value.strip() or value != value.strip()
               for value in identifiers):
            raise ValueError(f"context {role} selection contains an invalid id")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError(f"context {role} selection must be unique")
        if not identifiers and not allow_empty:
            raise ValueError(f"context {role} selection is required")
        unknown = sorted(set(identifiers).difference(available))
        if unknown:
            raise ValueError(f"unknown context {role}: {', '.join(unknown)}")
        return identifiers, tuple(available[value] for value in identifiers)

    @staticmethod
    def _authority(item: ContextItem) -> tuple:
        return (item.provider, item.item_id, item.source, item.revision, item.text,
                item.token_count, item.citations)

    @classmethod
    def _validate_ranked(cls, before: tuple[ContextItem, ...],
                         after: tuple[ContextItem, ...]) -> None:
        if Counter(map(cls._authority, before)) != Counter(map(cls._authority, after)):
            raise ValueError("ranker changed authoritative context")

    async def compose(
            self, query: str, *, provider_ids: Sequence[str],
            ranker_ids: Sequence[str] = (), limit_per_provider: int,
            budget_tokens: int,
            cancellation: ContextCancellation | None = None,
            ) -> ContextComposition:
        """Collect all candidates, rank explicitly, then apply one final budget."""
        # Validate a caller-owned budget before provider dispatch can have effects.
        assemble_context((), budget_tokens=budget_tokens)
        selected_provider_ids, providers = self._select(
            self._providers, provider_ids, "provider", allow_empty=False)
        selected_ranker_ids, rankers = self._select(
            self._rankers, ranker_ids, "ranker", allow_empty=True)
        candidates = await collect_context_candidates(
            providers, query, limit_per_provider=limit_per_provider,
            cancellation=cancellation)
        items: tuple[ContextItem, ...] = candidates.items
        failures: list[ContextRankerFailure] = []
        applied: list[str] = []
        for ranker_id, ranker in zip(selected_ranker_ids, rankers):
            if cancellation is not None:
                cancellation.checkpoint()
            try:
                result = ranker.rank(items)
                ranked = tuple(result)
                if any(not isinstance(item, ContextItem) for item in ranked):
                    raise ValueError("ranker returned invalid context item")
                self._validate_ranked(items, ranked)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.append(ContextRankerFailure(ranker_id,
                                                     type(exc).__name__))
                continue
            except BaseException:
                raise
            items = ranked
            applied.append(ranker_id)
        if cancellation is not None:
            cancellation.checkpoint()
        return ContextComposition(
            assembly=assemble_context(items, budget_tokens=budget_tokens),
            provider_failures=candidates.failures,
            ranker_failures=tuple(failures),
            providers=selected_provider_ids,
            rankers=tuple(applied),
        )
