"""Static contract for Operator Studio connection-inventory read states."""

from pathlib import Path


PANEL = Path(__file__).resolve().parents[1] / "vera/operator/operator_studio_panel.html"


def test_operator_inventory_uses_shared_safe_read_state():
    html = PANEL.read_text(encoding="utf-8")
    assert html.index('<script src="/ui/vera-ui.js"></script>') < html.index("async function loadConnectables")
    assert 'id="connReadState"' in html
    assert "window.veraUI.readState({loading:" in html
    assert "window.veraUI.renderReadState($('connReadState')" in html
    assert "onRetry:loadConnectables" in html


def test_operator_inventory_distinguishes_unavailable_empty_and_ready():
    html = PANEL.read_text(encoding="utf-8")
    assert "showState('loading')" in html
    assert "showState('error',(d&&d.error)||'unreadable response')" in html
    assert "if(!items.length){ showState('empty'); return; }" in html
    assert "!Array.isArray(d.connectables)" in html
    assert "$('connReadState').replaceChildren()" in html


def test_operator_inventory_disables_selection_until_ready():
    html = PANEL.read_text(encoding="utf-8")
    load = html[html.index("async function loadConnectables"):html.index("function allowList")]
    assert "sel.disabled=true" in load
    assert "sel.disabled=false" in load
    assert "const items=d.connectables" in load
