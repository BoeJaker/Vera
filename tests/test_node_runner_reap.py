"""A runner nothing is waiting for must be stopped; a busy one must not.

Pins edge/node_runner_core.py — the decision shared by the node agent
(edge/vera_node_agent.py) and Vera's reaper (workers/node_agent_capabilities.py).

The incident (2026-09-16, cpu-247 / CT130): an ollama `llama-server` at 1200%
CPU — twelve of the Proxmox host's forty-eight cores — 7.9GB resident, 16h03m of
accumulated CPU time, started 15:53. Vera's orchestrator restarted at 16:23, so
the client was gone: no ESTABLISHED connection from the Vera host to any ollama
node, and the socket the node still showed was half-open. `llama-server` cannot
tell; it only finds out when it finishes and tries to write, and at the ~0.05
tok/s a CPU node manages against a 24,576-token window that was days away.

The danger in fixing this is killing the wrong thing, so these tests are mostly
about what must NOT be reaped.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))

from node_runner_core import (  # noqa: E402
    DEFAULT_STUCK_S, DispatchProbe, Runner, dispatch_finding, is_active, is_embedding_model, is_node_saturated, probe_call,
    is_dispatch_wedged, parse_model_from_cmdline, parse_port_from_cmdline,
    reap_plan,
)

STUCK_S = DEFAULT_STUCK_S  # 1800s = 2x OLLAMA_GEN_TIMEOUT


def _r(pid=1, *, state="R", cpu=600.0, age=3600.0, model="blob", node="cpu-247"):
    return Runner(pid=pid, model=model, state=state, cpu_seconds=cpu,
                  age_s=age, node=node)


# ── the incident itself ──────────────────────────────────────────────────────

def test_the_cpu_247_runner_is_reaped():
    """The real one: 80 minutes old, 16h03m of CPU across 12 cores, state R."""
    r = _r(pid=545984, state="R", cpu=57822.0, age=4800.0)
    v = reap_plan([r], stuck_s=STUCK_S)
    assert [x.pid for x in v.stuck] == [545984]
    assert "timed out" in v.reasons[545984]


# ── what must NOT be reaped ──────────────────────────────────────────────────

def test_a_resident_but_idle_runner_is_left_alone():
    """cpu-246 holds two models on keep_alive at 0% CPU. That is ollama working
    as intended, not a wedge — reaping it would evict a warm model."""
    v = reap_plan([_r(pid=7, state="S", cpu=74000.0, age=99999.0)], stuck_s=STUCK_S)
    assert v.stuck == []
    assert "idle" in v.reasons[7]


def test_a_long_but_legitimate_generation_is_left_alone():
    """Under the bound, someone may still be waiting. LLM duration is unbounded;
    only the CLIENT timeout makes it safe to judge."""
    v = reap_plan([_r(pid=8, age=STUCK_S - 1, cpu=600.0)], stuck_s=STUCK_S)
    assert v.stuck == []


def test_a_freshly_started_runner_is_left_alone():
    v = reap_plan([_r(pid=9, age=5.0, cpu=4.0)], stuck_s=STUCK_S)
    assert v.stuck == []


def test_an_old_runner_doing_no_work_is_left_alone():
    """Old AND resident is not the same as old AND computing."""
    v = reap_plan([_r(pid=10, state="S", cpu=0.5, age=999999.0)], stuck_s=STUCK_S)
    assert v.stuck == []


def test_state_R_with_trivial_cpu_is_not_yet_active():
    """A single sample can catch a runner mid-tick; real CPU time is the second
    signal that stops one sample from condemning it."""
    assert is_active(_r(state="R", cpu=1.0)) is False
    assert is_active(_r(state="R", cpu=120.0)) is True


def test_vera_still_waiting_protects_a_runner_at_any_age():
    """The hook for an in-flight registry: if Vera knows it is waiting, age is
    irrelevant — it is not orphaned, it is slow."""
    v = reap_plan([_r(pid=11, age=99999.0, cpu=99999.0)], stuck_s=STUCK_S,
                  protected_pids=[11])
    assert v.stuck == []
    assert "still waiting" in v.reasons[11]


def test_disabled_when_threshold_is_zero():
    v = reap_plan([_r(pid=12, age=99999.0, cpu=99999.0)], stuck_s=0)
    assert v.stuck == [] and len(v.kept) == 1


def test_malformed_runners_are_skipped_not_fatal():
    v = reap_plan([None, _r(pid=0), _r(pid=13, age=99999.0)], stuck_s=STUCK_S)
    assert [x.pid for x in v.stuck] == [13]


# ── mixed estate ─────────────────────────────────────────────────────────────

def test_only_the_stuck_one_is_picked_from_a_realistic_estate():
    runners = [
        _r(pid=657128, state="S", cpu=74100.0, age=200000.0, node="cpu-246"),
        _r(pid=708540, state="S", cpu=380.0, age=200000.0, node="cpu-246"),
        _r(pid=1617436, state="R", cpu=300.0, age=500.0, node="gpu-250"),
        _r(pid=545984, state="R", cpu=57822.0, age=4800.0, node="cpu-247"),
    ]
    v = reap_plan(runners, stuck_s=STUCK_S)
    assert [x.pid for x in v.stuck] == [545984]
    assert len(v.kept) == 3


# ── cmdline parsing (all the node can see is the blob) ───────────────────────

def test_parses_model_blob_and_port_from_a_real_cmdline():
    cmd = ("/usr/local/lib/ollama/llama-server --model "
           "/.ollama/models/blobs/sha256-c0ba7beb68fd3fe47891bd549486d38dcf62d00817296"
           " --port 36447 --host 127.0.0.1 --no-webui")
    assert parse_model_from_cmdline(cmd).startswith("sha256-c0ba7beb")
    assert parse_port_from_cmdline(cmd) == 36447


def test_parsing_is_safe_on_junk():
    assert parse_model_from_cmdline("") == ""
    assert parse_model_from_cmdline("llama-server --model") == ""
    assert parse_port_from_cmdline("llama-server --port notanint") == 0


# ── the OTHER failure: a healthy runner nothing dispatches to ────────────────
# gpu-250, 2026-09-20. ollama's scheduler deadlocked: it accepted generations
# and never handed them to its own runner, which stayed idle, healthy and
# resident. Every check Vera had said "online, idle" — including this reaper,
# correctly by its own rule — while chat produced nothing for eight hours.

def _p(**kw):
    base = dict(node="gpu-250", metadata_ok=True, resident_models=1,
                dispatched=False, probe_s=25.0)
    base.update(kw)
    return DispatchProbe(**base)


def test_the_incident_is_recognised():
    """Metadata answers, a model is resident, generation never comes back."""
    assert is_dispatch_wedged(_p()) is True


def test_a_node_that_is_simply_down_is_not_called_wedged():
    """`unreachable` already reports that, and the remedy is different."""
    assert is_dispatch_wedged(_p(metadata_ok=False)) is False


def test_a_cold_node_with_nothing_loaded_is_not_called_wedged():
    """With no model resident a slow reply is a model LOAD, which is normal."""
    assert is_dispatch_wedged(_p(resident_models=0)) is False


def test_an_unprobed_node_is_unknown_not_wedged():
    """None must never read as False — that would flag every skipped node."""
    assert is_dispatch_wedged(_p(dispatched=None)) is False
    assert is_dispatch_wedged(None) is False


def test_a_skipped_probe_is_never_a_finding():
    """We skip while a census or the GPU gate is busy; that is not evidence."""
    assert is_dispatch_wedged(_p(skipped="census in flight (goal 3)")) is False


def test_a_healthy_node_is_not_wedged():
    assert is_dispatch_wedged(_p(dispatched=True, probe_s=0.4)) is False


def test_finding_is_none_when_healthy():
    assert dispatch_finding(_p(dispatched=True)) is None
    assert dispatch_finding(None) is None


def test_finding_names_the_node_and_forbids_killing_the_runner():
    """The obvious reading of 'runner idle, node not serving' is to kill the
    runner. That is wrong: it loses the loaded model and leaves the deadlock."""
    f = dispatch_finding(_p())
    assert f["node"] == "gpu-250"
    assert f["severity"] == "crit"
    remedy = f["remedy"].lower()
    assert "restart ollama" in remedy
    assert "not kill" in remedy or "never kill" in remedy


def test_finding_tells_the_reader_how_to_rule_out_a_merely_busy_node():
    """From outside, a saturated node and a deadlocked one look identical — and
    the remedies are opposite, so the finding must not assert just one."""
    f = dispatch_finding(_p())
    disc = f["discriminator"].lower()
    assert "/health" in disc and "slot" in disc
    assert "saturated" in disc
    assert "wedged" in disc


def test_finding_warns_that_metadata_checks_cannot_see_this():
    """The whole reason it went unreported for eight hours."""
    detail = dispatch_finding(_p())["detail"].lower()
    assert "/api/ps" in detail
    assert "bypass" in detail or "online" in detail


def test_to_dict_carries_the_verdict():
    assert _p().to_dict()["wedged"] is True
    assert _p(dispatched=True).to_dict()["wedged"] is False


def test_a_node_that_answered_is_never_wedged_however_it_answered():
    """`dispatched` means ANSWERED, not answered-200.

    The first cut asserted status==200 and flagged a perfectly healthy gpu-250
    as wedged in 0.06s: /api/ps happened to have nomic-embed-text resident, and
    ollama correctly refused to /api/generate with an embedding model. A fast
    refusal is proof the scheduler is alive — it is the opposite of a wedge.
    """
    answered_fast = _p(dispatched=True, probe_s=0.06)
    assert is_dispatch_wedged(answered_fast) is False
    assert dispatch_finding(answered_fast) is None


def test_probe_model_is_reported_so_a_finding_can_be_checked():
    """Without it, 'node X did not answer' cannot be reproduced by hand."""
    assert _p(probe_model="qwen3.5:9b").to_dict()["probe_model"] == "qwen3.5:9b"


# ── choosing what to ask a node ──────────────────────────────────────────────

_EMBED = {"name": "nomic-embed-text:latest",
          "details": {"family": "nomic-bert", "families": ["nomic-bert"]}}
_GEN = {"name": "jaahas/qwen3.5-uncensored:9b",
        "details": {"family": "qwen35", "families": ["qwen35"]}}


def test_an_embedding_model_is_recognised():
    assert is_embedding_model(_EMBED) is True
    assert is_embedding_model(_GEN) is False
    assert is_embedding_model({}) is False
    assert is_embedding_model(None) is False


def test_a_generative_model_is_preferred_even_when_listed_second():
    """This is the actual gpu-250 case: /api/ps had the embed model first."""
    model, path, payload = probe_call([_EMBED, _GEN])
    assert model == _GEN["name"]
    assert path == "/api/generate"
    assert payload["options"]["num_predict"] == 1


def test_the_probe_never_sends_num_ctx():
    """Sending one would force a runner reload if it differed from the resident
    window — the exact fault this whole diagnosis started from."""
    _, _, payload = probe_call([_GEN])
    assert "num_ctx" not in (payload.get("options") or {})


def test_an_embedding_only_node_is_probed_with_embed_not_generate():
    """Asking it to generate is a 4xx about the MODEL, which proves nothing."""
    model, path, payload = probe_call([_EMBED])
    assert model == _EMBED["name"]
    assert path == "/api/embed"
    assert "input" in payload


def test_probe_call_is_safe_on_junk():
    for bad in (None, [], [None], [{}], ["nope"]):
        model, path, payload = probe_call(bad)
        assert model == "" and path == "" and payload == {}


# ── saturated is NOT wedged: same symptom, opposite remedy ───────────────────
# 2026-09-20: cpu-246 and cpu-247 failed a 150s probe while their embedding
# runner held ~12 cores of a 48-core host at load 77. Calling that a deadlock
# sends someone to restart ollama, which discards the work in flight and the
# queue rebuilds at once. Only gpu-250 was genuinely deadlocked — its runner
# was at 0% CPU with its slot idle.

def test_a_busy_runner_means_saturated_not_wedged():
    p = _p(runner_busy=True, runner_cpu_s=25513.0)
    assert is_node_saturated(p) is True
    assert is_dispatch_wedged(p) is False


def test_an_idle_runner_that_will_not_answer_is_wedged():
    p = _p(runner_busy=False, runner_cpu_s=2637.0)
    assert is_dispatch_wedged(p) is True
    assert is_node_saturated(p) is False


def test_unknown_runner_state_still_reads_as_wedged():
    """Without the agent we cannot prove it is busy; the probe failing with a
    resident model is still the stronger signal. `runner_busy` defaults None."""
    assert is_dispatch_wedged(_p()) is True


def test_a_node_that_answered_is_neither():
    p = _p(dispatched=True, runner_busy=True)
    assert is_dispatch_wedged(p) is False
    assert is_node_saturated(p) is False


def test_the_two_findings_carry_opposite_remedies():
    wedged = dispatch_finding(_p(runner_busy=False))
    saturated = dispatch_finding(_p(runner_busy=True, runner_cpu_s=25513.0))
    assert wedged["kind"] == "wedged"
    assert saturated["kind"] == "saturated"
    assert "restart ollama" in wedged["remedy"].lower()
    assert "not restart" in saturated["remedy"].lower()
    assert "shed load" in saturated["remedy"].lower()
    # A restart is the wrong move on a loaded box, so it must not be suggested.
    assert saturated["severity"] == "warn" and wedged["severity"] == "crit"


def test_saturated_finding_warns_that_loadavg_lies_in_a_container():
    """All three nodes reported the same load average because they are LXCs on
    one host — the number that made this look like three separate failures."""
    d = dispatch_finding(_p(runner_busy=True))["detail"].lower()
    assert "loadavg" in d or "load" in d
    assert "host" in d


def test_to_dict_separates_the_two():
    d = _p(runner_busy=True).to_dict()
    assert d["saturated"] is True and d["wedged"] is False
    d2 = _p(runner_busy=False).to_dict()
    assert d2["wedged"] is True and d2["saturated"] is False


def test_a_wedged_node_does_not_make_its_idle_runner_reapable():
    """The safety property. The runner is the part that still works, and
    reap_plan must keep it for exactly the reason it always did: idle."""
    idle = _r(pid=2478281, state="S", cpu=2637.0, age=36074.0, node="gpu-250")
    v = reap_plan([idle], stuck_s=STUCK_S)
    assert v.stuck == []
    assert idle in v.kept
    assert "idle" in v.reasons[idle.pid]
