"""operator_capabilities.py — the ``operator.*`` capability surface.

Registers the operator's primitives (session / observe / act / read), drivers
(think / step / run), missions (mission.list / mission.run + the ``docs.*``
aliases) and testing (``operator.test.run``), plus the Operator Studio panel.

Loaded by the orchestrator from ``_module_files`` (basename import), so this
entry module uses **absolute** ``Vera.vera.operator.*`` imports; the sibling
submodules it pulls in are imported through the package and resolve their own
relative imports normally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, enum_schema, register_ui,
)

from Vera.vera.operator import browser_engine as _be
from Vera.vera.operator import perception as _perception
from Vera.vera.operator import actions as _actions
from Vera.vera.operator import safety as _safety
from Vera.vera.operator import thinker as _thinker
from Vera.vera.operator import operator_loop as _loop
from Vera.vera.operator import targets as _targets
from Vera.vera.operator import capture as _capture
from Vera.vera.operator import tours as _tours
from Vera.vera.operator import connectors as _connectors
from Vera.vera.operator.actions import ACTIONS
from Vera.vera.operator.missions import run_mission, list_missions
from Vera.vera.operator.docs import gallery as _gallery
from Vera.vera.operator.docs import directives as _directives

log = logging.getLogger("vera.operator")

_HERE = Path(__file__).resolve().parent
_PANEL_PATH = _HERE / "operator_studio_panel.html"


def _repo_root() -> Path:
    # …/Vera/vera/operator/operator_capabilities.py → parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _default_base_url() -> str:
    c = getattr(_orch, "cfg", None)
    scheme = "https" if getattr(c, "TLS_ENABLED", False) else "http"
    port = getattr(c, "ORCHESTRATOR_PORT", 8999)
    return f"{scheme}://localhost:{port}"


def _shots_dir(session_id: str) -> str:
    return str(_repo_root() / "artifacts" / "operator" / (session_id or "misc"))


def _safe_seg(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "")).strip("-") or "capture"


def _gif_out_path(domain: str, name: str) -> Dict[str, str]:
    """Resolve where a GIF should be written. With a ``domain`` it lands in the
    committed docs assets (referenced from markdown); otherwise in artifacts
    (served live via /operator/artifact). Returns {path, rel, url}."""
    name = _safe_seg(name)
    if domain:
        rel = f"assets/{_safe_seg(domain)}/{name}.gif"
        path = _repo_root() / "documentation" / rel
        return {"path": str(path), "rel": rel, "url": ""}
    path = _repo_root() / "artifacts" / "operator" / "captures" / f"{name}.gif"
    return {"path": str(path), "rel": "",
            "url": f"/operator/artifact?path=captures/{name}.gif"}


def _artifact_rel(abspath: str) -> str:
    """Path under artifacts/operator suitable for the /operator/artifact route."""
    try:
        root = _repo_root() / "artifacts" / "operator"
        return str(Path(abspath).resolve().relative_to(root)).replace("\\", "/")
    except Exception:
        return ""


async def _call(name: str, **kw) -> Any:
    """In-process capability dispatch (used by think/mission plumbing)."""
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


def _target_caller(base_url: str):
    """Return an async (name, args)->result that calls a cap on the TARGET Vera
    (sandbox or live) over its /mcp/call — for seeding a tour's data."""
    async def _call_t(name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60, verify=False) as c:
                r = await c.post(base_url.rstrip("/") + "/mcp/call",
                                 json={"name": name, "arguments": args or {}})
            r.raise_for_status()
            d = r.json()
            return d.get("result", d.get("content", d)) if isinstance(d, dict) else d
        except Exception as e:
            return {"error": str(e)}
    return _call_t


def _policy_for_session(s, *, allowlist: Optional[List[str]] = None,
                        dry_run: Optional[bool] = None,
                        allow_destructive: Optional[bool] = None,
                        confirm: Optional[bool] = None) -> "_safety.SafetyPolicy":
    """Build the effective SafetyPolicy for a session: the policy stored at
    connect (allowlist / allow_destructive / dry_run) UNIONed with any per-call
    overrides. This is why a host allowlisted once at connect stays permitted for
    every later act and run on that session."""
    sp = getattr(s, "policy", {}) or {}
    kind = (getattr(s, "target", {}) or {}).get("kind", "url")
    merged = list(sp.get("allowlist", []) or [])
    for h in (allowlist or []):
        h = (h or "").strip()
        if h and h not in merged:
            merged.append(h)
    return _safety.SafetyPolicy.for_target(
        kind, getattr(s, "base_url", ""), allowlist=merged,
        dry_run=(dry_run if dry_run is not None else sp.get("dry_run")),
        allow_destructive=(allow_destructive if allow_destructive is not None
                           else sp.get("allow_destructive")),
        confirm=(confirm if confirm is not None else None))


