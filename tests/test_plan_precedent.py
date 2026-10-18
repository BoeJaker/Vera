"""Precedent for the planner from similar past runs (four-minds FM-4.2, switch plan.precedent)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import precedent_core as PC, switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical


def test_a_run_outcome():
    ok = [{"complete": True, "missing": []}]
    assert PC.outcome("done", ok) == "good" and PC.outcome("done", ok, 0.8) == "good"
    assert PC.outcome("done", ok, 0.6) == "unknown"
    assert PC.outcome("done", [{"complete": False, "missing": ["a chart"]}]) == "bad"
    assert PC.outcome("failed", ok) == "bad" and PC.outcome("done", ok, 0.3) == "bad"
    assert PC.outcome("done", []) == "unknown" and PC.outcome("done", ok, "None") == "good"


def test_a_run_record_from_a_trace():
    trace = {"run": {"goal": "Produce a short report on Valkey", "status": "done"},
             "gates": [{"complete": True, "missing": []}], "plan": {"style": "stepwise-reviewed"},
             "steps": [{"title": "Fetch", "calls": [{"tool": "web.fetch", "ok": True}, {"tool": "x", "ok": False}]},
                       {"title": "Write", "calls": [{"tool": "prose.author", "ok": True}]}]}
    r = PC.run_record({"run_id": "r1", "source": "census", "template": "default", "score": 1.0}, trace)
    assert r["outcome"] == "good" and r["steps"] == [{"title": "Fetch", "tools": ["web.fetch"]},
                                                       {"title": "Write", "tools": ["prose.author"]}]
    assert r["origin"] == "census" and r["style"] == "stepwise-reviewed"
    assert PC.run_record({"run_id": "r2"}, {"run": {}}) is None


def test_choose_takes_good_ones_and_one_warning_and_keeps_census_honest():
    g = {"run": "a", "goal": "Report on Valkey", "outcome": "good", "origin": "user", "steps": []}
    near = [(0.95, dict(g, run="same", goal="report on valkey", origin="census")),        # census, same goal
            (0.9, g), (0.85, dict(g, run="b")), (0.8, dict(g, run="c")),
            (0.75, dict(g, run="d", outcome="bad")), (0.7, dict(g, run="e", outcome="bad")),
            (0.5, dict(g, run="f")), (0.99, dict(g, run="me"))]
    ch = PC.choose(near, "Report on  VALKEY", "census", exclude_run="me")
    assert [p["run"] for p in ch] == ["a", "b", "d"] and ch[0]["similarity"] == 0.9
    assert [p["run"] for p in PC.choose(near, "Report on Valkey", "user", "me")] == ["same", "a", "d"]
    assert PC.choose([(0.5, g)], "x", "user") == []


def test_render_and_wrap_keep_the_objective_last():
    good = {"goal": "Report on Valkey", "outcome": "good", "steps_n": 2,
            "steps": [{"title": "Fetch", "tools": ["web.fetch"]}, {"title": "Write", "tools": []}]}
    bad = {"goal": "Report on Redis", "outcome": "bad", "steps": [{"title": "Fetch", "tools": []}], "missing": ["citations"]}
    block = PC.render([good, bad])
    assert "done well in 2 step(s): Fetch [web.fetch]; Write" in block and "WARNING" in block and "citations" in block
    assert PC.render([]) == "" and PC.wrap("p", "p", "") == "p"
    w = PC.wrap("Report on X", "Report on X", block)
    assert w.startswith("OBJECTIVE") and w.rstrip().endswith("Plan for the OBJECTIVE stated above: Report on X")
    recalled = "OBJECTIVE ...\n----- END BACKGROUND -----\n\nPlan for the OBJECTIVE stated above: Report on X"
    w2 = PC.wrap(recalled, "Report on X", block)
    assert w2.index("PRECEDENT") < w2.index("Plan for the OBJECTIVE stated above") and w2.endswith("Report on X")


def test_the_switch_and_the_loop_hook():
    f = SB.REGISTRY["plan.precedent"]
    assert f.modes == ("off", "on") and f.default == "off"
    src = open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()
    hook = src[src.index("# ── Precedent (four-minds FM-4.2"):src.index("# ── Catalog (identical construction to v5)")]
    assert '_minds_core.mode_of("plan.precedent") == "on"' in hook and "timeout=4" in hook
    assert "plan_goal = _precedent_core.wrap(plan_goal, _orig_goal" in hook and "goal = " not in hook.replace("plan_goal", "")
    assert 'os.path.join(_here, "minds/precedent.py"),' in open(
        os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    pr = open(os.path.join(ROOT, "vera", "minds", "precedent.py"), encoding="utf-8").read()
    assert 'DD.knn("run_goals"' in pr and 'getattr(_orch, "_IS_WORKER", False)' in pr
