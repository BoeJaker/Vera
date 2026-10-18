"""plan.goal_frame (compute-roles B2 / four-minds FM-1.3, agreed 11 Oct 2026): the goal's dates,
versions and numeric limits become done_when items and the final gate's must-cover list, and the
run records which ones its answer met. Off by default."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import goal_constraints_core as C  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
SB = (ROOT / "vera" / "minds" / "switchboard_core.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical


def test_extracts_dates_versions_and_limits():
    g = ("Write a report on Valkey since March 2024 for Python 3.12 users, in under 500 words, "
         "listing the top 5 changes made in 2025.")
    cons = C.extract(g)
    kinds = [(c["kind"], c["token"]) for c in cons]
    assert ("date", "2024") in kinds and ("date", "2025") in kinds
    assert ("version", "3.12") in kinds
    assert ("word_limit", "500") in kinds and ("count", "5") in kinds
    assert C.extract("write hello.html with a heading") == []
    assert len(C.extract(" ".join("in %d" % (2000 + i) for i in range(20)))) == C.MAX_ITEMS


def test_block_and_coverage():
    cons = C.extract("Summarise news since 2024 in under 10 words for Python 3.12")
    assert C.block(cons).startswith("GOAL CONSTRAINTS") and C.block([]) == ""
    cov = C.coverage(cons, "Python 3.12 shipped; 2024 was busy.")
    assert cov["missing"] == [] and cov["ratio"] == 1.0
    cov = C.coverage(cons, "A long answer " * 10)
    assert "since 2024" in cov["missing"] and any("under 10 words" == m for m in cov["missing"])


def test_wired_behind_the_switch():
    assert '_f("plan.goal_frame", ("off", "on", "writer"), "loop", "FM-1.3",' in SB
    assert 'mode_of("plan.goal_frame")' in SRC
    i = SRC.index('    if _gf_mode in ("on", "writer"):\n        _found = _goal_cons_core.extract(goal)')
    assert i < SRC.index('    steps = plan.get("steps") or []\n    if not steps:', i)
    assert "_gate_goal = _gate_goal + \"\\n\\n\" + _goal_cons_core.block(_goal_cons)" in SRC
    assert '"agent_loop_v6.goal_constraint_coverage"' in SRC


def test_writer_mode_gives_the_constraints_to_the_writer_not_the_gate():
    # R-B2 (compute-roles, 12 Oct; four-minds FM-1.3 retry): 'on' hurt (self-verification, research loops) - 'writer'
    # appends the constraints to code.author / prose.author tasks only, and coverage is a shadow signal
    cons = C.extract("Summarise news since 2024 in under 10 words for Python 3.12")
    blk = C.writer_block(cons)
    assert blk.startswith("HARD CONSTRAINTS FROM THE GOAL") and "since 2024" in blk and C.writer_block([]) == ""
    assert '_f("plan.goal_frame", ("off", "on", "writer"), "loop", "FM-1.3",' in SB
    assert 'elif _found:\n            _goal_writer_cons = _found\n            _GOAL_WRITER_CONS.set(_goal_writer_cons)' in SRC
    assert 'if _found and _gf_mode == "on":\n            _goal_cons = _found' in SRC       # only 'on' touches done_when
    assert SRC.count("_writer_constraints_apply(") == 3                                  # the def + both call_tool sites
    assert SRC.index("h_args = _writer_constraints_apply(hop_tool, h_args)") < SRC.index("invoke = await call_tool(hop_tool, h_args")
    assert SRC.index("args = _writer_constraints_apply(tool, args)") < SRC.index("invoke = await call_tool(tool, args, session_id=sid")
    assert "_goal_cons_core.coverage(_goal_cons or _goal_writer_cons, deliverable or final)" in SRC


def test_the_apply_helper_touches_only_authoring_tasks():
    import ast, contextvars, types
    tree = ast.parse(SRC)
    ns = {"Optional": object, "List": list, "Dict": dict, "Any": object, "_goal_cons_core": C,
          "_V5_AUTHORING_CAPS": {"code.author", "prose.author"}, "contextvars": contextvars}
    for name in ("_writer_constraints_apply",):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        ns["_GOAL_WRITER_CONS"] = contextvars.ContextVar("t", default=None)
        exec(compile(ast.get_source_segment(SRC, node), "dag", "exec"), ns)
    apply = ns["_writer_constraints_apply"]
    cons = C.extract("a report in under 50 words about Python 3.12")
    assert apply("prose.author", {"task": "write it"}) == {"task": "write it"}          # mode off: var unset
    ns["_GOAL_WRITER_CONS"].set(cons)
    out = apply("prose.author", {"task": "write it", "path": "r.md"})
    assert out["path"] == "r.md" and out["task"].startswith("write it\n\nHARD CONSTRAINTS") and "under 50 words" in out["task"]
    assert apply("prose.author", out) == out                                            # idempotent
    assert apply("web.fetch", {"task": "x"}) == {"task": "x"} and apply("code.author", {"path": "a.py"}) == {"path": "a.py"}
