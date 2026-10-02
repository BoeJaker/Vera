"""A think reply cut off mid-"Thinking Process" is asked again with thinking off
(prod 2026-10-02, operator run b7e3c5a00e: 2 of 5 thinks lost 5.5 min to the cap)."""

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.operator import thinker as T  # noqa: E402

PROSE = {"text": "Thinking Process:\n\n1.  **Analyze the Goal:** The user wants", "truncated": True}
GOOD = {"text": '{"thought": "frozen", "action": "done", "args": {}, "done": true}'}


class _Obs:
    url = "data:text/html,x"
    title = "t"

    def compact(self, max_elements=60):
        return "TEXT: 00:30 Start"


def _caller(replies):
    calls = []

    async def call_cap(name, **kw):
        calls.append(kw.get("think", "unset"))
        return replies[len(calls) - 1]
    return call_cap, calls


def _decide(replies, **kw):
    cap, calls = _caller(replies)
    d = asyncio.new_event_loop().run_until_complete(
        T.decide("confirm it counts down", _Obs(), [], cap, **kw))
    return d, calls


def test_a_truncated_thinking_reply_is_retried_without_thinking():
    d, calls = _decide([PROSE, GOOD])
    assert calls == [None, False]
    assert d["action"] == "done" and d["retried_without_thinking"] is True and not d.get("error")


def test_the_retry_failing_keeps_the_truncation_error():
    d, calls = _decide([PROSE, PROSE])
    assert calls == [None, False]
    assert "cut off at the output limit" in d["error"]


def test_no_retry_when_thinking_was_already_off_or_the_reply_parsed():
    d, calls = _decide([PROSE], think=False)
    assert calls == [False] and "cut off" in d["error"]
    d, calls = _decide([GOOD])
    assert calls == [None] and d["action"] == "done"


def test_an_untruncated_parse_failure_is_not_retried():
    d, calls = _decide([{"text": "no json here at all"}])
    assert calls == [None] and d.get("error")
