"""Frozen, non-executing discovery/context cooperation evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from vera.context_provider import ContextCitation
    from vera.discovery_benchmark import (
        BenchmarkAuthority, BenchmarkHit, ContextBenchmarkCase,
        ContextBenchmarkFixture, ContextBenchmarkObservation,
        ContextBenchmarkVariant, compare_context_benchmark,
    )
    from vera.discovery_contract import DiscoveryRequest
    from vera.fabric.dataset_provider import DatasetSnapshot
except ImportError:  # pragma: no cover - deployed package spelling
    from Vera.vera.context_provider import ContextCitation
    from Vera.vera.discovery_benchmark import (
        BenchmarkAuthority, BenchmarkHit, ContextBenchmarkCase,
        ContextBenchmarkFixture, ContextBenchmarkObservation,
        ContextBenchmarkVariant, compare_context_benchmark,
    )
    from Vera.vera.discovery_contract import DiscoveryRequest
    from Vera.vera.fabric.dataset_provider import DatasetSnapshot


CORPUS_SCHEMA = "vera.discovery-cooperation-corpus/v1"
REPORT_SCHEMA = "vera.discovery-cooperation-evaluation/v1"
POLICY = {
    "synthetic_only": True,
    "network_calls": False,
    "model_calls": False,
    "capability_execution": False,
    "writes": False,
    "production_decision": False,
}
MAX_CASES = 64
MAX_REPETITIONS = 20


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_corpus(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("corpus_root_must_be_object")
    return value


def validate_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if corpus.get("schema") != CORPUS_SCHEMA:
        issues.append({"code": "schema.invalid", "path": "schema"})
    policy = corpus.get("policy")
    if not isinstance(policy, Mapping) or any(policy.get(k) is not v
                                               for k, v in POLICY.items()):
        issues.append({"code": "policy.nonexecuting_required", "path": "policy"})
    repetitions = corpus.get("repetitions")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) \
            or not 1 <= repetitions <= MAX_REPETITIONS:
        issues.append({"code": "repetitions.invalid", "path": "repetitions"})
        repetitions = 0
    variants = corpus.get("variants")
    if not isinstance(variants, list) or len(variants) != 2:
        issues.append({"code": "variants.two_required", "path": "variants"})
        variants = []
    variant_ids = [str(row.get("id") or "") for row in variants
                   if isinstance(row, Mapping)]
    if len(variant_ids) != 2 or len(set(variant_ids)) != 2:
        issues.append({"code": "variants.invalid", "path": "variants"})
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        issues.append({"code": "cases.invalid", "path": "cases"})
        cases = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        path = f"cases.{index}"
        if not isinstance(case, Mapping):
            issues.append({"code": "case.invalid", "path": path})
            continue
        case_id = str(case.get("id") or "")
        if not case_id or case_id in seen:
            issues.append({"code": "case.id_invalid", "path": f"{path}.id"})
        seen.add(case_id)
        for field in ("query", "relevant_sources", "relevant_authorities",
                      "required_claims", "variants"):
            if field not in case:
                issues.append({"code": f"case.{field}_required", "path": f"{path}.{field}"})
        rows = case.get("variants")
        if not isinstance(rows, Mapping) or set(rows) != set(variant_ids):
            issues.append({"code": "case.variant_matrix_invalid", "path": f"{path}.variants"})
            continue
        for variant_id, row in rows.items():
            if not isinstance(row, Mapping):
                issues.append({"code": "observation.invalid",
                               "path": f"{path}.variants.{variant_id}"})
                continue
            for timing in ("scout_ms", "first_useful_context_ms", "end_to_end_ms"):
                values = row.get(timing)
                if not isinstance(values, list) or len(values) != repetitions \
                        or any(isinstance(value, bool) or not isinstance(value, int)
                               or value < 0 for value in values):
                    issues.append({"code": f"observation.{timing}_invalid",
                                   "path": f"{path}.variants.{variant_id}.{timing}"})
    return {"schema": REPORT_SCHEMA, "ok": not issues, "issues": issues,
            "case_count": len(cases), "fingerprint": _fingerprint(corpus)}


def _build_fixture(corpus: Mapping[str, Any]) -> ContextBenchmarkFixture:
    dataset = corpus["dataset"]
    snapshot = DatasetSnapshot.create(
        dataset_id=dataset["id"], created_at=dataset["created_at"],
        records=tuple(dataset["records"]), schema=dict(dataset["record_schema"]),
        provenance=dict(dataset["provenance"]),
    )[0]
    variants = tuple(ContextBenchmarkVariant(
        row["id"], row["revision"], _sha(row["configuration"]),
        tuple(row.get("ablations") or ())) for row in corpus["variants"])
    cases: list[ContextBenchmarkCase] = []
    observations: list[ContextBenchmarkObservation] = []
    repetitions = int(corpus["repetitions"])
    for raw in corpus["cases"]:
        request = DiscoveryRequest(
            query=raw["query"], requester="benchmark.discovery.frozen",
            tenant_id="vera.fixture", namespace="discovery.cooperation",
            as_of=dataset["created_at"], source_kinds=("dataset", "web"),
            max_sources=8, max_context_items=16, timeout_ms=10_000,
            max_bytes=1_000_000, max_cost_units=100)
        case = ContextBenchmarkCase(
            case_key=raw["id"], request=request, query_digest=_sha(raw["query"]),
            snapshot_id=snapshot.snapshot_id,
            relevant_sources=tuple(raw["relevant_sources"]),
            relevant_authorities=tuple(BenchmarkAuthority(row["identity"], row["revision"])
                                       for row in raw["relevant_authorities"]),
            required_claim_ids=tuple(_sha(value) for value in raw["required_claims"]),
        )
        cases.append(case)
        for variant in variants:
            row = raw["variants"][variant.variant_id]
            hits = tuple(BenchmarkHit(
                BenchmarkAuthority(hit["identity"], hit["revision"]),
                (ContextCitation("fixture." + raw["id"],
                                 "fixture://" + raw["id"] + "/" + str(index)),),
            ) for index, hit in enumerate(row["hits"]))
            claims = tuple(_sha(value) for value in row["supported_claims"])
            for repetition in range(1, repetitions + 1):
                observations.append(ContextBenchmarkObservation(
                    case_id=case.case_id, variant_id=variant.variant_id,
                    repetition=repetition, status="completed",
                    discovered_sources=tuple(row["discovered_sources"]),
                    selected_sources=tuple(row["selected_sources"]), hits=hits,
                    supported_claim_ids=claims,
                    scout_ms=row["scout_ms"][repetition - 1],
                    first_useful_context_ms=row["first_useful_context_ms"][repetition - 1],
                    end_to_end_ms=row["end_to_end_ms"][repetition - 1],
                    byte_count=int(row["byte_count"]), cost_units=int(row["cost_units"]),
                    cpu_ms=int(row["cpu_ms"]), gpu_ms=0,
                ))
    return ContextBenchmarkFixture(snapshot, tuple(cases), variants,
                                   tuple(observations), repetitions)


def evaluate_corpus(corpus: Mapping[str, Any]) -> dict[str, Any]:
    validation = validate_corpus(corpus)
    if not validation["ok"]:
        return {**validation, "deterministic": False, "comparison": None,
                "decision_scope": "frozen_fixture_conformance",
                "production_ready": False, "authority_changed": False}
    try:
        first = _build_fixture(corpus)
        baseline, candidate = (row["id"] for row in corpus["variants"])
        comparison = compare_context_benchmark(first, baseline, candidate)
        repeated = compare_context_benchmark(
            _build_fixture(corpus), baseline, candidate)
    except (KeyError, TypeError, ValueError) as exc:
        return {**validation, "ok": False, "deterministic": False,
                "issues": validation["issues"] + [{
                    "code": "corpus.contract_invalid", "path": type(exc).__name__}],
                "comparison": None, "decision_scope": "frozen_fixture_conformance",
                "production_ready": False, "authority_changed": False}
    deterministic = comparison == repeated
    expected = corpus.get("expected") or {}
    expected_match = (comparison["passed"] is expected.get("passed") and
                      comparison["blockers"] == expected.get("blockers"))
    return {**validation, "ok": bool(deterministic and expected_match),
            "deterministic": deterministic, "expected_match": expected_match,
            "comparison": comparison,
            "decision_scope": "frozen_fixture_conformance",
            "production_ready": False, "authority_changed": False,
            "providers_invoked": False, "effects": "none"}
