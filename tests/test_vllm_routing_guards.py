"""vLLM gets traffic only through the routing table, and registers its real GPU flag.

Static guards (the code paths sit inside large app modules):
* llm.generate(backend="auto") used to send every prefer_gpu call to ANY online
  vLLM instance - a CPU one included, asking for an Ollama model name it does not
  serve - before the routing table ran. "auto" must follow the routing table;
  only backend="vllm" goes to vLLM directly.
* nodes.provision registered every vLLM server with has_gpu=True.
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


def test_auto_backend_does_not_bypass_the_routing_table():
    body = _func("vera/capabilities/capabilities.py", "llm_generate")
    block = body[body.index("_use_vllm = False"):body.index("if _use_vllm:")]
    code = "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))
    assert 'if backend == "vllm":' in code
    assert '"auto"' not in code
    assert "prefer_gpu" not in code


def test_vllm_response_reports_the_instance_gpu_flag():
    body = _func("vera/capabilities/capabilities.py", "llm_generate")
    assert '"backend": "vllm", "has_gpu": True' not in body


def test_register_vllm_does_not_hardcode_a_gpu():
    body = _func("vera/workers/nodes_capabilities.py", "_register_vllm")
    assert "has_gpu=True" not in body
    assert "has_gpu=bool(has_gpu)" in body


def test_backend_switch_registers_the_ct_gpu_flag():
    body = _func("vera/proxmox/pxstore_capabilities.py", "cap_backend_switch")
    assert "has_gpu=_ct_gpu" in body


def test_provisioners_share_the_install_recipe():
    assert "_vllm_install_core.install_script(" in _func(
        "vera/proxmox/pxstore_capabilities.py", "cap_backend_provision_vllm")
    assert "_vllm_core.install_script(" in _func(
        "vera/provisioning/software_capabilities.py", "cap_install")


def test_runtime_registry_is_persisted():
    src = _src("vera/vllm/vllm_capabilities.py")
    assert "_reg_core.note_added(" in _func("vera/vllm/vllm_capabilities.py", "vllm_instances_add")
    assert "_reg_core.note_removed(" in _func("vera/vllm/vllm_capabilities.py", "vllm_instances_remove")
    assert "await _ensure_registry_loaded()" in _func("vera/vllm/vllm_capabilities.py", "_health_loop")
    assert "vllm.instances.list" not in src.split('"""', 2)[1]
