"""Warm model slots: each node keeps its models loaded, and a hot workload
takes the slots over.

Measured on prod 2026-09-28: every CPU node let its models lapse after
Ollama's five-minute default, so the first call to a node paid a cold load
(the 9b 10.5 s on gpu-250-cpu, the 35b MoE 62 s on cpu-247). A re-arm of a
resident 9b on CPU took ~33 s and held up the node's embeds, so the plan must
never re-arm on every tick, and Vera's own calls must keep a planned model on
the SAME runner (keep_alive=-1 as a NUMBER - the string "-1" is a 400 - and
the planned window).

Pure: dicts in, plans out.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.workers import warm_models_core as W      # noqa: E402

pytestmark = pytest.mark.critical

DEFAULT = "jaahas/qwen3.5-uncensored"
LONG = "qwen3.6:35b-a3b"
NAMING = "qwen2.5:0.5b"
EMBED = "nomic-embed-text:latest"
CODER = "qwen3-coder:30b"
ALIASES = {"@default": DEFAULT, "@naming": NAMING, "@embed": EMBED, "@long_horizon": LONG}

INSTANCES = {
    "gpu-250": {"has_gpu": True, "status": "online", "enabled": True},
    "gpu-250-cpu": {"has_gpu": False, "status": "online", "enabled": True},
    "cpu-246": {"has_gpu": False, "status": "online", "enabled": True},
    "cpu-247": {"has_gpu": False, "status": "online", "enabled": True},
}
# the live 'default' profile (2026-09-28), effective rules
RULES = {
    "embedding": {"deny_gpu": True, "prefer": "gpu-250-cpu", "model": EMBED},
    "naming": {"deny_gpu": True, "prefer": "gpu-250-cpu", "model": NAMING},
    "summarize": {"deny_gpu": True, "prefer": "gpu-250-cpu", "model": NAMING},
    "chat": {"prefer_gpu": True},
    "research_reader": {"deny_gpu": True, "prefer": "cpu-247"},
    "dream_director": {"deny_gpu": True, "pin": "cpu-247", "model": LONG},
    "plan_enrich": {"deny_gpu": True, "pin": "cpu-247"},
    "chat_enrich": {"deny_gpu": True, "pin": "cpu-247", "model": LONG},
}
SIZES = {DEFAULT: 7_400_000_000, LONG: 23_900_000_000, NAMING: 400_000_000,
         EMBED: 270_000_000, CODER: 18_600_000_000}
RAM = {"gpu-250": 12, "gpu-250-cpu": 25, "cpu-246": 50, "cpu-247": 50}


def _plan(cfg=None, states=None, **kw):
    cfg = W.merge_config(cfg)
    return W.plan(INSTANCES, cfg, RULES, ALIASES, states or {},
                  {k: SIZES for k in INSTANCES}, RAM, **kw)


def _models(p, node):
    return [m["model"] for m in p[node]["models"]]


def test_classes_and_slots():
    assert W.node_class("gpu-250", INSTANCES["gpu-250"], INSTANCES) == "gpu"
    assert W.node_class("gpu-250-cpu", INSTANCES["gpu-250-cpu"], INSTANCES) == "cpu_sibling"
    assert W.node_class("cpu-246", INSTANCES["cpu-246"], INSTANCES) == "cpu"
    assert W.slots_for("gpu") == 1 and W.slots_for("cpu") == 2 and W.slots_for("cpu_sibling") == 2
    assert W.slots_for("cpu", override=3) == 3


def test_baseline_gpu_one_llm_cpu_two():
    """The user's layout: the GPU holds the one configured LLM; every CPU node
    holds two; the GPU node's CPU sibling holds the configured GPU model too
    (so it can take a second caller or a big context) and embeds beside it."""
    p = _plan()
    assert _models(p, "gpu-250") == [DEFAULT]
    assert p["gpu-250"]["embed"] == ""                 # the GPU never embeds
    assert set(_models(p, "gpu-250-cpu")) == {NAMING, DEFAULT}
    assert p["gpu-250-cpu"]["embed"] == EMBED
    assert _models(p, "cpu-247") == [LONG, DEFAULT]    # pinned routes first
    assert _models(p, "cpu-246") == [DEFAULT, LONG]
    for n in ("gpu-250-cpu", "cpu-246", "cpu-247"):
        assert len(_models(p, n)) == 2


def test_routes_drive_the_baseline():
    """Changing a route's model changes what the node keeps warm."""
    rules = dict(RULES, naming={"deny_gpu": True, "prefer": "gpu-250-cpu", "model": "qwen3.5:2b"})
    p = W.plan(INSTANCES, W.merge_config(None), rules, ALIASES, {},
               {k: SIZES for k in INSTANCES}, RAM)
    assert "qwen3.5:2b" in _models(p, "gpu-250-cpu")


