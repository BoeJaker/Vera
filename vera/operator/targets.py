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
        start = _panel_url(base, target["panel_id"]) if target.get("panel_id") else ""
        return {"kind": "sandbox", "base_url": base, "start_url": start, "canvas": False}

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
    if call_cap and (branch or name):
        try:
            lst = await call_cap("evolve.sandbox.list")
            for s in (lst or {}).get("sandboxes", []):
                if s.get("running") and s.get("url") and (
                        (branch and s.get("branch") == branch) or
                        (name and s.get("name") == name)):
                    resolved["base_url"] = s["url"]
                    resolved["start_url"] = (_panel_url(s["url"], target["panel_id"])
                                             if target.get("panel_id") else "")
                    resolved.update({"ready": True, "error": "", "container": s.get("name")})
                    return resolved
        except Exception as e:
            log.debug("sandbox target list lookup failed: %s", e)

    # Boot / ensure the PRIMARY sandbox Vera. evolve.sandbox.ensure is idempotent.
    res = await call_cap("evolve.sandbox.ensure", branch=branch) if call_cap else \
        {"error": "no cap dispatcher"}
    if isinstance(res, dict) and res.get("error"):
        resolved.update({"ready": False, "error": f"sandbox ensure: {res['error']}"})
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
