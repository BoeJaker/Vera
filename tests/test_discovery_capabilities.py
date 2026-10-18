import asyncio
import importlib.util
import os
import sys
import types

from orchestration_stub import stubbed_orchestration


ROOT = os.path.join(os.path.dirname(__file__), "..")


def load_module():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}

    def capability(name, **metadata):
        def decorate(fn):
            orch.CAPABILITY_REGISTRY[name] = {"raw": fn, "func": fn, "meta": metadata}
            return fn
        return decorate

    orch.capability = capability
    path = os.path.join(ROOT, "vera", "discovery_capabilities.py")
    spec = importlib.util.spec_from_file_location("discovery_capabilities_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with stubbed_orchestration(orch):
        spec.loader.exec_module(module)
    return module, orch


def run(value):
    return asyncio.run(value)


def test_route_is_registered_as_opt_in_and_redacted():
    _module, orch = load_module()
    entry = orch.CAPABILITY_REGISTRY["discovery.route.run"]
    assert entry["meta"]["redact_args"] == ["query"]
    assert entry["meta"]["redact_result"] is True
    assert "does not replace" in entry["meta"]["description"]


def test_route_calls_native_boundaries_once_with_persistence_disabled(monkeypatch):
    module, orch = load_module()
    calls = []

    async def search(**kwargs):
        calls.append(("search", kwargs))
        return {"results": [{"url": "https://example.test/a?private=1",
                              "title": "Portable discovery", "score": .9}]}

    async def fetch(**kwargs):
        calls.append(("fetch", kwargs))
        return {"text": "Cited portable context."}

    orch.CAPABILITY_REGISTRY["web.search"] = {"raw": search}
    orch.CAPABILITY_REGISTRY["web.fetch"] = {"raw": fetch}
    monkeypatch.setattr(module.time, "time", lambda: 2_000.0)
    monkeypatch.setattr(module, "_now", lambda: "2026-10-17T12:00:00Z")
    result = run(orch.CAPABILITY_REGISTRY["discovery.route.run"]["raw"](
        query="portable context", max_sources=2, max_context_items=2,
        timeout_ms=3_000, max_bytes=2_000))

    assert result["ok"] is True
    assert result["mode"] == "opt_in"
    assert result["authority"] == {
        "search": "web.search", "fetch": "web.fetch", "retry": "native",
        "persistence": "disabled", "consumer_routing_changed": False,
    }
    assert calls[0] == ("search", {"query": "portable context", "limit": 2,
                                    "discover": "off"})
    assert calls[1][0] == "fetch"
    assert calls[1][1]["url"] == "https://example.test/a?private=1"
    assert calls[1][1]["ingest_to_fabric"] is False
    assert calls[1][1]["await_ingest"] is False
    candidate = result["result"]["candidates"][0]
    assert "private=1" not in candidate["locator"]
    assert result["plan"] == {"plan_id": result["plan"]["plan_id"],
                               "assignments": 1, "refusals": 0}


def test_missing_native_capability_is_payload_free_failure(monkeypatch):
    module, orch = load_module()

    async def search(**kwargs):
        return {"results": [{"url": "https://example.test/a", "title": "one"}]}

    orch.CAPABILITY_REGISTRY["web.search"] = {"raw": search}
    monkeypatch.setattr(module.time, "time", lambda: 2_000.0)
    monkeypatch.setattr(module, "_now", lambda: "2026-10-17T12:00:00Z")
    result = run(orch.CAPABILITY_REGISTRY["discovery.route.run"]["raw"](
        query="portable context", timeout_ms=3_000))
    assert result["ok"] is True
    assert result["result"]["receipts"] == []
    assert result["failures"][0]["reason"] == "RuntimeError"
    assert "web_fetch_unavailable" not in str(result)


def test_blank_query_returns_before_any_native_call():
    _module, orch = load_module()
    result = run(orch.CAPABILITY_REGISTRY["discovery.route.run"]["raw"](query=" "))
    assert result == {"ok": False, "error": "query_required"}


def test_frozen_benchmark_is_registered_and_never_claims_production_readiness():
    _module, orch = load_module()
    entry = orch.CAPABILITY_REGISTRY["discovery.benchmark.evaluate"]
    result = run(entry["raw"]())
    assert result["ok"] is True
    assert result["production_ready"] is False
    assert result["authority_changed"] is False
    assert result["providers_invoked"] is False
    assert result["operator_projection"]["passed"] is True
    assert "never calls" in entry["meta"]["description"]
