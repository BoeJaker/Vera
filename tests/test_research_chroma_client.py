"""Research reuses one Chroma client per server (RF-82).

A new chromadb.HttpClient per research query left its keep-alive connections open: a mirror
held 518 connections to Chroma after a day and prod's memory search timed out.
researcher_api has app-level side effects, so the helper and the client builder are lifted
out with ast and run against a fake chromadb.
"""
import ast
import logging
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "research", "researcher_api.py")
TEXT = open(SRC, encoding="utf-8").read()
TREE = ast.parse(TEXT)


def _seg(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(TEXT, n)
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        if any(getattr(x, "id", "") == name for x in targets):
            return ast.get_source_segment(TEXT, n)
    raise AssertionError(name)


def test_one_http_client_per_server(monkeypatch):
    made = []

    class HttpClient:
        def __init__(self, host, port):
            made.append((host, port))

    monkeypatch.setitem(sys.modules, "chromadb", types.SimpleNamespace(HttpClient=HttpClient))
    ns = {"log": logging.getLogger("t")}
    for name in ("_CHROMA_CLIENTS", "_chroma_client", "_chroma_make_clients"):
        exec(_seg(name), ns)
    src = types.SimpleNamespace(config={"host": "chroma", "port": 8008})
    for _ in range(5):
        clients = ns["_chroma_make_clients"](src)
        assert clients and clients[0][1] == "http:chroma:8008"
    assert made == [("chroma", 8008)]


def test_no_client_is_built_outside_the_cache():
    for call in ("chromadb.HttpClient(", "chromadb.PersistentClient("):
        for line in TEXT.splitlines():
            if call in line and "lambda" not in line:
                pytest.fail(f"uncached client: {line.strip()}")
