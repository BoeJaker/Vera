"""A node worker follows routing, node and embed changes the host makes (5 Oct 2026).

A worker loaded routing profiles, node enabled flags and the embed config from
Redis once at boot; a backend switch or a new pin after that never reached it.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_worker_rereads_shared_routing_state_each_health_cycle():
    with open(os.path.join(ROOT, "vera/capability_orchestration.py"), encoding="utf-8") as f:
        src = f.read()
    loop = src[src.index("async def instance_health_loop("):]
    loop = loop[:loop.index("await asyncio.sleep(interval)")]
    i = loop.index("if _IS_WORKER:")
    assert "await _load_ollama_persistence()" in loop[i:i + 900]
    assert loop.index("_load_ollama_persistence()") < loop.index("_ping_instance(")
