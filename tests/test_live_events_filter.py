"""DA-03a: a /ws/mcp event subscriber may ask for only its events - {sid, trace, types} - and the emit_event fan-out
checks the filter per socket before enqueueing; a subscriber with no filter gets every event, as before."""
import asyncio

import pytest

from vera.live_events_core import event_matches, filter_summary, normalize_event_filter, MAX_TRACES
from vera import capability_orchestration as orch

pytestmark = pytest.mark.critical


def test_no_filter_or_an_empty_one_is_everything():
    assert normalize_event_filter(None) is None
    assert normalize_event_filter({}) is None
    assert normalize_event_filter({"sid": "", "trace": [], "types": []}) is None
    assert normalize_event_filter("chat-1") is None
    assert event_matches({"type": "x"}, None) is True


def test_each_given_part_must_match():
    f = normalize_event_filter({"sid": "chat-1", "trace": ["t-1", "t-2"], "types": ["info.seek.", "cap."]})
    assert event_matches({"type": "info.seek.step", "sid": "chat-1", "trace_id": "t-2"}, f)
    assert not event_matches({"type": "info.seek.step", "sid": "chat-2", "trace_id": "t-2"}, f)      # another session
    assert not event_matches({"type": "info.seek.step", "sid": "chat-1", "trace_id": "t-9"}, f)      # another call
    assert not event_matches({"type": "dream.tick", "sid": "chat-1", "trace_id": "t-1"}, f)          # another type
    assert event_matches({"type": "cap.ok", "session_id": "chat-1", "trace_id": "t-1"}, f)           # session_id too


def test_parts_alone():
    assert event_matches({"type": "a.b", "sid": "s"}, normalize_event_filter({"types": "a."}))
    assert not event_matches({"type": "b.a"}, normalize_event_filter({"types": "a."}))
    assert event_matches({"type": "z", "trace_id": "t"}, normalize_event_filter({"trace": "t"}))
    assert not event_matches({"type": "z"}, normalize_event_filter({"trace": ["t"]}))
    assert event_matches({"type": "z", "sid": "s"}, normalize_event_filter({"sid": "s"}))


def test_the_filter_is_bounded_and_said_back():
    f = normalize_event_filter({"trace": ["t%d" % i for i in range(MAX_TRACES + 50)], "types": ["a.", "a.", "b."]})
    assert len(f["trace"]) == MAX_TRACES and f["types"] == ("a.", "b.")
    s = filter_summary(normalize_event_filter({"sid": "s", "trace": ["b", "a"], "types": ["x."]}))
    assert s == {"sid": "s", "trace": ["a", "b"], "types": ["x."]}
    assert filter_summary(None) == {}


def test_the_fan_out_sends_a_filtered_subscriber_only_its_events(monkeypatch):
    sent = []
    a, b = object(), object()
    monkeypatch.setattr(orch, "WS_CONNECTIONS", [(a, "__events__"), (b, "__events__"), (b, "__events__")])
    monkeypatch.setattr(orch, "WS_EVENT_FILTERS", {id(b): normalize_event_filter({"sid": "chat-1", "types": ["info.seek."]})})
    monkeypatch.setattr(orch, "REDIS", None)
    monkeypatch.setattr(orch, "_ws_enqueue", lambda ws, payload: sent.append((ws, payload["data"]["type"])))
    run = asyncio.new_event_loop().run_until_complete
    run(orch.emit_event({"type": "info.seek.step", "sid": "chat-1", "trace_id": "t-1"}))
    run(orch.emit_event({"type": "info.seek.step", "sid": "chat-2", "trace_id": "t-2"}))
    run(orch.emit_event({"type": "dream.tick", "sid": "chat-1"}))
    to_a = [t for ws, t in sent if ws is a]
    to_b = [t for ws, t in sent if ws is b]
    assert to_a == ["info.seek.step", "info.seek.step", "dream.tick"]       # no filter: every event, as before
    assert to_b == ["info.seek.step"]                                       # its session's lookups only, once


def test_the_ws_handler_sets_replaces_and_drops_the_filter():
    src = open(orch.__file__, encoding="utf-8").read()
    assert '_flt = normalize_event_filter(msg.get("filter"))' in src
    assert "if _flt: WS_EVENT_FILTERS[id(ws)] = _flt" in src and "else: WS_EVENT_FILTERS.pop(id(ws), None)" in src
    assert 'if (ws,"__events__") not in WS_CONNECTIONS:' in src                 # subscribing again replaces, not doubles
    assert 'if msg.get("stream")=="__events__": WS_EVENT_FILTERS.pop(id(ws), None)' in src
    assert "WS_CONNECTIONS[:]=[p for p in WS_CONNECTIONS if p[0] is not ws]\n        WS_EVENT_FILTERS.pop(id(ws), None)" in src


def test_obs_events_since_returns_only_what_came_after(monkeypatch):
    import json as _j

    class FakeRedis:
        async def xrevrange(self, stream, count=100):
            evs = [{"type": "c", "ts": "2026-10-12T10:00:03"}, {"type": "b", "ts": "2026-10-12T10:00:02"}, {"type": "a", "ts": "2026-10-12T10:00:01"}]
            return [(b"id", {b"data": _j.dumps(e).encode()}) for e in evs][:count]

    monkeypatch.setattr(orch, "REDIS", FakeRedis())
    run = asyncio.new_event_loop().run_until_complete
    fn = getattr(orch.obs_events, "__wrapped__", orch.obs_events)
    assert [e["type"] for e in run(fn(limit=10))] == ["c", "b", "a"]
    assert [e["type"] for e in run(fn(limit=10, since="2026-10-12T10:00:01"))] == ["c", "b"]


def test_the_live_client_is_served():
    import os
    libs = open(os.path.join(os.path.dirname(__file__), "..", "vera", "ui", "libs.py"), encoding="utf-8").read()
    assert '@APP.get("/ui/live.js", include_in_schema=False)' in libs and 'return _script("live.js")' in libs


def test_alternatives_are_a_union():
    f = normalize_event_filter({"any": [{"trace": ["t-1"]}, {"types": ["fabric."]}, {"sid": "s", "types": ["memory.feedback"]}]})
    assert event_matches({"type": "cap.ok", "trace_id": "t-1"}, f)
    assert event_matches({"type": "fabric.loom.progress"}, f)
    assert event_matches({"type": "memory.feedback", "session_id": "s"}, f)
    assert not event_matches({"type": "memory.feedback", "session_id": "other"}, f)
    assert not event_matches({"type": "dream.tick", "trace_id": "t-9"}, f)
    assert normalize_event_filter({"any": [{"trace": ["t"]}, {}]}) is None          # one alternative is everything
    assert normalize_event_filter({"any": []}) is None
    assert filter_summary(normalize_event_filter({"any": [{"types": ["a."]}]})) == {"any": [{"sid": "", "trace": [], "types": ["a."]}]}
