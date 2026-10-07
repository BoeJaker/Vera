"""NLP / media / worker calls reach the Estate Activity pane too.

The Ollama taps cover LLM traffic; the node's own Python services carry a
pure-ASGI recorder instead of another proxy, and node-worker tasks record
themselves - all into the same stream, vera:node_activity."""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))

import activity_record as ar  # noqa: E402
import Vera.vera.capability_orchestration as CO  # noqa: E402

pytestmark = pytest.mark.critical
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

AUDIO = "QUJD" * 400                                  # 1600 base64 chars


def _app(monkeypatch):
    monkeypatch.setenv("VERA_ACTIVITY_REDIS_URL", "")
    monkeypatch.setattr(ar, "REDIS_ENV_FILE", "/nonexistent")
    app = fastapi.FastAPI()

    @app.post("/tts")
    async def tts(body: dict):
        return {"audio_b64": AUDIO, "sample_rate": 24000, "voice": body.get("voice")}

    @app.post("/embed")
    async def embed(body: dict):
        return {"embeddings": [[0.1] * 384]}

    @app.post("/other")
    async def other(body: dict):
        return {"ok": True}

    rec = ar.install(app, "media", ("/tts", "/embed"), port=8765)
    return app, rec


def test_calls_pass_through_unchanged_and_are_recorded(monkeypatch):
    app, rec = _app(monkeypatch)
    c = TestClient(app)
    r = c.post("/tts", json={"text": "hello world", "voice": "af_heart"},
               headers={"X-Vera-Origin": "prod|tts|r1|tts.synthesize"})
    assert r.json() == {"audio_b64": AUDIO, "sample_rate": 24000, "voice": "af_heart"}
    c.post("/embed", json={"texts": ["a"]})
    c.post("/other", json={})                        # not a captured path
    recs = list(rec.ring)
    assert [x["path"] for x in recs] == ["/tts", "/embed"]
    t = recs[0]
    assert t["service"] == "media" and t["kind"] == "tts" and t["status"] == 200
    assert t["origin"] == "prod|tts|r1|tts.synthesize"
    assert json.loads(t["prompt"]) == {"text": "hello world", "voice": "af_heart"}
    # payloads recorded as sizes, not bytes
    assert json.loads(t["response"])["audio_b64"] == "[base64 1600 chars]"
    assert json.loads(recs[1]["response"])["embeddings"] == ["[vector 384]"]
    assert t["response_bytes"] > 1600 and rec.inflight == {}


def test_multipart_uploads_are_sized_not_stored(monkeypatch):
    app, rec = _app(monkeypatch)
    app2 = fastapi.FastAPI()

    @app2.post("/stt")
    async def stt():
        return {"text": "transcribed"}

    rec2 = ar.install(app2, "media", ("/stt",))
    TestClient(app2).post("/stt", files={"file": ("a.webm", b"\x00\x01" * 5000, "audio/webm")})
    assert list(rec2.ring)[0]["prompt"].startswith("[multipart upload ")


def test_worker_tasks_are_recorded_with_the_caps_redaction(monkeypatch):
    added = []

    class FakeRedis:
        async def xadd(self, stream, fields, **kw):
            added.append((stream, json.loads(fields["r"])))

    monkeypatch.setattr(CO, "REDIS", FakeRedis())
    cap = {"redact_args": ["data"], "redact_result": True}
    asyncio.run(CO._worker_activity("worker-x", "secstore.kv.put", cap,
                                    {"path": "p", "data": {"pw": "s3cret"}}, "t1", "",
                                    {"t0": 0.0, "result": {"pw": "s3cret"}, "error": ""}))
    stream, r = added[0]
    assert stream == "vera:node_activity" and r["service"] == "worker"
    assert r["path"] == "secstore.kv.put" and "s3cret" not in json.dumps(r)
    assert json.loads(r["prompt"])["data"] == "[redacted]" and r["response"] == '"[redacted]"'
