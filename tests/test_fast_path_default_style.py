"""v7's DEFAULT plan style keeps the single-cap fast path; only a chosen style turns
it off. From 3 Oct 2026 (v7 defaulting to stepwise-reviewed) the guard read the
default as a choice: every fast-path census goal planned, 3-8x slower."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def _v6_signature():
    s = SRC[SRC.index("async def cap_dag_agent_loop_v6("):]
    return s[:s.index("\n):")]


def test_v6_takes_the_flag_off_by_default():
    assert "plan_style_defaulted: bool = False," in _v6_signature()


def test_the_guard_spares_a_defaulted_style():
    assert 'if _plan_style_eff != "auto" and not plan_style_defaulted:' in SRC
    assert 'if _plan_style_eff != "auto":\n        enable_fast_path = False' not in SRC


def test_the_v7_wrapper_marks_its_default():
    w = SRC[SRC.index("async def cap_dag_agent_loop_v7("):]
    w = w[:w.index("\n\n\n")]
    block = w[w.index('if not str(kwargs.get("plan_style") or "").strip():'):]
    block = block[:block.index("kwargs.setdefault(")]
    assert 'kwargs["plan_style_defaulted"] = True' in block


def test_the_v7_route_marks_its_default_and_passes_it():
    r = SRC[SRC.index('_v7_default = (version == "v7")'):]
    assert 'v6_plan_style_defaulted = bool(_v7_default and not str(body.get("plan_style") or "").strip())' in r
    assert "plan_style_defaulted=v6_plan_style_defaulted," in r
