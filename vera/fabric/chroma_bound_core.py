"""Chroma calls must never wedge the process's shared thread pool (RF-77).

chromadb 1.0.15's HttpClient builds its httpx session with `timeout=None`. Every call
site runs the client in a thread (it is synchronous), and the callers' own timeouts -
memory's 10 s "Memory backend chroma store timed out" - only abandon the AWAIT: the
thread stays parked on the socket until Chroma answers. 4 Oct 2026 03:24 (census stall
dump) all 16 default-executor threads sat in chromadb `_make_request` and 57 unrelated
`nodes.list` calls queued behind them in `asyncio.to_thread` - anything in the process
that needed a thread waited on Chroma.

Two guards:
  bound(client)  - give the client's httpx session a finite timeout, so a stuck call
                   frees its thread. chromadb caches one server object per settings, so
                   every client for the same host/port shares the bound.
  run(fn, ...)   - run a Chroma call on a small pool of its own ("vera-chroma"), so a slow
                   Chroma can fill only that pool, never the default one.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


READ_S = _env_float("CHROMA_HTTP_TIMEOUT_S", 60.0)
CONNECT_S = 10.0
THREADS = int(_env_float("CHROMA_THREADS", 4))

_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def bound(client: Any, read_s: Optional[float] = None, connect_s: Optional[float] = None) -> bool:
    """Give a chromadb HttpClient's httpx session a finite timeout. Never raises;
    False when the client has no such session (another chromadb version, a stub)."""
    try:
        import httpx
        sess = getattr(getattr(client, "_server", None), "_session", None)
        if sess is None or not hasattr(sess, "timeout"):
            return False
        sess.timeout = httpx.Timeout(float(read_s or READ_S), connect=float(connect_s or CONNECT_S))
        return True
    except Exception:
        return False


def pool() -> ThreadPoolExecutor:
    """The Chroma-only thread pool (created on first use)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=max(1, THREADS), thread_name_prefix="vera-chroma")
    return _pool


async def run(fn, *args, **kwargs):
    """asyncio.to_thread, but on the Chroma pool (context vars carried the same way)."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(pool(), functools.partial(ctx.run, fn, *args, **kwargs))
