import pytest

from vera.godseye.portable_dataset import (
    MAX_GEOMETRY_COORDINATES, MAX_RECORD_BYTES, MAX_RECORDS,
    make_portable_dataset,
)


pytestmark = pytest.mark.critical
NOW = "2026-09-29T10:00:00Z"


def build(kind, records, **extra):
    return make_portable_dataset(kind=kind, records=records,
                                 source_revision="capture-42", created_at=NOW,
                                 **extra)


def test_cctv_snapshot_is_order_independent_and_revision_qualified():
    rows = [
        {"id": "b", "lat": 51.5, "lng": -0.1, "provider": "TfL"},
        {"id": "a", "lat": 34.1, "lng": -118.2, "provider": "Caltrans"},
    ]
    first = build("cctv", rows)
    replay = build("cctv", list(reversed(rows)))
    assert first.snapshot.snapshot_id == replay.snapshot.snapshot_id
    assert first.artifact_id == replay.artifact_id
    assert [row["record_id"] for row in first.records] == ["a", "b"]
    assert all(row["record_revision"].startswith("rev_") for row in first.records)
    assert first.snapshot.provenance["jepa_authority"] is False


def test_all_current_godseye_shapes_are_portable():
    imagery = build("imagery", [{"id": "photo-1", "lat": 1, "lng": 2,
                                  "license": "CC BY", "capturedAt": "2026-01-01"}])
    buildings = build("buildings", [{"id": "way-1", "coords": [2, 1, 3, 1, 3, 2],
                                      "height": 8.0}])
    assert imagery.snapshot.dataset_id == "godseye.imagery"
    assert buildings.snapshot.schema["geospatial_kind"] == "buildings"
    assert buildings.manifest()["artifact_id"].startswith("artifact_sha256:")


def test_non_jepa_worldview_is_explicit_and_never_claims_jepa_authority():
    value = build("imagery", [{"id": "x", "lat": 1, "lng": 2}],
                  source="worldview-non-jepa")
    assert value.snapshot.dataset_id == "worldview-non-jepa.imagery"
    assert value.snapshot.provenance["lineage"] == "non-jepa-worldview-godseye"
    assert value.snapshot.provenance["jepa_authority"] is False


@pytest.mark.parametrize("records, message", [
    ([{"id": "x", "lat": 1, "lng": 2}, {"id": "x", "lat": 2, "lng": 3}], "unique"),
    ([{"id": "x", "lat": 91, "lng": 2}], "coordinate"),
    ([{"id": "x", "lat": float("nan"), "lng": 2}], "coordinate"),
])
def test_invalid_or_ambiguous_records_fail_closed(records, message):
    with pytest.raises(ValueError, match=message):
        build("imagery", records)


def test_building_geometry_and_bounds_fail_closed():
    with pytest.raises(ValueError, match="coords"):
        build("buildings", [{"id": "x", "coords": [1, 2]}])
    with pytest.raises(ValueError, match="at most"):
        build("cctv", [{"id": str(i), "lat": 1, "lng": 2}
                       for i in range(MAX_RECORDS + 1)])
    with pytest.raises(ValueError, match="coordinate limit"):
        build("buildings", [{"id": "x", "coords": [1, 2] *
                              (MAX_GEOMETRY_COORDINATES // 2 + 1)}])
    with pytest.raises(ValueError, match="encoded size"):
        build("imagery", [{"id": "x", "lat": 1, "lng": 2,
                           "title": "x" * MAX_RECORD_BYTES}])


def test_revision_and_created_time_change_authority():
    rows = [{"id": "x", "lat": 1, "lng": 2}]
    first = build("cctv", rows)
    revised = make_portable_dataset(kind="cctv", records=rows,
                                    source_revision="capture-43", created_at=NOW)
    later = make_portable_dataset(kind="cctv", records=rows,
                                  source_revision="capture-42",
                                  created_at="2026-09-29T11:00:00Z")
    assert len({first.snapshot.snapshot_id, revised.snapshot.snapshot_id,
                later.snapshot.snapshot_id}) == 3
