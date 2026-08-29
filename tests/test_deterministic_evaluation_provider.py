import asyncio

import pytest

from vera.models import (
    DeterministicScalarEvalProvider, EvalProvider, EvaluationRequest,
    ScalarCaseObservation, ScalarEvaluationFixture, ScalarMetricPolicy)


pytestmark = pytest.mark.critical


def fixture(cases=None):
    return ScalarEvaluationFixture(
        subject_kind="model_package",
        subject_id="mpkg_candidate",
        dataset_revision_id="dataset_rev_1",
        policies=(
            ScalarMetricPolicy("latency", 20, "minimize"),
            ScalarMetricPolicy("accuracy", 0.8),
        ),
        cases=cases or (
            ScalarCaseObservation("case-2", (("latency", 15), ("accuracy", 0.9))),
            ScalarCaseObservation("case-1", (("accuracy", 0.7), ("latency", 10))),
        ),
    )


def request(metrics=("latency", "accuracy")):
    return EvaluationRequest(
        "model_package", "mpkg_candidate", "dataset_rev_1", metrics)


def test_provider_is_structural_offline_and_reproducible():
    provider = DeterministicScalarEvalProvider(fixture())
    assert isinstance(provider, EvalProvider)
    assert provider.profile().to_dict() == {
        "provider_id": "deterministic-scalar/v1",
        "kind": "evaluation",
        "capabilities": ["arithmetic-mean", "offline", "scalar-observations"],
    }
    first = asyncio.run(provider.evaluate(request()))
    second = asyncio.run(provider.evaluate(request()))
    assert first == second
    assert first.evaluation_report_id == second.evaluation_report_id
    assert [(metric.metric_id, metric.value) for metric in first.metrics] == [
        ("accuracy", 0.8), ("latency", 12.5)]
    assert first.evaluated_cases == 2
    assert first.failed_cases == 1
    assert not first.passed


def test_requested_metric_subset_controls_case_gate_and_report_shape():
    report = asyncio.run(DeterministicScalarEvalProvider(fixture()).evaluate(
        request(("latency",))))
    assert tuple(metric.metric_id for metric in report.metrics) == ("latency",)
    assert report.failed_cases == 0
    assert report.passed


@pytest.mark.parametrize("change, message", [
    ({"subject_id": "mpkg_other"}, "subject"),
    ({"dataset_revision_id": "dataset_rev_2"}, "dataset revision"),
    ({"metric_ids": ("quality",)}, "unsupported metrics"),
])
def test_provider_fails_closed_on_request_fixture_mismatch(change, message):
    values = dict(
        subject_kind="model_package", subject_id="mpkg_candidate",
        dataset_revision_id="dataset_rev_1", metric_ids=("accuracy",))
    values.update(change)
    with pytest.raises(ValueError, match=message):
        asyncio.run(DeterministicScalarEvalProvider(fixture()).evaluate(
            EvaluationRequest(**values)))


def test_fixture_canonicalizes_order_and_requires_complete_unique_cases():
    first = fixture()
    reversed_fixture = ScalarEvaluationFixture(
        first.subject_kind, first.subject_id, first.dataset_revision_id,
        tuple(reversed(first.policies)), tuple(reversed(first.cases)))
    assert reversed_fixture == first
    with pytest.raises(ValueError, match="exactly the policy metric set"):
        fixture((ScalarCaseObservation("case", (("accuracy", 1),)),))
    with pytest.raises(ValueError, match="case IDs must be unique"):
        fixture((first.cases[0], first.cases[0]))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_observations_reject_nonfinite_and_boolean_values(value):
    expected = TypeError if value is True else ValueError
    with pytest.raises(expected):
        ScalarCaseObservation("case", (("accuracy", value),))
