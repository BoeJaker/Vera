"""Static contract for honest, independent provider-panel read states."""

from pathlib import Path

import pytest


pytestmark = pytest.mark.critical
SOURCE = Path(__file__).resolve().parents[1] / "vera" / "providers" / "providers_panel.html"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    source = _source()
    start = source.index(f"async function {name}(")
    end = source.index(f"async function {next_name}(", start)
    return source[start:end]


def test_shared_read_state_loads_before_the_panel_consumer():
    source = _source()
    assert source.index('<script src="/ui/vera-ui.js"></script>') < source.index("const P = (function(){")
    assert "window.veraUI.readState" in source
    assert "window.veraUI.renderReadState" in source


def test_transport_rejects_http_unreadable_and_malformed_responses():
    source = _source()
    assert "if(!r.ok) throw new Error('HTTP '+r.status)" in source
    assert "throw new Error('Unreadable response')" in source
    assert "throw new Error('Malformed response')" in source


def test_health_reads_are_independent_and_retryable():
    reload_source = _function("reload", "test")
    assert "Promise.all([loadProviders(),loadStructured(),loadDocuments(),loadUsage()])" in reload_source
    for reader, label in (
        ("loadProviders", "Provider inventory unavailable:"),
        ("loadStructured", "Structured-generation status unavailable:"),
        ("loadDocuments", "Document-parser status unavailable:"),
    ):
        block = _function(reader, {
            "loadProviders": "loadStructured",
            "loadStructured": "loadDocuments",
            "loadDocuments": "reload",
        }[reader])
        assert "{loading:true" in block
        assert "{error:true" in block
        assert label in block
        assert "}," + reader + ")" in block


def test_model_inventory_failure_cannot_leave_a_plausible_action():
    source = _source()
    load_models = _function("loadModels", "loadProviders")
    playground = _function("pgModels", "send")
    assert "if(!Array.isArray(j.models))" in load_models
    assert "(models unavailable)" in load_models
    assert "sel.disabled=true" in load_models
    assert "if(!Array.isArray(j.models))" in playground
    assert "(models unavailable)" in playground
    assert "$('pg-send').disabled=true" in playground
    assert "catch(e){ sel.innerHTML='<option>(error)</option>'; }" not in source


def test_usage_failure_clears_values_and_offers_retry():
    usage = _function("loadUsage", "clearUsage")
    assert "Malformed provider usage response" in usage
    assert "['k-cost','k-reqs','k-in','k-out'].forEach" in usage
    assert "textContent='—'" in usage
    assert "Provider usage unavailable:" in usage
    assert "},loadUsage)" in usage
    assert "catch(e){ /* leave */ }" not in usage


def test_internal_delivery_ids_are_not_product_copy():
    source = _source()
    assert "LIB04" not in source
    assert "LIB06" not in source
