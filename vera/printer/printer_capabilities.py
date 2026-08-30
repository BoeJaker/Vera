"""printer_capabilities.py -- Vera thermal-printer service.

Auto-detecting, reconnect-safe ESC/POS printing to a USB thermal printer that the
host exposes at /dev/vera-printer (a udev symlink; group `plugdev` so Vera's user can
write it). Every print re-resolves + re-opens the device, so unplug/replug just works
-- there is no long-lived handle to go stale.

Capabilities
------------
  printer.status              device present? + Pillow/env introspection
  printer.print.raw           raw ESC/POS (text | base64 | hex)
  printer.print.text          built-in-font text (size/align/bold) -- no Pillow needed
  printer.print.nice          text rendered with a TTF font at any point size (Pillow)
  printer.print.image         print an image (base64 | path | url), dithered to 1-bpp
  printer.print.label         composed label: title + lines + optional image
  printer.notify              formatted notification -- also a delivery CHANNEL, so
                              Vera can route important updates to the printer

Pillow powers "nice fonts / sizes / images"; it is imported lazily so the module still
loads (and text/raw/notify still work) if Pillow is missing -- those caps then say so.
"""
from __future__ import annotations

import base64
import glob
import os
import sys
import time
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import capability, emit_event
from Vera.vera.printer.escpos_core import text_job, raster_job, INIT, CUT

# 58mm heads are 384 dots; 80mm are 576. Default 384; override per-call or via env.
DEFAULT_WIDTH = int(os.environ.get("VERA_PRINTER_WIDTH", "384"))
# Cheap USB thermal printers overrun on a single large bulk write (dmesg
# "nonzero write bulk status -108" -> the device USB-resets); pace it in chunks.
_CHUNK = int(os.environ.get("VERA_PRINTER_CHUNK", "512"))
_PACE = float(os.environ.get("VERA_PRINTER_PACE", "0.012"))
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


# ── device seam (reconnect-safe) ────────────────────────────────────────────────
def _find_device() -> str:
    for d in (["/dev/vera-printer"]
              + sorted(glob.glob("/dev/usb/lp*"))
              + sorted(glob.glob("/dev/ttyUSB*"))
              + sorted(glob.glob("/dev/ttyACM*"))):
        if os.path.exists(d):
            return d
    return ""


def _write(data: bytes) -> Dict[str, Any]:
    # Pace the write in chunks so a big raster does not overrun the printer buffer,
    # and retry once if the device is mid-reconnect (udev re-creates the symlink on
    # re-enumeration) or reset us mid-write.
    err = "no thermal printer found (looked for /dev/vera-printer, /dev/usb/lp*, /dev/ttyUSB*/ttyACM*)"
    for attempt in (1, 2):
        dev = _find_device()
        if not dev:
            if attempt == 1:
                time.sleep(1.5); continue
            break
        try:
            with open(dev, "wb", buffering=0) as f:
                for i in range(0, len(data), _CHUNK):
                    f.write(data[i:i + _CHUNK]); f.flush()
                    if _PACE and i + _CHUNK < len(data):
                        time.sleep(_PACE)
            return {"ok": True, "device": dev, "bytes": len(data)}
        except PermissionError:
            return {"ok": False, "device": dev,
                    "error": "permission denied on %s -- Vera's user needs write access "
                             "(udev rule: GROUP=plugdev, MODE=0660)" % dev}
        except OSError as e:
            err = "write %s failed: %s" % (dev, e)
            if attempt == 1:
                time.sleep(1.5); continue   # printer may have reset mid-write; let it re-enumerate
            break
    return {"ok": False, "error": err}


def _pil():
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    return Image, ImageDraw, ImageFont


def _font(size: int, bold: bool = False):
    _, _, ImageFont = _pil()
    for p in (_FONT_BOLD if bold else []) + _FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, max(6, int(size)))
    return ImageFont.load_default()


def _image_to_rows(img, width: int):
    """PIL image -> (w, h, [row_bytes]) 1-bpp, MSB-first, 1=black."""
    Image, _, _ = _pil()
    if img.mode != "L":
        img = img.convert("L")
    if img.width != width:
        h = max(1, int(img.height * width / img.width))
        img = img.resize((width, h))
    img = img.convert("1")            # Floyd-Steinberg dither to 1-bpp
    w, h = img.size
    bpr = (w + 7) // 8
    px = img.load()
    rows = []
    for y in range(h):
        row = bytearray(bpr)
        for x in range(w):
            if px[x, y] == 0:          # 0 = black in mode "1"
                row[x >> 3] |= (0x80 >> (x & 7))
        rows.append(bytes(row))
    return w, h, rows


