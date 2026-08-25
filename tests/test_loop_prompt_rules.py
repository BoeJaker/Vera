"""Loop prompt rule registry (vera/dag/loop_prompt_rules.py) — Phase 1.

One definition per rule, with the stages that must carry it named. The registry
exists because rules shared between stages were being edited in one place and
silently missed in another, in both directions — see the module docstring.

Pure (no app import), so it runs in the critical gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import loop_prompt_rules as R  # noqa: E402


# ── the registry is populated and self-consistent ────────────────────────────
def test_every_rule_has_text_stages_and_evidence():
    assert R.RULES, "registry is empty"
    for rid, rule in R.RULES.items():
        assert rule.id == rid, f"{rid}: id does not match its key"
        assert rule.text.strip(), f"{rid}: empty text"
        assert rule.stages, f"{rid}: no consumers declared — nothing would carry it"
        assert rule.evidence.strip(), f"{rid}: no evidence recorded for why it exists"


def test_the_two_known_drift_rules_are_registered():
    """Both bit us for real; they are the reason this module exists."""
    assert "cap_routing" in R.RULES
    assert "criteria_settleable" in R.RULES


def test_both_planner_variants_are_declared_consumers():
    """A rule that names only one planner variant is the exact bug we hit twice."""
    for rid in ("cap_routing", "criteria_settleable"):
        stages = R.RULES[rid].stages
        assert "planner:full" in stages and "planner:minimal" in stages, (
            f"{rid} must be declared for BOTH planner variants, got {stages}")


# ── lookups ──────────────────────────────────────────────────────────────────
def test_rules_for_returns_only_that_stages_rules():
    ids = R.rule_ids_for("controller")
    assert "cap_routing" in ids
    assert "criteria_settleable" not in ids       # controller does not carry it


def test_unknown_stage_returns_nothing_rather_than_raising():
    assert R.rules_for("no-such-stage") == []
    assert R.rule_ids_for("") == []


def test_rule_ids_for_matches_rules_for():
    for stage in ("planner:full", "planner:minimal", "controller", "adjust"):
        assert R.rule_ids_for(stage) == [r.id for r in R.rules_for(stage)]


# ── the drift check ──────────────────────────────────────────────────────────
def test_missing_from_detects_an_absent_rule():
    """THE point of the registry: name what a prompt should carry and doesn't."""
    composed = R.RULES["cap_routing"].text          # carries one, not the other
    missing = R.missing_from("planner:minimal", composed)
    assert missing == ["criteria_settleable"]


def test_missing_from_is_empty_when_everything_is_present():
    composed = "".join(r.text for r in R.rules_for("planner:full"))
    assert R.missing_from("planner:full", composed) == []


def test_missing_from_handles_empty_and_none():
    assert set(R.missing_from("planner:full", "")) == set(R.rule_ids_for("planner:full"))
    assert set(R.missing_from("planner:full", None)) == set(R.rule_ids_for("planner:full"))


# ── the rule bodies are prompt text, not placeholders ────────────────────────
def test_rule_text_looks_like_real_prompt_content():
    assert "CAPABILITY ROUTING" in R.RULES["cap_routing"].text
    assert "code.author" in R.RULES["cap_routing"].text
    assert len(R.RULES["cap_routing"].text) > 500
    assert len(R.RULES["criteria_settleable"].text) > 500
