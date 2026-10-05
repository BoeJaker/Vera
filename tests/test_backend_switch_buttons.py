"""Backend buttons on the Models page (owner, 6 Oct 2026): switch all or ticked
nodes between Ollama and vLLM with one click each.

A node switches with the settings it last ran on vLLM, else the store model
suggested for it (name + the flags it needs on that node); nodes switch one after
another in the background (nodes.backend.switch_many), installing vLLM where it
is missing.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.vllm import node_backend_core as bc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_store_models_get_an_ollama_style_name():
    assert bc.suggest_alias("/opt/vera-store/models/hf/QuantTrio--Qwen3.5-9B-AWQ") == "qwen3.5:9b"
    assert bc.suggest_alias("/opt/vera-store/models/hf/Qwen--Qwen3-4B") == "qwen3:4b"  # pragma: allowlist secret
    assert bc.suggest_alias("/x/something-else") == ""
    assert bc.is_embedding_model("/x/nomic-ai--nomic-embed-text-v1.5")
    assert not bc.is_embedding_model("/x/Qwen--Qwen3-4B")


def test_suggested_flags_are_what_each_node_needed():
    gpu = bc.suggest_extra("/x/Qwen--Qwen3-4B", True)
    assert "--dtype float16" in gpu and "--max-num-seqs 16" in gpu
    cpu = bc.suggest_extra("/x/QuantTrio--Qwen3.5-9B-AWQ", False)
    assert "--dtype bfloat16" in cpu and "--limit-mm-per-prompt.image 0" in cpu
    assert bc.suggest_extra("/x/Qwen--Qwen3-4B", False) == ""


def test_switch_many_is_one_after_another_in_the_background():
    nc = _src("vera/workers/nodes_capabilities.py")
    many = nc[nc.index("async def cap_nodes_backend_switch_many("):]
    many = many[:many.index("@capability(")]
    assert "asyncio.create_task(_run())" in many and "for iid in ids:" in many
    assert "a bulk switch is already running" in many
    one = nc[nc.index("async def _switch_one_planned("):]
    assert "cap_nodes_backend_config(instance_id=iid)" in one[:600]
    # the switch remembers what it ran with
    assert "await _switch_config_save(instance_id, {" in nc


def test_the_models_page_has_the_buttons():
    ui = _src("vera/workers/workers_ollama_panel.html")
    for label in ("All → vLLM", "All → Ollama", "Ticked → vLLM", "Ticked → Ollama"):
        assert label in ui
    assert "P.olSwitchOne(${sq(id)}" in ui
    assert "'/nodes/backend/switch_many'" in ui and "dry_run:true" in ui and "confirm(" in ui
