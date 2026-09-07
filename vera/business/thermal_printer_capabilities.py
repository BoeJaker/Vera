"""
thermal_printer_capabilities.py — USB thermal printer + serial forwarding
==========================================================================

A **self-contained** printing subsystem, deliberately separate from the Business
UI but surfaced there as an element (``<vera-thermal-printer>``). It turns any
ESC/POS USB thermal printer into a Vera capability so an agent (or the operator)
can print receipts, packing slips, address labels for eBay/Etsy sales, or ad-hoc
notes on thermal paper.

Three transports, one command model — every ``print.*`` capability builds ESC/POS
bytes and then routes them:

  • **server_serial** — the printer is plugged into the *server*. We write the
    bytes straight to a serial/USB COM port with ``pyserial`` (optional dep;
    guarded — the caps still return the bytes if it is missing).
  • **webserial** — the printer is plugged into a *web client*. The server just
    returns the ESC/POS bytes (base64); the ``<vera-thermal-printer>`` element
    writes them to the printer over the browser's Web Serial API. This is the
    "serial/USB forwarding from web clients" path.
  • **mesh** — the printer hangs off a USB-enabled **ESP32 in the mesh**. We
    forward the bytes via ``mesh.send(node_id, "serial_write", {data_b64})``;
    the firmware writes them to its UART/USB-serial. (Firmware job added in
    ``mesh/firmware/micropython/main.py``.)

Printers are named + configured once (``print.printer.upsert``) so both agents
and the UI address them by a friendly id; the chosen transport decides routing.

``pyserial`` is imported lazily and never at module load, so this module loads
cleanly on a server without it.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import io
import json
import logging
import os
import time
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.print")

# Cheap USB thermal printers overrun on a single large bulk write (dmesg
# "nonzero write bulk status received: -108" -> the device USB-resets); every
# server transport paces the bytes in _CHUNK-sized slices with a short gap.
_CHUNK = int(os.environ.get("VERA_PRINTER_CHUNK", "512"))
_PACE = float(os.environ.get("VERA_PRINTER_PACE", "0.012"))

# Pure, app-free transport helpers (unit-tested in tests/test_print_transport_core.py).
from Vera.vera.business.print_transport_core import (
    is_raw_lp as _is_raw_lp,
    find_server_device as _find_server_device,
    paced_chunks as _paced_chunks_raw,
)


def _paced_chunks(data: bytes):
    return _paced_chunks_raw(data, _CHUNK)


# Raw ESC/POS raster framing lives in the shared, pure, unit-tested escpos_core;
# this module renders images/fonts to 1-bpp rows (Pillow) and hands them there.
try:
    from Vera.vera.printer.escpos_core import raster_job as _raster_job
except Exception:                                   # pragma: no cover
    _raster_job = None

# Line fitting also lives in escpos_core (pure + unit-tested). Paper does not
# re-flow: any line not wrapped to the head width here is clipped at the edge.
try:
    from Vera.vera.printer.escpos_core import (
        wrap_measured as _wrap_measured, wrap_cols as _wrap_cols,
    )
except Exception:                                   # pragma: no cover
    _wrap_measured = _wrap_cols = None

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, register_ui, enum_schema, schedule, CAPABILITY_REGISTRY,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.print").warning("thermal printer caps unavailable: %s", e)
    _CAP_AVAILABLE = False


TRANSPORTS = ["server_serial", "server_usb", "webserial", "mesh"]
_SCHEMA_READY = False


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ─────────────────────────────────────────────────────────────────────────────
# ESC/POS command builder — a small, well-behaved subset that covers the common
# 58/80 mm thermal printers (Epson TM-T, Xprinter, GOOJPRT, etc.).
# ─────────────────────────────────────────────────────────────────────────────

ESC = b"\x1b"
GS  = b"\x1d"

_INIT    = ESC + b"@"                    # initialise
_CUT     = GS + b"V\x00"                 # full cut
_ALIGN   = {"left": ESC + b"a\x00", "center": ESC + b"a\x01", "right": ESC + b"a\x02"}
_BOLD_ON = ESC + b"E\x01"
_BOLD_OFF= ESC + b"E\x00"
_FEED3   = b"\n\n\n"


def _size(w: int, h: int) -> bytes:
    """GS ! n — character magnification (width/height 1..8)."""
    w = max(1, min(8, int(w))); h = max(1, min(8, int(h)))
    n = ((w - 1) << 4) | (h - 1)
    return GS + b"!" + bytes([n])


def _text_line(s: str) -> bytes:
    return s.encode("cp437", errors="replace") + b"\n"


def _barcode(data: str, kind: str = "CODE128") -> bytes:
    """GS k — a couple of common symbologies; height + HRI set first."""
    out = GS + b"h\x50"          # height 80 dots
    out += GS + b"H\x02"         # HRI below barcode
    out += GS + b"w\x02"         # module width
    d = data.encode("ascii", errors="replace")
    if kind.upper() == "CODE39":
        out += GS + b"k\x04" + d + b"\x00"
    else:  # CODE128 (function-code B)
        payload = b"{B" + d
        out += GS + b"k\x49" + bytes([len(payload)]) + payload
    return out + b"\n"


def _qr(data: str, module: int = 6) -> bytes:
    """GS ( k — QR model 2, store + print."""
    d = data.encode("utf-8", errors="replace")
    module = max(1, min(16, int(module)))
    out  = GS + b"(k\x04\x00\x31\x41\x32\x00"          # model 2
    out += GS + b"(k\x03\x00\x31\x43" + bytes([module])  # module size
    out += GS + b"(k\x03\x00\x31\x45\x30"              # error correction L
    pl = len(d) + 3
    out += GS + b"(k" + bytes([pl & 0xff, (pl >> 8) & 0xff]) + b"\x31\x50\x30" + d  # store
    out += GS + b"(k\x03\x00\x31\x51\x30"              # print
    return out + b"\n"


def build_text(text: str, *, align: str = "left", bold: bool = False,
               width: int = 1, height: int = 1, cut: bool = True,
               title: str = "", cols: int = 32) -> bytes:
    """Built-in-font text. `cols` is the paper's character width (32 for 58 mm,
    48 for 80 mm); lines are wrapped to it because not every printer soft-wraps
    an over-long line -- plenty just drop the tail."""
    body_cols = max(8, int(cols or 32) // max(1, int(width)))
    out = bytearray(_INIT)
    if title:
        out += _ALIGN["center"] + _BOLD_ON + _size(2, 2)
        # The title prints at 2x magnification, so it fits half the columns.
        for ln in (_wrap_cols(title, max(4, int(cols or 32) // 2))
                   if _wrap_cols else [title]):
            out += _text_line(ln)
        out += _size(1, 1) + _BOLD_OFF + _ALIGN["left"] + b"\n"
    out += _ALIGN.get(align, _ALIGN["left"])
    if bold:
        out += _BOLD_ON
    if width > 1 or height > 1:
        out += _size(width, height)
    for ln in (_wrap_cols(text, body_cols) if _wrap_cols
               else (text or "").split("\n")):
        out += _text_line(ln)
    out += _size(1, 1) + _BOLD_OFF + _ALIGN["left"]
    out += _FEED3
    if cut:
        out += _CUT
    return bytes(out)


def build_receipt(spec: dict, cols: int = 32) -> bytes:
    """spec = {header, subheader, items:[{name, qty, price}], subtotal, tax,
    total, currency, footer, qr, barcode, order_id}.
    `cols` is the paper's character width (32 for 58 mm, 48 for 80 mm)."""
    cur = spec.get("currency", "")
    def money(v):
        try: return f"{cur}{float(v):,.2f}"
        except Exception: return str(v)
    W = max(8, int(cols or 32))   # 58 mm ≈ 32 cols at font A, 80 mm ≈ 48
    def row(left, right):
        left = str(left); right = str(right)
        pad = max(1, W - len(left) - len(right))
        return (left + " " * pad + right)[:W]

    out = bytearray(_INIT)
    if spec.get("header"):
        out += _ALIGN["center"] + _BOLD_ON + _size(2, 2)
        out += _text_line(str(spec["header"]))
        out += _size(1, 1) + _BOLD_OFF
    if spec.get("subheader"):
        out += _ALIGN["center"] + _text_line(str(spec["subheader"]))
    out += _ALIGN["left"] + _text_line("-" * W)
    for it in spec.get("items", []) or []:
        name = str(it.get("name", "")); qty = it.get("qty", 1)
        price = it.get("price", 0)
        try: line_total = float(price) * float(qty)
        except Exception: line_total = price
        out += _text_line(name[:W])
        out += _text_line(row(f"  {qty} x {money(price)}", money(line_total)))
    out += _text_line("-" * W)
    if spec.get("subtotal") is not None:
        out += _text_line(row("Subtotal", money(spec["subtotal"])))
    if spec.get("tax") is not None:
        out += _text_line(row("Tax", money(spec["tax"])))
    if spec.get("total") is not None:
        out += _BOLD_ON + _size(1, 2)
        out += _text_line(row("TOTAL", money(spec["total"])))
        out += _size(1, 1) + _BOLD_OFF
    if spec.get("order_id"):
        out += b"\n" + _ALIGN["center"] + _text_line(f"Order {spec['order_id']}")
    if spec.get("barcode"):
        out += _ALIGN["center"] + _barcode(str(spec["barcode"]))
    if spec.get("qr"):
        out += _ALIGN["center"] + _qr(str(spec["qr"]))
    if spec.get("footer"):
        out += _ALIGN["center"] + b"\n" + _text_line(str(spec["footer"]))
    out += _ALIGN["left"] + _FEED3 + _CUT
    return bytes(out)


