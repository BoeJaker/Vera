# ============================================================================
# mesh_ui_capabilities.py — server-driven UI for display nodes + auto-OTA + SD
# ============================================================================
#
# A mesh display node (ILI9488 touch shield) is a thin renderer: Vera pushes a
# "screen" (a list of widgets) and the node draws it; the node reports touches
# back (increment 2) so a tap can run a capability. All app logic stays here —
# so "apps" (status, system monitor, macro pad, chat, companion/sprite viewer)
# are just screen-builders on the server, no reflashing to add one.
#
#   • mesh.ui.screen  — push a raw screen definition
#   • mesh.ui.text    — quick title + body screen
#   • mesh.ui.home    — the app launcher (tiles)
#   • mesh.ui.sysmon  — a live Vera + node system-monitor screen
#   • mesh.ui.macropad— a grid of buttons that each trigger a capability
#   • mesh.sd.ls/cat/put — SD "file server" access (wraps the node sd_* jobs)
#   • auto-OTA        — a node whose config.ota.auto is set and whose FW_VERSION
#                       is behind the served firmware gets an OTA queued on hello
#
# Widget schema (rendered by the firmware): {t, x, y, ...}
#   label  {text,color,bg,size}   rect {w,h,color,fill}   hline {w,color}
#   button {w,h,text,color,bg,size,action}   bar {w,h,val(0-100),color,label}
# Colours are RGB565 ints (see the C_* table below).
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso

log = logging.getLogger("vera.mesh.ui")

_FW_MPY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "firmware", "micropython", "main.py")

# RGB565 palette (must match the firmware constants)
C_BLACK, C_WHITE, C_GREEN, C_RED = 0x0000, 0xFFFF, 0x07E0, 0xF800
C_YELL, C_GREY, C_NAVY, C_BLUE, C_CYAN = 0xFFE0, 0x8410, 0x000F, 0x001F, 0x07FF

# Per-node action→capability map set by mesh.ui.macropad, consumed when a touch
# event arrives (increment 2 touch routing).
_MACROS: Dict[str, Dict[str, dict]] = {}
_OTA_SENT: Dict[str, str] = {}          # node_id → fw version we last queued OTA for


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _call_cap(name: str, **kwargs) -> Any:
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"unknown_cap:{name}"}
    try:
        return await cap["func"](**kwargs)
    except TypeError:
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        return await cap["func"](**{k: v for k, v in kwargs.items() if k in accepted})
    except Exception as e:
        log.warning("mesh_ui _call_cap %s: %s", name, e)
        return {"error": f"{type(e).__name__}: {e}"}


async def _push(node_id: str, screen: dict) -> dict:
    r = await _call_cap("mesh.send", node_id=node_id, type="ui_screen",
                        payload={"screen": screen})
    return r


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Screen builders
# ─────────────────────────────────────────────────────────────────────────────

def _grid_buttons(items: List[dict], cols: int = 2, x0: int = 8, y0: int = 46,
                  bw: int = 150, bh: int = 46, gx: int = 10, gy: int = 10) -> List[dict]:
    """Lay out button widgets in a grid. items: [{text, action, color?, bg?}]."""
    out = []
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        out.append({"t": "button", "x": x0 + c * (bw + gx), "y": y0 + r * (bh + gy),
                    "w": bw, "h": bh, "text": it.get("text", "?"),
                    "action": it.get("action", ""),
                    "color": it.get("color", C_WHITE), "bg": it.get("bg", C_NAVY),
                    "size": it.get("size", 2)})
    return out


def build_home(node_name: str = "") -> dict:
    tiles = [
        {"text": "Status",    "action": "nav:status",    "bg": C_NAVY},
        {"text": "SysMon",    "action": "nav:sysmon",    "bg": C_NAVY},
        {"text": "Macros",    "action": "nav:macros",    "bg": C_NAVY},
        {"text": "Companion", "action": "nav:companion", "bg": C_NAVY},
    ]
    return {"title": "Vera" + (" · " + node_name if node_name else ""),
            "bg": C_BLACK, "widgets": _grid_buttons(tiles, cols=2)}


def _bar_row(label: str, val: float, y: int, color: int = C_GREEN,
             suffix: str = "%") -> List[dict]:
    v = max(0, min(100, int(val)))
    return [
        {"t": "label", "x": 8, "y": y, "text": label, "color": C_WHITE, "size": 1},
        {"t": "bar", "x": 8, "y": y + 12, "w": 300, "h": 14, "val": v, "color": color},
        {"t": "label", "x": 315, "y": y + 12, "text": str(int(val)) + suffix,
         "color": C_CYAN, "size": 1},
    ]


