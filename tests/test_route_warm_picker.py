"""The load-aware picker with warm residency, slots, context escalation and
warm spill - the real function, on the estate's shape (2026-09-28).
"""
import pytest

import Vera.vera.capability_orchestration as CO

# importing cluster REPLACES CO.pick_instance for the whole process; put back
# whatever was there so tests of the base picker that run after this one
# still exercise it
_BASE_PICK = CO.pick_instance
from Vera.vera.workers import cluster  # noqa: E402
CO.pick_instance = _BASE_PICK

pytestmark = pytest.mark.critical

M = "jaahas/qwen3.5-uncensored"
EMBED = "nomic-embed-text:latest"


def _inst(has_gpu, running, in_use=0, prio=0):
    return {"url": "http://x", "has_gpu": has_gpu, "status": "online", "enabled": True,
            "in_use": in_use, "priority": prio, "models": [M, EMBED, "qwen2.5:0.5b"],
            "running": list(running)}


@pytest.fixture
def estate(monkeypatch):
    insts = {
        "gpu-250": _inst(True, [M]),
        "gpu-250-cpu": _inst(False, [M, EMBED], prio=3),
        "cpu-246": _inst(False, [EMBED], prio=1),
        "cpu-247": _inst(False, ["qwen3.6:35b-a3b"], prio=2),
    }
    monkeypatch.setattr(cluster, "OLLAMA_INSTANCES", insts)
    monkeypatch.setattr(CO, "_LAST_PICKED", {})
    monkeypatch.setattr(CO, "WARM_STATE", {})
    monkeypatch.setattr(CO, "gpu_safe_ctx", lambda model, iid: 28672 if iid == "gpu-250" else 0)
    monkeypatch.setattr(CO, "_route_tps", lambda model, iid: {"gpu-250-cpu": 3.86}.get(iid, 0.0))
    monkeypatch.setattr(CO, "_ollama_sem_limit", lambda iid: 1 if insts[iid]["has_gpu"] else 2)
    return insts


def pick(**kw):
    ex = {}
    out = cluster._pick_instance_load_aware(explain=ex, **kw)
    return out, " | ".join(ex.get("reason") or [])


def test_chat_goes_to_the_gpu(estate):
    assert pick(model=M, job_type="chat", rule_override={"prefer_gpu": True})[0] == "gpu-250"


def test_a_context_too_big_for_the_gpu_goes_to_the_warm_cpu_node(estate):
    node, why = pick(model=M, job_type="chat", rule_override={"prefer_gpu": True}, ctx_need=60000)
    assert node == "gpu-250-cpu" and "ctx escalation" in why


def test_only_the_prompt_decides_escalation(estate):
    """A big num_predict inflates ctx_need; on the GPU the output just shrinks
    to fit the capped window, so a prompt that fits stays on the card."""
    node, _ = pick(model=M, job_type="code", rule_override={"prefer_gpu": True},
                   ctx_need=40000, prompt_need=12000)
    assert node == "gpu-250"
    node, _ = pick(model=M, job_type="code", rule_override={"prefer_gpu": True},
                   ctx_need=40000, prompt_need=30000)
    assert node == "gpu-250-cpu"


def test_prompt_need_uses_measured_chars_per_token(monkeypatch):
    monkeypatch.setattr(CO, "OLLAMA_INSTANCES", {"g": {"has_gpu": True}})
    monkeypatch.setattr(CO, "_ROUTE_STATS", {})
    # unmeasured: 3 chars/token, not the window arithmetic's 2.3
    assert CO.prompt_need_tokens("x" * 64000, "", M, "chat") == 64000 // 3 + CO._CTX_RESERVE_OUT
    monkeypatch.setattr(CO, "_ROUTE_STATS", {CO._route_stats_key(M, "g", "chat"): {
        "model": M, "instance": "g", "n_tok_measured": 10, "ema_chars_per_token": 4.0}})
    assert CO.prompt_need_tokens("x" * 64000, "", M, "chat") == 16000 + CO._CTX_RESERVE_OUT


def test_warm_node_wins_among_idle_cpu_nodes(estate):
    node, _ = pick(model=M, rule_override={"deny_gpu": True})
    assert node == "gpu-250-cpu"


def test_gpu_full_waits_unless_spill_is_allowed(estate):
    estate["gpu-250"]["in_use"] = 1
    assert pick(model=M, job_type="chat", rule_override={"prefer_gpu": True})[0] == "gpu-250"
    node, why = pick(model=M, job_type="chat", rule_override={"prefer_gpu": True, "spill": True})
    assert node == "gpu-250-cpu" and "spill" in why


def test_a_scenario_turns_spill_on_for_its_job_types(estate, monkeypatch):
    estate["gpu-250"]["in_use"] = 1
    monkeypatch.setattr(CO, "WARM_STATE", {"spill_job_types": ["loop_coder"], "spill_min_tps": 3.0,
                                           "spill_max_ctx": 8192})
    assert pick(model=M, job_type="loop_coder", rule_override={"prefer_gpu": True})[0] == "gpu-250-cpu"
    assert pick(model=M, job_type="chat", rule_override={"prefer_gpu": True})[0] == "gpu-250"


def test_a_cpu_node_with_a_free_slot_beats_a_full_one(estate):
    estate["gpu-250-cpu"]["in_use"] = 2
    estate["cpu-246"]["in_use"] = 1
    node, _ = pick(model=M, rule_override={"deny_gpu": True})
    assert node in ("cpu-246", "cpu-247")


# ── the light job types (2026-09-28) ─────────────────────────────────────────
def test_glob_rule_covers_every_idle_job_type():
    r = CO._resolve_rule("idle_intel")
    assert r.get("prefer") == CO.EMBED_PRIMARY_NODE and not r.get("prefer_gpu")
    assert CO._resolve_rule("idle_whatever") is r
    assert CO._resolve_rule("chat") is not r


def test_quick_opener_stays_off_the_gpu(estate):
    rule = CO._resolve_rule("quick_opener")
    assert rule["deny_gpu"] and rule["prefer"] == CO.EMBED_PRIMARY_NODE
    node, _ = pick(model=M, job_type="quick_opener", rule_override=rule)
    assert node == "gpu-250-cpu"


def test_idle_work_takes_the_sibling_and_overflows_to_an_idle_gpu(estate):
    rule = CO._resolve_rule("idle_intel")
    assert pick(model=M, job_type="idle_intel", rule_override=rule)[0] == "gpu-250-cpu"
    estate["gpu-250-cpu"]["in_use"] = 1
    assert pick(model=M, job_type="idle_intel", rule_override=rule)[0] == "gpu-250"
