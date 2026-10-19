"""The crawl must not hold prod's event loop (19 Oct 2026: _fetch_batch in 11 of 15 main-thread samples; the Loop Lab's
readings took 45 s+). One HTTP client per loop, and the page row is built in a worker thread."""
import os

import pytest

pytestmark = pytest.mark.critical

SRC = open(os.path.join(os.path.dirname(__file__), "..", "vera", "fabric", "ingress_capabilities.py"), encoding="utf-8").read()


def test_one_client_per_loop_not_one_per_batch():
    fb = SRC[SRC.index("async def _fetch_batch("):SRC.index("@capability(", SRC.index("async def _fetch_batch("))]
    assert "httpx.AsyncClient(" not in fb and "c = _fetch_client()" in fb
    fc = SRC[SRC.index("def _fetch_client("):SRC.index("async def _fetch_batch(")]
    assert "_FETCH_CLIENT.get(loop)" in fc and "c.is_closed" in fc


def test_the_body_is_decoded_off_the_loop():
    fb = SRC[SRC.index("async def _fetch_batch("):SRC.index("@capability(", SRC.index("async def _fetch_batch("))]
    assert "await asyncio.to_thread(" in fb and "body=r.text" in fb and "body=resp.text" not in fb
