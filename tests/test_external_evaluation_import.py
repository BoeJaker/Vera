import copy

import pytest

from vera.models.evaluation_evidence import EvaluationCaseIdentity, JudgeProvenance
from vera.models.external_evaluation_import import (
    import_deepeval_projection, import_promptfoo_projection)
from vera.models.training_contracts import EvaluationRequest


pytestmark = pytest.mark.critical
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def projection(kind="deepeval", count=2):
    request = EvaluationRequest(
        "prompt_package", "ppkg_candidate", "dataset_rev_1", ("quality",))
    cases = [EvaluationCaseIdentity(
        "dataset_rev_1", f"case-{index}", DIGEST_A,
        "sha256:" + format(index + 1, "064x")) for index in range(count)]
    return {
        "schema": f"vera.{kind}-frozen-projection/v1",
        "source_version": "1.2.3",
        "request": request.to_dict(),
        "expected_cases": [item.to_dict() for item in cases],
        "results": [{
            "case_id": item.case_id, "status": "completed",
            "metrics": [{"metric_id": "quality", "value": 1,
                         "threshold": 0.5, "direction": "maximize"}],
            "judge": JudgeProvenance("exact", "1").to_dict(),
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cost_microunits": 0, "latency_ms": 1},
            "error_code": "",
        } for item in cases],
    }


@pytest.mark.parametrize("kind,loader", [
    ("deepeval", import_deepeval_projection),
    ("promptfoo", import_promptfoo_projection),
])
def test_imports_complete_frozen_projection_without_payloads(kind, loader):
    value = projection(kind)
    receipt = loader(value)
    assert receipt.report.status == "completed" and receipt.report.passed is True
    assert receipt.source_kind == kind and receipt.export_digest.startswith("sha256:")
    assert receipt.to_dict()["provider_invoked"] is False
    assert receipt.to_dict()["payloads_retained"] is False


def test_subset_becomes_honest_partial_report():
    value = projection()
    value["results"] = value["results"][:1]
    report = import_deepeval_projection(value).report
    assert report.status == "partial" and report.coverage == 0.5
    assert report.passed is False


@pytest.mark.parametrize("field", [
    "input", "actual_output", "expected_output", "prompt", "response",
    "reason", "config", "env", "vars", "messages", "traceback",
])
def test_rejects_payload_bearing_fields_at_any_depth(field):
    value = projection()
    value["results"][0][field] = "sensitive"
    with pytest.raises(ValueError, match="payload-bearing"):
        import_deepeval_projection(value)


def test_rejects_unknown_fields_schema_case_and_metric_drift():
    unknown = projection()
    unknown["timestamp"] = "not admitted"
    with pytest.raises(ValueError, match="unsupported fields"):
        import_deepeval_projection(unknown)
    wrong_schema = projection()
    wrong_schema["schema"] = "third.party/raw"
    with pytest.raises(ValueError, match="schema"):
        import_deepeval_projection(wrong_schema)
    unknown_case = projection()
    unknown_case["results"][0]["case_id"] = "ecase_unknown"
    with pytest.raises(ValueError, match="expected case set"):
        import_deepeval_projection(unknown_case)
    metric = projection()
    metric["results"][0]["metrics"][0]["metric_id"] = "other"
    with pytest.raises(ValueError, match="every requested metric"):
        import_deepeval_projection(metric)
    version = projection()
    version["source_version"] = "free form secret-like text"
    with pytest.raises(ValueError, match="source version"):
        import_deepeval_projection(version)
    nonfinite = projection()
    nonfinite["results"][0]["metrics"][0]["value"] = float("nan")
    with pytest.raises(ValueError):
        import_deepeval_projection(nonfinite)


def test_rejects_duplicate_results_and_forged_case_identity():
    duplicate = projection()
    duplicate["results"][1] = copy.deepcopy(duplicate["results"][0])
    with pytest.raises(ValueError, match="result case IDs must be unique"):
        import_deepeval_projection(duplicate)
    forged = projection()
    forged["expected_cases"][0]["case_key"] = "changed"
    with pytest.raises(ValueError, match="identity"):
        import_deepeval_projection(forged)
    non_string_key = projection()
    non_string_key["results"][0][1] = "ambiguous"
    with pytest.raises(ValueError, match="keys must be strings"):
        import_deepeval_projection(non_string_key)
