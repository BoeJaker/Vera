"""
theme_defs.py  —  Single source of truth for the Vera UI theme system
======================================================================
Both ``ui builder/ui_capabilities.py`` (which serves ``/ui/themes.css``) and
``chat/chat_panels_capabilities.py`` (which serves the ``/ui/theme*`` JSON API)
import their theme tables from here so the CSS stylesheet and the JSON var
payloads can never drift apart.

Every theme's ``vars`` are expressed in the *research namespace* (``--bg``,
``--s1``..``--s3``, ``--bd``/``--bd2``, ``--t1``..``--t3``, ``--ac``..``--ac5``).
vera-ui.js maps these onto the orchestrator (``--bg0``, ``--acc`` …) and IDE
(``--text0`` …) namespaces at apply time.

New in this revision:
  • ``--on-ac`` — the text colour to paint on top of an accent-coloured
    surface (active tabs, primary buttons, "on" chips). Previously panels
    hard-coded a near-black value which became black-on-dark in light themes.
  • A library of themes reminiscent of famous computing colour schemes.
  • ``ensure_contrast()`` — guarantees a theme never has text that blends into
    its background, used when validating user-created custom themes.
"""
from __future__ import annotations

from typing import Dict, Tuple

# Shared layout tail applied to every built-in theme so the shell picks up the
# same radius / sidebar metrics regardless of which theme is active.
_TAIL = {"--ui-radius": "5px", "--lhm-width": "184px", "--lhm-collapsed-width": "46px"}


def _t(vars_: dict) -> dict:
    """Merge a theme's colour vars with the shared layout tail."""
    out = dict(vars_)
    for k, v in _TAIL.items():
        out.setdefault(k, v)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Colour maths — relative luminance + WCAG contrast ratio (hex inputs only;
# rgba()/named values fall back to a neutral mid luminance so we never crash).
# ─────────────────────────────────────────────────────────────────────────────
def _rgb(hex_: str) -> Tuple[float, float, float] | None:
    if not isinstance(hex_, str):
        return None
    h = hex_.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) < 6:
        return None
    try:
        return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)
    except ValueError:
        return None


def luminance(hex_: str) -> float:
    rgb = _rgb(hex_)
    if rgb is None:
        return 0.5
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# Light/dark ink used when a theme doesn't specify (or specifies a poor)
# on-accent / text colour. Kept just shy of pure black/white so they read as
# "ink" rather than as harsh #000/#fff.
INK_DARK = "#0c0d10"
INK_LIGHT = "#f6f4ef"


def on_accent_for(accent: str, dark: str = INK_DARK, light: str = INK_LIGHT) -> str:
    """Pick the text colour (dark or light) that best contrasts an accent."""
    return dark if luminance(accent) > 0.45 else light


def ensure_contrast(vars_: dict) -> dict:
    """
    Return a copy of a theme's vars with text guaranteed to be legible:
      • ``--t1`` (primary text) must clear 4.5:1 against ``--bg``; if it
        doesn't we replace it with the appropriate ink.
      • ``--on-ac`` must exist and clear 3:1 against ``--ac`` (accent buttons
        are large/bold, so 3:1 is the WCAG threshold); otherwise it is derived.
    User-created themes flow through this so "text similar to the background"
    can't be saved.
    """
    v = dict(vars_ or {})
    bg = v.get("--bg", INK_DARK)
    bg_is_dark = luminance(bg) < 0.5

    t1 = v.get("--t1")
    if not t1 or contrast(t1, bg) < 4.5:
        v["--t1"] = INK_LIGHT if bg_is_dark else INK_DARK

    # Nudge secondary/muted text toward legibility too (softer threshold).
    for key, floor in (("--t2", 2.6), ("--t3", 1.9)):
        val = v.get(key)
        if val and contrast(val, bg) < floor:
            # Blend toward the primary ink by just using it — callers rarely
            # notice, and it beats an invisible label.
            v[key] = v["--t1"]

    ac = v.get("--ac", "#888888")
    on = v.get("--on-ac")
    if not on or contrast(on, ac) < 3.0:
        v["--on-ac"] = on_accent_for(ac)
    return v


