#!/usr/bin/env python3
"""
smolagents_entrypoint.py — runs INSIDE the vera-smolagents container.

Reads a goal + model config from env vars, runs one smolagents CodeAgent
turn against it in STREAMING mode (agent.run(goal, stream=True) — a real
smolagents 1.x feature, not homegrown: it returns a generator yielding each
TaskStep/PlanningStep/ActionStep/FinalAnswerStep as it's produced, instead
of blocking until the whole run finishes). Each step is printed immediately
as its own SMOLAGENTS_STEP:<json> line (flush=True — unbuffered, since the
host side reads this line-by-line as it arrives, not after EOF), so the
host-side bridge (smolagents_capabilities.py) can emit a live progress event
per step rather than going quiet until the whole container exits. The final
SMOLAGENTS_RESULT:<json> line is still printed last, unchanged in shape, so
anything reading only that line (or the old blocking behaviour) keeps
working.

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
from typing import Optional


def _describe_step(step) -> Optional[dict]:
    """Turn one smolagents streamed step object into a small JSON-safe dict
    with a human-readable `text` — the thing actually worth showing the user
    as it happens, not the full step object (which carries raw model-message
    history, unneeded here and not reliably JSON-serializable). Returns None
    for step-internal sub-events (ToolCall, ActionOutput — smolagents'
    stream=True yields these too, INSIDE a single ActionStep, before the
    ActionStep itself) whose content is already covered more cleanly by the
    ActionStep that follows them — surfacing both would just show the same
    code/result twice in slightly different shapes."""
    kind = type(step).__name__
    if kind == "TaskStep":
        return {"kind": "task", "text": f"Task received: {str(getattr(step, 'task', ''))[:200]}"}
    if kind == "PlanningStep":
        plan = str(getattr(step, "plan", "") or "")
        return {"kind": "planning", "text": "Planning:\n" + plan[:600]}
    if kind == "ActionStep":
        parts = []
        code = getattr(step, "code_action", None)
        if code:
            parts.append("Code:\n" + str(code)[:800])
        obs = getattr(step, "observations", None)
        if obs:
            parts.append("Observation: " + str(obs)[:500])
        err = getattr(step, "error", None)
        if err:
            parts.append("Error: " + str(err)[:500])
        return {
            "kind": "action",
            "step_number": getattr(step, "step_number", None),
            "text": "\n".join(parts) or "(step executed, no code/observation captured)",
        }
    if kind == "FinalAnswerStep":
        return {"kind": "final", "text": str(getattr(step, "output", ""))[:2000]}
    if kind in ("ToolCall", "ActionOutput", "ChatMessageStreamDelta"):
        return None
    # Any future/unrecognized step type — still surface something rather than
    # silently drop it.
    return {"kind": kind.lower(), "text": str(step)[:500]}


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

        print(f"[smolagents] goal: {goal[:200]}", file=sys.stderr, flush=True)

        answer = None
        action_steps = 0
        step_texts = []
        for step in agent.run(goal, stream=True):
            info = _describe_step(step)
            if info is None:
                continue
            if info["kind"] == "action":
                action_steps += 1
            step_texts.append(info["text"])
            print("SMOLAGENTS_STEP:" + json.dumps(info), flush=True)
            if info["kind"] == "final":
                answer = info["text"]

        if answer is None:
            # Streaming ended without a FinalAnswerStep (shouldn't normally
            # happen, but max_steps exhaustion or an odd agent state could
            # do it) — fall back to whatever the last step said rather than
            # reporting a blank answer.
            answer = step_texts[-1] if step_texts else ""

        elapsed = round(time.time() - t0, 2)
        result = {
            "ok": True,
            "answer": str(answer),
            "steps": action_steps,
            "elapsed_s": elapsed,
            "model": model_id,
        }
        print("SMOLAGENTS_RESULT:" + json.dumps(result))
        return 0
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        print(f"[smolagents] FAILED after {elapsed}s: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        print("SMOLAGENTS_RESULT:" + json.dumps({
            "ok": False, "error": str(e), "elapsed_s": elapsed,
        }))
        return 1


if __name__ == "__main__":
    sys.exit(main())
