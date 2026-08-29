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
    summarize_contract_observations,
)
from vera.capability_resolver_core import resolve_shadow
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


def test_run_and_workflow_inspection_families_have_gated_contracts():
    expected_tasks = {
        "run.shadow.list": "run.observe.list",
        "run.shadow.graph": "run.observe.graph",
        "run.shadow.get": "run.observe.get",
        "run.shadow.export": "run.observe.export",
        "workflow.ir.import_dag": "workflow.translate.import_dag",
        "workflow.ir.export_dag": "workflow.translate.export_dag",
        "workflow.ir.validate": "workflow.validate",
        "workflow.ir.migrate": "workflow.migrate",
        "workflow.ir.adapters": "workflow.adapters.list",
        "workflow.ir.gaps": "workflow.compatibility.analyze",
        "workflow.durability.fixture": "workflow.durability.fixture",
        "workflow.durability.gaps": "workflow.durability.analyze",
    }
    manifests = [
        project_contract(name, runtime_orchestration.CAPABILITY_REGISTRY[name])
        for name in reversed(tuple(expected_tasks))
    ]

    assert gate_contracts(manifests)["ok"] is True
    assert manifest_fingerprint(manifests) == manifest_fingerprint(reversed(manifests))
    by_name = {manifest["name"]: manifest for manifest in manifests}
    for name, canonical_task in expected_tasks.items():
        assert by_name[name]["canonical_task"] == canonical_task
        assert by_name[name]["declaration"]["status"] == "declared"
        assert "model" not in by_name[name]["effects"]["declared"]

    assert by_name["workflow.ir.validate"]["effects"] == {
        "status": "declared", "declared": ["none"]}
    assert by_name["workflow.durability.gaps"]["effects"] == {
        "status": "declared", "declared": ["none"]}
    assert by_name["run.shadow.list"]["effects"] == {
        "status": "declared", "declared": ["filesystem", "read"]}
    assert by_name["run.shadow.list"]["policy"]["filesystem"] == {
        "status": "conditional_read_only"}


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


def test_observations_aggregate_outcomes_and_latency_without_payloads():
    events = [
        {"type": "cap.ok", "name": "a.cap", "elapsed_ms": 100,
         "ts": "2026-08-24T10:00:00Z", "preview": "must not leak"},
        {"type": "cap.error", "name": "a.cap", "elapsed_ms": 300,
         "ts": "2026-08-24T10:01:00Z", "error": "must not leak"},
        {"type": "cap.ok", "name": "b.cap", "elapsed_ms": 50,
         "ts": "2026-08-24T10:02:00Z", "args_preview": "must not leak"},
        {"type": "other", "name": "a.cap"},
    ]
    result = summarize_contract_observations(events)
    assert result["events"] == {"supplied": 4, "accepted": 3, "ignored": 1}
    assert result["observations"][0] == {
        "name": "a.cap", "calls": 2, "ok": 1, "errors": 1,
        "last_seen": "2026-08-24T10:01:00Z", "success_rate": 0.5,
        "health": {"status": "observed", "samples": 2, "healthy": False},
        "latency_ms": {"status": "observed", "samples": 2,
                       "p50": 200, "p95": 290, "max": 300},
    }
    rendered = repr(result)
    assert "must not leak" not in rendered
    assert "preview" not in rendered


def test_observations_filter_names_and_ignore_invalid_latency():
    result = summarize_contract_observations([
        {"type": "cap.ok", "name": "keep", "elapsed_ms": float("nan")},
        {"type": "cap.ok", "name": "drop", "elapsed_ms": 10},
    ], allowed_names={"keep"})
    assert result["observed_capabilities"] == 1
    assert result["observations"][0]["latency_ms"] == {
        "status": "unknown", "samples": 0, "p50": None, "p95": None, "max": None}


