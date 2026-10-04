"""/mcp/call does not turn the caller's attribution id into a memory search filter.

The MCP bridge sends a session_id with every Claude/Codex call; injected into memory.search
or memory.recall it limited each agent's search to its own session - almost always nothing.
capability_orchestration has app-level side effects, so the helper is lifted out with ast.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "capability_orchestration.py")


def _load():
    text = open(SRC, encoding="utf-8").read()
    tree = ast.parse(text)
    ns = {"Dict": dict, "Any": object}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_inject_attribution_session":
            exec(ast.get_source_segment(text, n), ns)
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_SESSION_FILTER_CAPS" for t in n.targets):
            exec(ast.get_source_segment(text, n), ns)
    return ns["_inject_attribution_session"], text


def test_filters_are_not_injected_attribution_is():
    inject, _ = _load()
    props = {"session_id": {"type": "string"}, "query": {}}
    assert not inject("memory.search", props)
    assert not inject("memory.recall", props)
    assert inject("memory.store", props)
    assert inject("sandbox.exec", props)
    assert not inject("memory.store", {"query": {}})


def test_handler_uses_the_helper():
    _, text = _load()
    assert "if _inject_attribution_session(name, _own):" in text
    assert 'if "session_id" in _own:' not in text
