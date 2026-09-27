"""The loop measures how much of what the goal NAMED its final output carries -
the goal's entities from the NLP nodes' NER, checked against the output
(roadmap B4, user 2026-09-27: use the NER/NLP nodes for reporting). A measure
that is reported, never a criterion; NER being down or slow changes nothing.
"""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import entity_coverage_core as C  # noqa: E402
from vera.dag import loop_trace_core as T  # noqa: E402
from vera.evolve import loop_record_core as R  # noqa: E402

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")

NER = {"entities": [
    {"entity": "ORG", "word": "Redis", "score": 0.99},
    {"entity": "ORG", "word": "Valkey", "score": 0.97},
    {"entity": "DATE", "word": "March 2024", "score": 0.95},
    {"entity": "QUANTITY", "word": "90 seconds", "score": 0.9},
    {"entity": "CARDINAL", "word": "one", "score": 0.99},      # spelled-out number: dropped
    {"entity": "ORG", "word": "redis", "score": 0.99},         # duplicate: dropped
    {"entity": "PERSON", "word": "Bob", "score": 0.3},         # low confidence: dropped
    {"entity": "ORDINAL", "word": "first", "score": 0.99},     # not a checked label
]}


def test_goal_entities_keep_named_and_digit_bearing_ones():
    ents = C.goal_entities(NER)
    assert [e["text"] for e in ents] == ["Redis", "Valkey", "March 2024", "90 seconds"]
    assert C.goal_entities({"error": "down"}) == [] and C.goal_entities(None) == []


def test_coverage_is_whole_token_and_case_blind():
    ents = C.goal_entities(NER)
    out = "Redis moved to RSAL/SSPL in March 2024; the timer now counts down from 90 seconds."
    cov = C.coverage(ents, out)
    assert cov["covered"] == ["Redis", "March 2024", "90 seconds"]
    assert cov["missing"] == ["Valkey"] and cov["ratio"] == 0.75
    # a token inside another word is not a mention
    assert C.coverage([{"text": "Redis", "norm": "redis"}], "Rediscover it")["missing"] == ["Redis"]
    assert C.coverage([], "anything")["ratio"] is None


def test_the_trace_digest_and_the_loop_record_carry_it():
    events = [{"type": "agent_loop_v6.entity_coverage", "entities": 4, "ratio": 0.75,
               "covered": ["Redis"], "missing": ["Valkey"]},
              {"type": "agent_loop_v6.done", "session_id": "c1", "ts": "2026-09-27T10:00:00+00:00"}]
    d = T.digest_events(events)
    assert d["plan"]["entity_coverage"] == {"entities": 4, "ratio": 0.75, "missing": ["Valkey"]}
    compact, _ = R.run_record_from_events("c1", events, d)
    assert compact["entity_coverage"] == 0.75


def _wire(monkeypatch, ner_result=None, delay=0.0, raise_exc=False):
    seen = {"calls": [], "events": []}

    async def fake_ner(text="", **kw):
        seen["calls"].append(text)
        await asyncio.sleep(delay)
        if raise_exc:
            raise RuntimeError("node down")
        return ner_result

    async def fake_emit(ev):
        seen["events"].append(ev)

    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "nlp.ner", {"raw": fake_ner})
    monkeypatch.setattr(M, "emit_event", fake_emit)
    return seen


@needs_app
def test_the_loop_emits_coverage_from_the_nlp_node(monkeypatch):
    seen = _wire(monkeypatch, NER)

    async def go():
        task = asyncio.ensure_future(M._v6_goal_entities("compare Redis and Valkey"))
        await M._v6_entity_coverage(task, "Redis changed licence in March 2024.", sid="s", stream_id="")

    asyncio.run(go())
    (ev,) = [e for e in seen["events"] if e["type"] == "agent_loop_v6.entity_coverage"]
    assert ev["entities"] == 4 and ev["missing"] == ["Valkey", "90 seconds"]
    assert ev["labels"]["Redis"] == "ORG"


@needs_app
@pytest.mark.parametrize("kw", [{"raise_exc": True}, {"ner_result": {"error": "no model"}}])
def test_ner_down_changes_nothing(monkeypatch, kw):
    seen = _wire(monkeypatch, **kw)

    async def go():
        task = asyncio.ensure_future(M._v6_goal_entities("compare Redis and Valkey"))
        await M._v6_entity_coverage(task, "anything", sid="s", stream_id="")

    asyncio.run(go())
    assert seen["calls"] and not seen["events"]


@needs_app
def test_slow_ner_is_bounded(monkeypatch):
    seen = _wire(monkeypatch, NER, delay=5.0)
    monkeypatch.setattr(M, "_ENTITY_NER_TIMEOUT_S", 0.05)

    async def go():
        task = asyncio.ensure_future(M._v6_goal_entities("compare Redis and Valkey"))
        await M._v6_entity_coverage(task, "anything", sid="s", stream_id="")

    asyncio.run(asyncio.wait_for(go(), timeout=2))
    assert not seen["events"]


@needs_app
def test_the_run_measures_before_it_reports_done():
    import inspect
    src = inspect.getsource(M.cap_dag_agent_loop_v6)
    i_cov = src.index("await _v6_entity_coverage(_goal_ents_task")
    i_done = src.index('{"type": "agent_loop_v6.done", "stream_id": stream_id, "session_id": sid,')
    assert i_cov < i_done
    assert "agent_loop_v6.entity_coverage" in inspect.getsource(M)   # forwarded to the UI
