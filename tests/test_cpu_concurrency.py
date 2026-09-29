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


def test_a_fresh_gpu_node_gets_its_sibling_on_the_installers_store():
    """A node Vera just installed runs the STOCK unit: no OLLAMA_MODELS, user
    `ollama`. Provisioning must still be able to give it the CPU sibling."""
    def probe(user):
        return core.parse_tune_probe(chr(10).join(
            ["UNIT=ollama", "MODELS=", "DROPIN_B64=", "SIBLING=absent", "USER=" + user]))
    p = core.tune_plan(True, probe("ollama"))
    assert p["action"] == "add_sibling" and p["models"] == "/usr/share/ollama/.ollama/models"
    assert core.tune_plan(True, probe(""))["models"] == "/root/.ollama/models"
    assert "USER=" in core.tune_probe_cmd()


def test_the_sibling_is_cpu_only_on_the_same_readonly_store():
    u = core.cpu_sibling_unit("/.ollama/models")
    for line in ('OLLAMA_MODELS=/.ollama/models', 'OLLAMA_HOST=0.0.0.0:11436',
                 'OLLAMA_NOPRUNE=1', 'CUDA_VISIBLE_DEVICES=', 'OLLAMA_LLM_LIBRARY=cpu',
                 'OLLAMA_NUM_PARALLEL=2', 'OLLAMA_MAX_LOADED_MODELS=3'):
        assert line in u, line
    d = core.concurrency_dropin()
    assert "OLLAMA_NUM_PARALLEL=2" in d and "OLLAMA_MAX_LOADED_MODELS=3" in d
    assert core.tune_probe_cmd().count("systemctl") >= 3


# ── runner thread default + node settings (2026-09-29) ───────────────────────
def test_threads_default_is_set_server_side():
    """A bare /api/generate on gpu-250-cpu spawned a runner with no -t -
    llama.cpp's 24 on 12 CPUs. LLAMA_ARG_THREADS makes num_thread the default
    for every caller; an explicit -t still wins."""
    assert 'Environment="LLAMA_ARG_THREADS=6"' in core.threads_dropin(6)
    assert core.threads_plan("", 6)["action"] == "set_threads"
    assert core.threads_plan("24", 6)["action"] == "set_threads"
    assert core.threads_plan("6", 6)["action"] == "none"
    assert core.threads_plan("", 0)["action"] == "none"
    assert "LLAMA_ARG_THREADS" in core.threads_probe_cmd("ollama-vera-cpu")


def test_settings_parse():
    import base64
    d = base64.b64encode(b'[Service]\nEnvironment="OLLAMA_NUM_PARALLEL=2"\n').decode()
    out = core.parse_settings(chr(10).join([
        "UNIT=ollama-vera-cpu", "ENV=OLLAMA_NUM_PARALLEL=2", "ENV=LLAMA_ARG_THREADS=6",
        "DROPIN=20-vera-concurrency.conf:" + d,
        "RUNNER=32768|2|6|sha256-abc|15|0", "RUNNER=4096|1||sha256-def|27|0",
        "RUNNER=2048|1|6|sha256-emb|15|1"]))
    assert out["unit"] == "ollama-vera-cpu"
    assert out["env"] == {"OLLAMA_NUM_PARALLEL": "2", "LLAMA_ARG_THREADS": "6"}
    assert out["dropins"][0]["name"] == "20-vera-concurrency.conf"
    assert "OLLAMA_NUM_PARALLEL=2" in out["dropins"][0]["text"]
    assert out["runners"][1] == {"ctx": 4096, "parallel": 1, "threads": 0, "blob": "sha256-def",
                                 "os_threads": 27, "embedding": False, "model": ""}
    assert out["runners"][2]["embedding"] is True


def test_custom_flags_are_allow_listed_and_round_trip():
    clean, errs = core.validate_flags({"OLLAMA_FLASH_ATTENTION": "1", "LLAMA_ARG_BATCH": "512",
                                       "OLLAMA_KV_CACHE_TYPE": None, "PATH": "/tmp",
                                       "OLLAMA_HOST": "0.0.0.0", "LLAMA_ARG_THREADS": "8",
                                       "OLLAMA_X": "a b; rm -rf /"})
    assert clean == {"OLLAMA_FLASH_ATTENTION": "1", "LLAMA_ARG_BATCH": "512",
                     "OLLAMA_KV_CACHE_TYPE": None}
    assert len(errs) == 4                      # PATH, two managed keys, the unsafe value
    text = core.custom_dropin({"OLLAMA_FLASH_ATTENTION": "1", "LLAMA_ARG_BATCH": "512"})
    assert core.custom_flags_from_dropin(text) == {"OLLAMA_FLASH_ATTENTION": "1", "LLAMA_ARG_BATCH": "512"}


def test_runner_model_from_its_manifest():
    """A runner's --model is the GGUF blob; /api/tags lists MANIFEST digests,
    so the name comes from the manifest that references the blob."""
    assert core.model_from_manifest("registry.ollama.ai/library/qwen2.5/0.5b") == "qwen2.5:0.5b"
    assert core.model_from_manifest("registry.ollama.ai/jaahas/qwen3.5-uncensored/9b") == "jaahas/qwen3.5-uncensored:9b"
    assert core.model_from_manifest("hf.co/unsloth/Qwen3-Coder-GGUF/Q8_K_XL") == "hf.co/unsloth/Qwen3-Coder-GGUF:Q8_K_XL"
    assert core.model_from_manifest("") == ""
    out = core.parse_settings("RUNNER=2048|1|6|sha256-e|15|1|registry.ollama.ai/library/nomic-embed-text/latest")
    assert out["runners"][0]["model"] == "nomic-embed-text:latest"
