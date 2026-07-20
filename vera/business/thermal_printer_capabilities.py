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
import json
import logging
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.print")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, register_ui, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.print").warning("thermal printer caps unavailable: %s", e)
    _CAP_AVAILABLE = False


TRANSPORTS = ["server_serial", "webserial", "mesh"]
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
               title: str = "") -> bytes:
    out = bytearray(_INIT)
    if title:
        out += _ALIGN["center"] + _BOLD_ON + _size(2, 2)
        out += _text_line(title)
        out += _size(1, 1) + _BOLD_OFF + _ALIGN["left"] + b"\n"
    out += _ALIGN.get(align, _ALIGN["left"])
    if bold:
        out += _BOLD_ON
    if width > 1 or height > 1:
        out += _size(width, height)
    for ln in (text or "").split("\n"):
        out += _text_line(ln)
    out += _size(1, 1) + _BOLD_OFF + _ALIGN["left"]
    out += _FEED3
    if cut:
        out += _CUT
    return bytes(out)


def build_receipt(spec: dict) -> bytes:
    """spec = {header, subheader, items:[{name, qty, price}], subtotal, tax,
    total, currency, footer, qr, barcode, order_id}."""
    cur = spec.get("currency", "")
    def money(v):
        try: return f"{cur}{float(v):,.2f}"
        except Exception: return str(v)
    W = 32  # 58 mm ≈ 32 cols at font A
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


def build_label(spec: dict) -> bytes:
    """A shipping/address label. spec = {to:[lines], from:[lines], ref,
    barcode, note}."""
    W = 32
    out = bytearray(_INIT)
    if spec.get("from"):
        out += _ALIGN["left"] + _text_line("FROM:")
        for ln in spec["from"]:
            out += _text_line("  " + str(ln))
        out += b"\n"
    out += _ALIGN["left"] + _BOLD_ON + _text_line("SHIP TO:") + _BOLD_OFF
    out += _size(1, 2)
    for ln in spec.get("to", []) or []:
        out += _text_line(str(ln))
    out += _size(1, 1)
    if spec.get("ref"):
        out += b"\n" + _text_line("Ref: " + str(spec["ref"]))
    if spec.get("barcode"):
        out += _ALIGN["center"] + _barcode(str(spec["barcode"])) + _ALIGN["left"]
    if spec.get("note"):
        out += b"\n" + _text_line(str(spec["note"]))
    out += _FEED3 + _CUT
    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# Printer registry (sqlite)
# ─────────────────────────────────────────────────────────────────────────────

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

def _serial_write(port: str, baud: int, data: bytes) -> dict:
    serial = _pyserial()
    if not serial:
        return {"ok": False, "error": "pyserial not installed on server (pip install pyserial)"}
    if not port:
        return {"ok": False, "error": "no serial port configured"}
    try:
        with serial.Serial(port, int(baud or 9600), timeout=2) as ser:
            ser.write(data)
            ser.flush()
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
    if tr == "server_serial":
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
        return {"pyserial": serial_ok, "ports": ports, "printers": printers,
                "transports": TRANSPORTS}

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
        data = build_text(text, align=align, bold=bold, width=width, height=height,
                          cut=cut, title=title)
        printer = await _run(_db_get_printer, printer_id) if printer_id else \
                  await _run(_db_get_printer, "default")
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
        data = build_receipt(spec)
        printer = await _run(_db_get_printer, printer_id or "default")
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
        data = build_label({"to": to, "from": from_ or [], "ref": ref,
                            "barcode": barcode, "note": note})
        printer = await _run(_db_get_printer, printer_id or "default")
        res = await _route(printer, data)
        await emit_event({"type": "print.job", "stage": "label",
                          "message": f"label via {res.get('transport')}"})
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

    # ── Element registration ─────────────────────────────────────────────────

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
    except Exception as _e:                       # pragma: no cover
        log.debug("thermal element route not mounted: %s", _e)

    log.info("thermal printer: ready (server_serial=%s, webserial, mesh)",
             _pyserial() is not None)
