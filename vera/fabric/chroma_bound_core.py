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

And one for connecting (RF-82):
  discard_on_failure() - a connect that fails closes the HTTP sessions of every chromadb
                   system it created. A failed chromadb.HttpClient(...) keeps its system in
                   chromadb's registry with an open connection; the memory store's reconnect
                   loop retried every 5 min for ever, and each attempt left one more (a mirror
                   with an incompatible client reached 518 connections and starved prod).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, Optional


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


def _registry() -> Optional[Dict[Any, Any]]:
    """chromadb's process-wide {identifier: System} map, or None if this version has none."""
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
    except Exception:
        try:
            from chromadb.api.client import SharedSystemClient  # older layout
        except Exception:
            return None
    reg = getattr(SharedSystemClient, "_identifier_to_system", None)
    return reg if isinstance(reg, dict) else None


def close_system(system: Any) -> int:
    """Close every httpx session a chromadb System's components hold. Never raises.
    Returns how many sessions were closed."""
    closed = 0
    for comp in list((getattr(system, "_instances", None) or {}).values()):
        sess = getattr(comp, "_session", None)
        if sess is not None and hasattr(sess, "close"):
            try:
                sess.close()
                closed += 1
            except Exception:
                pass
    try:
        system.stop()
    except Exception:
        pass
    return closed


@contextmanager
def discard_on_failure():
    """Wrap a chromadb connect: if it raises, close and unregister every System it added
    (new identifiers, and identifiers whose System object was replaced), then re-raise."""
    reg = _registry()
    before = {k: id(v) for k, v in (reg or {}).items()}
    try:
        yield
    except BaseException:
        if reg is not None:
            for k in list(reg.keys()):
                v = reg.get(k)
                if v is not None and before.get(k) != id(v):
                    close_system(v)
                    reg.pop(k, None)
        raise


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


#: words a chromadb / httpx error carries when the CLIENT is unusable (not when the query is wrong)
_DEAD_MARKERS = ("client has been closed", "cannot send a request", "connection refused", "connecterror",
                 "remoteprotocolerror", "server disconnected", "connection reset", "broken pipe",
                 "readtimeout", "connecttimeout", "pooltimeout")


def looks_dead(exc: BaseException) -> bool:
    """True when an exception says the Chroma client cannot talk to the server (a reconnect may help), False for a
    query the server refused (a wrong dimension, a bad filter) - reconnecting would not change that."""
    text = (type(exc).__name__ + ": " + str(exc)).lower()
    return any(m in text for m in _DEAD_MARKERS)


class Throttle:
    """At most one True per `every_s` per key: a failing hot path logs once, not once per call."""

    def __init__(self, every_s: float = 300.0, clock=None):
        import time as _t
        self.every_s = float(every_s)
        self._clock = clock or _t.monotonic
        self._last: Dict[str, float] = {}

    def due(self, key: str) -> bool:
        now = self._clock()
        if now - self._last.get(key, -1e18) >= self.every_s:
            self._last[key] = now
            return True
        return False