def test_explicit_node_list_wins():
    p = _plan({"nodes": {"cpu-246": {"models": [CODER, "@default"]}}})
    assert _models(p, "cpu-246") == [CODER, DEFAULT]
    assert p["cpu-246"]["source"] == "node config"


def test_windows_cpu_planned_gpu_router():
    p = _plan(gpu_windows={"gpu-250": 28672})
    assert p["gpu-250"]["models"][0]["num_ctx"] == 28672
    assert all(m["num_ctx"] == 16384 for m in p["cpu-247"]["models"])


def test_memory_budget_drops_what_does_not_fit():
    p = W.plan(INSTANCES, W.merge_config({"nodes": {"cpu-246": {"models": [LONG, CODER]}}}),
               RULES, ALIASES, {}, {k: SIZES for k in INSTANCES}, dict(RAM, **{"cpu-246": 40}))
    assert _models(p, "cpu-246") == [LONG]
    assert p["cpu-246"]["dropped"][0]["model"] == CODER


def test_model_not_on_node_is_not_planned():
    inst = dict(INSTANCES, **{"cpu-246": dict(INSTANCES["cpu-246"], models=[DEFAULT, EMBED])})
    p = W.plan(inst, W.merge_config(None), RULES, ALIASES, {}, {k: SIZES for k in inst}, RAM)
    assert _models(p, "cpu-246") == [DEFAULT]
    assert any(d["model"] == LONG and "not on this node" in d["why"] for d in p["cpu-246"]["dropped"])


# ── scenarios ────────────────────────────────────────────────────────────────
CODING = dict(W.DEFAULT_CONFIG["scenarios"][0], enabled=True,
              models={"gpu": ["qwen2.5-coder:14b"], "cpu": [CODER], "cpu_sibling": [CODER]})


def test_scenario_turns_on_from_demand_and_holds():
    now = 10_000.0
    ev = [(now - i * 30, "loop_coder") for i in range(6)]
    st = W.scenario_states([CODING], ev, {}, {}, now)
    assert st["coding"]["active"] and st["coding"]["hot"]
    # demand gone: still on while it cools ...
    st2 = W.scenario_states([CODING], [], {}, st, now + 300)
    assert st2["coding"]["active"] and not st2["coding"]["hot"]
    assert "cooling" in st2["coding"]["why"]
    # ... and off after hold_s
    st3 = W.scenario_states([CODING], [], {}, st2, now + 300 + CODING["hold_s"])
    assert not st3["coding"]["active"]


def test_scenario_turns_on_from_inflight():
    st = W.scenario_states([CODING], [], {"code": 2}, {}, 1.0)
    assert st["coding"]["active"]


def test_scenario_disabled_or_blocked_stays_off():
    ev = [(1.0, "code")] * 20
    assert not W.scenario_states([dict(CODING, enabled=False)], ev, {}, {}, 2.0)["coding"]["active"]
    st = W.scenario_states([CODING], ev, {}, {}, 2.0, blocked="a census goal is in flight")
    assert not st["coding"]["active"] and "census" in st["coding"]["why"]


