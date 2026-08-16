#!/usr/bin/env python3
"""
Runs INSIDE a dedicated, throwaway container (vera-langgraph:latest).
Builds a minimal LangGraph ReAct agent (langgraph.prebuilt.create_react_agent)
against a real Ollama instance via its OpenAI-compatible surface, executes one
goal in STREAMING mode (graph.stream(..., stream_mode="values") — a native
LangGraph feature: yields the full graph state after every step, not just the
final result), and prints one LANGGRAPH_STEP:<json> line per new message that
appears as the graph runs, then a final LANGGRAPH_RESULT:<json> line — same
two-line-prefix protocol smolagents_entrypoint.py uses, so the host-side
bridge (langgraph_capabilities.py) can stream progress the same way.

Deliberately the opposite paradigm from smolagents: explicit graph of nodes/
edges with real tool-calling (JSON tool-call protocol), not code-as-action.
Kept dependency-free of Vera's own agentic-loop code by design - this file
never imports anything from Vera.
"""
import ast
import json
import os
import sys
import time


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


def _describe_message(m) -> dict:
    """Turn one new LangChain message (yielded via the growing `messages`
    state list) into a small JSON-safe progress dict. An AI message
    requesting tool calls and a tool message returning a result are the
    two genuinely informative "something happened" moments in a ReAct
    graph — surfaced distinctly from a plain AI reply."""
    role = getattr(m, "type", type(m).__name__)
    content = str(getattr(m, "content", "") or "")
    tool_calls = getattr(m, "tool_calls", None) or []
    if role == "ai" and tool_calls:
        calls = "; ".join(f"{tc.get('name')}({tc.get('args')})" for tc in tool_calls)
        text = (content + "\n" if content else "") + f"Calling tool: {calls}"
        return {"kind": "tool_call", "text": text[:1500]}
    if role == "tool":
        name = getattr(m, "name", "") or "tool"
        return {"kind": "tool_result", "text": f"{name} -> {content}"[:1500]}
    if role == "ai":
        return {"kind": "ai", "text": content[:2000]}
    if role == "human":
        return {"kind": "human", "text": f"Task received: {content[:200]}"}
    return {"kind": role, "text": content[:500]}


def main() -> int:
    goal = os.environ.get("GOAL", "").strip()
    base_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    model_id = os.environ.get("OLLAMA_MODEL", "").strip()
    max_steps = int(os.environ.get("LANGGRAPH_MAX_STEPS", "8"))

    if not goal:
        print("LANGGRAPH_RESULT:" + json.dumps({"ok": False, "error": "GOAL env var required"}))
        return 1
    if not base_url or not model_id:
        print("LANGGRAPH_RESULT:" + json.dumps({"ok": False, "error": "OLLAMA_BASE_URL/OLLAMA_MODEL env vars required"}))
        return 1

    t0 = time.time()
    try:
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        @tool
        def calculator(expression: str) -> str:
            """Evaluate a basic arithmetic expression, e.g. "42 * 17"."""
            return _safe_calculator(expression)

        llm = ChatOpenAI(
            model=model_id,
            base_url=f"{base_url}/v1",
            api_key="not-needed",  # pragma: allowlist secret
            temperature=0,
        )
        agent = create_react_agent(llm, tools=[calculator])

        seen = 0
        answer = ""
        tool_steps = 0
        for state in agent.stream(
            {"messages": [{"role": "user", "content": goal}]},
            stream_mode="values",
            config={"recursion_limit": max_steps * 2},
        ):
            messages = state.get("messages", [])
            for m in messages[seen:]:
                info = _describe_message(m)
                if info["kind"] in ("tool_call", "tool_result"):
                    tool_steps += 1
                print("LANGGRAPH_STEP:" + json.dumps(info), flush=True)
                if info["kind"] == "ai":
                    answer = info["text"]
            seen = len(messages)

        out = {
            "ok": True,
            "answer": answer,
            "steps": tool_steps,
            "elapsed_s": round(time.time() - t0, 2),
            "model": model_id,
        }
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_s": round(time.time() - t0, 2)}

    print("LANGGRAPH_RESULT:" + json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
