"""A routing-table pin reaches vLLM with the same semantics an Ollama node gets.

Pure decisions (vllm_route_core) shared by the generate, chat and embedding paths:
pin / offline fallback / deny_gpu / prefer_gpu / model affinity / load, the model
actually served, the hardware gate, and the Ollama<->OpenAI chat translation.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.vllm import vllm_route_core as rc  # noqa: E402

pytestmark = pytest.mark.critical


def _inst(status="online", gpu=False, models=("m",), in_use=0, prio=0, gate_as=""):
    return {"status": status, "has_gpu": gpu, "models": list(models), "in_use": in_use,
            "waiting_requests": 0, "priority": prio, "gate_as": gate_as}


FLEET = {
    "vllm-gpu-251": _inst(gpu=True, models=("Qwen/Qwen2.5-7B", "qwen2.5:7b"), gate_as="gpu-250"),
    "vllm-cpu-252": _inst(models=("Qwen/Qwen2.5-0.5B", "qwen2.5:0.5b"), prio=1, gate_as="cpu-246"),
    "vllm-cpu-253": _inst(models=("Qwen/Qwen2.5-0.5B", "qwen2.5:0.5b"), prio=2, gate_as="cpu-247"),
}


# ── the pin ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pin,want", [
    ("vllm:vllm-gpu-251", "vllm-gpu-251"), ("vllm:*", "*"), ("vllm:", "*"),
    ("cpu-246", None), ("provider:openai", None), ("", None),
])
def test_pin_target(pin, want):
    assert rc.pin_target({"pin": pin}) == want
    assert rc.pin_target(None) is None


# ── same semantics as an Ollama pin ──────────────────────────────────────────
def test_pinned_id_up_is_used():
    assert rc.choose(FLEET, "vllm-cpu-252") == "vllm-cpu-252"


def test_pinned_id_offline_falls_back_like_an_ollama_pin():
    # the live Ollama picker only honours a pin that is online (cluster.py);
    # an offline vLLM pin likewise yields no target -> normal routing
    fleet = dict(FLEET, **{"vllm-cpu-252": _inst(status="offline")})
    assert rc.choose(fleet, "vllm-cpu-252") is None
    assert rc.choose(FLEET, "no-such") is None


def test_star_deny_gpu_drops_gpu_servers():
    assert rc.choose(FLEET, "*", deny_gpu=True) in ("vllm-cpu-252", "vllm-cpu-253")
    only_gpu = {"g": FLEET["vllm-gpu-251"]}
    assert rc.choose(only_gpu, "*", deny_gpu=True) is None


def test_star_prefer_gpu_narrows_to_gpu_when_one_is_up():
    assert rc.choose(FLEET, "*", prefer_gpu=True) == "vllm-gpu-251"
    fleet = dict(FLEET, **{"vllm-gpu-251": _inst(status="offline", gpu=True)})
    assert rc.choose(fleet, "*", prefer_gpu=True) == "vllm-cpu-252"


def test_star_model_affinity_then_load_then_priority():
    assert rc.choose(FLEET, "*", model="qwen2.5:7b") == "vllm-gpu-251"
    busy = dict(FLEET, **{"vllm-cpu-252": _inst(models=("qwen2.5:0.5b",), in_use=2, prio=1)})
    assert rc.choose(busy, "*", model="qwen2.5:0.5b") == "vllm-cpu-253"
    assert rc.choose(FLEET, "*", model="qwen2.5:0.5b") == "vllm-cpu-252"


def test_degraded_is_used_but_ranked_after_online():
    fleet = {"a": _inst(status="degraded"), "b": _inst(prio=5)}
    assert rc.choose(fleet, "*") == "b"
    assert rc.choose({"a": _inst(status="degraded")}, "*") == "a"


# ── model, gate ──────────────────────────────────────────────────────────────
def test_served_model_alias_or_reported_substitute():
    inst = FLEET["vllm-cpu-252"]
    assert rc.served_model(inst, "qwen2.5:0.5b") == "qwen2.5:0.5b"
    assert rc.served_model(inst, "qwen3.5:9b") == "Qwen/Qwen2.5-0.5B"
    assert rc.served_model(_inst(models=()), "x") == "x"


def test_gate_follows_the_hardware():
    assert rc.gate_id(FLEET["vllm-gpu-251"], "vllm-gpu-251") == "gpu-250"
    assert rc.gate_id(_inst(), "vllm-x") == "vllm-x"
    assert list(rc.ollama_ids_shadowed(FLEET)) == ["cpu-246", "cpu-247", "gpu-250"]


# ── chat translation ─────────────────────────────────────────────────────────
def test_chat_body_from_an_ollama_body():
    b = rc.chat_body({"model": "qwen2.5:0.5b", "stream": True, "think": False,
                      "messages": [{"role": "system", "content": "s"},
                                   {"role": "assistant", "content": "a", "thinking": "t"}],
                      "options": {"num_ctx": 8192, "num_predict": 64, "temperature": 0.2,
                                  "num_thread": 6}}, "Qwen/Qwen2.5-0.5B")
    assert b["model"] == "Qwen/Qwen2.5-0.5B"
    assert b["messages"][1] == {"role": "assistant", "content": "a"}     # no Ollama-only keys
    assert b["max_tokens"] == 64 and b["temperature"] == 0.2
    assert "num_ctx" not in b and "num_thread" not in b and "options" not in b
    assert b["stream_options"] == {"include_usage": True}
    assert b["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_body_json_mode():
    assert rc.chat_body({"format": "json", "messages": []}, "m")["response_format"] == \
        {"type": "json_object"}


def test_chat_reply_is_ollama_shaped():
    r = rc.chat_reply({"choices": [{"message": {"content": "hi", "reasoning_content": "why"},
                                    "finish_reason": "stop"}],
                       "usage": {"completion_tokens": 3, "prompt_tokens": 9}})
    assert r["message"]["content"] == "hi" and r["message"]["thinking"] == "why"
    assert r["done"] and r["eval_count"] == 3 and r["prompt_eval_count"] == 9


def _sse(obj):
    return "data: " + json.dumps(obj)


def test_sse_lines_become_ollama_chunks():
    tok = rc.sse_to_ollama_line(_sse({"choices": [{"delta": {"content": "He"}}]}))
    assert tok == {"message": {"role": "assistant", "content": "He", "thinking": ""},
                   "done": False}
    th = rc.sse_to_ollama_line(_sse({"choices": [{"delta": {"reasoning_content": "hm"}}]}))
    assert th["message"]["thinking"] == "hm" and not th["done"]
    use = rc.sse_to_ollama_line(_sse({"choices": [], "usage": {"completion_tokens": 7,
                                                                 "prompt_tokens": 2}}))
    assert use["done"] and use["eval_count"] == 7 and use["prompt_eval_count"] == 2
    assert rc.sse_to_ollama_line("data: [DONE]")["done"] is True
    for junk in ("", ": keep-alive", "data: {bad", _sse({"choices": [{"delta": {}}]})):
        assert rc.sse_to_ollama_line(junk) is None
