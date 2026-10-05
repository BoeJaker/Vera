"""A vLLM server can stand in for model names it does not run (owner, 5 Oct 2026).

With no Ollama left, requests for models vLLM cannot run on this hardware go to a
chosen stand-in: the server is routed those names (and offered by the picker for
them) and is sent its own model. Set per server on the vLLM page; persisted.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.vllm import node_backend_core as bc  # noqa: E402
from vera.vllm import vllm_registry_core as reg  # noqa: E402
from vera.vllm import vllm_route_core as rc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPU = {"status": "online", "has_gpu": True, "gate_as": "gpu-250",
       "models": ["/store/Qwen--Qwen3-4B", "qwen3:4b"],
       "stands_in_for": ["qwen2.5:0.5b", "qwen3.6:35b-a3b"]}


def test_a_stand_in_name_routes_here_and_sends_the_servers_own_model():
    assert rc.serves(GPU, "qwen2.5:0.5b") and rc.serves(GPU, "qwen3:4b")
    assert not rc.serves(GPU, "nomic-embed-text")
    assert rc.served_model(GPU, "qwen2.5:0.5b") == "/store/Qwen--Qwen3-4B"
    assert rc.served_model(GPU, "qwen3:4b") == "qwen3:4b"          # a real served name is kept
    assert rc.choose({"vllm-gpu-250": GPU}, "*", model="qwen3.6:35b-a3b") == "vllm-gpu-250"


def test_the_picker_offers_a_switched_node_for_its_stand_in_names():
    ollama = {"gpu-250": {"url": "http://192.168.0.250:11435", "enabled": False, "has_gpu": True}}
    vllm = {"vllm-gpu-250": GPU}
    out = bc.standins(ollama, vllm, model="qwen2.5:0.5b", serves=rc.serves)
    assert "gpu-250" in out and "qwen2.5:0.5b" in out["gpu-250"]["models"]
    assert bc.standins(ollama, vllm, model="nomic-embed-text", serves=rc.serves) == {}


def test_names_are_persisted_clean():
    assert reg.record({"url": "u", "stands_in_for": " a, b ,a,,c"})["stands_in_for"] == ["a", "b", "c"]
    assert reg.record({"url": "u"})["stands_in_for"] == []
    assert "stands_in_for" in reg.PERSISTED_FIELDS


def test_the_page_edits_them():
    with open(os.path.join(ROOT, "vera/vllm/vllm_panel.html"), encoding="utf-8") as f:
        ui = f.read()
    assert "data-standin-inst=" in ui and "'/vllm/instances/stand_in'" in ui
    with open(os.path.join(ROOT, "vera/vllm/vllm_capabilities.py"), encoding="utf-8") as f:
        assert 'name="vllm.instances.stand_in"' in f.read()
