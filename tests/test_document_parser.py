import hashlib
import json

import pytest

from vera.providers.document_parser import (
    DOCUMENT_PARSE_RESULT_SCHEMA,
    compile_document_parse_plan,
    deterministic_element_id,
    document_parser_status,
    document_parser_teardown_plan,
    evaluate_frozen_document_corpus,
    validate_document_parse_result,
)


pytestmark = pytest.mark.critical


def sha(char):
    return "sha256:" + char * 64


def digest(values):
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def source(**overrides):
    value = {
        "id": "artifact-source-1", "kind": "document.original",
        "uri": "artifact://documents/source-1", "checksum": sha("a"),
        "media_type": "application/pdf", "size_bytes": 4096,
    }
    value.update(overrides)
    return value


def plan(**overrides):
    values = dict(artifact=source(), inspection={
        "state": "inspected", "page_count": 2, "requires_ocr": False,
        "encrypted": False, "corrupt": False,
        "inspection_ref": "inspection:source-1",
    }, ocr_policy="disabled", resource_limits={"derived_artifact_bytes": 4096})
    values.update(overrides)
    return compile_document_parse_plan(**values)


def result(parse_plan=None, **overrides):
    parse_plan = parse_plan or plan()
    locator = {"bbox": [10, 20, 300, 80], "page_width": 600,
               "page_height": 800}
    element = {
        "id": deterministic_element_id(source()["checksum"], page=1, ordinal=0,
                                       kind="paragraph", locator=locator),
        "kind": "paragraph", "page": 1, "ordinal": 0, "locator": locator,
        "text_hash": sha("b"), "structure_hash": sha("c"),
        "citation": {"source_artifact_id": source()["id"],
                     "source_checksum": source()["checksum"], "page": 1,
                     "locator": locator},
    }
    value = {
        "schema": DOCUMENT_PARSE_RESULT_SCHEMA, "plan_id": parse_plan["plan_id"],
        "provider": "docling", "provider_version": "2.0.0",
        "config_hash": sha("d"), "source": source(), "elements": [element],
        "derived_artifacts": [{
            "id": "artifact-derived-1", "kind": "document.figure",
            "uri": "artifact://documents/derived-1", "checksum": sha("e"),
            "media_type": "image/png", "size_bytes": 1024,
        }],
        "ocr": {"requested": False, "used": False, "engine": "", "languages": []},
        "cancelled": False,
    }
    value.update(overrides)
    return value


def test_status_is_honest_and_does_not_claim_docling_execution():
    status = document_parser_status()
    assert status["contract"] == "implemented"
    assert status["provider_profiles"][0]["availability"] == "not_imported"
    assert status["parser_execution"] == "queued_live"
    assert status["ocr_execution"] == "queued_live"
    assert status["optional_provider_imported"] is False
    assert status["files_read"] is False
    assert status["executes"] is False
    assert status["render_policy"] == "separate_product_policy"


def test_plan_is_stable_bounded_and_non_executing():
    first = plan()
    second = plan()
    assert first == second
    assert first["accepted"] is True
    assert first["inspection_trusted"] is True
    assert first["ready_for_execution"] is False
    assert first["record_authority"] == "proposal_until_verified_and_stored"
    assert first["parser_imported"] is False
    assert first["files_read"] is False
    assert first["executes"] is False

    too_large = plan(artifact=source(size_bytes=8192),
                     resource_limits={"max_bytes": 4096})
    assert too_large["accepted"] is False
    assert too_large["issues"] == [{
        "code": "source_size_exceeds_limit", "path": "artifact.size_bytes"}]


@pytest.mark.parametrize("inspection,ocr_policy,code", [
    ({"state": "inspected", "encrypted": True}, "disabled",
     "encrypted_document_unsupported"),
    ({"state": "inspected", "corrupt": True}, "disabled", "corrupt_document"),
    ({"state": "inspected", "requires_ocr": True}, "disabled",
     "ocr_required_but_disabled"),
])
def test_plan_fails_closed_for_encrypted_corrupt_and_undeclared_ocr(
        inspection, ocr_policy, code):
    value = plan(inspection=inspection, ocr_policy=ocr_policy)
    assert value["accepted"] is False
    assert code in {issue["code"] for issue in value["issues"]}
    assert value["executes"] is False


def test_cancellation_is_terminal_before_provider_or_file_access():
    value = plan(cancelled=True)
    assert value["status"] == "cancelled"
    assert value["accepted"] is False
    assert value["files_read"] is False
    assert value["parser_imported"] is False
    assert value["executes"] is False


def test_element_ids_are_stable_and_bind_source_page_kind_and_locator():
    locator = {"bbox": [1, 2, 3, 4]}
    first = deterministic_element_id(sha("a"), page=1, ordinal=0,
                                     kind="paragraph", locator=locator)
    assert first == deterministic_element_id(sha("a"), page=1, ordinal=0,
                                             kind="paragraph", locator=locator)
    assert first != deterministic_element_id(sha("a"), page=2, ordinal=0,
                                             kind="paragraph", locator=locator)
    assert first != deterministic_element_id(sha("f"), page=1, ordinal=0,
                                             kind="paragraph", locator=locator)


def test_result_validation_preserves_provenance_citations_and_content_privacy():
    validation = validate_document_parse_result(plan(), result())
    assert validation["valid"] is True
    assert validation["element_count"] == 1
    assert validation["derived_artifact_count"] == 1
    assert validation["fidelity"] == {
        "text_hash": digest([sha("b")]),
        "table_hash": digest([]),
        "layout_hash": digest([sha("c")]),
    }
    assert validation["content_returned"] is False
    assert validation["records_written"] == 0
    assert validation["artifacts_written"] == 0
    assert validation["executes"] is False


