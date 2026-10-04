"""4 Oct 2026: GPU calls sat the full 900 s on gpu-250 at 0% while Ollama thought a runner
was busy, and requests Vera had abandoned stayed queued in Ollama behind the tap.

1. ollama_generate gives a GPU node OLLAMA_GPU_FIRST_BYTE_S (300 s) to start, then falls
   back; CPU nodes keep the full timeout (their prompt pass is genuinely slow).
2. The tap cancels its upstream request when the client leaves before Ollama answered."""
import asyncio
import json
import os
import sys
import time
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
_spec = importlib.util.spec_from_file_location("owc", ROOT / "vera" / "ollama_wait_core.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)
CO = (ROOT / "vera" / "capability_orchestration.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical


# ── 1. the GPU first-byte cap ─────────────────────────────────────────────────

def test_gpu_gets_the_cap_cpu_keeps_the_full_timeout():
    assert W.read_timeout(900, True, 300) == 300
    assert W.read_timeout(900, False, 300) == 900
    assert W.read_timeout(120, True, 300) == 120          # a shorter caller timeout wins
    assert W.read_timeout(900, True, 0) == 900            # 0 disables the cap
    assert W.read_timeout("x", True, 300) == 300 and W.read_timeout(900, True, "x") == 900


def test_ollama_generate_uses_it():
    assert 'OLLAMA_GPU_FIRST_BYTE_S = float(os.environ.get("OLLAMA_GPU_FIRST_BYTE_S", "300"))' in CO
    i = CO.index("_gto = httpx.Timeout(_ollama_wait_core.read_timeout(")
    assert '(OLLAMA_INSTANCES.get(chosen) or {}).get("has_gpu")' in CO[i:i + 300]
    assert "_gto = httpx.Timeout(gen_timeout, connect=15.0)" not in CO


# ── 2. the tap drops an abandoned request ─────────────────────────────────────

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
import ollama_tap as tap  # noqa: E402


async def _abandon(tmp_path, client_wait=1.0, upstream_hold=8.0):
    seen = {"closed_at": None, "answered": False}

    async def upstream_generate(request):
        # Ollama "queued": no headers yet. Watch our own connection while we hold.
        t0 = time.monotonic()
        while time.monotonic() - t0 < upstream_hold:
            tr = request.transport
            if tr is None or tr.is_closing():
                seen["closed_at"] = time.monotonic() - t0
                return web.Response(status=499)
            await asyncio.sleep(0.05)
        seen["answered"] = True
        return web.json_response({"model": "m", "response": "late", "done": True})

    up = web.Application()
    up.router.add_post("/api/generate", upstream_generate)
    up_runner = web.AppRunner(up)
    await up_runner.setup()
    up_site = web.TCPSite(up_runner, "127.0.0.1", 0)
    await up_site.start()
    up_port = up_site._server.sockets[0].getsockname()[1]

    t = tap.Tap(f"http://127.0.0.1:{up_port}", "node-x", 11435, redis_url="", log_dir=str(tmp_path))
    await t.start()
    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{tail:.*}", t.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    body = json.dumps({"model": "m", "prompt": "x"}).encode()
    gave_up = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=client_wait)) as s:
        try:
            async with s.post(f"http://127.0.0.1:{port}/api/generate", data=body) as r:
                await r.read()
        except asyncio.TimeoutError:
            gave_up = True
    await asyncio.sleep(client_wait + 3 * tap.CLIENT_CHECK_S)
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/vera-tap/inflight") as r:
            inflight = await r.json()
        async with s.get(f"http://127.0.0.1:{port}/vera-tap/recent") as r:
            recent = await r.json()
    await runner.cleanup()
    await up_runner.cleanup()
    await t.session.close()
    return gave_up, seen, inflight, recent


def test_a_client_that_leaves_cancels_the_queued_upstream(tmp_path):
    gave_up, seen, inflight, recent = asyncio.run(_abandon(tmp_path))
    assert gave_up
    assert seen["closed_at"] is not None, "the tap kept the abandoned request open upstream"
    assert seen["closed_at"] < 1.0 + 3 * tap.CLIENT_CHECK_S
    assert seen["answered"] is False
    assert inflight == []
    assert recent and recent[-1].get("error") == "client went away"


def test_client_gone_reads_the_transport():
    class Tr:
        def __init__(self, c): self.c = c
        def is_closing(self): return self.c
    class Rq:
        def __init__(self, tr): self.transport = tr
    assert tap.client_gone(Rq(None)) is True
    assert tap.client_gone(Rq(Tr(True))) is True
    assert tap.client_gone(Rq(Tr(False))) is False
