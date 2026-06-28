"""
vera_companion.py — Desktop companion ("pet") for Vera
======================================================
A small, frameless, transparent, always-on-top desktop window that renders the
SAME `<vera-character>` web component used in the web Companion panel. Because
it reuses the component verbatim, animation, TTS/STT, and event-narration are
all identical to the web — there is no duplicated rendering logic.

The window loads a tiny local HTML page that pulls the element from a running
Vera backend (`/ui/elements/character.js`) and points it at that backend for
character assets, the event stream, and TTS/STT.

Run
───
    pip install pywebview
    python vera_companion.py --base http://llm.int:8999 --agent assistant

Options
───────
    --base   Vera backend origin (default $VERA_BASE or http://localhost:8999)
    --agent  Agent name (default $VERA_COMPANION_AGENT or "assistant")
    --size   Character size in px (default 280)
    --no-narrate   Do not subscribe to the event stream / narrate

Notes
─────
• Transparent + frameless windows need a platform backend that supports it
  (Windows: EdgeChromium/WebView2; macOS: Cocoa; Linux: GTK/QT). pywebview
  picks the best available. If transparency is unsupported, it degrades to a
  normal small window — the companion still works.
• Drag the character to move the window; right-click-less: a small ✕ closes it.
"""

import argparse
import os
import sys

DEFAULT_BASE = os.getenv("VERA_BASE", "http://localhost:8999")
DEFAULT_AGENT = os.getenv("VERA_COMPANION_AGENT", "assistant")


def _page(base: str, agent: str, size: int, narrate: bool) -> str:
    base = base.rstrip("/")
    narrate_attr = "narrate" if narrate else ""
    # The page sets window._veraBase so the element resolves the backend for
    # /character/*, /tts, /stt and the /ws/mcp event stream. The whole body is
    # draggable (so the frameless window can be moved); the character and its
    # mic button opt out of dragging so they stay clickable.
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:system-ui,-apple-system,Segoe UI,sans-serif;-webkit-user-select:none;user-select:none}}
  #bar{{position:fixed;top:0;left:0;right:0;height:18px;-webkit-app-region:drag;app-region:drag;z-index:50}}
  #close{{position:fixed;top:2px;right:4px;z-index:60;width:16px;height:16px;border:none;border-radius:50%;
    background:rgba(0,0,0,.35);color:#ddd;font-size:11px;line-height:14px;cursor:pointer;-webkit-app-region:no-drag;opacity:.4}}
  #close:hover{{opacity:1}}
  #stage{{height:100vh;display:flex;align-items:center;justify-content:center;-webkit-app-region:drag;app-region:drag}}
  vera-character{{-webkit-app-region:no-drag;app-region:no-drag}}
</style>
<script>window._veraBase = "{base}";</script>
<script src="{base}/ui/elements/character.js"></script>
</head><body>
  <div id="bar"></div>
  <button id="close" onclick="try{{pywebview.api.close()}}catch(e){{window.close()}}">✕</button>
  <div id="stage">
    <vera-character agent="{agent}" size="{size}" interactive {narrate_attr}></vera-character>
  </div>
</body></html>"""


class _Api:
    def __init__(self, window_getter):
        self._get = window_getter

    def close(self):
        w = self._get()
        if w:
            w.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Vera desktop companion")
    ap.add_argument("--base", default=DEFAULT_BASE, help="Vera backend origin")
    ap.add_argument("--agent", default=DEFAULT_AGENT, help="Agent name to embody")
    ap.add_argument("--size", type=int, default=280, help="Character size (px)")
    ap.add_argument("--no-narrate", action="store_true", help="Disable event narration")
    args = ap.parse_args(argv)

    try:
        import webview  # pywebview
    except ImportError:
        sys.stderr.write(
            "pywebview is required for the desktop companion.\n"
            "  pip install pywebview\n"
            "(On Windows it also needs the WebView2 runtime; on Linux a GTK or "
            "Qt backend.)\n")
        return 2

    win = int(args.size) + 40
    html = _page(args.base, args.agent, args.size, not args.no_narrate)

    holder = {}
    api = _Api(lambda: holder.get("w"))
    holder["w"] = webview.create_window(
        "Vera Companion",
        html=html,
        width=win, height=win,
        frameless=True,
        easy_drag=False,        # we manage drag regions via -webkit-app-region
        on_top=True,
        transparent=True,
        background_color="#000000",
        js_api=api,
    )
    # gui="edgechromium" on Windows supports transparency; let pywebview auto-pick
    # but fall back gracefully if the chosen backend rejects transparency.
    try:
        webview.start()
    except Exception as e:
        sys.stderr.write(f"Companion window failed to start: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
