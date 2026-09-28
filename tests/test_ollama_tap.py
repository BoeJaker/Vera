"""The activity tap in front of each node's Ollama (user, 2026-09-28: "prompts +
responses via capture proxy").

The retired ollama_wrapper altered requests (num_predict, stop tokens) and
killed processes; this proxy must do neither. The tap is run for real here,
between a fake streaming Ollama and a client."""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))

import ollama_tap as tap  # noqa: E402
from Vera.vera.provisioning import ollama_tap_core as core  # noqa: E402

pytestmark = pytest.mark.critical
aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

CHUNKS = [{"model": "m", "response": "Hel", "done": False},
          {"model": "m", "response": "lo", "done": False},
          {"model": "m", "response": "", "done": True, "eval_count": 2,
           "eval_duration": 1_000_000_000, "prompt_eval_count": 7, "done_reason": "stop"}]


async def _run(tmp_path):
    seen = {}

    async def upstream_generate(request):
        seen["body"] = await request.read()
        seen["origin"] = request.headers.get("X-Vera-Origin")
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        for c in CHUNKS:
            await resp.write((json.dumps(c) + "\n").encode())
            await asyncio.sleep(0.01)
        await resp.write_eof()
        return resp

    async def upstream_tags(_r):
        return web.json_response({"models": []})

    up = web.Application()
    up.router.add_post("/api/generate", upstream_generate)
    up.router.add_get("/api/tags", upstream_tags)
    up_runner = web.AppRunner(up)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    t = tap.Tap(f"http://127.0.0.1:{up_port}", "node-x", 11435, redis_url="",
                log_dir=str(tmp_path))
    await t.start()
    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{tail:.*}", t.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    body = json.dumps({"model": "m", "prompt": "Say hello", "options": {"num_ctx": 2048}}).encode()
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}/api/generate", data=body,
                          headers={"X-Vera-Origin": "prod|chat|abc"}) as r:
            got = await r.read()
        async with s.get(f"http://127.0.0.1:{port}/api/tags") as r:
            tags = await r.json()
        await asyncio.sleep(0.05)
        async with s.get(f"http://127.0.0.1:{port}/vera-tap/recent") as r:
            recent = await r.json()
        async with s.get(f"http://127.0.0.1:{port}/vera-tap/inflight") as r:
            inflight = await r.json()
        async with s.get(f"http://127.0.0.1:{port}/vera-tap/record/{recent[-1]['id']}") as r:
            full = await r.json()
    await runner.cleanup()
    await up_runner.cleanup()
    await t.session.close()
    return seen, body, got, tags, recent, inflight, full


def test_the_tap_forwards_unchanged_and_records_everything(tmp_path):
    seen, body, got, tags, recent, inflight, full = asyncio.run(_run(tmp_path))
    # byte for byte, both ways
    assert seen["body"] == body
    assert got == b"".join((json.dumps(c) + "\n").encode() for c in CHUNKS)
    assert seen["origin"] == "prod|chat|abc"            # headers reach Ollama
    assert tags == {"models": []}                        # uncaptured paths pass through
    r = recent[-1]
    assert r["kind"] == "generate" and r["model"] == "m" and r["node"] == "node-x"
    assert r["prompt"] == "Say hello" and r["response"] == "Hello"
    assert r["eval_count"] == 2 and r["tps"] == 2.0 and r["prompt_eval_count"] == 7
    assert r["origin"] == "prod|chat|abc" and r["options"] == {"num_ctx": 2048}
    assert r["status"] == 200 and r["duration_s"] >= 0
    assert inflight == []
    assert full["id"] == r["id"]                         # the node file keeps it too
    assert os.path.exists(tmp_path / "tap-11435.jsonl")


def test_response_tee_shapes():
    t = tap.ResponseTee("chat")
    t.feed(b'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: {"choices":[{"delta":{"content":"b"}}],'
           b'"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\ndata: [DONE]\n')
    out = t.finish()
    assert out["text"] == "ab" and out["stats"]["eval_count"] == 2
    t = tap.ResponseTee("embed")
    t.feed(json.dumps({"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}).encode())
    out = t.finish()
    assert out["stats"] == {"vectors": 2, "dims": 3} and out["text"] == ""
    assert tap.prompt_text("embed", {"input": ["x", "y"]}).startswith("[2 inputs]")
    assert tap.prompt_text("chat", {"messages": [{"role": "user", "content": "hi"}]}) == "[user] hi"


def test_cutover_rolls_back_and_never_carries_the_credential():
    s = core.cutover_script("ollama-vera", "cpu-246", 11435)
    assert "OLLAMA_HOST=127.0.0.1:11445" in s                 # Ollama to loopback
    assert "VERA_TAP_LISTEN=0.0.0.0:11435" in s                # tap on the public port
    assert core.ROLLED_BACK in s and core.DONE in s
    assert "rm -f /etc/systemd/system/ollama-vera.service.d/30-vera-tap.conf" in s
    for cmd in (s, core.install_cmd(), core.secret_cmd()):
        assert "redis://" not in cmd and "PASSWORD" not in cmd.upper()
    assert "cat > /etc/vera-tap/redis.env" in core.secret_cmd()   # from stdin
    assert "EnvironmentFile=-/etc/vera-tap/redis.env" in core.tap_unit()
