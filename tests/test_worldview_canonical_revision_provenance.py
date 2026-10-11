import pytest

from vera.worldview.canonical_revision_provenance import enrich_matching_revisions


pytestmark = pytest.mark.critical


def _revision(record_id="r1", revision_id="rev_1", content_hash="hash-a"):
    return {
        "record_id": record_id,
        "revision_id": revision_id,
        "content_hash": content_hash,
    }


def test_enriches_only_an_exact_content_hash_match():
    records = {"r1": {"content_hash": "hash-a", "revision_id": ""}}

    counts = enrich_matching_revisions(
        records, lambda _record_ids: {"r1": _revision()})

    assert records["r1"]["revision_id"] == "rev_1"
    assert counts["enriched"] == 1


@pytest.mark.parametrize("revision", [
    None,
    _revision(content_hash="hash-b"),
    _revision(record_id="other"),
    {"record_id": "r1", "revision_id": "", "content_hash": "hash-a"},
])
def test_refuses_missing_mismatched_or_invalid_revision_authority(revision):
    records = {"r1": {"content_hash": "hash-a", "revision_id": ""}}

    counts = enrich_matching_revisions(
        records, lambda _record_ids: {"r1": revision} if revision else {})

    assert records["r1"]["revision_id"] == ""
    assert counts["enriched"] == 0
    assert sum(counts[key] for key in (
        "missing_revision", "hash_mismatch", "invalid_revision")) == 1


def test_refuses_to_invent_a_missing_projection_hash():
    records = {"r1": {"content_hash": "", "revision_id": ""}}
    called = False

    def resolve(_record_ids):
        nonlocal called
        called = True
        return {"r1": _revision()}

    counts = enrich_matching_revisions(records, resolve)

    assert called is False
    assert records["r1"] == {"content_hash": "", "revision_id": ""}
    assert counts["missing_content_hash"] == 1


def test_preserves_complete_source_metadata_without_lookup():
    records = {"r1": {"content_hash": "source-hash", "revision_id": "source-rev"}}

    counts = enrich_matching_revisions(
        records, lambda _record_ids: pytest.fail("lookup must not run"))

    assert records["r1"]["revision_id"] == "source-rev"
    assert counts["already_complete"] == 1


def test_invalid_bulk_result_is_isolated_per_record():
    records = {
        "broken": {"content_hash": "hash-a", "revision_id": ""},
        "good": {"content_hash": "hash-a", "revision_id": ""},
    }

    def resolve(record_ids):
        assert record_ids == ("broken", "good")
        return {"broken": object(), "good": _revision(record_id="good")}

    counts = enrich_matching_revisions(records, resolve)

    assert records["broken"]["revision_id"] == ""
    assert records["good"]["revision_id"] == "rev_1"
    assert counts["invalid_revision"] == 1
    assert counts["enriched"] == 1


def test_bulk_lookup_failure_is_counted_without_partial_authority():
    records = {
        "r1": {"content_hash": "hash-a", "revision_id": ""},
        "r2": {"content_hash": "hash-b", "revision_id": ""},
    }

    def fail(_record_ids):
        raise RuntimeError("store unavailable")

    counts = enrich_matching_revisions(records, fail)

    assert counts["lookup_error"] == 2
    assert counts["enriched"] == 0