def test_observation_capability_bounds_windows_and_registered_caps(monkeypatch):
    calls = []

    async def observe(limit, trace_id=None):
        calls.append((limit, trace_id))
        return [{"type": "cap.ok", "name": "cap.contract.gate", "elapsed_ms": 7},
                {"type": "cap.ok", "name": "not.registered", "elapsed_ms": 1}]

    monkeypatch.setitem(orchestration.CAPABILITY_REGISTRY, "obs.events", {"raw": observe})
    result = asyncio.run(orchestration.cap_contract_observations.__wrapped__(
        prefix="cap.contract.", event_limit=9999, cap_limit=1, trace_id="obs-test"))
    assert calls == [(500, "obs-test")]
    assert result["registered"] >= 1
    assert result["observation_count"] == 1
    assert result["observations"][0]["name"] == "cap.contract.gate"


def test_shadow_resolver_explains_exclusions_and_never_executes():
    manifests = [project_contract(name, runtime_orchestration.CAPABILITY_REGISTRY[name])
                 for name in ("llm.generate", "ollama.generate_raw")]
    manifests.append(project_contract("unsafe.generate", _entry(contract={
        "canonical_task": "text.generate", "effects": ["delete"],
        "resources": {"status": "declared", "classes": ["cpu"]}})))
    result = resolve_shadow(manifests, {
        "canonical_task": "text.generate",
        "allowed_effects": ["filesystem", "model", "network"],
        "preferred": ["ollama.generate_raw"],
    })
    assert result["selected"] == "ollama.generate_raw"
    assert result["authorized"] is False
    assert result["executed"] is False
    assert [row["name"] for row in result["eligible"]] == [
        "ollama.generate_raw", "llm.generate"]
    assert result["excluded"][0]["name"] == "unsafe.generate"
    assert result["excluded"][0]["exclusions"] == [
        {"code": "effects_not_allowed", "actual": ["delete"]}]
    assert result["counts"]["matched"] == 3


def test_shadow_resolver_bounds_only_the_requested_task_family():
    manifests = [project_contract(f"impl.{index}", _entry(contract={
        "canonical_task": "same.task", "effects": ["read"]})) for index in range(4)]
    manifests.append(project_contract("unrelated", _entry(contract={
        "canonical_task": "other.task", "effects": ["read"]})))
    result = resolve_shadow(manifests, {"canonical_task": "same.task",
                                       "allowed_effects": ["read"],
                                       "candidate_limit": 2})
    assert result["counts"] == {"matched": 4, "considered": 2,
                                "eligible": 2, "excluded": 0}
    assert result["truncated"] is True


def test_shadow_resolver_excludes_unhealthy_and_ranks_observed_evidence():
    manifests = [project_contract(name, runtime_orchestration.CAPABILITY_REGISTRY[name])
                 for name in ("llm.generate", "ollama.generate_raw")]
    observations = [
        {"name": "llm.generate", "success_rate": 1.0,
         "health": {"status": "observed", "healthy": True},
         "latency_ms": {"status": "observed", "p95": 50}},
        {"name": "ollama.generate_raw", "success_rate": 0.5,
         "health": {"status": "observed", "healthy": False},
         "latency_ms": {"status": "observed", "p95": 10}},
    ]
    result = resolve_shadow(manifests, {
        "canonical_task": "text.generate",
        "allowed_effects": ["filesystem", "model", "network"],
    }, observations=observations)
    assert result["selected"] == "llm.generate"
    assert result["excluded"][0]["exclusions"] == [
        {"code": "observed_unhealthy"}]


