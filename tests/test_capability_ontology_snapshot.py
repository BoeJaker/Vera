import asyncio
import copy
import os

import pytest

from vera.ontologies.capability_ontology_snapshot import (
    AUTO_GENERATION_ENV,
    GENERATED_RELATIONS_ENV,
    auto_generation_status,
    build_capability_ontology_snapshot,
    generated_relation_consumption_status,
)


pytestmark = pytest.mark.critical


def relation(source="code.author", target="llm.generate", *, auto=False):
    return {
        "from": source, "to": target, "relation": "preferred_over",
        "description": "Use the specialist for code generation.",
        "direction": "forward", "strength": .9, "confidence": .95,
        "wire": "", "auto": auto,
        "tags": ["reviewed"] if not auto else ["llm-generated"],
        "updated_at": "2026-09-11T12:00:00Z",
    }


def test_snapshot_is_order_stable_content_addressed_and_preserves_provenance():
    manual = relation()
    generated = relation("prose.author", "llm.generate", auto=True)
    first = build_capability_ontology_snapshot((manual, generated))
    second = build_capability_ontology_snapshot((generated, manual))
    assert first.snapshot_id == second.snapshot_id
    assert first.content_sha256 == first.snapshot_id.removeprefix("capsont_")
    assert (first.relation_count, first.manual_count, first.generated_count) == (2, 1, 1)
    result = first.to_dict()
    assert result["restorable"] is True
    assert result["executes"] is False
    assert result["relations"][0]["auto"] is False


def test_snapshot_identity_changes_with_relation_content_or_revision():
    first = relation()
    changed = copy.deepcopy(first)
    changed["confidence"] = .8
    revised = copy.deepcopy(first)
    revised["updated_at"] = "2026-09-11T12:01:00Z"
    assert build_capability_ontology_snapshot((first,)).snapshot_id != \
        build_capability_ontology_snapshot((changed,)).snapshot_id
    assert build_capability_ontology_snapshot((first,)).snapshot_id != \
        build_capability_ontology_snapshot((revised,)).snapshot_id


@pytest.mark.parametrize("bad,match", [
    ([relation(), relation()], "duplicate"),
    ([{**relation(), "confidence": float("nan")}], "confidence"),
    ([{**relation(), "auto": "yes"}], "boolean"),
    ([{**relation(), "direction": "sideways"}], "unsupported"),
])
def test_malformed_or_ambiguous_snapshot_fails_closed(bad, match):
    with pytest.raises(ValueError, match=match):
        build_capability_ontology_snapshot(bad)


def test_persistent_generation_is_default_off_invalid_values_fail_closed_and_rollback_is_explicit():
    default = auto_generation_status({})
    invalid = auto_generation_status({AUTO_GENERATION_ENV: "surprise"})
    enabled = auto_generation_status({AUTO_GENERATION_ENV: "enabled"})
    assert default["mode"] == "disabled" and default["enabled"] is False
    assert invalid["mode"] == "disabled" and invalid["config_valid"] is False
    assert enabled["enabled"] is True and enabled["config_valid"] is True
    assert AUTO_GENERATION_ENV in enabled["rollback"]


def test_generated_relation_consumption_is_independently_default_off():
    default = generated_relation_consumption_status({})
    invalid = generated_relation_consumption_status({GENERATED_RELATIONS_ENV: "maybe"})
    enabled = generated_relation_consumption_status({GENERATED_RELATIONS_ENV: "enabled"})
    assert default["enabled"] is False and default["mode"] == "disabled"
    assert invalid["enabled"] is False and invalid["config_valid"] is False
    assert enabled["enabled"] is True
    assert enabled["scope"] == "planner_and_agent_prompt_context"
    assert enabled["stored_relations_changed"] is False
    assert GENERATED_RELATIONS_ENV in enabled["rollback"]


def test_capability_gate_blocks_before_model_or_database_access(monkeypatch):
    from vera.ontologies import cap_ontology

    monkeypatch.delenv(AUTO_GENERATION_ENV, raising=False)
    monkeypatch.setattr(cap_ontology, "_auto_pair", lambda *args, **kwargs:
                        pytest.fail("model path must not be reached"))
    monkeypatch.setattr(cap_ontology, "_all_cap_names", lambda:
                        pytest.fail("registry scan must not be reached"))
    for call in (
        cap_ontology.co_auto_pair.__wrapped__("a", "b"),
        cap_ontology.co_auto_group.__wrapped__("a"),
        cap_ontology.co_auto_grid.__wrapped__(),
    ):
        result = asyncio.run(call)
        assert result["code"] == "generated_relations_disabled"
        assert result["relations_changed"] is False
        assert result["model_called"] is False


