import asyncio
import copy

import pytest

from vera.capability_contract_core import (
    CONTRACT_SCHEMA,
    contract_coverage,
    gate_contracts,
    lint_contracts,
    manifest_fingerprint,
    project_contract,
    project_registry,
)
from vera import capability_orchestration as orchestration
from Vera.vera import capability_orchestration as runtime_orchestration
from Vera.vera.capabilities import capabilities as _runtime_capabilities  # noqa: F401
from Vera.vera.dag import dag_workshop_capabilities as _runtime_workshop  # noqa: F401


pytestmark = pytest.mark.critical


def _entry(**overrides):
    value = {
        "schema": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"]},
        "description": "Search records.",
        "streams": ["results"],
        "mode": "local",
        "source": "local",
        "retries": 1,
        "tags": ["search"],
        "mcp_expose": True,
        "http_method": "GET",
        "http_path": "/search",
    }
    value.update(overrides)
    return value


def test_projection_is_explicit_without_inventing_missing_contract_facts():
    manifest = project_contract("records.search", _entry())
    assert manifest["schema"] == CONTRACT_SCHEMA
    assert manifest["canonical_task"] == "records.search"
    assert manifest["schemas"]["input"]["required"] == ["query"]
    assert manifest["schemas"]["output_status"] == "unknown"
    assert manifest["effects"] == {"status": "unknown", "declared": []}
    assert manifest["policy"]["approval"] == {"status": "unknown"}
    assert manifest["execution"]["retries"] == 1
    assert manifest["declaration"] == {"status": "legacy_projected", "fields": []}


def test_declared_metadata_projects_without_affecting_registry_entry():
    entry = _entry(contract={
        "canonical_task": "records.query",
        "aliases": ["search.records"],
        "effects": ["read"],
        "lifecycle": "active",
        "output_schema": {"type": "object"},
        "secrets": {"status": "not_required"},
    })
    before = copy.deepcopy(entry)
    manifest = project_contract("provider.records.search", entry)
    assert entry == before
    assert manifest["canonical_task"] == "records.query"
    assert manifest["aliases"] == ["search.records"]
    assert manifest["effects"] == {"status": "declared", "declared": ["read"]}
    assert manifest["schemas"]["output_status"] == "declared"


def test_registry_projection_and_fingerprint_are_load_order_stable():
    left = {"z.cap": _entry(), "a.cap": _entry()}
    right = {"a.cap": _entry(), "z.cap": _entry()}
    left_manifests = project_registry(left)
    right_manifests = project_registry(right)
    assert [item["name"] for item in left_manifests] == ["a.cap", "z.cap"]
    assert left_manifests == right_manifests
    assert manifest_fingerprint(left_manifests) == manifest_fingerprint(right_manifests)


def test_projection_excludes_internal_caps_unless_requested():
    registry = {"public.cap": _entry(), "internal.cap": _entry(mcp_expose=False)}
    assert [item["name"] for item in project_registry(registry)] == ["public.cap"]
    assert [item["name"] for item in project_registry(
        registry, include_internal=True)] == ["internal.cap", "public.cap"]


def test_linter_rejects_known_bad_contract_fixtures():
    bad_schema = _entry(
        schema={"type": "object", "properties": {
            "api_token": {"type": "string"}}, "required": ["missing"]},
        http_method="POST",
        source="mcp_proxy",
        contract={"aliases": ["shared.alias"], "lifecycle": "mystery",
                  "effects": ["telepathy"]},
    )
    other = _entry(contract={"aliases": ["shared.alias"], "effects": ["read"]})
    issues = lint_contracts(project_registry({"provider.bad": bad_schema, "other": other}))
    codes = {issue["code"] for issue in issues}
    assert {"schema.required_unknown", "schema.secret_plaintext", "effects.invalid",
            "lifecycle.invalid", "provider.unmapped_task", "alias.ambiguous"} <= codes


def test_alias_cannot_shadow_an_existing_canonical_capability_name():
    owner = _entry(contract={"aliases": ["records.read"], "effects": ["read"]})
    target = _entry(contract={"effects": ["read"]})
    issues = lint_contracts(project_registry({"records.impl": owner, "records.read": target}))
    assert any(issue["code"] == "alias.ambiguous" and issue["name"] == "records.impl"
               for issue in issues)


def test_opaque_secret_reference_and_declared_write_effect_are_clean():
    entry = _entry(
        schema={"type": "object", "properties": {
            "api_token": {"type": "string", "format": "secret-ref"}}},
        http_method="POST",
        contract={"effects": ["write"], "canonical_task": "records.update"},
    )
    issues = lint_contracts(project_registry({"records.update.impl": entry}))
    assert not [issue for issue in issues if issue["severity"] == "error"]


def test_manifest_capability_is_bounded_and_exposes_declared_self_contract():
    result = asyncio.run(orchestration.cap_contract_manifest.__wrapped__(
        name="cap.contract.manifest", limit=500))
    assert result["count"] == result["returned"] == 1
    assert result["truncated"] is False
    manifest = result["manifests"][0]
    assert manifest["canonical_task"] == "capability.contract.inspect"
    assert manifest["effects"] == {"status": "declared", "declared": ["read"]}
    assert len(result["fingerprint"]) == 64


