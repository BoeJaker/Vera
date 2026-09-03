"""thinker.py — the provider-pluggable "think" step.

Given the goal, the current :class:`Observation` and recent history, decide the
next action as strict JSON: ``{thought, action, args, done}``.

By default the model reasons over the **accessibility/DOM tree** (stable element
refs) — robust, cheap, and works with any text model on Vera's cluster. The
screenshot is always captured alongside (for the record and for future vision
models); ``include_screenshot`` is a forward hook for vision-capable providers.

Providers (same convention as evolve's critic/editor):
  • ``ollama`` / ``ollama:<model>``     → local cluster via ``llm.generate``
  • ``anthropic:<model>`` / ``openai:<model>`` / any stored provider id
                                        → ``providers.chat`` (sealed keys, cost)

``build_prompt`` and ``parse_decision`` are pure (unit-testable); ``decide`` does
the LLM hop through an injected ``call_cap``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .actions import ACTIONS, action_space_text, validate_action

log = logging.getLogger("vera.operator.thinker")

_SYSTEM = (
    "You are Vera's web operator. You drive a real web browser to accomplish a "
    "GOAL by choosing ONE next action at a time. You are given the current page "
    "(its interactive ELEMENTS, each with a stable ref like e12, and its visible "
    "TEXT) and a short history of what you already did.\n\n"
    "Rules:\n"
    "• Prefer acting by element ref (e.g. click ref=e12). Use x,y only when no "
    "ref fits (e.g. a canvas / remote-desktop surface).\n"
    "• Take the smallest useful step. Do not repeat an action that just failed.\n"
    "• When the GOAL is achieved, use action \"done\" with a short summary.\n"
    "• Respond with EXACTLY ONE JSON object and nothing else."
)


def build_prompt(goal: str, observation, history: Optional[List[Dict[str, Any]]] = None,
                 canvas: bool = False, max_elements: int = 60) -> Dict[str, str]:
    """Return {system, user} strings for the LLM call."""
    hist = history or []
    hist_lines = []
    for h in hist[-8:]:
        act = h.get("action", "?")
        args = {k: v for k, v in (h.get("args") or {}).items() if k != "text"}
        res = h.get("result") or {}
        status = "ok" if res.get("ok") else ("error: " + str(res.get("error", ""))[:80]
                                             if res.get("error") else "?")
        hist_lines.append(f"- {act} {json.dumps(args, default=str)[:80]} → {status}")
    hist_text = "\n".join(hist_lines) or "(nothing yet)"

    obs_text = observation.compact(max_elements=max_elements) \
        if hasattr(observation, "compact") else str(observation)
    canvas_note = ("\nNOTE: this surface is a canvas/remote desktop — element "
                   "refs may be empty; use x,y coordinates read off the screenshot."
                   if canvas else "")

    user = (
        f"GOAL:\n{goal}\n\n"
        f"ACTION SPACE (choose one):\n{action_space_text()}\n{canvas_note}\n\n"
        f"HISTORY (most recent last):\n{hist_text}\n\n"
        f"CURRENT PAGE:\n{obs_text}\n\n"
        'Reply with one JSON object: '
        '{"thought": "...", "action": "<name>", "args": {...}, "done": false}'
    )
    return {"system": _SYSTEM, "user": user}


# Quote characters models substitute for ASCII quotes. Keyed by ordinal so this
# source stays ASCII (non-ASCII does not survive every edit path here).
_SMART_QUOTES = {
    0x201C: chr(34), 0x201D: chr(34), 0x201E: chr(34), 0x201F: chr(34),
    0x2033: chr(34), 0x00AB: chr(34), 0x00BB: chr(34),
    0x2018: chr(39), 0x2019: chr(39), 0x201A: chr(39), 0x201B: chr(39),
    0x2032: chr(39),
}


def finalise_decision(decision):
    """Structural check + argument repair, applied to a parsed decision.

    Split out so the repair is one function the loop and the tests both call.

    validate_action REPAIRS argument names a model glued annotation onto
    ("text!" -> "text"). actions.perform re-validates and already executes the
    repaired args, so EXECUTION was never the gap. The gap was the decision
    itself: it kept the RAW args, so operator_loop recorded "text!" in its
    history, build_prompt echoed that back as the argument name every turn, and
    its ``k != "text"`` filter no longer matched - so the typed value was fed
    back too. The model was being retaught the wrong key on every pass. Writing
    the repaired args back onto the decision is what closes that loop.

    An invalid action is marked, never executed.
    """
    if not isinstance(decision, dict) or decision.get("error"):
        return decision
    v = validate_action(decision.get("action"), decision.get("args"))
    if not v["ok"]:
        decision["invalid"] = v["error"]
    else:
        decision["args"] = v["args"]
    return decision


def parse_decision(text: str) -> Dict[str, Any]:
    """Extract {thought, action, args, done} from an LLM response. Tolerant of
    code fences and surrounding prose. Returns {error} if unrecoverable."""
    if not text:
        return {"error": "empty model response"}
    raw = text.strip()
    # strip ```json fences
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            raw = brace.group(0)
    try:
        d = json.loads(raw)
    except Exception:
        # Curly quotes are not JSON, but a model that emits them around its keys
        # is otherwise handing over a perfectly good decision. Rejecting it
        # outright cost a whole operator step (census build-browser-verified,
        # 2026-08-29: "could not parse decision JSON"). Retried only AFTER a
        # strict parse fails, so this is strictly more permissive than before and
        # never changes a response that already parsed.
        try:
            d = json.loads(raw.translate(_SMART_QUOTES))
        except Exception:
            d = None
    if d is None:
        # last resort: some models emit action on its own line
        m = re.search(r'"?action"?\s*[:=]\s*"?(\w+)"?', text)
        if m:
            return {"thought": "", "action": m.group(1), "args": {}, "done": False,
                    "raw": text[:400]}
        return {"error": f"could not parse decision JSON: {text[:160]}"}
    if not isinstance(d, dict):
        return {"error": "decision was not a JSON object"}
    action = str(d.get("action") or "").strip()
    args = d.get("args") if isinstance(d.get("args"), dict) else {}
    done = bool(d.get("done")) or action == "done"
    return {"thought": str(d.get("thought") or ""), "action": action,
            "args": args, "done": done, "raw": text[:400]}


def _split_provider(provider: str) -> tuple:
    p = (provider or "ollama").strip()
    if ":" in p:
        name, model = p.split(":", 1)
        return name.strip(), model.strip()
    return p, ""


async def decide(goal: str, observation, history: Optional[List[Dict[str, Any]]],
                 call_cap: Callable[..., Awaitable[Any]],
                 provider: str = "ollama", model: str = "",
                 canvas: bool = False, max_tokens: int = 512,
                 think=None) -> Dict[str, Any]:
    """Run one think step through the LLM. Returns a decision dict (see
    ``parse_decision``) plus {provider, error}. Never raises."""
    prompt = build_prompt(goal, observation, history, canvas=canvas)
    name, pmodel = _split_provider(provider)
    model = model or pmodel

    try:
        if name in ("ollama", "vllm", "local", "cluster"):
            res = await call_cap(
                "llm.generate", prompt=prompt["user"], system=prompt["system"],
                model=model or None, job_type="code", caller="operator.think",
                think=think,
            )
        else:
            res = await call_cap(
                "providers.chat", provider=name, model=model,
                prompt=prompt["user"], system=prompt["system"],
                max_tokens=max_tokens, caller="operator.think",
            )
    except Exception as e:
        return {"error": f"think LLM call failed: {e}", "provider": provider}

    if isinstance(res, dict) and res.get("error"):
        return {"error": res["error"], "provider": provider}
    text = (res or {}).get("text", "") if isinstance(res, dict) else str(res)
    decision = parse_decision(text)
    decision["provider"] = provider
    if decision.get("error"):
        return decision
    # structural sanity — surface (don't execute) an illegal action
    decision = finalise_decision(decision)
    return decision
