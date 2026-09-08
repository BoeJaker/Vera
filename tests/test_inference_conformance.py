import pytest

from vera.models import (
    InferenceArtifact, InferenceConformanceExpectation, InferenceTranscript,
    InferenceValue,
    evaluate_inference_conformance,
    inference_conformance_expectation_from_dict)

pytestmark = pytest.mark.critical


def transcript(provider="baseline", *, text="hello", status="completed",
               error_code="", usage=(("output_tokens", 1),)):
    outputs = (() if status != "completed" else
               (InferenceValue.from_json("text", text, media_type="text/plain"),))
    return InferenceTranscript(
        "ireq_fixture", provider, "mpkg_one", outputs, status,
        usage if status == "completed" else (), error_code)


def test_parity_expectation_compares_transcripts_without_provider_identity():
    expectation = InferenceConformanceExpectation.from_transcript(
        "same-output", transcript())
    report = evaluate_inference_conformance(
        expectation, transcript("candidate"))
    assert report.passed
    assert report.provider_id == "candidate"
    assert report.mismatches == ()
    assert expectation.to_dict()["output_digests"][0].startswith("sha256:")
    assert inference_conformance_expectation_from_dict(
        expectation.to_dict()) == expectation


def test_parity_report_names_only_mismatched_fields_and_never_returns_payloads():
    expectation = InferenceConformanceExpectation.from_transcript(
        "changed-output", transcript())
    report = evaluate_inference_conformance(
        expectation, transcript("candidate", text="different",
                                usage=(("output_tokens", 2),)))
    assert report.mismatches == ("output_digests", "usage")
    assert "hello" not in str(report.to_dict())
    assert "different" not in str(report.to_dict())


def test_outage_fixture_requires_the_stable_terminal_error_code():
    expected = InferenceConformanceExpectation(
        "provider-outage", "ireq_fixture", "mpkg_one", "failed",
        error_code="provider_unavailable")
    assert evaluate_inference_conformance(
        expected, transcript("candidate", status="failed",
                             error_code="provider_unavailable")).passed
    report = evaluate_inference_conformance(
        expected, transcript("candidate", status="failed",
                             error_code="runtime_error"))
    assert report.mismatches == ("error_code",)


def test_usage_comparison_can_be_explicitly_omitted_for_cross_runtime_parity():
    expected = InferenceConformanceExpectation.from_transcript(
        "semantic-only", transcript(), compare_usage=False)
    assert evaluate_inference_conformance(
        expected, transcript("candidate", usage=(("output_tokens", 99),))).passed


def test_artifact_parity_uses_verified_content_not_provider_specific_location():
    left = InferenceTranscript(
        "ireq_fixture", "left", "mpkg_one",
        (InferenceValue("tensor", "application/octet-stream",
                        artifact=InferenceArtifact(
                            "artifact://left", "a" * 64, 4,
                            "application/octet-stream")),),
        "completed", ())
    right = InferenceTranscript(
        "ireq_fixture", "right", "mpkg_one",
        (InferenceValue("tensor", "application/octet-stream",
                        artifact=InferenceArtifact(
                            "artifact://right", "a" * 64, 4,
                            "application/octet-stream")),),
        "completed", ())
    expected = InferenceConformanceExpectation.from_transcript("artifact", left)
    assert evaluate_inference_conformance(expected, right).passed
