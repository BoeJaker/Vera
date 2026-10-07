"""A loop whose final gate said 'not met' must not finish as 'complete' or claim the
missing parts (census 2026-09-30: underspecified, author-then-edit)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import gate_finish_core as G  # noqa: E402


def test_an_open_gate_is_carried_to_the_end():
    last = {"complete": False, "missing": ["pause capability", "resume capability"]}
    assert G.unmet(last) == ["pause capability", "resume capability"]
    assert G.reason(G.unmet(last)) == "incomplete"
    n = G.note(G.unmet(last))
    assert "NOT met" in n and "- pause capability" in n and "never describe them as done" in n


def test_a_passed_gate_or_no_gate_claims_nothing():
    assert G.unmet({"complete": True, "missing": ["x"]}) == []
    assert G.unmet({}) == [] and G.unmet(None) == []
    assert G.reason([]) == "complete" and G.note([]) == ""


def test_work_after_the_verdict_means_the_verdict_is_stale():
    assert G.unmet({"complete": False, "missing": ["x"]}, ran_after=1) == []


def test_incomplete_without_a_named_gap_still_says_so():
    assert G.unmet({"complete": False, "missing": []}) == [
        "the final completion check did not confirm the goal was met"]


def test_bounded():
    assert len(G.unmet({"complete": False, "missing": [str(i) for i in range(20)]})) == G.MAX_ITEMS


def test_the_loop_carries_it_to_the_synthesiser_the_deliverable_and_done():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    assert "_gate_last = gate" in src and "_gate_ran_after += 1" in src
    i = src.index("_unmet = _gate_finish_core.unmet(_gate_last, _gate_ran_after)")
    tail = src[i:i + 6000]
    assert "unmet=_unmet" in tail.split("handover_output")[0]            # synthesiser
    assert tail.count("unmet=_unmet") >= 2                                # + delivery agent
    assert '"reason": _gate_finish_core.reason(_unmet), "unmet": _unmet' in tail
    for fn in ("async def _v5_synthesize_final(", "async def _v6_deliver("):
        sig = src[src.index(fn):src.index(fn) + 400]
        assert "unmet" in sig
