"""llm.generate reports the model and node the router actually used."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]
CAPS = ROOT / "vera/capabilities/capabilities.py"
ORCH = ROOT / "vera/capability_orchestration.py"


def boundary():
    tree = ast.parse(CAPS.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"_normalize_generation_think", "llm_generate"}]
    for node in functions:
        node.decorator_list = []
        if node.name == "llm_generate":
            node.body = [stmt for stmt in node.body if not isinstance(stmt, ast.ImportFrom)]
    namespace = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(CAPS), "exec"), namespace)
    return namespace


def scope_routed_to(route, picked="gpu-250"):
    scope = boundary()

    async def generate(prompt, meta_out=None, **kwargs):
        meta_out.update(route)
        return "Routing Probe Chat"

    async def context(*args, **kwargs):
        return 32

    async def save(*args):
        return {}

    scope.update(os=SimpleNamespace(getenv=lambda *args: "32"),
                 _ollama_caller_info=lambda: {"caller_func": "test"},
                 _output_budget=None, effective_num_ctx=context,
                 ollama_generate=generate, _llm_save_output=save,
                 pick_instance=lambda **kwargs: picked,
                 OLLAMA_INSTANCES={"cpu-246": {"url": "http://cpu-246", "has_gpu": False},
                                   "gpu-250": {"url": "http://gpu-250", "has_gpu": True}},
                 OLLAMA_MODEL="jaahas/qwen3.5-uncensored")
    return scope


@pytest.mark.asyncio
async def test_a_job_type_route_reports_the_rules_model_and_node():
    scope = scope_routed_to({"model": "qwen2.5:7b", "instance": "cpu-246"})
    result = await scope["llm_generate"]("name this chat", backend="ollama", job_type="naming")
    assert result["model"] == "qwen2.5:7b"
    assert result["instance"] == "cpu-246"
    assert result["instance_url"] == "http://cpu-246"
    assert result["has_gpu"] is False


@pytest.mark.asyncio
async def test_without_a_route_record_it_falls_back_to_the_request():
    scope = scope_routed_to({}, picked="gpu-250")
    result = await scope["llm_generate"]("hello", backend="ollama", model="gemma3:12b")
    assert result["model"] == "gemma3:12b"
    assert result["instance"] == "gpu-250"


def test_the_router_records_the_model_and_node_it_used():
    source = ORCH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    router = next(node for node in tree.body
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "ollama_generate")
    body = ast.get_source_segment(source, router)
    assert 'meta_out.update({"model": mdl, "instance": chosen})' in body
    assert 'meta_out["instance"] = fb_id' in body
