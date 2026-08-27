import pytest

from vera.worldview.legacy_snapshot_provenance import (
    MAX_PROVENANCE_VALUE_LENGTH,
    legacy_snapshot_provenance,
)


pytestmark = pytest.mark.critical


def test_preserves_canonical_legacy_provenance_exactly():
    assert legacy_snapshot_provenance({
        "revision_id": "rev_123",
        "content_hash": "a" * 64,
    }) == {"revision_id": "rev_123", "content_hash": "a" * 64}


def test_accepts_projection_content_hash_alias_without_overriding_legacy_key():
    assert legacy_snapshot_provenance({
        "content_hash": "legacy",
        "source_content_hash": "projection",
    })["content_hash"] == "legacy"
    assert legacy_snapshot_provenance({
        "source_content_hash": "projection",
    })["content_hash"] == "projection"


def test_missing_provenance_remains_explicitly_empty():
    assert legacy_snapshot_provenance({"dataset_id": "dataset"}) == {
        "revision_id": "",
        "content_hash": "",
    }
    assert legacy_snapshot_provenance(None) == {
        "revision_id": "",
        "content_hash": "",
    }


def test_untrusted_non_string_and_oversized_values_are_not_evidence():
    result = legacy_snapshot_provenance({
        "revision_id": 123,
        "content_hash": "x" * (MAX_PROVENANCE_VALUE_LENGTH + 1),
    })
    assert result == {"revision_id": "", "content_hash": ""}
