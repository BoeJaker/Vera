import dataclasses

import pytest

from vera.execution.run_protocol import ArtifactRef
from vera.fabric.dataset_provider import DatasetSnapshot
from vera.fabric.lance_format_experiment import (
    FormatArtifactBinding, FormatObservation, LanceFormatExperimentPlan,
    evaluate_lance_format_experiment)


pytestmark = pytest.mark.critical


def digest(character):
    return "sha256:" + character * 64


def snapshot():
    value, _ = DatasetSnapshot.create(
        dataset_id="multimodal", created_at="2026-10-05T00:00:00Z",
        records=({"id": 1, "text": "one"}, {"id": 2, "text": "two"}),
        schema={"id": "int64", "text": "string", "image": "binary"},
        provenance={"source": "fixture", "revision": "7"})
    return value


def binding(value_format):
    media = ("application/vnd.apache.lance" if value_format == "lance"
             else "application/vnd.apache.parquet")
    char = "a" if value_format == "lance" else "b"
    artifact = ArtifactRef(
        f"artifact_{value_format}", f"dataset.{value_format}",
        f"private://formats/{value_format}", digest(char), media, 4096)
    return FormatArtifactBinding(
        value_format, artifact, "2.1.0" if value_format == "lance" else "2.0.0",
        "0.38.0" if value_format == "lance" else "20.0.0",
        f"{value_format}_revision_7")


def plan(previous=True):
    kwargs = ({"previous_snapshot_id": "snap_" + "e" * 64,
               "previous_lance_revision": "lance_revision_6"}
              if previous else {})
    return LanceFormatExperimentPlan(
        snapshot=snapshot(), bindings=(binding("lance"), binding("parquet")),
        modalities=("text", "image", "tabular"),
        query_fixture_ids=("query_random", "query_scan"),
        maximum_read_ms=500, maximum_peak_memory_bytes=1024 * 1024,
        **kwargs)


def observation(value_plan, value_format, **changes):
    value_binding = {item.format: item for item in value_plan.bindings}[value_format]
    evidence = [
        ("cleanup", digest("2")), ("corruption", digest("3")),
        ("portable_reader", digest("4")), ("removal", digest("5")),
    ]
    if value_format == "lance":
        evidence.extend((("schema_evolution", digest("6")),
                         ("transaction", digest("7"))))
        if value_plan.previous_snapshot_id:
            evidence.append(("time_travel", digest("8")))
    kwargs = dict(
        plan_id=value_plan.plan_id, format=value_format,
        snapshot_id=value_plan.snapshot.snapshot_id,
        artifact_checksum=value_binding.to_dict()["checksum"],
        revision=value_binding.revision, schema_digest=value_plan.schema_digest,
        record_count=value_plan.snapshot.record_count,
        query_result_digests=(("query_random", digest("c")),
                              ("query_scan", digest("d"))),
        modality_digests=(("image", digest("e")), ("tabular", digest("f")),
                          ("text", digest("1"))),
        evidence_digests=tuple(evidence),
        read_ms=100, peak_memory_bytes=1000, corruption_rejected=True,
        portable_reader_verified=True, removal_verified=True,
        cleanup_verified=True,
        transaction_verified=value_format == "lance",
        schema_evolution_verified=value_format == "lance",
        time_travel_snapshot_id=(value_plan.previous_snapshot_id
                                 if value_format == "lance" else ""),
        time_travel_revision=(value_plan.previous_lance_revision
                              if value_format == "lance" else ""))
    kwargs.update(changes)
    return FormatObservation(**kwargs)


def test_plan_is_deterministic_payload_safe_and_non_authoritative():
    first, second = plan(), plan()
    assert first.plan_id == second.plan_id
    value = first.to_dict()
    assert value["record_count"] == 2
    assert value["modalities"] == ["image", "tabular", "text"]
    assert value["effect"] == "none"
    assert value["canonical_store_authority"] is False
    assert "private://" not in repr(value)


def test_exact_matching_evidence_yields_only_an_experimental_candidate():
    value_plan = plan()
    result = evaluate_lance_format_experiment(
        value_plan, (observation(value_plan, "lance"),
                     observation(value_plan, "parquet")))
    assert result.verdict == "experimental_candidate"
    assert result.reasons == ()
    assert result.to_dict()["migration_authority"] is False
    assert result.to_dict()["query_routing_changed"] is False