def test_coders_into_every_slot():
    """user, 2026-09-28: 'if the coding workload is high it could load coders
    into all available model slots (2x per cpu and 1x per gpu)'."""
    cfg = W.merge_config({"scenarios": [CODING]})
    st = {"coding": {"active": True}}
    p = W.plan(INSTANCES, cfg, RULES, ALIASES, st, {k: SIZES for k in INSTANCES},
               dict(RAM, **{"gpu-250": 32, "gpu-250-cpu": 40}))
    assert _models(p, "gpu-250") == ["qwen2.5-coder:14b"]
    for n in ("cpu-246", "cpu-247", "gpu-250-cpu"):
        assert _models(p, n)[0] == CODER and p[n]["scenarios"] == ["coding"]
    # the embedder stays beside the slots
    assert p["gpu-250-cpu"]["embed"] == EMBED


def test_reserved_model_keeps_its_slot():
    cfg = W.merge_config({"scenarios": [CODING], "nodes": {"cpu-247": {"reserve": [LONG]}}})
    p = W.plan(INSTANCES, cfg, RULES, ALIASES, {"coding": {"active": True}},
               {k: SIZES for k in INSTANCES}, dict(RAM, **{"cpu-247": 64}))
    assert _models(p, "cpu-247") == [LONG, CODER]
    # on the real 50 GB node the coder does not fit beside the 35b: it is
    # dropped and said so, never loaded into a spill
    p = W.plan(INSTANCES, cfg, RULES, ALIASES, {"coding": {"active": True}},
               {k: SIZES for k in INSTANCES}, RAM)
    assert _models(p, "cpu-247") == [LONG]
    assert p["cpu-247"]["dropped"][0]["model"] == CODER


# ── the request path ─────────────────────────────────────────────────────────
def test_request_overrides_keep_the_planned_runner():
    planned = {"cpu-247": {LONG: 16384}}
    o = W.request_overrides(planned, "cpu-247", LONG, want_ctx=4096)
    assert o == {"keep_alive": -1, "num_ctx": 16384}
    assert isinstance(o["keep_alive"], int)            # "-1" is a 400
    # a bigger prompt needs its own window (a reload is unavoidable)
    assert W.request_overrides(planned, "cpu-247", LONG, want_ctx=32768) == {"keep_alive": -1}
    # `x` == `x:latest`
    assert W.request_overrides({"n": {"m:latest": 8192}}, "n", "m", 100)["num_ctx"] == 8192
    assert W.request_overrides(planned, "cpu-246", LONG, 4096) == {}
    # never above what the node can safely hold
    assert "num_ctx" not in W.request_overrides(planned, "cpu-247", LONG, 4096, node_cap=8192)


# ── actions ──────────────────────────────────────────────────────────────────
NOW = 1_800_000_000.0
FOREVER = NOW + 300 * 365 * 86400


def test_actions_load_absent_one_per_node():
    p = _plan()
    acts = W.actions(p, {"cpu-247": []}, busy=(), now=NOW)
    loads = [a for a in acts if a["node"] == "cpu-247" and a["action"] == "load"]
    assert len(loads) == 1 and loads[0]["model"] == LONG and loads[0]["num_ctx"] == 16384


def test_actions_never_rearm_a_model_with_time_left():
    p = _plan()
    run = {"cpu-247": [{"name": LONG, "expires_at_s": NOW + 3600, "context_length": 16384},
                       {"name": DEFAULT, "expires_at_s": FOREVER, "context_length": 16384},
                       {"name": EMBED, "expires_at_s": FOREVER, "context_length": 2048}]}
    assert W.actions(p, run, busy=(), now=NOW) == []


def test_actions_rearm_only_when_about_to_lapse_at_its_own_window():
    p = _plan()
    run = {"cpu-247": [{"name": LONG, "expires_at_s": NOW + 60, "context_length": 8192},
                       {"name": DEFAULT, "expires_at_s": FOREVER, "context_length": 16384},
                       {"name": EMBED, "expires_at_s": FOREVER, "context_length": 2048}]}
    a = W.actions(p, run, busy=(), now=NOW)
    assert a == [{"node": "cpu-247", "action": "rearm", "model": LONG, "num_ctx": 8192}]


