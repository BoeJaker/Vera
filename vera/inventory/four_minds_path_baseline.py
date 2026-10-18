"""Source-bound inventory of Four Minds' real decision paths.

This module deliberately does not import the operational Minds, NLP, model,
ledger, or agent-loop modules it inventories.  It establishes the exact
producer, compute, persistence, outcome, evaluation, and consumer boundaries
that later evidence joins must preserve without pretending that those joins or
an authority migration already exist.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Sequence


SCHEMA = "vera.four-minds-path-baseline/v1"
_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_STAGES = {"control", "sense", "decision", "compute", "persistence",
           "outcome", "evaluation", "consumer", "monitoring"}
_STATES = {"live", "default_on", "default_off", "shadow", "scheduled",
           "read_only", "source_only"}
_AUTHORITIES = {"native", "switchboard", "agent-loop", "nlp-node",
                "postgres", "background-queue", "operator"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _source_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix not in {
            ".py", ".html", ".js", ".json", ".md"}:
        raise ValueError("source must be a supported repository-relative file")
    return path


@dataclass(frozen=True, slots=True)
class FourMindsPath:
    path_id: str
    stage: str
    authority: str
    runtime_state: str
    source: str
    source_assertion: str
    contract: str
    identities: tuple[str, ...]
    timing: str = "none"
    target: str = ""

    def __post_init__(self) -> None:
        for name in ("path_id", "contract"):
            if not _ID.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a bounded identifier")
        if self.target and not _ID.fullmatch(self.target):
            raise ValueError("target must be a bounded identifier")
        if self.stage not in _STAGES or self.runtime_state not in _STATES:
            raise ValueError("unsupported stage or runtime state")
        if self.authority not in _AUTHORITIES:
            raise ValueError("unsupported authority")
        if not self.identities or len(set(self.identities)) != len(self.identities):
            raise ValueError("identities must be non-empty and unique")
        if any(not _ID.fullmatch(value) for value in self.identities):
            raise ValueError("identity fields must be bounded identifiers")
        if self.timing not in {"none", "sampled", "per-call", "aggregate"}:
            raise ValueError("unsupported timing coverage")
        _source_path(self.source)
        if not self.source_assertion or len(self.source_assertion) > 256:
            raise ValueError("source_assertion must be present and bounded")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id, "stage": self.stage,
            "authority": self.authority, "runtime_state": self.runtime_state,
            "source": self.source, "contract": self.contract,
            "identities": list(self.identities), "timing": self.timing,
            "target": self.target,
        }


PATHS = (
    FourMindsPath(
        "switchboard.effective", "control", "switchboard", "live",
        "vera/minds/switchboard_capabilities.py", '"minds.effective", http_method="POST"',
        "switchboard.effective", ("feature-id", "mode", "digest"),
        target="four-minds"),
    FourMindsPath(
        "sense.frame", "sense", "native", "live",
        "vera/minds/sense_capabilities.py", '"sense.frame", serves=_SERVES_FRAME',
        "sense.frame", ("text-hash", "kind", "cards-version"), "per-call",
        "decision-input"),
    FourMindsPath(
        "sense.nlp-inputs", "sense", "nlp-node", "live",
        "vera/minds/sense_capabilities.py", 'emb, cards = await asyncio.gather(_raw("nlp.embed"',
        "nlp.embed-ner-langid", ("text-hash", "cards-version"),
        "per-call", "sense.frame"),
    FourMindsPath(
        "loop.decision-producer", "decision", "agent-loop", "live",
        "vera/dag/dag_workshop_capabilities.py", "_dl = _loop_ledger.RunLedger(",
        "decision.loop-native", ("run-id", "step-id", "question-id", "minds-digest"),
        target="decision.ledger"),
    FourMindsPath(
        "loop.decision-record", "persistence", "native", "live",
        "vera/minds/loop_ledger.py", "class RunLedger:",
        "decision.loop-ledger-adapter", ("decision-id", "run-id", "question-id", "state-hash"),
        target="decision.ledger"),
    FourMindsPath(
        "decision.ledger-stream", "persistence", "native", "live",
        "vera/minds/ledger_capabilities.py", 'STREAM = "vera:decide:ledger"',
        "decision.ledger-stream", ("decision-id", "schema", "timestamp"),
        target="decision.ledger-postgres"),
    FourMindsPath(
        "decision.ledger-postgres", "persistence", "postgres", "live",
        "vera/minds/ledger_capabilities.py", "CREATE TABLE IF NOT EXISTS decision_ledger",
        "decision.ledger-postgres", ("decision-id", "question-id", "run-id", "timestamp"),
        target="decision.outcomes"),
    FourMindsPath(
        "decision.training-state", "persistence", "postgres", "live",
        "vera/minds/training_state.py", "def put_framed(decision:",
        "decision.training-state", ("decision-id", "question-id", "state-hash"),
        target="decision.students"),
    FourMindsPath(
        "decision.run-outcomes", "outcome", "agent-loop", "live",
        "vera/minds/loop_ledger.py", "def finish(self, gate:",
        "decision.run-outcomes", ("decision-id", "run-id", "outcome-source"),
        target="decision.calibration"),
    FourMindsPath(
        "precedent.index", "persistence", "postgres", "scheduled",
        "vera/minds/precedent.py", "async def build_index(limit_runs:",
        "precedent.run-index", ("run-id", "goal", "origin"),
        target="precedent.lookup"),
    FourMindsPath(
        "precedent.lookup", "decision", "nlp-node", "default_off",
        "vera/minds/precedent.py", 'res = await DD.knn("run_goals"',
        "precedent.lookup", ("index-id", "index-version", "run-id"),
        "per-call", "planner"),
    FourMindsPath(
        "distributed-decide.dispatch", "compute", "nlp-node", "live",
        "vera/minds/decide_dispatch.py", "async def knn(index_id:",
        "vera.decide.v1", ("index-id", "index-version", "node-id"),
        "per-call", "distributed-decide.knn"),
    FourMindsPath(
        "distributed-decide.knn", "compute", "nlp-node", "live",
        "edge/nlp_server.py", "def decide_knn(index_id, version, vector, k=15, exclude_run=\"\"):",
        "vera.decide.v1", ("index-id", "index-version", "row-id", "run-id"),
        "per-call", "decision.student-knn"),
    FourMindsPath(
        "decision.student-router", "decision", "native", "default_off",
        "vera/minds/student.py", "async def ask(question_id:",
        "decision.student", ("question-id", "implementation", "model", "node-id"),
        "per-call", "decision.ledger"),
    FourMindsPath(
        "decision.student-knn", "decision", "nlp-node", "default_off",
        "vera/minds/student.py", "async def ask_knn(question_id:",
        "decision.student-knn", ("question-id", "index-version", "implementation"),
        "per-call", "decision.ledger"),
    FourMindsPath(
        "decision.shadow-llm", "evaluation", "background-queue", "shadow",
        "vera/minds/judge.py", '"decide.shadow.score",',
        "decision.shadow-llm", ("decision-id", "question-id", "implementation", "model"),
        "per-call", "decision.matrix"),
    FourMindsPath(
        "decision.shadow-knn", "evaluation", "nlp-node", "shadow",
        "vera/minds/judge.py", '"decide.shadow.knn",',
        "decision.shadow-knn", ("decision-id", "index-version", "implementation", "node-id"),
        "per-call", "decision.matrix"),
    FourMindsPath(
        "decision.shadow-nlp", "evaluation", "nlp-node", "shadow",
        "vera/minds/judge.py", '"decide.shadow.nlp", serves=_SERVES_NLP',
        "decision.shadow-nlp", ("decision-id", "question-id", "implementation", "family"),
        "per-call", "decision.matrix"),
    FourMindsPath(
        "decision.shadow-heads", "evaluation", "native", "shadow",
        "vera/minds/judge.py", '"decide.shadow.heads", serves=_SERVES_HEADS',
        "decision.shadow-onnx", ("decision-id", "question-id", "implementation", "model-slug"),
        "per-call", "decision.matrix"),
    FourMindsPath(
        "decision.matrix", "evaluation", "operator", "read_only",
        "vera/minds/judge.py", '"decide.matrix.report", http_method="GET"',
        "decision.matrix-report", ("question-id", "implementation", "decision-id"),
        "aggregate", "operator"),
    FourMindsPath(
        "decision.calibration", "evaluation", "postgres", "read_only",
        "vera/minds/calibration.py", '"decide.calibration", http_method="GET"',
        "decision.calibration", ("question-id", "implementation", "decision-id", "outcome-source"),
        "aggregate", "operator"),
    FourMindsPath(
        "memory.switch-consumer", "consumer", "native", "live",
        "vera/fabric/memory_retrieval.py", "async def _switch(feature_id: str, default: str = \"off\")",
        "switchboard.consumer", ("feature-id", "mode"),
        target="memory.retrieval"),
    FourMindsPath(
        "worldview.spread-monitor", "monitoring", "operator", "scheduled",
        "vera/minds/tests_monitor.py", 'health = await _call("worldview.health")',
        "worldview.spread-history", ("snapshot-time", "worldview-health"),
        "aggregate", "operator"),
)


GAPS = (
    "no_single_join_spans_input_route_model_decision_outcome_and_calibration",
    "sense_frame_does_not_retain_exact_nlp_model_dimension_deployment_or_modelpackage_identity",
    "distributed_decide_retains_node_and_index_but_not_portable_modelpackage_identity",
    "student_and_shadow_paths_use_implementation_labels_without_one_package_revision_contract",
    "context_evidence_is_not_an_exact_provenance_qualified_decision_input",
    "jepa_worldview_is_monitored_but_not_joined_as_a_decision_input",
    "rollback_is_fragmented_across_switches_modes_and_native_fallbacks",
    "operator_read_models_do_not_explain_the_full_evidence_chain",
)


def semantic_baseline(paths: Sequence[FourMindsPath] = PATHS) -> dict[str, Any]:
    ordered = tuple(sorted(paths, key=lambda value: value.path_id))
    ids = [value.path_id for value in ordered]
    if not ordered or len(ids) != len(set(ids)):
        raise ValueError("paths must be non-empty and uniquely identified")
    payload = {
        "schema": SCHEMA,
        "paths": [value.semantic_dict() for value in ordered],
        "gaps": list(GAPS),
        "authority_moved": False,
        "runtime_changed": False,
        "joined_evidence_complete": False,
    }
    return {"baseline_id": _identity("fmpb_", payload), **payload}


def _source_evidence(repo_root: Path, paths: Sequence[FourMindsPath]) -> list[dict[str, str]]:
    root = repo_root.resolve()
    evidence = []
    for value in sorted(paths, key=lambda item: item.path_id):
        path = (root / value.source).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"source is unavailable: {value.source}")
        raw = path.read_bytes()
        if len(raw) > 3_000_000:
            raise ValueError(f"source is too large: {value.source}")
        if value.source_assertion not in raw.decode("utf-8"):
            raise ValueError(f"source assertion missing for {value.path_id}")
        evidence.append({
            "path_id": value.path_id,
            "source_path_digest": "sha256:" + hashlib.sha256(value.source.encode()).hexdigest(),
            "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "assertion_digest": "sha256:" + hashlib.sha256(
                value.source_assertion.encode()).hexdigest(),
        })
    return evidence


def build_four_minds_path_baseline(
        repo_root: Path, paths: Sequence[FourMindsPath] = PATHS) -> dict[str, Any]:
    stable = semantic_baseline(paths)
    evidence = _source_evidence(Path(repo_root), paths)
    by_stage: dict[str, list[str]] = {}
    for value in sorted(paths, key=lambda item: item.path_id):
        by_stage.setdefault(value.stage, []).append(value.path_id)
    return {
        **stable,
        "inventory_id": _identity(
            "fmpi_", {"baseline_id": stable["baseline_id"],
                      "source_evidence": evidence}),
        "source_evidence": evidence,
        "paths_by_stage": dict(sorted(by_stage.items())),
        "counts": {
            "paths": len(paths), "stages": len(by_stage), "gaps": len(GAPS),
            "live": sum(value.runtime_state == "live" for value in paths),
            "shadow": sum(value.runtime_state == "shadow" for value in paths),
            "timed": sum(value.timing != "none" for value in paths),
        },
        "constraints": {
            "imports_inventoried_subsystems": False,
            "invokes_capabilities": False,
            "contacts_nodes": False,
            "contacts_models": False,
            "uses_accelerators": False,
            "mutates": False,
        },
    }
