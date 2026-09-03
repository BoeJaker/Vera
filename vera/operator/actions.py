"""actions.py — the operator's act primitives (ref-based + xy fallback).

One unified surface: ``perform(session, action, args)``. Acts resolve an element
**ref** (from the last observation's ref map) to a deterministic locator
(``[data-vera-ref="…"]``); when no ref is available (opaque canvas / VM surface)
they fall back to raw ``x,y`` mouse/keyboard.

``ACTIONS`` is the machine-readable action space handed to the thinker.
``validate_action`` is pure (unit-testable, no browser); ``perform`` needs a live
session.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("vera.operator.actions")

# action → {args, doc}. ``!`` marks a required arg. This is rendered into the
# thinker prompt so the model emits only legal actions.
ACTIONS: Dict[str, Dict[str, Any]] = {
    "goto":    {"args": "url!", "doc": "Navigate to an absolute or site-relative URL."},
    "click":   {"args": "ref | x,y", "doc": "Click an element by ref (preferred) or pixel."},
    "type":    {"args": "text! ref? clear? submit?", "doc": "Type text into a field (ref or currently-focused). submit=true presses Enter after."},
    "press":   {"args": "key!", "doc": "Press a key or chord, e.g. 'Enter', 'Tab', 'Control+A'."},
    "scroll":  {"args": "dy? dx? ref?", "doc": "Scroll the page by dx/dy, or scroll a ref into view."},
    "hover":   {"args": "ref | x,y", "doc": "Hover an element or point (reveals menus/tooltips)."},
    "select":  {"args": "ref! value? label?", "doc": "Pick an option in a <select> by value or visible label."},
    "wait":    {"args": "ms? selector?", "doc": "Wait a fixed time, or until a CSS selector appears."},
    "nav":     {"args": "direction!", "doc": "Browser history: direction=back|forward|reload."},
    "screenshot": {"args": "", "doc": "Take a fresh screenshot (observation captures one anyway)."},
    "done":    {"args": "summary?", "doc": "Signal the goal is complete; ends the loop."},
}

# Acts that change page/server state — the safety gate can require confirmation
# for these on non-sandbox targets.
MUTATING_ACTIONS = {"click", "type", "press", "select", "goto", "nav"}


#: Markers the ACTIONS specs use to annotate an argument: "!" required,
#: "?" optional. They are NOTATION, never part of the key name.
_ARG_MARKERS = "!?"


def split_arg_spec(spec):
    """('text! ref? clear?') -> (['text'], ['ref', 'clear'], '').

    The third element is a free-form alternation ('ref | x,y') that has no
    required/optional split, kept verbatim so click/hover still read naturally.
    """
    spec = str(spec or "").strip()
    if not spec:
        return [], [], ""
    if "|" in spec:
        return [], [], spec
    required, optional = [], []
    for tok in spec.replace(",", " ").split():
        bare = tok.strip().rstrip(_ARG_MARKERS)
        if not bare:
            continue
        (optional if tok.endswith("?") else required).append(bare)
    return required, optional, ""


def action_space_text() -> str:
    """Human/LLM-readable listing of the action space for the thinker prompt.

    Argument names are rendered BARE, with required/optional said in words.

    They used to be rendered with their annotation markers still attached -
    "type(text! ref? clear? submit?)" - and nothing anywhere told the reader
    that "!" meant required. So the model did the reasonable thing and used
    what it was shown as the key name, emitting {"text!": "bad-email"}, which
    validate_action then rejected as "type requires 'text'". Census 28's
    build-browser-verified lost five consecutive operator runs to it, and the
    history line echoed the bad key back each turn, teaching it again.

    A prompt that documents an argument as `text!` and then rejects `text!` is
    not a model failure. Fixed at the source here, and forgiven at the boundary
    by normalise_arg_keys - the prompt should be right AND the parser should be
    kind, because the next model will invent its own punctuation.
    """
    lines = []
    for name, spec in ACTIONS.items():
        required, optional, alternation = split_arg_spec(spec.get("args", ""))
        if alternation:
            args = "args: " + alternation
        elif required or optional:
            bits = []
            if required:
                bits.append("required: " + ", ".join(required))
            if optional:
                bits.append("optional: " + ", ".join(optional))
            args = "; ".join(bits)
        else:
            args = "no args"
        lines.append("- %s — %s — %s" % (name, args, spec["doc"]))
    return "\n".join(lines)


#: Trailing characters a model may glue onto an argument NAME: the "!"/"?"
#: annotation markers, and the "=" / ":" of an assignment it half-copied
#: ({"ref= ": "e1"} was observed alongside {"text! ": "..."}).
_KEY_NOISE = "!?=: \t"


def normalise_arg_keys(args):
    """Strip annotation noise a model glued onto argument NAMES.

    {"text! ": "x", "ref= ": "e1", "clear?": True} -> {"text": "x", "ref": "e1",
    "clear": True}. Values are never touched - only keys.

    An exact key always wins: given both "text" and "text!", the documented one
    is kept, because a model that emitted both meant the real one.

    No action argument legitimately contains any of these characters (url, ref,
    x, y, text, clear, submit, key, dy, dx, value, label, ms, selector,
    direction, summary), so this cannot mangle a valid call.
    """
    out = {}
    for raw_key, value in dict(args or {}).items():
        if not isinstance(raw_key, str):
            out[raw_key] = value
            continue
        clean = raw_key.strip().strip(_KEY_NOISE)
        if not clean:
            continue
        # An exactly-correct key already present must not be overwritten by a
        # noisy duplicate that normalises onto it.
        if clean in out and raw_key != clean:
            continue
        out[clean] = value
    return out


def validate_action(action: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pure structural validation. Returns {ok, error, action, args}.

    The returned ``args`` are NORMALISED and are what the caller must execute -
    validating a repaired call and then running the raw one would fix nothing.
    """
    args = normalise_arg_keys(args)
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action '{action}'. "
                f"Valid: {', '.join(ACTIONS)}"}
    has_ref = bool(args.get("ref"))
    has_xy = args.get("x") is not None and args.get("y") is not None
    if action == "goto" and not str(args.get("url") or "").strip():
        return {"ok": False, "error": "goto requires 'url'"}
    if action in ("click", "hover") and not (has_ref or has_xy):
        return {"ok": False, "error": f"{action} requires 'ref' or 'x'+'y'"}
    if action == "type" and not str(args.get("text") or ""):
        return {"ok": False, "error": "type requires 'text'"}
    if action == "press" and not str(args.get("key") or "").strip():
        return {"ok": False, "error": "press requires 'key'"}
    if action == "select":
        if not has_ref:
            return {"ok": False, "error": "select requires 'ref'"}
        if args.get("value") is None and args.get("label") is None:
            return {"ok": False, "error": "select requires 'value' or 'label'"}
    if action == "nav":
        d = str(args.get("direction") or "").lower()
        if d not in ("back", "forward", "reload"):
            return {"ok": False, "error": "nav 'direction' must be back|forward|reload"}
    return {"ok": True, "error": "", "action": action, "args": args}


