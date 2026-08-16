#!/usr/bin/env python3
"""
Runs INSIDE a dedicated, throwaway container (vera-pydanticai:latest).
Builds a minimal PydanticAI agent against a real Ollama instance via its
OpenAI-compatible surface, executes one goal in STREAMING mode
(agent.iter(goal) — a native PydanticAI feature: an async context manager
that async-iterates the agent's graph node-by-node, a third distinct
streaming shape from smolagents' generator and LangGraph's .stream()), and
prints one BRIDGE_STEP:<json> line per node worth surfacing, then a final
BRIDGE_RESULT:<json> line — the standardized protocol agentbridge_runtime.py
(host side) reads, shared by every container-based bridge.

Deliberately the THIRD distinct paradigm alongside smolagents (code-as-
action) and LangGraph (message-graph, JSON tool-calls): PydanticAI is
typed/schema-first — tool args and results flow through Pydantic validation,
and node types are richer (a ThinkingPart is a first-class part of the
model's response, not something bolted on). Kept dependency-free of Vera's
own agentic-loop code by design - this file never imports anything from Vera.
"""
import ast
import asyncio
import json
import os
import sys
import time
from typing import Optional


def _safe_calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression (+ - * / ** ( ) only). Use this
    for any arithmetic instead of guessing the answer yourself."""
    allowed = set("0123456789+-*/(). ")
    if not set(expression) <= allowed:
        return f"error: expression contains disallowed characters: {expression!r}"
    try:
        node = ast.parse(expression, mode="eval")
        for sub in ast.walk(node):
            if not isinstance(sub, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                                     ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
                                     ast.USub, ast.UAdd)):
                return f"error: disallowed expression: {expression!r}"
        return str(eval(compile(node, "<calc>", "eval"), {"__builtins__": {}}, {}))
    except Exception as e:
        return f"error: {e}"


def _describe_node(node) -> Optional[dict]:
    """Turn one PydanticAI graph node into a small JSON-safe progress dict.
    Returns None for nodes with nothing new to show (the first
    ModelRequestNode just echoes the user prompt already shown by
    UserPromptNode; a CallToolsNode with neither a tool call, text, nor
    thinking part shouldn't normally happen but is handled gracefully)."""
    kind = type(node).__name__

    if kind == "UserPromptNode":
        return {"kind": "task", "text": f"Task received: {str(getattr(node, 'user_prompt', ''))[:200]}"}

    if kind == "ModelRequestNode":
        request = getattr(node, "request", None)
        parts = getattr(request, "parts", []) if request else []
        tool_returns = [p for p in parts if type(p).__name__ == "ToolReturnPart"]
        if not tool_returns:
            return None
        texts = [f"{getattr(p, 'tool_name', 'tool')} -> {getattr(p, 'content', '')}"
                 for p in tool_returns]
        return {"kind": "tool_result", "text": "\n".join(texts)[:1500]}

    if kind == "CallToolsNode":
        resp = getattr(node, "model_response", None)
        parts = getattr(resp, "parts", []) if resp else []
        thinking, calls, texts = [], [], []
        for p in parts:
            ptype = type(p).__name__
            if ptype == "ThinkingPart":
                thinking.append(str(getattr(p, "content", "")))
            elif ptype == "ToolCallPart":
                calls.append(f"{getattr(p, 'tool_name', '?')}({getattr(p, 'args', '')})")
            elif ptype == "TextPart":
                texts.append(str(getattr(p, "content", "")))
        prefix = ("Thinking: " + " ".join(thinking) + "\n") if thinking else ""
        if calls:
            return {"kind": "tool_call", "text": (prefix + "Calling tool: " + "; ".join(calls))[:1500]}
        if texts:
            return {"kind": "ai", "text": (prefix + " ".join(texts))[:2000]}
        if thinking:
            return {"kind": "thinking", "text": " ".join(thinking)[:1500]}
        return None

    if kind == "End":
        data = getattr(node, "data", None)
        output = getattr(data, "output", "") if data else ""
        return {"kind": "final", "text": str(output)[:2000]}

    return {"kind": kind.lower(), "text": str(node)[:500]}


async def _run(goal: str, base_url: str, model_id: str, max_steps: int) -> dict:
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    def calculator(expression: str) -> str:
        """Evaluate a basic arithmetic expression, e.g. "42 * 17"."""
        return _safe_calculator(expression)

    model = OpenAIChatModel(
        model_id,
        provider=OpenAIProvider(base_url=f"{base_url}/v1", api_key="not-needed"),  # pragma: allowlist secret
    )
    agent = Agent(model, tools=[calculator])

    answer = ""
    tool_steps = 0
    node_count = 0
    async with agent.iter(goal) as run:
        async for node in run:
            node_count += 1
            if node_count > max_steps * 4:
                # Graph nodes run several-per-turn (request/response pairs) —
                # a generous multiple of max_steps as a runaway guard, since
                # PydanticAI's agent.iter() has no built-in step cap of its own.
                break
            info = _describe_node(node)
            if info is None:
                continue
            if info["kind"] in ("tool_call", "tool_result"):
                tool_steps += 1
            print("BRIDGE_STEP:" + json.dumps(info), flush=True)
            if info["kind"] == "ai":
                answer = info["text"]
            elif info["kind"] == "final":
                answer = info["text"]

    return {"answer": answer, "steps": tool_steps}


def main() -> int:
    goal = os.environ.get("GOAL", "").strip()
    base_url = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
    model_id = os.environ.get("OLLAMA_MODEL", "").strip()
    max_steps = int(os.environ.get("PYDANTICAI_MAX_STEPS", "8"))

    if not goal:
        print("BRIDGE_RESULT:" + json.dumps({"ok": False, "error": "GOAL env var required"}))
        return 1
    if not base_url or not model_id:
        print("BRIDGE_RESULT:" + json.dumps({"ok": False, "error": "OLLAMA_BASE_URL/OLLAMA_MODEL env vars required"}))
        return 1

    t0 = time.time()
    try:
        out = asyncio.run(_run(goal, base_url, model_id, max_steps))
        result = {
            "ok": True,
            "answer": out["answer"],
            "steps": out["steps"],
            "elapsed_s": round(time.time() - t0, 2),
            "model": model_id,
        }
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "elapsed_s": round(time.time() - t0, 2)}

    print("BRIDGE_RESULT:" + json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
