"""A node switched to vLLM keeps taking its share of load-balanced work (5 Oct 2026).

Switching a node disables its Ollama instance, and both pickers route only to
enabled nodes - so a switched node was reached only by a rule naming it, and
ordinary chat, agent loops and generation never used it. A switched node whose
vLLM server is up now stands in as a candidate (with that server's models and
load); the caller serves a pick of it on vLLM (ollama_generate, agents.run /
run_stream, embeddings).
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.vllm import node_backend_core as bc  # noqa: E402
from vera.vllm import vllm_route_core as rc  # noqa: E402

M = "qwen3.5:9b"
EMBED = "nomic-embed-text:latest"


def _ollama(enabled=True, has_gpu=False, in_use=0, prio=0, models=(M, EMBED)):
    return {"url": "http://x", "has_gpu": has_gpu, "status": "offline" if not enabled else "online",
            "enabled": enabled, "in_use": in_use, "priority": prio, "models": list(models),
            "running": []}


def _vllm(gate_as, status="online", models=("/store/Qwen3.5-9B-AWQ", M), in_use=0):
    return {"gate_as": gate_as, "status": status, "models": list(models), "in_use": in_use,
            "waiting_requests": 0, "has_gpu": False}


# ── pure ──────────────────────────────────────────────────────────────────────

def test_a_switched_node_with_its_server_up_stands_in_with_the_servers_state():
    ollama = {"cpu-246": _ollama(enabled=False, prio=1), "gpu-250": _ollama(has_gpu=True)}
    vllm = {"vllm-cpu-246": _vllm("cpu-246", in_use=2)}
    out = bc.standins(ollama, vllm, model=M, serves=rc.serves)
    assert list(out) == ["cpu-246"]
    v = out["cpu-246"]
    assert v["status"] == "online" and v["enabled"] and v["backend"] == "vllm"
    assert M in v["models"] and EMBED not in v["models"]      # the server's names, not Ollama's
    assert v["in_use"] == 2 and v["priority"] == 1
    assert bc.vllm_of("cpu-246", vllm) == "vllm-cpu-246"


def test_no_standin_for_a_model_the_server_does_not_serve_or_a_server_that_is_down():
    ollama = {"cpu-246": _ollama(enabled=False)}
    assert bc.standins(ollama, {"vllm-cpu-246": _vllm("cpu-246")}, model=EMBED, serves=rc.serves) == {}
    assert bc.standins(ollama, {"vllm-cpu-246": _vllm("cpu-246", status="offline")},
                       model=M, serves=rc.serves) == {}
    # an enabled node is not switched, whatever gates as it
    assert bc.standins({"cpu-246": _ollama()}, {"v": _vllm("cpu-246")}, model=M, serves=rc.serves) == {}


# ── the load-aware picker (the real function) ─────────────────────────────────

CO = pytest.importorskip("Vera.vera.capability_orchestration")
_BASE_PICK = CO.pick_instance
from Vera.vera.workers import cluster  # noqa: E402
CO.pick_instance = _BASE_PICK


@pytest.fixture
def estate(monkeypatch):
    ollama = {
        "gpu-250": _ollama(has_gpu=True, in_use=1),
        "gpu-250-cpu": _ollama(prio=3, in_use=1),
        "cpu-246": _ollama(enabled=False, prio=1),
        "cpu-247": _ollama(enabled=False, prio=2),
    }
    vllm = {"vllm-cpu-246": _vllm("cpu-246"), "vllm-cpu-247": _vllm("cpu-247", in_use=1)}
    mod = types.SimpleNamespace(
        standins=lambda model=None: bc.standins(ollama, vllm, model=model, serves=rc.serves),
        standin_for=lambda nid: bc.vllm_of(nid, vllm) if nid in bc.switched_ids(ollama, vllm) else "")
    monkeypatch.setitem(sys.modules, "vllm_capabilities", mod)
    monkeypatch.setattr(cluster, "OLLAMA_INSTANCES", ollama)
    monkeypatch.setattr(CO, "OLLAMA_INSTANCES", ollama)
    monkeypatch.setattr(CO, "_LAST_PICKED", {})
    monkeypatch.setattr(CO, "WARM_STATE", {})
    monkeypatch.setattr(CO, "_ollama_sem_limit", lambda iid: 1 if ollama[iid]["has_gpu"] else 2)
    return ollama, mod


def pick(**kw):
    ex = {}
    out = cluster._pick_instance_load_aware(explain=ex, **kw)
    return out, " | ".join(ex.get("reason") or [])


def test_a_cpu_job_reaches_the_idle_switched_node(estate):
    node, why = pick(model=M, job_type="summarize", rule_override={"deny_gpu": True})
    assert node == "cpu-246", why                       # idle; gpu-250-cpu and cpu-247 are busy
    assert "stands in through its vLLM server" in why
    assert estate[1].standin_for(node) == "vllm-cpu-246"


def test_an_embedding_never_lands_on_a_chat_models_server(estate):
    node, _ = pick(model=EMBED, job_type="embedding", rule_override={"deny_gpu": True})
    assert node == "gpu-250-cpu"


def test_no_standins_is_the_fallback_pick_after_a_failed_vllm_call(estate):
    node, _ = pick(model=M, job_type="summarize", rule_override={"deny_gpu": True}, no_standins=True)
    assert node == "gpu-250-cpu"


def test_a_rule_pin_naming_a_switched_node_still_picks_it(estate):
    node, _ = pick(model=M, job_type="chat", rule_override={"pin": "cpu-247"})
    assert node == "cpu-247" and estate[1].standin_for(node) == "vllm-cpu-247"


def test_the_base_picker_offers_standins_too(estate):
    # agents.py binds pick_instance at import - it may be this one
    assert _BASE_PICK(model=M, job_type="summarize", rule_override={"deny_gpu": True}) in (
        "cpu-246", "cpu-247")


# ── callers serve a stand-in pick on vLLM ─────────────────────────────────────

def _src(rel):
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel),
              encoding="utf-8") as f:
        return f.read()


def test_generate_chat_and_embed_divert_a_standin_pick():
    co = _src("vera/capability_orchestration.py")
    gen = co[co.index("async def ollama_generate("):]
    assert "_vm_r.standin_for(chosen)" in gen
    assert '_vllm_generate_routed(\n                _vm_r, {"pin": f"vllm:{_sv_id}"}' in gen
    assert "no_standins=True" in gen
    assert '_vm_e.routed_call("embed", {"pin": f"vllm:{_sv_e}"}' in co
    ag = _src("vera/agents/agents.py")
    assert ag.count("_vt = _vllm_standin_target(chosen, model)") == 2      # run + run_stream


def test_a_routed_generate_uses_the_chat_template_and_think():
    import asyncio
    seen = {}

    async def routed_call(kind, rule, **kw):
        seen.update(kind=kind, rule=rule, kw=kw)
        return {"ok": True, "instance": "vllm-cpu-247", "model": M,
                "result": {"role": "assistant", "content": "<think>x</think>\n\nRiver Notes Today"}}

    vm = types.SimpleNamespace(routed_call=routed_call, _route_core=rc)
    out = asyncio.run(CO._vllm_generate_routed(
        vm, {"pin": "vllm:vllm-cpu-247"}, prompt="Title this.", system="Be brief.",
        json_mode=False, think=False, options={"num_predict": 32}, model=M,
        prefer_gpu=False, job_type="naming", timeout=None, stream_cb=None))
    assert seen["kind"] == "chat"
    assert seen["kw"]["messages"] == [{"role": "system", "content": "Be brief."},
                                      {"role": "user", "content": "Title this."}]
    assert seen["kw"]["extra"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["kw"]["max_tokens"] == 32
    assert out["result"] == "River Notes Today"
    # json_mode turns thinking off unless asked, and asks for a JSON object
    asyncio.run(CO._vllm_generate_routed(
        vm, None, prompt="p", system="", json_mode=True, think=None, options=None, model=M,
        prefer_gpu=False, job_type="", timeout=None, stream_cb=None))
    assert seen["kw"]["extra"] == {"chat_template_kwargs": {"enable_thinking": False},
                                   "response_format": {"type": "json_object"}}
    assert seen["kw"]["max_tokens"] is None          # the rest of the window
