"""The inline-<script> JS check falls back to `node --check` when esprima is missing (compute-roles,
11 Oct 2026): prod's venv has no esprima, the gate image does, so on prod the check was silently
skipped and an HTML file with broken JS passed as 'syntax verified'."""
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical

BROKEN = "<html><body><script>function tick() { if (x) { go(); }</script></body></html>"
GOOD = "<html><body><script>function tick() { if (x) { go(); } }</script></body></html>"


def test_fallback_is_wired():
    i = SRC.index("def _v5_js_syntax_error(")
    body = SRC[i:SRC.index("\ndef ", i + 10)]
    assert "esprima = None" in body and "_node.node_available()" in body
    assert "_node._node_check(body, module=(_typ == \"module\"))" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed here")
def test_node_check_catches_unbalanced_braces():
    from vera.evolve import panel_check as pc
    assert pc._node_check("function tick() { if (x) { go(); }")
    assert pc._node_check("function tick() { if (x) { go(); } }") == ""


def test_fallback_path_with_esprima_hidden(monkeypatch):
    try:
        from vera.dag import dag_workshop_capabilities as M
    except Exception:
        pytest.skip("app module not importable here")
    import builtins
    real = builtins.__import__

    def no_esprima(name, *a, **k):
        if name == "esprima":
            raise ImportError("hidden for the test")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_esprima)
    if shutil.which("node") is None:
        assert M._v5_js_syntax_error(BROKEN) == ""        # neither checker: no false failure
    else:
        assert M._v5_js_syntax_error(BROKEN).startswith("<script> JS:")
        assert M._v5_js_syntax_error(GOOD) == ""
