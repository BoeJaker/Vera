import pytest

from vera.worldview.canonical_revision_provenance import summarize_provenance


pytestmark = pytest.mark.critical


def test_summary_is_payload_free_and_requires_every_record():
    records = {
        "r1": {"dataset_id": "b", "revision_id": "rev-1", "content_hash": "h1",
               "text": "must not escape"},
        "r2": {"dataset_id": "a", "revision_id": "", "content_hash": "h2",
               "text": "also private"},
    }

    result = summarize_provenance(
        records, enrichment={"enriched": 1, "hash_mismatch": 0})

    assert result == {
        "record_count": 2,
        "complete": 1,
        "missing": {"revision_id": 1, "content_hash": 0},
        "datasets": [
            {"dataset_id": "a", "count": 1},
            {"dataset_id": "b", "count": 1},
        ],
        "canonical_join": {"enriched": 1, "hash_mismatch": 0},
        "eligible": False,
        "retains_content": False,
        "mutates": False,
        "activation_authority": False,
    }
    assert "private" not in repr(result)


def test_empty_or_partial_corpus_is_ineligible():
    assert summarize_provenance({})["eligible"] is False
    partial = summarize_provenance({
        "r1": {"revision_id": "rev-1", "content_hash": ""},
    })
    assert partial["eligible"] is False
    assert partial["missing"] == {"revision_id": 0, "content_hash": 1}


def test_complete_corpus_is_eligible_but_never_authoritative():
    result = summarize_provenance({
        "r1": {"dataset_id": "source", "revision_id": "rev-1",
               "content_hash": "hash-1"},
        "r2": {"dataset_id": "source", "revision_id": "rev-2",
               "content_hash": "hash-2"},
    })

    assert result["eligible"] is True
    assert result["complete"] == 2
    assert result["activation_authority"] is False