def _fit(text, cols):
    """Wrap one string to `cols` columns -> list of lines. A truncated address
    line is a mis-delivered parcel, so nothing here may run off the paper."""
    if _wrap_cols is None:
        return str(text or "").split("\n")
    return _wrap_cols(str(text or ""), max(8, int(cols))) or [""]


def build_label(spec: dict, cols: int = 32) -> bytes:
    """A shipping/address label. spec = {to:[lines], from:[lines], ref,
    barcode, note}."""
    W = max(8, int(cols or 32))
    out = bytearray(_INIT)
    if spec.get("from"):
        out += _ALIGN["left"] + _text_line("FROM:")
        for ln in spec["from"]:
            for w in _fit("  " + str(ln), W):
                out += _text_line(w)
        out += b"\n"
    out += _ALIGN["left"] + _BOLD_ON + _text_line("SHIP TO:") + _BOLD_OFF
    out += _size(1, 2)
    for ln in spec.get("to", []) or []:
        for w in _fit(ln, W):            # double height, still full width
            out += _text_line(w)
    out += _size(1, 1)
    if spec.get("ref"):
        out += b"\n"
        for w in _fit("Ref: " + str(spec["ref"]), W):
            out += _text_line(w)
    if spec.get("barcode"):
        out += _ALIGN["center"] + _barcode(str(spec["barcode"])) + _ALIGN["left"]
    if spec.get("note"):
        out += b"\n"
        for w in _fit(spec["note"], W):
            out += _text_line(w)
    out += _FEED3 + _CUT
    return bytes(out)


def build_item_label(spec: dict, cols: int = 32) -> bytes:
    """An INTERNAL inventory label. Two modes:
      • 'sticker' — compact stick-on: title, price@location, CODE128 of the SKU.
        Deliberately one line per field — it is trimmed to fit, not wrapped.
      • 'slip'    — a fuller insert to pack in the box: title, console/year/edition,
                    condition/grade, price, location, notes, then the barcode.
                    Wrapped to the paper, so nothing is lost off the edge.
    spec = {sku, title, price, currency, condition, grade, completeness, console,
            year, edition, region, location, note, store, mode}.
    `cols` is the paper's character width (32 for 58 mm, 48 for 80 mm)."""
    mode = spec.get("mode", "sticker")
    W = max(8, int(cols or 32))
    cur = spec.get("currency", "")
    def money(v):
        try: return f"{cur}{float(v):,.2f}"
        except Exception: return str(v or "")
    sku = str(spec.get("sku") or "")
    out = bytearray(_INIT)
    if mode == "slip":
        if spec.get("store"):
            out += _ALIGN["center"] + _BOLD_ON
            for w in _fit(spec["store"], W):
                out += _text_line(w)
            out += _BOLD_OFF
        out += _ALIGN["center"] + _BOLD_ON + _size(1, 2)
        for w in _fit(str(spec.get("title", ""))[:120], W):
            out += _text_line(w)
        out += _size(1, 1) + _BOLD_OFF
        meta = " / ".join(str(x) for x in [spec.get("console"), spec.get("year"),
                          spec.get("edition"), spec.get("region")] if x)
        if meta:
            out += _ALIGN["center"]
            for w in _fit(meta, W):
                out += _text_line(w)
        out += _ALIGN["left"] + _text_line("-" * W)
        cond = " ".join(str(x) for x in [spec.get("condition"),
                        (f"grade {spec.get('grade')}" if spec.get("grade") else ""),
                        spec.get("completeness")] if x)
        if cond:
            for w in _fit("Condition: " + cond, W):
                out += _text_line(w)
        if spec.get("location"):
            for w in _fit("Location:  " + str(spec["location"]), W):
                out += _text_line(w)
        if spec.get("price") not in (None, ""):
            out += (_BOLD_ON + _size(1, 2) + _text_line("Price: " + money(spec.get("price")))
                    + _size(1, 1) + _BOLD_OFF)
        if spec.get("note"):
            for w in _fit(str(spec["note"])[:200], W):
                out += _text_line(w)
        out += _text_line("-" * W)
        if sku:
            out += _ALIGN["center"] + _barcode(sku)
        out += _ALIGN["left"] + _FEED3 + _CUT
    else:  # sticker
        t = str(spec.get("title", ""))
        if t:
            out += _ALIGN["center"] + _BOLD_ON + _text_line(t[:W]) + _BOLD_OFF
        line = "   ".join(x for x in [
            (money(spec.get("price")) if spec.get("price") not in (None, "") else ""),
            ("@" + str(spec["location"]) if spec.get("location") else "")] if x)
        if line:
            out += _ALIGN["center"] + _text_line(line)
        if sku:
            out += _ALIGN["center"] + _barcode(sku)
        out += _ALIGN["left"] + _FEED3 + _CUT
    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# Printer registry (sqlite)
