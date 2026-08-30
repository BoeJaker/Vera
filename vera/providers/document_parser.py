"""Portable LIB-06 document parser contracts; deliberately performs no I/O."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence


DOCUMENT_PARSE_PLAN_SCHEMA = "vera.document-parse-plan/v1"
DOCUMENT_PARSE_RESULT_SCHEMA = "vera.document-parse-result/v1"
DOCUMENT_CORPUS_REPORT_SCHEMA = "vera.document-corpus-report/v1"
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA_TYPES = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/html", "text/markdown", "text/plain",
})
_ELEMENT_KINDS = frozenset({
    "caption", "code", "figure", "footnote", "formula", "heading",
    "list_item", "page_header", "page_footer", "paragraph", "table",
})
_OCR_POLICIES = frozenset({"disabled", "auto", "force"})
_MAX_CASES = 64
_MAX_ELEMENTS = 10_000


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _text(value: Any, name: str, maximum: int = 256) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < minimum or value > maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _checksum(value: Any, name: str) -> str:
    value = _text(value, name, 71)
    if not _CHECKSUM.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 checksum")
    return value


def _artifact(raw: Mapping[str, Any], *, source: bool) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("artifact must be an object")
    artifact = {
        "id": _text(raw.get("id"), "artifact.id", 160),
        "kind": _text(raw.get("kind"), "artifact.kind", 80),
        "uri": _text(raw.get("uri"), "artifact.uri", 2048),
        "checksum": _checksum(raw.get("checksum"), "artifact.checksum"),
        "media_type": _text(raw.get("media_type"), "artifact.media_type", 160),
        "size_bytes": _integer(raw.get("size_bytes"), "artifact.size_bytes", 1,
                               512 * 1024 * 1024),
    }
    if source and artifact["media_type"] not in _MEDIA_TYPES:
        raise ValueError("source media_type is outside the portable document set")
    if not artifact["uri"].startswith(("artifact://", "fabric://", "file-ref://")):
        raise ValueError("artifact URI must be an opaque local reference")
    return artifact


def _limits(raw: Mapping[str, Any] | None) -> dict[str, int]:
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise TypeError("resource_limits must be an object")
    allowed = {"max_bytes", "max_pages", "max_elements", "timeout_ms",
               "memory_mb", "derived_artifact_bytes"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError("unknown resource limit: " + unknown[0])
    return {
        "max_bytes": _integer(raw.get("max_bytes", 64 * 1024 * 1024),
                              "max_bytes", 1, 512 * 1024 * 1024),
        "max_pages": _integer(raw.get("max_pages", 2_000), "max_pages", 1, 10_000),
        "max_elements": _integer(raw.get("max_elements", _MAX_ELEMENTS),
                                 "max_elements", 1, _MAX_ELEMENTS),
        "timeout_ms": _integer(raw.get("timeout_ms", 120_000),
                               "timeout_ms", 100, 900_000),
        "memory_mb": _integer(raw.get("memory_mb", 2_048), "memory_mb", 128, 16_384),
        "derived_artifact_bytes": _integer(
            raw.get("derived_artifact_bytes", 128 * 1024 * 1024),
            "derived_artifact_bytes", 1, 512 * 1024 * 1024),
    }


def deterministic_element_id(source_checksum: str, *, page: int, ordinal: int,
                             kind: str, locator: Mapping[str, Any]) -> str:
    """Return a content-free stable ID based on source identity and position."""
    source_checksum = _checksum(source_checksum, "source_checksum")
    page = _integer(page, "page", 1, 10_000)
    ordinal = _integer(ordinal, "ordinal", 0, _MAX_ELEMENTS - 1)
    kind = _text(kind, "kind", 40)
    if kind not in _ELEMENT_KINDS:
        raise ValueError("unsupported document element kind")
    if not isinstance(locator, Mapping) or len(locator) > 16:
        raise ValueError("locator must be a bounded object")
    encoded = _canonical({"source_checksum": source_checksum, "page": page,
                          "ordinal": ordinal, "kind": kind, "locator": locator})
    if len(encoded.encode()) > 4096:
        raise ValueError("locator exceeds 4096 bytes")
    return "docel_" + hashlib.sha256(encoded.encode()).hexdigest()


def document_parser_status() -> dict[str, Any]:
    return {
        "schema": "vera.document-parser-status/v1",
        "contract": "implemented",
        "provider_profiles": [{
            "id": "docling", "label": "Docling",
            "availability": "not_imported",
            "optional_dependency": "docling",
            "isolation": "required",
            "execution": "queued_live",
        }],
        "portable_media_types": sorted(_MEDIA_TYPES),
        "frozen_corpus_contract": "implemented",
        "deterministic_validation": "implemented",
        "stable_element_ids": "implemented",
        "citations": "source_checksum_page_locator",
        "render_policy": "separate_product_policy",
        "ocr_execution": "queued_live",
        "parser_execution": "queued_live",
        "optional_provider_imported": False,
        "network_io": False,
        "files_read": False,
        "executes": False,
        "queued_live_gates": [
            "docling_install_and_conversion", "text_table_layout_fidelity",
            "corrupt_and_encrypted_documents", "resource_ceiling_enforcement",
            "ocr_quality_and_declaration", "cancellation_propagation", "teardown",
        ],
    }


def compile_document_parse_plan(
        artifact: Mapping[str, Any], *, inspection: Mapping[str, Any] | None = None,
        provider_profile: str = "docling", ocr_policy: str = "disabled",
        resource_limits: Mapping[str, Any] | None = None,
        cancelled: bool = False) -> dict[str, Any]:
    """Compile an inert plan from supplied metadata; never opens the artifact."""
    source = _artifact(artifact, source=True)
    profile = _text(provider_profile or "docling", "provider_profile", 40)
    if profile != "docling":
        raise ValueError("unknown document parser profile")
    if ocr_policy not in _OCR_POLICIES:
        raise ValueError("ocr_policy must be disabled, auto, or force")
    inspection = inspection or {}
    if not isinstance(inspection, Mapping):
        raise TypeError("inspection must be an object")
    allowed_inspection = {"state", "page_count", "requires_ocr", "encrypted",
                          "corrupt", "inspection_ref"}
    unknown = sorted(set(inspection) - allowed_inspection)
    if unknown:
        raise ValueError("unknown inspection field: " + unknown[0])
    state = str(inspection.get("state") or "supplied_unverified")
    if state not in {"supplied_unverified", "inspected"}:
        raise ValueError("inspection state is invalid")
    limits = _limits(resource_limits)
    issues: list[dict[str, str]] = []
    if source["size_bytes"] > limits["max_bytes"]:
        issues.append({"code": "source_size_exceeds_limit", "path": "artifact.size_bytes"})
    page_count = inspection.get("page_count")
    if page_count is not None:
        page_count = _integer(page_count, "inspection.page_count", 1, 10_000)
        if page_count > limits["max_pages"]:
            issues.append({"code": "page_count_exceeds_limit", "path": "inspection.page_count"})
    if inspection.get("encrypted") is not None and not isinstance(inspection.get("encrypted"), bool):
        raise TypeError("inspection.encrypted must be boolean")
    if inspection.get("corrupt") is not None and not isinstance(inspection.get("corrupt"), bool):
        raise TypeError("inspection.corrupt must be boolean")
    if inspection.get("requires_ocr") is not None and not isinstance(
            inspection.get("requires_ocr"), bool):
        raise TypeError("inspection.requires_ocr must be boolean")
    inspection_ref = inspection.get("inspection_ref")
    if inspection_ref is not None:
        _text(inspection_ref, "inspection.inspection_ref", 512)
    if inspection.get("encrypted"):
        issues.append({"code": "encrypted_document_unsupported", "path": "inspection.encrypted"})
    if inspection.get("corrupt"):
        issues.append({"code": "corrupt_document", "path": "inspection.corrupt"})
    if inspection.get("requires_ocr") and ocr_policy == "disabled":
        issues.append({"code": "ocr_required_but_disabled", "path": "ocr_policy"})
    if cancelled:
        issues.append({"code": "cancelled_before_execution", "path": "cancelled"})
    identity_body = {
        "schema": DOCUMENT_PARSE_PLAN_SCHEMA, "source": source,
        "provider_profile": profile, "inspection": dict(inspection),
        "ocr_policy": ocr_policy, "resource_limits": limits,
        "issues": issues,
    }
    plan_id = "docplan_" + hashlib.sha256(_canonical(identity_body).encode()).hexdigest()
    return {
        **identity_body,
        "plan_id": plan_id,
        "accepted": not issues,
        "status": "cancelled" if cancelled else ("rejected" if issues else "planned"),
        "ready_for_execution": False,
        "inspection_trusted": state == "inspected",
        "original_preserved": True,
        "output_contract": DOCUMENT_PARSE_RESULT_SCHEMA,
        "record_authority": "proposal_until_verified_and_stored",
        "artifact_authority": "reference_until_checksum_verified_and_stored",
        "parser_imported": False,
        "files_read": False,
        "network_io": False,
        "executes": False,
    }


def _citation(raw: Mapping[str, Any], *, source: Mapping[str, Any], page: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("citation must be an object")
    allowed = {"source_artifact_id", "source_checksum", "page", "locator"}
    if set(raw) - allowed:
        raise ValueError("citation contains unsupported fields")
    locator = raw.get("locator") or {}
    if not isinstance(locator, Mapping) or len(locator) > 16:
        raise ValueError("citation locator must be a bounded object")
    if len(_canonical(locator).encode()) > 4096:
        raise ValueError("citation locator exceeds 4096 bytes")
    value = {
        "source_artifact_id": _text(raw.get("source_artifact_id"),
                                    "citation.source_artifact_id", 160),
        "source_checksum": _checksum(raw.get("source_checksum"),
                                     "citation.source_checksum"),
        "page": _integer(raw.get("page"), "citation.page", 1, 10_000),
        "locator": dict(locator),
    }
    if (value["source_artifact_id"] != source["id"]
            or value["source_checksum"] != source["checksum"]
            or value["page"] != page):
        raise ValueError("citation does not bind the source artifact and page")
    return value


def validate_document_parse_result(plan: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Validate supplied adapter output without persisting or returning its content."""
    if not isinstance(plan, Mapping) or plan.get("schema") != DOCUMENT_PARSE_PLAN_SCHEMA:
        raise ValueError("invalid document parse plan")
    expected = compile_document_parse_plan(
        plan.get("source") or {}, inspection=plan.get("inspection") or {},
        provider_profile=str(plan.get("provider_profile") or ""),
        ocr_policy=str(plan.get("ocr_policy") or ""),
        resource_limits=plan.get("resource_limits") or {},
        cancelled=plan.get("status") == "cancelled")
    if expected["plan_id"] != plan.get("plan_id"):
        raise ValueError("document parse plan identity mismatch")
    if not expected["accepted"]:
        return {"schema": "vera.document-parse-validation/v1", "valid": False,
                "issues": [{"code": "plan_not_accepted", "path": "plan"}],
                "content_returned": False, "executes": False}
    if not isinstance(result, Mapping):
        raise TypeError("result must be an object")
    allowed = {"schema", "plan_id", "provider", "provider_version", "config_hash",
               "source", "elements", "derived_artifacts", "ocr", "cancelled"}
    issues: list[dict[str, str]] = []
    if set(result) - allowed:
        issues.append({"code": "unknown_result_field", "path": "result"})
    if result.get("schema") != DOCUMENT_PARSE_RESULT_SCHEMA:
        issues.append({"code": "result_schema_mismatch", "path": "schema"})
    if result.get("plan_id") != expected["plan_id"]:
        issues.append({"code": "plan_id_mismatch", "path": "plan_id"})
    if result.get("provider") != "docling":
        issues.append({"code": "provider_mismatch", "path": "provider"})
    try:
        _text(result.get("provider_version"), "provider_version", 80)
        _checksum(result.get("config_hash"), "config_hash")
        if _artifact(result.get("source") or {}, source=True) != expected["source"]:
            issues.append({"code": "source_identity_mismatch", "path": "source"})
    except (TypeError, ValueError):
        issues.append({"code": "provenance_invalid", "path": "result"})
    if result.get("cancelled") not in {False, None}:
        issues.append({"code": "cancelled_result_must_not_publish", "path": "cancelled"})
    elements = result.get("elements") or []
    if (not isinstance(elements, list) or len(elements) > expected["resource_limits"]["max_elements"]):
        issues.append({"code": "elements_invalid_or_over_limit", "path": "elements"})
        elements = []
    seen: set[str] = set()
    seen_positions: set[tuple[int, int]] = set()
    text_hashes: list[str] = []
    table_hashes: list[str] = []
    layout_hashes: list[str] = []
    for index, raw in enumerate(elements):
        path = f"elements[{index}]"
        try:
            if not isinstance(raw, Mapping):
                raise TypeError("element must be object")
            if set(raw) - {"id", "kind", "page", "ordinal", "locator", "text_hash",
                           "structure_hash", "citation"}:
                raise ValueError("element contains unsupported fields")
            kind = _text(raw.get("kind"), "element.kind", 40)
            page = _integer(raw.get("page"), "element.page", 1, 10_000)
            ordinal = _integer(raw.get("ordinal"), "element.ordinal", 0, _MAX_ELEMENTS - 1)
            locator = raw.get("locator") or {}
            expected_id = deterministic_element_id(expected["source"]["checksum"],
                                                   page=page, ordinal=ordinal,
                                                   kind=kind, locator=locator)
            if raw.get("id") != expected_id or expected_id in seen:
                raise ValueError("element ID is unstable or duplicated")
            if (page, ordinal) in seen_positions:
                raise ValueError("element page and ordinal are duplicated")
            seen.add(expected_id)
            seen_positions.add((page, ordinal))
            text_hash = _checksum(raw.get("text_hash"), "element.text_hash")
            structure_hash = _checksum(raw.get("structure_hash"), "element.structure_hash")
            _citation(raw.get("citation") or {}, source=expected["source"], page=page)
            text_hashes.append(text_hash)
            layout_hashes.append(structure_hash)
            if kind == "table":
                table_hashes.append(structure_hash)
        except (TypeError, ValueError) as exc:
            issues.append({"code": "element_invalid", "path": path,
                           "detail": str(exc)[:160]})
    derived = result.get("derived_artifacts") or []
    total_derived = 0
    derived_ids: set[str] = set()
    if not isinstance(derived, list) or len(derived) > 256:
        issues.append({"code": "derived_artifacts_invalid", "path": "derived_artifacts"})
        derived = []
    for index, raw in enumerate(derived):
        try:
            artifact = _artifact(raw, source=False)
            if artifact["id"] in derived_ids:
                raise ValueError("derived artifact ID is duplicated")
            derived_ids.add(artifact["id"])
            total_derived += artifact["size_bytes"]
        except (TypeError, ValueError) as exc:
            issues.append({"code": "derived_artifact_invalid",
                           "path": f"derived_artifacts[{index}]", "detail": str(exc)[:160]})
    if total_derived > expected["resource_limits"]["derived_artifact_bytes"]:
        issues.append({"code": "derived_artifact_budget_exceeded", "path": "derived_artifacts"})
    ocr = result.get("ocr") or {}
    if not isinstance(ocr, Mapping) or set(ocr) - {"requested", "used", "engine", "languages"}:
        issues.append({"code": "ocr_declaration_invalid", "path": "ocr"})
    else:
        if not isinstance(ocr.get("requested", False), bool) or not isinstance(ocr.get("used", False), bool):
            issues.append({"code": "ocr_declaration_invalid", "path": "ocr"})
        if ocr.get("used") and (not ocr.get("engine") or not ocr.get("languages")):
            issues.append({"code": "ocr_provenance_missing", "path": "ocr"})
        try:
            if ocr.get("engine"):
                _text(ocr.get("engine"), "ocr.engine", 120)
            languages = ocr.get("languages") or []
            if (not isinstance(languages, list) or len(languages) > 32
                    or any(not isinstance(item, str) or not item.strip()
                           or len(item) > 32 for item in languages)):
                raise ValueError("OCR languages must be a bounded string list")
        except (TypeError, ValueError):
            issues.append({"code": "ocr_provenance_invalid", "path": "ocr"})
        if expected["ocr_policy"] == "disabled" and ocr.get("used"):
            issues.append({"code": "ocr_used_against_policy", "path": "ocr.used"})
    digest = lambda values: "sha256:" + hashlib.sha256(_canonical(values).encode()).hexdigest()
    return {
        "schema": "vera.document-parse-validation/v1",
        "valid": not issues,
        "issues": issues[:100],
        "element_count": len(elements),
        "derived_artifact_count": len(derived),
        "fidelity": {"text_hash": digest(text_hashes),
                     "table_hash": digest(table_hashes),
                     "layout_hash": digest(layout_hashes)},
        "content_returned": False,
        "records_written": 0,
        "artifacts_written": 0,
        "provider_imported": False,
        "executes": False,
    }