def _resolve_ref(session, ref: str) -> Optional[str]:
    info = (session.ref_map or {}).get(ref)
    if info and info.get("selector"):
        return info["selector"]
    return None


async def perform(session, action: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute one action against ``session.page``. Never raises — returns
    {ok, action, ...} or {error}."""
    v = validate_action(action, args)
    if not v["ok"]:
        return {"error": v["error"], "action": action}
    args = v["args"]
    page = getattr(session, "page", None)
    if action == "done":
        return {"ok": True, "action": "done", "summary": args.get("summary", "")}
    if action == "screenshot":
        return {"ok": True, "action": "screenshot"}
    if page is None:
        return {"error": "session has no live page (start a session first)", "action": action}

    try:
        if action == "goto":
            url = str(args["url"]).strip()
            if session.base_url and url.startswith("/"):
                url = session.base_url.rstrip("/") + url
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            return {"ok": True, "action": "goto", "url": page.url}

        if action == "click":
            if args.get("ref"):
                sel = _resolve_ref(session, args["ref"])
                if not sel:
                    return {"error": f"ref '{args['ref']}' not in current observation",
                            "action": action}
                await page.locator(sel).first.click(timeout=8000)
                return {"ok": True, "action": "click", "ref": args["ref"]}
            await page.mouse.click(float(args["x"]), float(args["y"]))
            return {"ok": True, "action": "click", "xy": [args["x"], args["y"]]}

        if action == "hover":
            if args.get("ref"):
                sel = _resolve_ref(session, args["ref"])
                if not sel:
                    return {"error": f"ref '{args['ref']}' not found", "action": action}
                await page.locator(sel).first.hover(timeout=8000)
                return {"ok": True, "action": "hover", "ref": args["ref"]}
            await page.mouse.move(float(args["x"]), float(args["y"]))
            return {"ok": True, "action": "hover", "xy": [args["x"], args["y"]]}

        if action == "type":
            text = str(args["text"])
            if args.get("ref"):
                sel = _resolve_ref(session, args["ref"])
                if not sel:
                    return {"error": f"ref '{args['ref']}' not found", "action": action}
                loc = page.locator(sel).first
                if args.get("clear", True):
                    try:
                        await loc.fill(text, timeout=8000)
                    except Exception:
                        await loc.click(timeout=8000)
                        await page.keyboard.type(text)
                else:
                    await loc.click(timeout=8000)
                    await page.keyboard.type(text)
            else:
                await page.keyboard.type(text)
            if args.get("submit"):
                await page.keyboard.press("Enter")
            return {"ok": True, "action": "type", "chars": len(text),
                    "submit": bool(args.get("submit"))}

        if action == "press":
            await page.keyboard.press(str(args["key"]))
            return {"ok": True, "action": "press", "key": args["key"]}

        if action == "scroll":
            if args.get("ref"):
                sel = _resolve_ref(session, args["ref"])
                if sel:
                    await page.locator(sel).first.scroll_into_view_if_needed(timeout=6000)
                    return {"ok": True, "action": "scroll", "ref": args["ref"]}
            dx = int(args.get("dx", 0) or 0)
            dy = int(args.get("dy", 400) or 0)
            await page.mouse.wheel(dx, dy)
            return {"ok": True, "action": "scroll", "dx": dx, "dy": dy}

        if action == "select":
            sel = _resolve_ref(session, args["ref"])
            if not sel:
                return {"error": f"ref '{args['ref']}' not found", "action": action}
            loc = page.locator(sel).first
            if args.get("value") is not None:
                await loc.select_option(value=str(args["value"]), timeout=8000)
            else:
                await loc.select_option(label=str(args["label"]), timeout=8000)
            return {"ok": True, "action": "select", "ref": args["ref"]}

        if action == "wait":
            if args.get("selector"):
                await page.wait_for_selector(str(args["selector"]),
                                             timeout=int(args.get("ms", 10000) or 10000))
            else:
                await page.wait_for_timeout(int(args.get("ms", 1000) or 1000))
            return {"ok": True, "action": "wait"}

        if action == "nav":
            d = str(args["direction"]).lower()
            if d == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
            elif d == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=15000)
            else:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
            return {"ok": True, "action": "nav", "direction": d, "url": page.url}

        return {"error": f"action '{action}' not implemented", "action": action}
    except Exception as e:
        return {"error": f"{action} failed: {e}", "action": action}