def _render_text_image(lines, width: int, font_size: int, align: str = "left",
                       title: Optional[str] = None):
    """Render text (optional bold title + body lines) to a 1-bpp bitmap for raster."""
    Image, ImageDraw, _ = _pil()
    body_font = _font(font_size)
    title_font = _font(int(font_size * 1.6), bold=True) if title else None
    pad = 6
    # measure
    def _wh(draw, text, font):
        try:
            b = draw.textbbox((0, 0), text, font=font); return b[2] - b[0], b[3] - b[1]
        except Exception:
            return draw.textsize(text, font=font)
    tmp = ImageDraw.Draw(Image.new("L", (width, 10), 255))
    total_h = pad
    heights = []
    if title:
        _, th = _wh(tmp, title, title_font); heights.append(("t", title, th)); total_h += th + 4
    for ln in lines:
        _, lh = _wh(tmp, ln or " ", body_font); heights.append(("b", ln, lh)); total_h += lh + 2
    total_h += pad
    img = Image.new("L", (width, total_h), 255)
    d = ImageDraw.Draw(img)
    y = pad
    for kind, text, h in heights:
        font = title_font if kind == "t" else body_font
        tw, _ = _wh(d, text or " ", font)
        x = 0 if align == "left" else (width - tw) // 2 if align == "center" else width - tw
        d.text((max(0, x), y), text or "", fill=0, font=font)
        y += h + (4 if kind == "t" else 2)
    return _image_to_rows(img, width)


# ── capabilities ────────────────────────────────────────────────────────────────
@capability(
    "printer.status", http_method="GET", http_path="/printer/status",
    http_tags=["printer"], memory="off", silent=True,
    description="Thermal-printer status: whether a device is connected + which node, "
                "plus whether Pillow (needed for image/nice-font/label printing) is "
                "available in Vera's env. Output: {ok, connected, device, pil, ...}.",
)
async def cap_status(trace_id=None) -> Dict:
    dev = _find_device()
    pil, pilver = False, ""
    try:
        import PIL
        pil, pilver = True, getattr(PIL, "__version__", "")
    except Exception:
        pass
    return {"ok": True, "connected": bool(dev), "device": dev or None,
            "pil": pil, "pil_version": pilver, "python": sys.executable,
            "width": DEFAULT_WIDTH}


@capability(
    "printer.print.raw", http_method="POST", http_path="/printer/print/raw",
    http_tags=["printer"], memory="off",
    description="Send raw ESC/POS to the thermal printer. Provide ONE of: data (str, "
                "plain text), b64 (base64 bytes), hex (hex bytes). cut (bool=True) "
                "appends a full cut. Output: {ok, device, bytes}.",
)
async def cap_print_raw(data: str = "", b64: str = "", hex: str = "",
                        cut: bool = True, trace_id=None) -> Dict:
    if b64:
        payload = base64.b64decode(b64)
    elif hex:
        payload = bytes.fromhex(hex.replace(" ", ""))
    elif data:
        payload = INIT + data.encode("cp437", "replace")
        if cut:
            payload += b"\n\n\n" + CUT
    else:
        return {"ok": False, "error": "provide data | b64 | hex"}
    r = _write(payload)
    if r.get("ok"):
        await emit_event({"type": "printer.printed", "kind": "raw", "bytes": r.get("bytes")})
    return r


@capability(
    "printer.print.text", http_method="POST", http_path="/printer/print/text",
    http_tags=["printer"], memory="off",
    description="Print text with the printer's BUILT-IN font (no Pillow needed). "
                "Inputs: text (str!), align (left|center|right), width (int 1-8), "
                "height (int 1-8) size multipliers, bold (bool), cut (bool=True). "
                "For arbitrary TTF fonts/sizes use printer.print.nice. Output: {ok,...}.",
)
async def cap_print_text(text: str = "", align: str = "left", width: int = 1,
                         height: int = 1, bold: bool = False, cut: bool = True,
                         trace_id=None) -> Dict:
    if not text:
        return {"ok": False, "error": "text required"}
    r = _write(text_job(text, align=align, width=width, height=height, bold=bold, cut=cut))
    if r.get("ok"):
        await emit_event({"type": "printer.printed", "kind": "text", "bytes": r.get("bytes")})
    return r


@capability(
    "printer.print.nice", http_method="POST", http_path="/printer/print/nice",
    http_tags=["printer"], memory="off",
    description="Print text rendered with a TrueType font at any point size (Pillow) -- "
                "nicer than the built-in font. Inputs: text (str!, newlines allowed), "
                "font_size (int=28), align (left|center|right), title (str -- bold "
                "header), width_px (int=384/576 head width), cut (bool=True).",
)
async def cap_print_nice(text: str = "", font_size: int = 28, align: str = "left",
                         title: str = "", width_px: int = 0, cut: bool = True,
                         trace_id=None) -> Dict:
    if not (text or title):
        return {"ok": False, "error": "text or title required"}
    width = int(width_px) or DEFAULT_WIDTH
    try:
        w, h, rows = _render_text_image((text or "").split("\n"), width, font_size,
                                        align=align, title=(title or None))
    except ImportError:
        return {"ok": False, "error": "Pillow not available in Vera's env -- "
                "printer.print.text (built-in font) works without it"}
    r = _write(raster_job(w, h, rows, cut=cut))
    if r.get("ok"):
        await emit_event({"type": "printer.printed", "kind": "nice", "bytes": r.get("bytes")})
    return r