async def _open_session(url: str = "", kind: str = "", base_url: str = "",
                        session_id: str = "", width: int = 1440, height: int = 900,
                        branch: str = "", panel_id: str = "", cs_id: str = "",
                        source: str = "", ref: str = "",
                        allowlist: Optional[List[str]] = None,
                        allow_destructive: Optional[bool] = None,
                        dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """Resolve a target, boot a browser session, navigate to its start page.
    Returns {ok, session_id, resolved, summary} or {error}."""
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    if source:
        # A registered connectable (integration/ollama/node/docker/proxmox).
        target: Dict[str, Any] = {"source": source, "ref": ref}
    else:
        target = {"kind": kind or ("url" if url else "live")}
        if url:
            target["url"] = url
        if base_url:
            target["base_url"] = base_url
        if branch:
            target["branch"] = branch
        if panel_id:
            target["panel_id"] = panel_id
        if cs_id:
            target["id"] = cs_id
    resolved = await _targets.ensure_target(target, _call, _default_base_url())
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready"), "resolved": resolved}
    try:
        sess = await _be.start_session(
            session_id=session_id, base_url=resolved["base_url"],
            viewport={"width": int(width), "height": int(height)}, target=resolved)
    except Exception as e:
        return {"error": f"session start failed: {e}"}
    # Persist the operating policy on the session so every subsequent act/run
    # honours it (allowlist a site ONCE at connect, not on every action).
    pol: Dict[str, Any] = dict(sess.policy or {})
    if allowlist is not None:
        pol["allowlist"] = [h.strip() for h in allowlist if h and h.strip()]
    if allow_destructive is not None:
        pol["allow_destructive"] = bool(allow_destructive)
    if dry_run is not None:
        pol["dry_run"] = bool(dry_run)
    sess.policy = pol
    if resolved.get("start_url"):
        try:
            await sess.page.goto(resolved["start_url"], wait_until="domcontentloaded",
                                 timeout=30000)
            try:
                await sess.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            sess.meta["last_url"] = sess.page.url
        except Exception as e:
            # Was previously a silently-swallowed log.warning — the session
            # would then enter run_loop() sitting on about:blank with NO
            # indication anything went wrong, and the model's own first
            # "observe" sees a genuinely blank page with no error attached.
            # Confirmed live (§5.22, Test U): the model then GUESSES its
            # own goto target (a bare relative filename) instead of retrying
            # the URL it was actually given, and that guess trips the
            # allowlist check — a confusing failure mode with no trace back
            # to the real cause. If the caller's own start page can't be
            # reached, that is itself the actionable problem; own the
            # session and close it rather than leak a half-started one.
            log.warning("operator: initial navigation to %s failed: %s",
                       resolved["start_url"], e)
            try:
                await _be.close_session(sess.session_id)
            except Exception:
                pass
            return {"error": f"could not load the start page ({resolved['start_url']}): {e}"}
    return {"ok": True, "session_id": sess.session_id, "resolved": resolved,
            "summary": sess.summary()}


# ─────────────────────────────────────────────────────────────────────────────
#  SESSION
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.session.start", memory="on",
            http_method="POST", http_path="/operator/session/start", http_tags=["operator"],
            description="Open a browser session on a target and navigate to it. "
                        "Inputs: url (any web page) OR kind (url|live|sandbox|panel|"
                        "codeserver|vm) + base_url + panel_id/id, session_id (reuse), "
                        "width, height, branch (sandbox), and the session's operating "
                        "policy: allowlist (external hosts you permit acting on), "
                        "allow_destructive (permit state-changing acts), dry_run. The "
                        "policy is stored on the session so every later act/run honours "
                        "it. Output: {session_id, resolved, summary}.",
            schema=enum_schema(kind=["url", "live", "sandbox", "panel", "codeserver", "vm"]))
async def cap_session_start(url: str = "", kind: str = "", base_url: str = "",
                            session_id: str = "", width: int = 1440, height: int = 900,
                            branch: str = "", panel_id: str = "", id: str = "",
                            source: str = "", ref: str = "",
                            allowlist: Optional[List[str]] = None,
                            allow_destructive: Optional[bool] = None,
                            dry_run: Optional[bool] = None,
                            trace_id=None) -> Dict[str, Any]:
    res = await _open_session(url=url, kind=kind, base_url=base_url,
                              session_id=session_id, width=width, height=height,
                              branch=branch, panel_id=panel_id, cs_id=id,
                              source=source, ref=ref,
                              allowlist=allowlist, allow_destructive=allow_destructive,
                              dry_run=dry_run)
    if res.get("ok"):
        await emit_event({"type": "operator.session", "stage": "start",
                          "session_id": res["session_id"],
                          "base_url": res["resolved"].get("base_url")})
    return res


@capability("operator.session.status", memory="off", silent=True,
            http_method="GET", http_path="/operator/session/status", http_tags=["operator"],
            description="Status of one session (session_id) or all sessions. "
                        "Output: {session_id,url,refs,steps,alive,...} or {sessions:[...]}.")
async def cap_session_status(session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if session_id:
        s = _be.get_session(session_id)
        return s.summary() if s else {"error": f"no such session: {session_id}"}
    return {"sessions": _be.list_sessions(), "count": len(_be.list_sessions())}


@capability("operator.session.close", memory="on",
            http_method="POST", http_path="/operator/session/close", http_tags=["operator"],
            description="Close a browser session and free its page/context. "
                        "Input: session_id (str!). Output: {ok}.")
async def cap_session_close(session_id: str = "", trace_id=None) -> Dict[str, Any]:
    if not session_id:
        return {"error": "session_id required"}
    ok = await _be.close_session(session_id)
    return {"ok": ok}


# ─────────────────────────────────────────────────────────────────────────────
#  CONNECTIONS  —  drive anything registered across Vera's infrastructure
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.connect.list", memory="off", silent=True,
            http_method="GET", http_path="/operator/connect/list", http_tags=["operator"],
            description="List everything the operator can connect to across Vera's "
                        "registries — Integrations Hub apps, Ollama instances, "
                        "worker/nodes, Docker containers (published ports), Proxmox "
                        "VM consoles — by calling each subsystem's existing list cap. "
                        "Inputs: sources (csv subset of integration,ollama,node,docker,"
                        "proxmox; blank=all). Output: {connectables:[{source,ref,label,"
                        "url,type(web|api|ssh|vnc),driveable,group,detail}], count, groups}.")
