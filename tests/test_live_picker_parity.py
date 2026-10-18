"""S2 / D01-1: the LIVE picker (cluster.py's load-aware pick_instance, which replaces the base at load) keeps the
base's guarantees: the per-request "GPU off" (NO_GPU), the GPU slot guard, the stuck-slot sweep and avoid_embed
sharing. Before 18 Oct it had none of them, so the owner's GPU toggle and the 11 Oct slot guard did nothing.

These drive the live picker itself on a fake two-node pool. Runs in the gate container."""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
import Vera.vera.workers.cluster as cl  # noqa: E402

M9, M7 = "jaahas/qwen3.5-uncensored:9b", "qwen2.5:7b"


def node(has_gpu, models, in_use=0, priority=0):
    return {"url": "http://x", "label": "", "has_gpu": has_gpu, "enabled": True, "priority": priority,
            "status": "online", "models": list(models), "in_use": in_use, "errors": 0}


@pytest.fixture
def pool(monkeypatch):
    saved = dict(orch.OLLAMA_INSTANCES)
    orch.OLLAMA_INSTANCES.clear()
    orch.OLLAMA_INSTANCES.update({"gpu-t": node(True, [M9, M7]), "cpu-t": node(False, [M7], priority=1)})
    monkeypatch.setattr(orch, "_GPU_RESIDENT", {})
    monkeypatch.setattr(cl, "_colocated_worker_load", lambda: {})
    monkeypatch.setattr(cl, "_proxy_queue_depth", lambda iid: 0)
    yield orch.OLLAMA_INSTANCES
    orch.OLLAMA_INSTANCES.clear()
    orch.OLLAMA_INSTANCES.update(saved)


def pick(**kw):
    ex = {}
    chosen = cl._pick_instance_load_aware(explain=ex, no_standins=True, **kw)
    return chosen, " | ".join(ex.get("reason") or [])


def test_the_live_picker_is_the_one_every_caller_gets():
    assert orch.pick_instance is cl._pick_instance_load_aware


def test_gpu_off_for_the_request_keeps_it_off_the_gpu(pool):
    assert pick(prefer_gpu=True, model=M7)[0] == "gpu-t"
    tok = orch.NO_GPU.set(True)
    try:
        chosen, why = pick(prefer_gpu=True, model=M7)
        assert chosen == "cpu-t" and "GPU off for this request" in why
        pool["cpu-t"]["status"] = "offline"                  # no CPU node: the GPU still serves, and says so
        chosen, why = pick(prefer_gpu=True, model=M7)
        assert chosen == "gpu-t" and "no CPU node is online" in why
    finally:
        orch.NO_GPU.reset(tok)


def test_slot_guard_sends_a_non_resident_model_off_a_busy_gpu(pool):
    pool["gpu-t"]["in_use"] = 1
    orch._GPU_RESIDENT["gpu-t"] = (time.time(), [M9])          # the 9b is seated and busy
    chosen, why = pick(prefer_gpu=True, model=M7)
    assert chosen == "cpu-t" and M7 in why
    chosen, _ = pick(prefer_gpu=True, model=M9)               # the resident model itself stays on the GPU
    assert chosen == "gpu-t"
    orch._GPU_RESIDENT["gpu-t"] = (time.time() - 10 ** 6, [M9])  # a stale reading never steers
    assert pick(prefer_gpu=True, model=M7)[0] == "gpu-t"


def test_a_leaked_slot_is_reclaimed_before_routing(pool):
    pool["gpu-t"]["in_use"] = 1
    pool["gpu-t"]["_inflight"] = {"leaked": time.monotonic() - 10 ** 6}
    pick(model=M7)
    assert pool["gpu-t"]["in_use"] == 0 and not pool["gpu-t"]["_inflight"]


def test_avoid_embed_shares_an_idle_embed_node_when_the_rest_is_busy(pool, monkeypatch):
    pool.clear()
    pool.update({"cpu-a": node(False, [M7]), "cpu-b": node(False, [M7], in_use=1, priority=1)})
    monkeypatch.setattr(orch, "_embed_node_id", lambda: "cpu-a")
    rule = {"avoid_embed": True}
    chosen, why = pick(model=M7, rule_override=rule)
    assert chosen == "cpu-a" and "avoid_embed relaxed" in why
    pool["cpu-b"]["in_use"] = 0                                 # an idle alternative: keep off the embed node
    chosen, why = pick(model=M7, rule_override=rule)
    assert chosen == "cpu-b" and "avoid_embed: excluded 'cpu-a'" in why