@pytest.mark.parametrize("dimension", [
    "reliability", "quality", "latency", "cost", "load",
])
def test_shadow_resolver_ranks_each_validated_observed_metric(dimension):
    manifests = [project_contract(name, _entry(contract={
        "canonical_task": "rank.task", "effects": ["none"],
        "output_schema": {"type": "object"},
    })) for name in ("rank.a", "rank.b")]

    def evidence(name, better=False):
        values = {
            "name": name,
            "success_rate": 0.8,
            "quality": {"status": "observed", "score": 0.5},
            "latency_ms": {"status": "observed", "p95": 100},
            "cost": {"status": "observed", "normalized_per_call": 1},
            "load": {"status": "observed", "utilization": 0.5},
        }
        if better:
            if dimension == "reliability":
                values["success_rate"] = 0.9
            elif dimension == "quality":
                values["quality"]["score"] = 0.6
            elif dimension == "latency":
                values["latency_ms"]["p95"] = 90
            elif dimension == "cost":
                values["cost"]["normalized_per_call"] = 0.9
            else:
                values["load"]["utilization"] = 0.4
        return values

    # Tie all dimensions that precede the one under test so each metric's
    # ordering contribution is exercised independently.
    left = evidence("rank.a")
    right = evidence("rank.b", better=True)
    if dimension != "reliability":
        right["success_rate"] = left["success_rate"]
    if dimension not in {"reliability", "quality"}:
        right["quality"] = dict(left["quality"])
    if dimension in {"cost", "load"}:
        right["latency_ms"] = dict(left["latency_ms"])
    if dimension == "load":
        right["cost"] = dict(left["cost"])

    result = resolve_shadow(manifests, {"canonical_task": "rank.task"},
                            observations=[left, right])
    assert result["selected"] == "rank.b"


def test_shadow_resolver_uses_declared_metrics_only_as_observation_fallback():
    manifests = [
        project_contract("rank.a", _entry(contract={
            "canonical_task": "rank.task", "effects": ["none"],
            "quality": {"status": "declared", "score": 0.2}})),
        project_contract("rank.b", _entry(contract={
            "canonical_task": "rank.task", "effects": ["none"],
            "quality": {"status": "declared", "score": 0.9}})),
    ]
    result = resolve_shadow(manifests, {"canonical_task": "rank.task"})
    assert result["selected"] == "rank.b"
    assert result["eligible"][0]["rank"]["quality"] == 0.9
    assert result["eligible"][0]["rank"]["evidence_sources"]["quality"] == "contract"


def test_shadow_resolver_prefers_locality_after_metric_ties():
    contract = {"canonical_task": "rank.task", "effects": ["none"]}
    manifests = [
        project_contract("rank.remote", _entry(mode="remote", contract=contract)),
        project_contract("rank.local", _entry(mode="local", contract=contract)),
    ]
    result = resolve_shadow(manifests, {"canonical_task": "rank.task"})
    assert result["selected"] == "rank.local"


def test_shadow_resolver_ignores_nonfinite_or_out_of_range_rank_evidence():
    manifests = [project_contract(name, _entry(contract={
        "canonical_task": "rank.task", "effects": ["none"],
    })) for name in ("rank.a", "rank.b")]
    result = resolve_shadow(manifests, {"canonical_task": "rank.task"}, observations=[{
        "name": "rank.b", "success_rate": float("nan"),
        "quality": {"status": "observed", "score": float("inf")},
        "latency_ms": {"status": "observed", "p95": -1},
        "cost": {"status": "observed", "normalized_per_call": float("nan")},
        "load": {"status": "observed", "utilization": 2},
    }])
    assert result["selected"] == "rank.a"
    rank = next(row["rank"] for row in result["eligible"] if row["name"] == "rank.b")
    assert {key: rank[key] for key in (
        "reliability", "quality", "latency_p95_ms",
        "cost_normalized_per_call", "load_utilization",
    )} == {
        "reliability": None, "quality": None, "latency_p95_ms": None,
        "cost_normalized_per_call": None, "load_utilization": None,
    }


