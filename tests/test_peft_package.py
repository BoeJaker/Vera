import dataclasses

import pytest

from vera.models.model_package import (
    ModelArtifact, ModelCompatibility, ModelPackage)
from vera.models.peft_package import (
    PeftMethodConfig, propose_peft_package, verify_peft_training_result)
from vera.models.training_contracts import (
    EvaluationReport, EvaluationRequest, MetricResult, TrainingRequest,
    TrainingRun)


pytestmark = pytest.mark.critical


def base(source_revision="base_commit_7"):
    return ModelPackage(
        "base", "v7", "decoder", "safetensors",
        (ModelArtifact("weights", "private://base/model.safetensors",
                       "a" * 64, 4096),),
        ModelCompatibility(("generate",), "text/v1", "text/v1", 1024,
                           ("cuda",)),
        framework="transformers", framework_version="4.57.0",
        tokenizer="tokenizer_7", preprocessing="text_v1",
        source_uri="private://base", source_revision=source_revision,
        license="apache-2.0")


def method():
    return PeftMethodConfig(
        "lora", "causal_lm", ("q_proj", "v_proj"), 8, 16.0, 0.05,
        ("lm_head",), "none")


def artifacts():
    return (
        ModelArtifact("adapter.weights", "private://adapter/weights.safetensors",
                      "b" * 64, 1024),
        ModelArtifact("adapter.config", "private://adapter/config.json",
                      "c" * 64, 256),
    )


def proposal():
    return propose_peft_package(
        base_package=base(), method_config=method(), name="base-domain-adapter",
        version="run-7", artifacts=artifacts(), training_run_id="train_7",
        dataset_revision_id="dataset_7", runtime_profile_id="dtrp_7",
        peft_version="0.18.0", transformers_version="4.57.0",
        precision="bf16", license="apache-2.0")


def evaluation_for(package_id, *, value=0.95, dataset="dataset_7"):
    request = EvaluationRequest(
        "model_package", package_id, dataset, ("quality",))
    return EvaluationReport(
        request, "exact_eval", "completed",
        (MetricResult("quality", value, 0.9),), 10, 0)


def test_proposal_is_deterministic_content_safe_and_non_authoritative():
    first = proposal()
    second = proposal()
    assert first == second
    assert first.package.package_id == second.package.package_id
    value = first.to_dict()
    assert value["derived_model_package_id"] == first.package.package_id
    assert value["base_model_package_id"] == base().package_id
    assert value["artifacts"] == [
        {"role": "adapter.config", "sha256": "sha256:" + "c" * 64,
         "size_bytes": 256},
        {"role": "adapter.weights", "sha256": "sha256:" + "b" * 64,
         "size_bytes": 1024},
    ]
    assert value["effect"] == "none"
    assert not any(value[key] for key in (
        "peft_invoked", "artifact_downloaded", "registered",
        "alias_mutated", "deployed", "activation_authority"))
    assert "private://" not in repr(value)
    metadata = dict(first.package.metadata)
    assert metadata["base_model_package_id"] == base().package_id
    assert metadata["dataset_revision_id"] == "dataset_7"


@pytest.mark.parametrize("factory", [
    lambda: PeftMethodConfig("unknown", "causal_lm", ("q",), 8, 16),
    lambda: PeftMethodConfig("lora", "unknown", ("q",), 8, 16),
    lambda: PeftMethodConfig("lora", "causal_lm", (), 8, 16),
    lambda: PeftMethodConfig("lora", "causal_lm", ("q",), True, 16),
    lambda: PeftMethodConfig("lora", "causal_lm", ("q",), 8, float("nan")),
    lambda: PeftMethodConfig("ia3", "causal_lm", ("q",), 8, None),
    lambda: PeftMethodConfig("ia3", "causal_lm", ("q",), None, None, .1),
])
def test_method_configuration_fails_closed(factory):
    with pytest.raises(ValueError):
        factory()