def test_actions_release_only_our_own_unplanned():
    p = _plan()
    run = {"cpu-246": [{"name": CODER, "expires_at_s": FOREVER},          # ours, dropped
                       {"name": "gemma3:12b", "expires_at_s": NOW + 200},  # someone else's
                       {"name": DEFAULT, "expires_at_s": FOREVER},
                       {"name": LONG, "expires_at_s": FOREVER},
                       {"name": EMBED, "expires_at_s": FOREVER}]}
    a = W.actions(p, run, busy=(), now=NOW)
    assert a == [{"node": "cpu-246", "action": "release", "model": CODER}]


def test_actions_skip_busy_nodes():
    p = _plan()
    assert not [a for a in W.actions(p, {"cpu-247": []}, busy={"cpu-247"}, now=NOW)
                if a["node"] == "cpu-247"]


def test_spill_needs_proven_speed_or_a_scenario():
    assert not W.spill_ok(0.08, 3.0, False)     # the 9b on an untuned CPU node
    assert not W.spill_ok(0, 3.0, False)        # never measured
    assert W.spill_ok(3.86, 3.0, False)         # gpu-250-cpu, measured 2026-09-28
    assert W.spill_ok(0, 3.0, True)


def test_parse_expiry_handles_ollama_nanoseconds():
    assert W.parse_expiry("2026-09-28T18:40:22.123456789Z") == 1790620822.0
    assert W.parse_expiry("2026-09-28T19:40:22.5+01:00") == 1790620822.0
    assert W.parse_expiry("2319-01-08T18:18:30.1+00:00") - 1790620822.0 > W.FOREVER_AFTER_S
    assert W.parse_expiry("") == 0.0 and W.parse_expiry("soon") == 0.0


def test_busy_nodes_leave_a_working_or_recently_used_node_alone():
    inst = dict(INSTANCES, **{"cpu-246": dict(INSTANCES["cpu-246"], in_use=1)})
    b = W.busy_nodes(inst, {"gpu-250": NOW - 300, "cpu-247": NOW - 300}, NOW)
    assert "cpu-246" in b                      # in flight
    assert "gpu-250" in b                      # the card waits 10 minutes of quiet
    assert "cpu-247" not in b                  # a CPU node 2
    assert "gpu-250-cpu" not in b
    # embeddings hit the CPU nodes every few seconds: recent use is not busy
    assert "cpu-247" not in W.busy_nodes(INSTANCES, {"cpu-247": NOW - 1}, NOW)



def test_one_model_under_two_tags_is_one_model():
    """gpu-250, 2026-09-28: `:latest` and `:9b` of the jaahas model share a
    digest. A resident `:9b` IS the planned `:latest`; no second load, and a
    call naming either tag keeps the planned runner."""
    dg = {DEFAULT + ":latest": "d1", DEFAULT + ":9b": "d1", "qwen3.5:9b": "d2"}
    assert W.tags_of(DEFAULT, dg) == [DEFAULT, DEFAULT + ":9b"]
    rows = W.canonical_rows([{"name": DEFAULT + ":9b", "digest": "d1", "expires_at_s": FOREVER}],
                            [DEFAULT], dg)
    assert rows[0]["name"] == DEFAULT and rows[0]["tag"] == DEFAULT + ":9b"
    p = _plan()
    assert not [a for a in W.actions(p, {"gpu-250": rows}, busy=(), now=NOW) if a["node"] == "gpu-250"]
    pairs = W.planned_pairs_with_tags(p, {"gpu-250": dg})
    assert W.request_overrides(pairs, "gpu-250", DEFAULT + ":9b", 1000)["keep_alive"] == -1
    # different weights stay different
    assert W.canonical_rows([{"name": "qwen3.5:9b", "digest": "d2"}], [DEFAULT], dg)[0]["name"] == "qwen3.5:9b"