@capability(
    "printer.print.image", http_method="POST", http_path="/printer/print/image",
    http_tags=["printer"], memory="off",
    description="Print an image (dithered to 1-bpp, scaled to head width). Provide ONE "
                "of: image_b64 (base64), path (server path), url (http). width_px "
                "(int=head width), cut (bool=True). Needs Pillow. Output: {ok,...}.",
)
async def cap_print_image(image_b64: str = "", path: str = "", url: str = "",
                          width_px: int = 0, cut: bool = True, trace_id=None) -> Dict:
    width = int(width_px) or DEFAULT_WIDTH
    try:
        Image, _, _ = _pil()
        import io
        if image_b64:
            img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
        elif path:
            img = Image.open(path)
        elif url:
            import urllib.request
            img = Image.open(io.BytesIO(urllib.request.urlopen(url, timeout=20).read()))
        else:
            return {"ok": False, "error": "provide image_b64 | path | url"}
    except ImportError:
        return {"ok": False, "error": "Pillow not available in Vera's env"}
    except Exception as e:
        return {"ok": False, "error": "could not load image: %s" % e}
    w, h, rows = _image_to_rows(img, width)
    r = _write(raster_job(w, h, rows, cut=cut))
    if r.get("ok"):
        await emit_event({"type": "printer.printed", "kind": "image", "bytes": r.get("bytes")})
    return r


@capability(
    "printer.print.label", http_method="POST", http_path="/printer/print/label",
    http_tags=["printer"], memory="off",
    description="Print a composed label: a bold title + body lines, optionally with a "
                "small image on top. Inputs: title (str), lines (csv/list), image_b64 "
                "(optional), font_size (int=24), align (left|center), width_px, "
                "cut (bool=True). Falls back to built-in-font text if Pillow is absent.",
)
async def cap_print_label(title: str = "", lines="", image_b64: str = "",
                          font_size: int = 24, align: str = "center", width_px: int = 0,
                          cut: bool = True, trace_id=None) -> Dict:
    if isinstance(lines, str):
        body = [x for x in lines.split("\n")] if "\n" in lines else \
               ([x.strip() for x in lines.split(",")] if lines else [])
    else:
        body = [str(x) for x in (lines or [])]
    width = int(width_px) or DEFAULT_WIDTH
    try:
        parts = []
        if image_b64:
            Image, _, _ = _pil(); import io
            iw, ih, irows = _image_to_rows(
                Image.open(io.BytesIO(base64.b64decode(image_b64))), width)
            parts.append(raster_job(iw, ih, irows, cut=False, feed=1))
        tw, th, trows = _render_text_image(body, width, font_size, align=align,
                                           title=(title or None))
        parts.append(raster_job(tw, th, trows, cut=cut))
        r = _write(b"".join(parts))
    except ImportError:
        # graceful degrade: built-in font
        txt = ((title + "\n") if title else "") + "\n".join(body)
        r = _write(text_job(txt, align=align, width=2 if title else 1, height=1, cut=cut))
    if r.get("ok"):
        await emit_event({"type": "printer.printed", "kind": "label", "bytes": r.get("bytes")})
    return r


@capability(
    "printer.notify", http_method="POST", http_path="/printer/notify",
    http_tags=["printer"], memory="off",
    description="Print a formatted NOTIFICATION -- Vera's important-work/update channel "
                "on paper. Inputs: title (str), body (str), level (info|warn|alert). "
                "Registered as the 'printer' delivery channel too. Output: {ok,...}.",
)
async def cap_notify(title: str = "", body: str = "", level: str = "info",
                     trace_id=None) -> Dict:
    mark = {"info": "*", "warn": "!!", "alert": "###"}.get((level or "info").lower(), "*")
    head = "%s VERA %s %s" % (mark, (level or "info").upper(), mark)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    # prefer a nice rendered notification; fall back to built-in font
    try:
        w, h, rows = _render_text_image(
            [stamp, ""] + (body or "").split("\n"), DEFAULT_WIDTH, 26,
            align="left", title=(title or head))
        r = _write(raster_job(w, h, rows, cut=True))
        kind = "nice"
    except ImportError:
        txt = "%s\n%s\n%s\n\n%s" % (head, (title or ""), stamp, body or "")
        r = _write(text_job(txt, align="left", width=1, height=1, cut=True))
        kind = "text"
    if r.get("ok"):
        await emit_event({"type": "printer.notified", "level": level, "kind": kind})
    return r


# register the printer as a delivery channel so Vera can route notifications to it
try:
    from Vera.vera import delivery as _delivery
    _delivery.register_channel(
        "printer", label="Thermal printer", cap="printer.notify",
        default_format="text", needs_target=False, source="printer")
except Exception as _e:  # delivery module optional / may change shape
    pass
