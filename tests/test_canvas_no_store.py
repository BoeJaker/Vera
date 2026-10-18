"""mirror-diag 2026-10-17: with no Redis connected, the session canvas resolver made a new canvas, "saved" it (a no-op) and
answered ok/created - and /canvas/get then said "unknown canvas". With no store there is no canvas: _target returns None,
so canvas.session.resolve answers {ok: false, error: "no store"}. Offline: _target is taken from the source and run with
stand-ins for what it calls."""
import ast
import asyncio
import os
from typing import Any, Dict, Optional

import pytest

pytestmark = pytest.mark.critical

SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vera", "canvas", "canvas_capabilities.py")
SRC = open(SRC_PATH, encoding="utf-8").read()


def _target_with(redis_up):
    node = next(n for n in ast.parse(SRC).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_target")
    writes = []

    async def _load(cid):
        return None

    async def _write(doc, op):
        writes.append((doc["id"], op))
        return 1
    ns = {"Optional": Optional, "Dict": Dict, "Any": Any, "_load": _load, "_write": _write, "now_iso": lambda: "t",
          "_session_canvas_id": lambda s: ("cv_session_" + s) if s else "", "_redis": lambda: (object() if redis_up else None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC_PATH, "exec"), ns)
    return ns["_target"], writes


def test_no_store_no_canvas():
    t, writes = _target_with(False)
    assert asyncio.run(t("", "chat-1")) is None and writes == []


def test_with_a_store_it_is_made_as_before():
    t, writes = _target_with(True)
    doc = asyncio.run(t("", "chat-1"))
    assert doc["id"] == "cv_session_chat-1" and doc["title"] == "Session canvas" and writes == [("cv_session_chat-1", "create")]


def test_the_resolver_says_so():
    assert 'doc = await _target("", session_id)\n    if not doc:\n        return {"ok": False, "error": "no store"}' in SRC
