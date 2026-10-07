"""GPU off means off; the chat's model and instance reach every request a turn makes.

Owner, 2026-10-02: "in the chat ui i set the model and instance using the dropdowns and it
still went to the gpu node (the default) i even tried turning 2 gpu toggles off in the chat
settings and it still went to the gpu?"

The router is driven for real (pick_instance over a stubbed node table); the chat page,
the chat stream and the loop are read as source for the wiring.
"""
import contextvars
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from Vera.vera import capability_orchestration as O
except Exception:                                    # pragma: no cover
    O = None

needs_app = pytest.mark.skipif(O is None, reason="app module not importable here")

CHAT = (ROOT / "vera" / "chat" / "chat_panel.html").read_text(encoding="utf-8")
AGENTS = (ROOT / "vera" / "agents" / "agents.py").read_text(encoding="utf-8")
LOOP = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def _nodes(cpu_status="online"):
    return {
        "gpu-250": {"has_gpu": True, "enabled": True, "status": "online", "in_use": 0,
                    "priority": 0, "models": ["qwen2.5:0.5b"]},
        "cpu-246": {"has_gpu": False, "enabled": True, "status": cpu_status, "in_use": 3,
                    "priority": 1, "models": ["qwen2.5:0.5b"]},
        "cpu-247": {"has_gpu": False, "enabled": True, "status": cpu_status, "in_use": 3,
                    "priority": 2, "models": ["qwen2.5:0.5b"]},
    }


def _in_a_request(fn):
    """Run fn in a fresh context, as each request runs: what one sets never reaches the next."""
    return contextvars.copy_context().run(fn)


@needs_app
def test_prefer_gpu_false_alone_still_lands_on_an_idle_gpu(monkeypatch):
    """Why NO_GPU exists: "do not prefer" picks the least busy node, and that is the GPU here."""
    monkeypatch.setattr(O, "_inflight_sweep", lambda: None)
    monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes())
    assert _in_a_request(lambda: O.pick_instance(prefer_gpu=False, model="qwen2.5:0.5b")) == "gpu-250"


@needs_app
def test_gpu_off_keeps_every_pick_off_the_gpu(monkeypatch):
    monkeypatch.setattr(O, "_inflight_sweep", lambda: None)
    monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes())

    def turn():
        O.NO_GPU.set(True)
        return [O.pick_instance(prefer_gpu=p, model="qwen2.5:0.5b", job_type=j)
                for p in (False, True) for j in ("", "chat", "loop_executor")]
    assert all(pick in ("cpu-246", "cpu-247") for pick in _in_a_request(turn))
    # and the next request is not touched by it
    assert _in_a_request(lambda: O.NO_GPU.get()) is False


@needs_app
def test_a_pinned_instance_still_wins_and_a_dead_cpu_pool_falls_back(monkeypatch):
    monkeypatch.setattr(O, "_inflight_sweep", lambda: None)
    monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes())

    def pinned():
        O.NO_GPU.set(True)
        return O.pick_instance(instance_id="gpu-250", model="qwen2.5:0.5b")
    assert _in_a_request(pinned) == "gpu-250"
    monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes(cpu_status="offline"))

    def no_cpu():
        O.NO_GPU.set(True)
        return O.pick_instance(model="qwen2.5:0.5b")
    assert _in_a_request(no_cpu) == "gpu-250"          # serving beats failing


def test_the_chat_stream_takes_the_toggle_both_ways_and_sets_no_gpu():
    assert 'if "prefer_gpu" in body:       agent.prefer_gpu  = bool(body.get("prefer_gpu"))' in AGENTS
    assert 'if body.get("prefer_gpu"):     agent.prefer_gpu  = True' not in AGENTS
    assert re.search(r'if body\.get\("no_gpu"\):\s*_orch\.NO_GPU\.set\(True\)', AGENTS)


def test_the_loop_sets_no_gpu_before_its_run_starts():
    i = LOOP.index('@APP.post("/workshop/agent_loop/stream")')
    j = LOOP.index('_orch.NO_GPU.set(True)', i)
    assert j - i < 3000


def test_every_chat_request_carries_the_configs_route():
    assert "function _routeFields(a){" in CHAT and "no_gpu:!on" in CHAT
    # the continuation after a capability and a stepwise step
    assert "message:prompt,history:JSON.stringify(HISTORY.slice(-6)),session_id:SID,..._routeFields(a)," in CHAT
    assert "message:stepPrompt,history:[],session_id:SID,..._routeFields(a)," in CHAT
    # the reply: the toggle as shown, not OR'd with the agent's default
    assert "prefer_gpu:_gpuOn(a), no_gpu:!_gpuOn(a)," in CHAT
    assert "cfgGpu')?.checked||(a?.prefer_gpu)" not in CHAT
    # the loop: the chat's instance when the loop pane names none, either toggle off keeps it off
    assert "|| document.getElementById('cfgInst')?.value" in CHAT
    assert "no_gpu: !(document.getElementById('loopPreferGpu')?.checked!==false && _gpuOn(a))," in CHAT
