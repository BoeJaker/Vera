"""Chat insights: the long-horizon model's optional second look at a finished
chat reply (user, 2026-09-27).

Pins what matters about it: it runs on the long-horizon CPU route (never the
GPU the reply came from), it never runs two at a time, a newer turn supersedes
one still waiting, an empty or unreadable answer shows nothing, and what it
finds reaches the chat as a card (`__chat_insight__`), not as a message.
"""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.agents import chat_insights_core as C  # noqa: E402

try:
    from Vera.vera.agents import agents as A
    from Vera.vera import capability_orchestration as O
except Exception:                                    # pragma: no cover
    A = O = None

needs_app = pytest.mark.skipif(A is None, reason="app module not importable here")

REPLY = "Redis switched to a dual licence in March 2024. " * 8


# ── the pure core ────────────────────────────────────────────────────────────

def test_parse_keeps_three_clipped_deduplicated_lists():
    raw = ('{"insights":["a","A","b"],"caveats":"one caveat",'
           '"follow_ups":[{"question":"What about Valkey?"}],"other":[1]}')
    out = C.parse(raw)
    assert out == {"insights": ["a", "b"], "caveats": ["one caveat"],
                   "follow_ups": ["What about Valkey?"]}


def test_parse_reads_past_reasoning_and_fences_and_fails_empty():
    raw = '<think>maybe {"x":1}</think>\n```json\n{"insights":["real"]}\n```'
    assert C.parse(raw)["insights"] == ["real"]
    assert C.is_empty(C.parse("not json at all"))
    assert C.is_empty(C.parse('{"insights":[],"caveats":[],"follow_ups":[]}'))


def test_parse_bounds_items():
    out = C.parse('{"insights":[%s]}' % ",".join('"%d %s"' % (i, "x" * 400) for i in range(9)))
    assert len(out["insights"]) == C.MAX_ITEMS
    assert all(len(i) <= C.MAX_ITEM_CHARS for i in out["insights"])


def test_wanted_only_for_a_real_reply_when_enabled():
    assert C.wanted(True, REPLY)
    assert not C.wanted(False, REPLY)
    assert not C.wanted(True, "ok, done")                 # an acknowledgement
    assert not C.wanted(True, REPLY, use_tts=True)        # a spoken turn


def test_prompt_carries_the_exchange_and_bounded_history():
    hist = [{"role": "user", "content": "q%d" % i} for i in range(10)]
    p = C.build_prompt("why?", REPLY, hist)
    assert "# The user asked\nwhy?" in p and "# The assistant replied" in p
    assert "q9" in p and "q5" not in p                    # last HISTORY_TURNS only


# ── the route and the runner ─────────────────────────────────────────────────

@needs_app
def test_insights_run_the_long_horizon_route_never_the_gpu():
    rule = O.DEFAULT_ROUTING_RULES[C.JOB_TYPE]
    assert C.JOB_TYPE in O.OLLAMA_JOB_TYPES
    assert rule["deny_gpu"] and rule["prefer"] == "cpu-247"
    assert rule["model"] == O.LONG_HORIZON_CPU_MODEL
    assert rule["options"] == O.LONG_HORIZON_CPU_OPTIONS    # the shared runner


def _wire(monkeypatch, answers, delay=0.0):
    seen = {"gen": [], "dispatch": [], "in_flight": 0, "max": 0}

    async def fake_gen(prompt, **kw):
        seen["gen"].append(kw)
        seen["in_flight"] += 1
        seen["max"] = max(seen["max"], seen["in_flight"])
        try:
            await asyncio.sleep(delay)
            if kw.get("meta_out") is not None:
                kw["meta_out"].update({"model": "qwen3.6:35b-a3b", "instance": "cpu-247"})
            return answers.pop(0) if answers else ""
        finally:
            seen["in_flight"] -= 1

    async def fake_dispatch(**kw):
        seen["dispatch"].append(kw)
        return {"ok": True}

    async def fake_emit(ev):
        return None

    monkeypatch.setattr(A, "_INSIGHT_SLOT", asyncio.Semaphore(1))   # this test's loop
    monkeypatch.setattr(A, "_INSIGHT_LATEST", {})
    monkeypatch.setattr(A, "ollama_generate", fake_gen)
    monkeypatch.setattr(A, "emit_event", fake_emit)
    monkeypatch.setitem(A.CAPABILITY_REGISTRY, "panel.dispatch", {"raw": fake_dispatch})
    return seen


@needs_app
def test_a_finding_reaches_the_chat_as_a_card(monkeypatch):
    seen = _wire(monkeypatch, ['{"insights":["x"],"follow_ups":["y?"]}'])
    asyncio.run(A._chat_insights("s1", "t1", "why?", REPLY, []))
    kw = seen["gen"][0]
    assert kw["job_type"] == C.JOB_TYPE and kw["prefer_gpu"] is False and kw["think"] is False
    (d,) = seen["dispatch"]
    assert d["session_id"] == "s1" and d["action"] == "__chat_insight__"
    assert d["payload"]["insights"] == ["x"] and d["payload"]["follow_ups"] == ["y?"]
    assert d["payload"]["model"] == "qwen3.6:35b-a3b" and d["payload"]["node"] == "cpu-247"


@needs_app
def test_nothing_to_add_shows_nothing(monkeypatch):
    seen = _wire(monkeypatch, ['{"insights":[],"caveats":[],"follow_ups":[]}'])
    asyncio.run(A._chat_insights("s1", "t1", "why?", REPLY, []))
    assert seen["gen"] and not seen["dispatch"]


@needs_app
def test_one_at_a_time_and_a_newer_turn_supersedes_a_waiting_one(monkeypatch):
    seen = _wire(monkeypatch, ['{"insights":["a"]}', '{"insights":["b"]}', '{"insights":["c"]}'],
                 delay=0.05)

    async def go():
        # s1's first turn holds the slot; its second turn waits; its third
        # arrives while the second is still waiting and replaces it.
        t1 = asyncio.ensure_future(A._chat_insights("s1", "t1", "q", REPLY, []))
        await asyncio.sleep(0.01)
        t2 = asyncio.ensure_future(A._chat_insights("s1", "t2", "q", REPLY, []))
        await asyncio.sleep(0.005)
        t3 = asyncio.ensure_future(A._chat_insights("s1", "t3", "q", REPLY, []))
        await asyncio.gather(t1, t2, t3)

    asyncio.run(go())
    assert seen["max"] == 1
    turns = [d["payload"]["turn"] for d in seen["dispatch"]]
    assert turns == ["t1", "t3"]