async def cap_connect_list(sources: str = "", trace_id=None) -> Dict[str, Any]:
    srcs = [s.strip() for s in sources.split(",") if s.strip()] or None
    return await _connectors.list_connectables(_call, sources=srcs)


@capability("operator.connect", memory="on",
            http_method="POST", http_path="/operator/connect", http_tags=["operator"],
            description="Open a browser session on a REGISTERED connectable (from "
                        "operator.connect.list) and, optionally, drive it. Inputs: "
                        "source (integration|ollama|node|docker|proxmox), ref (str! — "
                        "the connectable's ref), session_id (reuse), goal (str — if "
                        "given, runs the observe→think→act loop toward it), provider, "
                        "max_steps. Output: session summary, or the run result when a "
                        "goal is given. Web UIs drive fully; API/SSH endpoints open for "
                        "reference (control them via their own caps).")
async def cap_connect(source: str = "", ref: str = "", goal: str = "",
                      session_id: str = "", provider: str = "ollama",
                      max_steps: int = 15, keep_open: bool = True,
                      trace_id=None) -> Dict[str, Any]:
    if not source or not ref:
        return {"error": "source and ref are required (see operator.connect.list)"}
    start = await _open_session(source=source, ref=ref, session_id=session_id)
    if start.get("error"):
        return start
    sid = start["session_id"]
    s = _be.get_session(sid)
    await emit_event({"type": "operator.session", "stage": "connect",
                      "session_id": sid, "source": source, "ref": ref,
                      "driveable": (s.target or {}).get("driveable", True) if s else True})
    if not goal:
        return start
    resolved = (s.target or {}) if s else {}
    policy = _policy_for_session(s)
    run_id = uuid.uuid4().hex[:10]

    async def _on_step(rec: Dict[str, Any]):
        shot = rec.get("screenshot", "")
        await emit_event({"type": "operator.step", "run_id": run_id, "i": rec.get("i"),
                          "phase": rec.get("phase"), "action": rec.get("action"),
                          "thought": rec.get("thought", "")[:200], "reason": rec.get("reason", ""),
                          "screenshot": f"/operator/artifact?path={_artifact_rel(shot)}" if shot else ""})

    result = await _loop.run_loop(goal, s, call_cap=_call, policy=policy, provider=provider,
                                  max_steps=int(max_steps), canvas=resolved.get("canvas", False),
                                  shots_dir=_shots_dir(sid) + f"/run-{run_id}", on_step=_on_step)
    if not keep_open:
        await _be.close_session(sid)
    result.update({"run_id": run_id, "source": source, "ref": ref,
                   "session_id": sid if keep_open else ""})
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  OBSERVE / READ
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.observe", memory="off", silent=True,
            http_method="POST", http_path="/operator/observe", http_tags=["operator"],
            description="Hybrid observation of the session's current page: a "
                        "screenshot PLUS interactive elements with stable refs "
                        "(e1,e2,…) + visible text. Inputs: session_id (str!), "
                        "max_elements (120), full_page (bool), save_screenshot (bool). "
                        "Output: {url,title,elements:[{ref,role,name,bbox}],text,"
                        "screenshot_url}.")
