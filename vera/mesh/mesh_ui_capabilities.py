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

_FW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware")
_FW_MPY = os.path.join(_FW_DIR, "micropython", "main.py")
_FW_INO = os.path.join(_FW_DIR, "arduino", "vera_mesh_node.ino")
_FW_BIN_DIR = os.path.join(_FW_DIR, "bin")

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


# The panel's 5x7 font is ASCII 0x20-0x7E and substitutes '?' for anything else,
# so "Pokemon" beats "Pok?mon". Transliterate rather than strip: SD card game
# titles and calendar entries are full of accents and typographic punctuation.
_ASCII_MAP = {
    "–": "-", "—": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "·": "-",
    " ": " ", "•": "*", "✓": "OK", "✗": "X",
    "×": "x", "→": "->", "°": "deg",
}


def to_panel_text(v) -> str:
    """Any string bound for the display, folded to what the font can draw."""
    import unicodedata
    t = str(v)
    for k, r in _ASCII_MAP.items():
        t = t.replace(k, r)
    # Decompose accents (e -> e + combining acute) and drop the combining marks.
    t = "".join(c for c in unicodedata.normalize("NFKD", t)
                if not unicodedata.combining(c))
    return "".join(c if 0x20 <= ord(c) <= 0x7E else "?" for c in t)


def _asciify_screen(screen: dict) -> dict:
    """Fold every drawable string in a screen. Done here, at the one place every
    screen passes through, so no individual app has to remember."""
    if not isinstance(screen, dict):
        return screen
    out = dict(screen)
    if "title" in out:
        out["title"] = to_panel_text(out["title"])
    widgets = []
    for w in out.get("widgets") or []:
        if isinstance(w, dict):
            w = dict(w)
            for key in ("text", "label"):
                if key in w and w[key] is not None:
                    w[key] = to_panel_text(w[key])
        widgets.append(w)
    if widgets:
        out["widgets"] = widgets
    return out


async def _push(node_id: str, screen: dict) -> dict:
    r = await _call_cap("mesh.send", node_id=node_id, type="ui_screen",
                        payload={"screen": _asciify_screen(screen)})
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
    return {"title": "Vera" + (" - " + node_name if node_name else ""),
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
    """Read the FW_VERSION constant from a served firmware SOURCE file."""
    path = _FW_INO if str(flavor).startswith("ard") else _FW_MPY
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(6000)
        # micropython: FW_VERSION = "x"   ·   arduino: #define FW_VERSION "x"
        m = re.search(r'FW_VERSION\s*[= ]\s*"([^"]+)"', head)
        return m.group(1) if m else ""
    except Exception:
        return ""


def node_runtime(reported_runtime: str, reported_fw: str) -> str:
    """Which firmware family a node runs. Nodes flashed before `runtime` was
    reported are inferred from the FW_VERSION suffix; anything still unknown
    returns "" and is left alone rather than guessed at."""
    r = (reported_runtime or "").strip().lower()
    if r in ("arduino", "micropython"):
        return r
    fw = (reported_fw or "").lower()
    if fw.endswith("-ino") or fw.endswith("-arduino"):
        return "arduino"
    if fw.endswith("-mpy") or fw.endswith("-micropython"):
        return "micropython"
    return ""


def normalise_chip(chip: str) -> str:
    """'ESP32-S3' / 'ESP32S3 module…' → 'esp32s3', matching the sidecar's value."""
    c = re.sub(r"[^a-z0-9]", "", str(chip or "").lower())
    for known in ("esp32s3", "esp32s2", "esp32c6", "esp32c3", "esp32h2", "esp32"):
        if c.startswith(known):
            return known
    return ""


def newest_arduino_bin(chip: str = "") -> Optional[dict]:
    """The most recently built Arduino .bin (+ its sidecar metadata) for a chip.

    Only images WE built are eligible (they have a sidecar) — an uploaded .bin of
    unknown provenance is never auto-pushed. With no chip to match on we only
    answer when there is exactly one candidate: guessing here would flash an
    S3 node with, say, a C3 image."""
    want = normalise_chip(chip)
    cands = []
    try:
        for name in sorted(os.listdir(_FW_BIN_DIR)):
            if not name.endswith(".bin"):
                continue
            meta_path = os.path.join(_FW_BIN_DIR, name + ".json")
            if not os.path.exists(meta_path):
                continue                            # not one of ours — never auto-push it
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            if meta.get("runtime") != "arduino":
                continue
            if want and normalise_chip(meta.get("chip", "")) != want:
                continue
            cands.append((os.path.getmtime(os.path.join(_FW_BIN_DIR, name)),
                          dict(meta, name=name)))
    except Exception as e:
        log.debug("newest_arduino_bin: %s", e)
        return None
    if not cands:
        return None
    if not want and len({c[1].get("chip") for c in cands}) > 1:
        log.debug("auto-OTA: node chip unknown and %d chips available — refusing to guess",
                  len(cands))
        return None
    return max(cands, key=lambda c: c[0])[1]


def _auto_ota_enabled(node_cfg: dict) -> bool:
    """Auto-OTA is ON by default — a fleet that silently runs stale firmware is
    the worse failure. Opt OUT per node with config.ota.auto = false, or fleet-wide
    with the mesh setting ota_auto = 0."""
    if isinstance(node_cfg, dict):
        v = (node_cfg.get("ota") or {}).get("auto")
        if v is not None:
            return bool(v) and str(v).lower() not in ("0", "false", "off", "no")
    try:
        mesh = (sys.modules.get("mesh_capabilities")
                or sys.modules.get("Vera.vera.mesh.mesh_capabilities"))
        if mesh and hasattr(mesh, "_settings_get_sync"):
            v = (mesh._settings_get_sync() or {}).get("ota_auto")
            if v is not None and str(v).strip() != "":
                return str(v).strip().lower() not in ("0", "false", "off", "no")
    except Exception as e:
        log.debug("auto-OTA setting lookup: %s", e)
    return True


async def maybe_auto_ota(node_id: str, reported_fw: str, node_cfg: dict,
                         channels=None, runtime: str = "", chip: str = "") -> None:
    """Queue an OTA when a node's firmware trails what we'd serve it. On by
    default (see _auto_ota_enabled); de-duped per target version so a node that
    keeps saying hello isn't re-flashed in a loop.

    The artifact depends on the runtime: MicroPython takes a main.py push, Arduino
    needs a compiled .bin (mode=file is rejected by that firmware). Comparing an
    Arduino node against the .bin's recorded version — not the .ino source — is
    what stops an endless re-push when the source has moved on from the image."""
    try:
        if not _auto_ota_enabled(node_cfg):
            return
        if channels and "http" not in channels:
            return                                  # bridged/serial nodes can't self-fetch OTA
        if not reported_fw:
            return
        rt = node_runtime(runtime, reported_fw)
        if not rt:
            log.debug("auto-OTA: %s runtime unknown (fw=%r) — skipping", node_id, reported_fw)
            return

        if rt == "micropython":
            target = served_fw_version("micropython")
            if not target or reported_fw == target or _OTA_SENT.get(node_id) == target:
                return
            _OTA_SENT[node_id] = target
            log.info("mesh auto-OTA (micropython): %s %s → %s", node_id, reported_fw, target)
            await _call_cap("mesh.ota", node_id=node_id, mode="file", filename="main.py")
        else:
            art = newest_arduino_bin(chip)
            if not art:
                log.debug("auto-OTA: %s is Arduino but no built .bin to offer "
                          "(build one from the Mesh panel)", node_id)
                return
            target = art.get("fw_version") or ""
            if not target or reported_fw == target or _OTA_SENT.get(node_id) == target:
                return
            _OTA_SENT[node_id] = target
            log.info("mesh auto-OTA (arduino): %s %s → %s via %s",
                     node_id, reported_fw, target, art["name"])
            await _call_cap("mesh.ota", node_id=node_id, mode="bin", artifact=art["name"])

        await emit_event({"type": "mesh.ota.auto", "node_id": node_id, "runtime": rt,
                          "from": reported_fw, "to": target})
    except Exception as e:
        log.warning("mesh auto-OTA %s: %s", node_id, e)


async def route_ui_event(node_id: str, action: str) -> None:
    """Handle a touch event from a node (increment 2 wires the firmware side).
    nav:<screen> switches screens; app:<id> launches an app from the registry;
    macro:<i> runs the mapped capability."""
    try:
        if not action:
            return
        if await _run_spec_action(node_id, action):
            return                                # screen:/panel:/cap: from a built UI
        if action.startswith("app:"):
            await launch_app(node_id, action.split(":", 1)[1])
            return
        if action.startswith("nav:"):
            dest = action.split(":", 1)[1]
            if dest in ("home", ""):
                await cap_mesh_ui_home(node_id)
            elif dest == "sysmon":
                await cap_mesh_ui_sysmon(node_id)
            elif dest == "status":
                await _call_cap("mesh.send", node_id=node_id, type="ui_clear", payload={})
            elif dest == "pad":
                await _repush_pad(node_id)
            elif dest == "macros":
                # re-push the last macropad if we have one
                if node_id in _MACROS:
                    items = [{"text": v.get("cap", "?"), "action": k}
                             for k, v in _MACROS[node_id].items()]
                    items.append({"text": "Home", "action": "nav:home", "bg": C_GREY})
                    await _push(node_id, {"title": "Macro Pad", "bg": C_BLACK,
                                          "widgets": _grid_buttons(items)})
            return
        if action.startswith("followmode:"):
            await cap_mesh_app_follow_mode(node_id=node_id,
                                           mode=action.split(":", 1)[1])
            await launch_app(node_id, "follow")      # reflect the new choice
            return
        if action.startswith("applist:"):
            await launch_app(node_id, "launcher",
                             {"page": int(action.split(":", 1)[1])})
            return
        if action.startswith("page:"):
            await _repush_pad(node_id, int(action.split(":", 1)[1]))
            return
        if action.startswith(("macro:", "run:")):
            key = action.replace("run:", "macro:", 1)
            m = (_MACROS.get(node_id) or {}).get(key)
            if not (m and m.get("cap")):
                return
            # Destructive buttons ask first — a resistive panel picks up knocks
            # and sleeves, and there is no undo on the other side of a tap.
            if m.get("confirm") and action.startswith("macro:"):
                await _push(node_id, {
                    "title": "Confirm", "bg": C_BLACK, "widgets": [
                        {"t": "label", "x": 8, "y": 50, "size": 2, "color": C_YELL,
                         "text": str(m.get("label") or m["cap"])[:40]},
                        {"t": "label", "x": 8, "y": 76, "size": 1, "color": C_GREY,
                         "text": m["cap"]},
                        {"t": "button", "x": 8, "y": 150, "w": 150, "h": 56,
                         "text": "Run it", "action": key.replace("macro:", "run:", 1),
                         "bg": C_NAVY},
                        {"t": "button", "x": 168, "y": 150, "w": 150, "h": 56,
                         "text": "Cancel", "action": "nav:pad", "bg": C_GREY}]})
                return
            res = await _call_cap(m["cap"], **(m.get("args") or {}))
            await emit_event({"type": "mesh.ui.macro", "node_id": node_id,
                              "cap": m["cap"], "ok": not (isinstance(res, dict) and res.get("error"))})
            # Say what happened. A tap that changes nothing on screen is
            # indistinguishable from a tap that missed.
            await _show_result(node_id, m.get("label", ""), m["cap"], res)
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


# ════════════════════════════════════════════════════════════════════════════
# Images on the panel — sprites, companions, art, page screenshots
# ════════════════════════════════════════════════════════════════════════════
# A 480x320 frame is 300KB of RGB565: far too much to push through the job queue
# and far too much to hold in RAM on the node. So Vera renders whatever you give
# it into a flat "V565" file (8-byte header + raw rows) and the node STREAMS it
# straight to the panel, row by row, decoding nothing.

_IMG_DIR = os.path.join(_FW_DIR, "img")
_IMG_KEEP = 60                                  # cached frames before pruning


def pil_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def encode_v565(img, width: int, height: int, fit: str = "contain",
                bg=(0, 0, 0)) -> bytes:
    """A PIL image → the node's wire format. `fit`: contain (letterbox, keeps
    aspect), cover (fill, crops), stretch (ignores aspect)."""
    from PIL import Image
    img = img.convert("RGB")
    if fit == "stretch":
        img = img.resize((width, height), Image.LANCZOS)
    elif fit == "cover":
        sc = max(width / img.width, height / img.height)
        img = img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))),
                         Image.LANCZOS)
        l, t = (img.width - width) // 2, (img.height - height) // 2
        img = img.crop((l, t, l + width, t + height))
    else:                                        # contain
        sc = min(width / img.width, height / img.height)
        sized = img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))),
                           Image.LANCZOS)
        canvas = Image.new("RGB", (width, height), bg)
        canvas.paste(sized, ((width - sized.width) // 2, (height - sized.height) // 2))
        img = canvas

    out = bytearray(b"V565")
    out += bytes([width >> 8 & 0xFF, width & 0xFF, height >> 8 & 0xFF, height & 0xFF])
    for r, g, b in img.getdata():
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out += bytes([v >> 8, v & 0xFF])         # big-endian, matching the driver
    return bytes(out)


def _prune_images() -> None:
    try:
        files = sorted((os.path.join(_IMG_DIR, f) for f in os.listdir(_IMG_DIR)
                        if f.endswith(".v565")), key=os.path.getmtime)
        for p in files[:-_IMG_KEEP]:
            os.remove(p)
    except Exception as e:
        log.debug("image prune: %s", e)


def store_v565(data: bytes, name: str = "") -> str:
    """Write a rendered frame and return the name the node should fetch."""
    import hashlib
    os.makedirs(_IMG_DIR, exist_ok=True)
    n = re.sub(r"[^A-Za-z0-9_.-]", "-", name) if name else ""
    if not n.endswith(".v565"):
        n = (n or hashlib.sha1(data).hexdigest()[:16]) + ".v565"
    with open(os.path.join(_IMG_DIR, n), "wb") as f:
        f.write(data)
    _prune_images()
    return n


async def _load_source(url: str = "", path: str = "", data_b64: str = ""):
    """Resolve an image source → a PIL image. Accepts a URL, a server-side path,
    or inline base64 (what sprite/render capabilities hand back)."""
    import base64
    import io
    from PIL import Image
    if data_b64:
        return Image.open(io.BytesIO(base64.b64decode(data_b64)))
    if path:
        return Image.open(path)
    if url:
        if url.startswith(("http://", "https://")):
            import httpx
            async with httpx.AsyncClient(timeout=30, verify=False) as c:
                r = await c.get(url)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content))
        return Image.open(url)                   # a bare path is fine too
    raise ValueError("one of url / path / data_b64 is required")