def test_shadow_resolver_excludes_output_contract_mismatches():
    base_contract = {"canonical_task": "answer.generate", "effects": ["none"]}
    manifests = [
        project_contract("answer.good", _entry(contract={**base_contract,
            "output_schema": {"type": "object", "required": ["answer"],
                              "properties": {"answer": {"type": "string"}}}})),
        project_contract("answer.bad", _entry(contract={**base_contract,
            "output_schema": {"type": "object", "required": ["answer"],
                              "properties": {"answer": {"type": "number"}}}})),
        project_contract("answer.unknown", _entry(contract=base_contract)),
    ]
    result = resolve_shadow(manifests, {
        "canonical_task": "answer.generate",
        "output_schema": {"type": "object", "required": ["answer"],
                          "properties": {"answer": {"type": "string"}}},
    })
    assert result["selected"] == "answer.good"
    exclusions = {row["name"]: row["exclusions"] for row in result["excluded"]}
    assert exclusions["answer.bad"] == [{
        "code": "output_property_type_mismatch", "property": "answer",
        "expected": ["string"], "actual": ["number"]}]
    assert exclusions["answer.unknown"] == [{"code": "output_schema_unknown"}]


def test_shadow_resolver_excludes_policy_mismatch_and_unknown():
    common = {"canonical_task": "records.read", "effects": ["read"],
              "output_schema": {"type": "object"}}
    manifests = [
        project_contract("records.safe", _entry(contract={**common,
            "network": {"status": "not_required"}})),
        project_contract("records.remote", _entry(contract={**common,
            "network": {"status": "internal_model_cluster"}})),
        project_contract("records.unknown", _entry(contract=common)),
    ]
    result = resolve_shadow(manifests, {
        "canonical_task": "records.read", "allowed_effects": ["read"],
        "policy_requirements": {"network": "not_required"},
    })
    assert result["selected"] == "records.safe"
    exclusions = {row["name"]: row["exclusions"] for row in result["excluded"]}
    assert exclusions["records.remote"] == [{
        "code": "policy_mismatch", "dimension": "network",
        "expected": ["not_required"], "actual": "internal_model_cluster"}]
    assert exclusions["records.unknown"] == [{
        "code": "policy_unknown", "dimension": "network"}]


def test_shadow_resolver_fails_closed_on_invalid_constraint_request():
    manifest = project_contract("records.read", _entry(contract={
        "canonical_task": "records.read", "effects": ["read"]}))
    result = resolve_shadow([manifest], {
        "canonical_task": "records.read",
        "output_schema": "object",
        "policy_requirements": {"telepathy": ["allowed"]},
    })
    assert result["selected"] is None
    assert result["counts"] == {"matched": 1, "considered": 0,
                                "eligible": 0, "excluded": 0}
    assert result["request_issues"] == [
        {"code": "request.policy_dimension_unknown", "dimension": "telepathy"},
        {"code": "request.output_schema_invalid"},
    ]


def test_shadow_resolver_bounds_requested_output_properties():
    result = resolve_shadow([], {
        "canonical_task": "answer.generate",
        "output_schema": {"type": "object", "properties": {
            f"field_{index}": {"type": "string"} for index in range(101)}},
    })
    assert result["selected"] is None
    assert result["request_issues"] == [{
        "code": "request.output_schema_too_large", "max_properties": 100}]


def test_shadow_capability_does_not_call_candidates(monkeypatch):
    calls = []
    async def observe(limit, trace_id=None):
        calls.append((limit, trace_id))
        return []
    monkeypatch.setitem(runtime_orchestration.CAPABILITY_REGISTRY,
                        "obs.events", {"raw": observe})
    result = asyncio.run(runtime_orchestration.cap_resolve_shadow.__wrapped__(
        canonical_task="text.generate",
        allowed_effects=["filesystem", "model", "network"],
        output_schema={"type": "object"},
        policy_requirements={"network": ["internal_model_cluster"]},
        trace_id="shadow-test"))
    assert calls == [(200, "shadow-test")]
    assert result["executed"] is False and result["authorized"] is False
    assert result["selected"] in {"llm.generate", "ollama.generate_raw"}
    assert result["request"]["output_schema"] == {"type": "object"}
    assert result["request"]["policy_requirements"] == {
        "network": ["internal_model_cluster"]}
