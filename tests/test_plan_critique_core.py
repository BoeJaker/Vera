"""Plan critique (roadmap A3, compute-roles 11 Oct 2026): the CPU 35B reviews the whole plan beside
the run; findings reach the adaptive controller once, as advice. Off by default (per run)."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import plan_critique_core as C  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical

STEPS = [{"id": 1, "title": "Research Valkey history", "caps": ["web.research"], "success": "3 dated sources"},
         {"id": 2, "title": "Write summary", "caps": ["prose.author"], "success": "report.md exists"}]


def test_worth_and_prompt():
    assert not C.worth([STEPS[0]]) and C.worth(STEPS) and not C.worth([])
    p = C.prompt("Report on Valkey", "report.md", STEPS)
    assert "GOAL:\nReport on Valkey" in p and "1. Research Valkey history  [tools: web.research]" in p
    assert "success: report.md exists" in p and "DONE WHEN:\nreport.md" in p


def test_parse_keeps_known_kinds_and_steps():
    raw = ('<think>x</think> {"findings": [{"kind": "missing deliverable", "step": 2, "note": "No step writes the '
           'comparison table the goal asks for."}, {"kind": "vibes", "note": "meh meh meh"}, '
           '{"kind": "gap", "step": 9, "note": "The March 2024 licence change is not researched."}]}')
    f = C.parse(raw, [1, 2])
    assert [x["kind"] for x in f] == ["missing_deliverable", "gap"]
    assert f[0]["step"] == 2 and f[1]["step"] is None
    assert C.parse("no json here", [1]) == [] and C.parse('{"findings": "x"}', [1]) == []
    many = '{"findings": [%s]}' % ",".join('{"kind": "gap", "note": "note number %d here"}' % i for i in range(9))
    assert len(C.parse(many, [])) == C.MAX_FINDINGS


def test_block():
    assert C.block([]) == ""
    b = C.block([{"kind": "gap", "step": 1, "note": "Covers only 2025."}, {"kind": "order", "step": None, "note": "x y z"}])
    assert b.startswith("PLAN REVIEW") and "- [gap] (step 1) Covers only 2025." in b and "- [order] x y z" in b


def test_wired_per_run_and_never_waited_on():
    assert 'v6_plan_critique = bool(body.get("plan_critique", False))' in SRC
    assert "plan_critique=v6_plan_critique," in SRC
    i = SRC.index("# Plan critique (A3): started now")
    assert "asyncio.create_task(" in SRC[i:i + 600] and "await _v6_plan_critique" not in SRC
    assert "+ _plan_crit_take(step.get(\"id\")))" in SRC
    assert "prefer_gpu=False" in SRC[SRC.index("async def _v6_plan_critique"):][:1500]
    assert '"agent_loop_v6.plan_critique_outcome"' in SRC
