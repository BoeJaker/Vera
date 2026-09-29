"""The intent core is opt-in on the loop runner and applied after the intent pass."""

import inspect
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

# vera.* first: on the host, Vera.vera.* resolves to the primary checkout (main),
# not this worktree, and would test the wrong code.
try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    try:
        from Vera.vera.dag import dag_workshop_capabilities as M
    except Exception:
        M = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


@needs_app
def test_runner_default_is_off():
    assert inspect.signature(M.cap_dag_agent_loop_v6).parameters["intent_core"].default == "off"


def test_applied_after_the_intent_pass_and_before_the_fast_path():
    intent_ev = SRC.index('"type": "agent_loop_v6.intent", "session_id": sid')
    core = SRC.index('"type": "agent_loop_v6.intent_core"')
    fast = SRC.index("shortcut = await _v7_single_cap_shortcut(")
    assert intent_ev < core < fast
    # v7's master planner (the v5 runner above has its own copy) comes after it too
    assert SRC.find("mp = await _v5_master_plan(goal, catalog_brief", core) > fast


def test_http_body_passes_it_to_the_runner():
    assert 'v6_intent_core       = (body.get("intent_core", "off")' in SRC
    assert "intent_core=v6_intent_core," in SRC


def test_settings_panel_offers_it():
    js = (ROOT / "vera" / "agent_loop_config_element.js").read_text(encoding="utf-8")
    assert "k: 'intent_core'" in js and "['order'," in js