def evaluate_frozen_document_corpus(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare supplied, already-produced evidence with a bounded frozen manifest."""
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise TypeError("cases must be a sequence")
    if not cases or len(cases) > _MAX_CASES:
        raise ValueError(f"corpus must contain 1 to {_MAX_CASES} cases")
    outcomes = []
    seen: set[str] = set()
    for index, raw in enumerate(cases):
        if not isinstance(raw, Mapping):
            raise TypeError("corpus cases must be objects")
        case_id = _text(raw.get("case_id"), "case_id", 120)
        if case_id in seen:
            raise ValueError("corpus case IDs must be unique")
        seen.add(case_id)
        expected = raw.get("expected") or {}
        if not isinstance(expected, Mapping) or set(expected) != {
                "accepted", "text_hash", "table_hash", "layout_hash"}:
            raise ValueError("case expected evidence is incomplete")
        if not isinstance(expected["accepted"], bool):
            raise TypeError("expected.accepted must be boolean")
        for key in ("text_hash", "table_hash", "layout_hash"):
            _checksum(expected[key], f"expected.{key}")
        validation = validate_document_parse_result(raw.get("plan") or {},
                                                    raw.get("result") or {})
        observed = validation.get("fidelity") or {}
        matched = (validation["valid"] == bool(expected["accepted"])
                   and all(observed.get(key) == expected[key]
                           for key in ("text_hash", "table_hash", "layout_hash")))
        outcomes.append({"case_id": case_id, "matched": matched,
                         "valid": validation["valid"],
                         "issue_codes": sorted({item["code"] for item in validation["issues"]}),
                         "content_returned": False})
    manifest_body = [{"case_id": item["case_id"], "matched": item["matched"],
                      "valid": item["valid"], "issue_codes": item["issue_codes"]}
                     for item in outcomes]
    return {
        "schema": DOCUMENT_CORPUS_REPORT_SCHEMA,
        "corpus_id": "doccorpus_" + hashlib.sha256(
            _canonical(manifest_body).encode()).hexdigest(),
        "case_count": len(outcomes),
        "matched": sum(1 for item in outcomes if item["matched"]),
        "passed": all(item["matched"] for item in outcomes),
        "outcomes": outcomes,
        "content_returned": False,
        "provider_imported": False,
        "executes": False,
    }


def document_parser_teardown_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("schema") != DOCUMENT_PARSE_PLAN_SCHEMA:
        raise ValueError("invalid document parse plan")
    expected = compile_document_parse_plan(
        plan.get("source") or {}, inspection=plan.get("inspection") or {},
        provider_profile=str(plan.get("provider_profile") or ""),
        ocr_policy=str(plan.get("ocr_policy") or ""),
        resource_limits=plan.get("resource_limits") or {},
        cancelled=plan.get("status") == "cancelled")
    if expected["plan_id"] != plan.get("plan_id"):
        raise ValueError("document parse plan identity mismatch")
    return {
        "schema": "vera.document-parser-teardown-plan/v1",
        "plan_id": plan["plan_id"],
        "actions": [
            "stop_isolated_parser", "remove_ephemeral_workspace",
            "revoke_temporary_artifact_access", "retain_verified_receipts_only",
        ],
        "deletes_source_artifact": False,
        "deletes_verified_records": False,
        "requires_receipt_verification": True,
        "action_started": False,
        "executes": False,
    }
