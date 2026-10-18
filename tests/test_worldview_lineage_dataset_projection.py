from dataclasses import replace
import hashlib
import json

import pytest

from vera.fabric.dataset_provider import DatasetSnapshot
from vera.godseye.portable_dataset import (
    PortableGeospatialDataset, make_portable_dataset,
)
from vera.worldview.lineage_dataset_projection import project_lineage_dataset


pytestmark = pytest.mark.critical
NOW = "2026-10-18T12:00:00Z"


def build(*, source="godseye"):
    return make_portable_dataset(
        kind="imagery",
        records=[
            {"id": "b", "lat": 51.5, "lng": -0.1, "license": "CC-BY"},
            {"id": "a", "lat": 34.1, "lng": -118.2, "license": "public"},
        ],
        source_revision="capture-42", created_at=NOW, source=source,
    )


@pytest.mark.parametrize("source,lineage", [
    ("godseye", "godseye.integrated"),
    ("worldview-non-jepa", "worldview.non-jepa"),
])
def test_projection_preserves_lineage_snapshot_revision_and_hashes(source, lineage):
    dataset = build(source=source)
    result = project_lineage_dataset(dataset)
    assert result.source_lineage == lineage
    assert result.source_revision == "capture-42"
    assert result.source_artifact_id == dataset.artifact_id
    assert result.snapshot_id == dataset.snapshot.snapshot_id
    assert result.record_count == 2
    assert result.citation_revisions() == {
        row["record_id"]: row["record_revision"] for row in dataset.records}
    assert all(value.startswith("sha256:")
               for _, value in result.record_content_hashes)
    public = result.to_dict()
    assert public["authority"] == "candidate_evidence_only"
    assert public["eligible_for_influence"] is False
    assert public["executes"] is False
    assert public["runtime_changed"] is False


def test_projection_identity_is_stable_and_changes_with_source_authority():
    first = project_lineage_dataset(build())
    replay = project_lineage_dataset(build())
    non_jepa = project_lineage_dataset(build(source="worldview-non-jepa"))
    assert first == replay
    assert first.projection_id != non_jepa.projection_id
    assert first.projection_id.startswith("wvlproj_")


def test_projection_manifest_is_payload_free():
    public = project_lineage_dataset(build()).to_dict()
    encoded = json.dumps(public, sort_keys=True)
    for forbidden in ('"lat"', '"lng"', '"license"', '"records"',
                      '"payload"', '"text"', '"embedding"'):
        assert forbidden not in encoded


def test_snapshot_record_and_artifact_tampering_fail_closed():
    dataset = build()
    bad_snapshot = DatasetSnapshot(
        dataset_id=dataset.snapshot.dataset_id,
        snapshot_id=dataset.snapshot.snapshot_id,
        created_at=dataset.snapshot.created_at,
        record_count=dataset.snapshot.record_count + 1,
        schema=dataset.snapshot.schema,
        provenance=dataset.snapshot.provenance,
    )
    with pytest.raises(ValueError, match="snapshot identity"):
        project_lineage_dataset(replace(dataset, snapshot=bad_snapshot))

    records = list(dataset.records)
    records[0] = {**records[0], "license": "changed"}
    with pytest.raises(ValueError, match="snapshot identity"):
        project_lineage_dataset(replace(dataset, records=tuple(records)))

    with pytest.raises(ValueError, match="artifact identity"):
        project_lineage_dataset(replace(
            dataset, artifact_id="artifact_sha256:" + "0" * 64))


def test_authority_and_lineage_forgery_fail_closed_even_when_rehashed():
    dataset = build()
    provenance = {**dataset.snapshot.provenance, "jepa_authority": True}
    snapshot, records = DatasetSnapshot.create(
        dataset_id=dataset.snapshot.dataset_id,
        created_at=dataset.snapshot.created_at,
        records=dataset.records,
        schema=dataset.snapshot.schema,
        provenance=provenance,
    )
    artifact = "artifact_sha256:" + hashlib.sha256(json.dumps(
        {"snapshot_id": snapshot.snapshot_id, "records": records},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    forged = PortableGeospatialDataset(
        snapshot=snapshot, records=records, artifact_id=artifact,
        kind=dataset.kind,
    )
    with pytest.raises(ValueError, match="must not claim JEPA authority"):
        project_lineage_dataset(forged)


def test_forged_record_revision_fails_even_when_snapshot_and_artifact_match():
    dataset = build()
    records = list(dataset.records)
    records[0] = {**records[0], "record_revision": "rev_" + "0" * 64}
    snapshot, records = DatasetSnapshot.create(
        dataset_id=dataset.snapshot.dataset_id,
        created_at=dataset.snapshot.created_at,
        records=records,
        schema=dataset.snapshot.schema,
        provenance=dataset.snapshot.provenance,
    )
    artifact = "artifact_sha256:" + hashlib.sha256(json.dumps(
        {"snapshot_id": snapshot.snapshot_id, "records": records},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    forged = PortableGeospatialDataset(
        snapshot=snapshot, records=records, artifact_id=artifact,
        kind=dataset.kind,
    )
    with pytest.raises(ValueError, match="revision does not verify"):
        project_lineage_dataset(forged)


def test_projection_module_has_no_runtime_model_or_provider_dependency():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "worldview" /
              "lineage_dataset_projection.py").read_text(encoding="utf-8")
    for forbidden in ("worldview_jepa import", "godseye_capabilities import",
                      "requests", "httpx", "torch", "tensorflow",
                      "subprocess", "capability_orchestration import"):
        assert forbidden not in source
