"""Operator decisions think with the reasoning pass OFF unless asked (user, 2026-10-03,
after a prod A/B: thinking on was slower and left 2 of 4 checks unanswered)."""

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.operator import thinker as T  # noqa: E402

GOOD = {"text": '{"thought": "t", "action": "wait", "args": {"ms": 500}, "done": false}'}


class _Obs:
    url = "data:text/html,x"
    title = "t"

    def compact(self, max_elements=60):
        return "TEXT: hi"


def _calls(**kw):
    seen = []

    async def cap(name, **a):
        seen.append(a.get("think", "unset"))
        return GOOD
    asyncio.new_event_loop().run_until_complete(T.decide("g", _Obs(), [], cap, **kw))
    return seen


def test_default_is_off_and_explicit_values_win(monkeypatch):
    monkeypatch.delenv("VERA_OPERATOR_THINK", raising=False)
    assert T.default_think() is False
    assert _calls() == [False]
    assert _calls(think=True) == [True]
    assert _calls(think=False) == [False]


def test_env_override(monkeypatch):
    monkeypatch.setenv("VERA_OPERATOR_THINK", "on")
    assert T.default_think() is True and _calls() == [True]
    monkeypatch.setenv("VERA_OPERATOR_THINK", "model")
    assert T.default_think() is None and _calls() == [None]