# ─────────────────────────────────────────────────────────────────────────────

def _pil():
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    return Image, ImageDraw, ImageFont


def _font(size, bold=False):
    _, _, ImageFont = _pil()
    for p in (_FONT_BOLD if bold else []) + _FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, max(6, int(size)))
    return ImageFont.load_default()


def _image_to_rows(img, width):
    """PIL image -> (w, h, [row_bytes]) 1-bpp, MSB-first, 1=black (Floyd-Steinberg)."""
    if img.mode != "L":
        img = img.convert("L")
    if img.width != width:
        h = max(1, int(img.height * width / img.width))
        img = img.resize((width, h))
    img = img.convert("1")
    w, h = img.size
    bpr = (w + 7) // 8
    px = img.load()
    rows = []
    for y in range(h):
        row = bytearray(bpr)
        for x in range(w):
            if px[x, y] == 0:
                row[x >> 3] |= (0x80 >> (x & 7))
        rows.append(bytes(row))
    return w, h, rows


def _render_text_image(lines, width, font_size, align="left", title=None):
    """Render an optional bold title + body lines to a 1-bpp bitmap for raster.

    Lines are word-wrapped to the head width FIRST. Without that, anything wider
    than the paper -- a news headline, a chat reply, a calendar entry with a
    location -- is drawn past the right edge of the bitmap and simply lost."""
    Image, ImageDraw, _ = _pil()
    body_font = _font(font_size, bold=True)
    title_font = _font(int(font_size * 1.6), bold=True) if title else None
    pad = 6

    def _wh(draw, text, font):
        try:
            b = draw.textbbox((0, 0), text, font=font); return b[2] - b[0], b[3] - b[1]
        except Exception:
            return draw.textsize(text, font=font)

    tmp = ImageDraw.Draw(Image.new("L", (width, 10), 255))
    usable = max(32, int(width) - 2 * pad)
    title_lines = [title] if title else []
    if _wrap_measured is not None:
        lines = _wrap_measured(lines, lambda t: _wh(tmp, t, body_font)[0], usable)
        if title:
            # Wrapped at the TITLE's own (larger) metrics, and every resulting
            # row stays a title row — a long header must not half-demote itself
            # into body text.
            title_lines = _wrap_measured([title], lambda t: _wh(tmp, t, title_font)[0],
                                         usable, hang_indent=False)

    total_h, heights = pad, []
    for tl in title_lines:
        _, th = _wh(tmp, tl, title_font); heights.append(("t", tl, th)); total_h += th + 4
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


def _width_for(printer, width_px=0):
    if width_px:
        return int(width_px)
    mm = int((printer or {}).get("width_mm", 58) or 58)
    return 576 if mm >= 80 else 384


