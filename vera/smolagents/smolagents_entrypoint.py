#!/usr/bin/env python3
"""
smolagents_entrypoint.py — runs INSIDE the vera-smolagents container.

Reads a goal + model config from env vars, runs one smolagents CodeAgent
turn against it, and prints a single JSON result line to stdout prefixed
with SMOLAGENTS_RESULT: — the host-side bridge (smolagents_capabilities.py)
parses that line back out of the container's captured output. Everything
else printed (smolagents' own step-by-step logging) is left as human-
readable trace text above that line for debugging/log inspection.

Deliberately has NO Vera capabilities wired in as tools — this is Phase 2
of the external-agentic-loop plan, which the user explicitly wants
proven as fully independent of Vera's own loop system before any deeper
integration is considered. It answers using its own built-in code-
execution ability only.
"""
import json
import os
import sys
import time
import traceback


def main() -> int:
    goal = os.environ.get("GOAL", "").strip()
    if not goal:
        print(json.dumps({"ok": False, "error": "GOAL env var was empty"}))
        return 1

    base_url = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
    model_id = os.environ.get("OLLAMA_MODEL", "")
    if not base_url or not model_id:
        print("SMOLAGENTS_RESULT:" + json.dumps({
            "ok": False,
            "error": "OLLAMA_BASE_URL / OLLAMA_MODEL not set",
        }))
        return 1

    t0 = time.time()
    try:
        from smolagents import CodeAgent, OpenAIServerModel

        # Ollama exposes an OpenAI-compatible surface at <base>/v1 — no real
        # API key needed, but the client requires a non-empty string.
        model = OpenAIServerModel(
            model_id=model_id,
            api_base=f"{base_url}/v1",
            api_key="not-needed",  # pragma: allowlist secret
        )
        agent = CodeAgent(tools=[], model=model, max_steps=8)

        print(f"[smolagents] goal: {goal[:200]}", file=sys.stderr)
        answer = agent.run(goal)

        # Pull the step-by-step trace out of the agent's own memory so the
        # host side has something to show beyond the bare final answer.
        steps = []
        try:
            for step in getattr(agent, "memory", None).steps if getattr(agent, "memory", None) else []:
                steps.append(str(step)[:800])
        except Exception:
            pass

        elapsed = round(time.time() - t0, 2)
        result = {
            "ok": True,
            "answer": str(answer),
            "steps": steps,
            "elapsed_s": elapsed,
            "model": model_id,
        }
        print("SMOLAGENTS_RESULT:" + json.dumps(result))
        return 0
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        print(f"[smolagents] FAILED after {elapsed}s: {e}", file=sys.stderr)
        traceback.print_exc()
        print("SMOLAGENTS_RESULT:" + json.dumps({
            "ok": False, "error": str(e), "elapsed_s": elapsed,
        }))
        return 1


if __name__ == "__main__":
    sys.exit(main())
