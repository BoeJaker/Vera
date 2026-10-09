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


def test_one_backend_control_on_every_page_that_lists_nodes():
    el = _src("vera/workers/backend_switch_element.js")
    for label in ("All → vLLM", "All → Ollama", "Ticked → vLLM", "Ticked → Ollama"):
        assert label in el
    assert "customElements.define('vera-backend-switch'" in el
    assert "'/nodes/backend/switch_many'" in el and "dry_run: true" in el and "confirm(" in el
    assert "'/nodes/backend/embeddings'" in el and "'/nodes/backend/config/set'" in el
    assert "prompt(" not in el
    # Node settings, the Ollama page and the vLLM page all mount it
    assert "<vera-backend-switch></vera-backend-switch>" in _src("vera/workers/warm_models_capabilities.py")
    ol = _src("vera/workers/workers_ollama_panel.html")
    assert '<vera-backend-switch id="ol-backend"' in ol and "_olSel" not in ol
    assert "P.olSwitchOne(${sq(id)}" in ol
    vl = _src("vera/vllm/vllm_panel.html")
    assert '<vera-backend-switch id="vllm-backend">' in vl and 'data-tab="nodes"' in vl
    assert "backendSwitch([" in vl
    for page in (ol, vl):
        assert '/ui/elements/backend_switch.js' in page


def test_all_moves_embeddings_too():
    nc = _src("vera/workers/nodes_capabilities.py")
    many = nc[nc.index("async def cap_nodes_backend_switch_many("):]
    many = many[:many.index("@capability(")]
    assert "with_embed = not instance_ids" in many
    assert "cap_nodes_backend_embeddings(backend=backend, dry_run=False)" in many
    emb = nc[nc.index("async def cap_nodes_backend_embeddings("):]
    emb = emb[:emb.index("@capability(")]
    # to Ollama: unpin first, then stop; to vLLM: start, then pin
    assert emb.index('await _set_embedding_pin("")') < emb.index("disable --now vera-vllm_embed")
    assert emb.index("enable --now vera-vllm_embed") < emb.index('await _set_embedding_pin("vllm:*")')
    assert "stop the idle vLLM embedding server" in emb


def test_ollama_settings_wait_while_a_node_serves_vllm():
    nc = _src("vera/workers/nodes_capabilities.py")
    assert "serves vLLM now; its Ollama settings" in nc
    assert '"switched": iid in _switched_now()' in nc
