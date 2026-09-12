"""Independent, evidence-bound reviews of deprecation candidates.

Reviews are advisory inputs to the separate removal gate.  They deliberately
cannot execute a migration, disable a surface, or authorize deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .deprecation_inventory import DeprecationCandidate, DeprecationInventory


SCHEMA = "vera.deprecation-candidate-review/v1"
RECOMMENDATIONS = frozenset({
    "retain", "adapt", "migrate", "removal_candidate", "insufficient_evidence",
})
EVIDENCE_KINDS = frozenset({
    "inventory", "snapshot", "runtime_state", "semantic_contract",
    "evaluation", "rollback", "consumer",
})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def digest_evidence(value: Any) -> str:
    """Return a content digest without retaining the evidence payload."""
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def inventory_review_evidence(
    candidate: DeprecationCandidate, inventory: DeprecationInventory,
) -> "ReviewEvidence":
    if sum(item.candidate_id == candidate.candidate_id
           for item in inventory.candidates) != 1:
        raise ValueError("review candidate must occur exactly once in the inventory")
    return ReviewEvidence(
        candidate.candidate_id, "inventory", digest_evidence(inventory.to_dict()),
        "candidate_inventory_bound")


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    candidate_id: str
    kind: str
    digest: str
    assertion: str
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _identifier(
            self.candidate_id, "candidate ID"))
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError("unsupported review evidence kind")
        digest = str(self.digest or "").lower()
        if not _DIGEST.fullmatch(digest):
            raise ValueError("review evidence must use a sha256 digest")
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "assertion", _identifier(
            self.assertion, "evidence assertion"))
        object.__setattr__(self, "evidence_id", _identity(
            "depe_", self.identity_dict()))

    def identity_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id, "kind": self.kind,
            "digest": self.digest, "assertion": self.assertion,
        }

    def to_dict(self) -> dict[str, str]:
        return {"evidence_id": self.evidence_id, **self.identity_dict()}


@dataclass(frozen=True, slots=True)
class IndependentCandidateReview:
    candidate: DeprecationCandidate
    inventory_id: str
    reviewer: str
    review_window_id: str
    recommendation: str
    evidence: tuple[ReviewEvidence, ...]
    rationale_codes: tuple[str, ...]
    required_actions: tuple[str, ...]
    consumer_count: int
    coverage_complete: bool
    review_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inventory_id", _identifier(
            self.inventory_id, "inventory ID"))
        object.__setattr__(self, "reviewer", _identifier(self.reviewer, "reviewer"))
        object.__setattr__(self, "review_window_id", _identifier(
            self.review_window_id, "review window ID"))
        if self.recommendation not in RECOMMENDATIONS:
            raise ValueError("unsupported review recommendation")
        if (isinstance(self.consumer_count, bool) or
                not isinstance(self.consumer_count, int) or
                self.consumer_count < 0):
            raise ValueError("consumer count must be a non-negative integer")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage complete must be boolean")
        if not self.evidence:
            raise ValueError("an independent review requires evidence")
        if any(item.candidate_id != self.candidate.candidate_id
               for item in self.evidence):
            raise ValueError("review evidence must belong to exactly one candidate")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("review evidence must be unique")
        rationale = tuple(sorted({_identifier(item, "rationale code")
                                  for item in self.rationale_codes}))
        actions = tuple(sorted({_identifier(item, "required action")
                                for item in self.required_actions}))
        if not rationale or not actions:
            raise ValueError("review rationale and required actions cannot be empty")
        object.__setattr__(self, "rationale_codes", rationale)
        object.__setattr__(self, "required_actions", actions)
        evidence = tuple(sorted(self.evidence, key=lambda item: item.evidence_id))
        object.__setattr__(self, "evidence", evidence)
        if self.recommendation == "removal_candidate":
            kinds = {item.kind for item in evidence}
            required = {"inventory", "semantic_contract", "rollback"}
            if not self.coverage_complete or self.consumer_count or not required <= kinds:
                raise ValueError(
                    "removal candidacy requires complete zero-consumer coverage, "
                    "a semantic contract, and rollback evidence")
        object.__setattr__(self, "review_id", _identity("depr_", self.identity_dict()))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "candidate": self.candidate.to_dict(),
            "inventory_id": self.inventory_id,
            "reviewer": self.reviewer,
            "review_window_id": self.review_window_id,
            "recommendation": self.recommendation,
            "evidence": [item.to_dict() for item in self.evidence],
            "rationale_codes": list(self.rationale_codes),
            "required_actions": list(self.required_actions),
            "consumer_count": self.consumer_count,
            "coverage_complete": self.coverage_complete,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            **self.identity_dict(),
            "independent": True,
            "removal_authority": False,
            "executes": False,
            "mutates": False,
        }


def review_candidate(
    candidate: DeprecationCandidate, inventory: DeprecationInventory, *,
    reviewer: str, review_window_id: str, recommendation: str,
    evidence: Sequence[ReviewEvidence], rationale_codes: Sequence[str],
    required_actions: Sequence[str],
) -> IndependentCandidateReview:
    """Bind one candidate to its own inventory evidence and recommendation."""
    matches = [item for item in inventory.candidates
               if item.candidate_id == candidate.candidate_id]
    if len(matches) != 1:
        raise ValueError("review candidate must occur exactly once in the inventory")
    evidence = tuple(evidence)
    expected_inventory_digest = digest_evidence(inventory.to_dict())
    if not any(item.kind == "inventory" and
               item.digest == expected_inventory_digest for item in evidence):
        raise ValueError("review evidence must bind the exact candidate inventory")
    report = inventory.report()
    candidate_report = report["candidates"][candidate.candidate_id]
    return IndependentCandidateReview(
        candidate=candidate, inventory_id=inventory.inventory_id,
        reviewer=reviewer, review_window_id=review_window_id,
        recommendation=recommendation, evidence=evidence,
        rationale_codes=tuple(rationale_codes),
        required_actions=tuple(required_actions),
        consumer_count=candidate_report["consumer_evidence"]["total"],
        coverage_complete=report["coverage_complete"],
    )


def generated_ontology_review_evidence(
    candidate: DeprecationCandidate, *, snapshot: Mapping[str, Any],
    generation_status: Mapping[str, Any],
    consumption_status: Mapping[str, Any],
) -> tuple[ReviewEvidence, ...]:
    """Project the reversible state of generated ontology relations."""
    if candidate.kind != "ontology_projection":
        raise ValueError("generated ontology evidence requires an ontology candidate")
    if snapshot.get("restorable") is not True or snapshot.get("executes") is not False:
        raise ValueError("ontology snapshot must be restorable and non-executing")
    if generation_status.get("enabled") is not False:
        raise ValueError("persistent generation must be disabled for this review")
    if consumption_status.get("enabled") is not False:
        raise ValueError("generated prompt-context consumption must be disabled")
    return (
        ReviewEvidence(candidate.candidate_id, "snapshot",
                       digest_evidence(snapshot), "stored_relations_restorable"),
        ReviewEvidence(candidate.candidate_id, "runtime_state",
                       digest_evidence({
                           "generation": generation_status,
                           "consumption": consumption_status,
                       }), "generation_and_consumption_disabled"),
        ReviewEvidence(candidate.candidate_id, "rollback",
                       digest_evidence({
                           "generation": generation_status.get("rollback"),
                           "consumption": consumption_status.get("rollback"),
                       }), "feature_flags_preserve_rollback"),
    )
