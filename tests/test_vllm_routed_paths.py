"""Every routed path honours a vLLM pin the same way, through one helper.

Static guards over the app modules (they cannot be imported app-free):
* generate (ollama_generate), chat (agents.run / run_stream) and embeddings
  (_ollama_embed_impl) all resolve a "vllm:" pin via vllm_capabilities, so they
  share target choice, the hardware gate and the fallback to Ollama routing;
* the generate pin holds the GPU gate (it used to call vllm_generate ungated);
* an embedding pin never substitutes a different embedding model.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func(rel, name):
    src = _src(rel)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {rel}")


CO = "vera/capability_orchestration.py"
AG = "vera/agents/agents.py"
VL = "vera/vllm/vllm_capabilities.py"


def test_generate_pin_goes_through_routed_call_and_reports_meta():
    body = _func(CO, "ollama_generate")
    block = body[body.index('_pin.startswith("vllm:")'):]
    block = block[:block.index("API-provider delegation")]
    assert '_vllm_mod.routed_call(\n                "generate"' in block
    assert '"backend": "vllm"' in block
    assert "_vllm_gen(" not in block            # the old ungated direct call


def test_routed_call_holds_the_hardware_gate():
    body = _func(VL, "routed_call")
    assert "_orch._ollama_slot(gid" in body
    assert "_route_core.gate_id(inst, iid)" in body
    assert 'act["t"] = time.monotonic()' in body   # token liveness for the heartbeat


def test_chat_paths_route_vllm_pins():
    for fn in ("run", "run_stream"):
        body = _func(AG, fn)
        assert "_vllm_chat_target(agent, _jt, model)" in body, fn
        assert '_vt["gate"] if _vt else pick_instance(' in body, fn
    assert "ollama_chat_once(c, _vt, body)" in _func(AG, "run")
    assert "ollama_chat_stream(c, _vt, body) if _vt" in _func(AG, "run_stream")


def test_embedding_pin_requires_the_same_model():
    assert 'routed_call("embed", _resolve_rule("embedding")' in _func(CO, "_ollama_embed_impl")
    body = _func(VL, "routed_call")
    i = body.index('if kind == "embed":')
    assert "_route_core.serves(inst, model)" in body[i:i + 500]


def test_gate_capacity_reads_vllm_gpu_flag():
    assert "_node_has_gpu(iid)" in _func(CO, "_ollama_sem_limit")
    assert "VLLM_INSTANCES" in _func(CO, "_node_has_gpu")
