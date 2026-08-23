"""_StreamLines — polling a token stream must never destroy it.

Guards the 2026-08-23 incident: `await asyncio.wait_for(it.__anext__(), 5.0)`
CANCELS the pending `__anext__()` on timeout, which closes the underlying async
generator; every later call then raises StopAsyncIteration, so the read loop
exits "cleanly", the `done` frame is never seen, and the caller keeps only the
tokens that had already arrived.

On a GPU node tokens land inside the 5s poll so it never tripped. On a slow CPU
node the FIRST token can take minutes — so the first poll killed the stream and
EVERY CPU generation returned exactly one token (eval_count=1). That is what
made the system narrator emit one-word "narratives" and why the deep MoE
narrative never completed.

These tests import the pure helper via the lowercase `vera.` path with the
worktree on sys.path — `Vera.vera.X` would resolve to the MAIN checkout (Vera is
a namespace package), silently testing the OLD code.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stream_lines_cls():
    """Load _StreamLines without importing the whole orchestrator module."""
    import ast
    from typing import Optional

    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "vera", "capability_orchestration.py")
    with open(src_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_StreamLines":
            ns = {"asyncio": asyncio, "Optional": Optional}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         src_path, "exec"), ns)
            return ns["_StreamLines"]
    raise AssertionError("_StreamLines not found in capability_orchestration.py")


async def _slow_stream(first_delay: float, gap: float, n: int):
    """A stream whose first token is slow (cold prompt-eval) then steady."""
    for i in range(n):
        await asyncio.sleep(first_delay if i == 0 else gap)
        yield f"tok{i}"


@pytest.mark.critical
def test_slow_first_token_does_not_truncate_stream():
    """THE regression: a first token slower than the poll must not end the read.

    Reproduces the shape of a CPU-node generation — poll interval well under the
    time to the first token — and asserts every token still arrives.
    """
    cls = _stream_lines_cls()

    async def run():
        lines = cls(_slow_stream(first_delay=0.30, gap=0.12, n=5))
        got, timeouts = [], 0
        while True:
            try:
                got.append(await lines.next(timeout=0.05))
            except asyncio.TimeoutError:
                timeouts += 1
                if timeouts > 500:               # runaway guard, not the assertion
                    break
                continue
            except StopAsyncIteration:
                break
        return got, timeouts

    got, timeouts = asyncio.run(run())
    assert got == [f"tok{i}" for i in range(5)], (
        "stream truncated after a slow token — the destructive wait_for "
        f"cancellation is back (got {got!r})")
    assert timeouts > 0, "test did not exercise the timeout path at all"


@pytest.mark.critical
def test_naive_wait_for_would_truncate():
    """Pin the BROKEN behaviour, so the test above is proven to be load-bearing.

    If a future asyncio makes `wait_for` cancellation non-destructive this fails
    loudly rather than leaving a test that silently guards nothing.
    """
    async def run():
        it = _slow_stream(first_delay=0.30, gap=0.12, n=5)
        got = []
        for _ in range(20):
            try:
                got.append(await asyncio.wait_for(it.__anext__(), timeout=0.05))
            except asyncio.TimeoutError:
                continue
            except StopAsyncIteration:
                break
        return got

    assert asyncio.run(run()) == [], (
        "naive wait_for no longer destroys the generator — _StreamLines may no "
        "longer be necessary, re-verify before removing it")


@pytest.mark.critical
def test_timeout_leaves_stream_intact_for_stall_check():
    """A caller must be able to poll repeatedly (its stall timer) and keep reading."""
    cls = _stream_lines_cls()

    async def run():
        lines = cls(_slow_stream(first_delay=0.25, gap=0.0, n=3))
        polls = 0
        while True:                     # burn several timeouts before the first token
            try:
                first = await lines.next(timeout=0.02)
                break
            except asyncio.TimeoutError:
                polls += 1
        rest = []
        while True:
            try:
                rest.append(await lines.next(timeout=0.5))
            except StopAsyncIteration:
                break
        return first, rest, polls

    first, rest, polls = asyncio.run(run())
    assert polls >= 2, "expected multiple idle polls before the first token"
    assert first == "tok0"
    assert rest == ["tok1", "tok2"]


@pytest.mark.critical
def test_aclose_abandons_in_flight_read():
    """Giving up on a stalled stream must not leak the pending task."""
    cls = _stream_lines_cls()

    async def run():
        lines = cls(_slow_stream(first_delay=5.0, gap=0.1, n=3))
        try:
            await lines.next(timeout=0.02)
        except asyncio.TimeoutError:
            pass
        await lines.aclose()
        return lines._pending

    assert asyncio.run(run()) is None
