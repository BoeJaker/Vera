"""The broad planning style streams live into the agentic-loop UI.

Three links, each of which silently breaks the display on its own:
  1. the loop EMITS an event (dag_workshop_capabilities.py),
  2. the SSE wrapper FORWARDS it - /workshop/agent_loop/stream only passes
     types in ALWAYS_FORWARD, so an unlisted event never reaches the browser
     (agent_loop_v6.plan_style was emitted and dropped here from 2026-09-27),
  3. the panel RENDERS it (agent_loop_ouput.js).
Parsed from source - no app import - so it runs anywhere the gate runs.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
JS = (ROOT / "vera" / "agent_loop_ouput.js").read_text(encoding="utf-8")

LIVE = ["agent_loop_v6.plan_style",
        "agent_loop_v6.broad_split_token", "agent_loop_v6.broad_streams",
        "agent_loop_v6.broad_stream_token", "agent_loop_v6.broad_stream_planned",
        "agent_loop_v6.broad_brief_token", "agent_loop_v6.broad_stream_quick_brief",
        "agent_loop_v6.broad_stream_enriched", "agent_loop_v6.broad_brief_applied",
        "agent_loop_v6.broad_deduped"]


def _always_forward():
    tree = ast.parse(PY)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ALWAYS_FORWARD"
                                                 for t in node.targets):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError("ALWAYS_FORWARD not found")


def test_every_live_planning_event_is_emitted_by_the_loop():
    for t in LIVE:
        assert re.search(r'"type": "%s"' % re.escape(t), PY) or \
            re.search(r'_live\("%s"' % re.escape(t), PY), "%s is never emitted" % t


def test_every_live_planning_event_is_forwarded_to_the_ui():
    fwd = _always_forward()
    missing = [t for t in LIVE if t not in fwd]
    assert not missing, "emitted but dropped by the SSE wrapper: %s" % missing


def test_every_broad_event_the_loop_emits_is_forwarded():
    """Catches the NEXT broad event added without a forwarding entry."""
    emitted = set(re.findall(r'"(agent_loop_v6\.broad_[a-z_]+)"', PY))
    assert emitted, "no broad events found - the pattern is stale"
    assert emitted <= _always_forward(), sorted(emitted - _always_forward())


def test_every_live_planning_event_has_a_panel_handler():
    missing = [t for t in LIVE if ("'%s'" % t) not in JS]
    assert not missing, "forwarded but never rendered: %s" % missing


def test_the_broad_generations_stream_tokens():
    b = PY
    assert 'stream_cb=_live("agent_loop_v6.broad_split_token")' in b
    assert 'token_cb=_live("agent_loop_v6.broad_stream_token"' in b
    assert b.count('_live("agent_loop_v6.broad_brief_token"') == 2      # quick + deep


def test_the_planner_token_callback_is_opt_in():
    """Unset, the planner call is exactly as before (goldens pin the prompt; this
    pins that no stream callback appears where there was none)."""
    assert "stream_cb=token_cb)" in PY                                   # minimal body
    assert "stream_cb=(token_cb or (_plan_stream_cb if stream_id else None)))" in PY
