"""Operator loop control: stop conditions, dry-run, blocking (mocked phases)."""

import asyncio

from vera.operator import operator_loop as L
from vera.operator import safety as S


class _Obs:
    def __init__(self, url="", shot=""):
        self.url = url
        self.screenshot_path = shot


class _Session:
    def __init__(self, url="http://localhost:8998"):
        self.session_id = "t"
        self.history = []
        self.page = None
        self.ref_map = {}
        self.base_url = url
        self.target = {}
        self.meta = {}


def _run(**kw):
    return asyncio.run(L.run_loop("do it", _Session(), **kw))


def _observe(url="http://localhost:8998"):
    async def obs(session, i):
        return _Obs(url=url, shot=f"s{i}.png")
    return obs


def _script(decisions):
    seq = list(decisions)

    async def think(goal, observation, history, canvas):
        return seq.pop(0) if seq else {"action": "done", "done": True}
    return think


def test_reaches_done():
    async def act(session, action, args):
        return {"ok": True}
    out = _run(observe_fn=_observe(), act_fn=act,
               think_fn=_script([
                   {"action": "click", "args": {"ref": "e1"}, "done": False},
                   {"action": "done", "done": True, "args": {"summary": "yep"}},
               ]))
    assert out["done"] is True
    assert out["reason"] == "done"
    assert out["summary"] == "yep"


def test_respects_max_steps():
    async def act(session, action, args):
        return {"ok": True}
    out = _run(observe_fn=_observe(), act_fn=act, max_steps=3,
               think_fn=_script([{"action": "click", "args": {"ref": "e1"}}] * 10))
    assert out["reason"] == "max_steps"
    assert out["step_count"] == 3


def test_dry_run_does_not_act():
    called = {"n": 0}

    async def act(session, action, args):
        called["n"] += 1
        return {"ok": True}
    out = _run(observe_fn=_observe(url=""), act_fn=act,
               policy=S.SafetyPolicy(dry_run=True),
               think_fn=_script([{"action": "click", "args": {"ref": "e1"}},
                                 {"action": "done", "done": True}]))
    assert called["n"] == 0
    assert any(s.get("result", {}).get("dry_run") for s in out["steps"])


def test_blocked_on_external_host():
    async def act(session, action, args):
        return {"ok": True}
    out = _run(observe_fn=_observe(url="https://example.com"), act_fn=act,
               policy=S.SafetyPolicy(),  # no allowlist, external → blocked
               think_fn=_script([{"action": "click", "args": {"ref": "e1"}}]))
    assert out["reason"] == "blocked"


def test_think_error_stops():
    async def act(session, action, args):
        return {"ok": True}
    out = _run(observe_fn=_observe(), act_fn=act,
               think_fn=_script([{"error": "model down"}]))
    assert out["reason"] == "think_error"