def test_explicit_enable_reaches_existing_auto_pair_validation(monkeypatch):
    from vera.ontologies import cap_ontology

    monkeypatch.setenv(AUTO_GENERATION_ENV, "enabled")
    monkeypatch.setattr(cap_ontology, "CAPABILITY_REGISTRY", {"a": {}, "b": {}})

    async def inferred(*args, **kwargs):
        return {"relation": "", "description": "", "direction": "forward",
                "strength": 0.0, "confidence": 0.0, "wire": ""}

    monkeypatch.setattr(cap_ontology, "_auto_pair", inferred)
    result = asyncio.run(cap_ontology.co_auto_pair.__wrapped__("a", "b", save=False))
    assert result["saved"] is False
    assert result["from"] == "a" and result["to"] == "b"


def test_snapshot_capability_uses_all_rows_without_mutating_them(monkeypatch):
    from vera.ontologies import cap_ontology

    rows = [relation()]
    monkeypatch.setattr(cap_ontology, "_db_all", lambda *args, **kwargs: rows)
    result = asyncio.run(cap_ontology.co_snapshot.__wrapped__())
    assert result["relation_count"] == 1
    assert result["snapshot_id"].startswith("capsont_")
    assert rows == [relation()]


def test_planner_context_excludes_generated_rows_by_default_but_keeps_manual(monkeypatch):
    from vera.ontologies import cap_ontology

    rows = [relation(), relation("prose.author", "llm.generate", auto=True)]
    monkeypatch.delenv(GENERATED_RELATIONS_ENV, raising=False)
    monkeypatch.setattr(cap_ontology, "_db_all", lambda *args, **kwargs: rows)
    result = asyncio.run(cap_ontology.co_context_for.__wrapped__(
        "code.author,llm.generate,prose.author", include_hidden=False))
    assert "code.author" in result["snippet"]
    assert "prose.author" not in result["snippet"]
    assert result["allowed_count"] == 1
    assert result["excluded_generated_count"] == 1
    assert result["generated_relations"]["enabled"] is False
    assert rows[1]["auto"] is True


def test_explicit_consumption_enable_restores_generated_context_without_changing_rows(monkeypatch):
    from vera.ontologies import cap_ontology

    rows = [relation(), relation("prose.author", "llm.generate", auto=True)]
    monkeypatch.setenv(GENERATED_RELATIONS_ENV, "enabled")
    monkeypatch.setattr(cap_ontology, "_db_all", lambda *args, **kwargs: rows)
    result = asyncio.run(cap_ontology.co_context_for.__wrapped__(
        "code.author,llm.generate,prose.author", include_hidden=False))
    assert "code.author" in result["snippet"] and "prose.author" in result["snippet"]
    assert result["allowed_count"] == 2
    assert result["excluded_generated_count"] == 0
    assert result["generated_relations"]["enabled"] is True
    assert len(rows) == 2


def test_generated_rows_remain_visible_in_matrix_when_context_use_is_disabled(monkeypatch):
    from vera.ontologies import cap_ontology

    rows = [relation(), relation("prose.author", "llm.generate", auto=True)]
    monkeypatch.delenv(GENERATED_RELATIONS_ENV, raising=False)
    monkeypatch.setattr(cap_ontology, "_all_cap_names", lambda: [
        "code.author", "llm.generate", "prose.author"])
    monkeypatch.setattr(cap_ontology, "_db_all", lambda *args, **kwargs: rows)
    result = asyncio.run(cap_ontology.co_matrix.__wrapped__())
    assert result["total_cells"] == 2
    assert any(cell["auto"] is True for cell in result["cells"])


def test_planner_context_treats_legacy_rows_without_auto_flag_as_manual(monkeypatch):
    from vera.ontologies import cap_ontology

    legacy = relation()
    legacy.pop("auto")
    monkeypatch.delenv(GENERATED_RELATIONS_ENV, raising=False)
    monkeypatch.setattr(cap_ontology, "_db_all", lambda *args, **kwargs: [legacy])
    result = asyncio.run(cap_ontology.co_context_for.__wrapped__(
        "code.author,llm.generate", include_hidden=False))
    assert result["allowed_count"] == 1
    assert result["excluded_generated_count"] == 0
