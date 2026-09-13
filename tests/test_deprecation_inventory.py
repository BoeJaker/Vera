import asyncio
import hashlib
import json

import pytest

from vera.inventory.deprecation_inventory import (
    ALIAS_USAGE_COUNT_KEY,
    ALIAS_USAGE_META_KEY,
    SOURCE_KINDS,
    DeprecationCandidate,
    DeprecationInventory,
    SourceCoverage,
    UsageObservation,
    alias_usage_field,
    candidates_from_registry,
    observations_from_alias_usage_counts,
    record_alias_usage,
    scan_reference_documents,
)


pytestmark = pytest.mark.critical


def _digest(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _candidate(name="research.deep"):
    return DeprecationCandidate(
        name=name, kind="compatibility_alias", replacement="research.run",
        owner="research", reason_code="duplicate_task_surface",
    )


def _coverage(status="complete"):
    return tuple(SourceCoverage(
        source_kind=source,
        status=status,
        snapshot_digest=_digest(source) if status == "complete" else "",
        reason_code="collector_unavailable" if status != "complete" else "",
    ) for source in SOURCE_KINDS)


def _observation(candidate, source, classification="consumer", count=1, ref="caller"):
    return UsageObservation(
        candidate_id=candidate.candidate_id, source_kind=source,
        classification=classification, source_ref_digest=_digest(ref),
        count=count, window_id="cycle-2026-09",
    )


def test_report_separates_real_consumers_from_health_and_migration_probes():
    candidate = _candidate()
    inventory = DeprecationInventory(
        candidates=(candidate,),
        observations=(
            _observation(candidate, "mcp_caller", count=3, ref="agent"),
            _observation(candidate, "http_caller", "health_check", 20, "health"),
            _observation(candidate, "stored_workflow", "migration_probe", 2, "probe"),
        ),
        coverage=_coverage(),
    )

    report = inventory.report()
    row = report["candidates"][candidate.candidate_id]
    assert row["consumer_evidence"]["total"] == 3
    assert row["consumer_evidence"]["by_source"]["mcp_caller"] == 3
    assert row["consumer_evidence"]["by_source"]["http_caller"] == 0
    assert row["excluded_probes"] == {"health_check": 20, "migration_probe": 2}
    assert row["zero_consumer_evidence"] is False
    assert row["independent_review_candidate"] is False
    assert row["removal_authority"] is False
    assert report["effects"] == [] and report["mutates"] is False


def test_zero_usage_only_becomes_review_evidence_with_complete_coverage():
    candidate = _candidate()
    complete = DeprecationInventory((candidate,), (), _coverage()).report()
    partial = DeprecationInventory((candidate,), (), _coverage("partial")).report()

    assert complete["coverage_complete"] is True
    assert complete["candidates"][candidate.candidate_id][
        "independent_review_candidate"] is True
    assert partial["coverage_complete"] is False
    assert partial["candidates"][candidate.candidate_id][
        "independent_review_candidate"] is False
    assert partial["removal_authority"] is False


def test_identity_is_deterministic_and_payload_free():
    candidate = _candidate()
    observation = _observation(candidate, "code_reference", ref="private/file.py:20")
    first = DeprecationInventory((candidate,), (observation,), _coverage())
    second = DeprecationInventory((candidate,), (observation,), tuple(reversed(_coverage())))

    assert first.inventory_id == second.inventory_id
    encoded = repr(first.to_dict())
    assert "private/file.py" not in encoded
    assert observation.source_ref_digest in encoded


def test_inventory_requires_every_source_and_rejects_unknown_candidates():
    candidate = _candidate()
    with pytest.raises(ValueError, match="every required source"):
        DeprecationInventory((candidate,), (), _coverage()[:-1])

    other = _candidate("research.code")
    with pytest.raises(ValueError, match="inventory candidates"):
        DeprecationInventory(
            (candidate,), (_observation(other, "schedule"),), _coverage())


def test_duplicate_observations_are_rejected_instead_of_double_counted():
    candidate = _candidate()
    observation = _observation(candidate, "configuration")
    with pytest.raises(ValueError, match="observation identities"):
        DeprecationInventory(
            (candidate,), (observation, observation), _coverage())


@pytest.mark.parametrize("classification", ["probe", "poll", "unknown"])
def test_probe_exclusion_requires_an_explicit_supported_classification(classification):
    candidate = _candidate()
    with pytest.raises(ValueError, match="observation classification"):
        _observation(candidate, "mcp_caller", classification)


def test_incomplete_coverage_requires_a_machine_readable_reason():
    with pytest.raises(ValueError, match="requires a reason"):
        SourceCoverage("external_consumer", "unavailable")
    with pytest.raises(ValueError, match="requires a snapshot"):
        SourceCoverage("external_consumer", "complete")


def test_bool_counts_and_self_replacements_fail_closed():
    candidate = _candidate()
    with pytest.raises(ValueError, match="non-negative integer"):
        _observation(candidate, "artifact", count=True)
    with pytest.raises(ValueError, match="must differ"):
        DeprecationCandidate(
            "old.cap", "compatibility_alias", "old.cap", "owner", "duplicate")


class _Redis:
    def __init__(self):
        self.counts = {}
        self.metadata = {}

    async def hincrby(self, key, field, amount):
        assert key == ALIAS_USAGE_COUNT_KEY
        self.counts[field] = self.counts.get(field, 0) + amount

    async def hset(self, key, field, value):
        assert key == ALIAS_USAGE_META_KEY
        self.metadata[field] = value


def test_alias_usage_recorder_is_bounded_payload_free_and_conservative():
    redis = _Redis()
    assert asyncio.run(record_alias_usage(
        redis, candidate_name="research.deep", replacement="research.run",
        source_kind="mcp_caller", observed_at="2026-09-12T00:00:00Z")) is True
    field = alias_usage_field("research.deep", "mcp_caller")
    assert redis.counts == {field: 1}
    assert json.loads(redis.metadata[field]) == {
        "candidate": "research.deep", "replacement": "research.run",
        "source_kind": "mcp_caller", "classification": "consumer",
        "last_observed_at": "2026-09-12T00:00:00Z",
    }
    assert "prompt" not in redis.metadata[field]
    assert asyncio.run(record_alias_usage(
        None, candidate_name="research.deep", replacement="research.run",
        source_kind="mcp_caller", observed_at="ignored")) is False
    with pytest.raises(ValueError, match="HTTP or MCP"):
        alias_usage_field("research.deep", "health_check")


def test_capability_alias_metadata_and_server_owned_transport_markers(monkeypatch):
    from vera import capability_orchestration as orchestration

    redis = _Redis()
    monkeypatch.setattr(orchestration, "REDIS", redis)
    cap_name = "test.compatibility.alias"

    @orchestration.capability(
        cap_name, silent=True, memory="off",
        compatibility_alias_for="test.canonical",
    )
    async def alias_cap(trace_id=None):
        return {"ok": True}

    try:
        entry = orchestration.CAPABILITY_REGISTRY[cap_name]
        assert entry["source"] == "alias"
        assert entry["compatibility_alias_for"] == "test.canonical"

        asyncio.run(entry["func"]())
        assert redis.counts == {}

        token = orchestration.CURRENT_HTTP_CAP.set(cap_name)
        try:
            asyncio.run(entry["func"]())
        finally:
            orchestration.CURRENT_HTTP_CAP.reset(token)

        token = orchestration.MCP_CALL_ACTIVE.set(True)
        try:
            asyncio.run(entry["func"]())
        finally:
            orchestration.MCP_CALL_ACTIVE.reset(token)

        assert redis.counts == {
            alias_usage_field(cap_name, "http_caller"): 1,
            alias_usage_field(cap_name, "mcp_caller"): 1,
        }
        assert all(json.loads(value)["classification"] == "consumer"
                   for value in redis.metadata.values())
    finally:
        orchestration.CAPABILITY_REGISTRY.pop(cap_name, None)


def test_research_alias_declarations_match_the_single_source_of_truth():
    from pathlib import Path
    from vera.research.alias_compatibility import RESEARCH_ALIASES

    source = (Path(__file__).resolve().parents[1] / "vera" / "research" /
              "researcher_api.py").read_text(encoding="utf-8")
    for name, alias in RESEARCH_ALIASES.items():
        marker = f'@capability("{name}",'
        start = source.index(marker)
        declaration = source[start:start + 280]
        assert f'compatibility_alias_for="{alias.replacement}"' in declaration


def test_registry_projection_uses_only_explicit_alias_metadata():
    candidates = candidates_from_registry({
        "old.cap": {"source": "local", "compatibility_alias_for": "new.cap"},
        "legacy-looking.cap": {"source": "alias"},
        "new.cap": {"source": "local", "compatibility_alias_for": ""},
    })
    assert [item.name for item in candidates] == ["old.cap"]
    assert candidates[0].replacement == "new.cap"


def test_reference_scanner_counts_exact_tokens_and_retains_no_source_text():
    candidate = _candidate()
    observations = scan_reference_documents(
        (candidate,), source_kind="ui_link", window_id="snapshot-1",
        documents={
            "vera/panel.js": (
                "open('research.deep'); research.deeper; research.deep_extra; "
                "research.deep"),
        },
    )
    assert len(observations) == 1
    assert observations[0].count == 2
    assert observations[0].classification == "consumer"
    assert "vera/panel.js" not in repr(observations[0].to_dict())


def test_reference_scanner_excludes_only_explicit_probe_documents():
    candidate = _candidate()
    observations = scan_reference_documents(
        (candidate,), source_kind="code_reference", window_id="snapshot-1",
        documents={"health.py": "research.deep", "migration.py": "research.deep"},
        classifications={
            "health.py": "health_check", "migration.py": "migration_probe",
        },
    )
    assert {item.classification for item in observations} == {
        "health_check", "migration_probe"}
    assert all(item.excluded for item in observations)


def test_reference_scanner_rejects_classification_for_absent_document():
    with pytest.raises(ValueError, match="supplied documents"):
        scan_reference_documents(
            (_candidate(),), source_kind="configuration", window_id="snapshot-1",
            documents={}, classifications={"missing": "health_check"})


def test_runtime_counters_reconstruct_consumer_observations_without_payloads():
    candidate = _candidate()
    observations = observations_from_alias_usage_counts(
        {
            alias_usage_field(candidate.name, "http_caller").encode(): b"2",
            alias_usage_field(candidate.name, "mcp_caller"): 3,
        },
        (candidate,), window_id="cycle-1",
    )
    assert sum(item.count for item in observations) == 5
    assert {item.source_kind for item in observations} == {
        "http_caller", "mcp_caller"}
    assert all(item.classification == "consumer" for item in observations)
    assert "http" not in {item.source_ref_digest for item in observations}


def test_runtime_counters_fail_closed_on_orphaned_or_malformed_fields():
    candidate = _candidate()
    with pytest.raises(ValueError, match="undeclared candidate"):
        observations_from_alias_usage_counts(
            {"missing.alias|mcp_caller": 1}, (candidate,), window_id="cycle-1")
    with pytest.raises(ValueError, match="malformed"):
        observations_from_alias_usage_counts(
            {"research.deep|internal": 1}, (candidate,), window_id="cycle-1")
    with pytest.raises(ValueError, match="positive integer"):
        observations_from_alias_usage_counts(
            {"research.deep|mcp_caller": True}, (candidate,), window_id="cycle-1")
