from dataclasses import FrozenInstanceError

import pytest

from vera.models.evaluation_evidence import (
    CaseEvaluationEvidence, EvaluationCaseIdentity, EvaluationUsage,
    JudgeProvenance, PartialEvaluationReport)
from vera.models.optimizer_contracts import (
    OptimizationBudget, OptimizationRequest, OptimizerProfile, OptimizerProposal,
    OptimizerProvider, OptimizerSelectionPolicy, PromptOptimizationCandidate,
    optimizer_proposal_from_dict, select_optimizer_candidate)
from vera.models.training_contracts import (
    EvaluationRequest, LifecycleContractConflict, MetricResult, PromptMessage,
    PromptPackage)


pytestmark = pytest.mark.critical
DIGEST = "sha256:" + "a" * 64


def prompt(version="1", text="Answer carefully."):
    return PromptPackage("answer", version, (PromptMessage("system", text),))


def optimization_request(source):
    return OptimizationRequest(
        source.prompt_package_id, "dataset_train_1", "dataset_heldout_1",
        ("quality",), OptimizationBudget(maximum_candidates=3,
                                         maximum_cost_microunits=10))


def candidate(source, version="2", text="Answer accurately.", cost=1):
    return PromptOptimizationCandidate(
        prompt(version, text), source.prompt_package_id, "fixture-optimizer", "1",
        DIGEST, EvaluationUsage(cost_microunits=cost))


def proposal(source, candidates=None):
    return OptimizerProposal(
        optimization_request(source),
        OptimizerProfile("fixture-optimizer", "1", ("prompt-proposal", "offline")),
        tuple(candidates or (candidate(source),)))


def evaluation(subject_id, value):
    request = EvaluationRequest(
        "prompt_package", subject_id, "dataset_heldout_1", ("quality",))
    identity = EvaluationCaseIdentity(
        "dataset_heldout_1", "case-1", DIGEST, "sha256:" + "b" * 64)
    evidence = CaseEvaluationEvidence(
        identity, "completed", JudgeProvenance("exact", "1"),
        (MetricResult("quality", value, 0.5),))
    return PartialEvaluationReport(
        request, "offline", (identity.case_id,), (evidence,), status="completed")


def test_request_separates_training_and_heldout_and_has_stable_identity():
    source = prompt()
    first = optimization_request(source)
    second = optimization_request(source)
    assert first.request_id == second.request_id
    with pytest.raises(ValueError, match="must differ"):
        OptimizationRequest(source.prompt_package_id, "same", "same", ("quality",))
    with pytest.raises(FrozenInstanceError):
        first.source_prompt_package_id = "changed"


def test_proposal_is_content_addressed_and_has_no_activation_authority():
    source = prompt()
    first = proposal(source)
    second = proposal(source, tuple(reversed(first.candidates)))
    assert first.proposal_id == second.proposal_id
    data = first.to_dict()
    assert data["effect"] == "none" and data["activation_changed"] is False
    assert optimizer_proposal_from_dict(data) == first
    assert not any("activate" in name or "register" in name or "alias" in name
                   for name in dir(first))


def test_strict_reconstruction_rejects_forged_candidate_and_effect_claims():
    value = proposal(prompt()).to_dict()
    value["candidates"][0]["optimizer_version"] = "forged"
    with pytest.raises(LifecycleContractConflict, match="candidate identity"):
        optimizer_proposal_from_dict(value)
    value = proposal(prompt()).to_dict()
    value["activation_changed"] = True
    with pytest.raises(LifecycleContractConflict, match="activation"):
        optimizer_proposal_from_dict(value)


def test_proposal_rejects_budget_and_provenance_drift():
    source = prompt()
    too_costly = candidate(source, cost=11)
    with pytest.raises(ValueError, match="cost budget"):
        proposal(source, (too_costly,))
    foreign = PromptOptimizationCandidate(
        prompt("3"), source.prompt_package_id, "other-optimizer", "1", DIGEST)
    with pytest.raises(ValueError, match="provenance"):
        proposal(source, (foreign,))
    with pytest.raises(ValueError, match="differ"):
        PromptOptimizationCandidate(
            source, source.prompt_package_id, "fixture-optimizer", "1", DIGEST)


def test_protocol_is_structural_and_construction_invokes_nothing():
    calls = []

    class Provider:
        def profile(self):
            calls.append("profile")
            return OptimizerProfile("fixture", "1", ("proposal",))

        async def propose(self, request):
            calls.append("propose")

    assert isinstance(Provider(), OptimizerProvider)
    proposal(prompt())
    assert calls == []


def test_selection_recommends_best_heldout_improvement_without_activation():
    source = prompt()
    one = candidate(source, "2", "Candidate one")
    two = candidate(source, "3", "Candidate two")
    item = proposal(source, (one, two))
    result = select_optimizer_candidate(
        item, evaluation(source.prompt_package_id, 0.6),
        {one.candidate_id: evaluation(one.prompt_package.prompt_package_id, 0.7),
         two.candidate_id: evaluation(two.prompt_package.prompt_package_id, 0.9)},
        OptimizerSelectionPolicy("quality", minimum_improvement=0.1))
    assert result["decision"] == "recommend"
    assert result["recommended_candidate_id"] == two.candidate_id
    assert result["effect"] == "none" and result["activation_changed"] is False


def test_selection_declines_without_improvement_or_valid_baseline():
    source = prompt()
    item = proposal(source)
    cand = item.candidates[0]
    declined = select_optimizer_candidate(
        item, evaluation(source.prompt_package_id, 0.9),
        {cand.candidate_id: evaluation(cand.prompt_package.prompt_package_id, 0.8)},
        OptimizerSelectionPolicy("quality", minimum_improvement=0.01))
    assert declined["decision"] == "decline"
    bad_baseline = evaluation(source.prompt_package_id, 0.1)
    failed = select_optimizer_candidate(
        item, bad_baseline,
        {cand.candidate_id: evaluation(cand.prompt_package.prompt_package_id, 0.9)},
        OptimizerSelectionPolicy("quality"))
    assert failed["reason"] == "baseline-evidence-failed"


def test_selection_rejects_evidence_authority_drift():
    source = prompt()
    item = proposal(source)
    cand = item.candidates[0]
    with pytest.raises(ValueError, match="exactly match"):
        select_optimizer_candidate(
            item, evaluation(source.prompt_package_id, 0.6), {},
            OptimizerSelectionPolicy("quality"))
    with pytest.raises(ValueError, match="subject"):
        select_optimizer_candidate(
            item, evaluation(source.prompt_package_id, 0.6),
            {cand.candidate_id: evaluation("ppkg_wrong", 0.9)},
            OptimizerSelectionPolicy("quality"))
    with pytest.raises(ValueError, match="not an optimization objective"):
        select_optimizer_candidate(
            item, evaluation(source.prompt_package_id, 0.6),
            {cand.candidate_id: evaluation(cand.prompt_package.prompt_package_id, 0.9)},
            OptimizerSelectionPolicy("latency"))
