import asyncio
import json

import pytest

from vera import capability_orchestration as orchestration


pytestmark = pytest.mark.critical


class _Redis:
    def __init__(self):
        self.added = []
        self.published = []

    async def xadd(self, name, fields, maxlen=None):
        self.added.append((name, fields, maxlen))
        return b"1-0"

    async def publish(self, name, value):
        self.published.append((name, value))


def test_emit_event_mirrors_code_author_timing_to_dedicated_history(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(orchestration, "REDIS", redis)

    async def no_persist(event, payload):
        return None

    monkeypatch.setattr(orchestration, "_persist_loop_event", no_persist)
    event = {"type": "code.author.timing", "timing": {
        "schema": "vera.code-author-timing/v1", "total_ms": 123}}
    asyncio.run(orchestration.emit_event(event))

    names = [entry[0] for entry in redis.added]
    assert names == [orchestration.EVENT_STREAM, "vera:stream:code.author.timing"]
    mirrored = json.loads(redis.added[1][1]["data"])
    assert mirrored["timing"]["total_ms"] == 123


def test_emit_event_does_not_mirror_unrelated_activity(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(orchestration, "REDIS", redis)

    async def no_persist(event, payload):
        return None

    monkeypatch.setattr(orchestration, "_persist_loop_event", no_persist)
    asyncio.run(orchestration.emit_event({"type": "cap.call"}))
    assert [entry[0] for entry in redis.added] == [orchestration.EVENT_STREAM]
