from vera.intel.intel_record_reconciliation import (
    REPORT_DATASET,
    SCHEMA,
    SIGNAL_DATASET,
    family_contracts,
    fabric_business_id,
    reconcile_ids,
    result_envelope,
    unavailable_reconciliation,
)


def test_family_contracts_classify_every_native_record_kind_without_inventing_memory():
    rows = {row["id"]: row for row in family_contracts()}
    assert set(rows) == {
        "topics", "signals", "snapshots", "reports", "decisions_outcomes",
        "actions_deliveries", "configuration",
    }
    assert rows["signals"]["shared_contract"] == f"fabric.dataset:{SIGNAL_DATASET}"
    assert rows["reports"]["shared_contract"] == "explicit_delivery_only"
    assert rows["reports"]["memory_projection"]["status"] == "delivery_evidence_only"
    assert rows["reports"]["memory_projection"]["condition"] == "explicit_successful_memory_delivery"
    assert rows["topics"]["portable_record"] == {"applicable": False, "reason": "native_control_state"}
    assert rows["decisions_outcomes"]["shared_contract"] == "four_minds.decision_outcome"
    assert rows["actions_deliveries"]["portable_record"]["applicable"] is False
    assert REPORT_DATASET == "intel.reports"


def test_fabric_business_id_prefers_producer_id_and_understands_signal_physical_id():
    assert fabric_business_id({"id": "random", "data": {"id": "sig-a"}}, family="signals") == "sig-a"
    assert fabric_business_id({"id": "intel-sig-sig-b", "data": {}}, family="signals") == "sig-b"
    assert fabric_business_id({"id": "random", "data": {}}, family="reports") == ""


def test_reconcile_ids_reports_matches_missing_unexpected_and_unidentified():
    result = reconcile_ids(
        family="signals", local_ids=["a", "b"], local_total=2,
        shared_records=[
            {"id": "intel-sig-a", "data": {"id": "a"}},
            {"id": "intel-sig-c", "data": {"id": "c"}},
            {"id": "opaque", "data": {}},
        ],
        shared_total=3, local_complete=True, shared_complete=True,
    )
    assert result["status"] == "gaps"
    assert result["matched"] == 1
    assert result["missing_ids"] == ["b"]
    assert result["unexpected_ids"] == ["c"]
    assert result["shared"]["unidentified"] == 1


def test_reports_compare_only_explicit_successful_fabric_deliveries():
    result = reconcile_ids(
        family="reports", local_ids=["r1", "r2"], expected_shared_ids=["r2"],
        local_total=2, shared_total=1, local_complete=True, shared_complete=True,
        shared_records=[{"id": "random", "data": {"id": "r2"}}],
    )
    assert result["status"] == "matched"
    assert result["expected_shared"] == 1
    assert result["matched"] == 1


def test_bounded_and_unavailable_results_do_not_claim_absence_as_a_gap():
    bounded = reconcile_ids(
        family="signals", local_ids=["a"], local_total=9,
        shared_records=[], shared_total=12,
        local_complete=False, shared_complete=False,
    )
    assert bounded["status"] == "bounded_sample"
    assert bounded["coverage_complete"] is False

    unavailable = unavailable_reconciliation(
        family="signals", local_total=9, local_sampled=1, error="down")
    assert unavailable["status"] == "shared_source_unavailable"
    assert unavailable["missing"] == 0
    assert unavailable["error"] == "down"


def test_envelope_is_explicitly_read_only_and_summarises_reconciliation():
    value = result_envelope(
        families=family_contracts(), limit=200,
        reconciliation={
            "signals": {"status": "matched"},
            "reports": {"status": "shared_source_unavailable"},
        },
    )
    assert value["schema"] == SCHEMA
    assert value["native_authority"] == "intel.sqlite"
    assert value["read_only"] is True
    assert value["executes"] is False
    assert value["writes"] is False
    assert value["summary"] == {"matched": 1, "with_gaps": 0, "bounded": 0, "unavailable": 1}
