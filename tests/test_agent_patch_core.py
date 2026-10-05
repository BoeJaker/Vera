"""A partial update of an agent (vera/agents/agent_patch_core.py): change the named fields, keep the rest."""
import dataclasses
import os
import sys
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.agents import agent_patch_core as ap  # noqa: E402


@dataclasses.dataclass
class Rec:
    id: str = "a1"
    name: str = "aide"
    created_at: str = "2026-01-01"
    author: str = "user"
    temperature: float = 0.7
    num_ctx: int = 0
    prefer_gpu: bool = True
    two_tier: str = "off"
    system_prompt: str = ""
    domain_caps: List[str] = dataclasses.field(default_factory=list)


TYPES = ap.field_types(Rec)
CUR = dataclasses.asdict(Rec(temperature=0.15, two_tier="fetched", domain_caps=["web.search"], system_prompt="be brief"))


def test_protected_fields_are_not_patchable():
    assert "id" not in TYPES and "created_at" not in TYPES and "author" not in TYPES
    new, changed, rejected = ap.apply_patch(CUR, {"id": "x", "author": "me"}, TYPES)
    assert new == CUR and changed == [] and {r["field"] for r in rejected} == {"id", "author"}


def test_only_the_named_fields_change_and_the_rest_survive():
    new, changed, _ = ap.apply_patch(CUR, {"system_prompt": "be thorough"}, TYPES)
    assert changed == ["system_prompt"] and new["system_prompt"] == "be thorough"
    # the fields agent.update would have reset survive a patch
    assert new["two_tier"] == "fetched" and new["temperature"] == 0.15 and new["domain_caps"] == ["web.search"]


def test_form_values_are_coerced_to_the_field_type():
    new, changed, rejected = ap.apply_patch(CUR, {"temperature": "0.3", "num_ctx": "8192", "prefer_gpu": "false",
                                                  "domain_caps": "web.search, memory.search,\n fabric.query"}, TYPES)
    assert rejected == []
    assert new["temperature"] == 0.3 and new["num_ctx"] == 8192 and new["prefer_gpu"] is False
    assert new["domain_caps"] == ["web.search", "memory.search", "fabric.query"]
    assert set(changed) == {"temperature", "num_ctx", "prefer_gpu", "domain_caps"}


def test_bad_values_and_unknown_fields_are_refused_with_a_reason():
    new, changed, rejected = ap.apply_patch(CUR, {"temperature": "warm", "prefer_gpu": "maybe", "nope": 1}, TYPES)
    assert changed == [] and new == CUR
    assert {r["field"] for r in rejected} == {"temperature", "prefer_gpu", "nope"}


def test_an_unchanged_value_is_not_a_change():
    _, changed, _ = ap.apply_patch(CUR, {"temperature": 0.15, "two_tier": "fetched"}, TYPES)
    assert changed == []


def test_the_current_record_is_not_modified():
    before = dict(CUR)
    ap.apply_patch(CUR, {"system_prompt": "x"}, TYPES)
    assert CUR == before
