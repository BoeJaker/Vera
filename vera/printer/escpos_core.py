"""escpos_core.py -- pure ESC/POS command builder for a thermal receipt/label printer.

No I/O and no PIL, so it is fully unit-testable. Two job builders:

  * text_job()   -- the printer's BUILT-IN fonts (font A/B) with size multipliers,
                    bold, alignment, feed + cut. Needs no rendering library at all.
  * raster_job() -- a 1-bpp bit-image (GS v 0). The cap layer renders "nice" TTF
                    fonts and images into a monochrome bitmap (via Pillow) and hands
                    the packed rows here; this module just frames them in ESC/POS.

Consumers import uppercase (Vera.vera.printer.escpos_core); tests import lowercase
(vera.printer.escpos_core) so pytest binds to the worktree copy."""
from __future__ import annotations
from typing import List

ESC = b"\x1b"
GS = b"\x1d"
INIT = ESC + b"@"                      # ESC @  -- reset to defaults
CUT = GS + b"V" + b"\x00"              # GS V 0 -- full cut
_ALIGN = {"left": 0, "center": 1, "right": 2}


def _align(a: str) -> bytes:
    return ESC + b"a" + bytes([_ALIGN.get((a or "left").lower(), 0)])


def _size(width: int, height: int) -> bytes:
    """GS ! n -- character magnification, width/height each 1..8."""
    w = max(1, min(8, int(width)))
    h = max(1, min(8, int(height)))
    return GS + b"!" + bytes([((w - 1) << 4) | (h - 1)])


def _bold(on: bool) -> bytes:
    return ESC + b"E" + bytes([1 if on else 0])


def text_job(text: str, *, align: str = "left", width: int = 1, height: int = 1,
             bold: bool = False, cut: bool = True, feed: int = 3) -> bytes:
    """ESC/POS for a built-in-font text print. cp437 is the near-universal thermal
    codepage; unmappable chars are replaced rather than raising."""
    out = bytearray(INIT)
    out += _align(align) + _size(width, height) + _bold(bold)
    body = (text or "").replace("\r\n", "\n")
    out += body.encode("cp437", "replace")
    if not body.endswith("\n"):
        out += b"\n"
    out += _size(1, 1) + _bold(False) + _align("left")   # restore defaults
    out += b"\n" * max(0, int(feed))
    if cut:
        out += CUT
    return bytes(out)


def raster_job(width_px: int, height_px: int, mono_rows: List[bytes], *,
               align: str = "center", cut: bool = True, feed: int = 3) -> bytes:
    """Frame a 1-bpp bitmap as a GS v 0 raster bit-image.

    mono_rows: one bytes object per pixel row, each ceil(width_px/8) bytes,
    MSB-first, a 1 bit = black dot."""
    bpr = (int(width_px) + 7) // 8
    out = bytearray(INIT) + _align(align)
    out += GS + b"v0" + b"\x00"                          # GS v 0, mode 0 (normal)
    out += bytes([bpr & 0xFF, (bpr >> 8) & 0xFF,
                  int(height_px) & 0xFF, (int(height_px) >> 8) & 0xFF])
    for row in mono_rows:
        out += row[:bpr].ljust(bpr, b"\x00")
    out += _align("left") + b"\n" * max(0, int(feed))
    if cut:
        out += CUT
    return bytes(out)
