from pathlib import Path

import pytest

from vera.inventory.onnx_capability_authority_review import (
    CURRENT_SURFACES,
    ONNXIdentitySurface,
    build_onnx_capability_authority_review,
)


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_review_is_source_bound_non_executing_and_non_mutating():
    result = build_onnx_capability_authority_review(ROOT)
    assert result["schema"] == "vera.onnx-capability-authority-review/v1"
    assert len(result["surfaces"]) == len(CURRENT_SURFACES) == 3
    assert result["removal_authority"] is False
    assert result["changes_registration"] is False
    assert result["changes_routing"] is False
    assert result["loads_models"] is False
    assert result["executes_inference"] is False
    assert result["mutates"] is False
    assert all(row["source_content_digest"].startswith("sha256:")
               for row in result["source_evidence"].values())


def test_authority_separates_executor_artifact_and_compatibility_identity():
    result = build_onnx_capability_authority_review(ROOT)
    assert result["authority"] == {
        "stable_execution": "ml.onnx.run",
        "artifact_identity": "ModelPackage",
        "artifact_catalog": "model-package-registry",
        "compatibility_invocation": "ml.onnx.model.<slug>",
    }
    by_name = {row["name"]: row for row in result["surfaces"]}
    assert by_name["ml.onnx.run"]["selector_kind"] == "artifact-argument"
    assert by_name["ml.onnx.model.<slug>"]["identity_kind"] == "artifact_bound_compatibility"
    assert by_name["ModelPackage"]["callable_now"] is False


def test_review_preserves_current_callers_and_grants_no_retirement_authority():
    result = build_onnx_capability_authority_review(ROOT)
    assert "keep_both_invocation_forms_callable_during_compatibility_period" in result["recommendations"]
    assert "measure_stored_external_and_runtime_consumers_before_any_retirement" in result["recommendations"]
    assert result["coverage"]["runtime_calls"] == "not_examined"
    assert result["coverage"]["inference_parity"] == "not_run"
    assert not any(item.startswith(("remove_", "disable_", "retire_"))
                   for item in result["recommendations"])


def test_identity_is_order_stable():
    first = build_onnx_capability_authority_review(ROOT, CURRENT_SURFACES)
    second = build_onnx_capability_authority_review(ROOT, tuple(reversed(CURRENT_SURFACES)))
    assert first["review_id"] == second["review_id"]


def test_source_drift_fails_closed(tmp_path):
    source = tmp_path / "vera" / "sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("present = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source assertions are missing"):
        build_onnx_capability_authority_review(
            tmp_path,
            (ONNXIdentitySurface("sample", "stable_executor", "argument",
                                 "registry", True),),
            {"vera/sample.py": ("missing = True",)},
        )


def test_surface_validation_fails_closed():
    with pytest.raises(ValueError, match="unsupported ONNX identity kind"):
        ONNXIdentitySurface("sample", "guessed", "argument", "registry", True)
    with pytest.raises(ValueError, match="bounded identifier"):
        ONNXIdentitySurface("bad name", "stable_executor", "argument", "registry", True)
