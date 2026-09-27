"""A zero-shot intent from the NLP nodes is recorded beside the intent the loop
used (roadmap B1) - measured, never read back. Scores in these tests are the
ones measured on the census goals (DeBERTa-v3 mnli on cpu-246, 2026-09-27).
"""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import intent_zeroshot_core as Z  # noqa: E402
from vera.dag import loop_trace_core as T  # noqa: E402
from vera.evolve import loop_record_core as R  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")


def _res(build, research, action):
    L = Z.LABELS
    return {"ok": True, "labels": [{"label": L["build"], "score": build},
                                   {"label": L["research"], "score": research},
                                   {"label": L["action"], "score": action}]}


def test_mixed_is_not_a_candidate_and_is_read_off_build_and_research():
    assert "mixed" not in Z.LABELS and Z.MULTI_LABEL
    assert Z.parse(_res(0.89, 0.22, 0.0))[0] == "build"        # long-horizon
    assert Z.parse(_res(0.76, 0.97, 0.03))[0] == "mixed"       # research-report
    assert Z.parse(_res(0.43, 0.78, 0.47))[0] == "research"    # trivial-chat (a miss, recorded)


def test_unreadable_answers_give_nothing():
    assert Z.parse({"error": "down"}) == (None, {})
    assert Z.parse({"labels": [{"label": "something else", "score": 0.9}]}) == (None, {})
    assert Z.parse(None) == (None, {})


def test_compare_records_agreement_margin_and_confidence():
    zs, sc = Z.parse(_res(0.63, 0.02, 0.0))
    rec = Z.compare(zs, sc, used="build", heuristic="build", llm="")
    assert rec["agrees_used"] is True and rec["agrees_llm"] is None
    assert rec["confidence"] == 0.63 and rec["margin"] == 0.61
    rec2 = Z.compare("research", {"research": 0.5}, used="mixed", heuristic="mixed", llm="mixed")
    assert rec2["agrees_used"] is False and rec2["agrees_llm"] is False


def test_the_digest_and_the_loop_record_carry_it():
    events = [{"type": "agent_loop_v6.intent", "intent": "build"},
              {"type": "agent_loop_v6.intent_zeroshot", "zeroshot": "build", "margin": 0.61,
               "agrees_used": True, "agrees_llm": None},
              {"type": "agent_loop_v6.done", "session_id": "c1", "ts": "2026-09-27T10:00:00+00:00"}]
    d = T.digest_events(events)
    assert d["plan"]["intent"] == "build"                       # the real intent is untouched
    assert d["plan"]["intent_zeroshot"]["zeroshot"] == "build"
    compact, _ = R.run_record_from_events("c1", events, d)
    assert compact["intent_zeroshot"] == "build" and compact["intent_zeroshot_agrees"] is True


def _wire(monkeypatch, res, delay=0.0):
    seen = {"calls": [], "events": []}

    async def fake_zs(text="", labels=None, multi_label=False, **kw):
        seen["calls"].append({"labels": labels, "multi_label": multi_label})
        await asyncio.sleep(delay)
        return res

    async def fake_emit(ev):
        seen["events"].append(ev)

    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "nlp.zeroshot", {"raw": fake_zs})
    monkeypatch.setattr(M, "emit_event", fake_emit)
    return seen


@needs_app
def test_the_loop_records_the_zero_shot_beside_the_used_intent(monkeypatch):
    seen = _wire(monkeypatch, _res(0.89, 0.22, 0.0))

    async def go():
        task = asyncio.ensure_future(M._v6_zeroshot_intent("Build a to-do list web app"))
        await M._v6_intent_measure(task, {"heuristic": "build", "llm": ""}, "build",
                                   sid="s", stream_id="")

    asyncio.run(go())
    assert seen["calls"][0]["multi_label"] is True and len(seen["calls"][0]["labels"]) == 3
    (ev,) = [e for e in seen["events"] if e["type"] == "agent_loop_v6.intent_zeroshot"]
    assert ev["zeroshot"] == "build" and ev["used"] == "build" and ev["agrees_used"] is True


@needs_app
def test_zero_shot_down_or_slow_changes_nothing(monkeypatch):
    seen = _wire(monkeypatch, {"error": "no model"})

    async def go(task_goal="x"):
        task = asyncio.ensure_future(M._v6_zeroshot_intent(task_goal))
        await M._v6_intent_measure(task, {}, "build", sid="s", stream_id="")

    asyncio.run(go())
    assert not seen["events"]
    seen = _wire(monkeypatch, _res(0.9, 0.1, 0.0), delay=5.0)
    monkeypatch.setattr(M, "_ENTITY_NER_TIMEOUT_S", 0.05)
    asyncio.run(asyncio.wait_for(go(), timeout=2))
    assert not seen["events"]


@needs_app
def test_it_runs_only_when_the_run_decides_an_intent_and_reports_before_done():
    import inspect
    src = inspect.getsource(M.cap_dag_agent_loop_v6)
    assert "if (_intent_zs is not None and enable_tiering) else None" in src
    i_m = src.index("await _v6_intent_measure(_intent_zs_task")
    i_done = src.index('{"type": "agent_loop_v6.done", "stream_id": stream_id, "session_id": sid,')
    assert i_m < i_done
    assert "agent_loop_v6.intent_zeroshot" in inspect.getsource(M)    # forwarded to the UI
