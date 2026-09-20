"""__VERA_BASE__ is the origin the browser used, so a front in between (the
netctl portal) is never bypassed and a page opened by IP never calls a name the
client cannot resolve."""
import ast
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical
SRC = os.path.join(ROOT, "vera", "capability_orchestration.py")


def snippet_fn(tls):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "client_config_snippet")
    ns = {"json": __import__("json"), "cfg": type("C", (), {"BACKEND_HOST": "llm.int", "TLS_ENABLED": tls})(),
          "_CLIENT_CONFIG_SNIPPET": b"<script>fallback</script>"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["client_config_snippet"]


def test_the_base_is_the_host_the_browser_used():
    f = snippet_fn(True)
    assert b'"https://192.168.0.138:8999"' in f({"host": "192.168.0.138:8999"})
    assert b'"https://vera.vera.int"' in f({"host": "vera.vera.int"})
    assert b'__VERA_DOMAIN__="llm.int"' in f({"host": "vera.vera.int"}), "the configured domain still travels"


def test_a_front_in_between_wins_and_the_configured_base_is_the_fallback():
    f = snippet_fn(True)
    out = f({"host": "127.0.0.1:8999", "x-forwarded-host": "vera.vera.int", "x-forwarded-proto": "https"})
    assert b'"https://vera.vera.int"' in out
    assert snippet_fn(False)({"host": "llm.int:8999"}).count(b"http://llm.int:8999") == 1
    assert f({}) == b"<script>fallback</script>"
    src = open(SRC, encoding="utf-8").read()
    assert "snippet = client_config_snippet(request.headers)" in src