# ─────────────────────────────────────────────────────────────────────────────
# BUILT-IN THEMES
# The original five (ash/dusk/void/chalk/ice) keep their exact colours — only
# a computed --on-ac is added — so existing installs look unchanged. The rest
# are homages to well-known computing colour schemes.
# ─────────────────────────────────────────────────────────────────────────────
_RAW_THEMES: Dict[str, dict] = {
    # ── Vera originals ──────────────────────────────────────────────────────
    "ash": {"label": "Ash", "type": "light", "accent": "#2e6da4", "vars": {
        "--bg": "#f0ede8", "--s1": "#e8e5df", "--s2": "#dedbd4", "--s3": "#d4d1c8",
        "--bd": "rgba(0,0,0,.08)", "--bd2": "rgba(0,0,0,.15)",
        "--t1": "#1a1a18", "--t2": "#6a6860", "--t3": "#a8a69e",
        "--ac": "#2e6da4", "--ac2": "#228060", "--ac3": "#c47020", "--ac4": "#c03030", "--ac5": "#6040a0",
        "--on-ac": "#f6f4ef"}},
    "dusk": {"label": "Dusk", "type": "dark", "accent": "#6ea8d8", "vars": {
        "--bg": "#0e0f12", "--s1": "#13151a", "--s2": "#1a1d24", "--s3": "#222630",
        "--bd": "rgba(255,255,255,.07)", "--bd2": "rgba(255,255,255,.14)",
        "--t1": "#d4dae4", "--t2": "#6b7585", "--t3": "#3b4252",
        "--ac": "#6ea8d8", "--ac2": "#5ec9a0", "--ac3": "#e09a55", "--ac4": "#e06060", "--ac5": "#a78bfa",
        "--on-ac": "#0b1119"}},
    "void": {"label": "Void", "type": "dark", "accent": "#9b8dfa", "vars": {
        "--bg": "#000000", "--s1": "#070707", "--s2": "#0d0d0d", "--s3": "#141414",
        "--bd": "rgba(255,255,255,.05)", "--bd2": "rgba(255,255,255,.1)",
        "--t1": "#e0e0e0", "--t2": "#929292", "--t3": "#636363",
        "--ac": "#9b8dfa", "--ac2": "#5ecab0", "--ac3": "#dba355", "--ac4": "#e06060", "--ac5": "#f472b6",
        "--on-ac": "#0c0a16"}},
    "chalk": {"label": "Chalk", "type": "dark", "accent": "#d4a96a", "vars": {
        "--bg": "#1c1c1e", "--s1": "#242428", "--s2": "#2c2c32", "--s3": "#34343c",
        "--bd": "rgba(255,255,255,.08)", "--bd2": "rgba(255,255,255,.16)",
        "--t1": "#e8e4d8", "--t2": "#b6ac97", "--t3": "#6e6b5b",
        "--ac": "#d4a96a", "--ac2": "#80c090", "--ac3": "#c08878", "--ac4": "#e07070", "--ac5": "#9080c0",
        "--on-ac": "#1c160c"}},
    "ice": {"label": "Ice", "type": "dark", "accent": "#5ab0f0", "vars": {
        "--bg": "#090f18", "--s1": "#0d1620", "--s2": "#121e2c", "--s3": "#182638",
        "--bd": "rgba(80,160,255,.1)", "--bd2": "rgba(80,160,255,.2)",
        "--t1": "#c0d8f0", "--t2": "#406880", "--t3": "#1e3850",
        "--ac": "#5ab0f0", "--ac2": "#38d0b0", "--ac3": "#e0b060", "--ac4": "#e06868", "--ac5": "#8070e0",
        "--on-ac": "#041019"}},

    # ── Homages to famous computing colour schemes ──────────────────────────
    "solar-light": {"label": "Solarized Light", "type": "light", "accent": "#268bd2", "vars": {
        "--bg": "#fdf6e3", "--s1": "#f4edda", "--s2": "#eee8d5", "--s3": "#e3dcc4",
        "--bd": "rgba(101,123,131,.18)", "--bd2": "rgba(101,123,131,.32)",
        "--t1": "#073642", "--t2": "#657b83", "--t3": "#93a1a1",
        "--ac": "#268bd2", "--ac2": "#859900", "--ac3": "#b58900", "--ac4": "#dc322f", "--ac5": "#6c71c4",
        "--on-ac": "#fdf6e3"}},
    "solar-dark": {"label": "Solarized Dark", "type": "dark", "accent": "#268bd2", "vars": {
        "--bg": "#002b36", "--s1": "#073642", "--s2": "#0a404e", "--s3": "#0e4a5a",
        "--bd": "rgba(147,161,161,.14)", "--bd2": "rgba(147,161,161,.26)",
        "--t1": "#93a1a1", "--t2": "#839496", "--t3": "#586e75",
        "--ac": "#268bd2", "--ac2": "#859900", "--ac3": "#b58900", "--ac4": "#dc322f", "--ac5": "#6c71c4",
        "--on-ac": "#f5fbff"}},
    "gruvbox": {"label": "Gruvbox", "type": "dark", "accent": "#fabd2f", "vars": {
        "--bg": "#282828", "--s1": "#32302f", "--s2": "#3c3836", "--s3": "#504945",
        "--bd": "rgba(235,219,178,.12)", "--bd2": "rgba(235,219,178,.24)",
        "--t1": "#ebdbb2", "--t2": "#a89984", "--t3": "#7c6f64",
        "--ac": "#fabd2f", "--ac2": "#b8bb26", "--ac3": "#fe8019", "--ac4": "#fb4934", "--ac5": "#d3869b",
        "--on-ac": "#282828"}},
    "nord": {"label": "Nord", "type": "dark", "accent": "#88c0d0", "vars": {
        "--bg": "#2e3440", "--s1": "#333b4c", "--s2": "#3b4252", "--s3": "#434c5e",
        "--bd": "rgba(216,222,233,.12)", "--bd2": "rgba(216,222,233,.24)",
        "--t1": "#eceff4", "--t2": "#d8dee9", "--t3": "#7b88a1",
        "--ac": "#88c0d0", "--ac2": "#a3be8c", "--ac3": "#ebcb8b", "--ac4": "#bf616a", "--ac5": "#b48ead",
        "--on-ac": "#2e3440"}},
    "dracula": {"label": "Dracula", "type": "dark", "accent": "#bd93f9", "vars": {
        "--bg": "#282a36", "--s1": "#2f3240", "--s2": "#383a4a", "--s3": "#44475a",
        "--bd": "rgba(248,248,242,.1)", "--bd2": "rgba(248,248,242,.22)",
        "--t1": "#f8f8f2", "--t2": "#bdc0d6", "--t3": "#6272a4",
        "--ac": "#bd93f9", "--ac2": "#50fa7b", "--ac3": "#ffb86c", "--ac4": "#ff5555", "--ac5": "#ff79c6",
        "--on-ac": "#21222c"}},
    "monokai": {"label": "Monokai", "type": "dark", "accent": "#a6e22e", "vars": {
        "--bg": "#272822", "--s1": "#2f302a", "--s2": "#383930", "--s3": "#49483e",
        "--bd": "rgba(248,248,242,.1)", "--bd2": "rgba(248,248,242,.22)",
        "--t1": "#f8f8f2", "--t2": "#bcbcae", "--t3": "#75715e",
        "--ac": "#a6e22e", "--ac2": "#66d9ef", "--ac3": "#e6db74", "--ac4": "#f92672", "--ac5": "#ae81ff",
        "--on-ac": "#1c1d17"}},
    "tokyonight": {"label": "Tokyo Night", "type": "dark", "accent": "#7aa2f7", "vars": {
        "--bg": "#1a1b26", "--s1": "#1f2032", "--s2": "#24283b", "--s3": "#2f344d",
        "--bd": "rgba(169,177,214,.12)", "--bd2": "rgba(169,177,214,.24)",
        "--t1": "#c0caf5", "--t2": "#a9b1d6", "--t3": "#565f89",
        "--ac": "#7aa2f7", "--ac2": "#9ece6a", "--ac3": "#e0af68", "--ac4": "#f7768e", "--ac5": "#bb9af7",
        "--on-ac": "#131420"}},
    "matrix": {"label": "Matrix", "type": "dark", "accent": "#00ff41", "vars": {
        "--bg": "#020a02", "--s1": "#051305", "--s2": "#081b08", "--s3": "#0c2a0c",
        "--bd": "rgba(0,255,65,.14)", "--bd2": "rgba(0,255,65,.3)",
        "--t1": "#39ff5a", "--t2": "#1faa39", "--t3": "#127322",
        "--ac": "#00ff41", "--ac2": "#7fff00", "--ac3": "#adff2f", "--ac4": "#ff5555", "--ac5": "#00c8a0",
        "--on-ac": "#021005"}},
    "amber": {"label": "Amber CRT", "type": "dark", "accent": "#ffb000", "vars": {
        "--bg": "#150d00", "--s1": "#1e1300", "--s2": "#281a00", "--s3": "#352300",
        "--bd": "rgba(255,176,0,.14)", "--bd2": "rgba(255,176,0,.3)",
        "--t1": "#ffc233", "--t2": "#c88a12", "--t3": "#8a5f0c",
        "--ac": "#ffb000", "--ac2": "#ffcc33", "--ac3": "#ff8c00", "--ac4": "#ff5540", "--ac5": "#ffd98a",
        "--on-ac": "#1a1000"}},
    "c64": {"label": "Commodore 64", "type": "dark", "accent": "#8b7ff0", "vars": {
        "--bg": "#4038a8", "--s1": "#4640b0", "--s2": "#4d47bd", "--s3": "#5951c9",
        "--bd": "rgba(168,156,255,.24)", "--bd2": "rgba(168,156,255,.42)",
        "--t1": "#d5cffb", "--t2": "#a89cff", "--t3": "#7d72cf",
        "--ac": "#8b7ff0", "--ac2": "#8fe3b0", "--ac3": "#ffd98a", "--ac4": "#ff8f8f", "--ac5": "#f0a0d8",
        "--on-ac": "#221b6b"}},
    "paperwhite": {"label": "Paper White", "type": "light", "accent": "#2b6cb0", "vars": {
        "--bg": "#ffffff", "--s1": "#f7f7f5", "--s2": "#efefec", "--s3": "#e4e4df",
        "--bd": "rgba(20,20,20,.1)", "--bd2": "rgba(20,20,20,.2)",
        "--t1": "#16181d", "--t2": "#5a5f6a", "--t3": "#9298a3",
        "--ac": "#2b6cb0", "--ac2": "#2f855a", "--ac3": "#b7791f", "--ac4": "#c53030", "--ac5": "#6b46c1",
        "--on-ac": "#ffffff"}},
}

# Materialise: apply the layout tail and guarantee legibility for every theme.
BUILTIN_THEMES: Dict[str, dict] = {}
for _tid, _spec in _RAW_THEMES.items():
    BUILTIN_THEMES[_tid] = {
        "label": _spec["label"],
        "type": _spec["type"],
        "accent": _spec["accent"],
        "vars": ensure_contrast(_t(_spec["vars"])),
    }

DEFAULT_THEME = "dusk"

__all__ = [
    "BUILTIN_THEMES", "DEFAULT_THEME",
    "ensure_contrast", "on_accent_for", "contrast", "luminance",
    "INK_DARK", "INK_LIGHT",
]
