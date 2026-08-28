import pytest

from vera.worldview.worldview_shadow_evidence import (
    WorldviewShadowEvidenceEntry, WorldviewShadowEvidenceWindow)
from vera.worldview.worldview_shadow_parity import WorldviewShadowParityReport


pytestmark = pytest.mark.critical


def report(*, missing=(), drift=(), snapshot="a"):
    return WorldviewShadowParityReport(
        manifest_id="manifest-1", legacy_snapshot_hash="sha256:" + snapshot * 64,
        expected_records=1, observed_records=1, matched_records=1,
        missing_record_ids=tuple(missing), unexpected_record_ids=(),
        invalid_embedding_record_ids=(), missing_revision_evidence_record_ids=(),
        drifted_revision_record_ids=tuple(drift), edge_count=0,
        dangling_edge_count=0, relation_types=())


def entry(value, at="2026-08-28T12:00:00Z"):
    return WorldviewShadowEvidenceEntry.from_report(value, observed_at=at)


def test_window_is_immutable_bounded_and_evicts_oldest():
    first = WorldviewShadowEvidenceWindow(capacity=2)
    second = first.append(entry(report(snapshot="a")))
    third = second.append(entry(report(snapshot="b"))).append(entry(report(snapshot="c")))
    assert first.entries == () and len(second.entries) == 1
    assert [item.legacy_snapshot_hash[-1] for item in third.entries] == ["b", "c"]


def test_summary_preserves_failure_classes_and_resets_consecutive_readiness():
    window = WorldviewShadowEvidenceWindow(capacity=5)
    window = window.append(entry(report(snapshot="a")))
    window = window.append(entry(report(drift=("r1",), snapshot="b")))
    window = window.append(entry(report(snapshot="c")))
    summary = window.summary()
    assert summary.samples == 3 and summary.ready_samples == 2
    assert summary.consecutive_ready_samples == 1 and summary.latest_ready
    assert summary.to_dict()["failure_sample_counts"]["drifted_revisions"] == 1


def test_entry_contains_counts_and_identities_not_record_payloads():
    value = entry(report(missing=("secret-record-id",)))
    exposed = repr(value)
    assert "secret-record-id" not in exposed
    assert value.missing_records == 1 and not value.ready


@pytest.mark.parametrize("observed_at", ["not-a-time", "2026-08-28T12:00:00"])
def test_observation_time_must_be_zoned_iso8601(observed_at):
    with pytest.raises(ValueError):
        entry(report(), at=observed_at)


def test_identity_and_capacity_validation_fail_closed():
    bad = report()
    object.__setattr__(bad, "legacy_snapshot_hash", "bad")
    with pytest.raises(ValueError, match="sha256"):
        entry(bad)
    with pytest.raises(ValueError, match="capacity"):
        WorldviewShadowEvidenceWindow(capacity=0)
