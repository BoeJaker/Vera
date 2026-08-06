"""tours.py — deterministic, LLM-free "tours" of a UI for documentation.

A **tour** is an ordered list of steps that reproduces a real workflow the same
way every time — navigate, wait, click a labelled control, type, scroll — while
capturing **stills** and **GIF clips**. Because it's fully scripted, it's
reproducible for a docs build (unlike the LLM-driven ``operator.run``, whose
quality tracks the model).

Steps (dict form, or the compact mini-DSL parsed by :func:`parse_steps`):

    goto        {"do":"goto","panel":"markets-studio"}  | {"do":"goto","url":…}
    wait        {"do":"wait","ms":1200}
    scroll      {"do":"scroll","dy":400}
    shot        {"do":"shot","name":"overview"}            → a PNG still
    gif_start   {"do":"gif_start","interval_ms":700}
    gif_stop    {"do":"gif_stop","name":"clip","duration_ms":800}   → a GIF
    click_text  {"do":"click_text","text":"Run backtest"}  (matches an element by name)
    type_text   {"do":"type_text","text_target":"Goal","text":"BTC"}
    seed        {"do":"seed","cap":"markets.datasets","args":{}}   (populate via a cap)

``parse_steps`` / ``validate_step`` are pure (unit-testable); ``run_tour`` drives
a live session and is injected with the capture helpers, so nothing here hard-
imports the caps module.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import actions as _actions
from . import capture as _capture
from . import perception as _perception

log = logging.getLogger("vera.operator.tours")

STEP_KINDS = {"goto", "wait", "scroll", "shot", "gif_start", "gif_stop",
              "click_text", "type_text", "seed"}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-") or "x"


def validate_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """Pure structural check. Returns {ok, error}."""
    if not isinstance(step, dict):
        return {"ok": False, "error": "step must be an object"}
    do = step.get("do")
    if do not in STEP_KINDS:
        return {"ok": False, "error": f"unknown step '{do}'. Valid: {', '.join(sorted(STEP_KINDS))}"}
    if do == "goto" and not (step.get("panel") or step.get("url")):
        return {"ok": False, "error": "goto needs 'panel' or 'url'"}
    if do == "click_text" and not str(step.get("text") or "").strip():
        return {"ok": False, "error": "click_text needs 'text'"}
    if do == "type_text" and not str(step.get("text_target") or "").strip():
        return {"ok": False, "error": "type_text needs 'text_target'"}
    if do == "seed" and not str(step.get("cap") or "").strip():
        return {"ok": False, "error": "seed needs 'cap'"}
    return {"ok": True, "error": ""}


def parse_steps(dsl: str) -> List[Dict[str, Any]]:
    """Parse a compact ';'-separated mini-DSL into step dicts.

    e.g.  ``wait 1200; click_text Run backtest; gif_start 700; wait 2500;
            gif_stop equity 800; shot done``
    """
    out: List[Dict[str, Any]] = []
    for raw in (dsl or "").split(";"):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(None, 1)
        verb = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if verb == "wait":
            out.append({"do": "wait", "ms": int(rest or 1000)})
        elif verb == "scroll":
            out.append({"do": "scroll", "dy": int(rest or 400)})
        elif verb == "goto":
            out.append({"do": "goto", "panel": rest} if rest and "/" not in rest and "." not in rest
                       else {"do": "goto", "url": rest})
        elif verb == "shot":
            out.append({"do": "shot", "name": rest or "shot"})
        elif verb in ("click_text", "click"):
            out.append({"do": "click_text", "text": rest})
        elif verb in ("type", "type_text"):
            m = re.match(r'(.+?)\s+"(.*)"$', rest) or re.match(r"(.+?)\s+'(.*)'$", rest)
            if m:
                out.append({"do": "type_text", "text_target": m.group(1).strip(), "text": m.group(2)})
            else:
                t = rest.split(None, 1)
                out.append({"do": "type_text", "text_target": t[0] if t else "",
                            "text": t[1] if len(t) > 1 else ""})
        elif verb == "gif_start":
            out.append({"do": "gif_start", "interval_ms": int(rest or 700)})
        elif verb == "gif_stop":
            g = rest.split(None, 1)
            out.append({"do": "gif_stop", "name": (g[0] if g else "clip"),
                        "duration_ms": int(g[1]) if len(g) > 1 and g[1].isdigit() else 800})
        elif verb == "seed":
            out.append({"do": "seed", "cap": rest})
    return out


def find_ref(observation, text: str) -> str:
    """First element whose accessible name (or role) contains ``text`` (ci)."""
    t = (text or "").strip().lower()
    if not t:
        return ""
    for e in getattr(observation, "elements", []):
        if t in (e.name or "").lower():
            return e.ref
    for e in getattr(observation, "elements", []):
        if t in (e.role or "").lower():
            return e.ref
    return ""


async def _settle(page, ms: int = 700):
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(ms)
    except Exception:
        pass


async def run_tour(session, steps: List[Dict[str, Any]], *, out_dir: str,
                   rel_base: str = "",
                   call_target: Optional[Callable[..., Awaitable[Any]]] = None,
                   emit: Optional[Callable[..., Awaitable[None]]] = None) -> Dict[str, Any]:
    """Execute a validated step list against ``session``. Captures stills + GIFs
    into ``out_dir``; ``rel_base`` prefixes the returned relative paths (for
    markdown). Returns {shots, gifs, errors, steps}."""
    os.makedirs(out_dir, exist_ok=True)
    shots: List[Dict[str, Any]] = []
    gifs: List[Dict[str, Any]] = []
    errors: List[str] = []
    active = None
    page = session.page

    def _rel(fname: str) -> str:
        return f"{rel_base.rstrip('/')}/{fname}" if rel_base else fname

    async def _emit(**k):
        if emit:
            try:
                await emit(k)
            except Exception:
                pass

    for idx, st in enumerate(steps):
        v = validate_step(st)
        if not v["ok"]:
            errors.append(f"step {idx}: {v['error']}")
            continue
        do = st["do"]
        try:
            if do == "goto":
                url = st.get("url") or (
                    f"{session.base_url.rstrip('/')}/ui/panel/window?id={st['panel']}"
                    if st.get("panel") else "")
                if url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await _settle(page)
            elif do == "wait":
                await page.wait_for_timeout(int(st.get("ms", 1000)))
            elif do == "scroll":
                await page.mouse.wheel(0, int(st.get("dy", 400)))
                await page.wait_for_timeout(250)
            elif do == "shot":
                name = _safe(st.get("name") or f"step{idx}")
                p = os.path.join(out_dir, name + ".png")
                await page.screenshot(path=p, type="png")
                shots.append({"name": name, "path": p, "rel": _rel(name + ".png")})
            elif do == "click_text":
                obs = await _perception.observe_page(page)
                session.ref_map = obs.ref_map()
                ref = find_ref(obs, st.get("text", ""))
                if ref:
                    await _actions.perform(session, "click", {"ref": ref})
                    await _settle(page, 400)
                else:
                    errors.append(f"click_text: no element matching {st.get('text')!r}")
            elif do == "type_text":
                obs = await _perception.observe_page(page)
                session.ref_map = obs.ref_map()
                ref = find_ref(obs, st.get("text_target", ""))
                if ref:
                    await _actions.perform(session, "type",
                                           {"ref": ref, "text": st.get("text", ""), "clear": True})
                else:
                    errors.append(f"type_text: no field matching {st.get('text_target')!r}")
            elif do == "seed":
                if call_target and st.get("cap"):
                    await call_target(st["cap"], st.get("args", {}))
            elif do == "gif_start":
                fdir = os.path.join(out_dir, "_frames", f"g{idx}")
                active = _capture.start_capture(
                    session.session_id, fdir, (lambda: session.page),
                    interval_ms=int(st.get("interval_ms", 700)),
                    max_frames=int(st.get("max_frames", 120)))
            elif do == "gif_stop":
                if active:
                    cap = await _capture.stop_capture(active.capture_id)
                    name = _safe(st.get("name") or f"clip{idx}")
                    out = os.path.join(out_dir, name + ".gif")
                    r = _capture.assemble_gif(cap.frames, out,
                                              duration_ms=int(st.get("duration_ms", 800)))
                    if r.get("ok"):
                        gifs.append({"name": name, "path": out, "rel": _rel(name + ".gif")})
                    else:
                        errors.append("gif: " + str(r.get("error", "")))
                    active = None
            await _emit(stage="tour.step", i=idx, do=do)
        except Exception as e:
            errors.append(f"step {idx} ({do}): {e}")

    if active is not None:  # never leave a sampler running
        try:
            await _capture.stop_capture(active.capture_id)
        except Exception:
            pass
    return {"shots": shots, "gifs": gifs, "errors": errors, "steps": len(steps)}


# ─────────────────────────────────────────────────────────────────────────────
#  Example tours — conservative (navigate + scroll + still + a scroll GIF). They
#  demonstrate the framework; richer, workflow-driving tours can be authored per
#  domain (safe against a sandbox target, whose state is isolated).
# ─────────────────────────────────────────────────────────────────────────────
TOURS: Dict[str, Dict[str, Any]] = {
    "markets": {"title": "Markets & Quant Studio", "panel": "markets-studio",
                "seed": "markets",
                "steps": parse_steps(
                    "goto markets-studio; wait 1400; shot overview; "
                    "gif_start 700; scroll 500; wait 900; scroll 500; wait 900; "
                    "gif_stop scan 750")},
    "dream": {"title": "Dream", "panel": "dream", "seed": "dream",
              "steps": parse_steps(
                  "goto dream; wait 1400; shot overview; "
                  "gif_start 800; scroll 450; wait 1000; scroll 450; wait 1000; gif_stop boards 800")},
    "dag-engine": {"title": "DAG & Loop Engine", "panel": "dag-workshop", "seed": "dag",
                   "steps": parse_steps(
                       "goto dag-workshop; wait 1400; shot overview; "
                       "gif_start 800; scroll 400; wait 900; gif_stop workshop 800")},
    "operator": {"title": "Operator", "panel": "operator-studio", "seed": None,
                 "steps": parse_steps(
                     "goto operator-studio; wait 1400; shot overview; "
                     "gif_start 800; scroll 400; wait 900; scroll -400; wait 900; gif_stop studio 800")},
}


def get_tour(slug: str) -> Optional[Dict[str, Any]]:
    return TOURS.get(slug)


def list_tours() -> List[str]:
    return list(TOURS.keys())
