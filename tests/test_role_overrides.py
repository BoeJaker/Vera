"""Per-run role models and effort presets (user, 2026-09-28): an MoE executor
(Qwen-AgentWorld and others), qwen3-coder:30b as the coder - even as a one-click
"bigger coder" - and a max-effort mode that uses the CPUs' larger models.
The MoEs do not fit the 12 GB GPU: `auto` must send them to a CPU node rather
than half-load them onto the GPU and evict its own model.
"""

import asyncio
import inspect
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import role_override_core as R  # noqa: E402

try:
    from Vera.vera import capability_orchestration as O
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    O = M = None

needs_app = pytest.mark.skipif(O is None, reason="app module not importable here")
GB = 2 ** 30


def test_standard_changes_nothing():
    ovr, adj = R.build()
    assert ovr == {} and adj == {"effort": "standard"}


def test_bigger_coder_is_one_click():
    ovr, adj = R.build(effort="bigger-coder")
    assert ovr == {"loop/coder": {"model": R.BIG_CODER, "node": "auto"}}
    assert "plan_style" not in adj


def test_max_effort_spreads_the_big_models_over_both_cpu_nodes():
    ovr, adj = R.build(effort="max", plan_style="stepwise")
    assert ovr["loop/coder"] == {"model": R.BIG_CODER, "node": "cpu-246"}   # 247 keeps the 35B
    assert adj["plan_style"] == "stepwise-reviewed" and adj["critic_route"] == "cpu"
    assert "loop/executor" not in ovr                                     # only if picked
    _, adj2 = R.build(effort="max", plan_style="broad")
    assert "plan_style" not in adj2                                      # broad keeps its briefs


def test_explicit_choices_beat_the_preset():
    ovr, _ = R.build(effort="max", coder_model="qwen2.5-coder:14b", coder_node="gpu",
                     executor_model=R.EXECUTOR_MODELS[0], executor_node="cpu-246")
    assert ovr["loop/coder"] == {"model": "qwen2.5-coder:14b", "node": "gpu"}
    assert ovr["loop/executor"] == {"model": R.EXECUTOR_MODELS[0], "node": "cpu-246"}
    assert R.build(executor_model="x", executor_node="sideways")[0]["loop/executor"]["node"] == "auto"


def test_auto_puts_a_model_on_the_gpu_only_when_it_fits():
    usable = int(12 * GB * 0.86)
    assert R.resolve_node("auto", model_bytes=int(4.7 * GB), gpu_usable_bytes=usable) == "gpu"
    assert R.resolve_node("auto", model_bytes=int(21.7 * GB), gpu_usable_bytes=usable) == "cpu-247"
    assert R.resolve_node("auto", model_bytes=0, gpu_usable_bytes=usable) == "cpu-247"   # unknown: never gamble
    assert R.resolve_node("cpu-246", model_bytes=int(4 * GB), gpu_usable_bytes=usable) == "cpu-246"


def _nodes():
    return {"gpu-250": {"has_gpu": True, "enabled": True, "status": "online"},
            "cpu-246": {"has_gpu": False, "enabled": True, "status": "online"},
            "cpu-247": {"has_gpu": False, "enabled": True, "status": "online"}}


@needs_app
def test_the_run_override_applies_only_to_its_role(monkeypatch):
    sizes = {"hf.co/unsloth/Qwen-AgentWorld-35B-A3B-GGUF:MXFP4_MOE": int(21.7 * GB),
             "qwen2.5:7b": int(4.7 * GB)}

    async def fake_size(iid, model):
        return sizes.get(model, 0)

    monkeypatch.setattr(O, "OLLAMA_INSTANCES", _nodes())
    monkeypatch.setattr(O, "ollama_model_disk_size", fake_size)
    monkeypatch.setattr(O, "_NODE_USABLE_VRAM", {"gpu-250": int(12 * GB * 0.95)})

    async def go():
        O.RUN_ROLE_OVERRIDES.set({
            "loop/executor": {"model": "hf.co/unsloth/Qwen-AgentWorld-35B-A3B-GGUF:MXFP4_MOE", "node": "auto"},
            "loop/coder": {"model": "qwen2.5:7b", "node": "auto"}})
        ex = await O._run_role_override("loop", "executor")
        co = await O._run_role_override("loop", "coder")
        pl = await O._run_role_override("loop", "planner")
        other = await O._run_role_override("ide", "executor")
        return ex, co, pl, other

    ex, co, pl, other = asyncio.run(go())
    assert ex["instance_id"] == "cpu-247" and ex["prefer_gpu"] is False     # did not fit
    assert co["node"] == "gpu" and co["instance_id"] is None and co["prefer_gpu"] is True
    assert pl is None and other is None


@needs_app
def test_a_run_without_overrides_sees_none():
    async def go():
        return await O._run_role_override("loop", "executor")
    assert asyncio.run(go()) is None                    # a fresh context: no leak


@needs_app
def test_ollama_generate_and_the_loop_are_wired():
    gen = inspect.getsource(O.ollama_generate)
    i_ovr = gen.index("_rovr = await _run_role_override(profile, role)")
    i_rule = gen.index("role_rule = resolve_role_rule(profile, role)")
    assert i_ovr < i_rule                               # applied before routing resolves
    assert 'model = _rovr["model"]' in gen and "if not instance_id:" in gen
    v6 = inspect.getsource(M.cap_dag_agent_loop_v6)
    assert "_orch.RUN_ROLE_OVERRIDES.set(_role_overrides or None)" in v6
    assert v6.index("RUN_ROLE_OVERRIDES.set(") < v6.index('_plan_style_req = str(plan_style or "auto")')
    sig = inspect.signature(M.cap_dag_agent_loop_v6).parameters
    assert sig["effort"].default == "standard" and sig["executor_node"].default == "auto"
    assert "agent_loop_v6.role_models" in inspect.getsource(M)             # forwarded to the UI
