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
    DEFAULT_STUCK_S, Runner, is_active, parse_model_from_cmdline,
    parse_port_from_cmdline, reap_plan,
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
