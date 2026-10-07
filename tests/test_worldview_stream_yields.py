"""The worldview stream worker must never reattach without yielding.

2026-09-29: a release restart closed Redis while the worker was reading. Every
call on the closed client failed at once without yielding, and the worker
reattached straight away - a loop that never gave the event loop back. The
restart never reached its execv; prod sat at 18 GB RSS serving nothing until
it was killed. The read-error path now sleeps (a growing backoff) first.

Checked on the source tree: importing worldview_jepa pulls in the ML stack.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "worldview", "worldview_jepa.py")


def _worker():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    return next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_wv_stream_worker")


def _is_sleep(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and getattr(node.value.value.func, "attr", "") == "sleep")


def test_a_failed_read_sleeps_before_it_reattaches():
    # every handler that breaks out to reattach - not the CancelledError one,
    # which rightly stops at once
    handlers = [h for h in ast.walk(_worker()) if isinstance(h, ast.ExceptHandler)
                and any(isinstance(s, ast.Break) for s in h.body)
                and getattr(h.type, "attr", getattr(h.type, "id", "")) != "CancelledError"]
    assert handlers, "the read-error handler that breaks out to reattach"
    for h in handlers:
        body = h.body
        brk = next(i for i, s in enumerate(body) if isinstance(s, ast.Break))
        assert any(_is_sleep(s) for s in body[:brk]), \
            "the handler must await a sleep before `break` (else the reattach spins)"


def test_the_backoff_is_not_reset_on_attach():
    """Reset only after a read that worked - resetting on every attach kept
    the backoff at its floor while the client was closed."""
    src = ast.get_source_segment(open(SRC, encoding="utf-8").read(), _worker())
    attach = src.index("worldview stream worker attached")
    head = src[:attach]
    assert head.count("backoff = 2.0") == 1       # only the initial value
