"""The host relays the events and streams a NODE WORKER emits to its own UI.

A capability that runs on a node emits to Redis like any process, but only the
host has WebSocket clients, and it fanned out only its own events - so before
the relay, an event from a node reached no live view and the parity audit had
to keep every event-emitting capability on the host (172 of them, 2026-10-04).

Checked on the source tree (importing the orchestrator pulls in everything).
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "capability_orchestration.py")


def _tree():
    return ast.parse(open(SRC, encoding="utf-8").read())


def _fn(tree, name):
    return next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def test_a_worker_tags_what_it_emits():
    src = open(SRC, encoding="utf-8").read()
    t = _tree()
    for name in ("emit_event", "emit_stream"):
        body = ast.get_source_segment(src, _fn(t, name))
        assert "_IS_WORKER" in body and "_from_worker" in body, name


def test_the_host_relays_only_tagged_messages_to_its_clients():
    src = open(SRC, encoding="utf-8").read()
    body = ast.get_source_segment(src, _fn(_tree(), "worker_event_relay"))
    assert 'subscribe("vera:events:live")' in body and 'psubscribe("stream:*")' in body
    # only a worker's: the host's own events are delivered where emitted
    assert 'data.get("_from_worker")' in body
    # a worker never runs the relay
    assert body.index("if _IS_WORKER") < body.index("pubsub()")
    # started with the other listeners
    assert "asyncio.create_task(worker_event_relay())" in src
