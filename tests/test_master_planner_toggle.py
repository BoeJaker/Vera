"""Master-planner force switch (_v5_force_master_planner) — 2026-08-17 owner request
to run the strategic master planner as the primary plan source (the tier-'simple'
single-call planner was emitting generic non-goal steps). Imports the monolith, so
it runs in-container.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import dag_workshop_capabilities as W  # noqa: E402


def _with_env(val):
    orig = os.environ.get("VERA_LOOP_FORCE_MASTER_PLANNER")
    try:
        if val is None:
            os.environ.pop("VERA_LOOP_FORCE_MASTER_PLANNER", None)
        else:
            os.environ["VERA_LOOP_FORCE_MASTER_PLANNER"] = val
        return W._v5_force_master_planner()
    finally:
        if orig is None:
            os.environ.pop("VERA_LOOP_FORCE_MASTER_PLANNER", None)
        else:
            os.environ["VERA_LOOP_FORCE_MASTER_PLANNER"] = orig


def test_default_is_on():
    assert _with_env(None) is True


def test_explicit_on_values():
    for v in ("1", "true", "yes", "on", "TRUE", "On"):
        assert _with_env(v) is True


def test_explicit_off_values_restore_fallback_only():
    for v in ("0", "false", "no", "off", "False", "OFF"):
        assert _with_env(v) is False