def _cols_for(printer, width_px=0):
    """Character columns of the built-in font A (12 dots wide) on this paper:
    32 for 58 mm, 48 for 80 mm."""
    return max(8, _width_for(printer, width_px) // 12)


def build_image_raster(image_bytes, width_px, cut=True, align="center"):
    """Decode any image -> 1-bpp rows scaled to head width -> GS v 0 raster."""
    if _raster_job is None:
        raise RuntimeError("escpos_core.raster_job unavailable")
    Image, _, _ = _pil()
    img = Image.open(io.BytesIO(image_bytes))
    w, h, rows = _image_to_rows(img, int(width_px))
    return _raster_job(w, h, rows, align=align, cut=cut)


def build_nice_text(lines, width_px, font_size=28, align="left", title="", cut=True):
    if _raster_job is None:
        raise RuntimeError("escpos_core.raster_job unavailable")
    w, h, rows = _render_text_image(lines, int(width_px), int(font_size),
                                    align=align, title=(title or None))
    return _raster_job(w, h, rows, align="left", cut=cut)


def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS print_printers (
                id          TEXT PRIMARY KEY,
                name        TEXT,
                transport   TEXT,
                port        TEXT,
                baud        INTEGER DEFAULT 9600,
                node_id     TEXT,
                width_mm    INTEGER DEFAULT 58,
                is_default  INTEGER DEFAULT 0,
                config      TEXT,
                created_at  TEXT,
                updated_at  TEXT
            );
        """)
        conn.commit()
        try:
            empty = conn.execute("SELECT COUNT(*) FROM print_printers").fetchone()[0] == 0
        except Exception:
            empty = False
        if empty:
            dev = _find_server_device()
            if dev:
                import uuid as _uuid
                _now = now_iso()
                conn.execute(
                    "INSERT INTO print_printers (id,name,transport,port,baud,node_id,"
                    "width_mm,is_default,config,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"prn_{_uuid.uuid4().hex[:10]}", f"Server USB ({dev})",
                     "server_usb", dev, 9600, "", 58, 1, "{}", _now, _now))
                conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)

def _printer_out(row) -> dict:
    d = dict(row)
    try: d["config"] = json.loads(d.get("config") or "{}")
    except Exception: d["config"] = {}
    return d

def _db_list_printers() -> List[dict]:
    conn = _sqlite_conn()
    try:
        return [_printer_out(r) for r in
                conn.execute("SELECT * FROM print_printers ORDER BY name").fetchall()]
    finally:
        conn.close()

def _db_get_printer(pid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM print_printers WHERE id=?", (pid,)).fetchone()
        if not r:
            r = conn.execute("SELECT * FROM print_printers WHERE is_default=1 "
                             "LIMIT 1").fetchone() if pid in ("", "default") else None
        return _printer_out(r) if r else None
    finally:
        conn.close()

def _db_upsert_printer(fields: dict) -> dict:
    import uuid as _uuid
    conn = _sqlite_conn()
    try:
        pid = fields.get("id") or f"prn_{_uuid.uuid4().hex[:10]}"
        existing = conn.execute("SELECT * FROM print_printers WHERE id=?", (pid,)).fetchone()
        base = _printer_out(existing) if existing else {}
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        if fields.get("is_default"):
            conn.execute("UPDATE print_printers SET is_default=0")
        conn.execute(
            "INSERT OR REPLACE INTO print_printers (id,name,transport,port,baud,"
            "node_id,width_mm,is_default,config,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, merged.get("name", ""), merged.get("transport", "webserial"),
             merged.get("port", ""), int(merged.get("baud", 9600) or 9600),
             merged.get("node_id", ""), int(merged.get("width_mm", 58) or 58),
             int(bool(merged.get("is_default", 0))),
             json.dumps(merged.get("config") or {}),
             base.get("created_at") or now, now))
        conn.commit()
        return _db_get_printer(pid)
    finally:
        conn.close()

def _db_delete_printer(pid: str) -> bool:
    conn = _sqlite_conn()
    try:
        cur = conn.execute("DELETE FROM print_printers WHERE id=?", (pid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Serial availability + write (pyserial optional)
# ─────────────────────────────────────────────────────────────────────────────

def _pyserial():
    try:
        import serial  # type: ignore
        return serial
    except Exception:
        return None

def _list_serial_ports() -> List[dict]:
    try:
        from serial.tools import list_ports  # type: ignore
    except Exception:
        return []
    out = []
    for p in list_ports.comports():
        out.append({"port": p.device, "description": p.description,
                    "hwid": getattr(p, "hwid", "")})
    return out

def _raw_write(port: str, data: bytes) -> dict:
    """Chunked, reconnect-safe write to a raw usblp char device (/dev/vera-printer,
    /dev/usb/lp*). Re-resolves the device and retries once if it reset mid-write."""
    err = "no server thermal printer found"
    for attempt in (1, 2):
        dev = port if (port and os.path.exists(port)) else _find_server_device()
        if not dev:
            if attempt == 1:
                time.sleep(1.5); continue
            break
        try:
            with open(dev, "wb", buffering=0) as f:
                for chunk, more in _paced_chunks(data):
                    f.write(chunk); f.flush()
                    if _PACE and more:
                        time.sleep(_PACE)
            return {"ok": True, "wrote": len(data), "device": dev}
        except PermissionError:
            return {"ok": False, "error": f"permission denied on {dev} "
                    "(udev: GROUP=plugdev, MODE=0660)"}
        except OSError as e:
            err = f"raw write {dev} failed: {e}"
            if attempt == 1:
                time.sleep(1.5); continue
            break
    return {"ok": False, "error": err}


def _serial_write(port: str, baud: int, data: bytes) -> dict:
    # A usblp char device configured as a "serial" printer uses the raw path.
    if _is_raw_lp(port):
        return _raw_write(port, data)
    serial = _pyserial()
    if not serial:
        return {"ok": False, "error": "pyserial not installed on server (pip install pyserial)"}
    if not port:
        return {"ok": False, "error": "no serial port configured"}
    try:
        with serial.Serial(port, int(baud or 9600), timeout=2) as ser:
            for chunk, more in _paced_chunks(data):
                ser.write(chunk); ser.flush()
                if _PACE and more:
                    time.sleep(_PACE)
        return {"ok": True, "wrote": len(data)}
    except Exception as e:
        return {"ok": False, "error": f"serial write failed: {e}"}


async def _route(printer: Optional[dict], data: bytes) -> dict:
    """Send ESC/POS bytes via the printer's transport. Always includes the
    base64 payload so a webserial client can print regardless."""
    b64 = base64.b64encode(data).decode("ascii")
    result = {"escpos_b64": b64, "bytes": len(data), "transport": None, "routed": False}
    if not printer:
        result["transport"] = "webserial"
        result["note"] = "no printer selected — bytes returned for a Web Serial client"
        return result
    result["transport"] = printer.get("transport")
    tr = printer.get("transport")
    if tr == "server_usb":
        w = await _run(_raw_write, printer.get("port", ""), data)
        result["routed"] = bool(w.get("ok")); result.update(w)
    elif tr == "server_serial":
        w = await _run(_serial_write, printer.get("port", ""),
                       printer.get("baud", 9600), data)
        result["routed"] = bool(w.get("ok")); result.update(w)
    elif tr == "mesh":
        import sys
        mesh = sys.modules.get("mesh_capabilities")
        node = printer.get("node_id", "")
        if mesh and hasattr(mesh, "cap_mesh_send") and node:
            try:
                r = await mesh.cap_mesh_send(
                    node_id=node, type="serial_write",
                    payload={"data_b64": b64, "baud": printer.get("baud", 9600)})
                result["routed"] = bool(r.get("ok")); result["mesh"] = r
            except Exception as e:
                result["error"] = f"mesh forward failed: {e}"
        else:
            result["error"] = "mesh transport needs a node_id and the mesh module"
    else:  # webserial — client prints
        result["note"] = "returned for the Web Serial client to write"
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "print.status", http_method="GET", http_path="/print/status",
        http_tags=["print"], memory="off", silent=True,
        description="Thermal-printer subsystem status: whether server-side serial "
                    "is available (pyserial), detected serial ports, and configured "
                    "printers. Output: {pyserial, ports:[...], printers:[...]}.")
    async def cap_print_status(trace_id=None):
        await _ensure_schema()
        serial_ok = _pyserial() is not None
        ports = await _run(_list_serial_ports) if serial_ok else []
        printers = await _run(_db_list_printers)
        server_device = await _run(_find_server_device)
        try:
            import PIL
            pil_ok, pil_ver = True, getattr(PIL, "__version__", "")
        except Exception:
            pil_ok, pil_ver = False, ""
        return {"pyserial": serial_ok, "ports": ports, "printers": printers,
                "transports": TRANSPORTS, "server_device": server_device or None,
                "pil": pil_ok, "pil_version": pil_ver}

    @capability(
        "print.printers", http_method="GET", http_path="/print/printers",
        http_tags=["print"], memory="off", silent=True,
        description="List configured printers. Output: {printers:[...]}.")
    async def cap_print_printers(trace_id=None):
        await _ensure_schema()
        return {"printers": await _run(_db_list_printers)}

    @capability(
        "print.printer.upsert", http_method="POST", http_path="/print/printer/upsert",
        http_tags=["print"],
        schema=enum_schema(transport=TRANSPORTS),
        description="Create or update a printer. Input: id (omit to create), "
                    "name (str!), transport (server_serial|webserial|mesh), "
                    "port (server COM/tty), baud (int, default 9600), "
                    "node_id (for mesh), width_mm (58|80), is_default (bool). "
                    "Output: {ok, printer}.")
    async def cap_print_printer_upsert(
        id: str = "", name: str = "", transport: str = "webserial", port: str = "",
        baud: int = 9600, node_id: str = "", width_mm: int = 58,
        is_default: bool = False, config: Dict = None, trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        p = await _run(_db_upsert_printer, {
            "id": id or None, "name": name or None, "transport": transport,
            "port": port or None, "baud": baud, "node_id": node_id or None,
            "width_mm": width_mm, "is_default": is_default, "config": config})
        return {"ok": True, "printer": p}

    @capability(
        "print.printer.delete", http_method="POST", http_path="/print/printer/delete",
        http_tags=["print"],
        description="Delete a printer by id. Input: id (str!). Output: {ok}.")
    async def cap_print_printer_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete_printer, id)
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "print.text", http_method="POST", http_path="/print/text",
        http_tags=["print"],
        schema=enum_schema(align=["left", "center", "right"]),
        description="Print plain text on a thermal printer. Input: text (str!), "
                    "printer_id (str — omit for default/webserial), title (str — "
                    "large centred header), align, bold (bool), width (int 1-8), "
                    "height (int 1-8), cut (bool default true). "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_text(
        text: str = "", printer_id: str = "", title: str = "", align: str = "left",
        bold: bool = False, width: int = 1, height: int = 1, cut: bool = True,
        trace_id=None):
        await _ensure_schema()
        if not text and not title:
            return {"error": "text or title required"}
        # Resolve the printer first: its paper width decides how many columns
        # the text is wrapped to.
        printer = await _run(_db_get_printer, printer_id) if printer_id else \
                  await _run(_db_get_printer, "default")
        data = build_text(text, align=align, bold=bold, width=width, height=height,
                          cut=cut, title=title, cols=_cols_for(printer))
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "text",
                          "message": f"printed {len(data)}B via {res.get('transport')}"})
        return {"ok": True, **res}

    @capability(
        "print.receipt", http_method="POST", http_path="/print/receipt",
        http_tags=["print"],
        description="Print a structured receipt / packing slip. Input: printer_id, "
                    "header (str), subheader, items (list of {name, qty, price}), "
                    "subtotal (float), tax (float), total (float), currency, footer, "
                    "order_id, barcode (str — printed as CODE128), qr (str). "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_receipt(
        printer_id: str = "", header: str = "", subheader: str = "",
        items: List = None, subtotal: float = None, tax: float = None,
        total: float = None, currency: str = "", footer: str = "",
        order_id: str = "", barcode: str = "", qr: str = "", trace_id=None):
        await _ensure_schema()
        spec = {"header": header, "subheader": subheader, "items": items or [],
                "subtotal": subtotal, "tax": tax, "total": total, "currency": currency,
                "footer": footer, "order_id": order_id, "barcode": barcode, "qr": qr}
        printer = await _run(_db_get_printer, printer_id or "default")
        data = build_receipt(spec, cols=_cols_for(printer))
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "receipt",
                          "message": f"receipt {order_id or ''} via {res.get('transport')}"})
        return {"ok": True, **res}

    @capability(
        "print.label", http_method="POST", http_path="/print/label",
        http_tags=["print"],
        description="Print an address / shipping label (eBay/Etsy sale). Input: "
                    "printer_id, to (list of address lines!), from_ (list of lines), "
                    "ref (str — order/tracking), barcode (str), note (str). "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_label(
        printer_id: str = "", to: List = None, from_: List = None,
        ref: str = "", barcode: str = "", note: str = "", trace_id=None):
        await _ensure_schema()
        if not to:
            return {"error": "to (address lines) required"}
        printer = await _run(_db_get_printer, printer_id or "default")
        data = build_label({"to": to, "from": from_ or [], "ref": ref,
                            "barcode": barcode, "note": note},
                           cols=_cols_for(printer))
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "label",
                          "message": f"label via {res.get('transport')}"})
        return {"ok": True, **res}

    @capability(
        "print.item_label", http_method="POST", http_path="/print/item_label",
        http_tags=["print"],
        schema=enum_schema(mode=["sticker", "slip"]),
        description="Print an INTERNAL inventory barcode label to a thermal printer. "
                    "'sticker' = a compact stick-on (title, price @ location, CODE128 "
                    "of the SKU). 'slip' = a fuller insert to pack in the box (title, "
                    "console/year/edition, condition/grade, price, location, notes, "
                    "barcode). The SKU barcode scans back to pull the exact unit up. "
                    "Input: sku (str! — the internal code), title, price, currency "
                    "(GBP), condition, grade, completeness, console, year, edition, "
                    "region, location, note, store, mode (sticker|slip), printer_id. "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_item_label(
        sku: str = "", title: str = "", price: float = None, currency: str = "GBP",
        condition: str = "", grade: float = None, completeness: str = "",
        console: str = "", year: str = "", edition: str = "", region: str = "",
        location: str = "", note: str = "", store: str = "", mode: str = "sticker",
        printer_id: str = "", trace_id=None):
        await _ensure_schema()
        if not (sku or title):
            return {"error": "sku or title required"}
        printer = await _run(_db_get_printer, printer_id or "default")
        data = build_item_label({
            "sku": sku, "title": title, "price": price, "currency": currency,
            "condition": condition, "grade": grade, "completeness": completeness,
            "console": console, "year": year, "edition": edition, "region": region,
            "location": location, "note": note, "store": store, "mode": mode},
            cols=_cols_for(printer))
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "item_label",
                          "message": f"{mode} label {sku} via {res.get('transport')}"})
        return {"ok": True, **res}

    @capability(
        "print.raw", http_method="POST", http_path="/print/raw",
        http_tags=["print"],
        description="Send raw ESC/POS bytes (base64) to a printer — for advanced/"
                    "custom command sequences. Input: data_b64 (str!), printer_id. "
                    "Output: {ok, bytes, transport, routed}.")
    async def cap_print_raw(data_b64: str = "", printer_id: str = "", trace_id=None):
        await _ensure_schema()
        if not data_b64:
            return {"error": "data_b64 required"}
        try:
            data = base64.b64decode(data_b64)
        except Exception as e:
            return {"error": f"bad base64: {e}"}
        printer = await _run(_db_get_printer, printer_id or "default")
        res = await _route(printer, data)
        return {"ok": True, **res}

    @capability(
        "print.image", http_method="POST", http_path="/print/image",
        http_tags=["print"],
        schema=enum_schema(align=["left", "center", "right"]),
        description="Print an IMAGE (chart, photo, logo, a WYSIWYG-composed canvas) on a "
                    "thermal printer -- dithered to 1-bpp and scaled to the head width, then "
                    "routed via the printer's transport (server/webserial/mesh). Provide ONE "
                    "of: image_b64 (base64 PNG/JPG), path (server file), url (http). "
                    "printer_id (omit for default), width_px (omit to derive from width_mm: "
                    "384 for 58mm, 576 for 80mm), align, cut (bool=true). "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_image(
        image_b64: str = "", path: str = "", url: str = "", printer_id: str = "",
        width_px: int = 0, align: str = "center", cut: bool = True, trace_id=None):
        await _ensure_schema()
        try:
            if image_b64:
                raw = base64.b64decode(image_b64)
            elif path:
                raw = _Path(path).read_bytes()
            elif url:
                import urllib.request
                raw = urllib.request.urlopen(url, timeout=20).read()
            else:
                return {"error": "provide image_b64 | path | url"}
        except Exception as e:
            return {"error": f"could not load image: {e}"}
        printer = await _run(_db_get_printer, printer_id or "default")
        width = _width_for(printer, width_px)
        try:
            data = await _run(lambda: build_image_raster(raw, width, cut=cut, align=align))
        except ImportError:
            return {"error": "Pillow not available on the server (needed for image printing)"}
        except Exception as e:
            return {"error": f"raster build failed: {e}"}
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "image",
                          "message": f"image {len(data)}B via {res.get('transport')}"})
        return {"ok": True, **res}

    @capability(
        "print.nice", http_method="POST", http_path="/print/nice",
        http_tags=["print"],
        schema=enum_schema(align=["left", "center", "right"]),
        description="Print text rendered with a TrueType font at any point size (nicer than "
                    "the built-in font), rasterised and routed via the printer's transport. "
                    "Input: text (str!, newlines allowed), title (str -- bold header), "
                    "font_size (int=28), align, printer_id, width_px, cut (bool=true). "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_nice(
        text: str = "", title: str = "", font_size: int = 28, align: str = "left",
        printer_id: str = "", width_px: int = 0, cut: bool = True, trace_id=None):
        await _ensure_schema()
        if not (text or title):
            return {"error": "text or title required"}
        printer = await _run(_db_get_printer, printer_id or "default")
        width = _width_for(printer, width_px)
        try:
            data = await _run(lambda: build_nice_text(
                (text or "").split(chr(10)), width, font_size, align=align,
                title=title, cut=cut))
        except ImportError:
            return {"error": "Pillow not available on the server"}
        except Exception as e:
            return {"error": f"render failed: {e}"}
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "nice",
                          "message": f"nice text {len(data)}B via {res.get('transport')}"})
        return {"ok": True, **res}

    # ── Element registration ─────────────────────────────────────────────────

    # ── Live feeds -> printer (schedule / dreams / news) + daily auto-print ────

    async def _invoke(cap_name, **kw):
        """Call another Vera capability in-process; returns its dict or {}."""
        try:
            c = CAPABILITY_REGISTRY.get(cap_name)
            fn = (c.get("raw") or c.get("func")) if c else None
            if not fn:
                return {}
            r = await fn(**kw)
            return r if isinstance(r, dict) else {}
        except Exception as e:
            log.debug("print _invoke %s: %s", cap_name, e)
            return {}

    def _fmt_schedule(brief):
        today = (brief or {}).get("today") or []
        lines = [(brief or {}).get("today_human") or "Today", ""]
        if not today:
            lines.append("(nothing scheduled)")
        for e in today:
            loc = ("   @ " + str(e["location"])) if e.get("location") else ""
            if e.get("all_day"):
                lines.append("- all day   " + str(e.get("title", "")) + loc)
            else:
                s = str(e.get("start", "")); en = str(e.get("end", ""))
                hm = s[11:16] if len(s) >= 16 else s
                hm2 = ("-" + en[11:16]) if len(en) >= 16 else ""
                lines.append("- " + hm + hm2 + "  " + str(e.get("title", "")) + loc)
        return chr(10).join(lines)

    def _fmt_dreams(journal):
        ent = (journal or {}).get("entries") or []
        lines = ["Dream digest", ""]
        for e in ent[-6:]:
            ts = str(e.get("ts", "")); when = ts[:16].replace("T", " ")
            lines.append("* " + str(e.get("title") or e.get("kind") or "entry")
                         + (("  (" + when + ")") if when else ""))
            if e.get("text"):
                lines.append(str(e["text"])[:400])
            lines.append("")
        return chr(10).join(lines)

    def _fetch_news(n=8):
        import urllib.request as _u
        try:
            req = _u.Request("https://hn.algolia.com/api/v1/search?tags=front_page",
                             headers={"User-Agent": "Vera-printer"})
            data = json.loads(_u.urlopen(req, timeout=12).read().decode("utf-8", "replace"))
            lines = ["News - HN front page", ""]
            for h in (data.get("hits") or [])[:n]:
                t = str(h.get("title") or "").strip()
                if t:
                    lines.append("* " + t)
            return chr(10).join(lines) if len(lines) > 2 else ""
        except Exception:
            return ""

    async def _print_feed(text, printer_id, preview, stage):
        if not text:
            return {"error": "nothing to print"}
        if preview:
            return {"ok": True, "text": text, "preview": True}
        printer = await _run(_db_get_printer, printer_id or "default")
        try:
            data = await _run(lambda: build_nice_text(text.split(chr(10)), _width_for(printer, 0),
                                                      26, align="left", cut=True))
        except Exception:
            data = build_text(text, align="left", cut=True, cols=_cols_for(printer))
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": stage,
                          "message": f"{stage} via {res.get('transport')}"})
        return {"ok": True, "text": text, **res}

    @capability("print.schedule", http_method="POST", http_path="/print/schedule",
                http_tags=["print"],
                description="Print (or preview) today's calendar schedule. Input: printer_id, "
                            "preview (bool -- return text without printing). Output: {ok, text, ...}.")
    async def cap_print_schedule(printer_id: str = "", preview: bool = False, trace_id=None):
        await _ensure_schema()
        return await _print_feed(_fmt_schedule(await _invoke("cal.assistant.briefing")),
                                 printer_id, preview, "schedule")

    @capability("print.dream_digest", http_method="POST", http_path="/print/dream_digest",
                http_tags=["print"],
                description="Print (or preview) a digest of recent dream journal entries. "
                            "Input: printer_id, preview (bool). Output: {ok, text, ...}.")
    async def cap_print_dream_digest(printer_id: str = "", preview: bool = False, trace_id=None):
        await _ensure_schema()
        return await _print_feed(_fmt_dreams(await _invoke("dream.director.journal")),
                                 printer_id, preview, "dream_digest")

    @capability("print.news", http_method="POST", http_path="/print/news",
                http_tags=["print"],
                description="Print (or preview) current news headlines (HN front page). "
                            "Input: printer_id, preview (bool), count (int=8). Output: {ok, text, ...}.")
    async def cap_print_news(printer_id: str = "", preview: bool = False, count: int = 8, trace_id=None):
        await _ensure_schema()
        text = await _run(_fetch_news, max(1, min(20, int(count or 8))))
        return await _print_feed(text or "", printer_id, preview, "news")

    def _daily_cfg_get():
        try:
            conn = _sqlite_conn()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS print_kv (k TEXT PRIMARY KEY, v TEXT)")
                r = conn.execute("SELECT v FROM print_kv WHERE k='daily'").fetchone()
                conn.commit()
                return json.loads(dict(r)["v"]) if r else {}
            finally:
                conn.close()
        except Exception:
            return {}

    def _daily_cfg_set(patch):
        cur = _daily_cfg_get(); cur.update(patch or {})
        try:
            conn = _sqlite_conn()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS print_kv (k TEXT PRIMARY KEY, v TEXT)")
                conn.execute("INSERT OR REPLACE INTO print_kv (k,v) VALUES ('daily',?)",
                             (json.dumps(cur),))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return cur

    @capability("print.config.get", http_method="GET", http_path="/print/config",
                http_tags=["print"], memory="off", silent=True,
                description="Get daily auto-print settings {enabled, hour, minute, items, printer_id}.")
    async def cap_print_config_get(trace_id=None):
        return {"config": await _run(_daily_cfg_get)}

    @capability("print.config.set", http_method="POST", http_path="/print/config/set",
                http_tags=["print"],
                description="Set daily auto-print. Input: enabled (bool), hour (0-23), minute (0-59), "
                            "items (csv of schedule,dreams,news), printer_id. When enabled, Vera prints "
                            "those feeds once at the chosen time each day.")
    async def cap_print_config_set(enabled: bool = None, hour: int = None, minute: int = None,
                                   items: str = None, printer_id: str = None, trace_id=None):
        patch = {}
        if enabled is not None: patch["enabled"] = bool(enabled)
        if hour is not None: patch["hour"] = max(0, min(23, int(hour)))
        if minute is not None: patch["minute"] = max(0, min(59, int(minute)))
        if items is not None: patch["items"] = [x.strip() for x in str(items).split(",") if x.strip()]
        if printer_id is not None: patch["printer_id"] = printer_id
        return {"ok": True, "config": await _run(_daily_cfg_set, patch)}

    _daily_state = {"last_day": None}

    async def _daily_print_tick():
        try:
            cfg = await _run(_daily_cfg_get)
            if not cfg.get("enabled"):
                return
            import datetime as _dt
            now = _dt.datetime.now()
            if now.hour != int(cfg.get("hour", 7)) or now.minute < int(cfg.get("minute", 0)):
                return
            day = now.strftime("%Y-%m-%d")
            if _daily_state.get("last_day") == day:
                return
            _daily_state["last_day"] = day
            pid = cfg.get("printer_id") or ""
            for it in (cfg.get("items") or ["schedule"]):
                if it == "schedule": await cap_print_schedule(printer_id=pid)
                elif it == "dreams": await cap_print_dream_digest(printer_id=pid)
                elif it == "news": await cap_print_news(printer_id=pid)
            log.info("daily auto-print: printed %s", cfg.get("items"))
        except Exception as e:
            log.debug("daily print tick: %s", e)

    try:
        schedule(_daily_print_tick, 60, name="daily_print", skip_in_sandbox=True, singleton=True)
    except Exception as _e:                        # pragma: no cover
        log.debug("daily print scheduler not registered: %s", _e)

    @capability("print.fabric", http_method="POST", http_path="/print/fabric",
                http_tags=["print"],
                description="Print (or preview) the most recent items from a Data-Fabric dataset "
                            "(news / RSS / collector feeds). Input: dataset_id (str!), limit (int=8), "
                            "printer_id, preview (bool). Output: {ok, text, ...}.")
    async def cap_print_fabric(dataset_id: str = "", limit: int = 8, printer_id: str = "",
                               preview: bool = False, trace_id=None):
        await _ensure_schema()
        if not dataset_id:
            return {"error": "dataset_id required"}
        q = await _invoke("fabric.query", dataset_id=dataset_id,
                          limit=max(1, min(30, int(limit or 8))))
        rows = (q or {}).get("results") or []
        lines = [str(dataset_id), ""]
        for it in rows:
            t = (str(it.get("text") or "").strip().replace(chr(10), " "))[:180]
            if t:
                lines.append("* " + t)
        text = chr(10).join(lines) if len(lines) > 2 else ""
        return await _print_feed(text, printer_id, preview, "fabric")

    # ── Notification subscriptions: control what auto-prints ──────────────────
    _SUBS_DEFAULT = {"master": True, "sources": {"system": True, "dreams": False,
                                                 "narrator": False, "chat": False,
                                                 "markets": False, "deals": False,
                                                 "fabric": False}}

    def _subs_get():
        try:
            conn = _sqlite_conn()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS print_kv (k TEXT PRIMARY KEY, v TEXT)")
                r = conn.execute("SELECT v FROM print_kv WHERE k='subs'").fetchone()
                conn.commit()
                if not r:
                    return json.loads(json.dumps(_SUBS_DEFAULT))
                d = json.loads(dict(r)["v"]) or {}
                out = json.loads(json.dumps(_SUBS_DEFAULT))
                if "master" in d:
                    out["master"] = bool(d["master"])
                out["sources"].update({k: bool(v) for k, v in (d.get("sources") or {}).items()})
                return out
            finally:
                conn.close()
        except Exception:
            return json.loads(json.dumps(_SUBS_DEFAULT))

    def _subs_set(patch):
        cur = _subs_get()
        if "master" in (patch or {}):
            cur["master"] = bool(patch["master"])
        if "sources" in (patch or {}):
            cur["sources"].update({k: bool(v) for k, v in (patch["sources"] or {}).items()})
        try:
            conn = _sqlite_conn()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS print_kv (k TEXT PRIMARY KEY, v TEXT)")
                conn.execute("INSERT OR REPLACE INTO print_kv (k,v) VALUES ('subs',?)", (json.dumps(cur),))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        return cur

    def _subs_allows(source):
        s = _subs_get()
        if not s.get("master", True):
            return False
        return bool((s.get("sources") or {}).get(source, source == "system"))

    @capability("print.subs.get", http_method="GET", http_path="/print/subs",
                http_tags=["print"], memory="off", silent=True,
                description="Get printer notification subscriptions {master, sources:{system,dreams,narrator,chat}}.")
    async def cap_print_subs_get(trace_id=None):
        return {"subs": await _run(_subs_get)}

    @capability("print.subs.set", http_method="POST", http_path="/print/subs/set",
                http_tags=["print"],
                description="Control what auto-prints to the thermal printer. Input: master (bool -- "
                            "master on/off), system / dreams / narrator / chat (bool, per-source). "
                            "Output: {ok, subs}.")
    async def cap_print_subs_set(master: bool = None, system: bool = None, dreams: bool = None,
                                 narrator: bool = None, chat: bool = None, markets: bool = None,
                                 deals: bool = None, fabric: bool = None, trace_id=None):
        patch = {}
        if master is not None: patch["master"] = bool(master)
        src = {}
        for k, v in (("system", system), ("dreams", dreams), ("narrator", narrator), ("chat", chat), ("markets", markets), ("deals", deals), ("fabric", fabric)):
            if v is not None: src[k] = bool(v)
        if src: patch["sources"] = src
        return {"ok": True, "subs": await _run(_subs_set, patch)}

    @capability("print.push", http_method="POST", http_path="/print/push",
                http_tags=["print"],
                schema=enum_schema(source=["system", "dreams", "narrator", "chat"]),
                description="Route a piece of output to the thermal printer IF the user has subscribed "
                            "that source (see print.subs) -- the single entry any subsystem (dream "
                            "director, narrator, chat, alerts) calls. Input: source, title, body, level, "
                            "printer_id, force (bool, ignore subscription). Output: {ok, printed, ...}.")
    async def cap_print_push(source: str = "system", title: str = "", body: str = "",
                             level: str = "info", printer_id: str = "", force: bool = False,
                             trace_id=None):
        await _ensure_schema()
        if not force and not await _run(_subs_allows, source):
            return {"ok": True, "printed": False, "skipped": True, "source": source}
        _SRC_LABEL = {"system": "SYSTEM ALERT", "dreams": "DREAM DIRECTOR",
                      "narrator": "NARRATOR", "chat": "CHAT", "markets": "MARKET ALERT",
                      "deals": "PRODUCT DEAL", "fabric": "FABRIC SOURCE"}
        label = _SRC_LABEL.get(source, (source or "note").upper())
        sub = (title or "").strip()
        parts = ["=" * 28]
        if sub and sub.lower() != label.lower():
            parts.append(sub)
        parts += ["", (body or "")]
        r = await cap_print_notify(title=label, body=chr(10).join(parts), level=level,
                                   printer_id=printer_id, force=True)
        return {"ok": True, "printed": bool(r.get("routed") or r.get("escpos_b64")),
                "source": source, **r}

    @capability(
        "print.notify", http_method="POST", http_path="/print/notify",
        http_tags=["print"],
        schema=enum_schema(level=["info", "warn", "alert"]),
        description="Print a formatted NOTIFICATION on the thermal printer -- Vera's "
                    "important-work/update channel on paper. Input: title, body, level "
                    "(info|warn|alert), printer_id. Also the 'printer' delivery channel. "
                    "Output: {ok, escpos_b64, bytes, transport, routed}.")
    async def cap_print_notify(title: str = "", body: str = "", level: str = "info",
                               printer_id: str = "", force: bool = False, trace_id=None):
        await _ensure_schema()
        if not force and not (await _run(_subs_get)).get("master", True):
            return {"ok": True, "skipped": True, "reason": "printer notifications are off"}
        import time as _t
        mark = {"info": "*", "warn": "!!", "alert": "###"}.get((level or "info").lower(), "*")
        head = f"{mark} VERA {(level or 'info').upper()} {mark}"
        stamp = _t.strftime("%Y-%m-%d %H:%M")
        printer = await _run(_db_get_printer, printer_id or "default")
        width = _width_for(printer, 0)
        try:
            data = await _run(lambda: build_nice_text(
                [stamp, ""] + (body or "").split(chr(10)), width, 26, align="left",
                title=(title or head), cut=True))
        except Exception:
            data = build_text((title or head) + chr(10) + stamp + chr(10) * 2 + (body or ""),
                              align="left", cut=True, cols=_cols_for(printer))
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "notify",
                          "message": f"notify via {res.get('transport')}"})
        return {"ok": True, **res}

    try:
        from Vera.vera import delivery as _delivery
        _delivery.register_channel(
            "printer", label="Thermal printer", cap="print.notify",
            default_format="text", needs_target=False, source="print")
    except Exception:                              # pragma: no cover
        pass

    register_ui(
        "thermal-printer", "Thermal Printer", "\U0001f5a8\ufe0f",
        """<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/print/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          title="Thermal Printer" allow="serial; usb; clipboard-read; clipboard-write"></iframe>
</div>""",
        "",
        ui_caps=["print.status", "print.image", "print.nice", "print.text",
                 "print.label", "print.printers", "print.printer.upsert", "print.notify"],
        mode="element",
    )

    _HERE = _Path(__file__).parent

    try:
        from Vera.vera.capability_orchestration import APP as _APP
        @_APP.get("/ui/elements/thermal_printer_element.js", include_in_schema=False)
        async def _thermal_element_route():
            from fastapi.responses import Response
            p = _HERE / "thermal_printer_element.js"
            if p.exists():
                return Response(p.read_text(encoding="utf-8"),
                                media_type="application/javascript")
            return Response("/* thermal_printer_element.js not found */",
                            media_type="application/javascript", status_code=404)

        @_APP.get("/print/panel", include_in_schema=False)
        async def _print_panel_route():
            from fastapi.responses import HTMLResponse
            p = _HERE / "print_composer.html"
            return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                                else "<p style='color:red'>print_composer.html not found</p>")

        @_APP.get("/ui/vera-print-selection.js", include_in_schema=False)
        async def _print_selection_js():
            from fastapi.responses import Response
            p = _HERE / "vera-print-selection.js"
            if p.exists():
                return Response(p.read_text(encoding="utf-8"),
                                media_type="application/javascript; charset=utf-8")
            return Response("/* vera-print-selection.js not found */",
                            media_type="application/javascript; charset=utf-8", status_code=404)
    except Exception as _e:                       # pragma: no cover
        log.debug("thermal element route not mounted: %s", _e)

    log.info("thermal printer: ready (server_serial=%s, webserial, mesh)",
             _pyserial() is not None)
