"""docker.ps tags each container row with its host_id (slim and full), so a row read on its own - the Host app's
containers part joined to nodes.list's docker_host_id - still says which machine it runs on.

The capability module imports the running app (fastapi, Vera.vera.*), so the two pure helpers are lifted out of THIS
checkout's source with ast and run on their own - no Docker engine, no import of the main checkout."""
import ast
import os
from typing import Dict, List

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vera", "workers", "docker_capabilities.py")


def _load():
    with open(SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    want = {"_docker_ps_rows", "_docker_ps_slim"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
    assert {n.name for n in fns} == want
    ns = {"Dict": Dict, "List": List}
    exec(compile(ast.Module(body=fns, type_ignores=[]), SRC, "exec"), ns)
    return ns, tree


NS, TREE = _load()
ROWS = NS["_docker_ps_rows"]

ENGINE = [
    {"Id": "abcdef0123456789", "Names": ["/vera"], "Image": "vera:latest", "State": "running", "Status": "Up 2 hours",
     "Created": 1700000000, "Labels": {"com.docker.compose.project": "vera"}, "Ports": []},
    {"Id": "fedcba9876543210", "Names": ["/redis"], "Image": "redis:7", "State": "exited", "Status": "Exited (0)",
     "Created": 1700000100, "Labels": None},
]


def test_full_rows_keep_the_engine_record_and_say_the_host():
    out = ROWS(ENGINE, "local", slim=False)
    assert [r["host_id"] for r in out] == ["local", "local"]
    for src, row in zip(ENGINE, out):
        assert {k: v for k, v in row.items() if k != "host_id"} == src


def test_slim_rows_say_the_host_beside_the_slim_fields():
    out = ROWS(ENGINE, "ct132", slim=True)
    assert out[0] == {"Id": "abcdef012345", "Names": ["/vera"], "Image": "vera:latest", "State": "running",
                      "Status": "Up 2 hours", "Created": 1700000000, "project": "vera", "host_id": "ct132"}
    assert out[1]["host_id"] == "ct132" and out[1]["project"] == ""


def test_the_engine_records_are_not_changed_in_place():
    ROWS(ENGINE, "local")
    assert all("host_id" not in r for r in ENGINE)


def test_odd_engine_answers_give_no_rows():
    assert ROWS(None, "local") == []
    assert ROWS({"message": "x"}, "local", slim=True) == []
    assert ROWS([1, "x", {"Id": "a"}], "h") == [{"Id": "a", "host_id": "h"}]


def test_the_capability_answers_through_the_helper_and_keeps_its_top_level_host():
    cap = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "cap_docker_ps")
    src = ast.unparse(cap)
    assert "_docker_ps_rows(" in src
    assert "'host_id': rec['id'], 'containers': rows, 'count': len(rows)" in src
