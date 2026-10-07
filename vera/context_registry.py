"""Deterministic discovery and composition for portable context components."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
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
from vera.discovery_contract import DiscoveryResult


def _hash(prefix: str, value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode()
    return prefix + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextComponent:
    component_id: str
    role: str


@dataclass(frozen=True, slots=True, order=True)
class DiscoveredContextAuthority:
    provider_id: str
    item_id: str
    source: str
    revision: str

    def __post_init__(self) -> None:
        for name in ("provider_id", "item_id", "source", "revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"discovered context {name} must be canonical")

    @classmethod
    def from_item(cls, item: ContextItem) -> "DiscoveredContextAuthority":
        return cls(item.provider, item.item_id, item.source, item.revision)


@dataclass(frozen=True, slots=True)
class DiscoveryContextSelectionPolicy:
    allowed_provider_ids: tuple[str, ...] = ()
    maximum_providers: int = 64
    require_all_registered: bool = True

    def __post_init__(self) -> None:
        allowed = tuple(self.allowed_provider_ids)
        if (any(not isinstance(value, str) or not value.strip() or
                value != value.strip() for value in allowed) or
                len(allowed) != len(set(allowed))):
            raise ValueError("allowed context providers must be canonical and unique")
        if (isinstance(self.maximum_providers, bool) or
                not isinstance(self.maximum_providers, int) or
                not 1 <= self.maximum_providers <= 256):
            raise ValueError("maximum_providers must be between one and 256")
        if not isinstance(self.require_all_registered, bool):
            raise ValueError("require_all_registered must be boolean")
        object.__setattr__(self, "allowed_provider_ids", tuple(sorted(allowed)))

    def to_dict(self) -> dict[str, object]:
        return {"allowed_provider_ids": list(self.allowed_provider_ids),
                "maximum_providers": self.maximum_providers,
                "require_all_registered": self.require_all_registered}


@dataclass(frozen=True, slots=True)
class DiscoveryContextSelection:
    request_id: str
    result_id: str
    registry_manifest_id: str
    policy: DiscoveryContextSelectionPolicy
    provider_ids: tuple[str, ...]
    ranker_ids: tuple[str, ...]
    authorities: tuple[DiscoveredContextAuthority, ...]
    excluded_provider_ids: tuple[str, ...] = ()
    selection_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policy, DiscoveryContextSelectionPolicy):
            raise ValueError("selection requires a discovery context policy")
        for name in ("request_id", "result_id", "registry_manifest_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in ("provider_ids", "ranker_ids", "excluded_provider_ids"):
            values = tuple(getattr(self, name))
            if (any(not isinstance(value, str) or not value.strip() or
                    value != value.strip() for value in values) or
                    len(values) != len(set(values))):
                raise ValueError(f"{name} must be canonical and unique")
            object.__setattr__(self, name, values)
        if not self.provider_ids:
            raise ValueError("selection requires at least one context provider")
        if len(self.provider_ids) > self.policy.maximum_providers:
            raise ValueError("selection exceeds its provider policy")
        if set(self.provider_ids) & set(self.excluded_provider_ids):
            raise ValueError("selected and excluded providers must be disjoint")
        if (self.policy.allowed_provider_ids and
                not set(self.provider_ids).issubset(
                    set(self.policy.allowed_provider_ids))):
            raise ValueError("selection violates its provider allowlist")
        authorities = tuple(self.authorities)
        if (not authorities or
                not all(isinstance(value, DiscoveredContextAuthority)
                        for value in authorities) or
                len(authorities) != len(set(authorities))):
            raise ValueError("selection requires unique discovered authorities")
        object.__setattr__(self, "authorities", tuple(sorted(authorities)))
        if any(value.provider_id not in self.provider_ids for value in authorities):
            raise ValueError("discovered authority belongs to an unselected provider")
        identity = {
            "request_id": self.request_id, "result_id": self.result_id,
            "registry_manifest_id": self.registry_manifest_id,
            "policy": self.policy.to_dict(),
            "provider_ids": list(self.provider_ids),
            "ranker_ids": list(self.ranker_ids),
            "authorities": [{"provider_id": value.provider_id,
                             "item_id": value.item_id,
                             "source": value.source,
                             "revision": value.revision}
                            for value in self.authorities],
            "excluded_provider_ids": list(self.excluded_provider_ids),
        }
        object.__setattr__(self, "selection_id", _hash("dcs_", identity))


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

    def manifest_id(self) -> str:
        return _hash("ctxreg_", [
            {"role": value.role, "component_id": value.component_id}
            for value in self.manifest()])

    def select_discovery_result(
            self, result: DiscoveryResult, *, ranker_ids: Sequence[str] = (),
            policy: DiscoveryContextSelectionPolicy = DiscoveryContextSelectionPolicy(),
            ) -> DiscoveryContextSelection:
        """Bind discovered context authorities to this exact registry manifest.

        Selection projects identities only. It never copies item text or treats
        the registry as the authority for the discovery result.
        """
        if not isinstance(result, DiscoveryResult):
            raise ValueError("selection requires a DiscoveryResult")
        if not isinstance(policy, DiscoveryContextSelectionPolicy):
            raise ValueError("selection requires a DiscoveryContextSelectionPolicy")
        selected_ranker_ids, _ = self._select(
            self._rankers, ranker_ids, "ranker", allow_empty=True)
        discovered = tuple(sorted({value.item.provider for value in result.context}))
        allowed = set(policy.allowed_provider_ids) if policy.allowed_provider_ids else None
        policy_excluded = tuple(value for value in discovered
                                if allowed is not None and value not in allowed)
        eligible = tuple(value for value in discovered
                         if allowed is None or value in allowed)
        unregistered = tuple(value for value in eligible
                             if value not in self._providers)
        if unregistered and policy.require_all_registered:
            raise ValueError("unregistered discovered context provider: " +
                             ", ".join(unregistered))
        selected = tuple(value for value in eligible if value in self._providers)
        if not selected:
            raise ValueError("discovery result selects no registered context provider")
        if len(selected) > policy.maximum_providers:
            raise ValueError("discovery selection exceeds provider limit")
        excluded = tuple(sorted((*policy_excluded, *unregistered)))
        authorities = tuple(sorted({
            DiscoveredContextAuthority.from_item(value.item)
            for value in result.context if value.item.provider in selected}))
        return DiscoveryContextSelection(
            request_id=result.request.request_id, result_id=result.result_id,
            registry_manifest_id=self.manifest_id(), policy=policy,
            provider_ids=selected, ranker_ids=selected_ranker_ids,
            authorities=authorities, excluded_provider_ids=excluded)

    async def compose_selection(
            self, selection: DiscoveryContextSelection, query: str, *,
            limit_per_provider: int, budget_tokens: int,
            cancellation: ContextCancellation | None = None,
            ) -> ContextComposition:
        """Compose with a selection only while its registry snapshot is exact."""
        if not isinstance(selection, DiscoveryContextSelection):
            raise ValueError("selection must be DiscoveryContextSelection")
        if selection.registry_manifest_id != self.manifest_id():
            raise ValueError("context registry changed after discovery selection")
        return await self.compose(
            query, provider_ids=selection.provider_ids,
            ranker_ids=selection.ranker_ids,
            limit_per_provider=limit_per_provider, budget_tokens=budget_tokens,
            cancellation=cancellation)

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
        remaining: dict[tuple, list[ContextItem]] = {}
        for item in before:
            remaining.setdefault(cls._authority(item), []).append(item)
        for item in after:
            candidates = remaining.get(cls._authority(item), [])
            matches = [candidate for candidate in candidates
                       if item.ranking_evidence[:len(candidate.ranking_evidence)]
                       == candidate.ranking_evidence
                       and (item.score == candidate.score or
                            len(item.ranking_evidence)
                            > len(candidate.ranking_evidence))]
            if not matches:
                raise ValueError("ranker changed authoritative context or evidence lineage")
            matched = max(matches, key=lambda value: len(value.ranking_evidence))
            candidates.remove(matched)
        if any(remaining.values()):
            raise ValueError("ranker changed authoritative context or evidence lineage")

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
