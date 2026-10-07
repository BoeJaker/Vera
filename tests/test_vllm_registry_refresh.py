"""vLLM state a node worker routes by stays current (5 Oct 2026).

* A node worker loaded the vLLM registry once, at boot. A node switched to vLLM
  afterwards registered on the host, but the workers never saw it: calls
  offloaded to them could not reach the new server until each restarted. The
  health loop now re-reads the stored registry every cycle.
* Requests waited a fixed 180 s on the server; a CPU node's first request and
  ordinary replies run longer. The wait is VLLM_READ_TIMEOUT_S.
* The install recipe wrote uv's receipt into $HOME; a container whose /root it
  cannot write failed the install.

Requires the full app (the `orch` fixture skips cleanly without it) for the
refresh test; the others read source or the pure install recipe.
"""

import asyncio
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _vllm_module(monkeypatch):
    mod = sys.modules.get("vllm_capabilities")
    if mod is None:
        path = os.path.join(ROOT, "vera", "vllm", "vllm_capabilities.py")
        spec = importlib.util.spec_from_file_location("vllm_capabilities", path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "vllm_capabilities", mod)
        spec.loader.exec_module(mod)
    return mod


class _Redis:
    def __init__(self, doc):
        self.doc = doc

    async def get(self, key):
        return json.dumps(self.doc)

    async def set(self, key, value):
        self.doc = json.loads(value)


def test_a_worker_sees_an_instance_added_after_its_boot(orch, monkeypatch):
    mod = _vllm_module(monkeypatch)
    monkeypatch.setattr(mod, "VLLM_INSTANCES", {})
    monkeypatch.setattr(mod, "_REG_DOC", mod._reg_core.empty_doc())
    monkeypatch.setattr(mod, "_REG_LOADED", True)
    monkeypatch.setattr(mod, "_parse_instances_env", lambda: [])

    async def _no_ping(inst):
        inst.status = "online"

    monkeypatch.setattr(mod, "_ping_vllm", _no_ping)
    doc = mod._reg_core.note_added(mod._reg_core.empty_doc(), "vllm-cpu-247", {
        "url": "http://192.168.0.247:8000", "label": "cpu-247", "has_gpu": False,
        "priority": 0, "gate_as": "cpu-247"})
    monkeypatch.setattr(orch, "REDIS", _Redis(doc), raising=False)

    asyncio.run(mod._refresh_registry())
    inst = mod.VLLM_INSTANCES.get("vllm-cpu-247")
    assert inst is not None and inst.gate_as == "cpu-247" and inst.status == "online"

    # a removal on the host reaches the worker the same way
    orch.REDIS.doc = mod._reg_core.note_removed(orch.REDIS.doc, "vllm-cpu-247", [])
    asyncio.run(mod._refresh_registry())
    assert "vllm-cpu-247" not in mod.VLLM_INSTANCES


def test_the_health_loop_refreshes_every_cycle():
    src = _src("vera/vllm/vllm_capabilities.py")
    loop = src[src.index("async def _health_loop"):]
    loop = loop[:loop.index("await asyncio.sleep(interval)")]
    assert "await _refresh_registry()" in loop


def test_no_fixed_180s_request_timeout():
    src = _src("vera/vllm/vllm_capabilities.py")
    assert "timeout=180" not in src
    assert "VLLM_READ_TIMEOUT_S" in src


def test_the_recipe_keeps_uv_state_out_of_home():
    from vera.vllm import vllm_install_core as ic
    s = ic.install_script("/opt/vera-vllm/venv", mode="cpu")
    pre = s[:s.index('sh "$UVD/install.sh"')]
    for var in ("XDG_CONFIG_HOME=", "UV_CACHE_DIR=", "UV_PYTHON_INSTALL_DIR="):
        assert var in pre, var
