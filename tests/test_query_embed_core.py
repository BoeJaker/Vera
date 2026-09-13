"""bounded_embed: a query embed is waited for, never thrown away.

Imports lowercase `vera.dag.query_embed_core` with the repo root on sys.path so
the WORKTREE copy is exercised.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.dag import query_embed_core as qec  # noqa: E402

pytestmark = pytest.mark.critical


def test_a_vector_that_arrives_in_time_is_returned():
    async def embed():
        await asyncio.sleep(0.01)
        return [0.1, 0.2]

    assert asyncio.run(qec.bounded_embed(embed, 1.0)) == [0.1, 0.2]


def test_a_slow_embed_gives_up_waiting_but_is_not_cancelled():
    """The point of the module: the caller moves on, the work finishes anyway."""
    done = []

    async def embed():
        await asyncio.sleep(0.2)
        done.append(True)
        return [1.0]

    async def scenario():
        got = await qec.bounded_embed(embed, 0.05)
        assert got is None
        await asyncio.sleep(0.35)        # outlive the embed
        return done

    assert asyncio.run(scenario()) == [True]


def test_bounded_embed_result_tells_a_timeout_from_a_failure():
    """memory.embed_text / data_fabric._embed keep two breakers — a short
    slow-cooldown for a backed-up node, the long one for an unreachable node —
    so they need to know WHICH kind of miss this was."""
    async def slow():
        await asyncio.sleep(0.2)
        return [1.0]

    async def broken():
        raise RuntimeError("embed node unreachable")

    async def scenario():
        return (await qec.bounded_embed_result(slow, 0.05),
                await qec.bounded_embed_result(broken, 1.0))

    assert asyncio.run(scenario()) == ((None, True), (None, False))


def test_memory_and_fabric_embeds_wait_bounded_and_do_not_cancel():
    """The chat page's memory chip waited 10-135 s per embed behind fabric-
    ingest traffic on 2026-09-13; both embed entry points now go through
    bounded_embed_result and neither cuts the embed with wait_for."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel, fn in (("vera/fabric/memory.py", "embed_text"),
                    ("vera/fabric/data_fabric.py", "_embed")):
        src = open(os.path.join(root, *rel.split("/")), encoding="utf-8").read()
        body = src.split(f"async def {fn}(")[1]
        body = body[:min(i for i in (body.find("\nasync def "), body.find("\nclass "), body.find("\ndef ")) if i > 0)]
        assert "bounded_embed_result(" in body, f"{rel}:{fn} must use bounded_embed_result"
        assert "asyncio.wait_for(" not in body, f"{rel}:{fn} must not cancel the embed"
        assert "_EMBED_WAIT_S" in body and "SLOW" in body.upper(), f"{rel}:{fn} wait budget + slow-cooldown"


def test_a_failing_embed_is_a_miss_not_an_error():
    async def embed():
        raise RuntimeError("embed node unreachable")

    assert asyncio.run(qec.bounded_embed(embed, 1.0)) is None


def test_no_vector_is_a_miss():
    async def embed():
        return None

    assert asyncio.run(qec.bounded_embed(embed, 1.0)) is None


def test_dag_store_query_embeds_wait_without_cancelling():
    """dag_store's two search paths go through bounded_embed, and nothing in it
    passes its own short timeout to ollama_embed any more."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "vera", "dag", "dag_store.py"), encoding="utf-8").read()
    assert src.count("_query_embed.bounded_embed(") == 2
    assert "timeout=10)" not in src
    assert "timeout=15," not in src


def test_memory_seek_waits_for_its_query_vector_without_cancelling_it():
    """memory_retrieval cut the seek embed at 10 s with wait_for, which cancelled
    it: cpu-246 takes ~9.6 s per embed, so the System Comms feed's 45 s poll of
    memory.seek("daily brief report digest") failed 136 of 167 times in three
    hours and re-sent the same text every time (2026-09-11)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "vera", "fabric", "memory_retrieval.py"), encoding="utf-8").read()
    assert "asyncio.wait_for(df._embed(query), timeout=10)" not in src
    assert "_bounded_embed(lambda: df._embed(query), _SEEK_EMBED_WAIT_S)" in src


def test_syscomms_panel_polls_only_while_on_screen():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "vera", "syscomms", "syscomms_panel.html"), encoding="utf-8").read()
    assert "setInterval(load, 45000);" not in src, "an unguarded 45 s tick remains"
    assert "setInterval(() => { if (_onScreen()) load(); }, 45000);" in src
    assert "document.body.offsetWidth > 0" in src
