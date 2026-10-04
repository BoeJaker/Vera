"""The loop's controller and planner can run on a chosen model and node for one run (user
2026-10-04: let the MoE / specialised agentic models drive the loop), like the executor
and coder already could."""
import asyncio
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("roc", ROOT / "vera" / "dag" / "role_override_core.py")
O = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(O)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

AW = "hf.co/unsloth/Qwen-AgentWorld-35B-A3B-GGUF:MXFP4_MOE"


def test_controller_covers_controller_and_planner():
    out, adj = O.build(controller_model=AW, controller_node="cpu-246")
    assert out["loop/controller"] == {"model": AW, "node": "cpu-246"}
    assert out["loop/planner"] == {"model": AW, "node": "cpu-246"}
    assert "loop/executor" not in out and adj["effort"] == "standard"


def test_no_controller_means_no_override_and_others_unchanged():
    out, _ = O.build(executor_model="qwen3.6:35b-a3b", executor_node="cpu-247")
    assert set(out) == {"loop/executor"}
    out, _ = O.build(controller_model="qwen3.6:35b-a3b", controller_node="weird")
    assert out["loop/controller"]["node"] == "auto"
    assert "controller" in O.ROLES and "planner" in O.ROLES


def test_the_loop_and_route_carry_it():
    sig = SRC[SRC.index("async def cap_dag_agent_loop_v6("):]
    sig = sig[:sig.index("\n):")]
    assert 'controller_model:   str  = "",' in sig and 'controller_node:    str  = "auto",' in sig
    assert "plan_style=plan_style, controller_model=controller_model," in SRC
    assert 'v6_controller_model  = (body.get("controller_model", "") or "").strip()' in SRC
    assert "controller_model=v6_controller_model, controller_node=v6_controller_node," in SRC


try:
    from vera import capability_orchestration as CO
except Exception:                                   # pragma: no cover
    CO = None


@pytest.mark.skipif(CO is None, reason="app module not importable here")
def test_a_controller_call_resolves_to_the_override():
    out, _ = O.build(controller_model="qwen3.6:35b-a3b", controller_node="cpu-247")
    tok = CO.RUN_ROLE_OVERRIDES.set(out)
    try:
        got = (CO.RUN_ROLE_OVERRIDES.get() or {}).get(O.key("controller", "loop"))
        assert got == {"model": "qwen3.6:35b-a3b", "node": "cpu-247"}
    finally:
        CO.RUN_ROLE_OVERRIDES.reset(tok)