async def cap_observe(session_id: str = "", max_elements: int = 120,
                      full_page: bool = False, save_screenshot: bool = True,
                      trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session (operator.session.start first)"}
    shot = ""
    if save_screenshot:
        d = _shots_dir(session_id)
        os.makedirs(d, exist_ok=True)
        shot = os.path.join(d, f"obs-{int(time.time()*1000)}.png")
    obs = await _perception.observe_page(s.page, screenshot_path=shot,
                                         max_elements=int(max_elements),
                                         full_page=bool(full_page))
    s.ref_map = obs.ref_map()
    s.meta["last_url"] = obs.url
    out = obs.to_dict()
    out["screenshot_url"] = f"/operator/artifact?path={_artifact_rel(shot)}" if shot else ""
    return out


@capability("operator.read", memory="off", silent=True,
            http_method="POST", http_path="/operator/read", http_tags=["operator"],
            description="Read text from the current page (whole body, or a CSS "
                        "selector). Inputs: session_id (str!), selector (str). "
                        "Output: {text, chars}.")
async def cap_read(session_id: str = "", selector: str = "", trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session"}
    try:
        if selector:
            txt = await s.page.locator(selector).first.inner_text(timeout=6000)
        else:
            txt = await s.page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception as e:
        return {"error": str(e)}
    txt = txt or ""
    return {"ok": True, "text": txt[:8000], "chars": len(txt)}


@capability("operator.screenshot", memory="off", silent=True,
            http_method="POST", http_path="/operator/screenshot", http_tags=["operator"],
            description="Capture a screenshot of the session's current page. "
                        "Inputs: session_id (str!), full_page (bool). "
                        "Output: {screenshot, screenshot_url}.")
async def cap_screenshot(session_id: str = "", full_page: bool = False,
                         trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session"}
    d = _shots_dir(session_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"shot-{int(time.time()*1000)}.png")
    try:
        await s.page.screenshot(path=path, full_page=bool(full_page), type="png")
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "screenshot": path,
            "screenshot_url": f"/operator/artifact?path={_artifact_rel(path)}"}


# ─────────────────────────────────────────────────────────────────────────────
#  ACT
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.act", memory="on",
            http_method="POST", http_path="/operator/act", http_tags=["operator"],
            description="Perform one action on the session's page. Inputs: "
                        "session_id (str!), action (click|type|press|scroll|goto|"
                        "select|hover|wait|nav|screenshot), and per-action args: "
                        "ref (element ref from observe) | x,y (pixels); text, key, "
                        "url, value, label, direction, dx, dy, ms, selector, clear, "
                        "submit; confirm/allow_destructive for the safety gate. "
                        "Output: {ok, action, ...} or {blocked, reason} / {dry_run}.",
            schema=enum_schema(action=list(ACTIONS.keys())))
async def cap_act(session_id: str = "", action: str = "", ref: str = "",
                  x: Optional[float] = None, y: Optional[float] = None,
                  text: str = "", key: str = "", url: str = "", value: str = "",
                  label: str = "", direction: str = "", dx: int = 0, dy: int = 400,
                  ms: int = 1000, selector: str = "", clear: bool = True,
                  submit: bool = False, confirm: bool = False,
                  allow_destructive: Optional[bool] = None,
                  allowlist: Optional[List[str]] = None, trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s:
        return {"error": f"no such session: {session_id}"}
    if not action:
        return {"error": "action required"}
    args: Dict[str, Any] = {}
    if ref:
        args["ref"] = ref
    if x is not None:
        args["x"] = x
    if y is not None:
        args["y"] = y
    if text != "":
        args["text"] = text
    if key:
        args["key"] = key
    if url:
        args["url"] = url
    if value != "":
        args["value"] = value
    if label != "":
        args["label"] = label
    if direction:
        args["direction"] = direction
    if selector:
        args["selector"] = selector
    if action == "scroll":
        args["dx"], args["dy"] = int(dx), int(dy)
    if action == "wait":
        args["ms"] = int(ms)
    if action == "type":
        args["clear"], args["submit"] = bool(clear), bool(submit)

    policy = _policy_for_session(s, allowlist=allowlist, confirm=(confirm or None),
                                 allow_destructive=allow_destructive)
    gate = _safety.evaluate(policy, s.meta.get("last_url", s.base_url), action, args)
    if not gate["allowed"]:
        return {"blocked": True, "reason": gate["reason"], "action": action}
    if gate["dry_run"]:
        return {"ok": True, "dry_run": True, "note": gate["reason"],
                "action": action, "args": args}
    res = await _actions.perform(s, action, args)
    await emit_event({"type": "operator.act", "session_id": session_id,
                      "action": action, "ok": bool(res.get("ok")),
                      "error": res.get("error", "")})
    return res


# ─────────────────────────────────────────────────────────────────────────────
#  THINK / STEP / RUN
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.think", memory="off",
            http_method="POST", http_path="/operator/think", http_tags=["operator"],
            description="Observe once and let the LLM pick the next action WITHOUT "
                        "performing it. Inputs: session_id (str!), goal (str!), "
                        "provider (ollama|anthropic:model|openai:model|<id>), model. "
                        "Output: {observation, decision:{thought,action,args,done}}.")
async def cap_think(session_id: str = "", goal: str = "", provider: str = "ollama",
                    model: str = "", trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session"}
    if not goal:
        return {"error": "goal required"}
    obs = await _perception.observe_page(s.page)
    s.ref_map = obs.ref_map()
    decision = await _thinker.decide(goal, obs, s.history, _call, provider=provider,
                                     model=model, canvas=(s.target or {}).get("canvas", False))
    return {"observation": {"url": obs.url, "title": obs.title,
                            "elements": len(obs.elements)}, "decision": decision}


@capability("operator.step", memory="on",
            http_method="POST", http_path="/operator/step", http_tags=["operator"],
            description="Run ONE observe→think→act tick against a session. Inputs: "
                        "session_id (str!), goal (str!), provider, model. "
                        "Output: {steps:[one record], done, reason}.")
async def cap_step(session_id: str = "", goal: str = "", provider: str = "ollama",
                   model: str = "", trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s:
        return {"error": "no such session"}
    policy = _policy_for_session(s)
    return await _loop.run_loop(goal, s, call_cap=_call, policy=policy,
                                provider=provider, model=model, max_steps=1,
                                canvas=(s.target or {}).get("canvas", False),
                                shots_dir=_shots_dir(session_id))


@capability("operator.run", memory="on",
            http_method="POST", http_path="/operator/run", http_tags=["operator"],
            description="Drive a REAL browser session to a goal via observe→think→act — a "
                        "general-purpose web operator, not just a verification tool. WHEN TO "
                        "USE: any goal that means actually operating a real page — click a "
                        "button/link, fill a form and submit it, navigate a site's own "
                        "structure to find something (menus, search boxes, pagination), read/"
                        "extract real content off a rendered page (including JS-rendered "
                        "content a plain HTTP fetch never sees), OR verify a webpage's visible "
                        "UI actually changed after an interaction (a status text flipping, a "
                        "page navigating, an element appearing) — that's the tool for checking "
                        "a page/interface really works, not just that its file exists, but it "
                        "is one of several jobs this cap does, not the only one. For a pure "
                        "text/data lookup that doesn't need real navigation prefer the lighter "
                        "web.research (query-driven) or web.crawl (known-site, link-following) "
                        "— reach for operator.run when the page itself needs to be DRIVEN, not "
                        "just read. "
                        "Inputs: goal (str!), url OR kind+base_url (target), provider, "
                        "model, max_steps (15), session_id (reuse), allowlist (extra "
                        "hosts), dry_run, allow_destructive, keep_open, branch. "
                        "Output: {done, reason, steps:[...], run_id, screenshots}.",
            schema=enum_schema(kind=["url", "live", "sandbox", "panel", "codeserver", "vm"]))
async def cap_run(goal: str = "", url: str = "", kind: str = "", base_url: str = "",
                  provider: str = "ollama", model: str = "", max_steps: int = 15,
                  session_id: str = "", allowlist: Optional[List[str]] = None,
                  dry_run: Optional[bool] = None, allow_destructive: Optional[bool] = None,
                  keep_open: bool = False, branch: str = "", panel_id: str = "",
                  id: str = "", record_gif: bool = False, gif_duration_ms: int = 900,
                  trace_id=None) -> Dict[str, Any]:
    if not (goal or "").strip():
        return {"error": "goal required"}
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    own = False
    s = _be.get_session(session_id) if session_id else None
    if not s:
        start = await _open_session(url=url, kind=kind, base_url=base_url, branch=branch,
                                    panel_id=panel_id, cs_id=id, allowlist=allowlist,
                                    allow_destructive=allow_destructive, dry_run=dry_run)
        if start.get("error"):
            return start
        s = _be.get_session(start["session_id"])
        own = True
    resolved = s.target or {}
    # Session policy (set at connect) UNIONed with any per-run overrides.
    policy = _policy_for_session(s, allowlist=allowlist, dry_run=dry_run,
                                 allow_destructive=allow_destructive)
    run_id = uuid.uuid4().hex[:10]
    shots = _shots_dir(s.session_id) + f"/run-{run_id}"

    async def _on_step(rec: Dict[str, Any]):
        shot = rec.get("screenshot", "")
        await emit_event({"type": "operator.step", "run_id": run_id,
                          "i": rec.get("i"), "phase": rec.get("phase"),
                          "action": rec.get("action"), "thought": rec.get("thought", "")[:200],
                          "reason": rec.get("reason", ""), "error": rec.get("error", ""),
                          "screenshot": f"/operator/artifact?path={_artifact_rel(shot)}" if shot else ""})

    await emit_event({"type": "operator.run", "stage": "start", "run_id": run_id,
                      "goal": goal[:200], "target": resolved.get("kind")})
    result = await _loop.run_loop(
        goal, s, call_cap=_call, policy=policy, provider=provider, model=model,
        max_steps=int(max_steps), canvas=resolved.get("canvas", False),
        shots_dir=shots, on_step=_on_step)
    # Assemble the per-step screenshots into a GIF of the whole run (the frames
    # already exist — this is nearly free).
    if record_gif and result.get("screenshots"):
        gif_path = os.path.join(shots, "run.gif")
        ga = _capture.assemble_gif(result["screenshots"], gif_path,
                                   duration_ms=int(gif_duration_ms))
        if ga.get("ok"):
            result["gif"] = f"/operator/artifact?path={_artifact_rel(gif_path)}"
            result["gif_path"] = gif_path
            result["gif_frames"] = ga.get("frames")
        else:
            result["gif_error"] = ga.get("error")
    if own and not keep_open:
        await _be.close_session(s.session_id)
    result.update({"run_id": run_id, "goal": goal, "target": resolved.get("kind"),
                   "session_id": s.session_id if (keep_open or not own) else ""})
    await emit_event({"type": "operator.run", "stage": "done", "run_id": run_id,
                      "reason": result.get("reason"), "steps": result.get("step_count"),
                      "gif": result.get("gif", "")})
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  MISSIONS  (+ docs aliases)
# ─────────────────────────────────────────────────────────────────────────────
def _mission_ctx() -> Dict[str, Any]:
    return {"call_cap": _call, "emit": emit_event, "repo_root": str(_repo_root()),
            "default_base_url": _default_base_url()}


@capability("operator.mission.list", memory="off", silent=True,
            http_method="GET", http_path="/operator/mission/list", http_tags=["operator"],
            description="List available operator missions. Output: {missions:{name:desc}}.")
async def cap_mission_list(trace_id=None) -> Dict[str, Any]:
    return {"missions": list_missions()}


@capability("operator.mission.run", memory="on",
            http_method="POST", http_path="/operator/mission/run", http_tags=["operator"],
            description="Run a named operator mission. Inputs: mission (str!, e.g. "
                        "'documentation'), target (sandbox|live|{...}), domains "
                        "(list of doc slugs, empty=all), base_url, capture (bool), "
                        "write_docs (bool). Output depends on the mission.")
async def cap_mission_run(mission: str = "", target: str = "sandbox",
                          domains: Optional[List[str]] = None, base_url: str = "",
                          capture: bool = True, write_docs: bool = True,
                          trace_id=None) -> Dict[str, Any]:
    if not mission:
        return {"error": "mission required"}
    params = {"target": target, "domains": domains or [], "base_url": base_url,
              "capture": bool(capture), "write_docs": bool(write_docs)}
    return await run_mission(mission, params, _mission_ctx())


@capability("docs.build", memory="on",
            http_method="POST", http_path="/docs/build", http_tags=["docs", "operator"],
            description="Build/refresh Vera's documentation: screenshot every UI "
                        "panel (seeded) on a target Vera and regenerate the doc "
                        "auto-blocks + gallery. Alias for the 'documentation' "
                        "mission. Inputs: target (sandbox|live), domains (list, "
                        "empty=all), panels (list of panel ids for SELECTIVE "
                        "re-capture — leaves the rest untouched), settle_ms (extra "
                        "wait per panel, default 1400), full_page, base_url, capture, "
                        "write_docs. Output: {screenshots, domains, capabilities, ...}.")
async def cap_docs_build(target: str = "sandbox", domains: Optional[List[str]] = None,
                         panels: Optional[List[str]] = None, settle_ms: int = 1400,
                         full_page: bool = False, base_url: str = "",
                         capture: bool = True, write_docs: bool = True,
                         trace_id=None) -> Dict[str, Any]:
    params = {"target": target, "domains": domains or [], "panels": panels or [],
              "settle_ms": int(settle_ms), "full_page": bool(full_page),
              "base_url": base_url, "capture": bool(capture),
              "write_docs": bool(write_docs)}
    return await run_mission("documentation", params, _mission_ctx())


@capability("docs.assets", memory="off", silent=True,
            http_method="GET", http_path="/docs/assets", http_tags=["docs", "operator"],
            description="List captured documentation images for the gallery. Scans "
                        "documentation/assets/ on DISK (the source of truth, so a "
                        "stale/empty manifest never hides real images) and enriches "
                        "each with manifest metadata (label/via) when available. "
                        "Output: {domains:[{slug,title,doc,panels:[{id,label,shot,url,"
                        "via,mode,kind}]}], count}.")
async def cap_docs_assets(trace_id=None) -> Dict[str, Any]:
    from Vera.vera.operator.docs import domain_map as _dm
    assets = _repo_root() / "documentation" / "assets"
    if not assets.exists():
        return {"domains": [], "count": 0, "note": "no documentation/assets yet"}
    # Manifest is metadata only (labels, via); disk decides what exists.
    meta: Dict[str, Dict[str, Any]] = {}
    man = assets / "manifest.json"
    if man.exists():
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
            for slug, info in (data.get("domains") or {}).items():
                for p in info.get("panels", []):
                    meta[f"{slug}/{p.get('id')}"] = p
        except Exception:
            pass
    title_of = {d["slug"]: d["title"] for d in _dm.DOMAINS}
    doc_of = {d["slug"]: d["doc"] for d in _dm.DOMAINS}
    out = []
    total = 0
    for sub in sorted(p for p in assets.iterdir() if p.is_dir()):
        slug = sub.name
        pnls = []
        for f in sorted(sub.glob("*.png")) + sorted(sub.glob("*.gif")):
            stem = f.stem
            rel = f"assets/{slug}/{f.name}"
            m = meta.get(f"{slug}/{stem}", {})
            pnls.append({"id": stem, "label": m.get("label", stem), "shot": rel,
                         "url": f"/docs/asset?path={rel}", "via": m.get("via", ""),
                         "mode": m.get("mode", ""), "kind": f.suffix.lstrip(".")})
            total += 1
        if pnls:
            out.append({"slug": slug, "title": title_of.get(slug, slug),
                        "doc": doc_of.get(slug, ""), "panels": pnls})
    return {"domains": out, "count": total}


@capability("docs.capture", memory="on",
            http_method="POST", http_path="/docs/capture", http_tags=["docs", "operator"],
            description="Fulfil <!-- VERA:CAPTURE panel=... steps=... gif=... --> "
                        "directives in the docs: navigate to each panel, run the "
                        "deterministic steps, capture a still/GIF, and insert it in a "
                        "managed block after the directive (idempotent, preserves "
                        "prose). Inputs: doc (one 'NN-slug.md', blank=all docs with "
                        "directives), target (sandbox|live), base_url, branch. "
                        "Output: {ok, captures, docs:{file:n}}.")
async def cap_docs_capture(doc: str = "", target: str = "sandbox", base_url: str = "",
                           branch: str = "", trace_id=None) -> Dict[str, Any]:
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    docs_dir = _repo_root() / "documentation"
    if doc:
        f0 = docs_dir / doc
        if not f0.exists():
            return {"error": f"doc not found: {doc}"}
        files = [f0]
    else:
        files = sorted(docs_dir.glob("*.md"))
    # Only bother with docs that actually contain a directive.
    files = [f for f in files if "VERA:CAPTURE" in f.read_text(encoding="utf-8")]
    if not files:
        return {"ok": True, "captures": 0, "docs": {},
                "note": "no <!-- VERA:CAPTURE --> directives found"}

    tgt: Dict[str, Any] = {"kind": target} if isinstance(target, str) else dict(target)
    if branch:
        tgt["branch"] = branch
    resolved = await _targets.ensure_target(tgt, _call, _default_base_url())
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready")}
    base = base_url or resolved["base_url"]
    try:
        sess = await _be.start_session(base_url=base, target=resolved)
    except Exception as e:
        return {"error": f"session start failed: {e}"}
    caller = _target_caller(base)

    total = 0
    per_doc: Dict[str, int] = {}
    try:
        for f in files:
            md = f.read_text(encoding="utf-8")
            slug = _safe_seg(re.sub(r"^\d+-", "", f.stem))
            out_dir = str(docs_dir / "assets" / slug)
            rel_base = f"assets/{slug}"
            made = 0
            for d in _directives.parse_directives(md):
                attrs = d["attrs"]
                name = _safe_seg(attrs.get("name") or f"capture{made}")
                steps = _directives.directive_steps(attrs)
                await emit_event({"type": "operator.docs.progress", "stage": "capture",
                                  "message": f"[{f.name}] {name}"})
                res = await _tours.run_tour(sess, steps, out_dir=out_dir,
                                            rel_base=rel_base, call_target=caller)
                assets = (res.get("gifs") or []) + (res.get("shots") or [])
                asset = next((a for a in assets if a["name"] == name),
                             assets[-1] if assets else None)
                if not asset:
                    continue
                is_gif = str(asset["path"]).endswith(".gif")
                img = _directives.image_markdown(name, asset["rel"], gif=is_gif)
                # Re-read + re-locate the directive so growing inserts stay valid.
                md = f.read_text(encoding="utf-8")
                pos = md.find(d["raw"])
                after = (pos + len(d["raw"])) if pos >= 0 else None
                md = _directives.upsert_capture(md, name, img, after_pos=after)
                f.write_text(md, encoding="utf-8")
                made += 1
                total += 1
            per_doc[f.name] = made
    finally:
        await _be.close_session(sess.session_id)
    return {"ok": True, "captures": total, "docs": per_doc,
            "target": resolved.get("kind"), "base_url": base}


@capability("docs.gallery", memory="on",
            http_method="POST", http_path="/docs/gallery", http_tags=["docs", "operator"],
            description="Rebuild documentation/README.md gallery from the last "
                        "capture manifest (no screenshots taken). Output: {ok, domains}.")
async def cap_docs_gallery(trace_id=None) -> Dict[str, Any]:
    docs = _repo_root() / "documentation"
    man = docs / "assets" / "manifest.json"
    if not man.exists():
        return {"error": "no manifest — run docs.build first"}
    try:
        data = json.loads(man.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"manifest unreadable: {e}"}
    entries = []
    for slug, info in (data.get("domains") or {}).items():
        panels = info.get("panels", [])
        entries.append({"slug": slug, "title": info.get("title", slug),
                        "doc": info.get("doc", ""),
                        "cover_rel": panels[0]["shot"] if panels else "",
                        "shot_count": len(panels), "cap_count": info.get("cap_count", 0)})
    gal = _gallery.build_gallery(entries, generated_at=data.get("generated_at", ""),
                                 total_caps=sum(e["cap_count"] for e in entries))
    (docs / "README.md").write_text(gal, encoding="utf-8")
    return {"ok": True, "domains": len(entries)}


# ─────────────────────────────────────────────────────────────────────────────
#  CAPTURE  —  GIF / time-lapse
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.capture.start", memory="on",
            http_method="POST", http_path="/operator/capture/start", http_tags=["operator"],
            description="Start a time-lapse: screenshot the session's page every "
                        "interval_ms while a long task runs (a dream cycle, a "
                        "backtest, a loop). Stop with operator.capture.stop to get "
                        "a GIF. Inputs: session_id (str!), interval_ms (1000), "
                        "max_frames (180), name. Output: {capture_id, interval_ms}.")
async def cap_capture_start(session_id: str = "", interval_ms: int = 1000,
                            max_frames: int = 180, name: str = "",
                            trace_id=None) -> Dict[str, Any]:
    s = _be.get_session(session_id)
    if not s or not s.page:
        return {"error": "no live session (operator.session.start first)"}
    cid = _safe_seg(name) if name else ""
    frames_dir = str(_repo_root() / "artifacts" / "operator" / "captures" /
                     (cid or f"cap-{uuid.uuid4().hex[:8]}") / "frames")

    def _get_page():
        cur = _be.get_session(session_id)
        return cur.page if cur else None

    cap = _capture.start_capture(session_id, frames_dir, _get_page,
                                 interval_ms=int(interval_ms),
                                 max_frames=int(max_frames), capture_id=cid)
    await emit_event({"type": "operator.capture", "stage": "start",
                      "capture_id": cap.capture_id, "session_id": session_id})
    return {"ok": True, "capture_id": cap.capture_id, "interval_ms": int(interval_ms),
            "pillow": _capture.pil_available()}


@capability("operator.capture.status", memory="off", silent=True,
            http_method="GET", http_path="/operator/capture/status", http_tags=["operator"],
            description="Status of one capture (capture_id) or all. Output: "
                        "{capture_id, frames, running, ...} or {captures:[...]}.")
async def cap_capture_status(capture_id: str = "", trace_id=None) -> Dict[str, Any]:
    if capture_id:
        c = _capture.get_capture(capture_id)
        return c.summary() if c else {"error": f"no such capture: {capture_id}"}
    return {"captures": _capture.list_captures()}


@capability("operator.capture.stop", memory="on",
            http_method="POST", http_path="/operator/capture/stop", http_tags=["operator"],
            description="Stop a time-lapse and assemble its frames into a GIF. "
                        "Inputs: capture_id (str!), domain (docs domain slug → the "
                        "GIF lands in documentation/assets/<domain>/<name>.gif; blank "
                        "→ artifacts, served live), name, duration_ms (per frame, "
                        "800), max_width (900). Output: {ok, path, rel, url, frames, "
                        "bytes}.")
async def cap_capture_stop(capture_id: str = "", domain: str = "", name: str = "",
                           duration_ms: int = 800, max_width: int = 900,
                           trace_id=None) -> Dict[str, Any]:
    if not capture_id:
        return {"error": "capture_id required"}
    cap = await _capture.stop_capture(capture_id)
    if not cap:
        return {"error": f"no such capture: {capture_id}"}
    if not cap.frames:
        return {"error": "capture produced no frames", "capture_id": capture_id}
    out = _gif_out_path(domain, name or capture_id)
    res = _capture.assemble_gif(cap.frames, out["path"], duration_ms=int(duration_ms),
                                max_width=int(max_width))
    if res.get("error"):
        return res
    await emit_event({"type": "operator.capture", "stage": "gif",
                      "capture_id": capture_id, "frames": res.get("frames"),
                      "url": out.get("url", ""), "rel": out.get("rel", "")})
    return {"ok": True, "capture_id": capture_id, "frames": res["frames"],
            "bytes": res["bytes"], "path": out["path"], "rel": out["rel"],
            "url": out["url"]}


# ─────────────────────────────────────────────────────────────────────────────
#  TOURS  —  deterministic scripted walkthroughs (stills + GIFs), no LLM
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.tour.list", memory="off", silent=True,
            http_method="GET", http_path="/operator/tour/list", http_tags=["operator"],
            description="List available scripted tours. Output: {tours:[slug,...]}.")
async def cap_tour_list(trace_id=None) -> Dict[str, Any]:
    return {"tours": _tours.list_tours()}


@capability("operator.tour.run", memory="on",
            http_method="POST", http_path="/operator/tour/run", http_tags=["operator"],
            description="Run a deterministic scripted tour of a domain's UI and "
                        "capture stills + GIF clips into documentation/assets/<slug>/. "
                        "Reproducible 'in-action' docs without the LLM. Inputs: slug "
                        "(str! — see operator.tour.list), target (sandbox|live), "
                        "base_url, branch (sandbox). Output: {shots:[...], gifs:[...], "
                        "errors, slug}.")
async def cap_tour_run(slug: str = "", target: str = "sandbox", base_url: str = "",
                       branch: str = "", trace_id=None) -> Dict[str, Any]:
    tour = _tours.get_tour(slug)
    if not tour:
        return {"error": f"unknown tour '{slug}'. Available: {_tours.list_tours()}"}
    if not _be.playwright_available():
        return {"error": _be.INSTALL_HINT}
    tgt: Dict[str, Any] = {"kind": target} if isinstance(target, str) else dict(target)
    if branch:
        tgt["branch"] = branch
    resolved = await _targets.ensure_target(tgt, _call, _default_base_url())
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready"), "target": resolved}
    base = base_url or resolved["base_url"]
    try:
        sess = await _be.start_session(base_url=base, target=resolved)
    except Exception as e:
        return {"error": f"session start failed: {e}"}

    caller = _target_caller(base)
    # Seed representative data so the tour renders populated.
    if tour.get("seed"):
        from Vera.vera.operator.missions import seeds as _seeds
        try:
            await _seeds.run_seed(tour["seed"], caller)
        except Exception as e:
            log.debug("tour seed failed: %s", e)

    out_dir = str(_repo_root() / "documentation" / "assets" / _safe_seg(slug))
    rel_base = f"assets/{_safe_seg(slug)}"

    async def _emit(**k):
        await emit_event({"type": "operator.tour", "slug": slug, **k})

    await emit_event({"type": "operator.tour", "stage": "start", "slug": slug})
    try:
        res = await _tours.run_tour(sess, tour["steps"], out_dir=out_dir,
                                    rel_base=rel_base, call_target=caller, emit=_emit)
    finally:
        await _be.close_session(sess.session_id)
    res.update({"slug": slug, "title": tour.get("title"),
                "target": resolved.get("kind"), "base_url": base})
    await emit_event({"type": "operator.tour", "stage": "done", "slug": slug,
                      "shots": len(res.get("shots", [])), "gifs": len(res.get("gifs", []))})
    return res


# ─────────────────────────────────────────────────────────────────────────────
#  TESTING
# ─────────────────────────────────────────────────────────────────────────────
@capability("operator.test.run", memory="on",
            http_method="POST", http_path="/operator/test/run", http_tags=["operator"],
            description="Run the project's pytest unit suite. Inputs: path (default "
                        "'tests'), k (pytest -k expression). Output: {ok, code, out}.")
async def cap_test_run(path: str = "tests", k: str = "", trace_id=None) -> Dict[str, Any]:
    root = _repo_root()
    cmd = [sys.executable, "-m", "pytest", path or "tests", "-q"]
    if k:
        cmd += ["-k", k]

    def _run():
        try:
            p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                               timeout=600)
            return {"code": p.returncode, "out": (p.stdout or "")[-6000:],
                    "err": (p.stderr or "")[-2000:]}
        except Exception as e:
            return {"code": -1, "out": "", "err": str(e)}

    res = await asyncio.get_event_loop().run_in_executor(None, _run)
    res["ok"] = res.get("code") == 0
    return res


