from dataclasses import FrozenInstanceError

import pytest

from vera.models.training_contracts import (
    EvalProvider, EvaluationReport, EvaluationRequest, LifecycleContractConflict,
    MetricResult, PromptMessage, PromptPackage, ProviderProfile, TrainingRequest,
    TrainingRun, TrainingRuntime, evaluation_report_from_dict,
    prompt_package_from_dict, training_run_from_dict)


pytestmark = pytest.mark.critical


def prompt():
    return PromptPackage(
        name="judge", version="1",
        messages=(PromptMessage("system", "Score the answer."),
                  PromptMessage("user", "Question: {question}")),
        variables=("question",), output_contract="score/v1",
        metadata=(("owner", "eval"),))


def training_request():
    return TrainingRequest(
        dataset_revision_id="dataset_rev_1", objective="fine-tune",
        base_model_package_id="mpkg_base", prompt_package_id=prompt().prompt_package_id,
        hyperparameters=(("epochs", 2), ("learning_rate", 0.001)))


def evaluation_request():
    return EvaluationRequest(
        subject_kind="model_package", subject_id="mpkg_candidate",
        dataset_revision_id="dataset_rev_1", metric_ids=("accuracy", "latency"),
        prompt_package_id=prompt().prompt_package_id)


def test_prompt_identity_is_canonical_immutable_and_round_trips():
    first = prompt()
    second = PromptPackage(
        name="judge", version="1", messages=first.messages,
        variables=("question", "question"), output_contract="score/v1",
        metadata=(("owner", "eval"),))
    assert first.prompt_package_id == second.prompt_package_id
    assert prompt_package_from_dict(first.to_dict()) == first
    with pytest.raises(FrozenInstanceError):
        first.name = "changed"
    forged = first.to_dict()
    forged["version"] = "2"
    with pytest.raises(LifecycleContractConflict, match="identity"):
        prompt_package_from_dict(forged)


def test_training_request_is_canonical_and_rejects_non_scalar_hyperparameters():
    first = training_request()
    second = TrainingRequest(
        dataset_revision_id="dataset_rev_1", objective="fine-tune",
        base_model_package_id="mpkg_base", prompt_package_id=prompt().prompt_package_id,
        hyperparameters=(("learning_rate", 0.001), ("epochs", 2)))
    assert first.training_request_id == second.training_request_id
    with pytest.raises(TypeError, match="JSON scalars"):
        TrainingRequest("dataset_rev_1", "fine-tune", hyperparameters=(("layers", [1]),))


def test_training_run_terminal_invariants_and_round_trip():
    succeeded = TrainingRun(
        "train-1", training_request(), "accelerate/v1", "succeeded", attempts=2,
        output_model_package_id="mpkg_derived", evaluation_report_ids=("eval_2", "eval_1"))
    assert training_run_from_dict(succeeded.to_dict()) == succeeded
    with pytest.raises(ValueError, match="output ModelPackage"):
        TrainingRun("train-1", training_request(), "runtime", "succeeded")
    with pytest.raises(ValueError, match="error code"):
        TrainingRun("train-1", training_request(), "runtime", "failed")
    with pytest.raises(ValueError, match="non-terminal"):
        TrainingRun("train-1", training_request(), "runtime", "running",
                    output_model_package_id="mpkg_bad")
    with pytest.raises(ValueError, match="cannot claim"):
        TrainingRun("train-1", training_request(), "runtime", "cancelled",
                    output_model_package_id="mpkg_bad")


def test_completed_evaluation_reports_all_metrics_and_computes_gate():
    report = EvaluationReport(
        evaluation_request(), "deterministic/v1", "completed",
        metrics=(MetricResult("latency", 10, 20, "minimize"),
                 MetricResult("accuracy", 0.91, 0.9)), evaluated_cases=20)
    assert report.passed
    assert evaluation_report_from_dict(report.to_dict()) == report
    failed_gate = EvaluationReport(
        evaluation_request(), "deterministic/v1", "completed",
        metrics=(MetricResult("accuracy", 0.8, 0.9),
                 MetricResult("latency", 10, 20, "minimize")), evaluated_cases=20)
    assert not failed_gate.passed
    forged = report.to_dict()
    forged["metrics"][0]["passed"] = not forged["metrics"][0]["passed"]
    with pytest.raises(LifecycleContractConflict, match="metric pass flag"):
        evaluation_report_from_dict(forged)


def test_evaluation_fails_closed_on_missing_duplicate_or_nonfinite_metrics():
    request = evaluation_request()
    with pytest.raises(ValueError, match="every requested metric"):
        EvaluationReport(request, "provider", "completed",
                         metrics=(MetricResult("accuracy", 1, 0.9),), evaluated_cases=1)
    with pytest.raises(ValueError, match="unique"):
        EvaluationReport(
            EvaluationRequest("model_package", "model", "data", ("accuracy",)),
            "provider", "completed",
            metrics=(MetricResult("accuracy", 1, 0), MetricResult("accuracy", 1, 0)))
    with pytest.raises(ValueError, match="finite"):
        MetricResult("accuracy", float("nan"), 0.9)


def test_failed_and_cancelled_evaluations_cannot_claim_results():
    failed = EvaluationReport(
        evaluation_request(), "provider", "failed", error_code="provider-failed")
    assert not failed.passed
    assert evaluation_report_from_dict(failed.to_dict()) == failed
    with pytest.raises(ValueError, match="cannot claim"):
        EvaluationReport(evaluation_request(), "provider", "cancelled",
                         metrics=(MetricResult("accuracy", 1, 0),))


def test_protocols_are_structural_and_contract_construction_invokes_nothing():
    calls = []

    class Provider:
        def profile(self):
            calls.append("profile")
            return ProviderProfile("fixture", "evaluation", ("deterministic",))

        async def evaluate(self, request):
            calls.append("evaluate")

    class Runtime:
        def profile(self):
            calls.append("profile")
            return ProviderProfile("fixture", "training", ("submit",))

        async def submit(self, request):
            calls.append("submit")

    assert isinstance(Provider(), EvalProvider)
    assert isinstance(Runtime(), TrainingRuntime)
    prompt()
    training_request()
    evaluation_request()
    assert calls == []
