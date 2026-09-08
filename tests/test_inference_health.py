import pytest

from vera.models import (
    InferenceProviderHealth, inference_provider_health_from_dict)

pytestmark = pytest.mark.critical


def test_health_evidence_is_content_addressed_bounded_and_replayable():
    health = InferenceProviderHealth(
        "provider:a", "ready", 100, 200, "cluster-probe",
        ("mpkg_b", "mpkg_a"), concurrency_limit=8, in_flight=3,
        queue_depth=2, latency_ms=12, revision=4)
    same = InferenceProviderHealth(
        "provider:a", "ready", 100, 200, "cluster-probe",
        ("mpkg_a", "mpkg_b"), concurrency_limit=8, in_flight=3,
        queue_depth=2, latency_ms=12, revision=4)
    assert health.evidence_id == same.evidence_id
    assert health.available_slots == 5
    assert health.is_current(100) and health.is_current(200)
    assert not health.is_current(201)
    assert health.to_dict()["available_package_ids"] == ["mpkg_a", "mpkg_b"]
    assert inference_provider_health_from_dict(health.to_dict()) == health
    changed = health.to_dict()
    changed["in_flight"] = 4
    with pytest.raises(ValueError, match="identity"):
        inference_provider_health_from_dict(changed)


@pytest.mark.parametrize("kwargs, message", [
    ({"state": "ready", "available_package_ids": ()}, "available package"),
    ({"observed_at_ms": 5, "valid_until_ms": 4}, "validity"),
    ({"concurrency_limit": 1, "in_flight": 2}, "in_flight"),
    ({"queue_depth": -1}, "queue_depth"),
    ({"valid_until_ms": 86_400_002}, "validity window"),
])
def test_health_evidence_rejects_incoherent_or_unbounded_facts(kwargs, message):
    values = dict(provider_id="provider", state="ready", observed_at_ms=1,
                  valid_until_ms=2, source="probe",
                  available_package_ids=("mpkg",), concurrency_limit=2,
                  in_flight=1, queue_depth=0)
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        InferenceProviderHealth(**values)


def test_unavailable_evidence_carries_no_false_package_availability():
    health = InferenceProviderHealth(
        "provider", "unavailable", 1, 2, "probe",
        concurrency_limit=0, in_flight=0, queue_depth=7)
    assert health.available_package_ids == ()
    assert health.available_slots == 0
