"""CPU nodes take two LLM jobs at once; embeddings are never queued behind them;
the GPU node's CPU is the primary embedder (user, 2026-09-28).

Measured first: a CPU Ollama with one slot SERIALISES a second request; two
slots gave +20-35% total throughput; CPU embeds / a 0.5b on gpu-250's CPU did
not move the GPU's 9b throughput beyond noise."""
import base64

import pytest

from Vera.vera.provisioning import ollama_node_core as core
from Vera.vera import ollama_gate as gate
import Vera.vera.capability_orchestration as CO

pytestmark = pytest.mark.critical


def test_cpu_nodes_are_gated_to_two_generations_the_gpu_to_one():
    assert gate.capacity_for(False, {}) == 2
    assert gate.capacity_for(True, {}) == 1


def test_this_process_follows_the_node_not_one_global_limit(monkeypatch):
    monkeypatch.setattr(CO, "_OLLAMA_SEM_OVERRIDE", "")
    monkeypatch.setitem(CO.OLLAMA_INSTANCES, "t-gpu", {"has_gpu": True})
    monkeypatch.setitem(CO.OLLAMA_INSTANCES, "t-cpu", {"has_gpu": False})
    assert CO._ollama_sem_limit("t-gpu") == 1
    assert CO._ollama_sem_limit("t-cpu") == 2
    monkeypatch.setattr(CO, "_OLLAMA_SEM_OVERRIDE", "3")          # explicit env wins
    assert CO._ollama_sem_limit("t-gpu") == 3


def test_embedding_and_naming_prefer_the_gpu_nodes_cpu_sibling():
    rules = CO.DEFAULT_ROUTING_RULES
    assert CO.EMBED_PRIMARY_NODE == core.cpu_sibling_id("gpu-250") == "gpu-250-cpu"
    for jt in ("embedding", "naming"):
        assert rules[jt]["prefer"] == "gpu-250-cpu" and rules[jt]["deny_gpu"], jt
        assert not rules[jt]["pin"], jt          # soft: the CPU nodes take overflow
    # the large-model jobs stay on the CPU nodes
    assert rules["dream_director"]["pin"] == "cpu-247"


def _probe(unit="ollama-vera", models="/root/.ollama/models", dropin="", sibling="absent"):
    return core.parse_tune_probe(
        f"UNIT={unit}\nMODELS={models}\nDROPIN_B64={base64.b64encode(dropin.encode()).decode()}\n"
        f"SIBLING={sibling}\n")


def test_tune_plan():
    # a CPU node without the drop-in gets it (and is told it restarts)
    p = core.tune_plan(False, _probe())
    assert p["action"] == "set_concurrency" and "restarts ollama-vera" in p["why"]
    # already tuned -> nothing
    assert core.tune_plan(False, _probe(dropin=core.concurrency_dropin()))["action"] == "none"
    # a GPU node keeps its single GPU slot and gets the CPU sibling
    p = core.tune_plan(True, _probe(models="/.ollama/models"))
    assert p["action"] == "add_sibling" and p["models"] == "/.ollama/models"
    assert core.tune_plan(True, _probe(sibling="active"))["action"] == "none"
    assert core.tune_plan(True, _probe(models=""))["action"] == "skip"


def test_the_sibling_is_cpu_only_on_the_same_readonly_store():
    u = core.cpu_sibling_unit("/.ollama/models")
    for line in ('OLLAMA_MODELS=/.ollama/models', 'OLLAMA_HOST=0.0.0.0:11436',
                 'OLLAMA_NOPRUNE=1', 'CUDA_VISIBLE_DEVICES=', 'OLLAMA_LLM_LIBRARY=cpu',
                 'OLLAMA_NUM_PARALLEL=2', 'OLLAMA_MAX_LOADED_MODELS=3'):
        assert line in u, line
    d = core.concurrency_dropin()
    assert "OLLAMA_NUM_PARALLEL=2" in d and "OLLAMA_MAX_LOADED_MODELS=3" in d
    assert core.tune_probe_cmd().count("systemctl") >= 3
