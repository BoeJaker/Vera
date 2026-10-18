"""ollama.instances has ONE registration (owner decision D11 a).

It used to be registered twice - capability_orchestration.py and
capabilities/capabilities.py - and which copy served depended on load order
(the second one also carried num_thread and the /ollama/cluster mount). The
orchestration copy is the owner now: it is mounted at /ollama/instances, has a
plain /ollama/cluster alias, and computes num_thread. Source scan only (ast),
so it needs no app.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
VERA = os.path.join(ROOT, "vera")
ORCH = os.path.join(VERA, "capability_orchestration.py")
CAP = "ollama.instances"


def _trees():
    for base, _, files in os.walk(VERA):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(base, f)
            try:
                src = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if CAP not in src and "/ollama/cluster" not in src and "/ollama/instances" not in src:
                continue
            yield p, ast.parse(src)


def _registrations():
    out = []
    for p, tree in _trees():
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef) or isinstance(node, ast.FunctionDef)):
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "capability"
                            and dec.args and isinstance(dec.args[0], ast.Constant) and dec.args[0].value == CAP):
                        out.append((os.path.abspath(p), node, dec))
    return out


def test_ollama_instances_is_registered_exactly_once_in_orchestration():
    regs = _registrations()
    assert len(regs) == 1, [r[0] for r in regs]
    assert regs[0][0] == os.path.abspath(ORCH)


def test_both_paths_are_served_by_the_one_registration():
    (_, fn, dec), = _registrations()
    kw = {k.arg: k.value for k in dec.keywords}
    assert isinstance(kw.get("http_path"), ast.Constant) and kw["http_path"].value == "/ollama/instances"

    # /ollama/cluster: APP.add_api_route("/ollama/cluster", <alias>) whose alias awaits the cap's function
    tree = ast.parse(open(ORCH, encoding="utf-8").read())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    routes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) == "add_api_route"
              and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "/ollama/cluster"]
    assert len(routes) == 1
    alias = funcs[routes[0].args[1].id]
    called = {c.func.id for c in ast.walk(alias) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert fn.name in called


def test_no_other_module_mounts_the_paths():
    for p, tree in _trees():
        if os.path.abspath(p) == os.path.abspath(ORCH):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in ("/ollama/cluster", "/ollama/instances"):
                # a docstring/comment mention is fine; a route or http_path is not
                pytest.fail(f"{p} names {node.value} (only capability_orchestration.py serves it)")


def test_the_one_registration_carries_num_thread_and_a_presentation():
    (_, fn, dec), = _registrations()
    keys = {k.value for d in ast.walk(fn) if isinstance(d, ast.Dict) for k in d.keys if isinstance(k, ast.Constant)}
    assert {"num_thread", "num_ctx", "enabled", "status"} <= keys
    kw = {k.arg: k.value for k in dec.keywords}
    contract = ast.literal_eval(kw["contract"])
    # the answer is keyed by instance id: rows '$' reads it as rows, the key as the first column ('name') - with no
    # map the table form drew nothing (DA-01e, tried on prod's real answer)
    P = contract["presentation"]
    assert (P["kind"], P["form"], P["map"]) == ("table", "table", {"rows": "$"})
    assert P["draw"]["columns"][0] == "name"
