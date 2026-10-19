"""Frozen regression, evidence-recovery and rollback proof for Four Minds.

The evaluator calls only the pure joined-evidence projector.  It never invokes
a decider, model, node, capability, ledger or store.  A rollback result means
that a frozen route identity returned to its baseline value; it is not evidence
that production traffic moved or that a runtime rollback was executed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from . import ledger_core as LC
from .evidence_core import project_decision_evidence


SCHEMA = "vera.four-minds-recovery-evaluation/v1"
CORPUS_SCHEMA = "vera.four-minds-recovery-corpus/v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,191}$")
_STAGES = ("baseline", "candidate", "recovery", "rollback")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluation inputs must be canonical JSON") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class FrozenEvidenceAttempt:
    attempt_id: str
    decision: Mapping[str, Any]
    outcomes: tuple[Mapping[str, Any], ...]
    route: Mapping[str, Any]
    context_evidence_ids: tuple[str, ...] = ()
    require_context: bool = False
    require_model_package: bool = False

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "attempt_id")
        if not isinstance(self.decision, Mapping):
            raise ValueError("attempt decision must be an object")
        if not isinstance(self.route, Mapping):
            raise ValueError("attempt route must be an object")
        if (not isinstance(self.outcomes, tuple) or
                not all(isinstance(item, Mapping) for item in self.outcomes)):
            raise ValueError("attempt outcomes must be a tuple of objects")
        if (not isinstance(self.context_evidence_ids, tuple) or
                any(not isinstance(value, str) for value in self.context_evidence_ids)):
            raise ValueError("context evidence IDs must be a tuple of strings")

    def project(self) -> dict[str, Any]:
        return project_decision_evidence(
            self.decision, outcomes=self.outcomes, route=self.route,
            context_evidence_ids=self.context_evidence_ids,
            require_context=self.require_context,
            require_model_package=self.require_model_package)


@dataclass(frozen=True, slots=True)
class FrozenRecoveryCase:
    case_id: str
    expected_answer: Any
    expected_outcome_label: Any
    baseline: FrozenEvidenceAttempt
    candidate: FrozenEvidenceAttempt
    recovery: FrozenEvidenceAttempt
    rollback: FrozenEvidenceAttempt

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        attempts = tuple(getattr(self, stage) for stage in _STAGES)
        if not all(isinstance(item, FrozenEvidenceAttempt) for item in attempts):
            raise ValueError("each stage requires a frozen evidence attempt")
        if len({item.attempt_id for item in attempts}) != len(attempts):
            raise ValueError("attempt identities must be unique within a case")
        anchors = {
            (str(item.decision.get("question_id") or ""),
             str(item.decision.get("state_hash") or ""))
            for item in attempts
        }
        if len(anchors) != 1:
            raise ValueError("case attempts must preserve question and state identity")


def _summarize_attempt(
        stage: str, attempt: FrozenEvidenceAttempt,
        expected_answer_digest: str,
        expected_outcome_digest: str) -> dict[str, Any]:
    evidence = attempt.project()
    strongest = evidence["strongest_outcome"]
    return {
        "stage": stage,
        "attempt_id": attempt.attempt_id,
        "evidence_id": evidence["evidence_id"],
        "decision_id": evidence["decision"]["decision_id"],
        "route_id": evidence["route"]["route_id"],
        "eligible_for_evaluation": evidence["eligible_for_evaluation"],
        "answer_matches_expected": (
            evidence["decision"]["answer_digest"] == expected_answer_digest),
        "outcome_observed": strongest is not None,
        "outcome_matches_expected": bool(
            strongest and strongest["label_digest"] == expected_outcome_digest),
        "missing": list(evidence["missing"]),
    }


def evaluate_frozen_recovery(
        cases: Sequence[FrozenRecoveryCase]) -> dict[str, Any]:
    """Evaluate frozen attempts without executing or changing their routes."""
    if isinstance(cases, (str, bytes)):
        raise ValueError("cases must be a sequence of frozen recovery cases")
    cases = tuple(cases)
    if (not cases or len(cases) > 1000 or
            not all(isinstance(case, FrozenRecoveryCase) for case in cases) or
            len({case.case_id for case in cases}) != len(cases)):
        raise ValueError("cases must contain 1..1000 unique frozen recovery cases")

    rows = []
    corpus_bindings = []
    for case in sorted(cases, key=lambda item: item.case_id):
        expected = _digest(case.expected_answer)
        expected_outcome = _digest(case.expected_outcome_label)
        attempts = {
            stage: _summarize_attempt(
                stage, getattr(case, stage), expected, expected_outcome)
            for stage in _STAGES
        }
        baseline = attempts["baseline"]
        candidate = attempts["candidate"]
        recovery = attempts["recovery"]
        rollback = attempts["rollback"]
        if (not baseline["eligible_for_evaluation"] or
                not baseline["answer_matches_expected"] or
                not baseline["outcome_matches_expected"]):
            raise ValueError(
                "baseline must be complete and match the frozen decision and outcome")
        regression_detected = (
            not candidate["eligible_for_evaluation"] or
            not candidate["answer_matches_expected"] or
            not candidate["outcome_matches_expected"])
        recovery_complete = (
            recovery["eligible_for_evaluation"] and
            recovery["answer_matches_expected"] and
            recovery["outcome_matches_expected"])
        rollback_identity_restored = (
            rollback["route_id"] == baseline["route_id"])
        rollback_complete = (
            rollback["eligible_for_evaluation"] and
            rollback["answer_matches_expected"] and
            rollback["outcome_matches_expected"] and rollback_identity_restored)
        failure_parts = []
        if candidate["missing"]:
            failure_parts.append("missing_evidence")
        if not candidate["answer_matches_expected"]:
            failure_parts.append("answer_regression")
        if not candidate["outcome_matches_expected"]:
            failure_parts.append("outcome_regression")
        failure_class = "_and_".join(failure_parts) or "not_detected"
        rows.append({
            "case_id": case.case_id,
            "attempts": [attempts[stage] for stage in _STAGES],
            "failure_class": failure_class,
            "regression_detected": regression_detected,
            "recovery_complete": recovery_complete,
            "rollback_identity_restored": rollback_identity_restored,
            "rollback_complete": rollback_complete,
        })
        corpus_bindings.append({
            "case_id": case.case_id,
            "expected_answer_digest": expected,
            "expected_outcome_digest": expected_outcome,
            "attempts": [
                {
                    "stage": stage,
                    "attempt_id": attempts[stage]["attempt_id"],
                    "evidence_id": attempts[stage]["evidence_id"],
                }
                for stage in _STAGES
            ],
        })

    count = len(rows)
    rate = lambda field: sum(bool(row[field]) for row in rows) / count
    corpus = {
        "schema": CORPUS_SCHEMA,
        "cases": corpus_bindings,
    }
    result = {
        "schema": SCHEMA,
        "corpus_id": _digest(corpus),
        "evidence_scope": "frozen_fixture_conformance",
        "recovery_scope": "evidence_reconstruction_only",
        "rollback_scope": "route_identity_rehearsal_only",
        "case_count": count,
        "cases": rows,
        "metrics": {
            "regression_detection_rate": rate("regression_detected"),
            "evidence_recovery_rate": rate("recovery_complete"),
            "rollback_identity_rate": rate("rollback_identity_restored"),
            "rollback_completion_rate": rate("rollback_complete"),
        },
        "decision": "insufficient_for_production",
        "production_ready": False,
        "routes_executed": False,
        "rollback_executed": False,
        "models_invoked": False,
        "providers_invoked": False,
        "uses_accelerators": False,
        "writes": False,
        "authority_changed": False,
    }
    return {
        "evaluation_id": "fmreval_" + hashlib.sha256(
            _canonical(result).encode()).hexdigest(),
        **result,
    }


def _decision(
        identity: str, answer: str, *, implementation: str, node: str,
        package: str) -> dict[str, Any]:
    row = LC.make_decision(
        "loop.step_verify", answer, origin="census", surface="agent_loop",
        qtype="choice", labels=["accept", "revise", "escalate"],
        state={"fixture": "frozen-state"},
        state_ref={"store": "frozen", "id": "state-1"},
        decider="student", implementation=implementation,
        mode="student_first", package=package, node=node, ms=9,
        minds_digest="frozen-switches-v1", run_id="frozen-run",
        session_id="frozen-session", ts="2026-10-19T00:00:00Z")
    row["decision_id"] = "dec_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    return row


def _attempt(
        identity: str, answer: str, route: Mapping[str, Any], *,
        implementation: str, node: str, package: str,
        require_model_package: bool = False,
        outcome_label: str = "observed") -> FrozenEvidenceAttempt:
    decision = _decision(
        identity, answer, implementation=implementation, node=node,
        package=package)
    outcome = LC.make_outcome(
        decision["decision_id"], outcome_label, "frozen.evaluator", "strong",
        ts="2026-10-19T00:00:01Z")
    return FrozenEvidenceAttempt(
        attempt_id=identity, decision=decision, outcomes=(outcome,),
        route=dict(route), context_evidence_ids=("ctxrev_fixture",),
        require_context=True, require_model_package=require_model_package)


def frozen_recovery_fixture() -> dict[str, Any]:
    """Return a versioned synthetic evaluation with no live dependencies."""
    knn_baseline = {
        "kind": "nlp_knn", "node_id": "cpu-246", "index_id": "decisions.v1",
        "index_version": "idx-1", "route_revision": "route-stable",
    }
    knn_candidate = {
        "kind": "nlp_knn", "node_id": "cpu-246", "index_id": "decisions.v2",
        "route_revision": "route-candidate",
    }
    knn_recovery = {
        **knn_candidate, "index_version": "idx-2-recovered",
    }
    model_baseline = {
        "kind": "nlp_model", "node_id": "cpu-247", "provider_id": "nlp-node",
        "deployment_id": "dep-stable", "model_package_id": "mpkg-stable",
        "model_revision": "model-stable", "route_revision": "route-stable",
    }
    model_candidate = {
        **model_baseline, "deployment_id": "dep-candidate",
        "model_package_id": "", "route_revision": "route-candidate",
    }
    model_recovery = {
        **model_candidate, "model_package_id": "mpkg-candidate",
    }
    cases = (
        FrozenRecoveryCase(
            case_id="knn-index-regression", expected_answer="accept",
            expected_outcome_label="observed",
            baseline=_attempt("knn-baseline", "accept", knn_baseline,
                              implementation="knn:v1", node="cpu-246",
                              package="student.knn"),
            candidate=_attempt("knn-candidate", "revise", knn_candidate,
                               implementation="knn:v2", node="cpu-246",
                               package="student.knn", outcome_label="regressed"),
            recovery=_attempt("knn-recovery", "accept", knn_recovery,
                              implementation="knn:v2", node="cpu-246",
                              package="student.knn"),
            rollback=_attempt("knn-rollback", "accept", knn_baseline,
                              implementation="knn:v1", node="cpu-246",
                              package="student.knn"),
        ),
        FrozenRecoveryCase(
            case_id="model-package-regression", expected_answer="accept",
            expected_outcome_label="observed",
            baseline=_attempt("model-baseline", "accept", model_baseline,
                              implementation="card:minilm", node="cpu-247",
                              package="student.card", require_model_package=True),
            candidate=_attempt("model-candidate", "accept", model_candidate,
                               implementation="card:minilm", node="cpu-247",
                               package="student.card", require_model_package=True),
            recovery=_attempt("model-recovery", "accept", model_recovery,
                              implementation="card:minilm", node="cpu-247",
                              package="student.card", require_model_package=True),
            rollback=_attempt("model-rollback", "accept", model_baseline,
                              implementation="card:minilm", node="cpu-247",
                              package="student.card", require_model_package=True),
        ),
    )
    return evaluate_frozen_recovery(cases)