def test_result_rejects_forged_element_citation_and_source_identity():
    forged = result()
    forged["elements"][0]["id"] = "docel_forged"
    forged["elements"][0]["citation"]["source_checksum"] = sha("f")
    forged["source"] = source(checksum=sha("f"))
    validation = validate_document_parse_result(plan(), forged)
    assert validation["valid"] is False
    assert {issue["code"] for issue in validation["issues"]} >= {
        "source_identity_mismatch", "element_invalid"}
    assert validation["content_returned"] is False


def test_result_enforces_derived_budget_and_ocr_declaration():
    oversized = result()
    oversized["derived_artifacts"][0]["size_bytes"] = 4097
    oversized["ocr"] = {"requested": True, "used": True,
                        "engine": "", "languages": []}
    validation = validate_document_parse_result(plan(), oversized)
    codes = {issue["code"] for issue in validation["issues"]}
    assert "derived_artifact_budget_exceeded" in codes
    assert "ocr_provenance_missing" in codes
    assert "ocr_used_against_policy" in codes


def test_duplicate_positions_artifacts_and_unbounded_ocr_metadata_fail_closed():
    parsed = result()
    duplicate_element = dict(parsed["elements"][0])
    duplicate_element["locator"] = {"bbox": [20, 30, 40, 50]}
    duplicate_element["id"] = deterministic_element_id(
        source()["checksum"], page=1, ordinal=0, kind="paragraph",
        locator=duplicate_element["locator"])
    duplicate_element["citation"] = dict(duplicate_element["citation"])
    duplicate_element["citation"]["locator"] = duplicate_element["locator"]
    parsed["elements"].append(duplicate_element)
    parsed["derived_artifacts"].append(dict(parsed["derived_artifacts"][0]))
    parsed["ocr"] = {"requested": True, "used": True,
                     "engine": "e" * 121, "languages": ["en"] * 33}
    validation = validate_document_parse_result(plan(), parsed)
    codes = {issue["code"] for issue in validation["issues"]}
    assert "element_invalid" in codes
    assert "derived_artifact_invalid" in codes
    assert "ocr_provenance_invalid" in codes


def test_frozen_corpus_evaluation_uses_hashes_not_document_content():
    parse_plan = plan()
    parsed = result(parse_plan)
    report = evaluate_frozen_document_corpus([{
        "case_id": "pdf-text-layout-1", "plan": parse_plan, "result": parsed,
        "expected": {"accepted": True, "text_hash": digest([sha("b")]),
                     "table_hash": digest([]),
                     "layout_hash": digest([sha("c")])},
    }])
    assert report["passed"] is True
    assert report["matched"] == 1
    assert report["content_returned"] is False
    assert "elements" not in report["outcomes"][0]
    assert report["provider_imported"] is False
    assert report["executes"] is False


def test_frozen_corpus_bounds_and_duplicate_ids_fail_closed():
    parse_plan = plan()
    case = {"case_id": "same", "plan": parse_plan, "result": result(parse_plan),
            "expected": {"accepted": True, "text_hash": digest([sha("b")]),
                         "table_hash": digest([]),
                         "layout_hash": digest([sha("c")])}}
    with pytest.raises(ValueError, match="unique"):
        evaluate_frozen_document_corpus([case, dict(case)])
    with pytest.raises(ValueError, match="1 to 64"):
        evaluate_frozen_document_corpus([])
    malformed = dict(case)
    malformed["case_id"] = "malformed"
    malformed["expected"] = dict(case["expected"], accepted="yes")
    with pytest.raises(TypeError, match="must be boolean"):
        evaluate_frozen_document_corpus([malformed])


def test_teardown_is_inert_and_never_deletes_authoritative_data():
    teardown = document_parser_teardown_plan(plan())
    assert teardown["requires_receipt_verification"] is True
    assert teardown["deletes_source_artifact"] is False
    assert teardown["deletes_verified_records"] is False
    assert teardown["action_started"] is False
    assert teardown["executes"] is False
    forged = dict(plan())
    forged["source"] = source(checksum=sha("f"))
    with pytest.raises(ValueError, match="identity mismatch"):
        document_parser_teardown_plan(forged)


def test_inspection_reference_is_bounded_before_plan_identity_hashing():
    with pytest.raises(ValueError, match="inspection.inspection_ref"):
        plan(inspection={"state": "inspected", "inspection_ref": "x" * 513})


def test_provider_and_agent_bridge_surfaces_are_registered_in_source():
    provider_source = open("vera/providers/providers_capabilities.py",
                           encoding="utf-8").read()
    bridge_source = open("vera/agentbridges/agentbridge_capabilities.py",
                         encoding="utf-8").read()
    provider_panel = open("vera/providers/providers_panel.html", encoding="utf-8").read()
    bridge_panel = open("vera/agentbridges/agentbridge_catalog_panel.html",
                        encoding="utf-8").read()
    for cap in ("providers.document.status", "providers.document.plan",
                "providers.document.validate", "providers.document.corpus.evaluate",
                "providers.document.teardown.plan"):
        assert cap in provider_source
        assert cap in bridge_source
    assert "Document parser" in provider_panel
    assert "Document parsing" in bridge_panel
