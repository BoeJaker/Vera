import copy
import json

import pytest

from vera.execution.run_protocol import ArtifactRef, Run, RunStatus
from vera.models.mlflow_interop import (
    MLFLOW_CANDIDATE_SCHEMA, import_mlflow_candidate, plan_mlflow_export)
from vera.models.model_package import (
    ModelArtifact, ModelCompatibility, ModelPackage)
from vera.models.training_contracts import (
    EvaluationReport, EvaluationRequest, MetricResult, PromptMessage,
    PromptPackage, TrainingRequest, TrainingRun)


pytestmark = pytest.mark.critical


def digest(value):
    import hashlib
    body = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def prompt():
    return PromptPackage(
        "assistant", "v1", (PromptMessage("user", "Secret {{question}}"),),
        ("question",), "text/v1", (("team", "private"),))


def model():
    return ModelPackage(
        "tiny", "v1", "encoder", "onnx",
        (ModelArtifact("weights", "private://models/tiny.onnx", "a" * 64, 12),),
        ModelCompatibility(("embed",), "text/v1", "vector/v1"),
        framework="onnxruntime", framework_version="1.23.2",
        source_uri="private://source", source_revision="commit123",
        metadata=(("owner", "private"),))


def evaluation(value_prompt):
    request = EvaluationRequest(
        "prompt_package", value_prompt.prompt_package_id, "dataset_rev_1",
        ("quality",), value_prompt.prompt_package_id)
    return EvaluationReport(
        request, "exact-evaluator", "completed",
        (MetricResult("quality", 1.0, 0.9),), 2, 0)


def training(value_prompt, value_model, value_eval):
    request = TrainingRequest(
        "dataset_rev_1", "fine_tune", value_model.package_id,
        value_prompt.prompt_package_id, (("epochs", 1),))
    return TrainingRun(
        "train_1", request, "cpu-runtime", "succeeded", 1,
        value_model.package_id, (value_eval.evaluation_report_id,))


def completed_run():
    value = Run(
        "run_1", "training", RunStatus.COMPLETED, attempt=1,
        created_at="2026-10-05T00:00:00+00:00",
        started_at="2026-10-05T00:00:01+00:00",
        ended_at="2026-10-05T00:00:02+00:00",
        progress=1.0, usage={"tokens": 12}, cost={"usd": 0.1},
        policy={"secret_ref": "must-not-export"}, retry_owner="vera",
        artifacts=[ArtifactRef(
            "artifact_1", "model", "private://artifact",
            "b" * 64, "application/octet-stream", 12)])
    return value


def projection(kind="model_package"):
    candidate = model().to_dict() if kind == "model_package" else prompt().to_dict()
    artifacts = ({"weights": "sha256:" + "a" * 64}
                 if kind == "model_package" else {})
    return {
        "schema": MLFLOW_CANDIDATE_SCHEMA,
        "source_revision": "mlflow-model-v7",
        "mlflow_run_id": "run-abc",
        "candidate_kind": kind,
        "candidate": candidate,
        "candidate_digest": digest(candidate),
        "artifact_digests": artifacts,
    }


def test_export_plan_preserves_vera_identity_without_prompt_or_uri_payloads():
    value_prompt = prompt()
    value_model = model()
    value_eval = evaluation(value_prompt)
    plan = plan_mlflow_export(
        source_revision="commit123", experiment_name="vera-eval",
        runs=(completed_run(),), evaluations=(value_eval,),
        prompts=(value_prompt,), models=(value_model,),
        training_runs=(training(value_prompt, value_model, value_eval),))
    result = plan.to_dict()
    assert result["schema"] == "vera.mlflow-export-plan/v1"
    assert result["effect"] == "none" and result["mlflow_invoked"] is False
    assert result["activation_authority"] is False
    by_kind = {item["kind"]: item for item in result["entities"]}
    assert by_kind["prompt_package"]["vera_id"] == value_prompt.prompt_package_id
    assert by_kind["model_package"]["vera_id"] == value_model.package_id
    assert by_kind["evaluation_report"]["vera_id"] == value_eval.evaluation_report_id
    encoded = json.dumps(result)
    assert "Secret {{question}}" not in encoded
    assert "private://" not in encoded
    assert "must-not-export" not in encoded
    assert "template_digest" in encoded and "uri_digest" in encoded


