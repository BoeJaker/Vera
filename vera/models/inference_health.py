"""Bounded, externally observed health evidence for inference providers."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .model_package import _identifier

INFERENCE_HEALTH_SCHEMA = "vera.inference-provider-health/v1"
MAX_PROVIDER_LOAD = 1_000_000
MAX_HEALTH_VALIDITY_MS = 86_400_000
_STATES = {"ready", "degraded", "unavailable", "draining", "unknown"}


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= MAX_PROVIDER_LOAD:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class InferenceProviderHealth:
    """A probe result, not a probe.

    Observation and expiry times are caller-supplied Unix milliseconds so
    registry decisions can be replayed without consulting a wall clock.
    """

    provider_id: str
    state: str
    observed_at_ms: int
    valid_until_ms: int
    source: str
    available_package_ids: tuple[str, ...] = ()
    concurrency_limit: int = 0
    in_flight: int = 0
    queue_depth: int = 0
    latency_ms: int | None = None
    revision: int = 1
    evidence_id: str = field(init=False)
    schema: str = INFERENCE_HEALTH_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _identifier(
            self.provider_id, "provider ID"))
        object.__setattr__(self, "source", _identifier(
            self.source, "health evidence source"))
        if self.state not in _STATES:
            raise ValueError("unsupported inference provider health state")
        for name in ("observed_at_ms", "valid_until_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative Unix millisecond")
        if self.valid_until_ms < self.observed_at_ms:
            raise ValueError("health evidence validity ends before observation")
        if self.valid_until_ms - self.observed_at_ms > MAX_HEALTH_VALIDITY_MS:
            raise ValueError("health evidence validity window exceeds its limit")
        packages = tuple(sorted({_identifier(value, "available package ID")
                                 for value in self.available_package_ids}))
        if len(packages) > 4096:
            raise ValueError("available package IDs exceed their limit")
        if self.state == "ready" and not packages:
            raise ValueError("ready health evidence requires an available package")
        object.__setattr__(self, "available_package_ids", packages)
        for name in ("concurrency_limit", "in_flight", "queue_depth"):
            object.__setattr__(self, name, _count(getattr(self, name), name))
        if self.in_flight > self.concurrency_limit:
            raise ValueError("in_flight exceeds the declared concurrency limit")
        if self.latency_ms is not None:
            object.__setattr__(self, "latency_ms", _count(
                self.latency_ms, "latency_ms"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) \
                or self.revision < 1:
            raise ValueError("health evidence revision must be positive")
        canonical = json.dumps(self.identity_dict(), sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False)
        object.__setattr__(self, "evidence_id", "ihealth_" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest())

    @property
    def available_slots(self) -> int:
        return self.concurrency_limit - self.in_flight

    def is_current(self, as_of_ms: int) -> bool:
        if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) \
                or as_of_ms < 0:
            raise ValueError("as_of_ms must be a non-negative Unix millisecond")
        return self.observed_at_ms <= as_of_ms <= self.valid_until_ms

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "provider_id": self.provider_id,
            "state": self.state, "observed_at_ms": self.observed_at_ms,
            "valid_until_ms": self.valid_until_ms, "source": self.source,
            "available_package_ids": list(self.available_package_ids),
            "concurrency_limit": self.concurrency_limit,
            "in_flight": self.in_flight, "queue_depth": self.queue_depth,
            "latency_ms": self.latency_ms, "revision": self.revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id,
                "available_slots": self.available_slots, **self.identity_dict()}


def inference_provider_health_from_dict(
        value: Mapping[str, Any]) -> InferenceProviderHealth:
    if not isinstance(value, Mapping) or value.get("schema") != INFERENCE_HEALTH_SCHEMA:
        raise ValueError("unsupported inference provider health schema")
    try:
        health = InferenceProviderHealth(
            provider_id=value["provider_id"], state=value["state"],
            observed_at_ms=value["observed_at_ms"],
            valid_until_ms=value["valid_until_ms"], source=value["source"],
            available_package_ids=tuple(value.get("available_package_ids") or ()),
            concurrency_limit=value.get("concurrency_limit", 0),
            in_flight=value.get("in_flight", 0),
            queue_depth=value.get("queue_depth", 0),
            latency_ms=value.get("latency_ms"), revision=value.get("revision", 1))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference provider health evidence") from exc
    if value.get("evidence_id") != health.evidence_id:
        raise ValueError("inference health evidence identity does not match content")
    if "available_slots" in value and value["available_slots"] != health.available_slots:
        raise ValueError("inference health available slots do not match load facts")
    return health
