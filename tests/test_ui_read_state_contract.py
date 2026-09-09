from pathlib import Path

import pytest


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def test_shared_read_state_has_four_exclusive_states_and_safe_rendering():
    source = (ROOT / "vera" / "vera-ui.js").read_text(encoding="utf-8")

    assert "function readState(input)" in source
    assert "input.loading ? 'loading'" in source
    assert "input.error ? 'error'" in source
    assert "input.hasData === false ? 'empty' : 'ready'" in source
    assert "message.textContent = state.label" in source
    assert "data-vera-read-state" in source
    assert "state.kind === 'error' ? 'alert' : 'status'" in source
    assert "typeof options.onRetry === 'function'" in source
    assert "readState: readState" in source
    assert "renderReadState: renderReadState" in source


def test_agent_bridge_uses_independent_retryable_read_states():
    source = (ROOT / "vera" / "agentbridges" / "agentbridge_catalog_panel.html").read_text(
        encoding="utf-8")

    assert '<script src="/ui/vera-ui.js"></script>' in source
    assert "showReadState('#list',{loading:true" in source
    assert "showReadState('#interop',{loading:true" in source
    assert "if (INTEROP_ERROR){ showReadState(el,{error:true" in source
    assert "if (CATALOG_ERROR){ showReadState(el,{error:true" in source
    assert "No interoperability status is available." in source
    assert "No agent bridges are registered." in source
    assert "catch(e) { return {error:'The service could not be reached.'}" in source


def test_read_state_renderer_does_not_inject_server_text_as_html():
    source = (ROOT / "vera" / "vera-ui.js").read_text(encoding="utf-8")
    function = source[source.index("function renderReadState"):
                      source.index("// ── Public API")]

    assert "textContent = state.label" in function
    assert "innerHTML" not in function
