"""targets.py — resolve a target spec to a base URL + starting page.

A **target** says *what* the operator should drive. Supported kinds:

  • ``url``       — any web page: {"kind":"url","url":"https://…"}
  • ``live``      — Vera's own running UI (this orchestrator's origin)
  • ``sandbox``   — a loop-lab sandbox Vera (evolve.sandbox.ensure, :8998)
  • ``panel``     — a specific Vera panel window: {"panel_id":"markets", ...}
  • ``codeserver``— a browser-served IDE: {"id":"sbxw-…"} → /vscode/{id}/
  • ``vm``/``novnc`` — a desktop served in-browser (canvas; acts are xy)

``resolve_target`` is pure. ``ensure_target`` additionally boots a sandbox when
asked, via an injected ``call_cap`` coroutine (the caps module passes the
in-process ``_call``) so this file never imports the orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from . import target_fallback as _fallback

log = logging.getLogger("vera.operator.targets")

SANDBOX_BASE = "http://localhost:8998"


def _panel_url(base_url: str, panel_id: str) -> str:
    return f"{base_url.rstrip('/')}/ui/panel/window?id={panel_id}"


def resolve_target(target: Dict[str, Any], default_base_url: str = "") -> Dict[str, Any]:
    """Pure resolution → {kind, base_url, start_url, canvas}.

    ``canvas`` True hints the surface is opaque (VM/desktop) so acts should use
    xy rather than element refs. ``start_url`` may be empty (caller navigates).
    """
    target = dict(target or {})
    kind = (target.get("kind") or ("url" if target.get("url") else
            "panel" if target.get("panel_id") else "live")).lower()

    if kind == "url":
        url = str(target.get("url") or "").strip()
        base = target.get("base_url") or _origin(url) or default_base_url
        return {"kind": "url", "base_url": base, "start_url": url, "canvas": False}

    if kind == "live":
        base = target.get("base_url") or default_base_url or "http://localhost:8999"
        start = _panel_url(base, target["panel_id"]) if target.get("panel_id") else base
        return {"kind": "live", "base_url": base, "start_url": start, "canvas": False}

    if kind == "sandbox":
        base = target.get("base_url") or SANDBOX_BASE
        # An EXPLICIT url wins. Without this the caller's url was silently
        # dropped: kind="sandbox" resolved a base and an EMPTY start_url, so
        # _open_session skipped its goto and the browser sat on about:blank.
        # Observed 2026-08-27 (session 5be4562e): the loop asked the operator to
        # verify a countdown page and passed the correct preview url alongside
        # kind="sandbox" - which in THIS vocabulary means a Loop Lab dev
        # container, not the session's workspace preview. The url was discarded,
        # the operator woke on a blank page with the goal "click the Start
        # button", went looking for a countdown timer on the open internet, was
        # allowlist-blocked twice, and ended up clicking around Vera's own UI.
        # Roughly fifteen cycles of churn from one dropped argument.
        _u = str(target.get("url") or "").strip()
        if target.get("panel_id"):
            start = _panel_url(base, target["panel_id"])
        else:
            start = _u
        return {"kind": "sandbox", "base_url": (base or _origin(_u) or base),
                "start_url": start, "canvas": False}

    if kind == "panel":
        base = target.get("base_url") or default_base_url or "http://localhost:8999"
        pid = str(target.get("panel_id") or "").strip()
        return {"kind": "panel", "base_url": base,
                "start_url": _panel_url(base, pid) if pid else base, "canvas": False}

    if kind == "codeserver":
        base = target.get("base_url") or default_base_url or "http://localhost:8999"
        cid = str(target.get("id") or "").strip()
        start = f"{base.rstrip('/')}/vscode/{cid}/" if cid else base
        return {"kind": "codeserver", "base_url": base, "start_url": start, "canvas": False}

    if kind in ("vm", "novnc", "desktop"):
        url = str(target.get("url") or "").strip()
        base = target.get("base_url") or _origin(url) or default_base_url
        return {"kind": "vm", "base_url": base, "start_url": url, "canvas": True}

    # Fallback: treat as a raw url/base.
    base = target.get("base_url") or default_base_url
    return {"kind": kind, "base_url": base,
            "start_url": str(target.get("url") or target.get("start_url") or ""),
            "canvas": bool(target.get("canvas"))}


def _origin(url: str) -> str:
    from urllib.parse import urlparse
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return ""


async def _boot_reserved(call_cap) -> Optional[dict]:
    """Bring the reserved standing container up, or None if it cannot be.

    Returns a sandbox-shaped dict so the caller can adopt it directly. A failure
    here is not fatal - the caller still has the old primary path behind it -
    so every error is swallowed deliberately and logged rather than raised.
    """
    try:
        res = await call_cap("evolve.bleeding_edge.container.ensure")
    except Exception as e:
        log.debug("reserved sandbox ensure failed: %s", e)
        return None
    if not isinstance(res, dict) or res.get("error") or not res.get("url"):
        log.debug("reserved sandbox ensure returned nothing driveable: %r", res)
        return None
    if not res.get("reachable", True):
        log.debug("reserved sandbox came up unreachable: %r", res.get("name"))
        return None
    return dict(res)


async def ensure_target(target: Dict[str, Any],
                        call_cap: Callable[..., Awaitable[Any]],
                        default_base_url: str = "") -> Dict[str, Any]:
    """Resolve + make the target reachable. For ``sandbox`` this boots/ensures
    the loop-lab sandbox via ``evolve.sandbox.ensure`` and reads back its port.

    Returns the resolved dict plus {ready, error}.
    """
    # Connector targets (integration / ollama / node / docker / proxmox): resolve
    # against the registered infrastructure via its existing caps.
    from . import connectors as _connectors
    source = (target.get("source") or "").lower()
    if not source and target.get("kind") in _connectors.CONNECTOR_SOURCES and target.get("ref"):
        source = target["kind"]
    if source in _connectors.CONNECTOR_SOURCES:
        if not call_cap:
            return {"kind": source, "base_url": "", "start_url": "", "canvas": False,
                    "ready": False, "error": "no cap dispatcher"}
        res = await _connectors.resolve(source, target.get("ref", ""), call_cap)
        if isinstance(res, dict) and res.get("error"):
            return {"kind": source, "base_url": "", "start_url": "", "canvas": False,
                    "ready": False, "error": res["error"]}
        return {"kind": source, "base_url": res.get("base_url", ""),
                "start_url": res.get("url", ""), "canvas": bool(res.get("canvas")),
                "ready": True, "error": "", "conn_type": res.get("type", ""),
                "driveable": res.get("driveable", True), "ref": target.get("ref", "")}

    resolved = resolve_target(target, default_base_url)
    if resolved["kind"] != "sandbox":
        resolved.update({"ready": True, "error": ""})
        return resolved

    # A SPECIFIC per-branch dev container (not just the primary): resolve it from
    # evolve.sandbox.list — every live sandbox (primary + spawned) is thus a
    # driveable operator target, at its own scheme-aware url (HTTPS now; the
    # operator browser already ignores the self-signed cert).
    branch = target.get("branch") or ""
    name = target.get("name") or ""

    def _adopt(s):
        resolved["base_url"] = s["url"]
        resolved["start_url"] = (_panel_url(s["url"], target["panel_id"])
                                 if target.get("panel_id") else resolved.get("start_url") or "")
        resolved.update({"ready": True, "error": "", "container": s.get("name")})
        return resolved

    async def _list_sandboxes():
        try:
            lst = await call_cap("evolve.sandbox.list")
            return (lst or {}).get("sandboxes", []) or []
        except Exception as e:
            log.debug("sandbox target list lookup failed: %s", e)
            return []

    if call_cap and (branch or name):
        hit = _fallback.pick_sandbox(await _list_sandboxes(), branch=branch, name=name)
        if hit:
            return _adopt(hit)

    # NOTHING NAMED - "just give me a browser". That request has no claim on the
    # PRIMARY, which is a one-owner-at-a-time singleton belonging to whichever
    # agent is working in it. Asking for it first and only falling back on
    # refusal made contention the normal path: census 26's
    # build-browser-verified burned its entire 1800s wall cap losing that race
    # ("primary sandbox is occupied by another branch"), and runs 16 and 19 died
    # the same way. The reserved standing container exists precisely so a
    # browser step does not have to compete for anything, so go there FIRST and
    # leave the primary alone.
    if call_cap and not (branch or name):
        reserved = _fallback.pick_reserved(await _list_sandboxes())
        if not reserved:
            # No reservation is up. BRING ONE UP rather than reaching for the
            # primary - the standing container is idempotent and pinned, so
            # this converges on the reserved sandbox instead of a queue.
            reserved = await _boot_reserved(call_cap)
        if reserved:
            _adopt(reserved)
            resolved["note"] = _fallback.reservation_note(reserved)
            log.info("operator target: %s", resolved["note"])
            return resolved

    # Boot / ensure the PRIMARY sandbox Vera. evolve.sandbox.ensure is idempotent.
    res = await call_cap("evolve.sandbox.ensure", branch=branch) if call_cap else \
        {"error": "no cap dispatcher"}
    if isinstance(res, dict) and res.get("error"):
        # The primary is a one-owner-at-a-time singleton, so on a busy estate
        # this is the NORMAL outcome, not an exceptional one - and it used to
        # end the browser step outright even with a standing sandbox running
        # and idle. Nobody named a container, so any safe one will do; see
        # target_fallback for what "safe" excludes.
        # Fall back even when a branch/name WAS named. The named lookup above
        # already failed, so that container does not exist - and refusing to
        # substitute then just fails the step, which is strictly worse than
        # driving a safe sandbox and saying so. Census run 19: an operator.run
        # died in 2.4s on the occupied primary while a pinned, running,
        # driveable sandbox was registered the whole time, because the model had
        # named a branch and that silently disabled this.
        _pool = await _list_sandboxes() if call_cap else []
        alt = _fallback.pick_sandbox(_pool) if call_cap else None
        if alt:
            _adopt(alt)
            note = _fallback.substitution_note(alt, res["error"])
            if branch or name:
                note += (f" (the requested {'branch ' + branch if branch else 'name ' + name}"
                         " has no live sandbox)")
            resolved["note"] = note
            resolved["primary_error"] = res["error"]
            log.info("operator target: %s", note)
            return resolved
        # Nothing was chosen - say WHY, or the next occurrence is as opaque as
        # this one was.
        why = _fallback.decline_reason(_pool, branch=branch, name=name)
        log.warning("operator target: primary unavailable (%s) and no fallback "
                    "taken - %s", res["error"], why)
        resolved.update({"ready": False,
                         "error": f"sandbox ensure: {res['error']} - {why}"})
        return resolved
    # Prefer a base_url/port the cap reports back, else the default.
    base = SANDBOX_BASE
    if isinstance(res, dict):
        if res.get("base_url"):
            base = res["base_url"]
        elif res.get("port"):
            base = f"http://localhost:{res['port']}"
    resolved["base_url"] = base
    if target.get("panel_id"):
        resolved["start_url"] = _panel_url(base, target["panel_id"])
    resolved.update({"ready": True, "error": "", "ensure": res})
    return resolved
