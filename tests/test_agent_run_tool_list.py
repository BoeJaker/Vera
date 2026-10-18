"""S5 (part) / DEEP-04 §6.2: AgentRunner.run puts the agent's tool list in its system prompt.

run() imported `vera_dag_store`, a module that does not exist; the ImportError was swallowed, so Telegram, agent.chat
and agent.consult replies never saw their tools. The test stops run() at its first model-bound step (the context
sizing call that receives the finished messages). Runs in the gate container."""
import asyncio
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
import Vera.vera.agents.agents as ag  # noqa: E402
import Vera.vera.dag.dag_store  # noqa: E402,F401  (registers CAP_INDEX under the module's names)


class Stop(Exception):
    pass


def test_run_lists_the_agents_tools(monkeypatch):
    seen = {}

    async def stop_here(messages, *a, **kw):
        seen["messages"] = messages
        raise Stop()

    async def ctx(*a, **kw):
        return 4096

    async def no_rag(*a, **kw):
        return []
    monkeypatch.setattr(ag, "_chat_num_ctx", stop_here)
    monkeypatch.setattr(ag, "pick_instance", lambda **kw: "cpu-test")
    monkeypatch.setattr(ag, "_vllm_chat_target", lambda *a, **kw: None)
    monkeypatch.setattr(ag, "_vllm_standin_target", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "effective_num_ctx", ctx)
    monkeypatch.setattr(ag, "agent_rag_retrieve", no_rag, raising=False)
    tok = orch.SUPPRESS_MEMORY_INJECT.set(True)
    try:
        agent = ag.AgentRecord(id="s5", name="s5", domain_caps=["obs.provenance"], tool_mode="")
        try:
            asyncio.run(ag.AgentRunner().run(agent, "hello"))
        except Stop:
            pass
    finally:
        orch.SUPPRESS_MEMORY_INJECT.reset(tok)
    system = next(m["content"] for m in seen["messages"] if m["role"] == "system")
    assert "Available tools you may reference:" in system and "obs.provenance" in system


def test_no_module_imports_the_nonexistent_vera_dag_store():
    hits = []
    for base, _d, files in os.walk(os.path.join(ROOT, "vera")):
        for n in files:
            if n.endswith(".py"):
                p = os.path.join(base, n)
                if "vera_dag_store import" in open(p, encoding="utf-8", errors="ignore").read():
                    hits.append(os.path.relpath(p, ROOT))
    assert not hits, hits
