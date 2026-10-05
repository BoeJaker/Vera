"""vLLM is part of node provisioning: a component installed beside Ollama, never
auto-started, and a switched node is left alone by tune / tap / warm.

Static guards over app modules that cannot be imported app-free.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.vllm import vllm_install_core as ic  # noqa: E402

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMP = "vera/provisioning/components_capabilities.py"
NODES = "vera/workers/nodes_capabilities.py"
WARM = "vera/workers/warm_models_capabilities.py"


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func(rel, name):
    src = _src(rel)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {rel}")


def _component_block():
    src = _src(COMP)
    i = src.index('"vllm_server": {')
    return src[i:src.index('\n    },\n', i)]


def test_vllm_server_component_is_installed_not_started():
    blk = _component_block()
    assert '"autostart": False' in blk
    assert '"install_script": lambda has_gpu, venv, sudo:' in blk
    assert "wheel_dir=VLLM_STORE_WHEELS" in blk
    # thread parity with the node's Ollama runners
    assert "cpu_threads=_node_threads_core.DEFAULT_CPU_THREADS" in blk
    assert '"unit_env_file": VLLM_NODE_ENV' in blk


def test_install_script_is_part_of_the_component_version():
    body = _func(COMP, "_deps_spec")
    assert '"install_script"' in body and "callable(script)" in body


def test_deploy_runs_the_install_script():
    body = _func(COMP, "cap_deploy")
    assert 'callable(comp.get("install_script"))' in body
    assert "_vllm_core.DONE_MARKER" in body


def test_autostart_false_never_enables_or_starts_a_stopped_unit():
    body = _func(COMP, "_launch_systemd")
    i = body.index('if comp.get("autostart") is False:')
    branch = body[i:body.index("else:", i)]
    assert "is-active --quiet" in branch and "restart" in branch
    assert "systemctl enable" not in branch


def test_tune_tap_and_warm_skip_a_switched_node():
    assert "if iid in _sw:" in _func(NODES, "cap_nodes_ollama_tune")
    assert "if iid in _sw:" in _func(NODES, "cap_nodes_ollama_tap")
    assert "switched_ids" in _func(WARM, "_instances")


def test_switch_capability_registers_its_vllm_instance_gated_as_the_node():
    body = _func(NODES, "cap_nodes_backend_switch")
    assert "gate_as=instance_id" in body
    assert 'inst["enabled"] = False' in body and 'inst["enabled"] = True' in body
    assert "_node_threads(inst)" in body          # Ollama thread parity
    assert "is_dev_sandbox()" in body


def test_cpu_env_default_is_overridable_by_the_unit():
    body = ic.env_body("cpu", cpu_threads=6)
    assert "OMP_NUM_THREADS=\\${OMP_NUM_THREADS:-6}" in body


def test_cpu_recipe_prefers_the_store_wheel():
    s = ic.install_script("/opt/vera-vllm/venv", mode="cpu", wheel_dir="/opt/vera-store/models/vllm")
    assert "WHEEL_DIR=/opt/vera-store/models/vllm" in s
    assert s.index("WHEEL_DIR=") < s.index("needs glibc")


def test_the_cpu_slot_component_is_the_cpu_build_in_its_own_place():
    src = _src("vera/provisioning/components_capabilities.py")
    i = src.index('"vllm_server_cpu": {')
    body = src[i:i + 1600]
    assert 'mode="cpu"' in body and "VLLM_CPU_NODE_DIR" in body
    assert '"autostart": False' in body and "VLLM_CPU_NODE_ENV" in body
    assert 'VLLM_CPU_NODE_PORT = 8001' in src
