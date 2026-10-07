"""Regressions for three bridges that reached a capability the wrong way.

* /openclaw/call/{cap} read cap["fn"], which the registry never sets, so every
  call returned HTTP 500; /openclaw/tools read cap["doc"] and served empty
  descriptions.
* llm.generate(backend="vllm") imported the vLLM module from a package path the
  loader never uses, swallowed the ImportError, and always answered from Ollama.
* GET /ollama/instances stopped answering when capabilities.py's registration
  of ollama.instances (mounted at /ollama/cluster) replaced the orchestrator's,
  yet panels still call that path.

Requires the full app (the `orch` fixture skips cleanly without it).
"""

import asyncio
import importlib.util
import os
import sys

import pytest


@pytest.fixture
def client(orch):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    import Vera.vera.openclaw.openclaw_capabilities  # noqa: F401  registers the bridge
    import Vera.vera.capabilities.capabilities  # noqa: F401  ollama.instances, llm.generate

    if "test.bridge.echo" not in orch.CAPABILITY_REGISTRY:
        @orch.capability("test.bridge.echo", memory="off", description="echo for the bridge test")
        async def _echo(text: str = "", trace_id=None):
            return {"echo": text}

    return TestClient(orch.APP)


def test_openclaw_call_invokes_the_registered_wrapper(client):
    r = client.post("/openclaw/call/test.bridge.echo", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"echo": "hi"}


def test_openclaw_call_rejects_a_non_object_body(client):
    r = client.post("/openclaw/call/test.bridge.echo", json=[1, 2])
    assert r.status_code == 400


def test_openclaw_tools_carry_the_registry_description(client):
    tools = client.get("/openclaw/tools").json()["tools"]
    echo = [t for t in tools if t["name"] == "test.bridge.echo"]
    assert echo and echo[0]["description"] == "echo for the bridge test"


def test_ollama_instances_get_path_serves_the_capability(client, orch):
    # /ollama/cluster is mounted by the lifespan; the alias is a plain route.
    a = client.get("/ollama/instances")
    assert a.status_code == 200
    direct = asyncio.run(orch.CAPABILITY_REGISTRY["ollama.instances"]["raw"]())
    assert set(a.json()) == set(direct)
    for node in a.json().values():
        # fields from both former registrations
        assert {"enabled", "num_ctx", "num_thread", "status"} <= set(node)


def test_llm_generate_reaches_the_loaded_vllm_module(orch, monkeypatch):
    import Vera.vera.capabilities.capabilities  # noqa: F401
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "vera", "vllm", "vllm_capabilities.py")
    mod = sys.modules.get("vllm_capabilities")
    if mod is None:
        spec = importlib.util.spec_from_file_location("vllm_capabilities", path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "vllm_capabilities", mod)
        spec.loader.exec_module(mod)

    inst = mod.VLLMInstance(id="test-v0", url="http://127.0.0.1:9", has_gpu=True)
    inst.status = "online"
    monkeypatch.setitem(mod.VLLM_INSTANCES, "test-v0", inst)

    seen = {}

    async def fake_routed(kind, rule, *, model=None, prefer_gpu=False, job_type="",
                          slot_timeout=None, stream_cb=None, **kw):
        seen.update(kind=kind, rule=rule, kw=kw)
        return {"ok": True, "instance": "test-v0", "model": "m", "gate": "test-v0",
                "result": {"role": "assistant", "content": "<think>hm</think>\nfrom-vllm"}}

    # the routed path (chat template, node slot/gate) - not vllm.generate's raw
    # completion, whose signature takes no trace_id (5 Oct 2026: every
    # backend=vllm call failed on it and fell back to Ollama)
    monkeypatch.setattr(mod, "routed_call", fake_routed)
    out = asyncio.run(orch.CAPABILITY_REGISTRY["llm.generate"]["raw"](
        prompt="hello", system="be brief", backend="vllm", trace_id="t1"))
    assert out["backend"] == "vllm" and out["text"] == "from-vllm"
    assert out["instance"] == "test-v0"
    assert seen["kind"] == "chat" and seen["rule"] == {"pin": "vllm:"}
    assert seen["kw"]["messages"] == [{"role": "system", "content": "be brief"},
                                      {"role": "user", "content": "hello"}]
    assert seen["kw"]["max_tokens"] is None