def test_export_is_order_independent_and_duplicate_identities_fail_closed():
    first = completed_run()
    second = Run("run_2", "training", RunStatus.COMPLETED)
    left = plan_mlflow_export(
        source_revision="commit123", experiment_name="vera-eval",
        runs=(first, second))
    right = plan_mlflow_export(
        source_revision="commit123", experiment_name="vera-eval",
        runs=(second, first))
    assert left.plan_id == right.plan_id
    with pytest.raises(ValueError, match="identities must be unique"):
        plan_mlflow_export(
            source_revision="commit123", experiment_name="vera-eval",
            runs=(first, first))


def test_export_projection_cannot_mutate_plan_identity():
    plan = plan_mlflow_export(
        source_revision="commit123", experiment_name="vera-eval",
        runs=(completed_run(),))
    before = plan.plan_id
    projection_value = plan.entities[0].projection
    projection_value["status"] = "forged"
    assert plan.entities[0].projection["status"] == "completed"
    assert plan.plan_id == before


def test_export_rejects_unverified_artifacts_and_non_scalar_usage():
    missing_hash = Run(
        "run_bad", "task", RunStatus.COMPLETED,
        artifacts=[ArtifactRef("a", "file", "private://a")])
    with pytest.raises(ValueError, match="artifact checksum"):
        plan_mlflow_export(
            source_revision="commit123", experiment_name="vera-eval",
            runs=(missing_hash,))
    nested_usage = Run(
        "run_bad_usage", "task", RunStatus.COMPLETED,
        usage={"tokens": {"input": 1}})
    with pytest.raises(TypeError, match="JSON scalars"):
        plan_mlflow_export(
            source_revision="commit123", experiment_name="vera-eval",
            runs=(nested_usage,))
    with pytest.raises(ValueError, match="1..1000"):
        plan_mlflow_export(
            source_revision="commit123", experiment_name="vera-eval",
            runs=tuple(completed_run() for _ in range(1_001)))


@pytest.mark.parametrize("kind", ["model_package", "prompt_package"])
def test_import_candidate_is_untrusted_and_never_mutates_authority(kind):
    value = projection(kind)
    proposal = import_mlflow_candidate(value)
    result = proposal.to_dict()
    assert result["status"] == "untrusted_proposal"
    assert result["effect"] == "none" and result["mlflow_invoked"] is False
    assert result["artifact_downloaded"] is False
    assert result["registered"] is False and result["alias_mutated"] is False
    assert result["activation_authority"] is False
    assert "candidate" not in result


def test_import_rejects_digest_revision_schema_and_unknown_field_drift():
    bad_digest = projection()
    bad_digest["candidate"]["version"] = "v2"
    with pytest.raises(ValueError, match="candidate digest"):
        import_mlflow_candidate(bad_digest)
    missing_revision = projection()
    missing_revision["source_revision"] = ""
    with pytest.raises(ValueError, match="source revision"):
        import_mlflow_candidate(missing_revision)
    bad_schema = projection()
    bad_schema["schema"] = "mlflow.raw/v1"
    with pytest.raises(ValueError, match="schema"):
        import_mlflow_candidate(bad_schema)
    unknown = projection()
    unknown["tracking_uri"] = "secret"
    with pytest.raises(ValueError, match="unsupported fields"):
        import_mlflow_candidate(unknown)
    nested_unknown = projection()
    nested_unknown["candidate"]["tracking_uri"] = "secret"
    nested_unknown["candidate_digest"] = digest(nested_unknown["candidate"])
    with pytest.raises(ValueError, match="canonical schema"):
        import_mlflow_candidate(nested_unknown)


def test_import_rejects_artifact_and_content_identity_forgery():
    artifact = projection()
    artifact["artifact_digests"]["weights"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="artifact digest evidence"):
        import_mlflow_candidate(artifact)
    forged = projection()
    forged["candidate"]["package_id"] = "mpkg_" + "0" * 64
    forged["candidate_digest"] = digest(forged["candidate"])
    with pytest.raises(ValueError, match="identity"):
        import_mlflow_candidate(forged)
    prompt_artifact = projection("prompt_package")
    prompt_artifact["artifact_digests"] = {"weights": "sha256:" + "a" * 64}
    with pytest.raises(ValueError, match="cannot claim model artifacts"):
        import_mlflow_candidate(prompt_artifact)


def test_import_projection_is_not_mutated():
    value = projection()
    before = copy.deepcopy(value)
    import_mlflow_candidate(value)
    assert value == before
