"""Every LLM call names what processed it - Ollama, vLLM, an API (and which
provider) or something else - and Loop Lab shows it (owner, 10 Oct 2026).

Calls served on vLLM or by an API provider used to return before the request
log entry was written, so Loop Lab's routing tables and the census rows did not
see them at all.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.census import control as ct  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_backend_label_names_what_processed_a_call():
    assert ct.backend_label({"backend": "ollama", "instance": "cpu-246"}) == "ollama"
    assert ct.backend_label({"backend": "vllm", "instance": "vllm-cpu-247"}) == "vllm"
    assert ct.backend_label({"backend": "api", "provider": "anthropic"}) == "api:anthropic"
    assert ct.backend_label({"backend": "other", "provider": "fastembed"}) == "other:fastembed"
    # entries from before the log said so: read from the instance
    assert ct.backend_label({"instance": "provider:openai"}) == "api:openai"
    assert ct.backend_label({"instance": "vllm-gpu-250"}) == "vllm"
    assert ct.backend_label({"instance": "gpu-250"}) == "ollama"


def test_census_summaries_count_calls_by_backend():
    calls = [{"backend": "ollama", "node": "gpu-250"}, {"node": "vllm-cpu-247"},
             {"backend": "api:anthropic"}, {"backend": "vllm", "node": "vllm-cpu-247"}]
    assert ct.calls_by_backend(calls) == {"ollama": 1, "vllm": 2, "api:anthropic": 1}
    live = ct.live_routing([{"ts": "2026-10-10T10:00:01", "instance": "vllm-cpu-247", "backend": "vllm",
                             "model": "qwen3.5:9b", "tok_per_s": 3.0},
                            {"ts": "2026-10-10T10:00:02", "instance": "provider:anthropic", "backend": "api",
                             "provider": "anthropic", "model": "claude", "tok_per_s": 40.0}],
                           "2026-10-10T10:00:00")
    assert live["by_backend"] == {"vllm": 1, "api:anthropic": 1}
    assert {x["backend"] for x in live["last"]} == {"vllm", "api:anthropic"}
    roll = ct.routing_rollup([{"routing": {"at_start": {"roles": {}}, "calls": {
        "calls": calls, "n": 4, "nodes": {}, "models": {}}}}])
    assert roll["backends"] == {"ollama": 1, "vllm": 2, "api:anthropic": 1}


def test_the_census_harness_records_the_backend():
    spec = importlib.util.spec_from_file_location("run_census_t", os.path.join(ROOT, "tools/loop-census/run_census.py"))
    src = _src("tools/loop-census/run_census.py")
    assert '"backend": backend_label(e),' in src and '"backends": backends,' in src
    # the same rule as vera/census/control.py
    ns = {}
    exec(src[src.index("def backend_label(e):"):src.index("def routing_calls(")], ns)
    assert ns["backend_label"]({"instance": "provider:openai"}) == "api:openai"
    assert ns["backend_label"]({"backend": "vllm"}) == "vllm"
    assert spec is not None


def test_calls_served_off_the_ollama_nodes_are_logged():
    co = _src("vera/capability_orchestration.py")
    gen = co[co.index("async def ollama_generate("):]
    gen = gen[:gen.index("    req_entry = {")]
    # the vLLM pin, an API provider, a switched node: each logs done AND error
    assert gen.count('_log_served_elsewhere("vllm"') == 4
    assert gen.count('_log_served_elsewhere("api"') == 2
    assert 'provider=_prov_id' in gen
    # Ollama's own entries say so; writers that do not are read from the instance
    assert '"backend": "ollama"' in co
    san = co[co.index("def _sanitize_ollama_log_entry("):]
    assert 'clean["backend"] = _log_backend_of(' in san[:600]
    emb = co[co.index("async def ollama_embed("):] if "async def ollama_embed(" in co else co
    assert '_log_served_elsewhere("other", provider="fastembed"' in co
    assert emb.count('_log_served_elsewhere("vllm"') >= 2
    ag = _src("vera/agents/agents.py")
    assert '"backend": "vllm" if _vt else "ollama"' in ag


def test_loop_lab_shows_the_backend():
    ui = _src("vera/evolve/evolve_panel.html")
    for name in ("function _beOf(", "function _beTag(", "function _beTags(", "function _beLive("):
        assert name in ui
    assert "<th>model</th><th>backend</th><th>node</th>" in ui       # a goal's call table
    assert "processed by</span>" in ui                               # its summary line
    assert "_beTags(ro.backends)" in ui and "_beTags(ro.by_backend)" in ui
    assert "<th>coder</th><th>backend</th>" in ui                    # task history
    # the request-log views and the header pills say it too
    assert "<th>Backend</th>" in _src("vera/routing_panel.html")
    assert "i.backend==='vllm'" in _src("vera/capability_orchestration.html")
