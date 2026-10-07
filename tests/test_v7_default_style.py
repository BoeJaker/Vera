"""v7 runs stepwise-reviewed when no style is named (user, 2026-10-02); auto stays
available; broad-stepwise-reviewed is broad-stepwise plus the CPU critic."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_v7_default_is_stepwise_reviewed_and_auto_is_still_a_style():
    assert PS.V7_DEFAULT_LOOP_STYLE == "stepwise-reviewed"
    assert PS.DEFAULT_LOOP_STYLE == "auto"                      # v6 / unknown-name fallback
    assert PS.resolve_loop_style("auto") == ("auto", PS.LOOP_STYLES["auto"], "default")


def test_broad_stepwise_reviewed_is_broad_stepwise_plus_the_critic():
    a = dict(PS.LOOP_STYLES["broad-stepwise-reviewed"])
    b = dict(PS.LOOP_STYLES["broad-stepwise"])
    assert a.pop("step_critic") is True
    a.pop("label"), b.pop("label")
    assert a == b
    assert PS.resolve_loop_style("broad-stepwise-reviewed")[2] == "requested"


def test_the_v7_wrapper_and_the_v7_route_default_to_it_but_honour_an_explicit_style():
    w = SRC[SRC.index("async def cap_dag_agent_loop_v7("):]
    w = w[:w.index("\n\n\n")]
    assert 'if not str(kwargs.get("plan_style") or "").strip():' in w
    assert "V7_DEFAULT_LOOP_STYLE" in w
    r = SRC[SRC.index("_v7_default = (version == \"v7\")"):]
    r = r[:r.index("v6_plan_style        =") + 200]
    assert 'if _v7_default else "auto"' in r
    assert 'body.get("plan_style") or _style_default' in r
