"""The agentic loop takes a planning style (plan_style), logs which one ran, and
the census can force one.

Styles: auto (the behaviour before styles existed), flat, stepwise, detailed -
their meaning for the loop is ONE table, planner_styles.LOOP_STYLES, which the
loop reads at its own call sites. Pure tests use the lowercase import; the
wiring tests parse the loop's source (the repo's call-site pattern); the
controller test imports the app and runs in-container, where the gate runs.
"""
import ast
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.planning import planner_styles as PS  # noqa: E402
from vera.dag import loop_trace_core as T  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


# ── the table ────────────────────────────────────────────────────────────────

def test_the_four_styles_exist_and_auto_is_the_default():
    assert PS.loop_style_ids() == ["auto", "flat", "stepwise", "detailed"]
    assert PS.resolve_loop_style("")[0] == "auto"
    assert PS.resolve_loop_style(None)[0] == "auto"
    assert PS.resolve_loop_style("auto")[2] == "default"


def test_a_known_style_resolves_to_itself():
    for s in ("flat", "stepwise", "detailed"):
        eff, sw, why = PS.resolve_loop_style(s.upper())
        assert eff == s and why == "requested" and sw == PS.LOOP_STYLES[s]


def test_an_unknown_style_runs_as_auto_and_says_so():
    eff, sw, why = PS.resolve_loop_style("stepwsie")
    assert eff == "auto" and sw == PS.LOOP_STYLES["auto"]
    assert "unknown style" in why and "stepwsie" in why


def test_auto_is_everything_the_loop_did_before_styles():
    a = PS.LOOP_STYLES["auto"]
    assert a["run_planner"] and a["master_plan"] and a["recon"] and a["shape_guards"]
    assert not a["lens_brief"] and not a["stepwise_controller"]


def test_flat_is_one_plan_with_no_escalation_and_no_recon():
    f = PS.LOOP_STYLES["flat"]
    assert f["run_planner"] and not f["master_plan"] and not f["recon"]


def test_stepwise_skips_the_planner_and_the_guards_that_would_undo_it():
    s = PS.LOOP_STYLES["stepwise"]
    assert not s["run_planner"] and not s["master_plan"] and not s["recon"]
    assert not s["shape_guards"] and s["stepwise_controller"]


def test_detailed_briefs_the_planner_rather_than_replacing_it():
    d = PS.LOOP_STYLES["detailed"]
    assert d["lens_brief"] and d["run_planner"]


def test_resolving_returns_a_copy_the_loop_cannot_corrupt():
    _, sw, _ = PS.resolve_loop_style("flat")
    sw["recon"] = True
    assert PS.LOOP_STYLES["flat"]["recon"] is False


# ── the log: trace digest -> census row ─────────────────────────────────────

def test_the_trace_digest_records_asked_and_used_style():
    d = T.digest_events([
        {"type": "agent_loop_v6.plan_style", "requested": "detailed",
         "effective": "auto", "reason": "detailed: no lens answered - planned without a brief"},
        {"type": "agent_loop_v6.plan", "steps": [{"id": 1, "title": "t", "caps": []}]},
    ])
    plan = d.get("plan") or {}
    assert plan["style"] == "auto" and plan["style_requested"] == "detailed"
    assert "no lens answered" in plan["style_reason"]


def test_a_run_from_before_styles_has_no_style_in_its_digest():
    d = T.digest_events([{"type": "agent_loop_v6.plan", "steps": []}])
    assert "style" not in (d.get("plan") or {})


# ── wiring, parsed from the loop's source ───────────────────────────────────

def _fn(name):
    tree = ast.parse(SRC)
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _body(name):
    f = _fn(name)
    return "\n".join(SRC.splitlines()[f.lineno - 1:f.end_lineno])


def test_v6_takes_plan_style_defaulting_to_auto():
    f = _fn("cap_dag_agent_loop_v6")
    args = f.args.args + f.args.kwonlyargs
    names = [a.arg for a in args]
    assert "plan_style" in names
    defaults = dict(zip([a.arg for a in f.args.args][-len(f.args.defaults):], f.args.defaults))
    defaults.update({a.arg: d for a, d in zip(f.args.kwonlyargs, f.args.kw_defaults)})
    assert isinstance(defaults["plan_style"], ast.Constant) and defaults["plan_style"].value == "auto"


def test_the_stream_endpoint_forwards_plan_style():
    assert 'v6_plan_style        = (body.get("plan_style", "auto")' in SRC
    assert "plan_style=v6_plan_style," in SRC


def test_every_planning_site_reads_its_switch():
    b = _body("cap_dag_agent_loop_v6")
    assert 'if _pstyle.get("run_planner", True):' in b
    assert '_master_ok = enable_master_planner and bool(_pstyle.get("master_plan", True))' in b
    assert b.count("_master_ok") >= 3                      # defined + both master branches
    assert 'enable_recon and _pstyle.get("recon", True)' in b
    assert '_shape_guards = bool(_pstyle.get("shape_guards", True))' in b
    assert "if _shape_guards and _plan_drifted(_orig_goal, steps):" in b
    assert "_plan_hygiene is not None and steps and _shape_guards" in b
    assert 'if _pstyle.get("lens_brief")' in b
    assert "plan_note=_style_note" in b


def test_the_run_emits_and_returns_the_style_it_used():
    b = _body("cap_dag_agent_loop_v6")
    assert '"type": "agent_loop_v6.plan_style"' in b
    assert '"plan_style": _plan_style_rec,' in b


def test_the_controller_is_told_about_stepwise_only_in_stepwise():
    b = _body("cap_dag_agent_loop_v6")
    assert "STEPWISE_CONTROLLER_NOTE" in b and 'stepwise_controller' in b


def test_an_explicit_style_skips_the_single_cap_fast_path():
    b = _body("cap_dag_agent_loop_v6")
    assert 'if _plan_style_eff != "auto":\n        enable_fast_path = False' in b


# ── the controller's prompt, for real ───────────────────────────────────────

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


def _controller_system(monkeypatch, **extra):
    seen = {}

    async def _stub(prompt, system=None, **kw):
        seen.setdefault("system", system or "")
        return '{"action":"continue","goal_met":false}'
    monkeypatch.setattr(M, "_safe_ollama_generate_dw", _stub)
    last = {"id": 1, "title": "bootstrap", "ok": True, "summary": "did a thing"}
    asyncio.run(M._v6_control(
        "make a thing", "a thing exists", [last], [], last,
        catalog_names=["exec.bash.run"], valid_skill_ids=set(), base_id=1,
        steps_left=5, model="", instance_id="", prefer_gpu=True, **extra))
    return seen["system"]


@needs_app
def test_the_stepwise_note_reaches_the_controller_prompt(monkeypatch):
    sys_sw = _controller_system(monkeypatch, style_note=PS.STEPWISE_CONTROLLER_NOTE)
    assert "STEPWISE MODE" in sys_sw


@needs_app
def test_every_other_style_leaves_the_controller_prompt_byte_identical(monkeypatch):
    before = _controller_system(monkeypatch)
    empty = _controller_system(monkeypatch, style_note="")
    assert before == empty and "STEPWISE MODE" not in before
