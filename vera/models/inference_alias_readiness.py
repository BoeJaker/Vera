"""Payload-free parity evidence and fail-closed alias activation readiness."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .inference_alias_route import InferenceAliasRoute
from .model_package import _identifier

INFERENCE_ALIAS_PARITY_SCHEMA = "vera.inference-alias-parity-evidence/v1"
INFERENCE_ALIAS_READINESS_SCHEMA = "vera.inference-alias-readiness/v1"
REQUIRED_ALIAS_PARITY_SCENARIOS = (
    "nonstream", "stream", "cancellation", "outage", "rollback")


def _hash(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _commit(value: str) -> str:
    value = str(value or "").lower()
    if len(value) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("source commit must be a full hexadecimal commit ID")
    return value


@dataclass(frozen=True, slots=True)
class InferenceAliasParityEvidence:
    route_id: str
    scenario: str
    source_commit: str
    observed_at_ms: int
    passed: bool
    evidence_refs: tuple[str, ...]
    failure_codes: tuple[str, ...] = ()
    evidence_id: str = field(init=False)
    schema: str = INFERENCE_ALIAS_PARITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INFERENCE_ALIAS_PARITY_SCHEMA:
            raise ValueError("unsupported inference alias parity schema")
        object.__setattr__(self, "route_id", _identifier(self.route_id, "route ID"))
        if self.scenario not in REQUIRED_ALIAS_PARITY_SCENARIOS:
            raise ValueError("unsupported inference alias parity scenario")
        object.__setattr__(self, "source_commit", _commit(self.source_commit))
        if isinstance(self.observed_at_ms, bool) or not isinstance(
                self.observed_at_ms, int) or self.observed_at_ms < 0:
            raise ValueError("observed_at_ms must be a non-negative Unix millisecond")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean")
        refs = tuple(sorted({_identifier(value, "evidence reference")
                             for value in self.evidence_refs}))
        if not refs or len(refs) > 16:
            raise ValueError("evidence references must be present and bounded")
        object.__setattr__(self, "evidence_refs", refs)
        failures = tuple(sorted({_identifier(value, "failure code")
                                 for value in self.failure_codes}))
        if len(failures) > 16 or self.passed == bool(failures):
            raise ValueError("failed evidence requires failure codes; passing evidence forbids them")
        object.__setattr__(self, "failure_codes", failures)
        object.__setattr__(self, "evidence_id", _hash(
            "iaparity_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "route_id": self.route_id,
            "scenario": self.scenario, "source_commit": self.source_commit,
            "observed_at_ms": self.observed_at_ms, "passed": self.passed,
            "evidence_refs": list(self.evidence_refs),
            "failure_codes": list(self.failure_codes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **self.identity_dict()}


def inference_alias_parity_evidence_from_dict(
        value: Mapping[str, Any]) -> InferenceAliasParityEvidence:
    if not isinstance(value, Mapping) or value.get("schema") != INFERENCE_ALIAS_PARITY_SCHEMA:
        raise ValueError("unsupported inference alias parity schema")
    try:
        evidence = InferenceAliasParityEvidence(
            route_id=value["route_id"], scenario=value["scenario"],
            source_commit=value["source_commit"],
            observed_at_ms=value["observed_at_ms"], passed=value["passed"],
            evidence_refs=tuple(value["evidence_refs"]),
            failure_codes=tuple(value.get("failure_codes") or ()))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed inference alias parity evidence") from exc
    if value.get("evidence_id") != evidence.evidence_id:
        raise ValueError("inference alias parity identity does not match content")
    return evidence


class SQLiteInferenceAliasParityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inference_alias_parity_evidence (
                    evidence_id TEXT PRIMARY KEY, route_id TEXT NOT NULL,
                    scenario TEXT NOT NULL, source_commit TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL, evidence_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS inference_alias_parity_latest
                    ON inference_alias_parity_evidence
                    (route_id, source_commit, scenario, observed_at_ms DESC,
                     evidence_id DESC);
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    def record(self, evidence: InferenceAliasParityEvidence) -> InferenceAliasParityEvidence:
        if not isinstance(evidence, InferenceAliasParityEvidence):
            raise TypeError("evidence must be InferenceAliasParityEvidence")
        encoded = json.dumps(evidence.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT evidence_json FROM inference_alias_parity_evidence "
                "WHERE evidence_id=?", (evidence.evidence_id,)).fetchone()
            if row and row[0] != encoded:
                raise ValueError("inference alias parity evidence identity collision")
            conn.execute(
                "INSERT OR IGNORE INTO inference_alias_parity_evidence "
                "VALUES (?,?,?,?,?,?)", (evidence.evidence_id, evidence.route_id,
                evidence.scenario, evidence.source_commit,
                evidence.observed_at_ms, encoded))
        return evidence

    def latest(self, route_id: str, source_commit: str
               ) -> tuple[InferenceAliasParityEvidence, ...]:
        route_id = _identifier(route_id, "route ID")
        source_commit = _commit(source_commit)
        with self._connect() as conn:
            rows = []
            for scenario in REQUIRED_ALIAS_PARITY_SCENARIOS:
                row = conn.execute(
                    "SELECT evidence_json FROM inference_alias_parity_evidence "
                    "WHERE route_id=? AND source_commit=? AND scenario=? "
                    "ORDER BY observed_at_ms DESC,evidence_id DESC LIMIT 1",
                    (route_id, source_commit, scenario)).fetchone()
                if row:
                    rows.append(row)
        return tuple(inference_alias_parity_evidence_from_dict(json.loads(row[0]))
                     for row in rows)


@dataclass(frozen=True, slots=True)
class InferenceAliasReadiness:
    route_id: str
    source_commit: str
    as_of_ms: int
    ready_for_activation_review: bool
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    traffic_activation_allowed: bool = False
    schema: str = INFERENCE_ALIAS_READINESS_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "route_id": self.route_id,
            "source_commit": self.source_commit, "as_of_ms": self.as_of_ms,
            "ready_for_activation_review": self.ready_for_activation_review,
            "traffic_activation_allowed": self.traffic_activation_allowed,
            "reasons": list(self.reasons), "evidence_ids": list(self.evidence_ids),
        }


def evaluate_inference_alias_readiness(
        route: InferenceAliasRoute, store: SQLiteInferenceAliasParityStore, *,
        source_commit: str, as_of_ms: int, max_age_ms: int,
        ) -> InferenceAliasReadiness:
    """Classify bounded evidence; never activate, resolve, or execute a route."""
    if not isinstance(route, InferenceAliasRoute):
        raise TypeError("route must be InferenceAliasRoute")
    if not isinstance(store, SQLiteInferenceAliasParityStore):
        raise TypeError("store must be SQLiteInferenceAliasParityStore")
    source_commit = _commit(source_commit)
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int) or as_of_ms < 0:
        raise ValueError("as_of_ms must be a non-negative Unix millisecond")
    if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) \
            or max_age_ms < 1:
        raise ValueError("max_age_ms must be positive")
    evidence = {item.scenario: item for item in store.latest(
        route.route_id, source_commit)}
    reasons: list[str] = []
    for scenario in REQUIRED_ALIAS_PARITY_SCENARIOS:
        item = evidence.get(scenario)
        if item is None:
            reasons.append(f"missing_{scenario}")
        elif item.observed_at_ms > as_of_ms:
            reasons.append(f"future_{scenario}")
        elif as_of_ms - item.observed_at_ms > max_age_ms:
            reasons.append(f"stale_{scenario}")
        elif not item.passed:
            reasons.append(f"failed_{scenario}")
    evidence_ids = tuple(evidence[key].evidence_id for key in sorted(evidence))
    return InferenceAliasReadiness(
        route.route_id, source_commit, as_of_ms, not reasons, tuple(reasons),
        evidence_ids, False)
