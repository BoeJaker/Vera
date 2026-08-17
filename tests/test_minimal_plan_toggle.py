"""Minimal-plan-primary switch (_v5_minimal_plan_primary) — 2026-08-17: use the
minimal/tolerant plan schema (the goal-focused fallback) as the primary plan instead
of the full schema that emitted generic non-goal steps. Imports the monolith, runs
in-container.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def _with_env(val):
    orig = os.environ.get("VERA_LOOP_MINIMAL_PLAN")
    try:
        if val is None:
            os.environ.pop("VERA_LOOP_MINIMAL_PLAN", None)
        else:
            os.environ["VERA_LOOP_MINIMAL_PLAN"] = val
        return W._v5_minimal_plan_primary()
    finally:
        if orig is None:
            os.environ.pop("VERA_LOOP_MINIMAL_PLAN", None)
        else:
            os.environ["VERA_LOOP_MINIMAL_PLAN"] = orig


def test_default_is_on():
    assert _with_env(None) is True


def test_explicit_on_values():
    for v in ("1", "true", "yes", "on", "TRUE", "On"):
        assert _with_env(v) is True


def test_explicit_off_restores_full_schema_primary():
    for v in ("0", "false", "no", "off", "False", "OFF"):
        assert _with_env(v) is False