async def build_sysmon(node_id: str) -> dict:
    """A system-monitor screen: the node's own vitals + Vera stack summary."""
    widgets: List[dict] = []
    y = 44
    # Node vitals (from the mesh record / latest telemetry)
    node = await _call_cap("mesh.node", node_id=node_id)
    n = (node or {}).get("node") if isinstance(node, dict) else None
    tele = (n or {}).get("telemetry") or {}
    rssi = _num(tele.get("rssi") or (n or {}).get("rssi"), -100)
    rssi_pct = max(0, min(100, (rssi + 100) * 2))          # -100..-50 dBm → 0..100
    heap = _num(tele.get("mem") or tele.get("heap"), 0)
    for w in _bar_row("Wi-Fi RSSI  %ddBm" % int(rssi), rssi_pct, y, C_GREEN, ""):
        widgets.append(w)
    y += 42
    if heap:
        for w in _bar_row("Free heap  %dKB" % int(heap / 1024), min(100, heap / 3000), y, C_CYAN, ""):
            widgets.append(w)
        y += 42
    # Vera stack (best-effort — sysmon.status shape varies, so pull generically)
    st = await _call_cap("sysmon.status")
    if isinstance(st, dict) and not st.get("error"):
        flat = st.get("summary") or st
        for key, lbl, col in (("cpu", "Vera CPU", C_YELL), ("cpu_pct", "Vera CPU", C_YELL),
                              ("mem", "Vera RAM", C_RED), ("mem_pct", "Vera RAM", C_RED)):
            v = flat.get(key) if isinstance(flat, dict) else None
            if isinstance(v, (int, float)):
                for w in _bar_row(lbl, _num(v), y, col):
                    widgets.append(w)
                y += 42
                if y > 260:
                    break
    widgets.append({"t": "button", "x": 8, "y": 288, "w": 100, "h": 34,
                    "text": "Home", "action": "nav:home", "bg": C_NAVY})
    return {"title": "System Monitor", "bg": C_BLACK, "widgets": widgets}


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "mesh.ui.screen", http_method="POST", http_path="/mesh/ui/screen",
    http_tags=["mesh", "ui"], memory="on",
    description="Push a UI screen to a display node. The node renders the widgets and (with "
                "touch) reports taps. Input: node_id (str!), screen (JSON — {title?, bg?, "
                "widgets:[{t,x,y,...}]} where t is label|rect|hline|button|bar; colours are "
                "RGB565 ints). Output: {ok, job_id}.",
)
async def cap_mesh_ui_screen(node_id: str = "", screen=None, trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    if isinstance(screen, str):
        try:
            screen = json.loads(screen)
        except Exception:
            return {"error": "screen must be JSON"}
    if not isinstance(screen, dict):
        return {"error": "screen object required"}
    return await _push(node_id, screen)


@capability(
    "mesh.ui.text", http_method="POST", http_path="/mesh/ui/text",
    http_tags=["mesh", "ui"], memory="on",
    description="Quick text screen on a display node. Input: node_id (str!), title (str), "
                "body (str — \\n for line breaks), color (RGB565 int), bg (RGB565 int), "
                "size (int 1-4). Output: {ok, job_id}.",
)
async def cap_mesh_ui_text(node_id: str = "", title: str = "", body: str = "",
                           color: int = C_WHITE, bg: int = C_BLACK, size: int = 2,
                           trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    widgets = []
    y = 46
    for line in (body or "").split("\n"):
        widgets.append({"t": "label", "x": 8, "y": y, "text": line,
                        "color": int(color), "size": int(size)})
        y += 10 * int(size) + 6
    return await _push(node_id, {"title": title, "bg": int(bg), "widgets": widgets})


@capability(
    "mesh.ui.home", http_method="POST", http_path="/mesh/ui/home",
    http_tags=["mesh", "ui"], memory="on",
    description="Show the app launcher (Status / SysMon / Macros / Companion tiles) on a "
                "display node. Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_ui_home(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _push(node_id, build_home(node_id))


@capability(
    "mesh.ui.sysmon", http_method="POST", http_path="/mesh/ui/sysmon",
    http_tags=["mesh", "ui"], memory="on",
    description="Render a live system-monitor screen (the node's Wi-Fi/heap vitals + a Vera "
                "stack summary) on a display node. Call on a timer to keep it live. "
                "Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_ui_sysmon(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _push(node_id, await build_sysmon(node_id))


@capability(
    "mesh.ui.macropad", http_method="POST", http_path="/mesh/ui/macropad",
    http_tags=["mesh", "ui"], memory="on",
    description="Render a macro pad — a grid of buttons that each trigger a Vera capability when "
                "tapped (tap routing lands with the touch increment; the pad renders now). "
                "Input: node_id (str!), buttons (JSON list [{label, cap, args?}]), cols (int=2). "
                "Output: {ok, job_id, buttons}.",
)
async def cap_mesh_ui_macropad(node_id: str = "", buttons=None, cols: int = 2,
                               trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    if isinstance(buttons, str):
        try:
            buttons = json.loads(buttons)
        except Exception:
            return {"error": "buttons must be JSON"}
    buttons = buttons or []
    items, mapping = [], {}
    for i, b in enumerate(buttons):
        if not isinstance(b, dict):
            continue
        action = "macro:%d" % i
        items.append({"text": b.get("label") or b.get("cap") or "?", "action": action,
                      "bg": int(b.get("bg", C_NAVY))})
        mapping[action] = {"cap": b.get("cap"), "args": b.get("args") or {}}
    _MACROS[node_id] = mapping
    items.append({"text": "Home", "action": "nav:home", "bg": C_GREY})
    screen = {"title": "Macro Pad", "bg": C_BLACK,
              "widgets": _grid_buttons(items, cols=max(1, int(cols)))}
    r = await _push(node_id, screen)
    r["buttons"] = len(mapping)
    return r


# ── SD "file server" — thin shortcuts over the node's sd_* jobs ──────────────

@capability(
    "mesh.sd.ls", http_method="POST", http_path="/mesh/sd/ls", http_tags=["mesh", "sd"],
    memory="on",
    description="List a directory on a node's SD card (the listing returns as the job result — "
                "read it back with mesh.jobs). Input: node_id (str!), path (str='/'). "
                "Output: {ok, job_id}.",
)
async def cap_mesh_sd_ls(node_id: str = "", path: str = "/", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type="sd_list", payload={"path": path})


@capability(
    "mesh.sd.cat", http_method="POST", http_path="/mesh/sd/cat", http_tags=["mesh", "sd"],
    memory="on",
    description="Read a file from a node's SD card (result returns via mesh.jobs; ≤1400 bytes "
                "per call). Input: node_id (str!), path (str!), max (int=1024). Output: {ok, job_id}.",
)
async def cap_mesh_sd_cat(node_id: str = "", path: str = "", max: int = 1024, trace_id=None) -> dict:
    if not node_id or not path:
        return {"error": "node_id and path required"}
    return await _call_cap("mesh.send", node_id=node_id, type="sd_read",
                           payload={"path": path, "max": int(max)})


@capability(
    "mesh.sd.put", http_method="POST", http_path="/mesh/sd/put", http_tags=["mesh", "sd"],
    memory="on",
    description="Write/append a file on a node's SD card. Input: node_id (str!), path (str!), "
                "content (str!), append (bool=False). Output: {ok, job_id}.",
)
async def cap_mesh_sd_put(node_id: str = "", path: str = "", content: str = "",
                          append: bool = False, trace_id=None) -> dict:
    if not node_id or not path:
        return {"error": "node_id and path required"}
    return await _call_cap("mesh.send", node_id=node_id, type="sd_write",
                           payload={"path": path, "content": content, "append": bool(append)})


# ─────────────────────────────────────────────────────────────────────────────
# Auto-OTA — hook called from mesh_capabilities._handle_hello
# ─────────────────────────────────────────────────────────────────────────────

def served_fw_version(flavor: str = "micropython") -> str:
    """Read the FW_VERSION constant from the served firmware file."""
    try:
        with open(_FW_MPY, encoding="utf-8") as f:
            head = f.read(4000)
        m = re.search(r'FW_VERSION\s*=\s*"([^"]+)"', head)
        return m.group(1) if m else ""
    except Exception:
        return ""


async def maybe_auto_ota(node_id: str, reported_fw: str, node_cfg: dict,
                         channels=None) -> None:
    """If the node opted into auto-OTA (config.ota.auto) and its firmware version
    trails the served one, queue an OTA file update. De-duped per version so we
    don't re-queue on every hello."""
    try:
        if not isinstance(node_cfg, dict) or not (node_cfg.get("ota") or {}).get("auto"):
            return
        if channels and "http" not in channels:
            return                                  # bridged/serial nodes can't self-fetch OTA
        cur = served_fw_version()
        if not cur or not reported_fw or reported_fw == cur:
            return
        if _OTA_SENT.get(node_id) == cur:
            return                                  # already queued for this version
        _OTA_SENT[node_id] = cur
        log.info("mesh auto-OTA: %s %s → %s", node_id, reported_fw, cur)
        await _call_cap("mesh.ota", node_id=node_id, mode="file", filename="main.py")
        await emit_event({"type": "mesh.ota.auto", "node_id": node_id,
                          "from": reported_fw, "to": cur})
    except Exception as e:
        log.warning("mesh auto-OTA %s: %s", node_id, e)


async def route_ui_event(node_id: str, action: str) -> None:
    """Handle a touch event from a node (increment 2 wires the firmware side).
    nav:<screen> switches screens; macro:<i> runs the mapped capability."""
    try:
        if not action:
            return
        if action.startswith("nav:"):
            dest = action.split(":", 1)[1]
            if dest in ("home", ""):
                await cap_mesh_ui_home(node_id)
            elif dest == "sysmon":
                await cap_mesh_ui_sysmon(node_id)
            elif dest == "status":
                await _call_cap("mesh.send", node_id=node_id, type="ui_clear", payload={})
            elif dest == "macros":
                # re-push the last macropad if we have one
                if node_id in _MACROS:
                    items = [{"text": v.get("cap", "?"), "action": k}
                             for k, v in _MACROS[node_id].items()]
                    items.append({"text": "Home", "action": "nav:home", "bg": C_GREY})
                    await _push(node_id, {"title": "Macro Pad", "bg": C_BLACK,
                                          "widgets": _grid_buttons(items)})
            return
        if action.startswith("macro:"):
            m = (_MACROS.get(node_id) or {}).get(action)
            if m and m.get("cap"):
                res = await _call_cap(m["cap"], **(m.get("args") or {}))
                await emit_event({"type": "mesh.ui.macro", "node_id": node_id,
                                  "cap": m["cap"], "ok": not (isinstance(res, dict) and res.get("error"))})
    except Exception as e:
        log.warning("mesh route_ui_event %s: %s", node_id, e)


# ── Touch: calibration helpers + inbound event route ────────────────────────

@capability(
    "mesh.ui.touch_raw", http_method="POST", http_path="/mesh/ui/touch_raw",
    http_tags=["mesh", "ui"], memory="on",
    description="Ask a display node for raw resistive-touch ADC values (touch the screen while it "
                "runs) to calibrate. Result (via mesh.jobs) has {raw:[x,y,z], cal, pins}. Use the "
                "x/y at the screen corners to set x0/x1/y0/y1 and the z band via mesh.ui.calibrate. "
                "Input: node_id (str!), samples (int=5). Output: {ok, job_id}.",
)
async def cap_mesh_ui_touch_raw(node_id: str = "", samples: int = 5, trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type="touch_raw",
                           payload={"samples": int(samples)})


@capability(
    "mesh.ui.calibrate", http_method="POST", http_path="/mesh/ui/calibrate",
    http_tags=["mesh", "ui"], memory="on",
    description="Set a display node's touch calibration. Input: node_id (str!), and any of "
                "x0,x1,y0,y1 (raw ADC at screen edges), zmin,zmax (pressure gate), swap,invx,invy "
                "(0/1 axis orientation). Persist by also calling mesh.config io.touch. Output: {ok, job_id}.",
)
async def cap_mesh_ui_calibrate(node_id: str = "", x0: int = None, x1: int = None,
                                y0: int = None, y1: int = None, zmin: int = None,
                                zmax: int = None, swap: int = None, invx: int = None,
                                invy: int = None, trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    payload = {k: v for k, v in dict(x0=x0, x1=x1, y0=y0, y1=y1, zmin=zmin, zmax=zmax,
                                     swap=swap, invx=invx, invy=invy).items() if v is not None}
    if not payload:
        return {"error": "nothing to set"}
    return await _call_cap("mesh.send", node_id=node_id, type="touch_cal", payload=payload)


try:
    from fastapi import Request                     # noqa
    from fastapi.responses import JSONResponse      # noqa

    @APP.post("/mesh/ui/event", include_in_schema=False)
    async def _mesh_ui_event(req: "Request"):
        try:
            body = await req.json()
        except Exception:
            body = {}
        await route_ui_event(body.get("node_id", ""), body.get("action", ""))
        return JSONResponse({"ok": True})
except Exception as _e:                              # FastAPI not present / APP is a stub
    log.debug("mesh_ui: /mesh/ui/event not registered: %s", _e)


log.info("mesh_ui_capabilities ready — served fw=%s", served_fw_version())
