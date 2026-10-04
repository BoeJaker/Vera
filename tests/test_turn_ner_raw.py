"""The turn pipeline's NER must call nlp.ner raw (no activity node per chunk, per reply).

nlp.ner is registered memory="on"; calling it through the recording wrapper from
turn.understand / turn.prepare wrote up to 12 activity nodes on every chat turn.
Source-level guard: the module imports the orchestrator, so it is checked by AST.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "canvas", "enrich_capabilities.py")


def _tree():
    with open(SRC, encoding="utf-8") as f:
        return ast.parse(f.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _called_names(fn):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            first = node.args[0] if node.args else None
            out.append((node.func.id, first.value if isinstance(first, ast.Constant) else None))
    return out


def test_ner_uses_the_raw_call():
    calls = _called_names(_func(_tree(), "_ner"))
    assert ("_call_raw", "nlp.ner") in calls
    assert ("_call", "nlp.ner") not in calls


def test_call_raw_prefers_the_bare_function():
    src = ast.unparse(_func(_tree(), "_call_raw")) if hasattr(ast, "unparse") else open(SRC, encoding="utf-8").read()
    assert 'cap.get("raw")' in src or "cap.get('raw')" in src
