"""Strict, payload-free import of frozen third-party evaluation projections."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .evaluation_evidence import (
    CaseEvaluationEvidence, EvaluationCaseIdentity, EvaluationUsage,
    JudgeProvenance, PartialEvaluationReport, evaluation_case_identity_from_dict,
    evaluation_usage_from_dict, judge_provenance_from_dict)
from .model_package import _identifier
from .training_contracts import (
    EvaluationRequest, MetricResult, evaluation_request_from_dict)
from ..fabric.retrieval_comparison import RetrievalCitation


MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_IMPORT_CASES = 100_000
_SCHEMAS = {
    "deepeval": "vera.deepeval-frozen-projection/v1",
    "promptfoo": "vera.promptfoo-frozen-projection/v1",
    "ragas": "vera.ragas-frozen-projection/v1",
}
_PROVIDERS = {
    "deepeval": "deepeval-frozen-import/v1",
    "promptfoo": "promptfoo-frozen-import/v1",
    "ragas": "ragas-frozen-import/v1",
}
_FORBIDDEN_KEYS = {
    "input", "actual_output", "expected_output", "prompt", "prompts",
    "output", "outputs", "response", "responses", "reason", "reasons",
    "config", "env", "vars", "variables", "messages", "error_detail",
    "stack", "stacktrace", "traceback",
}


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {sorted(unknown)}")


def _scan_payload_keys(value: Any, path: str = "projection") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"projection keys must be strings at {path}")
            if key.lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"payload-bearing field is forbidden at {path}.{key}")
            _scan_payload_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_payload_keys(item, f"{path}[{index}]")


@dataclass(frozen=True)
class EvaluationCitationBinding:
    case_id: str
    citations: tuple[RetrievalCitation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _identifier(self.case_id, "case ID"))
        citations = tuple(sorted(self.citations))
        if not citations or not all(isinstance(item, RetrievalCitation)
                                    for item in citations):
            raise ValueError("citation bindings require exact citations")
        if len(set(citations)) != len(citations):
            raise ValueError("citation bindings must not contain duplicates")
        object.__setattr__(self, "citations", citations)

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id,
                "citations": [item.to_dict() for item in self.citations]}


@dataclass(frozen=True)
class ExternalEvaluationImportReceipt:
    source_kind: str
    source_version: str
    export_digest: str
    report: PartialEvaluationReport
    citation_bindings: tuple[EvaluationCitationBinding, ...] = ()

    def __post_init__(self) -> None:
        bindings = tuple(sorted(self.citation_bindings, key=lambda item: item.case_id))
        if not all(isinstance(item, EvaluationCitationBinding) for item in bindings):
            raise TypeError("citation_bindings must contain EvaluationCitationBinding values")
        if len({item.case_id for item in bindings}) != len(bindings):
            raise ValueError("citation binding case IDs must be unique")
        if self.source_kind == "ragas" and not bindings:
            raise ValueError("Ragas receipts require citation bindings")
        if self.source_kind != "ragas" and bindings:
            raise ValueError("only Ragas receipts may contain citation bindings")
        object.__setattr__(self, "citation_bindings", bindings)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": "vera.external-evaluation-import-receipt/v1",
            "source_kind": self.source_kind,
            "source_version": self.source_version,
            "export_digest": self.export_digest,
            "report": self.report.to_dict(),
            "effect": "none", "provider_invoked": False,
            "payloads_retained": False,
        }
        if self.citation_bindings:
            value["citation_bindings"] = [item.to_dict() for item in self.citation_bindings]
        return value


def _metric(value: Mapping[str, Any]) -> MetricResult:
    if not isinstance(value, Mapping):
        raise ValueError("metric entries must be objects")
    _keys(value, {"metric_id", "value", "threshold", "direction"}, "metric")
    try:
        return MetricResult(
            value["metric_id"], value["value"], value["threshold"],
            value.get("direction", "maximize"))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed metric entry") from exc


def _result(value: Mapping[str, Any], cases: Mapping[str, EvaluationCaseIdentity],
            request: EvaluationRequest) -> CaseEvaluationEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("result entries must be objects")
    _keys(value, {"case_id", "status", "metrics", "judge", "usage", "error_code"},
          "result")
    case_id = str(value.get("case_id") or "")
    if case_id not in cases:
        raise ValueError("result case ID is not in the expected case set")
    status = value.get("status")
    if status not in {"completed", "failed", "skipped"}:
        raise ValueError("unsupported imported result status")
    metrics = tuple(_metric(item) for item in value.get("metrics") or ())
    if status == "completed" and {item.metric_id for item in metrics} != set(request.metric_ids):
        raise ValueError("completed imported results must report every requested metric")
    judge = value.get("judge")
    if not isinstance(judge, Mapping):
        raise ValueError("imported result requires judge provenance")
    _keys(judge, {"scorer_id", "scorer_version", "kind", "provider_id",
                  "model_package_id", "prompt_package_id", "configuration_digest"},
          "judge")
    usage = value.get("usage") or {}
    if not isinstance(usage, Mapping):
        raise ValueError("imported result usage must be an object")
    _keys(usage, {"input_tokens", "output_tokens", "cost_microunits", "latency_ms"},
          "usage")
    try:
        return CaseEvaluationEvidence(
            case=cases[case_id], status=status,
            provenance=judge_provenance_from_dict(judge), metrics=metrics,
            usage=evaluation_usage_from_dict(usage),
            error_code=value.get("error_code", ""))
    except KeyError as exc:
        raise ValueError("imported result requires judge provenance") from exc


def import_external_evaluation_projection(
        source_kind: str, projection: Mapping[str, Any]) -> ExternalEvaluationImportReceipt:
    """Normalize an already-produced safe projection; never runs the source tool."""
    if source_kind not in _SCHEMAS:
        raise ValueError("unsupported external evaluation source")
    if not isinstance(projection, Mapping):
        raise TypeError("projection must be an object")
    _scan_payload_keys(projection)
    allowed = {"schema", "source_version", "request", "expected_cases", "results"}
    if source_kind == "ragas":
        allowed.add("citation_bindings")
    _keys(projection, allowed, "projection")
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_IMPORT_BYTES:
        raise ValueError("external evaluation projection exceeds byte limit")
    if projection.get("schema") != _SCHEMAS[source_kind]:
        raise ValueError("unsupported external evaluation projection schema")
    source_version = _identifier(projection.get("source_version"), "source version")
    request = evaluation_request_from_dict(projection.get("request") or {})
    raw_cases = projection.get("expected_cases") or ()
    raw_results = projection.get("results") or ()
    if not isinstance(raw_cases, (list, tuple)) or not 0 < len(raw_cases) <= MAX_IMPORT_CASES:
        raise ValueError("expected cases must contain 1..100000 entries")
    if not isinstance(raw_results, (list, tuple)) or not 0 < len(raw_results) <= len(raw_cases):
        raise ValueError("results must be a non-empty subset of expected cases")
    for item in raw_cases:
        if not isinstance(item, Mapping):
            raise ValueError("expected case entries must be objects")
        _keys(item, {"case_id", "dataset_revision_id", "case_key", "input_digest",
                     "expected_digest"}, "expected case")
    identities = tuple(evaluation_case_identity_from_dict(item) for item in raw_cases)
    if any(item.dataset_revision_id != request.dataset_revision_id for item in identities):
        raise ValueError("case dataset revisions must match the evaluation request")
    by_id = {item.case_id: item for item in identities}
    if len(by_id) != len(identities):
        raise ValueError("expected case identities must be unique")
    results = tuple(_result(item, by_id, request) for item in raw_results)
    if len({item.case.case_id for item in results}) != len(results):
        raise ValueError("imported result case IDs must be unique")
    status = "completed" if len(results) == len(identities) else "partial"
    report = PartialEvaluationReport(
        request=request, provider_id=_PROVIDERS[source_kind],
        expected_case_ids=tuple(by_id), cases=results, status=status)
    bindings: tuple[EvaluationCitationBinding, ...] = ()
    if source_kind == "ragas":
        raw_bindings = projection.get("citation_bindings")
        if not isinstance(raw_bindings, (list, tuple)) or len(raw_bindings) != len(by_id):
            raise ValueError("Ragas citation bindings must cover every expected case")
        parsed = []
        for value in raw_bindings:
            if not isinstance(value, Mapping):
                raise ValueError("citation binding entries must be objects")
            _keys(value, {"case_id", "citations"}, "citation binding")
            case_id = str(value.get("case_id") or "")
            if case_id not in by_id:
                raise ValueError("citation binding case ID is not expected")
            citations = value.get("citations")
            if not isinstance(citations, (list, tuple)):
                raise ValueError("citation bindings require citation arrays")
            for citation in citations:
                if not isinstance(citation, Mapping):
                    raise ValueError("citation entries must be objects")
                _keys(citation, {"record_id", "revision_id"}, "citation")
            parsed.append(EvaluationCitationBinding(
                case_id=case_id,
                citations=tuple(RetrievalCitation(**item) for item in citations)))
        if len({item.case_id for item in parsed}) != len(parsed):
            raise ValueError("Ragas citation binding case IDs must be unique")
        bindings = tuple(sorted(parsed, key=lambda item: item.case_id))
    return ExternalEvaluationImportReceipt(
        source_kind=source_kind, source_version=source_version,
        export_digest="sha256:" + hashlib.sha256(encoded).hexdigest(), report=report,
        citation_bindings=bindings)


def import_deepeval_projection(projection: Mapping[str, Any]) -> ExternalEvaluationImportReceipt:
    return import_external_evaluation_projection("deepeval", projection)


def import_promptfoo_projection(projection: Mapping[str, Any]) -> ExternalEvaluationImportReceipt:
    return import_external_evaluation_projection("promptfoo", projection)


def import_ragas_projection(projection: Mapping[str, Any]) -> ExternalEvaluationImportReceipt:
    return import_external_evaluation_projection("ragas", projection)