@pytest.mark.parametrize("field,value", [
    ("snapshot_id", "snap_" + "9" * 64),
    ("artifact_checksum", digest("9")),
    ("revision", "other_revision"),
    ("schema_digest", digest("8")),
    ("record_count", 3),
])
def test_identity_and_schema_drift_fail_closed(field, value):
    value_plan = plan()
    with pytest.raises(ValueError, match="identity drifted"):
        evaluate_lance_format_experiment(
            value_plan, (observation(value_plan, "lance", **{field: value}),
                         observation(value_plan, "parquet")))


def test_fixture_and_modality_coverage_fail_closed():
    value_plan = plan()
    with pytest.raises(ValueError, match="query evidence"):
        evaluate_lance_format_experiment(
            value_plan, (observation(
                value_plan, "lance",
                query_result_digests=(("query_scan", digest("d")),)),
                observation(value_plan, "parquet")))
    with pytest.raises(ValueError, match="modality evidence"):
        evaluate_lance_format_experiment(
            value_plan, (observation(
                value_plan, "lance",
                modality_digests=(("text", digest("1")),)),
                observation(value_plan, "parquet")))


def test_quality_resource_and_cleanup_gaps_reject_without_authority():
    value_plan = plan()
    lance = observation(
        value_plan, "lance", read_ms=501,
        query_result_digests=(("query_random", digest("7")),
                              ("query_scan", digest("d"))),
        transaction_verified=False, cleanup_verified=False,
        time_travel_snapshot_id="snap_" + "9" * 64,
        time_travel_revision="wrong_revision")
    parquet = observation(
        value_plan, "parquet", peak_memory_bytes=2 * 1024 * 1024,
        removal_verified=False)
    result = evaluate_lance_format_experiment(value_plan, (lance, parquet))
    assert result.verdict == "rejected"
    assert "query_result_mismatch" in result.reasons
    assert "lance_cleanup_verified_missing" in result.reasons
    assert "lance_time_travel_evidence_missing" in result.reasons
    assert "parquet_memory_budget_exceeded" in result.reasons
    assert result.to_dict()["artifacts_deleted"] is False


@pytest.mark.parametrize("changes", [
    {"read_ms": float("nan")},
    {"peak_memory_bytes": True},
    {"query_result_digests": (("query_scan", "bad"),)},
    {"evidence_digests": (("cleanup", "bad"),)},
    {"cleanup_verified": "yes"},
])
def test_invalid_observation_metrics_and_evidence_are_rejected(changes):
    with pytest.raises((TypeError, ValueError)):
        observation(plan(), "lance", **changes)


def test_plan_rejects_wrong_media_duplicate_artifacts_and_partial_history():
    with pytest.raises(ValueError, match="type and size"):
        FormatArtifactBinding(
            "lance", ArtifactRef("a", "dataset.lance", "x", digest("a"),
                                 "application/octet-stream", 1),
            "2.1.0", "0.38.0", "revision")
    value = binding("lance")
    with pytest.raises(ValueError, match="one Lance and one Parquet"):
        LanceFormatExperimentPlan(
            snapshot=snapshot(), bindings=(value, value), modalities=("text",),
            query_fixture_ids=("query",), maximum_read_ms=1,
            maximum_peak_memory_bytes=1)
    with pytest.raises(ValueError, match="both prior identities"):
        LanceFormatExperimentPlan(
            snapshot=snapshot(), bindings=(binding("lance"), binding("parquet")),
            modalities=("text",), query_fixture_ids=("query",),
            maximum_read_ms=1, maximum_peak_memory_bytes=1,
            previous_snapshot_id="snap_" + "e" * 64)


def test_unplanned_time_travel_claim_is_rejected():
    value_plan = plan(previous=False)
    with pytest.raises(ValueError, match="unplanned"):
        evaluate_lance_format_experiment(
            value_plan, (observation(
                value_plan, "lance",
                time_travel_snapshot_id="snap_" + "9" * 64,
                time_travel_revision="revision_6"),
                observation(value_plan, "parquet")))


def test_receipt_coverage_is_exact_and_required():
    value_plan = plan()
    with pytest.raises(ValueError, match="receipt evidence"):
        evaluate_lance_format_experiment(
            value_plan, (observation(
                value_plan, "lance",
                evidence_digests=(("cleanup", digest("2")),)),
                observation(value_plan, "parquet")))
