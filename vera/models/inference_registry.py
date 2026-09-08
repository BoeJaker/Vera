"""Deterministic discovery and explicit resolution for inference providers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .inference_contracts import (
    InferenceContractConflict, InferenceProvider, InferenceRequest)
from .inference_health import InferenceProviderHealth
from .model_package import _identifier
from .training_contracts import ProviderProfile

INFERENCE_PROVIDER_DESCRIPTOR_SCHEMA = "vera.inference-provider-descriptor/v2"
_STATES = {"ready", "degraded", "unavailable", "draining", "unknown"}


@dataclass(frozen=True, slots=True)
class InferenceProviderDescriptor:
    provider_id: str
    package_ids: tuple[str, ...]
    tasks: tuple[str, ...]
    placements: tuple[str, ...] = ()
    state: str = "unknown"
    health: InferenceProviderHealth | None = None
    revision: int = 1
    schema: str = INFERENCE_PROVIDER_DESCRIPTOR_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _identifier(
            self.provider_id, "provider ID"))
        for name in ("package_ids", "tasks", "placements"):
            values = tuple(sorted({_identifier(value, name)
                                   for value in getattr(self, name)}))
            if name in {"package_ids", "tasks"} and not values:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, values)
        if self.state not in _STATES:
            raise ValueError("unsupported inference provider state")
        if self.health is not None:
            if not isinstance(self.health, InferenceProviderHealth):
                raise TypeError("health must be InferenceProviderHealth")
            if self.health.provider_id != self.provider_id:
                raise ValueError("health evidence provider does not match descriptor")
            if self.health.state != self.state:
                raise ValueError("descriptor state does not match health evidence")
            if not frozenset(self.health.available_package_ids) <= frozenset(
                    self.package_ids):
                raise ValueError("health evidence names an undeclared package")
        elif self.state != "unknown":
            raise ValueError("provider readiness requires health evidence")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) \
                or self.revision < 1:
            raise ValueError("provider descriptor revision must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "provider_id": self.provider_id,
                "package_ids": list(self.package_ids), "tasks": list(self.tasks),
                "placements": list(self.placements), "state": self.state,
                "health": self.health.to_dict() if self.health else None,
                "revision": self.revision}


@dataclass(frozen=True, slots=True)
class _Entry:
    provider: InferenceProvider
    descriptor: InferenceProviderDescriptor


class InferenceProviderRegistry:
    """Catalog providers without choosing routes, retrying, or probing health."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, provider: InferenceProvider, *, package_ids: tuple[str, ...],
                 placements: tuple[str, ...] = (),
                 health: InferenceProviderHealth | None = None,
                 expected_revision: int = 0) -> InferenceProviderDescriptor:
        profile = self._profile(provider)
        current = self._entries.get(profile.provider_id)
        current_revision = current.descriptor.revision if current else 0
        if expected_revision != current_revision:
            raise InferenceContractConflict(
                "inference provider registration revision conflict")
        descriptor = InferenceProviderDescriptor(
            profile.provider_id, package_ids, profile.capabilities, placements,
            health.state if health else "unknown", health, current_revision + 1)
        self._entries[profile.provider_id] = _Entry(provider, descriptor)
        return descriptor

    def remove(self, provider_id: str, *, expected_revision: int) -> None:
        provider_id = _identifier(provider_id, "provider ID")
        current = self._entries.get(provider_id)
        if current is None:
            raise KeyError("inference provider is not registered")
        if expected_revision != current.descriptor.revision:
            raise InferenceContractConflict(
                "inference provider registration revision conflict")
        del self._entries[provider_id]

    def get(self, provider_id: str) -> InferenceProviderDescriptor | None:
        provider_id = _identifier(provider_id, "provider ID")
        entry = self._entries.get(provider_id)
        return entry.descriptor if entry else None

    def list(self) -> tuple[InferenceProviderDescriptor, ...]:
        return tuple(self._entries[key].descriptor for key in sorted(self._entries))

    def candidates(self, request: InferenceRequest, *,
                   placements: tuple[str, ...] = (),
                   include_unavailable: bool = False,
                   as_of_ms: int | None = None,
                   ) -> tuple[InferenceProviderDescriptor, ...]:
        if not isinstance(request, InferenceRequest):
            raise TypeError("request must be InferenceRequest")
        required = frozenset(_identifier(value, "required placement")
                             for value in placements)
        found = []
        for descriptor in self.list():
            if request.model_package_id not in descriptor.package_ids \
                    or request.task not in descriptor.tasks \
                    or not required <= frozenset(descriptor.placements):
                continue
            if not include_unavailable:
                health = descriptor.health
                if (health is None or descriptor.state != "ready"
                        or as_of_ms is None or not health.is_current(as_of_ms)
                        or request.model_package_id not in
                        health.available_package_ids):
                    continue
            found.append(descriptor)
        return tuple(found)

    def resolve(self, request: InferenceRequest, provider_id: str, *,
                placements: tuple[str, ...] = (),
                require_ready: bool = True,
                as_of_ms: int | None = None) -> InferenceProvider:
        """Return only the caller's explicit compatible provider selection."""
        provider_id = _identifier(provider_id, "provider ID")
        entry = self._entries.get(provider_id)
        if entry is None:
            raise KeyError("inference provider is not registered")
        candidates = self.candidates(
            request, placements=placements,
            include_unavailable=not require_ready, as_of_ms=as_of_ms)
        if entry.descriptor not in candidates:
            raise InferenceContractConflict(
                "selected inference provider is not an eligible candidate")
        return entry.provider

    @staticmethod
    def _profile(provider: InferenceProvider) -> ProviderProfile:
        if not isinstance(provider, InferenceProvider):
            raise TypeError("provider must implement InferenceProvider")
        profile = provider.profile()
        if not isinstance(profile, ProviderProfile) or profile.kind != "inference":
            raise InferenceContractConflict("provider profile is not inference")
        return profile