def test_base_artifacts_and_adapter_evidence_must_be_pinned_and_complete():
    with pytest.raises(ValueError, match="source revision"):
        propose_peft_package(
            base_package=base(""), method_config=method(), name="adapter",
            version="v1", artifacts=artifacts(), training_run_id="train",
            dataset_revision_id="data", runtime_profile_id="runtime",
            peft_version="0.18.0", transformers_version="4.57.0",
            precision="fp32", license="mit")
    with pytest.raises(ValueError, match="license"):
        propose_peft_package(
            base_package=base(), method_config=method(), name="adapter",
            version="v1", artifacts=artifacts(), training_run_id="train",
            dataset_revision_id="data", runtime_profile_id="runtime",
            peft_version="0.18.0", transformers_version="4.57.0",
            precision="fp32", license="mit")
    with pytest.raises(ValueError, match="weights and config"):
        propose_peft_package(
            base_package=base(), method_config=method(), name="adapter",
            version="v1", artifacts=(artifacts()[0],), training_run_id="train",
            dataset_revision_id="data", runtime_profile_id="runtime",
            peft_version="0.18.0", transformers_version="4.57.0",
            precision="fp32", license="mit")


@pytest.mark.parametrize("field,value", [
    ("peft_version", "latest"),
    ("transformers_version", "main"),
    ("precision", "int4"),
    ("license", ""),
])
def test_versions_precision_and_license_are_required(field, value):
    kwargs = dict(
        base_package=base(), method_config=method(), name="adapter", version="v1",
        artifacts=artifacts(), training_run_id="train",
        dataset_revision_id="data", runtime_profile_id="runtime",
        peft_version="0.18.0", transformers_version="4.57.0",
        precision="fp32", license="mit")
    kwargs[field] = value
    with pytest.raises(ValueError):
        propose_peft_package(**kwargs)


def test_terminal_training_and_evaluation_evidence_verify_exactly():
    value = proposal()
    report = evaluation_for(value.package.package_id)
    run = TrainingRun(
        "train_7", TrainingRequest(
            "dataset_7", "fine_tune", base().package_id),
        "dtrp_7", "succeeded", 1, value.package.package_id,
        (report.evaluation_report_id,))
    receipt = verify_peft_training_result(value, run, (report,))
    assert receipt.to_dict()["verified"] is True
    assert receipt.to_dict()["derived_model_package_id"] == value.package.package_id
    assert receipt.to_dict()["effect"] == "none"


def test_terminal_verification_rejects_mismatched_run_or_evaluation():
    value = proposal()
    report = evaluation_for(value.package.package_id)
    run = TrainingRun(
        "train_7", TrainingRequest(
            "dataset_7", "fine_tune", base().package_id),
        "dtrp_7", "succeeded", 1, value.package.package_id,
        ("eval_wrong",))
    with pytest.raises(ValueError, match="evaluation identities"):
        verify_peft_training_result(value, run, (report,))
    wrong_output = dataclasses.replace(run, output_model_package_id="mpkg_wrong")
    with pytest.raises(ValueError, match="output package"):
        verify_peft_training_result(value, wrong_output, ())
    running = TrainingRun(
        "train_7", TrainingRequest(
            "dataset_7", "fine_tune", base().package_id),
        "dtrp_7", "running")
    with pytest.raises(ValueError, match="succeeded"):
        verify_peft_training_result(value, running, ())


def test_terminal_verification_rejects_runtime_objective_and_failed_evaluation():
    value = proposal()
    report = evaluation_for(value.package.package_id)
    request = TrainingRequest("dataset_7", "fine_tune", base().package_id)
    run = TrainingRun(
        "train_7", request, "wrong_runtime", "succeeded", 1,
        value.package.package_id, (report.evaluation_report_id,))
    with pytest.raises(ValueError, match="runtime"):
        verify_peft_training_result(value, run, (report,))
    wrong_objective = dataclasses.replace(
        run, runtime_id="dtrp_7",
        request=TrainingRequest("dataset_7", "pretrain", base().package_id))
    with pytest.raises(ValueError, match="objective"):
        verify_peft_training_result(value, wrong_objective, (report,))
    failed_report = evaluation_for(value.package.package_id, value=0.5)
    failed_run = dataclasses.replace(
        run, runtime_id="dtrp_7",
        evaluation_report_ids=(failed_report.evaluation_report_id,))
    with pytest.raises(ValueError, match="must pass"):
        verify_peft_training_result(value, failed_run, (failed_report,))
