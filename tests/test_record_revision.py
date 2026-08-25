import math

import pytest

from vera.fabric.record_revision import (
    RECORD_REVISION_SCHEMA,
    create_record_revision,
)


pytestmark = pytest.mark.critical
NOW = "2026-01-02T03:04:05Z"


def revision(**overrides):
    values = {
        "namespace": "tenant.project.dataset",
        "record_type": "document",
        "logical_key": "source:item-1",
        "content": {"b": [2, 3], "a": 1},
        "created_at": NOW,
        "source": {"provider": "fabric", "source_id": "item-1"},
        "policy": {"classification": "internal", "acl": []},
    }
    values.update(overrides)
    return create_record_revision(**values)


def test_same_observation_is_idempotent_and_key_order_independent():
    first = revision()
    second = revision(content={"a": 1, "b": [2, 3]},
                      source={"source_id": "item-1", "provider": "fabric"})
    assert first.record_id == second.record_id
    assert first.revision_id == second.revision_id
    assert first.content_hash == second.content_hash
    assert first.to_dict()["schema"] == RECORD_REVISION_SCHEMA


def test_equivalent_timestamp_offsets_have_one_revision_identity():
    first = revision(created_at="2026-01-02T03:04:05Z")
    second = revision(created_at="2026-01-02T04:04:05+01:00")
    assert first.created_at == second.created_at == "2026-01-02T03:04:05Z"
    assert first.revision_id == second.revision_id


def test_record_identity_is_stable_while_observation_revision_changes():
    first = revision(content={"value": 1})
    second = revision(content={"value": 2})
    assert first.record_id == second.record_id
    assert first.revision_id != second.revision_id
    assert first.content_hash != second.content_hash


def test_nested_input_mutation_cannot_change_the_revision():
    content = {"items": [{"value": 1}]}
    metadata = {"labels": ["safe"]}
    value = revision(content=content, metadata=metadata)
    before = value.to_dict()
    content["items"][0]["value"] = 999
    metadata["labels"].append("changed")
    assert value.to_dict() == before
    projected = value.to_dict()
    projected["content"]["inline"]["items"].append({"value": 2})
    assert value.to_dict() == before


@pytest.mark.parametrize("bad", [math.nan, math.inf, b"bytes", {1: "key"}])
def test_non_json_or_ambiguous_content_fails_closed(bad):
    with pytest.raises(ValueError, match="content"):
        revision(content=bad)


def test_lineage_is_ordered_unique_and_bound_into_revision_identity():
    parent_a = "rev_" + "a" * 64
    parent_b = "rev_" + "b" * 64
    first = revision(parents=[parent_a, parent_b])
    reordered = revision(parents=[parent_b, parent_a])
    assert first.revision_id != reordered.revision_id
    assert first.to_dict()["provenance"]["parents"] == [parent_a, parent_b]
    with pytest.raises(ValueError, match="unique"):
        revision(parents=[parent_a, parent_a])
    with pytest.raises(ValueError, match="conflict"):
        revision(parents=[parent_a], provenance={"parents": [parent_b]})


def test_tombstone_is_an_explicit_child_revision_without_payload():
    parent = revision()
    deleted = revision(content=None, tombstone=True, parents=[parent.revision_id])
    assert deleted.record_id == parent.record_id
    assert deleted.tombstone is True
    assert deleted.to_dict()["content"] == {
        "media_type": "application/json", "inline": None, "artifact": {},
        "schema": {}}
    with pytest.raises(ValueError, match="parent"):
        revision(content=None, tombstone=True)
    with pytest.raises(ValueError, match="cannot carry"):
        revision(tombstone=True, parents=[parent.revision_id])


def test_artifact_revision_hashes_reference_without_fetching_bytes():
    value = revision(content=None, media_type="application/pdf",
                     artifact={"id": "art_1", "checksum": "sha256:abc"})
    assert value.content_hash.startswith("sha256:")
    assert value.to_dict()["content"]["artifact"]["id"] == "art_1"


def test_snapshot_and_content_schema_are_first_class_revision_inputs():
    first = revision(snapshot_id="snap_dataset-1",
                     content_schema={"type": "object", "required": ["a"]})
    changed = revision(snapshot_id="snap_dataset-2",
                       content_schema={"type": "object", "required": ["a"]})
    assert first.to_dict()["snapshot_id"] == "snap_dataset-1"
    assert first.to_dict()["content"]["schema"]["type"] == "object"
    assert first.revision_id != changed.revision_id


@pytest.mark.parametrize("created", ["", "2026-01-01", "not-a-date"])
def test_timestamp_requires_explicit_timezone(created):
    with pytest.raises(ValueError, match="created_at"):
        revision(created_at=created)


def test_valid_time_cannot_run_backwards():
    with pytest.raises(ValueError, match="valid_to"):
        revision(valid_from="2026-01-02T00:00:00Z",
                 valid_to="2026-01-01T00:00:00Z")


def test_identity_requires_explicit_record_or_logical_key():
    with pytest.raises(ValueError, match="record_id or logical_key"):
        revision(logical_key="")
    explicit = revision(logical_key="", record_id="rec_external-1")
    assert explicit.record_id == "rec_external-1"


def test_content_metadata_and_lineage_are_structurally_bounded():
    with pytest.raises(ValueError, match="content exceeds"):
        revision(content="x" * 1_048_577)
    with pytest.raises(ValueError, match="metadata exceeds"):
        revision(metadata={"value": "x" * 65_536})
    with pytest.raises(ValueError, match="at most 64"):
        revision(parents=["rev_" + f"{index:064x}" for index in range(65)])


def test_parent_revision_requires_full_canonical_digest():
    with pytest.raises(ValueError, match="invalid parent revision"):
        revision(parents=["rev_short"])