def test_lint_capability_bounds_returned_issues_but_keeps_total_counts(monkeypatch):
    bad = _entry(http_method="POST")
    monkeypatch.setattr(orchestration, "CAPABILITY_REGISTRY", {
        "bad.one": bad, "bad.two": copy.deepcopy(bad)})
    result = asyncio.run(orchestration.cap_contract_lint.__wrapped__(limit=1))
    assert result["issue_count"] == 2
    assert result["returned"] == 1
    assert result["truncated"] is True
    assert result["counts"] == {"error": 0, "warning": 2}


def test_coverage_counts_only_explicit_declarations_and_orders_hotspots():
    legacy = project_contract("z.legacy", _entry())
    partial = project_contract("a.partial", _entry(contract={
        "effects": ["read"], "owner": "team-a"}))
    covered = project_contract("a.covered", _entry(contract={
        "effects": ["read"], "owner": "team-a", "output_schema": {"type": "object"},
        "approval": {"status": "not_required"}, "trust": {"status": "internal"},
        "secrets": {"status": "not_required"}, "filesystem": {"status": "not_required"},
        "network": {"status": "not_required"}, "tenant": {"status": "request_scoped"},
        "idempotency": {"status": "idempotent"}, "cancellation": {"status": "not_required"},
        "pagination": {"status": "not_applicable"}, "health": {"status": "observed"},
        "cost": {"status": "observed"}, "latency": {"status": "observed"},
        "quality": {"status": "observed"}, "resources": {"status": "declared"},
    }))
    result = contract_coverage([covered, legacy, partial])
    assert result["manifests"] == 3
    assert result["coverage"]["contract"] == {"declared": 2, "total": 3, "rate": 0.6667}
    assert result["coverage"]["effects"]["declared"] == 2
    assert result["hotspots"][0]["name"] == "z.legacy"
    assert result["hotspots"][-1]["name"] == "a.covered"


def test_generation_and_authoring_family_has_explicit_canonical_tasks():
    expected = {
        "llm.generate": "text.generate",
        "ollama.generate_raw": "text.generate",
        "code.author": "source_file.author",
        "prose.author": "document.author",
    }
    for name, canonical_task in expected.items():
        manifest = project_contract(name, runtime_orchestration.CAPABILITY_REGISTRY[name])
        assert manifest["canonical_task"] == canonical_task
        assert manifest["declaration"]["status"] == "declared"
        assert manifest["effects"]["status"] == "declared"


def test_deprecated_contract_requires_replacement_sunset_and_reason():
    bad = project_contract("old.cap", _entry(contract={
        "lifecycle": "deprecated", "effects": ["read"]}))
    codes = {issue["code"] for issue in lint_contracts([bad])}
    assert {"lifecycle.replacement_missing", "lifecycle.sunset_invalid",
            "lifecycle.reason_missing"} <= codes

    good = project_contract("old.cap", _entry(contract={
        "lifecycle": "deprecated", "replacement": "new.cap", "sunset": "2027-01-31",
        "deprecation_reason": "superseded", "effects": ["read"]}))
    assert not [issue for issue in lint_contracts([good])
                if issue["code"].startswith("lifecycle.")]


def test_removed_contract_cannot_remain_mcp_exposed():
    removed = project_contract("gone.cap", _entry(contract={
        "lifecycle": "removed", "effects": ["none"]}))
    assert any(issue["code"] == "lifecycle.removed_exposed"
               for issue in lint_contracts([removed]))


def test_incremental_gate_passes_migrated_family_and_fails_legacy_projection():
    migrated = [project_contract(name, runtime_orchestration.CAPABILITY_REGISTRY[name])
                for name in ("llm.generate", "code.author", "prose.author")]
    assert gate_contracts(migrated)["ok"] is True

    legacy = project_contract("legacy.cap", _entry())
    result = gate_contracts([legacy])
    assert result["ok"] is False
    assert any(issue["code"] == "gate.declaration_missing" for issue in result["issues"])


def test_gate_capability_rejects_unknown_names_without_invocation():
    result = asyncio.run(orchestration.cap_contract_gate.__wrapped__(
        names=["cap.contract.manifest", "does.not.exist"]))
    assert result["ok"] is False
    assert result["selected"] == ["cap.contract.manifest", "does.not.exist"]
    assert any(issue["code"] == "gate.capability_unknown" for issue in result["issues"])


def test_gate_capability_accepts_mcp_coerced_python_list_string():
    result = asyncio.run(orchestration.cap_contract_gate.__wrapped__(
        names="['cap.contract.coverage', 'cap.contract.gate']"))
    assert result["selected"] == ["cap.contract.coverage", "cap.contract.gate"]
    assert not [issue for issue in result["issues"]
                if issue["code"] == "gate.capability_unknown"]
