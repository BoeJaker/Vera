"""Nodes running vLLM report alive, and embeddings work with no Ollama left (5 Oct 2026).

* The health loop pinged every node's Ollama, so a node switched to vLLM (its
  Ollama stopped on purpose) read "offline" in /health, the monitor, the
  cluster view and node activity. A switched node now reports its vLLM
  server's state, with backend="vllm" and served_by.
* A switched node reporting "online" must not become an Ollama failover target.
* Embeddings: nomic-embed-text on vLLM (vllm_embed component), vectors scaled to
  unit length as Ollama's /api/embed returns them; the fabric batch path and the
  worldview's direct path reach vLLM when no Ollama node is up.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.vllm import node_backend_core as bc  # noqa: E402
from vera.vllm import vllm_route_core as rc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_a_switched_node_reports_its_vllm_server():
    up = bc.liveness({"status": "online", "latency_ms": 12.0, "models": ["m", "qwen3.5:9b"]},
                     "vllm-cpu-246")
    assert up["status"] == "online" and up["backend"] == "vllm"
    assert up["served_by"] == "vllm-cpu-246" and up["models"] == ["m", "qwen3.5:9b"]
    assert bc.liveness({"status": "degraded"}, "v")["status"] == "online"
    assert bc.liveness({"status": "offline"}, "v")["status"] == "offline"
    assert bc.liveness(None, "v")["status"] == "offline"


def test_the_health_ping_and_reports_are_switch_aware():
    co = _src("vera/capability_orchestration.py")
    ping = co[co.index("async def _ping_instance("):]
    ping = ping[:ping.index("t0 = time.monotonic()")]
    assert "_switched_liveness(iid)" in ping and "return" in ping
    health = co[co.index("async def obs_health("):]
    health = health[:health.index('"gpu_gate"')]
    assert '"backend":i.get("backend","ollama")' in health
    caps = _src("vera/capabilities/capabilities.py")
    assert '"backend":i.get("backend","ollama"),"served_by":i.get("served_by","")' in caps
    assert '"backend":    i.get("backend", "ollama")' in _src("vera/monitor/monitor_capabilities.py")


def test_failovers_skip_a_disabled_node():
    co = _src("vera/capability_orchestration.py")
    assert 'fb_inst["status"]!="online" or not fb_inst.get("enabled", True)' in co
    assert 'or not fb_inst.get("enabled", True)):' in co


def test_routed_embeddings_are_unit_length():
    v = rc.unit([3.0, 4.0])
    assert abs(v[0] - 0.6) < 1e-9 and abs(v[1] - 0.8) < 1e-9
    assert rc.unit([0.0, 0.0]) == [0.0, 0.0]
    vc = _src("vera/vllm/vllm_capabilities.py")
    rcall = vc[vc.index("async def routed_call("):]
    assert "_route_core.unit(v) for v in (vecs or [])" in rcall[:rcall.index("gid = _route_core.gate_id")]


def test_fabric_batch_and_worldview_reach_vllm_without_ollama():
    fab = _src("vera/fabric/data_fabric.py")
    many = fab[fab.index("async def _embed_many(texts"):]
    assert many.index("_embed_many_vllm(") < many.index('"/api/embed"')
    assert "no_standins=True" in many
    wv = _src("vera/worldview/worldview_jepa.py")
    direct = wv[wv.index("async def _embed_direct("):]
    direct = direct[:direct.index("with_model = [")]
    assert 'i.get("enabled", True)' in direct and "_orch.ollama_embed(ch, model=ollama_model)" in direct


def test_the_embed_component_serves_nomic_under_ollamas_names():
    cc = _src("vera/provisioning/components_capabilities.py")
    i = cc.index('"vllm_embed": {')
    body = cc[i:i + 1600]
    assert 'mode="cpu"' in body and "VLLM_EMBED_MODEL" in body and "--trust-remote-code" in body
    assert '"autostart"' not in body                    # started on deploy
    assert 'VLLM_EMBED_NAMES = ("nomic-embed-text", "nomic-embed-text:latest")' in cc
    nc = _src("vera/workers/nodes_capabilities.py")
    assert 'if key == "vllm_embed" and res.get("ok"):' in nc and 'gate_as=""' in nc