@capability(
    "mesh.ui.image", http_method="POST", http_path="/mesh/ui/image",
    http_tags=["mesh", "ui"], memory="on",
    description="Show a picture on a display node — a sprite/companion frame, generated art, or a "
                "page screenshot. Vera renders the source to the panel's native RGB565 and the node "
                "streams it row by row (a full frame is 300KB, far too big to push through the job "
                "queue). Input: node_id (str!), url (str — http(s) or a server path) OR path (str) OR "
                "data_b64 (str — raw image bytes, what render/sprite caps return), w (int=480), "
                "h (int=320), x (int=0), y (int=0), fit (str=contain|cover|stretch), clear (bool=True), "
                "name (str — reuse a stable name to overwrite a frame instead of piling up). "
                "Output: {ok, name, bytes, w, h, job_id}.",
    schema={"properties": {"fit": {"enum": ["contain", "cover", "stretch"]}}},
)
async def cap_mesh_ui_image(node_id: str = "", url: str = "", path: str = "", data_b64: str = "",
                            w: int = 480, h: int = 320, x: int = 0, y: int = 0,
                            fit: str = "contain", clear: bool = True, name: str = "",
                            trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    if not pil_available():
        return {"error": "Pillow is not installed on the server — needed to render for the panel"}
    try:
        img = await _load_source(url, path, data_b64)
    except Exception as e:
        return {"error": f"could not read the image source: {e}"}
    try:
        blob = await asyncio.get_running_loop().run_in_executor(
            None, encode_v565, img, int(w), int(h), fit)
        fname = store_v565(blob, name)
    except Exception as e:
        return {"error": f"render failed: {e}"}
    job = await _call_cap("mesh.send", node_id=node_id, type="ui_image",
                          payload={"url": f"/mesh/ui/img/{fname}", "x": int(x), "y": int(y),
                                   "clear": bool(clear)})
    await emit_event({"type": "mesh.ui.image", "node_id": node_id, "name": fname,
                      "bytes": len(blob)})
    return {"ok": bool(isinstance(job, dict) and not job.get("error")), "name": fname,
            "bytes": len(blob), "w": int(w), "h": int(h),
            "url": f"/mesh/ui/img/{fname}", "job_id": (job or {}).get("job_id")}


try:
    from fastapi.responses import FileResponse, JSONResponse

    @APP.get("/mesh/ui/img/{name}", include_in_schema=False)
    async def _mesh_ui_img(name: str):
        """Serve a rendered frame to the node. Basenames only — this is fetched by
        an unauthenticated device on the LAN, so no path traversal."""
        safe = os.path.basename(name or "").replace("\\", "")
        p = os.path.join(_IMG_DIR, safe)
        if not safe.endswith(".v565") or not os.path.exists(p):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(p, media_type="application/octet-stream")
except Exception as _e:                          # pragma: no cover - import guard
    log.debug("ui image route not registered: %s", _e)


# ── Animation: one file, played locally ─────────────────────────────────────
# Re-fetching per frame stutters and floods the link, so the whole sequence goes
# in one "V56A" file the node caches in PSRAM and plays with no network at all.

_ANIM_MAX_FRAMES = 240


def encode_v56a(frames, width: int, height: int, fps: int = 10,
                fit: str = "contain", bg=(0, 0, 0)) -> bytes:
    """PIL frames → the node's animation format: 12-byte header then raw frames."""
    frames = list(frames)[:_ANIM_MAX_FRAMES]
    if not frames:
        raise ValueError("no frames")
    fps = max(1, min(60, int(fps)))
    out = bytearray(b"V56A")
    out += bytes([width >> 8 & 0xFF, width & 0xFF, height >> 8 & 0xFF, height & 0xFF,
                  len(frames) >> 8 & 0xFF, len(frames) & 0xFF,
                  fps >> 8 & 0xFF, fps & 0xFF])
    for fr in frames:
        # Reuse the single-frame encoder and drop its 8-byte header.
        out += encode_v565(fr, width, height, fit, bg)[8:]
    return bytes(out)


def _frames_from_image(img, cols: int = 0, rows: int = 1):
    """Split a source into frames: an animated GIF/WebP by its own frames, or a
    sprite SHEET by a cols x rows grid."""
    from PIL import ImageSequence
    if cols and cols > 0:
        fw, fh = img.width // cols, img.height // max(1, rows)
        return [img.crop((c * fw, r * fh, (c + 1) * fw, (r + 1) * fh))
                for r in range(max(1, rows)) for c in range(cols)]
    n = getattr(img, "n_frames", 1)
    if n > 1:
        return [f.copy() for f in ImageSequence.Iterator(img)]
    return [img]


@capability(
    "mesh.ui.animate", http_method="POST", http_path="/mesh/ui/animate",
    http_tags=["mesh", "ui"], memory="on",
    description="Play an animation on a display node — a companion, a Sprite Studio sheet, an "
                "animated GIF, or a live emoji. The whole sequence is sent as ONE file that the node "
                "caches in PSRAM and plays locally, so nothing streams per frame. Input: node_id "
                "(str!), url/path/data_b64 (an animated GIF/WebP, or a sprite sheet — give cols/rows "
                "to slice it) OR frames (list of urls/paths), cols (int — sheet columns), rows "
                "(int=1), w (int=128), h (int=128), x (int=0), y (int=0), fps (int=10), loop "
                "(bool=True), fit (str=contain|cover|stretch), clear (bool=True), name (str). "
                "Output: {ok, name, frames, fps, bytes, job_id}.",
    schema={"properties": {"fit": {"enum": ["contain", "cover", "stretch"]}}},
)
async def cap_mesh_ui_animate(node_id: str = "", url: str = "", path: str = "", data_b64: str = "",
                              frames=None, cols: int = 0, rows: int = 1,
                              w: int = 128, h: int = 128, x: int = 0, y: int = 0,
                              fps: int = 10, loop: bool = True, fit: str = "contain",
                              clear: bool = True, name: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    if not pil_available():
        return {"error": "Pillow is not installed on the server"}
    frames = json.loads(frames) if isinstance(frames, str) else frames
    try:
        if frames:
            imgs = [await _load_source(url=f) for f in frames]
        else:
            imgs = _frames_from_image(await _load_source(url, path, data_b64),
                                      int(cols or 0), int(rows or 1))
    except Exception as e:
        return {"error": f"could not read the animation source: {e}"}
    if not imgs:
        return {"error": "no frames found — for a sprite sheet pass cols (and rows)"}

    try:
        blob = await asyncio.get_running_loop().run_in_executor(
            None, encode_v56a, imgs, int(w), int(h), int(fps), fit)
    except Exception as e:
        return {"error": f"render failed: {e}"}
    # A node holds this in PSRAM; say so plainly rather than letting it fail there.
    if len(blob) > 3 * 1024 * 1024:
        return {"error": f"sequence is {len(blob)//1024}KB — too big for a node; "
                         f"use fewer frames or a smaller w/h",
                "frames": len(imgs), "bytes": len(blob)}
    fname = store_v565(blob, (name or "") and (name + ".v565"))
    job = await _call_cap("mesh.send", node_id=node_id, type="ui_anim",
                          payload={"url": f"/mesh/ui/img/{fname}", "x": int(x), "y": int(y),
                                   "loop": bool(loop), "clear": bool(clear)})
    await emit_event({"type": "mesh.ui.animate", "node_id": node_id, "name": fname,
                      "frames": len(imgs), "fps": int(fps), "bytes": len(blob)})
    return {"ok": bool(isinstance(job, dict) and not job.get("error")), "name": fname,
            "frames": len(imgs), "fps": int(fps), "bytes": len(blob),
            "url": f"/mesh/ui/img/{fname}", "job_id": (job or {}).get("job_id")}


@capability(
    "mesh.ui.animate.stop", http_method="POST", http_path="/mesh/ui/animate/stop",
    http_tags=["mesh", "ui"], memory="on",
    description="Stop an animation on a node, free its PSRAM and return to the status screen. "
                "Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_ui_animate_stop(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    job = await _call_cap("mesh.send", node_id=node_id, type="ui_anim_stop", payload={})
    return {"ok": bool(isinstance(job, dict) and not job.get("error")),
            "job_id": (job or {}).get("job_id")}


# ════════════════════════════════════════════════════════════════════════════
# App library — named apps a node can run, and pads that follow the Vera UI
# ════════════════════════════════════════════════════════════════════════════
# An "app" is just a screen-builder plus its tap→capability map, so adding one
# never means reflashing a node. `follow` turns the panel you are looking at in
# Vera into the pad on your desk: open Markets, the node shows Markets controls.

# Per-panel pads. Keep each to ~6 buttons — a 480x320 panel fits 2x3 comfortably
# and a wall of tiny targets is worse than a short list.
PANEL_PADS: Dict[str, dict] = {
    "mesh": {"label": "Mesh", "buttons": [
        {"label": "Test pattern", "cap": "mesh.display.test", "self": True},
        {"label": "Probe display", "cap": "mesh.display.probe", "self": True},
        {"label": "Sysinfo", "cap": "mesh.sysinfo", "self": True},
        {"label": "Wi-Fi scan", "cap": "mesh.wifi.scan", "self": True},
    ]},
    "markets": {"label": "Markets", "buttons": [
        {"label": "Overview", "cap": "markets.overview"},
        {"label": "Watchlist", "cap": "markets.watchlist.list"},
        {"label": "Alerts", "cap": "markets.alerts.list"},
    ]},
    "dream": {"label": "Dream", "buttons": [
        {"label": "Cycle now", "cap": "dream.cycle.run", "confirm": True},
        {"label": "Progress", "cap": "dream.cycle.progress"},
    ]},
    "workers": {"label": "Workers", "buttons": [
        {"label": "Docker ps", "cap": "docker.ps", "args": {"host_id": "local"}},
        {"label": "Build status", "cap": "build.status"},
        {"label": "Workers", "cap": "obs.workers"},
    ]},
    "business": {"label": "Business", "buttons": [
        {"label": "Orders", "cap": "business.order.list"},
        {"label": "Inventory", "cap": "business.inventory.list"},
        {"label": "Brief", "cap": "business.brief"},
    ]},
    "chat": {"label": "Chat", "buttons": [
        {"label": "Notify me", "cap": "chat.deliver",
         "args": {"text": "ping from the mesh display"}},
    ]},
    "netmap": {"label": "Network", "buttons": [
        {"label": "LAN scan", "cap": "netscan.lan.scan", "confirm": True},
        {"label": "Topology", "cap": "netgraph.topology"},
    ]},
    "evolve": {"label": "Loop Lab", "buttons": [
        {"label": "Sandbox", "cap": "evolve.sandbox.status"},
        {"label": "Runs", "cap": "evolve.runs"},
    ]},
    "spritegen": {"label": "Sprites", "buttons": [
        {"label": "Sprites", "cap": "spritegen.list"},
    ]},
}

# Apps are (label, icon, builder). The builder returns (screen, macro-map).
def _app_status(node_id: str, ctx: dict):
    return {"__status__": True}, {}


def _app_launcher(node_id: str, ctx: dict):
    items = [{"text": a["label"], "action": "app:" + aid, "bg": C_NAVY}
             for aid, a in sorted(APPS.items()) if aid != "launcher"]
    return ({"title": "Apps", "bg": C_BLACK, "widgets": _grid_buttons(items, cols=2)}, {})


def _app_pad(panel: str):
    def build(node_id: str, ctx: dict):
        pad = pad_for_panel(panel)
        return build_pad_screen(node_id, pad.get("label") or panel,
                                pad.get("buttons") or [], 0)
    return build


APPS: Dict[str, dict] = {
    "launcher": {"label": "Apps", "build": _app_launcher},
    "status":   {"label": "Status", "build": _app_status},
}
def _register_pads() -> None:
    """(Re)build the app registry from built-in pads plus anything saved. Called
    at import and after every save/delete so a new pad shows up immediately."""
    for aid in [a for a in APPS if a.startswith("pad:")]:
        APPS.pop(aid, None)
    for pid in sorted(set(PANEL_PADS) | set(_panel_registry())):
        pad = PANEL_PADS.get(pid) or {}
        label = pad.get("label") or (_panel_registry().get(pid) or {}).get("label") or pid
        APPS["pad:" + pid] = {"label": str(label) + " pad",
                              "build": _app_pad(pid), "panel": pid}
    for aid in INFO_APPS:
        APPS[aid] = {"label": INFO_APPS[aid]["label"], "build": _app_dashboard(aid)}
    APPS["home"] = {"label": "Home", "build": _app_home}
    APPS["info:candles"] = {"label": "Candles", "build": _app_candles}
    APPS["follow"] = {"label": "Follow mode", "build": _app_modeswitch}
    for aid in LIST_APPS:
        APPS[aid] = {"label": LIST_APPS[aid]["label"], "build": _app_list(aid)}
    APPS["pad:intake"] = {"label": "Stock intake", "build": _app_pad("intake"),
                          "panel": "intake"}
    APPS["pad:scans"] = {"label": "Scans", "build": _app_pad("scans"), "panel": "scans"}
    for pid, pad in _load_custom_pads().items():
        APPS["pad:" + pid] = {"label": (pad.get("label") or pid) + " pad",
                              "build": _app_pad(pid), "panel": pid, "custom": True}



_NODE_APP: Dict[str, str] = {}                     # node_id → app currently shown


async def launch_app(node_id: str, app_id: str, ctx: dict = None) -> dict:
    app = APPS.get(app_id)
    if not app:
        return {"error": f"unknown app: {app_id}", "apps": sorted(APPS)}
    built = app["build"](node_id, ctx or {})
    if asyncio.iscoroutine(built):
        built = await built                       # data-driven dashboards are async
    screen, mapping = built
    if screen.get("__status__"):                   # the built-in "just show status"
        _NODE_APP[node_id] = app_id
        r = await _call_cap("mesh.send", node_id=node_id, type="ui_clear", payload={})
        return {"ok": True, "app": app_id, "job_id": (r or {}).get("job_id")}
    _MACROS[node_id] = mapping
    _NODE_APP[node_id] = app_id
    r = await _push(node_id, screen)
    r["app"] = app_id
    r["buttons"] = len(mapping)
    return r


@capability(
    "mesh.app.list", http_method="GET", http_path="/mesh/apps",
    http_tags=["mesh", "ui"], memory="off", silent=True,
    description="List the apps a display node can run (launcher, status, and a pad per Vera panel) "
                "plus which app each node is currently showing. Output: {apps:[{id,label,panel}], "
                "panels:[...], running:{node_id:app_id}}.",
)
async def cap_mesh_app_list(trace_id=None) -> dict:
    return {"apps": [{"id": a, "label": v["label"], "panel": v.get("panel", "")}
                     for a, v in sorted(APPS.items())],
            "panels": sorted(PANEL_PADS), "running": dict(_NODE_APP)}


@capability(
    "mesh.app.launch", http_method="POST", http_path="/mesh/app/launch",
    http_tags=["mesh", "ui"], memory="on",
    description="Run an app on a display node — its screen is pushed and its buttons wired to "
                "capabilities. No reflash: apps are server-side screen builders. Input: node_id "
                "(str!), app (str! — an id from mesh.app.list, e.g. 'launcher' or 'pad:markets'). "
                "Output: {ok, app, buttons, job_id}.",
)
async def cap_mesh_app_launch(node_id: str = "", app: str = "", trace_id=None) -> dict:
    if not node_id or not app:
        return {"error": "node_id and app required"}
    r = await launch_app(node_id, app)
    if not (isinstance(r, dict) and r.get("error")):
        await emit_event({"type": "mesh.app", "stage": "launch", "node_id": node_id, "app": app})
    return r


@capability(
    "mesh.app.stop", http_method="POST", http_path="/mesh/app/stop",
    http_tags=["mesh", "ui"], memory="on",
    description="Close whatever app a node is showing and return it to the status dashboard. "
                "Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_app_stop(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    _NODE_APP.pop(node_id, None)
    _MACROS.pop(node_id, None)
    r = await _call_cap("mesh.send", node_id=node_id, type="ui_clear", payload={})
    return {"ok": bool(isinstance(r, dict) and not r.get("error")),
            "job_id": (r or {}).get("job_id")}


@capability(
    "mesh.app.follow", http_method="POST", http_path="/mesh/app/follow",
    http_tags=["mesh", "ui"], memory="off", silent=True,
    description="Make a node's screen follow the Vera panel you are looking at — open Markets and "
                "the pad becomes Markets controls. Called by the harness on tab change; idempotent "
                "and a no-op when the right pad is already showing, so it is safe to call often. "
                "Panels with no pad leave the node alone rather than blanking it. Input: node_id "
                "(str!), panel (str! — the panel id), force (bool=False). "
                "Output: {ok, app, changed}.",
)
async def cap_mesh_app_follow(node_id: str = "", panel: str = "", force: bool = False,
                              trace_id=None) -> dict:                   
    if not node_id or not panel:
        return {"error": "node_id and panel required"}
    mode = follow_mode(node_id)
    if mode == "off":
        return {"ok": True, "changed": False, "mode": mode, "reason": "following is off"}

    if mode == "dash":
        app_id = PANEL_DASHBOARDS.get(panel, "")
        if not app_id or app_id not in APPS:
            # No readout for this panel: leave the screen alone rather than
            # blanking it just because you opened an unrelated tab.
            return {"ok": True, "changed": False, "mode": mode,
                    "reason": "no dashboard for panel %r" % panel}
    else:
        app_id = "pad:" + panel
        if app_id not in APPS and pad_for_panel(panel).get("buttons"):
            APPS[app_id] = {"label": panel + " pad", "build": _app_pad(panel),
                            "panel": panel}
        if app_id not in APPS:
            return {"ok": True, "changed": False, "mode": mode,
                    "reason": "no pad for panel %r" % panel}

    if _NODE_APP.get(node_id) == app_id and not force:
        return {"ok": True, "app": app_id, "changed": False, "mode": mode}
    _FOLLOW_LAST[node_id] = panel
    _FOLLOW_SEEN[node_id] = {"panel": panel, "at": now_iso(), "app": app_id}
    r = await launch_app(node_id, app_id)
    return {"ok": not r.get("error"), "app": app_id, "changed": True,
            "mode": mode, "error": r.get("error", "")}


# ════════════════════════════════════════════════════════════════════════════
# Macro pads, expanded: paging, tap feedback, confirmation, saved pads
# ════════════════════════════════════════════════════════════════════════════
# A tap used to fire a capability and change nothing on screen, so you could not
# tell a success from a silent failure. Every tap now answers on the panel.

_PADS_CUSTOM = os.path.join(_FW_DIR, "pads_custom.json")
_PAGE_SIZE = 6                     # 2 x 3 reads well at arm's length on 480x320
_PAD_CTX: Dict[str, dict] = {}     # node_id → the pad it is showing


def _load_custom_pads() -> Dict[str, dict]:
    try:
        with open(_PADS_CUSTOM, encoding="utf-8") as f:
            raw = json.load(f)
        return {p["id"]: p for p in raw if isinstance(p, dict) and p.get("id")}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("custom pads unreadable: %s", e)
        return {}


def _save_custom_pads(pads: Dict[str, dict]) -> None:
    tmp = _PADS_CUSTOM + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(pads.values()), f, indent=2)
    os.replace(tmp, _PADS_CUSTOM)


def summarise_result(cap: str, res) -> List[str]:
    """A capability result → a few lines that fit a 480x320 panel. Errors win:
    a truncated success is fine, a hidden failure is not."""
    if isinstance(res, dict) and res.get("error"):
        return ["FAILED", str(res["error"])[:120]]
    if isinstance(res, dict):
        skip = {"ok", "trace_id", "job_id"}
        parts = []
        for k, v in res.items():
            if k in skip or v in (None, "", [], {}):
                continue
            if isinstance(v, (list, dict)):
                parts.append(f"{k}: {len(v)}")
            else:
                parts.append(f"{k}: {str(v)[:40]}")
            if len(parts) >= 5:
                break
        return parts or ["ok"]
    return [str(res)[:120]] if res is not None else ["ok"]


def build_pad_screen(node_id: str, title: str, buttons: List[dict], page: int = 0):
    """Lay a pad out with paging, and return (screen, macro-map). Buttons carry
    their absolute index so the map stays stable across pages."""
    pages = max(1, (len(buttons) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _PAGE_SIZE
    items, mapping = [], {}
    for i, b in enumerate(buttons[start:start + _PAGE_SIZE], start=start):
        action = "macro:%d" % i
        args = dict(b.get("args") or {})
        if b.get("self"):
            args["node_id"] = node_id
        items.append({"text": b.get("label") or b.get("cap") or "?", "action": action,
                      "bg": int(b.get("bg", C_NAVY))})
        mapping[action] = {"cap": b.get("cap"), "args": args,
                           "confirm": bool(b.get("confirm")),
                           "label": b.get("label") or b.get("cap")}
    if pages > 1:
        items.append({"text": "< %d/%d" % (page + 1, pages), "action": "page:%d" % (page - 1),
                      "bg": C_GREY})
        items.append({"text": "next >", "action": "page:%d" % (page + 1), "bg": C_GREY})
    items.append({"text": "Apps", "action": "app:launcher", "bg": C_GREY})
    _PAD_CTX[node_id] = {"title": title, "buttons": buttons, "page": page}
    return ({"title": title, "bg": C_BLACK, "widgets": _grid_buttons(items, cols=2)},
            mapping)


async def _show_result(node_id: str, label: str, cap: str, res) -> None:
    """Answer a tap on the panel itself, with a way back to the pad."""
    lines = summarise_result(cap, res)
    failed = isinstance(res, dict) and bool(res.get("error"))
    widgets = [{"t": "label", "x": 8, "y": 46, "text": cap, "size": 1, "color": C_GREY}]
    y = 62
    for ln in lines[:5]:
        widgets.append({"t": "label", "x": 8, "y": y, "text": str(ln)[:44], "size": 2,
                        "color": C_RED if failed else C_GREEN})
        y += 22
    widgets.append({"t": "button", "x": 8, "y": max(y + 8, 250), "w": 150, "h": 46,
                    "text": "Back", "action": "nav:pad", "bg": C_NAVY})
    await _push(node_id, {"title": ("FAIL " if failed else "OK ") + (label or cap)[:18],
                          "bg": C_BLACK, "widgets": widgets})


async def _repush_pad(node_id: str, page: int = None) -> None:
    ctx = _PAD_CTX.get(node_id)
    if not ctx:
        await cap_mesh_ui_home(node_id)
        return
    screen, mapping = build_pad_screen(node_id, ctx["title"], ctx["buttons"],
                                       ctx["page"] if page is None else page)
    _MACROS[node_id] = mapping
    await _push(node_id, screen)


@capability(
    "mesh.app.pad.save", http_method="POST", http_path="/mesh/app/pad/save",
    http_tags=["mesh", "ui"], memory="on",
    description="Save (or overwrite) a custom macro pad so it appears in the launcher and can be "
                "launched as 'pad:<id>'. Buttons page automatically at 6 per screen. Input: id "
                "(str!), label (str), buttons (JSON list! — [{label, cap, args?, self?, confirm?}]; "
                "`self` injects the tapping node's node_id, `confirm` asks before running). "
                "Output: {ok, id, buttons, pads}.",
)
async def cap_mesh_app_pad_save(id: str = "", label: str = "", buttons=None,
                                trace_id=None) -> dict:
    if not id:
        return {"error": "id required"}
    if id in PANEL_PADS:
        return {"error": f"'{id}' is a built-in panel pad — choose another id"}
    buttons = json.loads(buttons) if isinstance(buttons, str) else buttons
    if not isinstance(buttons, list) or not buttons:
        return {"error": "buttons must be a non-empty list"}
    clean = []
    for b in buttons:
        if isinstance(b, dict) and b.get("cap"):
            clean.append({"label": b.get("label") or b["cap"], "cap": b["cap"],
                          "args": b.get("args") or {}, "self": bool(b.get("self")),
                          "confirm": bool(b.get("confirm"))})
    if not clean:
        return {"error": "every button needs a cap"}
    pads = _load_custom_pads()
    pads[id] = {"id": id, "label": label or id, "buttons": clean}
    try:
        _save_custom_pads(pads)
    except Exception as e:
        return {"error": f"save failed: {e}"}
    _register_pads()
    await emit_event({"type": "mesh.app.pad", "stage": "save", "id": id})
    return {"ok": True, "id": id, "buttons": len(clean), "pads": sorted(pads)}


@capability(
    "mesh.app.pad.delete", http_method="POST", http_path="/mesh/app/pad/delete",
    http_tags=["mesh", "ui"], memory="on",
    description="Delete a saved macro pad (built-in panel pads can't be deleted). "
                "Input: id (str!). Output: {ok, pads}.",
)
async def cap_mesh_app_pad_delete(id: str = "", trace_id=None) -> dict:
    pads = _load_custom_pads()
    if id not in pads:
        return {"error": "unknown custom pad", "pads": sorted(pads)}
    pads.pop(id)
    try:
        _save_custom_pads(pads)
    except Exception as e:
        return {"error": f"delete failed: {e}"}
    _register_pads()
    return {"ok": True, "pads": sorted(pads)}


# ════════════════════════════════════════════════════════════════════════════
# SD toolkit — walk a card, identify what it is, archive it idempotently
# ════════════════════════════════════════════════════════════════════════════
# The shield's SD slot turns a node into a card reader you can drive from Vera:
# see what is on a card, work out which console it came from, and pull it into a
# file store without re-copying what is already there.

_SD_STORE = os.path.join(_FW_DIR, "sdstore")

# Signature paths. Console SD layouts are distinctive enough that a handful of
# directories identify them without reading a single file.
_CONSOLE_SIGNS = [
    ("switch", "Nintendo Switch",
     ["nintendo/contents", "atmosphere", "bootloader", "switch", "emummc", "sxos"]),
    ("3ds", "Nintendo 3DS",
     ["nintendo 3ds", "luma", "boot.firm", "cias", "3ds"]),
    ("wiiu", "Wii U", ["wiiu", "install", "sdcafiine"]),
    ("ps_vita", "PS Vita", ["ux0", "tai", "vd0"]),
    ("retro", "Retro handheld / RetroArch", ["retroarch", "roms", "bios"]),
]

# Extension to platform, for the things people actually keep on these cards.
_GAME_EXT = {
    ".nsp": "Switch", ".xci": "Switch", ".nsz": "Switch", ".xcz": "Switch",
    ".3ds": "3DS", ".cia": "3DS", ".cci": "3DS", ".cxi": "3DS", ".3dsx": "3DS homebrew",
    ".wux": "Wii U", ".wud": "Wii U", ".rpx": "Wii U",
    ".vpk": "Vita",
    ".nes": "NES", ".sfc": "SNES", ".smc": "SNES", ".gba": "GBA", ".gb": "GB",
    ".gbc": "GBC", ".n64": "N64", ".z64": "N64", ".nds": "DS", ".iso": "disc image",
}

_DECOR = re.compile(r"[\[(][^\])]*[\])]")


def identify_card(files: List[dict]) -> dict:
    """Work out what a card is from its listing alone."""
    paths = [str(f.get("path") or "").lstrip("/").lower() for f in files]
    consoles = []
    for cid, label, signs in _CONSOLE_SIGNS:
        hits = [s for s in signs
                if any(p == s or p.startswith(s + "/") for p in paths)]
        if hits:
            consoles.append({"id": cid, "label": label, "matched": hits,
                             "confidence": round(min(1.0, len(hits) / 2.0), 2)})
    consoles.sort(key=lambda c: -c["confidence"])

    games, by_platform = [], {}
    for f in files:
        p = str(f.get("path") or "")
        ext = os.path.splitext(p)[1].lower()
        plat = _GAME_EXT.get(ext)
        if not plat:
            continue
        title = os.path.splitext(os.path.basename(p))[0]
        # Strip the usual scene/dump decorations so titles read like titles.
        title = _DECOR.sub("", title).replace("_", " ").strip()
        games.append({"title": title or os.path.basename(p), "platform": plat,
                      "path": p, "size": f.get("size", 0), "ext": ext})
        by_platform[plat] = by_platform.get(plat, 0) + 1
    games.sort(key=lambda g: -int(g.get("size") or 0))
    return {"consoles": consoles,
            "console": consoles[0]["label"] if consoles else "unknown",
            "games": games, "game_count": len(games), "by_platform": by_platform}


@capability(
    "mesh.sd.walk", http_method="POST", http_path="/mesh/sd/walk", http_tags=["mesh", "sd"],
    memory="on",
    description="Recursively list a node's SD card (the listing returns as the job result - read it "
                "back with mesh.jobs). Budgeted: a card can hold tens of thousands of files and the "
                "result must fit one response, so it reports `truncated`. Input: node_id (str!), "
                "path (str='/'), max_files (int=300), max_depth (int=4). Output: {ok, job_id}.",
)
async def cap_mesh_sd_walk(node_id: str = "", path: str = "/", max_files: int = 300,
                           max_depth: int = 4, trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type="sd_walk",
                           payload={"path": path, "max_files": int(max_files),
                                    "max_depth": int(max_depth)})


@capability(
    "mesh.sd.identify", http_method="POST", http_path="/mesh/sd/identify",
    http_tags=["mesh", "sd"], memory="on",
    description="Work out what an inserted SD card IS from a listing - which console (Switch, 3DS, "
                "Wii U, Vita, retro handheld) and which games are on it, from signature directories "
                "and ROM/title extensions. Pass a listing from mesh.sd.walk as `files`, or omit it "
                "to queue a walk first. Titles come from FILENAMES - this reads no title database, "
                "so a badly named dump reads badly. Input: node_id (str), files (JSON list of "
                "{path,size}). Output: {console, consoles, games, by_platform, game_count}.",
)
async def cap_mesh_sd_identify(node_id: str = "", files=None, trace_id=None) -> dict:
    files = json.loads(files) if isinstance(files, str) else files
    if not files:
        if not node_id:
            return {"error": "pass files, or node_id to queue a walk"}
        r = await cap_mesh_sd_walk(node_id=node_id, max_files=400)
        return {"queued": True, "job_id": (r or {}).get("job_id"),
                "hint": "read the walk result with mesh.jobs, then call again with files=<result.files>"}
    if not isinstance(files, list):
        return {"error": "files must be a list of {path,size}"}
    return identify_card(files)


@capability(
    "mesh.sd.dump", http_method="POST", http_path="/mesh/sd/dump", http_tags=["mesh", "sd"],
    memory="on",
    description="Archive files off a node's SD card into Vera's store, idempotently - the node "
                "streams each file straight off the card and anything already stored at the same "
                "size is skipped, so re-running costs almost nothing and never duplicates. Files are "
                "PUSHED by the node (pulling them as job results would cost a long-poll round trip "
                "per KB). Input: node_id (str!), paths (JSON list! of paths from mesh.sd.walk), "
                "max_bytes (int=8388608 - per-job budget). Output: {ok, job_id, queued}.",
)
async def cap_mesh_sd_dump(node_id: str = "", paths=None, max_bytes: int = 8388608,
                           trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    paths = json.loads(paths) if isinstance(paths, str) else paths
    if not isinstance(paths, list) or not paths:
        return {"error": "paths must be a non-empty list (use mesh.sd.walk to get them)"}
    r = await _call_cap("mesh.send", node_id=node_id, type="sd_upload",
                        payload={"paths": [str(p) for p in paths],
                                 "max_bytes": int(max_bytes)})
    return {"ok": bool(isinstance(r, dict) and not r.get("error")),
            "queued": len(paths), "job_id": (r or {}).get("job_id"),
            "store": _SD_STORE}


@capability(
    "mesh.sd.store.list", http_method="GET", http_path="/mesh/sd/store",
    http_tags=["mesh", "sd"], memory="off", silent=True,
    description="List what has been archived off SD cards, per node. Input: node_id (str - optional "
                "filter). Output: {files:[{node,path,bytes}], count, bytes, store}.",
)
async def cap_mesh_sd_store_list(node_id: str = "", trace_id=None) -> dict:
    out, total = [], 0
    for root, _dirs, names in os.walk(_SD_STORE):
        for n in names:
            full = os.path.join(root, n)
            rel = os.path.relpath(full, _SD_STORE).replace("\\", "/")
            node = rel.split("/", 1)[0]
            if node_id and node != node_id:
                continue
            sz = os.path.getsize(full)
            total += sz
            out.append({"node": node, "path": rel.split("/", 1)[-1], "bytes": sz})
    return {"files": sorted(out, key=lambda f: -f["bytes"])[:500],
            "count": len(out), "bytes": total, "store": _SD_STORE}


def _sd_store_path(node_id: str, sd_path: str) -> str:
    """Map node + card path to a store path, refusing anything that would escape
    the store. An unauthenticated LAN device chooses these names."""
    node = re.sub(r"[^A-Za-z0-9_.-]", "-", node_id or "unknown")[:64] or "unknown"
    # Spaces and brackets are normal in game filenames and harmless here; what
    # matters is that no part can be a separator or a parent reference.
    parts = [re.sub(r"[^A-Za-z0-9 _.()\[\]-]", "-", p).strip()[:80]
             for p in (sd_path or "").replace("\\", "/").split("/")
             if p not in ("", ".", "..")]
    parts = [p for p in parts if p and p not in (".", "..")]
    if not parts:
        parts = ["unnamed"]
    return os.path.join(_SD_STORE, node, *parts)


try:
    from fastapi import Request as _Req
    from fastapi.responses import JSONResponse as _JSON

    @APP.post("/mesh/sd/upload", include_in_schema=False)
    async def _mesh_sd_upload(req: "_Req"):
        """Receive one file streamed off a node's SD card.

        Idempotent by (path, size): an unchanged file answers 208 and is not
        rewritten, which is what makes re-running a dump cheap."""
        node = req.headers.get("X-Node-Id", "")
        sd_path = req.headers.get("X-Sd-Path", "")
        if not node or not sd_path:
            return _JSON({"error": "X-Node-Id and X-Sd-Path required"}, status_code=400)
        dest = _sd_store_path(node, sd_path)
        try:
            declared = int(req.headers.get("X-Sd-Size") or 0)
        except ValueError:
            declared = 0
        if declared and os.path.exists(dest) and os.path.getsize(dest) == declared:
            return _JSON({"ok": True, "skipped": True, "path": dest}, status_code=208)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        written = 0
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as f:
                async for chunk in req.stream():
                    f.write(chunk)
                    written += len(chunk)
            os.replace(tmp, dest)          # only becomes visible once complete
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            log.warning("sd upload %s: %s", sd_path, e)
            return _JSON({"error": str(e)}, status_code=500)
        await emit_event({"type": "mesh.sd.upload", "node_id": node,
                          "path": sd_path, "bytes": written})
        return _JSON({"ok": True, "path": dest, "bytes": written}, status_code=201)
except Exception as _e:                      # pragma: no cover - import guard
    log.debug("sd upload route not registered: %s", _e)


# ════════════════════════════════════════════════════════════════════════════
# Info dashboards — live Vera data on the panel
# ════════════════════════════════════════════════════════════════════════════
# A dashboard is DATA, not code: rows of {label, cap, args, pick}. The renderer
# calls each capability, digs a value out of the result and draws a line. Adding
# "show me X" means adding a row, and every cap name is checked against the live
# registry so a typo shows on screen instead of rendering a blank panel.

def _dig(obj, path: str):
    """Follow a dotted path into a result. `a.b.0.c` walks dicts and lists;
    a bare list or dict yields its length, which is usually what you want on a
    one-line readout ("alerts: 3")."""
    cur = obj
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    if isinstance(cur, (list, dict)):
        return len(cur)
    return cur


def _fmt(v) -> str:
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


# Every cap name here is verified against the running registry at launch.
INFO_APPS: Dict[str, dict] = {
    "info:mesh": {"label": "Mesh stats", "rows": [
        {"label": "nodes", "cap": "mesh.nodes", "pick": "nodes"},
        {"label": "online", "cap": "mesh.nodes", "pick": "online"},
        {"label": "transports", "cap": "mesh.nodes", "pick": "transports"},
        {"label": "jobs queued", "cap": "mesh.jobs", "pick": "count"},
    ]},
    "info:system": {"label": "Vera system", "rows": [
        {"label": "status", "cap": "dash.health.summary", "pick": "status"},
        {"label": "services", "cap": "dash.health.summary", "pick": "services"},
        {"label": "cpu %", "cap": "dash.health.summary", "pick": "cpu_percent"},
        {"label": "mem %", "cap": "dash.health.summary", "pick": "mem_percent"},
        {"label": "workers", "cap": "obs.workers", "pick": "workers"},
    ]},
    "info:markets": {"label": "Markets", "rows": [
        {"label": "watchlist", "cap": "markets.watchlist.list", "pick": "watchlist"},
        {"label": "alerts", "cap": "markets.alerts.list", "pick": "alerts"},
    ]},
    "info:calendar": {"label": "Calendar", "rows": [
        {"label": "today", "cap": "cal.events.list", "pick": "events"},
        {"label": "todos", "cap": "cal.todos.list", "pick": "todos"},
    ]},
    "info:weather": {"label": "Weather", "rows": [], "needs": ("weather_lat", "weather_lon")},
    "info:news": {"label": "News", "rows": [
        {"label": "headlines", "cap": "markets.news.feed", "pick": "items"},
    ]},
}


def _cap_exists(name: str) -> bool:
    try:
        from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
        return name in CAPABILITY_REGISTRY
    except Exception:
        return False


async def _app_dashboard_build(spec: dict, node_id: str):
    """Render one dashboard. Rows resolve concurrently — a serial pass over five
    capabilities would leave the panel stale for seconds."""
    rows = spec.get("rows") or []
    known = [r for r in rows if _cap_exists(r.get("cap", ""))]
    missing = [r for r in rows if r not in known]

    async def _one(r):
        try:
            res = await _call_cap(r["cap"], **(r.get("args") or {}))
            # http.get and friends hand back a raw body; `json: true` parses it so
            # any REST endpoint can be a row without needing its own capability.
            if r.get("json") and isinstance(res, dict) and isinstance(res.get("body"), str):
                try:
                    return json.loads(res["body"])
                except Exception:
                    return {"error": "bad json"}
            return res
        except Exception as e:
            return {"error": str(e)}

    results = await asyncio.gather(*[_one(r) for r in known]) if known else []

    widgets, y = [], 50
    for r, res in zip(known, results):
        if isinstance(res, dict) and res.get("error"):
            val, col = "error", C_RED
        else:
            v = _dig(res, r.get("pick", ""))
            val = _fmt(v) if v is not None else "-"
            col = C_GREEN
        widgets.append({"t": "label", "x": 8, "y": y, "size": 2, "color": C_WHITE,
                        "text": str(r["label"])[:14]})
        widgets.append({"t": "label", "x": 200, "y": y, "size": 2, "color": col,
                        "text": val[:16]})
        y += 26
    for r in missing:
        # Say so rather than silently dropping the row — a missing capability is
        # a configuration fact worth seeing.
        widgets.append({"t": "label", "x": 8, "y": y, "size": 1, "color": C_GREY,
                        "text": "%s: no capability %s" % (r.get("label"), r.get("cap"))})
        y += 16
    if not widgets:
        widgets.append({"t": "label", "x": 8, "y": 60, "size": 2, "color": C_GREY,
                        "text": "nothing to show"})
    widgets.append({"t": "button", "x": 8, "y": 258, "w": 150, "h": 46,
                    "text": "Refresh", "action": "app:" + spec["_id"], "bg": C_NAVY})
    widgets.append({"t": "button", "x": 168, "y": 258, "w": 150, "h": 46,
                    "text": "Apps", "action": "app:launcher", "bg": C_GREY})
    return ({"title": spec.get("label") or "Info", "bg": C_BLACK, "widgets": widgets}, {})


_WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                "&current=temperature_2m,apparent_temperature,wind_speed_10m,"
                "relative_humidity_2m&timezone=auto")


def _weather_rows() -> List[dict]:
    """Vera has no weather capability, so this drives open-meteo (public, keyless)
    through http.get. Coordinates come from mesh settings — guessing someone's
    location would be worse than saying it is not configured."""
    lat = lon = ""
    try:
        mesh = (sys.modules.get("mesh_capabilities")
                or sys.modules.get("Vera.vera.mesh.mesh_capabilities"))
        if mesh and hasattr(mesh, "_settings_get_sync"):
            st = mesh._settings_get_sync() or {}
            lat, lon = str(st.get("weather_lat", "")), str(st.get("weather_lon", ""))
    except Exception as e:
        log.debug("weather settings: %s", e)
    if not (lat and lon):
        return []
    url = _WEATHER_URL % (lat, lon)
    return [
        {"label": "temp C", "cap": "http.get", "args": {"url": url}, "json": True,
         "pick": "current.temperature_2m"},
        {"label": "feels C", "cap": "http.get", "args": {"url": url}, "json": True,
         "pick": "current.apparent_temperature"},
        {"label": "wind", "cap": "http.get", "args": {"url": url}, "json": True,
         "pick": "current.wind_speed_10m"},
        {"label": "humidity", "cap": "http.get", "args": {"url": url}, "json": True,
         "pick": "current.relative_humidity_2m"},
    ]


def _app_dashboard(app_id: str):
    async def build(node_id: str, ctx: dict):
        spec = dict(INFO_APPS[app_id], _id=app_id)
        if app_id == "info:weather":
            spec["rows"] = _weather_rows()
            if not spec["rows"]:
                return ({"title": "Weather", "bg": C_BLACK, "widgets": [
                    {"t": "label", "x": 8, "y": 60, "size": 2, "color": C_YELL,
                     "text": "not configured"},
                    {"t": "label", "x": 8, "y": 90, "size": 1, "color": C_GREY,
                     "text": "set weather_lat / weather_lon via mesh.settings.set"},
                    {"t": "button", "x": 8, "y": 258, "w": 150, "h": 46,
                     "text": "Apps", "action": "app:launcher", "bg": C_GREY}]}, {})
        return await _app_dashboard_build(spec, node_id)
    return build


# ── Scans + web pages, straight from the panel ──────────────────────────────

# Stock intake. The shield has no barcode reader, so this drives the SAME
# capabilities the Scan Station UI uses — the node is the controller, and a
# BLE/USB scanner still pairs with the workstation running that UI.
INTAKE_PAD = {"label": "Stock intake", "buttons": [
    {"label": "Open units", "cap": "business.unit.list"},
    {"label": "Inventory", "cap": "business.inventory.list"},
    {"label": "Orders", "cap": "business.order.list"},
    {"label": "Brief", "cap": "business.brief"},
]}

SCAN_PAD = {"label": "Scans", "buttons": [
    {"label": "Wi-Fi scan", "cap": "mesh.wifi.scan", "self": True},
    {"label": "Channel survey", "cap": "mesh.channel.survey", "self": True},
    {"label": "BLE scan", "cap": "mesh.ble.scan", "self": True},
    {"label": "LAN scan", "cap": "netscan.lan.scan", "confirm": True},
    {"label": "Display probe", "cap": "mesh.display.probe", "self": True},
    {"label": "Sysinfo", "cap": "mesh.sysinfo", "self": True},
]}


@capability(
    "mesh.ui.webview", http_method="POST", http_path="/mesh/ui/webview",
    http_tags=["mesh", "ui"], memory="on",
    description="Put a web page on a display node. Vera screenshots the URL with the headless "
                "browser, renders it to the panel's native RGB565 and the node streams it — the "
                "device runs no browser. Best on pages with large type; a 480x320 panel cannot make "
                "a dense desktop layout legible. Input: node_id (str!), url (str!), w (int=480), "
                "h (int=320), fit (str=contain|cover|stretch), full_page (bool=False — the visible "
                "viewport usually reads better than a long page squeezed down), wait_ms (int=0). "
                "Output: {ok, url, title, name, bytes, job_id}.",
    schema={"properties": {"fit": {"enum": ["contain", "cover", "stretch"]}}},
)
async def cap_mesh_ui_webview(node_id: str = "", url: str = "", w: int = 480, h: int = 320,
                              fit: str = "cover", full_page: bool = False, wait_ms: int = 0,
                              trace_id=None) -> dict:
    if not node_id or not url:
        return {"error": "node_id and url required"}
    if not _cap_exists("browser.screenshot"):
        return {"error": "browser.screenshot is unavailable — the headless browser "
                         "(Playwright) is not installed on this Vera"}
    shot = await _call_cap("browser.screenshot", url=url, full_page=bool(full_page),
                           wait_ms=int(wait_ms))
    if not (isinstance(shot, dict) and shot.get("image_b64")):
        return {"error": "screenshot failed: %s" % ((shot or {}).get("error") or "no image"),
                "url": url}
    res = await cap_mesh_ui_image(node_id=node_id, data_b64=shot["image_b64"],
                                  w=int(w), h=int(h), fit=fit, clear=True)
    res["url"] = shot.get("url", url)
    res["title"] = shot.get("title", "")
    return res


# ── Fleet update ────────────────────────────────────────────────────────────
# "Update everything" must not mean "make every node identical". Nodes are
# flashed with different options (display on/off, board pin map, CSI, Wi-Fi), so
# a blanket push of one image would silently reconfigure half the fleet. Each
# node is matched to the artifact built for ITS board and runtime, and anything
# without a match is reported rather than given something that nearly fits.


async def plan_fleet_update(only_behind: bool = True, node_ids=None) -> dict:
    """Work out what each node would receive. Pure planning — sends nothing."""
    nodes_res = await _call_cap("mesh.nodes")
    nodes = (nodes_res or {}).get("nodes") or []
    if node_ids:
        wanted = set(node_ids)
        nodes = [n for n in nodes if n.get("node_id") in wanted]

    plan, skipped = [], []
    for n in nodes:
        nid = n.get("node_id") or ""
        cfg = n.get("config") or {}
        fw = str(n.get("fw") or "")
        rt = node_runtime(str(n.get("runtime") or ""), fw)
        online = bool(n.get("online", n.get("last_seen")))
        chans = n.get("channels") or []

        def _skip(why):
            skipped.append({"node_id": nid, "reason": why, "fw": fw, "runtime": rt})

        if not online:
            _skip("offline")
            continue
        if chans and "http" not in chans:
            _skip("no http channel — cannot self-fetch an update")
            continue
        if not _auto_ota_enabled(cfg):
            _skip("auto-OTA disabled for this node (config.ota.auto=false)")
            continue
        if not rt:
            _skip(f"unknown runtime (fw {fw!r}) — reflash once so it reports one")
            continue

        if rt == "micropython":
            target = served_fw_version("micropython")
            if not target:
                _skip("no served micropython version")
                continue
            if only_behind and fw == target:
                _skip("already current")
                continue
            plan.append({"node_id": nid, "runtime": rt, "from": fw, "to": target,
                         "mode": "file", "artifact": "main.py"})
        else:
            art = newest_arduino_bin(str(n.get("chip") or ""))
            if not art:
                _skip("no built image for this node's chip — build one first")
                continue
            target = art.get("fw_version") or ""
            if only_behind and target and fw == target:
                _skip("already current")
                continue
            plan.append({"node_id": nid, "runtime": rt, "from": fw, "to": target,
                         "mode": "bin", "artifact": art["name"],
                         "board": art.get("board", "")})
    return {"plan": plan, "skipped": skipped,
            "count": len(plan), "skipped_count": len(skipped)}


@capability(
    "mesh.ota.plan", http_method="POST", http_path="/mesh/ota/plan",
    http_tags=["mesh", "ota"], memory="off", silent=True,
    description="Show what a fleet update WOULD do, without sending anything — per node, which "
                "image it would get and why, plus every node that would be skipped and the reason "
                "(offline, no http channel, auto-OTA disabled, unknown runtime, no image for its "
                "chip, already current). Input: only_behind (bool=True), node_ids (JSON list). "
                "Output: {plan:[...], skipped:[...], count, skipped_count}.",
)
async def cap_mesh_ota_plan(only_behind: bool = True, node_ids=None, trace_id=None) -> dict:
    node_ids = json.loads(node_ids) if isinstance(node_ids, str) else node_ids
    return await plan_fleet_update(bool(only_behind), node_ids)


@capability(
    "mesh.ota.all", http_method="POST", http_path="/mesh/ota/all",
    http_tags=["mesh", "ota"], memory="on",
    description="Update the whole fleet over Wi-Fi, each node to the artifact built for ITS board "
                "and runtime — nodes are flashed with different options, so one blanket image would "
                "silently reconfigure half of them. Honours each node's auto-OTA setting and skips "
                "anything offline, serial-only, or with no matching image, reporting why. Run "
                "mesh.ota.plan first to see the effect. Input: only_behind (bool=True — skip nodes "
                "already on the target version), node_ids (JSON list — limit to these), confirm "
                "(bool=False — must be true to actually send). Output: {sent, failed, skipped, plan}.",
)
async def cap_mesh_ota_all(only_behind: bool = True, node_ids=None, confirm: bool = False,
                           trace_id=None) -> dict:
    node_ids = json.loads(node_ids) if isinstance(node_ids, str) else node_ids
    planned = await plan_fleet_update(bool(only_behind), node_ids)
    if not confirm:
        return dict(planned, dry_run=True,
                    hint="re-run with confirm=true to send these updates")
    sent, failed = [], []
    for item in planned["plan"]:
        kw = {"node_id": item["node_id"], "mode": item["mode"]}
        if item["mode"] == "file":
            kw["filename"] = "main.py"
        else:
            kw["artifact"] = item["artifact"]
        r = await _call_cap("mesh.ota", **kw)
        if isinstance(r, dict) and r.get("error"):
            failed.append({**item, "error": r["error"]})
        else:
            sent.append({**item, "job_id": (r or {}).get("job_id")})
    await emit_event({"type": "mesh.ota.fleet", "sent": len(sent), "failed": len(failed)})
    return {"ok": not failed, "sent": sent, "failed": failed,
            "skipped": planned["skipped"], "counts": {
                "sent": len(sent), "failed": len(failed),
                "skipped": len(planned["skipped"])}}


# ── List dashboards: real rows, not counts ──────────────────────────────────
# A count is almost never the thing you want on a wall display. "watchlist: 4"
# tells you nothing; four tickers with prices does. A LIST dashboard pulls a
# collection out of a capability result and renders one line per item, with an
# optional right-hand value column.

def _pick_list(res, path: str) -> List:
    """Like _dig, but returns the collection itself rather than its length."""
    cur = res
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return []
        else:
            return []
        if cur is None:
            return []
    if isinstance(cur, dict):
        # dict-of-things: render its values, keeping the key as a fallback label
        return [dict(v, _key=k) if isinstance(v, dict) else {"_key": k, "value": v}
                for k, v in cur.items()]
    return cur if isinstance(cur, list) else []


def _field(item, names, default=""):
    """First present field from a list of candidate names."""
    if not isinstance(item, dict):
        return str(item)
    for n in names:
        if item.get(n) not in (None, ""):
            return item[n]
    return default


LIST_APPS: Dict[str, dict] = {
    "info:watchlist": {
        "label": "Watchlist", "cap": "markets.watchlist.list", "path": "watchlist",
        "left": ["symbol", "ticker", "pair", "name", "_key"],
        "right": ["last", "price", "close", "value"],
    },
    "info:alerts": {
        "label": "Market alerts", "cap": "markets.alerts.list", "path": "alerts",
        "left": ["symbol", "title", "name", "_key"],
        "right": ["state", "status", "level", "value"],
    },
    "info:agenda": {
        "label": "Agenda", "cap": "cal.events.list", "path": "events",
        "left": ["title", "summary", "name", "_key"],
        "right": ["start", "start_time", "when", "time"],
    },
    "info:todos": {
        "label": "To-do", "cap": "cal.todos.list", "path": "todos",
        "left": ["title", "text", "name", "_key"],
        "right": ["due", "due_date", "status"],
    },
    "info:headlines": {
        "label": "Headlines", "cap": "markets.news.feed", "path": "items",
        "left": ["title", "headline", "summary", "_key"],
        "right": ["source", "published", "ts"],
    },
    "info:orders": {
        "label": "Orders", "cap": "business.order.list", "path": "orders",
        "left": ["reference", "id", "customer", "_key"],
        "right": ["status", "total", "state"],
    },
    "info:stock": {
        "label": "Inventory", "cap": "business.inventory.list", "path": "items",
        "left": ["name", "sku", "title", "_key"],
        "right": ["qty", "quantity", "stock", "count"],
    },
    "info:nodes": {
        "label": "Mesh nodes", "cap": "mesh.nodes", "path": "nodes",
        "left": ["node_id", "name", "_key"],
        "right": ["fw", "rssi", "status"],
    },
}

_LIST_ROWS = 8          # what fits at size 2 with a title and a button row


async def _app_list_build(spec: dict, node_id: str):
    if not _cap_exists(spec["cap"]):
        widgets = [{"t": "label", "x": 8, "y": 60, "size": 1, "color": C_GREY,
                    "text": "no capability " + spec["cap"]}]
    else:
        try:
            res = await _call_cap(spec["cap"], **(spec.get("args") or {}))
        except Exception as e:
            res = {"error": str(e)}
        if isinstance(res, dict) and res.get("error"):
            widgets = [{"t": "label", "x": 8, "y": 60, "size": 2, "color": C_RED,
                        "text": str(res["error"])[:40]}]
        else:
            items = _pick_list(res, spec.get("path", ""))
            widgets, y = [], 48
            for it in items[:_LIST_ROWS]:
                left = str(_field(it, spec["left"], "?"))
                right = str(_field(it, spec.get("right") or [], ""))
                widgets.append({"t": "label", "x": 8, "y": y, "size": 2,
                                "color": C_WHITE, "text": left[:26]})
                if right:
                    widgets.append({"t": "label", "x": 300, "y": y, "size": 2,
                                    "color": C_GREEN, "text": right[:9]})
                y += 24
            if not items:
                widgets = [{"t": "label", "x": 8, "y": 60, "size": 2, "color": C_GREY,
                            "text": "nothing to show"}]
            elif len(items) > _LIST_ROWS:
                widgets.append({"t": "label", "x": 8, "y": y, "size": 1, "color": C_GREY,
                                "text": "+%d more" % (len(items) - _LIST_ROWS)})
    widgets.append({"t": "button", "x": 8, "y": 258, "w": 150, "h": 46,
                    "text": "Refresh", "action": "app:" + spec["_id"], "bg": C_NAVY})
    widgets.append({"t": "button", "x": 168, "y": 258, "w": 150, "h": 46,
                    "text": "Apps", "action": "app:launcher", "bg": C_GREY})
    return ({"title": spec.get("label") or "List", "bg": C_BLACK,
             "widgets": widgets}, {})


def _app_list(app_id: str):
    async def build(node_id: str, ctx: dict):
        return await _app_list_build(dict(LIST_APPS[app_id], _id=app_id), node_id)
    return build


# ── Sprites and companions from Sprite Studio ───────────────────────────────

@capability(
    "mesh.ui.sprite", http_method="POST", http_path="/mesh/ui/sprite",
    http_tags=["mesh", "ui"], memory="on",
    description="Put a Sprite Studio character on a display node — animated if the sprite has "
                "frames, otherwise a still. Names come from spritegen.list; pass animation to pick "
                "one of its animations. Input: node_id (str!), sprite (str! — a spritegen id/name), "
                "animation (str — defaults to the first), w (int=128), h (int=128), x (int=0), "
                "y (int=0), fps (int=10), loop (bool=True). Output: {ok, sprite, frames, job_id}.",
)
async def cap_mesh_ui_sprite(node_id: str = "", sprite: str = "", animation: str = "",
                             w: int = 128, h: int = 128, x: int = 0, y: int = 0,
                             fps: int = 10, loop: bool = True, trace_id=None) -> dict:
    if not node_id or not sprite:
        return {"error": "node_id and sprite required"}

    # A generated image is already a picture — show it directly rather than
    # sending it through a character lookup that will not find it.
    if sprite.startswith("/") or sprite.startswith("http"):
        return await cap_mesh_ui_image(node_id=node_id, url=sprite, w=w, h=h,
                                       x=x, y=y, fit="contain")

    rec = None
    if _cap_exists("spritegen.get"):
        rec = await _call_cap("spritegen.get", char_id=sprite)
        if not (isinstance(rec, dict) and rec.get("char_id")):
            rec = None
    # Companions live in a different subsystem with the same shape of problem.
    if rec is None and _cap_exists("character.get"):
        c = await _call_cap("character.get", agent_id=sprite)
        if isinstance(c, dict) and (c.get("agent_id") or c.get("id")):
            still = c.get("sheet") or c.get("preview") or c.get("image")
            if not (c.get("urls") or {}).get("frames") and still:
                return await cap_mesh_ui_image(node_id=node_id, url=still, w=w,
                                               h=h, x=x, y=y, fit="contain")
            rec = dict(c, char_id=c.get("agent_id") or c.get("id"))
    if rec is None:
        return {"error": f"{sprite!r} not found in Sprite Studio, companions or images"}

    frames, sheet, anim = _sprite_anim_frames(rec, animation)
    rate = int(fps or (rec.get("animations") or {}).get(anim, {}).get("fps") or 10)

    if frames:
        r = await cap_mesh_ui_animate(node_id=node_id, frames=frames, w=w, h=h,
                                      x=x, y=y, fps=rate, loop=loop, fit="contain")
        r["animation"] = anim
        return r
    if sheet.get("gif"):                       # the rendered preview animates too
        r = await cap_mesh_ui_animate(node_id=node_id, url=sheet["gif"], w=w, h=h,
                                      x=x, y=y, fps=rate, loop=loop, fit="contain")
        r["animation"] = anim
        return r
    if sheet.get("png"):
        return await cap_mesh_ui_image(node_id=node_id, url=sheet["png"], w=w, h=h,
                                       x=x, y=y, fit="contain")
    return {"error": f"'{sprite_summary(rec)['label']}' has no generated frames yet — "
                     f"run an animation in Sprite Studio, then show it here",
            "animations": sorted((rec.get("animations") or {}).keys())}


@capability(
    "mesh.ui.sprites", http_method="GET", http_path="/mesh/ui/sprites",
    http_tags=["mesh", "ui"], memory="off", silent=True,
    description="List Sprite Studio characters that can be shown on a display node. "
                "Output: {sprites:[{id,label}], count} (empty when spritegen is not installed).",
)
async def cap_mesh_ui_sprites(trace_id=None) -> dict:
    """Everything showable on a panel, from all three places Vera keeps art:
    Sprite Studio characters, companion characters, and generated images. They
    live in different subsystems with different record shapes, so a single
    picker has to speak all three rather than one and call it "sprites"."""
    out = []

    if _cap_exists("spritegen.list"):
        res = await _call_cap("spritegen.list")
        for c in (res or {}).get("characters") or []:
            if isinstance(c, dict) and c.get("char_id"):
                s = sprite_summary(c)
                s["source"] = "spritegen"
                out.append(s)

    if _cap_exists("character.list"):
        res = await _call_cap("character.list")
        for c in (res or {}).get("characters") or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("agent_id") or c.get("id") or ""
            if not cid:
                continue
            urls = c.get("urls") or {}
            ready = bool(c.get("sheet") or c.get("preview") or c.get("image")
                         or (urls.get("frames") or urls.get("sheets")))
            out.append({"id": cid, "source": "companion",
                        "label": (c.get("display_name") or c.get("name")
                                  or cid[:12]),
                        "animations": sorted((c.get("animations") or {}).keys()),
                        "ready": ready, "frames": 0, "animation": ""})

    if _cap_exists("images.list"):
        res = await _call_cap("images.list", limit=40)
        for im in (res or {}).get("images") or []:
            if isinstance(im, dict) and im.get("url"):
                out.append({"id": im["url"], "source": "image",
                            "label": (im.get("prompt") or im["url"].split("/")[-1])[:40],
                            "animations": [], "ready": True, "frames": 1,
                            "animation": ""})

    if not out:
        return {"sprites": [], "count": 0, "ready": 0,
                "note": "nothing found in Sprite Studio, companions or generated images"}
    ready = [s for s in out if s["ready"]]
    note = ""
    if not ready:
        note = ("%d item(s) found but none are renderable yet — generate frames "
                "in Sprite Studio first" % len(out))
    return {"sprites": out, "count": len(out), "ready": len(ready), "note": note}


# ════════════════════════════════════════════════════════════════════════════
# Node UI kit — the fundamentals every screen is built from
# ════════════════════════════════════════════════════════════════════════════
# Apps used to hand-place x/y, so every one drifted: different margins, text
# running under buttons, rows off the bottom edge. The kit fixes the geometry
# once, in terms of the actual hardware: a 480x320 panel and a 5x7 font that
# scales in whole pixels (size s => 6s wide, 8s tall per character).
#
# Everything below is derived from those two facts, so a screen composed through
# the kit cannot overflow, overlap the action bar, or drift from its neighbours.

UI_W, UI_H = 480, 320                 # the panel, in its landscape rotation
UI_MARGIN = 8                         # breathing room at every edge
UI_GUTTER = 10                        # between side-by-side elements

# Type scale. Sizes are font multipliers, not points — 1/2/3 are the only ones
# that stay legible at arm's length on this panel.
UI_TITLE, UI_BODY, UI_CAPTION = 3, 2, 1
UI_CHAR_W, UI_CHAR_H = 6, 8           # one character cell at size 1


def ui_text_w(text: str, size: int = UI_BODY) -> int:
    return len(str(text)) * UI_CHAR_W * max(1, int(size))


def ui_text_h(size: int = UI_BODY) -> int:
    return UI_CHAR_H * max(1, int(size))


def ui_clip(text, size: int = UI_BODY, width: int = None) -> str:
    width = UI_W - 2 * UI_MARGIN if width is None else width
    chars = max(1, width // (UI_CHAR_W * max(1, int(size))))
    t = str(text)
    return t if len(t) <= chars else t[:chars]


# Vertical zones. Fixed so the action bar is always in the same place — muscle
# memory matters on a wall-mounted panel.
UI_TITLE_Y = 6
UI_RULE_Y = UI_TITLE_Y + ui_text_h(UI_TITLE) + 4
UI_BODY_Y = UI_RULE_Y + 8                       # content starts here
UI_BTN_H = 46
UI_BTN_W = (UI_W - 2 * UI_MARGIN - UI_GUTTER) // 2
UI_BAR_Y = UI_H - UI_MARGIN - UI_BTN_H          # action bar sits on the bottom
UI_BODY_H = UI_BAR_Y - UI_BODY_Y - UI_GUTTER    # content must not run under it
UI_ROW_H = ui_text_h(UI_BODY) + 8               # one list/kv row
UI_ROWS = UI_BODY_H // UI_ROW_H                 # rows that genuinely fit

# Colour roles rather than raw colours, so a screen reads consistently and a
# palette change is one edit. (RGB565, matching the firmware constants.)
UI_ROLE = {
    "bg": C_BLACK, "fg": C_WHITE, "muted": C_GREY, "title": C_YELL,
    "good": C_GREEN, "warn": C_YELL, "bad": C_RED, "accent": C_NAVY,
    "info": C_CYAN,
}


def ui_colour(role_or_value, default="fg"):
    """Accept a role name or a raw RGB565 int, so specs can stay semantic."""
    if isinstance(role_or_value, int):
        return role_or_value
    if isinstance(role_or_value, str) and role_or_value in UI_ROLE:
        return UI_ROLE[role_or_value]
    return UI_ROLE.get(default, C_WHITE)


# ── Primitives ──────────────────────────────────────────────────────────────

def ui_label(x, y, text, size=UI_BODY, colour="fg", bg=None, width=None):
    w = {"t": "label", "x": int(x), "y": int(y),
         "text": ui_clip(text, size, width), "size": int(size),
         "color": ui_colour(colour)}
    if bg is not None:
        w["bg"] = ui_colour(bg, "bg")
    return w


def ui_rule(y, colour="muted"):
    return {"t": "rect", "x": UI_MARGIN, "y": int(y),
            "w": UI_W - 2 * UI_MARGIN, "h": 2, "color": ui_colour(colour, "muted")}


def ui_button(x, y, text, action, colour="fg", bg="accent",
              w=None, h=None, size=UI_BODY):
    return {"t": "button", "x": int(x), "y": int(y),
            "w": int(w or UI_BTN_W), "h": int(h or UI_BTN_H),
            "text": ui_clip(text, size, (w or UI_BTN_W) - 12),
            "action": action, "color": ui_colour(colour),
            "bg": ui_colour(bg, "accent"), "size": int(size)}


def ui_bar(x, y, val, label="", w=None, colour="good"):
    out = []
    w = int(w or (UI_W - 2 * UI_MARGIN))
    if label:
        out.append(ui_label(x, y - ui_text_h(UI_CAPTION) - 4, label,
                            UI_CAPTION, "muted"))
    out.append({"t": "bar", "x": int(x), "y": int(y), "w": w, "h": 12,
                "val": max(0, min(100, int(val))), "color": ui_colour(colour, "good")})
    return out


def ui_action_bar(buttons):
    """Up to two buttons pinned to the bottom. More than two on a touch panel
    this size means mis-taps, so the rest belong on another screen."""
    out = []
    for i, b in enumerate(buttons[:2]):
        out.append(ui_button(UI_MARGIN + i * (UI_BTN_W + UI_GUTTER), UI_BAR_Y,
                             b.get("text", "?"), b.get("action", ""),
                             bg=b.get("bg", "accent" if i == 0 else "muted")))
    return out


def ui_screen(title, blocks, actions=None, bg="bg"):
    """Compose a screen: title, flowed content, fixed action bar."""
    widgets = []
    if title:
        widgets.append(ui_label(UI_MARGIN, UI_TITLE_Y, title, UI_TITLE, "title"))
        widgets.append(ui_rule(UI_RULE_Y))
    widgets.extend(blocks or [])
    widgets.extend(ui_action_bar(actions or [{"text": "Apps", "action": "app:launcher"}]))
    return {"title": "", "bg": ui_colour(bg, "bg"), "widgets": widgets}


# ── Layout helpers: flow content without touching coordinates ───────────────

class UiFlow:
    """Stacks content down the body zone and refuses to run past it. Callers
    never compute y, which is what kept going wrong by hand."""

    def __init__(self, y=None):
        self.y = UI_BODY_Y if y is None else int(y)
        self.widgets = []
        self.overflow = 0

    @property
    def room(self) -> int:
        return max(0, UI_BAR_Y - UI_GUTTER - self.y)

    def _fits(self, h) -> bool:
        if h <= self.room:
            return True
        self.overflow += 1
        return False

    def text(self, text, size=UI_BODY, colour="fg"):
        h = ui_text_h(size) + 6
        if self._fits(h):
            self.widgets.append(ui_label(UI_MARGIN, self.y, text, size, colour))
            self.y += h
        return self

    def kv(self, key, value, colour="good"):
        """A label on the left, its value on the right — the workhorse row."""
        if self._fits(UI_ROW_H):
            self.widgets.append(ui_label(UI_MARGIN, self.y, key, UI_BODY, "fg",
                                         width=UI_W // 2))
            self.widgets.append(ui_label(UI_W // 2 + 20, self.y, value, UI_BODY,
                                         colour, width=UI_W // 2 - 28))
            self.y += UI_ROW_H
        return self

    def rows(self, items, value_key=None, colour="good"):
        for it in items:
            if isinstance(it, (list, tuple)) and len(it) == 2:
                self.kv(it[0], it[1], colour)
            elif isinstance(it, dict):
                self.kv(it.get("k", ""), it.get("v", ""), it.get("colour", colour))
            else:
                self.text(it)
        return self

    def bar(self, label, val, colour="good"):
        h = ui_text_h(UI_CAPTION) + 4 + 12 + 8
        if self._fits(h):
            self.y += ui_text_h(UI_CAPTION) + 4
            self.widgets.extend(ui_bar(UI_MARGIN, self.y, val, label))
            self.y += 12 + 8
        return self

    def note(self, text):
        return self.text(text, UI_CAPTION, "muted")

    def done(self):
        """Flush, adding an honest marker when content had to be dropped."""
        if self.overflow and self.room >= ui_text_h(UI_CAPTION):
            self.widgets.append(ui_label(UI_MARGIN, self.y,
                                         "+%d more" % self.overflow,
                                         UI_CAPTION, "muted"))
        return self.widgets


# ════════════════════════════════════════════════════════════════════════════
# UI builder — declarative screens, no coordinates
# ════════════════════════════════════════════════════════════════════════════
# Widget-level screens need pixel maths, which is exactly what a person (or an
# LLM) gets wrong: rows off the bottom, text under the buttons, drift between
# apps. A SPEC says what a screen contains; the kit decides where it goes.
#
#   {"title": "Kitchen",
#    "blocks": [
#      {"t": "kv",     "items": [{"k": "temp", "v": "21C"}]},
#      {"t": "list",   "items": ["Milk", "Bread"]},
#      {"t": "bars",   "items": [{"label": "cpu", "val": 42}]},
#      {"t": "text",   "text": "all quiet"},
#      {"t": "image",  "url": "/mesh/ui/img/x.v565"},
#      {"t": "grid",   "items": [{"text": "Run", "action": "cap:mesh.sysinfo"}]}],
#    "actions": [{"text": "Refresh", "action": "self"}]}
#
# Actions are semantic, so a spec never has to know Vera's internals:
#   cap:<name>[?k=v&...]  run a capability      app:<id>  open an app
#   panel:<id>            open that panel's pad  self     rebuild this screen
#   nav:home|status|pad   built-in navigation

UI_BLOCKS = ("text", "kv", "list", "bars", "grid", "image", "rule", "space")


def _spec_error(msg: str, where: str = "") -> dict:
    return {"ok": False, "error": (f"{where}: " if where else "") + msg}


def validate_ui_spec(spec) -> dict:
    """Check a spec before anything is pushed. Returns {ok} or {ok:False,error},
    naming the offending block — a screen that silently renders half of what was
    asked for is worse than a refusal."""
    if not isinstance(spec, dict):
        return _spec_error("spec must be an object")
    blocks = spec.get("blocks")
    if blocks is not None and not isinstance(blocks, list):
        return _spec_error("blocks must be a list")
    for i, b in enumerate(blocks or []):
        where = f"blocks[{i}]"
        if not isinstance(b, dict):
            return _spec_error("each block must be an object", where)
        t = b.get("t")
        if t not in UI_BLOCKS:
            return _spec_error(f"unknown block type {t!r}; expected one of "
                               + ", ".join(UI_BLOCKS), where)
        if t in ("kv", "list", "bars", "grid") and not isinstance(b.get("items"), list):
            return _spec_error(f"{t} needs an items list", where)
        if t == "bars":
            for it in b.get("items") or []:
                if not isinstance(it, dict) or "val" not in it:
                    return _spec_error("each bar needs {label, val}", where)
        if t == "image" and not b.get("url"):
            return _spec_error("image needs a url", where)
    acts = spec.get("actions")
    if acts is not None and not isinstance(acts, list):
        return _spec_error("actions must be a list")
    if isinstance(acts, list) and len(acts) > 2:
        return _spec_error("at most 2 action-bar buttons fit; put the rest in a grid")
    return {"ok": True}


def compile_ui_spec(spec: dict, screen_id: str = "") -> dict:
    """Spec -> a screen the firmware can render, laid out by the kit."""
    flow = UiFlow()
    grid_items = []
    images = []
    for b in spec.get("blocks") or []:
        t = b.get("t")
        if t == "text":
            flow.text(b.get("text", ""), int(b.get("size", UI_BODY)),
                      b.get("colour", "fg"))
        elif t == "kv":
            for it in b.get("items") or []:
                flow.kv(it.get("k", ""), it.get("v", ""), it.get("colour", "good"))
        elif t == "list":
            for it in b.get("items") or []:
                if isinstance(it, dict):
                    flow.kv(it.get("text", it.get("k", "")), it.get("value", it.get("v", "")),
                            it.get("colour", "good"))
                else:
                    flow.text(it)
        elif t == "bars":
            for it in b.get("items") or []:
                flow.bar(it.get("label", ""), it.get("val", 0), it.get("colour", "good"))
        elif t == "rule":
            if flow.room > 10:
                flow.widgets.append(ui_rule(flow.y))
                flow.y += 10
        elif t == "space":
            flow.y += int(b.get("h", UI_GUTTER))
        elif t == "image":
            images.append(b)
        elif t == "grid":
            grid_items.extend(b.get("items") or [])

    widgets = flow.done()

    # Images are drawn where the flow got to, so text above them still shows.
    for b in images:
        widgets.append({"t": "image", "x": int(b.get("x", UI_MARGIN)),
                        "y": int(b.get("y", flow.y)), "url": b["url"]})

    # A grid of tappable buttons fills whatever body space is left.
    if grid_items:
        cols = max(1, int(spec.get("cols", 2)))
        bw = (UI_W - 2 * UI_MARGIN - (cols - 1) * UI_GUTTER) // cols
        y = flow.y + UI_GUTTER
        for i, it in enumerate(grid_items):
            r, c = divmod(i, cols)
            by = y + r * (UI_BTN_H + UI_GUTTER)
            if by + UI_BTN_H > UI_BAR_Y - UI_GUTTER:
                break                                    # never under the action bar
            widgets.append(ui_button(UI_MARGIN + c * (bw + UI_GUTTER), by,
                                     it.get("text", "?"), it.get("action", ""),
                                     bg=it.get("bg", "accent"), w=bw))

    actions = list(spec.get("actions") or [])
    for a in actions:
        if a.get("action") == "self" and screen_id:
            a["action"] = "screen:" + screen_id
    if not actions:
        actions = [{"text": "Apps", "action": "app:launcher"}]
    elif len(actions) < 2:
        actions.append({"text": "Apps", "action": "app:launcher"})
    return ui_screen(spec.get("title", ""), widgets, actions,
                     spec.get("bg", "bg"))


# Saved screens, so a built UI can be re-opened, refreshed and shared.
_SCREENS_PATH = os.path.join(_FW_DIR, "screens.json")


def _load_screens() -> Dict[str, dict]:
    try:
        with open(_SCREENS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {s["id"]: s for s in raw if isinstance(s, dict) and s.get("id")}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("saved screens unreadable: %s", e)
        return {}


def _save_screens(screens: Dict[str, dict]) -> None:
    tmp = _SCREENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(screens.values()), f, indent=2)
    os.replace(tmp, _SCREENS_PATH)


async def _run_spec_action(node_id: str, action: str) -> bool:
    """Semantic actions from a built screen. Returns True if handled."""
    if action.startswith("screen:"):
        sid = action.split(":", 1)[1]
        saved = _load_screens().get(sid)
        if saved:
            await _push(node_id, compile_ui_spec(saved["spec"], sid))
        return True
    if action.startswith("panel:"):
        await cap_mesh_app_follow(node_id=node_id, panel=action.split(":", 1)[1],
                                  force=True)
        return True
    if action.startswith("cap:"):
        rest = action.split(":", 1)[1]
        cap, _, qs = rest.partition("?")
        args = {}
        for pair in qs.split("&") if qs else []:
            k, _, v = pair.partition("=")
            if k:
                args[k] = v
        if "node_id" not in args and cap.startswith("mesh."):
            args["node_id"] = node_id            # mesh caps act on the tapping node
        res = await _call_cap(cap, **args)
        await _show_result(node_id, cap.split(".")[-1], cap, res)
        return True
    return False


@capability(
    "mesh.ui.build", http_method="POST", http_path="/mesh/ui/build",
    http_tags=["mesh", "ui"], memory="on",
    description="Build a screen for a display node from a declarative spec — no pixel coordinates, "
                "so a person or an LLM can compose one safely. Blocks: text, kv (label/value rows), "
                "list, bars (0-100), grid (tappable buttons), image, rule, space. Actions are "
                "semantic: 'cap:<name>?k=v' runs a capability and shows the result, 'app:<id>' opens "
                "an app, 'panel:<id>' opens that panel's pad, 'self' rebuilds the screen. Layout is "
                "done by the node UI kit, so content cannot overflow or collide with the action bar; "
                "anything that does not fit is reported as '+N more'. Input: node_id (str — omit to "
                "just validate/preview), spec (JSON!), save_as (str — keep it, re-openable as "
                "'screen:<id>'), preview (bool=False — return the compiled widgets, send nothing). "
                "Output: {ok, widgets, job_id} or {ok:false, error}.",
)
async def cap_mesh_ui_build(node_id: str = "", spec=None, save_as: str = "",
                            preview: bool = False, trace_id=None) -> dict:
    spec = json.loads(spec) if isinstance(spec, str) else spec
    v = validate_ui_spec(spec)
    if not v.get("ok"):
        return v
    sid = re.sub(r"[^A-Za-z0-9_.-]", "-", save_as) if save_as else ""
    if sid:
        screens = _load_screens()
        screens[sid] = {"id": sid, "spec": spec}
        try:
            _save_screens(screens)
        except Exception as e:
            return _spec_error(f"could not save: {e}")
    screen = compile_ui_spec(spec, sid)
    if preview or not node_id:
        return {"ok": True, "preview": True, "id": sid, "screen": screen,
                "widgets": len(screen["widgets"])}
    r = await _push(node_id, screen)
    _NODE_APP[node_id] = "screen:" + sid if sid else "screen"
    return {"ok": bool(isinstance(r, dict) and not r.get("error")), "id": sid,
            "widgets": len(screen["widgets"]), "job_id": (r or {}).get("job_id")}


@capability(
    "mesh.ui.screens", http_method="GET", http_path="/mesh/ui/screens",
    http_tags=["mesh", "ui"], memory="off", silent=True,
    description="List screens saved by mesh.ui.build. Each can be shown again with "
                "mesh.ui.build (spec) or tapped to via 'screen:<id>'. Output: {screens:[{id,title}], count}.",
)
async def cap_mesh_ui_screens(trace_id=None) -> dict:
    saved = _load_screens()
    return {"screens": [{"id": s["id"], "title": (s.get("spec") or {}).get("title", "")}
                        for s in saved.values()], "count": len(saved)}


# ════════════════════════════════════════════════════════════════════════════
# Pads derived from the panel registry
# ════════════════════════════════════════════════════════════════════════════
# Hand-written pads only ever exposed the handful of buttons someone remembered
# to add, and drifted the moment a panel gained a capability. Every panel already
# declares `ui_caps` via register_ui, so a pad can be DERIVED from it: whatever a
# panel can do, its pad offers, paged, for every panel — no per-panel code.
#
# Hand-authored pads in PANEL_PADS still win where a curated set reads better;
# derivation fills in everything else.

# Capabilities that make no sense as a button on a wall panel.
_PAD_SKIP = re.compile(
    r"\.(panel|html|stream|sse|ws|upload|download|delete|purge|drop|wipe|reset|"
    r"clear|remove|destroy)$|^(ui|debug)\.")

# Anything that changes the world asks before it runs.
_PAD_CONFIRM = re.compile(
    r"\.(run|start|stop|cancel|deploy|restart|kill|scan|sync|send|flash|build|"
    r"promote|apply|commit|install|provision)$")


def _pad_label(cap: str) -> str:
    """'markets.watchlist.list' -> 'Watchlist list' — readable at a glance."""
    parts = cap.split(".")[1:] or cap.split(".")
    return " ".join(parts).replace("_", " ").capitalize()


def _panel_registry() -> dict:
    try:
        from Vera.vera.capability_orchestration import UI_PANELS
        return UI_PANELS or {}
    except Exception as e:
        log.debug("panel registry unavailable: %s", e)
        return {}


def derive_panel_pad(panel_id: str) -> dict:
    """Build a pad from what the panel says it uses. Read-only capabilities
    first: those are safe to tap and are what a glanceable pad is mostly for."""
    panel = _panel_registry().get(panel_id) or {}
    caps = [c for c in (panel.get("ui_caps") or []) if isinstance(c, str)]
    seen, buttons = set(), []
    for cap in caps:
        if cap in seen or _PAD_SKIP.search(cap) or not _cap_exists(cap):
            continue
        seen.add(cap)
        buttons.append({"label": _pad_label(cap), "cap": cap,
                        "self": cap.startswith("mesh."),
                        "confirm": bool(_PAD_CONFIRM.search(cap))})
    buttons.sort(key=lambda b: (b["confirm"], b["label"]))
    return {"label": panel.get("label") or panel_id, "buttons": buttons,
            "derived": True}


def pad_for_panel(panel_id: str) -> dict:
    """Curated pad if one exists, otherwise derive it from the registry."""
    if panel_id == "scans":
        return SCAN_PAD
    if panel_id == "intake":
        return INTAKE_PAD
    curated = PANEL_PADS.get(panel_id)
    derived = derive_panel_pad(panel_id)
    if curated and derived.get("buttons"):
        # Curated buttons first (someone chose them), then the rest of the
        # panel's capabilities so nothing is hidden.
        have = {b["cap"] for b in curated.get("buttons") or []}
        merged = list(curated["buttons"]) + [b for b in derived["buttons"]
                                             if b["cap"] not in have]
        return {"label": curated.get("label") or derived["label"], "buttons": merged}
    if curated:
        return curated
    if derived.get("buttons"):
        return derived
    return _load_custom_pads().get(panel_id) or {}


@capability(
    "mesh.app.pads", http_method="GET", http_path="/mesh/app/pads",
    http_tags=["mesh", "ui"], memory="off", silent=True,
    description="List every macro pad available, including ones derived automatically from each "
                "Vera panel's registered capabilities. Input: panel (str — inspect just this one). "
                "Output: {pads:[{id,label,buttons,derived}], count}.",
)
async def cap_mesh_app_pads(panel: str = "", trace_id=None) -> dict:
    ids = [panel] if panel else sorted(
        set(PANEL_PADS) | set(_panel_registry()) | set(_load_custom_pads())
        | {"scans", "intake"})
    pads = []
    for pid in ids:
        pad = pad_for_panel(pid)
        if pad.get("buttons"):
            pads.append({"id": pid, "label": pad.get("label") or pid,
                         "buttons": len(pad["buttons"]),
                         "derived": bool(pad.get("derived")),
                         "caps": [b["cap"] for b in pad["buttons"]][:24]})
    return {"pads": pads, "count": len(pads)}


# ── Visual language ─────────────────────────────────────────────────────────
# Plain text on black reads as a debug console, not a product. RGB565 gives 65k
# colours, so the panel can have real surfaces, depth and hierarchy — the earlier
# screens just weren't using any of it. Everything here is still made of the five
# primitives the firmware draws (label/rect/hline/button/bar), so nothing new is
# needed on the device.

def _rgb(r, g, b) -> int:
    """8-bit RGB -> RGB565, so the palette can be written in normal colours."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


UI_PALETTE = {
    "bg":        _rgb(11, 15, 24),     # near-black with a blue cast, not pure 0
    "surface":   _rgb(22, 29, 43),     # cards sit above the background
    "surface2":  _rgb(31, 41, 59),     # zebra banding / secondary fills
    "border":    _rgb(51, 65, 92),
    "header":    _rgb(30, 58, 95),     # title band
    "accent":    _rgb(56, 132, 255),
    "accentDim": _rgb(30, 64, 128),
    "text":      _rgb(226, 232, 240),  # off-white is easier on the eye than #fff
    "textDim":   _rgb(148, 163, 184),
    "good":      _rgb(52, 211, 153),
    "warn":      _rgb(251, 191, 36),
    "bad":       _rgb(248, 113, 113),
    "info":      _rgb(56, 189, 248),
}

# Roles now resolve to the palette; the old names keep working.
UI_ROLE.update({
    "bg": UI_PALETTE["bg"], "fg": UI_PALETTE["text"], "muted": UI_PALETTE["textDim"],
    "title": UI_PALETTE["text"], "good": UI_PALETTE["good"], "warn": UI_PALETTE["warn"],
    "bad": UI_PALETTE["bad"], "accent": UI_PALETTE["accent"], "info": UI_PALETTE["info"],
    "surface": UI_PALETTE["surface"], "surface2": UI_PALETTE["surface2"],
    "border": UI_PALETTE["border"], "header": UI_PALETTE["header"],
    "accentDim": UI_PALETTE["accentDim"],
})

UI_HEADER_H = 40
UI_FOOTER_H = UI_BTN_H + 2 * 6


def ui_fill(x, y, w, h, colour):
    return {"t": "rect", "x": int(x), "y": int(y), "w": int(w), "h": int(h),
            "color": ui_colour(colour, "surface")}


def ui_frame(x, y, w, h, colour="border"):
    """An outline drawn as four thin fills — the firmware's rect is fill-only."""
    c = ui_colour(colour, "border")
    return [ui_fill(x, y, w, 1, c), ui_fill(x, y + h - 1, w, 1, c),
            ui_fill(x, y, 1, h, c), ui_fill(x + w - 1, y, 1, h, c)]


def ui_right(text, right_edge, size=UI_BODY) -> int:
    """x for right-aligned text. Numbers in a column only read as a column when
    their right edges line up."""
    return max(UI_MARGIN, int(right_edge) - ui_text_w(text, size))


def ui_header(title, subtitle="", status_role=None):
    """A filled title band with an accent spine, and an optional status pip."""
    out = [ui_fill(0, 0, UI_W, UI_HEADER_H, "header"),
           ui_fill(0, 0, 4, UI_HEADER_H, "accent")]
    out.append(ui_label(UI_MARGIN + 6, 10, title, UI_BODY + 1, "fg"))
    if subtitle:
        out.append(ui_label(ui_right(subtitle, UI_W - UI_MARGIN - (14 if status_role else 0),
                                     UI_CAPTION), 16, subtitle, UI_CAPTION, "muted"))
    if status_role:
        out.append(ui_fill(UI_W - UI_MARGIN - 10, 15, 10, 10, status_role))
    return out


def ui_footer():
    return [ui_fill(0, UI_BAR_Y - 6, UI_W, UI_FOOTER_H, "surface")]


def ui_card(x, y, w, h, fill="surface", border="border"):
    return [ui_fill(x, y, w, h, fill)] + ui_frame(x, y, w, h, border)


def ui_pill(x, y, text, role="accent"):
    """A filled chip — for statuses and counts, where a bare word looks unfinished."""
    w = ui_text_w(text, UI_CAPTION) + 12
    return [ui_fill(x, y, w, 16, role),
            ui_label(x + 6, y + 4, text, UI_CAPTION, "bg")], w


def ui_metric(x, y, w, label, value, role="good"):
    """A stat tile: big value, small caption. Reads at a glance from across a room."""
    out = ui_card(x, y, w, 56)
    out.append(ui_label(x + 8, y + 8, label, UI_CAPTION, "muted", width=w - 16))
    out.append(ui_label(x + 8, y + 22, value, UI_BODY + 1, role, width=w - 16))
    return out


# Re-composed screen and rows, now that the panel has a visual language. The old
# signatures are kept so every existing app picks the styling up for free.

UI_BODY_Y_THEMED = UI_HEADER_H + 10          # content clears the title band


def ui_screen(title, blocks, actions=None, bg="bg", subtitle="", status_role=None):
    """Title band, content, buttons on a footer surface."""
    widgets = ui_header(title, subtitle, status_role) if title else []
    widgets.extend(blocks or [])
    widgets.extend(ui_footer())
    widgets.extend(ui_action_bar(actions or [{"text": "Apps", "action": "app:launcher"}]))
    return {"title": "", "bg": ui_colour(bg, "bg"), "widgets": widgets}


def ui_button(x, y, text, action, colour="fg", bg="accent",
              w=None, h=None, size=UI_BODY):
    """Buttons get a border so they read as controls, not coloured rectangles."""
    bw, bh = int(w or UI_BTN_W), int(h or UI_BTN_H)
    base = {"t": "button", "x": int(x), "y": int(y), "w": bw, "h": bh,
            "text": ui_clip(text, size, bw - 12), "action": action,
            "color": ui_colour(colour), "bg": ui_colour(bg, "accent"),
            "size": int(size)}
    return base


class UiFlow(UiFlow):                          # noqa: F811 — themed subclass
    """Same API, but rows sit on alternating surfaces and values right-align, so
    a list reads as a table rather than a wall of text."""

    def __init__(self, y=None):
        super().__init__(UI_BODY_Y_THEMED if y is None else y)
        self._row = 0

    def kv(self, key, value, colour="good"):
        if self._fits(UI_ROW_H):
            if self._row % 2 == 0:
                self.widgets.append(ui_fill(UI_MARGIN, self.y - 4,
                                            UI_W - 2 * UI_MARGIN, UI_ROW_H, "surface"))
            self.widgets.append(ui_label(UI_MARGIN + 8, self.y, key, UI_BODY, "fg",
                                         width=UI_W // 2))
            v = ui_clip(value, UI_BODY, UI_W // 2 - 30)
            self.widgets.append(ui_label(ui_right(v, UI_W - UI_MARGIN - 8), self.y,
                                         v, UI_BODY, colour))
            self.y += UI_ROW_H
            self._row += 1
        return self

    def metrics(self, items):
        """A row of stat tiles — the strongest way to show a few key numbers."""
        n = max(1, min(3, len(items)))
        if not self._fits(64):
            return self
        w = (UI_W - 2 * UI_MARGIN - (n - 1) * UI_GUTTER) // n
        for i, it in enumerate(items[:n]):
            self.widgets.extend(ui_metric(UI_MARGIN + i * (w + UI_GUTTER), self.y, w,
                                          it.get("label", ""), str(it.get("value", "")),
                                          it.get("colour", "good")))
        self.y += 64
        return self

    def bar(self, label, val, colour="good"):
        h = ui_text_h(UI_CAPTION) + 6 + 14 + 10
        if self._fits(h):
            v = max(0, min(100, int(val)))
            self.widgets.append(ui_label(UI_MARGIN, self.y, label, UI_CAPTION, "muted"))
            pct = "%d%%" % v
            self.widgets.append(ui_label(ui_right(pct, UI_W - UI_MARGIN, UI_CAPTION),
                                         self.y, pct, UI_CAPTION, colour))
            self.y += ui_text_h(UI_CAPTION) + 6
            track_w = UI_W - 2 * UI_MARGIN
            self.widgets.append(ui_fill(UI_MARGIN, self.y, track_w, 14, "surface2"))
            self.widgets.append(ui_fill(UI_MARGIN, self.y, max(2, track_w * v // 100),
                                        14, colour))
            self.y += 14 + 10
        return self


# ── Apps rebuilt on the kit ─────────────────────────────────────────────────
# These were hand-placing widgets, which is why they looked like a debug dump.
# Rebuilt through the kit they pick up the surfaces, banding and alignment, and
# they can no longer overflow.

async def _app_dashboard_build(spec: dict, node_id: str):          # noqa: F811
    rows = spec.get("rows") or []
    known = [r for r in rows if _cap_exists(r.get("cap", ""))]
    missing = [r for r in rows if r not in known]

    async def _one(r):
        try:
            res = await _call_cap(r["cap"], **(r.get("args") or {}))
            if r.get("json") and isinstance(res, dict) and isinstance(res.get("body"), str):
                try:
                    return json.loads(res["body"])
                except Exception:
                    return {"error": "bad json"}
            return res
        except Exception as e:
            return {"error": str(e)}

    results = await asyncio.gather(*[_one(r) for r in known]) if known else []

    flow = UiFlow()
    # The first few readings become stat tiles — a wall display is read from a
    # distance, and three big numbers carry further than eight small rows.
    tiles, rest = [], []
    for r, res in zip(known, results):
        failed = isinstance(res, dict) and bool(res.get("error"))
        val = "err" if failed else (_fmt(_dig(res, r.get("pick", "")))
                                    if _dig(res, r.get("pick", "")) is not None else "-")
        item = {"label": r["label"], "value": val,
                "colour": "bad" if failed else "good"}
        (tiles if len(tiles) < 3 else rest).append(item)
    if tiles:
        flow.metrics(tiles)
    for it in rest:
        flow.kv(it["label"], it["value"], it["colour"])
    for r in missing:
        flow.note("%s: no capability %s" % (r.get("label"), r.get("cap")))
    if not known and not missing:
        flow.text("nothing to show", UI_BODY, "muted")

    return (ui_screen(spec.get("label") or "Info", flow.done(),
                      [{"text": "Refresh", "action": "app:" + spec["_id"]},
                       {"text": "Apps", "action": "app:launcher"}],
                      subtitle=now_iso()[11:16]), {})


async def _app_list_build(spec: dict, node_id: str):               # noqa: F811
    flow = UiFlow()
    status_role = "good"
    if not _cap_exists(spec["cap"]):
        flow.note("no capability " + spec["cap"])
        status_role = "warn"
    else:
        try:
            res = await _call_cap(spec["cap"], **(spec.get("args") or {}))
        except Exception as e:
            res = {"error": str(e)}
        if isinstance(res, dict) and res.get("error"):
            flow.text(str(res["error"]), UI_BODY, "bad")
            status_role = "bad"
        else:
            items = _pick_list(res, spec.get("path", ""))
            for it in items:
                left = str(_field(it, spec["left"], "?"))
                right = str(_field(it, spec.get("right") or [], ""))
                flow.kv(left, right)
            if not items:
                flow.text("nothing to show", UI_BODY, "muted")
                status_role = "warn"
    return (ui_screen(spec.get("label") or "List", flow.done(),
                      [{"text": "Refresh", "action": "app:" + spec["_id"]},
                       {"text": "Apps", "action": "app:launcher"}],
                      status_role=status_role), {})


def build_pad_screen(node_id: str, title: str, buttons: List[dict], page: int = 0):  # noqa: F811
    """Pads as a real grid of controls on the themed surface."""
    pages = max(1, (len(buttons) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _PAGE_SIZE
    widgets, mapping = [], {}
    cols = 2
    bw = (UI_W - 2 * UI_MARGIN - UI_GUTTER) // cols
    bh = 44
    y0 = UI_HEADER_H + 10
    for i, b in enumerate(buttons[start:start + _PAGE_SIZE], start=start):
        action = "macro:%d" % i
        args = dict(b.get("args") or {})
        if b.get("self"):
            args["node_id"] = node_id
        r, c = divmod(i - start, cols)
        by = y0 + r * (bh + UI_GUTTER)
        if by + bh > UI_BAR_Y - UI_GUTTER:
            break
        widgets.append(ui_button(UI_MARGIN + c * (bw + UI_GUTTER), by,
                                 b.get("label") or b.get("cap") or "?", action,
                                 bg="bad" if b.get("confirm") else "accentDim",
                                 w=bw, h=bh))
        mapping[action] = {"cap": b.get("cap"), "args": args,
                           "confirm": bool(b.get("confirm")),
                           "label": b.get("label") or b.get("cap")}
    _PAD_CTX[node_id] = {"title": title, "buttons": buttons, "page": page}
    actions = ([{"text": "< page %d/%d" % (page + 1, pages), "action": "page:%d" % (page - 1)},
                {"text": "page %d/%d >" % (page + 2 if page + 1 < pages else 1, pages),
                 "action": "page:%d" % (page + 1)}]
               if pages > 1 else [{"text": "Apps", "action": "app:launcher"}])
    return (ui_screen(title, widgets, actions,
                      subtitle="%d actions" % len(buttons)), mapping)


async def _show_result(node_id: str, label: str, cap: str, res) -> None:   # noqa: F811
    lines = summarise_result(cap, res)
    failed = isinstance(res, dict) and bool(res.get("error"))
    flow = UiFlow()
    flow.note(cap)
    for ln in lines[:6]:
        flow.text(str(ln), UI_BODY, "bad" if failed else "good")
    await _push(node_id, ui_screen(("Failed" if failed else "Done"), flow.done(),
                                   [{"text": "Back", "action": "nav:pad"},
                                    {"text": "Apps", "action": "app:launcher"}],
                                   subtitle=(label or cap)[:22],
                                   status_role="bad" if failed else "good"))


def _app_launcher(node_id: str, ctx: dict):                        # noqa: F811
    """A grid of apps. Paged via the same context the pads use, so a long app
    list stays reachable instead of running off the bottom."""
    apps = [(aid, a["label"]) for aid, a in sorted(APPS.items()) if aid != "launcher"]
    page = int((ctx or {}).get("page", 0))
    per, cols = 6, 2
    pages = max(1, (len(apps) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    bw = (UI_W - 2 * UI_MARGIN - UI_GUTTER) // cols
    bh, y0 = 44, UI_HEADER_H + 10
    widgets = []
    for i, (aid, label) in enumerate(apps[page * per:(page + 1) * per]):
        r, c = divmod(i, cols)
        widgets.append(ui_button(UI_MARGIN + c * (bw + UI_GUTTER), y0 + r * (bh + UI_GUTTER),
                                 label, "app:" + aid, bg="accentDim", w=bw, h=bh))
    actions = ([{"text": "< prev", "action": "applist:%d" % (page - 1)},
                {"text": "next >", "action": "applist:%d" % (page + 1)}]
               if pages > 1 else [{"text": "Status", "action": "nav:status"}])
    return (ui_screen("Apps", widgets, actions,
                      subtitle="%d apps  %d/%d" % (len(apps), page + 1, pages)), {})


# ════════════════════════════════════════════════════════════════════════════
# Dashboard widget -> node screen
# ════════════════════════════════════════════════════════════════════════════
# A dashboard widget is HTML with its own JS and refresh loop; none of that can
# run on the node. Screenshotting it would give a dead picture. So a widget is
# mapped by TYPE onto the UI-kit spec instead: the node re-renders it natively,
# stays interactive, and refreshes itself from the same capability the widget
# uses. What travels is the widget's MEANING, not its pixels.
#
# A widget declares its type and where its data comes from; anything unmapped is
# reported rather than approximated, because a wrong-but-plausible screen is
# worse than an honest refusal.

WIDGET_KINDS = {
    # kind        -> how it becomes a spec
    "metric":  "kv",        # one or more label/value readings
    "stat":    "kv",
    "list":    "list",      # rows of things
    "table":   "list",
    "gauge":   "bars",      # 0-100 measures
    "progress": "bars",
    "chart":   "bars",      # series -> the latest value as a bar; no plotting here
    "actions": "grid",      # buttons that run capabilities
    "buttons": "grid",
    "text":    "text",
    "status":  "kv",
    "image":   "image",
}


def widget_to_spec(widget: dict, data=None) -> dict:
    """Map one dashboard widget onto a UI-kit spec.

    `widget` is {id, kind, title, cap, args, path, left, right, items, url}.
    `data` is an already-fetched capability result, when the caller has one.
    """
    kind = str(widget.get("kind") or widget.get("type") or "").lower()
    block = WIDGET_KINDS.get(kind)
    if not block:
        raise ValueError(
            "widget kind %r has no panel equivalent; supported: %s"
            % (kind, ", ".join(sorted(set(WIDGET_KINDS)))))

    title = widget.get("title") or widget.get("label") or kind
    spec = {"title": ui_clip(title, UI_BODY + 1), "blocks": []}

    if block == "image":
        spec["blocks"].append({"t": "image", "url": widget.get("url", "")})
    elif block == "grid":
        items = []
        for it in widget.get("items") or []:
            cap = it.get("cap") or it.get("action") or ""
            items.append({"text": it.get("label") or it.get("text") or cap,
                          "action": cap if ":" in cap else ("cap:" + cap)})
        spec["blocks"].append({"t": "grid", "items": items})
    elif block == "text":
        spec["blocks"].append({"t": "text", "text": str(
            widget.get("text") or _dig(data, widget.get("path", "")) or "")})
    elif block == "bars":
        items = []
        for it in _pick_list(data, widget.get("path", "")) or widget.get("items") or []:
            if isinstance(it, dict):
                items.append({"label": str(_field(it, widget.get("left") or
                                                  ["label", "name", "_key"], "")),
                              "val": _num(_field(it, widget.get("right") or
                                                 ["value", "val", "pct"], 0))})
        spec["blocks"].append({"t": "bars", "items": items[:5]})
    elif block == "list":
        rows = []
        for it in _pick_list(data, widget.get("path", "")) or widget.get("items") or []:
            if isinstance(it, dict):
                rows.append({"text": str(_field(it, widget.get("left") or
                                                ["title", "name", "label", "_key"], "?")),
                             "value": str(_field(it, widget.get("right") or
                                                 ["value", "status", "count"], ""))})
            else:
                rows.append({"text": str(it), "value": ""})
        spec["blocks"].append({"t": "list", "items": rows[:UI_ROWS]})
    else:                                             # kv
        items = []
        for f in widget.get("fields") or [{"label": title,
                                           "path": widget.get("path", "")}]:
            v = _dig(data, f.get("path", ""))
            items.append({"k": f.get("label", ""),
                          "v": _fmt(v) if v is not None else "-"})
        spec["blocks"].append({"t": "kv", "items": items})

    # Refresh re-runs the widget's own capability, so the node stays live rather
    # than showing a frozen copy of whatever the browser had.
    if widget.get("cap"):
        spec["actions"] = [{"text": "Refresh", "action": "self"}]
    return spec


@capability(
    "mesh.ui.widget", http_method="POST", http_path="/mesh/ui/widget",
    http_tags=["mesh", "ui"], memory="on",
    description="Send a Vera dashboard widget to a display node. The widget is mapped BY TYPE onto "
                "a native screen (metric/stat -> value rows, list/table -> rows, gauge/progress/"
                "chart -> bars, actions -> tappable buttons, text, image) — not screenshotted — so "
                "it stays interactive and refreshes itself from the same capability the widget "
                "uses. An unmapped widget kind is refused rather than approximated. Input: node_id "
                "(str!), widget (JSON! — {kind,title,cap,args,path,left,right,fields,items}), "
                "save_as (str — keep it as a re-openable screen). Output: {ok, kind, spec, job_id}.",
)
async def cap_mesh_ui_widget(node_id: str = "", widget=None, save_as: str = "",
                             trace_id=None) -> dict:
    widget = json.loads(widget) if isinstance(widget, str) else widget
    if not node_id or not isinstance(widget, dict):
        return {"error": "node_id and a widget object are required"}

    data = None
    cap = widget.get("cap")
    if cap:
        if not _cap_exists(cap):
            return {"error": f"widget capability {cap!r} is not registered here"}
        try:
            data = await _call_cap(cap, **(widget.get("args") or {}))
        except Exception as e:
            return {"error": f"{cap} failed: {e}"}
        if isinstance(data, dict) and data.get("error"):
            return {"error": f"{cap}: {data['error']}"}

    try:
        spec = widget_to_spec(widget, data)
    except ValueError as e:
        return {"error": str(e)}

    return await cap_mesh_ui_build(node_id=node_id, spec=spec,
                                   save_as=save_as or widget.get("id", ""))


@capability(
    "mesh.ui.widget.kinds", http_method="GET", http_path="/mesh/ui/widget/kinds",
    http_tags=["mesh", "ui"], memory="off", silent=True,
    description="Which dashboard widget kinds can be sent to a display node, and the panel block "
                "each becomes. Output: {kinds:{kind:block}, count}.",
)
async def cap_mesh_ui_widget_kinds(trace_id=None) -> dict:
    return {"kinds": dict(WIDGET_KINDS), "count": len(WIDGET_KINDS)}


# ── Sprite Studio, mapped to its ACTUAL record shape ────────────────────────
# spritegen.list returns {"characters": [...]} keyed by char_id, and the frame
# images live at urls.frames[<animation>] (a list of paths) with a rendered sheet
# and gif at urls.sheets[<animation>]. `animations[a].frames` is a COUNT, not
# images — reading that as a list is why nothing ever appeared.
#
# A character that has been DEFINED but not generated has empty urls. Those are
# listed with ready=False rather than hidden, because "no characters" and "no
# frames generated yet" are different problems and the second is the common one.

def _sprite_anim_frames(rec: dict, animation: str = ""):
    """(frames, sheet, chosen_animation) from a character record."""
    urls = rec.get("urls") or {}
    frames_by_anim = urls.get("frames") or {}
    sheets_by_anim = urls.get("sheets") or {}
    names = [a for a in (rec.get("animations") or {})] or list(frames_by_anim)
    chosen = animation or next((a for a in names if frames_by_anim.get(a)),
                               names[0] if names else "")
    return (list(frames_by_anim.get(chosen) or []),
            sheets_by_anim.get(chosen) or {}, chosen)


def sprite_summary(rec: dict) -> dict:
    frames, sheet, anim = _sprite_anim_frames(rec)
    urls = rec.get("urls") or {}
    any_frames = any((urls.get("frames") or {}).values())
    any_sheet = any((s or {}).get("png") or (s or {}).get("gif")
                    for s in (urls.get("sheets") or {}).values())
    return {"id": rec.get("char_id") or "",
            "label": (rec.get("name") or "").strip() or (rec.get("char_id") or "")[:12],
            "animations": sorted((rec.get("animations") or {}).keys()),
            "ready": bool(any_frames or any_sheet),
            "frames": len(frames), "animation": anim}






# ── Charts ──────────────────────────────────────────────────────────────────
# A candle is a filled body plus a one-pixel wick, and the firmware draws filled
# rects — so real OHLC needs no new device code, just arithmetic. Everything is
# scaled to the actual high/low of the window, because a chart with a misleading
# baseline is worse than no chart.

def ui_candles(x, y, w, h, bars, up="good", down="bad", axis="textDim"):
    """bars: [{o,h,l,c}] oldest-first. Returns widgets."""
    rows = [b for b in bars if isinstance(b, dict) and b.get("h") is not None]
    if not rows:
        return [ui_label(x, y + h // 2, "no data", UI_CAPTION, "muted")]
    hi = max(_num(b.get("h")) for b in rows)
    lo = min(_num(b.get("l")) for b in rows)
    span = (hi - lo) or 1.0

    def _py(v):                                   # price -> y, inverted
        return int(y + h - ((_num(v) - lo) / span) * h)

    n = len(rows)
    slot = max(3, w // n)
    body_w = max(1, slot - 2)
    out = [ui_fill(x, y + h, w, 1, axis)]         # baseline
    for i, b in enumerate(rows[-(w // slot or 1):]):
        cx = x + i * slot
        o, c = _num(b.get("o")), _num(b.get("c"))
        role = up if c >= o else down
        # wick first so the body sits over it
        out.append(ui_fill(cx + body_w // 2, _py(b.get("h")), 1,
                           max(1, _py(b.get("l")) - _py(b.get("h"))), role))
        top, bot = _py(max(o, c)), _py(min(o, c))
        out.append(ui_fill(cx, top, body_w, max(1, bot - top), role))
    return out


def ui_sparkline(x, y, w, h, values, colour="accent"):
    """A column sparkline — cheaper than candles when you only have closes."""
    vals = [_num(v) for v in values if v is not None]
    if not vals:
        return [ui_label(x, y + h // 2, "no data", UI_CAPTION, "muted")]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    slot = max(1, w // n)
    out = []
    for i, v in enumerate(vals[-(w // slot or 1):]):
        bh = max(1, int(((v - lo) / span) * h))
        out.append(ui_fill(x + i * slot, y + h - bh, max(1, slot - 1), bh, colour))
    return out


def _ohlc_rows(res) -> List[dict]:
    """Normalise whatever markets.bars hands back into [{o,h,l,c}]."""
    rows = res if isinstance(res, list) else (
        (res or {}).get("bars") or (res or {}).get("candles")
        or (res or {}).get("data") or [])
    out = []
    for r in rows:
        if isinstance(r, dict):
            o = r.get("open", r.get("o")); h = r.get("high", r.get("h"))
            l = r.get("low", r.get("l")); c = r.get("close", r.get("c"))
            if None not in (o, h, l, c):
                out.append({"o": o, "h": h, "l": l, "c": c})
        elif isinstance(r, (list, tuple)) and len(r) >= 5:
            out.append({"o": r[1], "h": r[2], "l": r[3], "c": r[4]})   # ts,o,h,l,c
    return out


async def _app_candles(node_id: str, ctx: dict):
    """A real candle chart with the live quote and any firing alerts."""
    cfg = _load_dash_config()
    symbol = (ctx or {}).get("symbol") or cfg.get("symbol") or "BTC/USD"
    tf = cfg.get("timeframe") or "1h"
    flow_widgets, sub = [], symbol

    bars = []
    if _cap_exists("markets.bars"):
        try:
            res = await _call_cap("markets.bars", symbol=symbol, timeframe=tf, limit=60)
            bars = _ohlc_rows(res)
        except Exception as e:
            log.debug("candles %s: %s", symbol, e)

    top = UI_HEADER_H + 8
    ch = 140
    flow_widgets.extend(ui_candles(UI_MARGIN, top, UI_W - 2 * UI_MARGIN, ch, bars))
    if bars:
        last, first = bars[-1]["c"], bars[0]["o"]
        chg = ((_num(last) - _num(first)) / (_num(first) or 1)) * 100
        sub = "%s %s  %+.2f%%" % (symbol, tf, chg)

    flow = UiFlow(top + ch + 14)
    alerts = []
    if _cap_exists("markets.alerts.list"):
        try:
            ares = await _call_cap("markets.alerts.list")
            alerts = _pick_list(ares, "alerts")
        except Exception as e:
            log.debug("alerts: %s", e)
    if alerts:
        for a in alerts[:3]:
            flow.kv(str(_field(a, ["symbol", "title", "name"], "alert")),
                    str(_field(a, ["state", "status", "level"], "")), "warn")
    else:
        flow.note("no alerts firing")

    return (ui_screen("Markets", flow_widgets + flow.done(),
                      [{"text": "Refresh", "action": "app:info:candles"},
                       {"text": "Apps", "action": "app:launcher"}],
                      subtitle=sub,
                      status_role="warn" if alerts else "good"), {})


# ── A configurable home dashboard ───────────────────────────────────────────
# Time, agenda and markets on one screen — and which sections appear, in what
# order, is the user's choice rather than mine.

_DASH_CFG = os.path.join(_FW_DIR, "dash_config.json")
_DASH_DEFAULT = {"sections": ["clock", "agenda", "markets"],
                 "symbol": "BTC/USD", "timeframe": "1h", "rows": 3}


def _load_dash_config() -> dict:
    try:
        with open(_DASH_CFG, encoding="utf-8") as f:
            return dict(_DASH_DEFAULT, **json.load(f))
    except FileNotFoundError:
        return dict(_DASH_DEFAULT)
    except Exception as e:
        log.warning("dash config unreadable: %s", e)
        return dict(_DASH_DEFAULT)


async def _dash_clock(flow):
    import time as _t
    now = _t.localtime()
    flow.metrics([{"label": _t.strftime("%A", now), "value": _t.strftime("%H:%M", now)},
                  {"label": "date", "value": _t.strftime("%d %b", now), "colour": "info"}])


async def _dash_agenda(flow, cfg):
    if not _cap_exists("cal.events.list"):
        return flow.note("calendar unavailable")
    try:
        res = await _call_cap("cal.events.list")
    except Exception as e:
        return flow.note("calendar: %s" % e)
    events = _pick_list(res, "events")
    if not events:
        return flow.note("nothing scheduled")
    for e in events[:int(cfg.get("rows", 3))]:
        flow.kv(str(_field(e, ["title", "summary", "name"], "event")),
                str(_field(e, ["start", "start_time", "when", "time"], "")), "info")


async def _dash_markets(flow, cfg):
    if not _cap_exists("markets.watchlist.list"):
        return flow.note("markets unavailable")
    try:
        res = await _call_cap("markets.watchlist.list")
    except Exception as e:
        return flow.note("markets: %s" % e)
    for it in _pick_list(res, "watchlist")[:int(cfg.get("rows", 3))]:
        flow.kv(str(_field(it, ["symbol", "ticker", "pair", "_key"], "?")),
                str(_field(it, ["last", "price", "close"], "")))


async def _app_home(node_id: str, ctx: dict):
    cfg = _load_dash_config()
    flow = UiFlow()
    builders = {"clock": lambda: _dash_clock(flow),
                "agenda": lambda: _dash_agenda(flow, cfg),
                "markets": lambda: _dash_markets(flow, cfg)}
    for name in cfg.get("sections") or []:
        b = builders.get(name)
        if b:
            await b()
        else:
            flow.note("unknown section %r" % name)
    return (ui_screen("Vera", flow.done(),
                      [{"text": "Refresh", "action": "app:home"},
                       {"text": "Apps", "action": "app:launcher"}]), {})


@capability(
    "mesh.ui.dash.config", http_method="POST", http_path="/mesh/ui/dash/config",
    http_tags=["mesh", "ui"], memory="on",
    description="Configure the node home dashboard — which sections it shows and in what order. "
                "Sections: clock, agenda, markets. Input: sections (JSON list), symbol (str — the "
                "candle chart's instrument), timeframe (str), rows (int — rows per section). "
                "Omit everything to read the current config. Output: {ok, config}.",
)
async def cap_mesh_ui_dash_config(sections=None, symbol: str = "", timeframe: str = "",
                                  rows: int = 0, trace_id=None) -> dict:
    cfg = _load_dash_config()
    sections = json.loads(sections) if isinstance(sections, str) else sections
    if sections is not None:
        known = {"clock", "agenda", "markets"}
        bad = [s for s in sections if s not in known]
        if bad:
            return {"error": "unknown section(s): %s; known: %s"
                             % (", ".join(map(str, bad)), ", ".join(sorted(known)))}
        cfg["sections"] = list(sections)
    if symbol:
        cfg["symbol"] = symbol
    if timeframe:
        cfg["timeframe"] = timeframe
    if rows:
        cfg["rows"] = max(1, min(8, int(rows)))
    try:
        with open(_DASH_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        return {"error": "could not save: %s" % e}
    return {"ok": True, "config": cfg}


# ── Follow modes ────────────────────────────────────────────────────────────
# Mirroring the Vera tab you're on is useful for CONTROLS and useful for
# READOUTS, and which one you want differs by node and by moment. So the mode is
# a per-node choice — pad, dashboard, or off — and it can be changed from the
# device itself, because walking back to a browser to change what a wall panel
# shows defeats the point.

_FOLLOW_MODES = ("pad", "dash", "off")
_FOLLOW_CFG = os.path.join(_FW_DIR, "follow_modes.json")

# Panels whose readouts are worth mirroring, and the dashboard app to show.
PANEL_DASHBOARDS = {
    "markets": "info:candles", "mesh": "info:mesh", "workers": "info:system",
    "calendar": "info:agenda", "comms": "info:agenda", "business": "info:orders",
    "dream": "info:system", "netmap": "info:nodes",
}


def _load_follow() -> Dict[str, str]:
    try:
        with open(_FOLLOW_CFG, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if v in _FOLLOW_MODES}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("follow modes unreadable: %s", e)
        return {}


def _save_follow(modes: Dict[str, str]) -> None:
    tmp = _FOLLOW_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(modes, f, indent=2)
    os.replace(tmp, _FOLLOW_CFG)


def follow_mode(node_id: str) -> str:
    return _load_follow().get(node_id, "pad")


@capability(
    "mesh.app.follow.mode", http_method="POST", http_path="/mesh/app/follow/mode",
    http_tags=["mesh", "ui"], memory="on",
    description="Choose what a node mirrors as you move around Vera: 'pad' (that panel's controls), "
                "'dash' (that panel's readout, e.g. Markets becomes a candle chart), or 'off'. "
                "Per node, changeable from the device too. Omit mode to read it. "
                "Input: node_id (str!), mode (str — pad|dash|off). Output: {ok, node_id, mode, modes}.",
    schema={"properties": {"mode": {"enum": ["pad", "dash", "off"]}}},
)
async def cap_mesh_app_follow_mode(node_id: str = "", mode: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    modes = _load_follow()
    if mode:
        if mode not in _FOLLOW_MODES:
            return {"error": "mode must be one of %s" % ", ".join(_FOLLOW_MODES)}
        modes[node_id] = mode
        try:
            _save_follow(modes)
        except Exception as e:
            return {"error": "could not save: %s" % e}
    return {"ok": True, "node_id": node_id,
            "mode": modes.get(node_id, "pad"), "modes": modes,
            # Empty means the harness has never called follow for this node —
            # the binding is missing, not the device.
            "last_seen": _FOLLOW_SEEN.get(node_id, {})}




_FOLLOW_LAST: Dict[str, str] = {}       # node -> the panel it is mirroring
# Last follow request actually RECEIVED, so "is it the browser or the device?"
# is answerable without being at the machine.
_FOLLOW_SEEN: Dict[str, dict] = {}


def _app_modeswitch(node_id: str, ctx: dict):
    """On-device mode switch, so the panel can be repurposed where it hangs."""
    cur = follow_mode(node_id)
    panel = _FOLLOW_LAST.get(node_id, "")
    widgets, y = [], UI_HEADER_H + 10
    bw = (UI_W - 2 * UI_MARGIN - UI_GUTTER) // 2
    for i, (m, label) in enumerate((("pad", "Controls"), ("dash", "Readouts"),
                                    ("off", "Stay put"))):
        r, c = divmod(i, 2)
        widgets.append(ui_button(UI_MARGIN + c * (bw + UI_GUTTER),
                                 y + r * (44 + UI_GUTTER), label,
                                 "followmode:" + m,
                                 bg="accent" if m == cur else "accentDim",
                                 w=bw, h=44))
    flow = UiFlow(y + 2 * (44 + UI_GUTTER) + 6)
    flow.note("following: %s%s" % (cur, (" (%s)" % panel) if panel else ""))
    return (ui_screen("Follow mode", widgets + flow.done(),
                      [{"text": "Apps", "action": "app:launcher"},
                       {"text": "Status", "action": "nav:status"}],
                      subtitle=node_id[:20]), {})


# ════════════════════════════════════════════════════════════════════════════
# Pin map — what is wired where, and how to read it
# ════════════════════════════════════════════════════════════════════════════
# Pins were scattered across config.io.tft / .sd / .touch / .neopixel with no
# single view, so nothing could answer "is GPIO7 free?" or "why is the display
# broken?" — the answer is usually that two things claim the same pin.
#
# A pin map is a list of ASSIGNMENTS: {pin, role, device, profile}. A DEVICE
# PROFILE describes a kind of hardware once (which roles it needs, how its
# signal is read) so adding a sensor is data, not code. Conflicts are detected
# rather than silently overwritten, because a double-booked pin is the single
# most common cause of "it just doesn't work".

# Chip pin capabilities. What a pin CAN do is a property of the silicon, and
# assigning an input-only or flash pin is a mistake worth catching up front.
CHIP_PINS = {
    "esp32s3": {
        "gpio": list(range(0, 22)) + list(range(26, 49)),
        "adc": list(range(1, 11)),          # ADC1 — usable while Wi-Fi is on
        "adc2": list(range(11, 21)),        # unusable with Wi-Fi active
        # Believed input-only, but ESP32 variants differ and getting this wrong
        # sends people chasing a hardware problem that isn't there. Confirm with
        # mesh.pins.probe, which measures it on the actual silicon.
        "input_only": [46], "input_only_confirmed": False,
        "reserved": {0: "boot strap", 3: "JTAG strap", 45: "VDD_SPI strap",
                     46: "input only / strap",
                     19: "USB D-", 20: "USB D+",
                     **{p: "SPI flash / PSRAM" for p in range(26, 33)}},
    },
    "esp32": {
        "gpio": list(range(0, 40)),
        "adc": [32, 33, 34, 35, 36, 39],
        "adc2": [0, 2, 4, 12, 13, 14, 15, 25, 26, 27],
        "input_only": [34, 35, 36, 37, 38, 39],
        "reserved": {0: "boot strap", 2: "boot strap", 12: "boot strap",
                     **{p: "SPI flash" for p in range(6, 12)}},
    },
    "esp32c3": {
        "gpio": list(range(0, 22)),
        "adc": list(range(0, 6)), "adc2": [],
        "input_only": [],
        "reserved": {8: "boot strap", 9: "boot strap", 18: "USB D-", 19: "USB D+"},
    },
}

# Device profiles: the roles a kind of hardware needs, and how its signal is
# interpreted. `read` describes the filter/decoding so a profile can be added
# without touching firmware.
DEVICE_PROFILES = {
    "tft_ili9488_p8": {
        "label": "ILI9488 parallel TFT (8-bit)",
        "roles": ["rst", "cs", "dc", "wr", "rd",
                  "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"],
        "kind": "display", "read": None,
        "note": "d4/d5 on GPIO19/20 collide with USB-Serial-JTAG on the S3",
    },
    "touch_4wire": {
        "label": "4-wire resistive touch",
        "roles": ["xp", "ym", "yp", "xm"],
        "kind": "input",
        "read": {"type": "resistive_touch", "adc_roles": ["yp", "xm"],
                 "pressure": "adc_max - (z2 - z1)"},
        "note": "shares LCD lines on Uno shields; yp/xm must be ADC-capable",
    },
    "sd_spi": {"label": "SD card (SPI)", "kind": "storage",
               "roles": ["clk", "miso", "mosi", "cs"], "read": None},
    "neopixel": {"label": "WS2812 / NeoPixel", "kind": "output",
                 "roles": ["data"], "read": None},
    "led": {"label": "LED", "kind": "output", "roles": ["pin"], "read": None},
    "relay": {"label": "Relay", "kind": "output", "roles": ["pin"], "read": None},
    "ds18b20": {"label": "DS18B20 temperature", "kind": "sensor",
                "roles": ["data"],
                "read": {"type": "onewire", "unit": "C", "poll_s": 30}},
    "dht22": {"label": "DHT22 temp/humidity", "kind": "sensor",
              "roles": ["data"],
              "read": {"type": "dht", "unit": "C,%", "poll_s": 30}},
    "analog_sensor": {"label": "Generic analogue sensor", "kind": "sensor",
                      "roles": ["adc"],
                      "read": {"type": "adc", "scale": 1.0, "offset": 0.0,
                               "smooth": 4, "poll_s": 10}},
    "digital_input": {"label": "Digital input / button", "kind": "input",
                      "roles": ["pin"],
                      "read": {"type": "digital", "pull": "up", "debounce_ms": 40}},
    "i2c_bus": {"label": "I2C bus", "kind": "bus", "roles": ["sda", "scl"],
                "read": {"type": "i2c", "freq": 100000}},
    "pwm_output": {"label": "PWM output", "kind": "output", "roles": ["pin"],
                   "read": {"type": "pwm", "freq": 1000}},
}


_PIN_PROBED: Dict[str, dict] = {}      # node_id -> {pin: drivable}


def note_pin_probe(node_id: str, results: List[dict]) -> None:
    """Record a measured drivability result so it overrides the static table."""
    if not node_id:
        return
    seen = _PIN_PROBED.setdefault(node_id, {})
    for r in results or []:
        if isinstance(r, dict) and isinstance(r.get("pin"), int) and "drivable" in r:
            seen[int(r["pin"])] = bool(r["drivable"])


def pin_capabilities(chip: str, node_id: str = "") -> dict:
    caps = dict(CHIP_PINS.get((chip or "").lower(), CHIP_PINS["esp32s3"]))
    probed = _PIN_PROBED.get(node_id or "", {})
    if probed:
        # Measured wins. A pin the board actually drove is not input-only, and a
        # pin it could not drive is — whatever the datasheet says.
        io_only = {p for p in caps.get("input_only", []) if probed.get(p, True) is False}
        io_only |= {p for p, ok in probed.items() if ok is False}
        caps["input_only"] = sorted(io_only)
        caps["input_only_confirmed"] = True
    return caps


def _assignments_from_io(io_cfg: dict) -> List[dict]:
    """The existing config.io shape -> flat assignments, so the map reflects what
    a node is ACTUALLY running rather than a separate parallel truth."""
    out = []
    io_cfg = io_cfg or {}
    tft = io_cfg.get("tft") or {}
    for role in ("rst", "cs", "dc", "wr", "rd"):
        if isinstance(tft.get(role), int):
            out.append({"pin": tft[role], "role": role, "device": "display",
                        "profile": "tft_ili9488_p8"})
    for i, p in enumerate(tft.get("d") or []):
        if isinstance(p, int):
            out.append({"pin": p, "role": "d%d" % i, "device": "display",
                        "profile": "tft_ili9488_p8"})
    for role in ("clk", "miso", "mosi", "cs"):
        sd = io_cfg.get("sd") or {}
        if isinstance(sd.get(role), int):
            out.append({"pin": sd[role], "role": role, "device": "sd",
                        "profile": "sd_spi"})
    touch = io_cfg.get("touch") or {}
    for role in ("xp", "ym", "yp", "xm"):
        if isinstance(touch.get(role), int):
            out.append({"pin": touch[role], "role": role, "device": "touch",
                        "profile": "touch_4wire"})
    for key, prof in (("neopixel", "neopixel"), ("led", "led"),
                      ("relay", "relay"), ("adc", "analog_sensor")):
        v = io_cfg.get(key)
        if isinstance(v, int) and v >= 0:
            out.append({"pin": v, "role": "data" if prof == "neopixel" else "pin",
                        "device": key, "profile": prof})
    for extra in io_cfg.get("devices") or []:
        if isinstance(extra, dict):
            for role, pin in (extra.get("pins") or {}).items():
                if isinstance(pin, int):
                    out.append({"pin": pin, "role": role,
                                "device": extra.get("name") or extra.get("profile", "device"),
                                "profile": extra.get("profile", "")})
    return out


def build_pin_map(io_cfg: dict, chip: str = "esp32s3", node_id: str = "") -> dict:
    """Every pin on the chip with what claims it, plus the problems."""
    caps = pin_capabilities(chip, node_id)
    assigns = _assignments_from_io(io_cfg)
    by_pin: Dict[int, List[dict]] = {}
    for a in assigns:
        by_pin.setdefault(int(a["pin"]), []).append(a)

    pins, conflicts, warnings = [], [], []
    for p in sorted(set(caps["gpio"]) | set(by_pin)):
        holders = by_pin.get(p, [])
        entry = {"pin": p, "used": bool(holders),
                 "holders": [{"device": h["device"], "role": h["role"],
                              "profile": h.get("profile", "")} for h in holders],
                 "adc": p in caps["adc"], "adc2_only": p in caps.get("adc2", []),
                 "input_only": p in caps.get("input_only", []),
                 "input_only_confirmed": bool(caps.get("input_only_confirmed")),
                 "reserved": caps.get("reserved", {}).get(p, "")}
        # Two devices on one pin is the commonest cause of "it just doesn't work".
        if len(holders) > 1:
            devs = sorted({h["device"] for h in holders})
            if len(devs) > 1:
                entry["conflict"] = True
                conflicts.append({"pin": p, "devices": devs,
                                  "roles": [h["role"] for h in holders]})
        if holders and entry["reserved"]:
            warnings.append({"pin": p, "issue": entry["reserved"],
                             "devices": sorted({h["device"] for h in holders})})
        if holders and entry["input_only"] and any(
                h["role"] not in ("adc",) for h in holders):
            warnings.append({"pin": p, "issue": "input only — cannot be driven",
                             "devices": sorted({h["device"] for h in holders})})
        pins.append(entry)

    free = [e["pin"] for e in pins if not e["used"] and not e["reserved"]]
    return {"chip": chip, "pins": pins, "conflicts": conflicts,
            "warnings": warnings, "free": free,
            "used": sum(1 for e in pins if e["used"]), "total": len(pins)}


@capability(
    "mesh.pins.map", http_method="POST", http_path="/mesh/pins/map",
    http_tags=["mesh", "pins"], memory="off", silent=True,
    description="The full pin map for a node: every GPIO, what claims it (device + role), whether "
                "it is ADC-capable, input-only or strapping/flash-reserved, plus detected CONFLICTS "
                "(two devices on one pin — the commonest cause of 'it just doesn't work') and "
                "warnings. Built from the node's live config.io, so it reflects what is actually "
                "running. Input: node_id (str!). Output: {chip, pins, conflicts, warnings, free}.",
)
async def cap_mesh_pins_map(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    node = await _call_cap("mesh.node", node_id=node_id)
    n = (node or {}).get("node") if isinstance(node, dict) else None
    if not n:
        return {"error": "unknown node", "node_id": node_id}
    cfg = n.get("config") or {}
    # Prefer what the node REPORTED (ESP.getChipModel). The `board` field is a
    # hardcoded "esp32" in the sketch, so believing it picked the classic ESP32
    # profile — GPIO 0-39 — for an S3 and hid GPIO40-48 completely, which is
    # where the display and touch pins actually live on this board.
    reported = ""
    try:
        mesh = (sys.modules.get("mesh_capabilities")
                or sys.modules.get("Vera.vera.mesh.mesh_capabilities"))
        if mesh is not None:
            reported = (getattr(mesh, "_NODE_CHIP", {}) or {}).get(node_id, "")
    except Exception as e:
        log.debug("reported chip lookup: %s", e)

    raw = str(reported or n.get("chip") or "")
    source = "reported" if raw else ""
    if not raw:
        # Fall back to the pin map already in use: pins above 39 can only exist
        # on an S3, so the config itself is better evidence than `board`.
        pins_in_use = [a["pin"] for a in _assignments_from_io(cfg.get("io") or {})]
        if pins_in_use and max(pins_in_use) > 39:
            raw, source = "esp32s3", "inferred from pins in use"
        else:
            raw, source = str(n.get("board") or ""), "board field"

    low = raw.lower()
    chip = ("esp32s3" if "s3" in low else
            "esp32c3" if "c3" in low else
            "esp32" if low.startswith("esp32") and low not in ("esp32",) else
            "esp32" if low == "esp32" else "")
    if not chip:
        chip, source = "esp32s3", "assumed"
    out = build_pin_map(cfg.get("io") or {}, chip, node_id)
    out["chip_reported"] = raw
    out["chip_source"] = source
    return out


@capability(
    "mesh.pins.profiles", http_method="GET", http_path="/mesh/pins/profiles",
    http_tags=["mesh", "pins"], memory="off", silent=True,
    description="Device profiles that can be mapped onto pins — display, touch, SD, NeoPixel, LED, "
                "relay, DS18B20, DHT22, generic analogue sensor, digital input, I2C, PWM. Each "
                "declares the roles it needs and how its signal is read (filter, units, poll rate), "
                "so supporting new hardware is data rather than code. Output: {profiles, count}.",
)
async def cap_mesh_pins_profiles(trace_id=None) -> dict:
    return {"profiles": DEVICE_PROFILES, "count": len(DEVICE_PROFILES)}


@capability(
    "mesh.pins.assign", http_method="POST", http_path="/mesh/pins/assign",
    http_tags=["mesh", "pins"], memory="on",
    description="Attach a device profile to pins on a node, validated against the chip before "
                "anything is pushed: unknown roles, pins that don't exist, input-only pins asked to "
                "drive, non-ADC pins asked to measure, and collisions with an existing device are "
                "all refused with the reason. Input: node_id (str!), profile (str! — from "
                "mesh.pins.profiles), name (str — what to call this device), pins (JSON! — "
                "{role: gpio}), force (bool=False — assign despite warnings). "
                "Output: {ok, assigned, warnings} or {error}.",
)
async def cap_mesh_pins_assign(node_id: str = "", profile: str = "", name: str = "",
                               pins=None, force: bool = False, trace_id=None) -> dict:
    if not node_id or not profile:
        return {"error": "node_id and profile required"}
    prof = DEVICE_PROFILES.get(profile)
    if not prof:
        return {"error": "unknown profile %r; see mesh.pins.profiles" % profile,
                "profiles": sorted(DEVICE_PROFILES)}
    pins = json.loads(pins) if isinstance(pins, str) else pins
    if not isinstance(pins, dict) or not pins:
        return {"error": "pins must be {role: gpio}; roles for %s are %s"
                         % (profile, ", ".join(prof["roles"]))}

    current = await cap_mesh_pins_map(node_id=node_id)
    if current.get("error"):
        return current
    caps = pin_capabilities(current["chip"], node_id)
    taken = {e["pin"]: e["holders"] for e in current["pins"] if e["used"]}

    bad, warn = [], []
    unknown = [r for r in pins if r not in prof["roles"]]
    if unknown:
        bad.append("roles not in profile %s: %s (expected %s)"
                   % (profile, ", ".join(unknown), ", ".join(prof["roles"])))
    adc_roles = set(((prof.get("read") or {}).get("adc_roles")) or
                    (["adc"] if "adc" in prof["roles"] else []))
    for role, pin in pins.items():
        if not isinstance(pin, int):
            bad.append("%s: pin must be an integer" % role)
            continue
        if pin not in caps["gpio"]:
            bad.append("%s: GPIO%d does not exist on %s" % (role, pin, current["chip"]))
            continue
        if pin in caps.get("input_only", []) and role not in adc_roles:
            msg = "%s: GPIO%d is input-only and cannot drive" % (role, pin)
            if caps.get("input_only_confirmed"):
                bad.append(msg)          # measured on this board — a real blocker
            else:
                warn.append(msg + " (unverified — run mesh.pins.probe to confirm)")
        if role in adc_roles and pin not in caps["adc"]:
            note = " (ADC2 is unusable while Wi-Fi is on)" if pin in caps.get("adc2", []) else ""
            bad.append("%s: GPIO%d cannot be read as analogue%s" % (role, pin, note))
        holder = taken.get(pin)
        if holder and any(h["device"] != (name or profile) for h in holder):
            msg = "GPIO%d already used by %s" % (pin, ", ".join(
                sorted({h["device"] + "." + h["role"] for h in holder})))
            (warn if force else bad).append(msg)
        res = caps.get("reserved", {}).get(pin)
        if res:
            warn.append("GPIO%d is %s" % (pin, res))
    if bad:
        return {"error": "; ".join(bad), "profile": profile,
                "roles": prof["roles"], "warnings": warn}

    # Land it in the shape the firmware already understands.
    io_patch: Dict[str, Any] = {}
    if profile == "tft_ili9488_p8":
        tft = {r: p for r, p in pins.items() if not r.startswith("d")}
        d = [pins[k] for k in sorted((k for k in pins if k.startswith("d")),
                                     key=lambda k: int(k[1:]))]
        if d:
            tft["d"] = d
        io_patch["tft"] = tft
    elif profile == "touch_4wire":
        io_patch["touch"] = dict(pins)
    elif profile == "sd_spi":
        io_patch["sd"] = dict(pins)
    elif profile == "neopixel":
        io_patch["neopixel"] = list(pins.values())[0]
    elif profile in ("led", "relay"):
        io_patch[profile] = list(pins.values())[0]
    else:
        io_patch["devices"] = [{"name": name or profile, "profile": profile,
                                "pins": dict(pins), "read": prof.get("read")}]

    r = await _call_cap("mesh.config", node_id=node_id, config={"io": io_patch},
                        merge=True)
    return {"ok": bool(isinstance(r, dict) and not r.get("error")),
            "assigned": {"profile": profile, "name": name or profile, "pins": pins},
            "warnings": warn, "job_id": (r or {}).get("job_id"),
            "error": (r or {}).get("error", "")}


@capability(
    "mesh.pins.probe", http_method="POST", http_path="/mesh/pins/probe",
    http_tags=["mesh", "pins"], memory="on",
    description="Measure which GPIOs a node can actually DRIVE, rather than trusting a table. Each "
                "pin is driven high then low and read back; one that reports the same level both "
                "ways has no usable output stage. Settles questions like 'is GPIO46 input-only on "
                "this chip?' on the real silicon, since ESP32 variants differ and a wrong answer "
                "sends you chasing a hardware fault that isn't there. Display control lines are "
                "skipped (driving them mid-draw corrupts the panel). Read the result with "
                "mesh.jobs. Input: node_id (str!), pins (JSON list — default every GPIO). "
                "Output: {ok, job_id}.",
)
async def cap_mesh_pins_probe(node_id: str = "", pins=None, results=None,
                              trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    # Feeding the job result back in is what makes the measurement stick, so the
    # pin map and the assign form stop guessing.
    results = json.loads(results) if isinstance(results, str) else results
    if results:
        note_pin_probe(node_id, results)
        return {"ok": True, "recorded": len(results),
                "not_drivable": sorted(p for p, ok in
                                       _PIN_PROBED.get(node_id, {}).items() if not ok)}
    pins = json.loads(pins) if isinstance(pins, str) else pins
    return await _call_cap("mesh.send", node_id=node_id, type="pin_probe",
                           payload={"pins": pins or []})


@capability(
    "mesh.pins.touch_hunt", http_method="POST", http_path="/mesh/pins/touch_hunt",
    http_tags=["mesh", "pins"], memory="on",
    description="Find the touch wires ON THE NODE'S OWN SCREEN. It prompts 'do not touch', measures "
                "continuity between all eight LCD data lines, prompts 'press and hold', measures "
                "again, and reports the pairs that conduct ONLY under pressure — those are the X-to-Y "
                "crossing, i.e. the touch wires. Purely digital, because only one of those pins is on "
                "ADC1 (the rest are ADC2 and unreadable while Wi-Fi is up). Results are drawn on the "
                "panel as well as returned, so no log-reading round trip is needed. Takes ~10s and "
                "needs someone to press when asked. Each phase counts down on the panel. Input: node_id (str!), hold_s (int=8 — seconds per phase). Output: {ok, job_id}.",
)
async def cap_mesh_pins_touch_hunt(node_id: str = "", hold_s: int = 8,
                                   trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type="touch_hunt",
                           payload={"hold_s": max(3, min(30, int(hold_s)))})


@capability(
    "mesh.pins.touch_scan", http_method="POST", http_path="/mesh/pins/touch_scan",
    http_tags=["mesh", "pins"], memory="on",
    description="Ask a node to FIND its touch wires rather than guessing them. A 4-wire resistive "
                "panel is two resistive plates, and on an Uno shield those wires are shared with "
                "LCD lines — so a plate shows up as continuity between two pins we already drive. "
                "No pressing needed. Read the result with mesh.jobs: `pairs` are connected pins; "
                "two disjoint pairs are the X and Y plates. Input: node_id (str!). "
                "Output: {ok, job_id}.",
)
async def cap_mesh_pins_touch_scan(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type="touch_scan", payload={})


# Built-ins plus anything saved. Runs last: it depends on the pad loader and the
# capabilities defined above.
_register_pads()