# ─────────────────────────────────────────────────────────────────────────────
#  RAW ROUTES: artifact serving + Operator Studio panel
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/operator/artifact", include_in_schema=False)
async def _operator_artifact(path: str = ""):
    from fastapi.responses import FileResponse, JSONResponse
    root = (_repo_root() / "artifacts" / "operator").resolve()
    target = (root / (path or "").lstrip("/\\")).resolve()
    if root != target and root not in target.parents:
        return JSONResponse({"error": "path escapes artifact root"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = target.suffix.lower()
    mt = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".webm": "video/webm", ".mp4": "video/mp4"}.get(ext, "image/png")
    return FileResponse(str(target), media_type=mt)


@APP.get("/docs/asset", include_in_schema=False)
async def _docs_asset(path: str = ""):
    """Serve a committed documentation screenshot/GIF (documentation/assets/…)
    so the Operator Studio gallery can display them. Path-jailed to that dir."""
    from fastapi.responses import FileResponse, JSONResponse
    root = (_repo_root() / "documentation").resolve()
    rel = (path or "").lstrip("/\\")
    target = (root / rel).resolve()
    assets = (root / "assets").resolve()
    if assets != target and assets not in target.parents:
        return JSONResponse({"error": "path escapes docs assets"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = target.suffix.lower()
    mt = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
    return FileResponse(str(target), media_type=mt)


@APP.get("/operator/panel", include_in_schema=False)
async def _operator_panel():
    from fastapi.responses import HTMLResponse
    if _PANEL_PATH.exists():
        return HTMLResponse(_PANEL_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:#c96b6b'>operator_studio_panel.html not found</p>",
                        status_code=404)


register_ui(
    "operator-studio", "Operator", "🕹",
    """<div id="operator-mount" style="height:100%;display:flex;flex-direction:column;">
        <iframe src="/operator/panel"
                style="flex:1;border:none;width:100%;height:100%"></iframe>
    </div>""",
    "",
    ui_caps=["operator.session.start", "operator.session.status",
             "operator.session.close", "operator.observe", "operator.read",
             "operator.screenshot", "operator.act", "operator.think",
             "operator.step", "operator.run", "operator.mission.list",
             "operator.mission.run", "docs.build", "docs.gallery",
             "operator.test.run"],
    mode="tab", tab_order=73,
)

log.info("operator: capabilities registered (playwright=%s)", _be.playwright_available())
