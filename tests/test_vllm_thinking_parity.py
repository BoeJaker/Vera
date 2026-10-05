"""Thinking on vLLM behaves as on Ollama, with a per-server switch (5 Oct 2026).

* A thinking model gets its reasoning parser when a node switches to it, so the
  thinking comes back in its own field - Ollama returns `thinking` apart from
  the answer - and agent chat / loops see the answer alone.
* A server has a thinking default (model default / on / off) for requests that
  do not choose, set from the vLLM page; a request that chooses (an agent's
  Think, llm.generate think=) always wins.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.vllm import node_backend_core as bc  # noqa: E402
from vera.vllm import vllm_registry_core as reg  # noqa: E402
from vera.vllm import vllm_route_core as rc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_a_qwen3_family_switch_gets_the_reasoning_parser():
    env = bc.env_file("/opt/vera-store/models/hf/QuantTrio--Qwen3.5-9B-AWQ", aliases=["qwen3.5:9b"],
                      extra="--dtype bfloat16")
    assert "--reasoning-parser qwen3" in env and "--dtype bfloat16" in env
    # an explicit parser is kept, not doubled
    env = bc.env_file("m", aliases=["qwen3:8b"], extra="--reasoning-parser deepseek_r1")
    assert env.count("--reasoning-parser") == 1 and "deepseek_r1" in env
    # a model that is not a known thinking family gets none
    assert "--reasoning-parser" not in bc.env_file("mistralai/Mistral-7B", aliases=[])


def test_the_server_default_applies_only_when_the_request_did_not_choose():
    b = {"messages": []}
    assert rc.apply_think_default(b, "off")["chat_template_kwargs"] == {"enable_thinking": False}
    assert rc.apply_think_default(b, "on")["chat_template_kwargs"] == {"enable_thinking": True}
    assert rc.apply_think_default(b, "") is b
    chose = {"chat_template_kwargs": {"enable_thinking": True}}
    assert rc.apply_think_default(chose, "off") is chose          # the request wins
    # an Ollama-shaped chat body with think set chooses too
    body = rc.chat_body({"model": "m", "messages": [], "think": False}, "m")
    assert rc.apply_think_default(body, "on")["chat_template_kwargs"] == {"enable_thinking": False}


def test_the_default_is_persisted_with_the_registry():
    assert "think_default" in reg.PERSISTED_FIELDS
    assert reg.record({"url": "u", "think_default": "OFF"})["think_default"] == "off"
    assert reg.record({"url": "u", "think_default": "maybe"})["think_default"] == ""


def test_vllm_chat_applies_it_and_the_page_sets_it():
    vc = _src("vera/vllm/vllm_capabilities.py")
    chat = vc[vc.index("async def vllm_chat("):]
    chat = chat[:chat.index("inst.in_use += 1")]
    assert "_route_core.apply_think_default(body, inst.think_default)" in chat
    assert 'name="vllm.instances.think"' in vc
    ui = _src("vera/vllm/vllm_panel.html")
    assert "data-think-inst=" in ui and "'/vllm/instances/think'" in ui
