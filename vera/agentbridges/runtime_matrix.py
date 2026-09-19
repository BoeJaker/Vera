"""Deterministic, non-executing comparison of agent runtime integration seams."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

SCHEMA = "vera.agent-runtime-matrix/v1"
LIVE_EVIDENCE_SCHEMA = "vera.agent-runtime-matrix-live-evidence/v1"
EVIDENCE_AS_OF = "2026-09-02"
DIMENSIONS = (
    "tools", "providers", "handoffs", "structured_output", "policy",
    "sessions", "recovery", "traces", "resources", "teardown",
    "streaming", "cancellation", "artifacts", "sandbox", "mcp",
)
UPSTREAM_STATES = {"supported", "partial", "unknown"}
VERA_STATES = {"supported", "partial", "not_integrated", "not_applicable"}
INTEGRATION_STATES = {"native", "shipped_bridge", "prospective", "compatibility_path"}
_ID = re.compile(r"[a-z][a-z0-9._-]{1,63}\Z")
_REASON = re.compile(r"[a-z][a-z0-9._-]{1,95}\Z")
LIVE_STATES = {"passed", "failed", "unavailable"}
_ADAPTER_DIMENSIONS = {
    "streaming": "stream",
    "cancellation": "cancellation",
    "resources": "resource_gates",
    "teardown": "teardown",
    "sandbox": "dependency_isolation",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded(value: str, name: str, maximum: int = 512) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must contain 1..{maximum} characters")
    return value


@dataclass(frozen=True)
class FeatureAssessment:
    dimension: str
    upstream: str
    vera: str
    evidence: str

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unknown runtime dimension: {self.dimension}")
        if self.upstream not in UPSTREAM_STATES:
            raise ValueError(f"invalid upstream state: {self.upstream}")
        if self.vera not in VERA_STATES:
            raise ValueError(f"invalid Vera state: {self.vera}")
        object.__setattr__(self, "evidence", _bounded(self.evidence, "feature evidence"))

    def to_dict(self) -> dict[str, str]:
        return {
            "dimension": self.dimension, "upstream": self.upstream,
            "vera": self.vera, "evidence": self.evidence,
        }


@dataclass(frozen=True)
class RuntimeCandidate:
    runtime_id: str
    label: str
    integration: str
    upstream_url: str
    package_refs: tuple[str, ...]
    assessments: tuple[FeatureAssessment, ...]
    gaps: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.runtime_id):
            raise ValueError("runtime_id must be a bounded lowercase identifier")
        object.__setattr__(self, "label", _bounded(self.label, "runtime label", 96))
        if self.integration not in INTEGRATION_STATES:
            raise ValueError(f"invalid integration state: {self.integration}")
        if not self.upstream_url.startswith("https://"):
            raise ValueError("upstream_url must use HTTPS")
        packages = tuple(sorted({_bounded(item, "package reference", 128)
                                 for item in self.package_refs}))
        object.__setattr__(self, "package_refs", packages)
        assessments = tuple(sorted(self.assessments, key=lambda item: item.dimension))
        if (len(assessments) != len(DIMENSIONS)
                or {item.dimension for item in assessments} != set(DIMENSIONS)):
            raise ValueError(f"{self.runtime_id} must assess every runtime dimension")
        object.__setattr__(self, "assessments", assessments)
        gaps = tuple(sorted({_bounded(item, "runtime gap") for item in self.gaps}))
        if not gaps:
            raise ValueError("runtime candidates must retain explicit gaps")
        object.__setattr__(self, "gaps", gaps)
        object.__setattr__(self, "notes", tuple(sorted({
            _bounded(item, "runtime note") for item in self.notes})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id, "label": self.label,
            "integration": self.integration, "upstream_url": self.upstream_url,
            "package_refs": list(self.package_refs),
            "assessments": [item.to_dict() for item in self.assessments],
            "gaps": list(self.gaps), "notes": list(self.notes),
            "execution_lane": "queued_live", "executes": False,
        }


@dataclass(frozen=True)
class RuntimeMatrix:
    candidates: tuple[RuntimeCandidate, ...]
    required_live_cases: tuple[str, ...]
    schema: str = SCHEMA
    evidence_as_of: str = EVIDENCE_AS_OF
    matrix_id: str = field(init=False)

    def __post_init__(self) -> None:
        candidates = tuple(sorted(self.candidates, key=lambda item: item.runtime_id))
        if len(candidates) < 10 or len({item.runtime_id for item in candidates}) != len(candidates):
            raise ValueError("runtime matrix requires at least ten unique candidates")
        object.__setattr__(self, "candidates", candidates)
        cases = tuple(sorted({_bounded(item, "live case") for item in self.required_live_cases}))
        if not cases:
            raise ValueError("runtime matrix requires queued live cases")
        object.__setattr__(self, "required_live_cases", cases)
        object.__setattr__(self, "matrix_id", "runtime_matrix_" + hashlib.sha256(
            _canonical(self.identity_dict()).encode("utf-8")).hexdigest())

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "evidence_as_of": self.evidence_as_of,
            "dimensions": list(DIMENSIONS),
            "candidates": [item.to_dict() for item in self.candidates],
            "required_live_cases": list(self.required_live_cases),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "matrix_id": self.matrix_id, **self.identity_dict(),
            "candidate_count": len(self.candidates),
            "execution_lane": "queued_live", "ready_for_selection": False,
            "universal_winner": None, "imports_runtimes": False, "executes": False,
        }


@dataclass(frozen=True)
class RuntimeLiveObservation:
    """Payload-free result for one runtime/case pair.

    Prompts, model output, stderr, traces, credentials, and container logs are
    deliberately absent.  The evidence records only the bounded facts needed
    to decide whether a declared conformance case was observed.
    """

    runtime_id: str
    case: str
    state: str
    reason_code: str
    terminal_count: int = 0
    cleanup_verified: bool = False
    resource_release_verified: bool = False

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.runtime_id):
            raise ValueError("runtime_id must be a bounded lowercase identifier")
        if self.state not in LIVE_STATES:
            raise ValueError("invalid live evidence state")
        if not _REASON.fullmatch(self.reason_code):
            raise ValueError("reason_code must be a bounded lowercase identifier")
        if not 0 <= int(self.terminal_count) <= 1:
            raise ValueError("terminal_count must be zero or one")
        if self.state == "passed" and self.case == "runtime.cleanup" \
                and not self.cleanup_verified:
            raise ValueError("cleanup pass requires verified container absence")
        if self.state == "passed" and self.case == "runtime.resource_release" \
                and not self.resource_release_verified:
            raise ValueError("resource release pass requires verified capacity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "case": self.case,
            "state": self.state,
            "reason_code": self.reason_code,
            "terminal_count": int(self.terminal_count),
            "cleanup_verified": bool(self.cleanup_verified),
            "resource_release_verified": bool(self.resource_release_verified),
        }


def evaluate_live_evidence(
    observations: Iterable[RuntimeLiveObservation | Mapping[str, Any]],
    selected_runtime_ids: Iterable[str],
) -> dict[str, Any]:
    """Validate and summarize selected live evidence without choosing a winner."""
    matrix = compile_runtime_matrix()
    candidate_ids = {item.runtime_id for item in matrix.candidates}
    required_cases = set(matrix.required_live_cases)
    selected_values = list(selected_runtime_ids)
    if len(selected_values) > 32:
        raise ValueError("selected runtime count exceeds bounds")
    selected = tuple(sorted({_bounded(item, "selected runtime", 64)
                             for item in selected_values}))
    if not selected:
        raise ValueError("at least one selected runtime is required")
    unknown = sorted(set(selected) - candidate_ids)
    if unknown:
        raise ValueError(f"unknown selected runtimes: {', '.join(unknown)}")

    raw_observations = list(observations)
    if len(raw_observations) > 1_024:
        raise ValueError("live observation count exceeds bounds")
    normalized = []
    seen = set()
    for raw in raw_observations:
        item = raw if isinstance(raw, RuntimeLiveObservation) \
            else RuntimeLiveObservation(**dict(raw))
        if item.runtime_id not in selected:
            raise ValueError("observation runtime is not selected")
        if item.case not in required_cases:
            raise ValueError("observation case is not declared by the matrix")
        key = (item.runtime_id, item.case)
        if key in seen:
            raise ValueError("duplicate runtime live observation")
        seen.add(key)
        normalized.append(item)
    normalized.sort(key=lambda item: (item.runtime_id, item.case))

    summaries = []
    for runtime_id in selected:
        items = [item for item in normalized if item.runtime_id == runtime_id]
        observed = {item.case for item in items}
        counts = {state: sum(item.state == state for item in items)
                  for state in sorted(LIVE_STATES)}
        missing = sorted(required_cases - observed)
        summaries.append({
            "runtime_id": runtime_id,
            "observed_cases": len(observed),
            "required_cases": len(required_cases),
            "missing_cases": missing,
            "counts": counts,
            "evidence_complete": not missing,
            "conformant": not missing and not counts["failed"] and not counts["unavailable"],
        })

    identity = {
        "schema": LIVE_EVIDENCE_SCHEMA,
        "matrix_id": matrix.matrix_id,
        "selected_runtime_ids": list(selected),
        "observations": [item.to_dict() for item in normalized],
    }
    return {
        **identity,
        "evidence_id": "runtime_live_" + hashlib.sha256(
            _canonical(identity).encode("utf-8")).hexdigest(),
        "summaries": summaries,
        "evidence_complete": all(item["evidence_complete"] for item in summaries),
        "ready_for_selection": False,
        "universal_winner": None,
        "payloads_retained": False,
        "executes": False,
    }

def _features(upstream: dict[str, str], vera_supported: tuple[str, ...] = (),
              vera_partial: tuple[str, ...] = ()) -> tuple[FeatureAssessment, ...]:
    supported = set(vera_supported)
    partial = set(vera_partial)
    return tuple(FeatureAssessment(
        dimension=name,
        upstream=upstream.get(name, "unknown"),
        vera="supported" if name in supported else (
            "partial" if name in partial else "not_integrated"),
        evidence=("Vera bridge source and deterministic contract" if name in supported | partial
                  else ("Upstream documentation only; Vera execution evidence queued"
                        if upstream.get(name) in {"supported", "partial"}
                        else "No verified declaration captured; Vera execution evidence queued")),
    ) for name in DIMENSIONS)


def _candidate(runtime_id: str, label: str, integration: str, url: str,
               packages: tuple[str, ...], upstream_supported: tuple[str, ...],
               upstream_partial: tuple[str, ...] = (),
               vera_supported: tuple[str, ...] = (), vera_partial: tuple[str, ...] = (),
               notes: tuple[str, ...] = ()) -> RuntimeCandidate:
    upstream = {name: "supported" for name in upstream_supported}
    upstream.update({name: "partial" for name in upstream_partial})
    gaps = (
        "No cross-runtime quality winner may be inferred from static declarations.",
        "Timeout, crash, cancellation, cleanup, resource release, malicious output, dependency loss, and version reporting need isolated live evidence.",
    )
    return RuntimeCandidate(runtime_id, label, integration, url, packages,
                            _features(upstream, vera_supported, vera_partial), gaps, notes)


def _apply_adapter(candidate: RuntimeCandidate, descriptor: Any) -> RuntimeCandidate:
    """Project only direct lifecycle equivalents from a shipped adapter.

    Tools, providers, policy, recovery, traces and other semantic dimensions
    remain independently assessed; an execution adapter must not inflate them.
    """
    if descriptor.runtime_id != candidate.runtime_id:
        raise ValueError("runtime adapter and matrix candidate IDs must match")
    features = {item.name: item for item in descriptor.features}
    assessments = []
    for item in candidate.assessments:
        feature_name = _ADAPTER_DIMENSIONS.get(item.dimension)
        if feature_name is None:
            assessments.append(item)
            continue
        feature = features[feature_name]
        vera_state = {
            "supported": "supported",
            "partial": "partial",
            "unsupported": "not_integrated",
        }[feature.state]
        assessments.append(FeatureAssessment(
            item.dimension, item.upstream, vera_state,
            f"RuntimeAdapter declaration: {feature.evidence}"))
    return RuntimeCandidate(
        runtime_id=candidate.runtime_id,
        label=candidate.label,
        integration=candidate.integration,
        upstream_url=candidate.upstream_url,
        package_refs=descriptor.package_refs,
        assessments=tuple(assessments),
        gaps=candidate.gaps,
        notes=candidate.notes,
    )


def compile_runtime_matrix() -> RuntimeMatrix:
    from Vera.vera.agentbridges.runtime_registry import RUNTIME_ADAPTERS

    common = ("tools", "providers", "structured_output", "streaming")
    candidates = (
        _candidate("vera-native", "Vera native loops and DAGs", "native",
                   "https://github.com/BoeJaker/Vera", (), DIMENSIONS,
                   vera_supported=("tools", "providers", "policy", "sessions", "traces", "resources", "streaming", "cancellation", "artifacts", "sandbox", "mcp"),
                   vera_partial=("handoffs", "structured_output", "recovery", "teardown")),
        _apply_adapter(
            _candidate("langgraph", "LangGraph", "shipped_bridge",
                       "https://docs.langchain.com/oss/python/langgraph/", (),
                       common + ("handoffs", "sessions", "recovery", "traces", "cancellation"),
                       vera_supported=("tools", "providers", "streaming", "sandbox"),
                       vera_partial=("resources", "teardown")),
            RUNTIME_ADAPTERS["langgraph"].descriptor),
        _candidate("pydanticai", "PydanticAI", "shipped_bridge",
                   "https://pydantic.dev/docs/ai/", ("pydantic-ai-slim==2.31.0",),
                   common + ("handoffs", "sessions", "recovery", "traces", "mcp"),
                   vera_supported=("tools", "providers", "structured_output", "streaming", "sandbox"),
                   vera_partial=("resources", "teardown")),
        _candidate("smolagents", "Hugging Face smolagents", "shipped_bridge",
                   "https://github.com/huggingface/smolagents", ("smolagents==1.26.0",),
                   ("tools", "providers", "handoffs", "sandbox", "mcp"),
                   upstream_partial=("structured_output", "traces"),
                   vera_supported=("tools", "providers", "streaming", "sandbox"),
                   vera_partial=("resources", "teardown"),
                   notes=("CodeAgent output is executable code and requires a stronger sandbox boundary than JSON tool calls.",)),
        _candidate("openclaw", "OpenClaw", "prospective",
                   "https://docs.openclaw.ai/", ("openclaw",),
                   ("tools", "providers", "handoffs", "sessions", "resources", "sandbox", "mcp"),
                   upstream_partial=("structured_output", "recovery", "traces", "cancellation", "artifacts", "streaming")),
        _candidate("google-adk", "Google Agent Development Kit", "prospective",
                   "https://google.github.io/adk-docs/", ("google-adk",),
                   common + ("handoffs", "policy", "sessions", "recovery", "traces", "artifacts", "mcp"),
                   upstream_partial=("resources", "teardown", "cancellation", "sandbox")),
        _candidate("openai-agents", "OpenAI Agents SDK", "prospective",
                   "https://openai.github.io/openai-agents-python/", ("openai-agents",),
                   common + ("handoffs", "policy", "sessions", "traces", "mcp"),
                   upstream_partial=("recovery", "resources", "teardown", "cancellation", "artifacts", "sandbox")),
        _candidate("strands", "Strands Agents", "prospective",
                   "https://strandsagents.com/", ("strands-agents",),
                   ("tools", "providers", "handoffs", "sessions", "traces", "streaming", "mcp"),
                   upstream_partial=("structured_output", "policy", "recovery", "resources", "teardown", "cancellation", "artifacts", "sandbox"),
                   notes=("Optional candidate: official surface must be reverified before any adapter implementation.",)),
        _candidate("agno", "Agno / AgentOS", "prospective",
                   "https://docs.agno.com/", ("agno",),
                   common + ("handoffs", "policy", "sessions", "recovery", "traces", "artifacts", "mcp"),
                   upstream_partial=("resources", "teardown", "cancellation", "sandbox")),
        _candidate("hermes-compatible", "Hermes-compatible path", "compatibility_path",
                   "https://github.com/NousResearch/hermes-agent", ("hermes-agent",),
                   ("tools", "providers", "handoffs", "sessions", "recovery", "resources", "sandbox", "mcp"),
                   upstream_partial=("structured_output", "policy", "traces", "teardown", "streaming", "cancellation", "artifacts"),
                   notes=("Prefer MCP, skills, and session/provenance adapters over embedding a second autonomous loop.",)),
    )
    cases = (
        "runtime.timeout", "runtime.crash", "runtime.cancel", "runtime.cleanup",
        "runtime.resource_release", "runtime.malicious_output",
        "runtime.missing_dependency", "runtime.version_report",
        "runtime.session_resume", "runtime.trace_redaction",
    )
    return RuntimeMatrix(candidates, cases)
