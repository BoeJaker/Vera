"""escpos_core.py -- pure ESC/POS command builder for a thermal receipt/label printer.

No I/O and no PIL, so it is fully unit-testable. Two job builders:

  * text_job()   -- the printer's BUILT-IN fonts (font A/B) with size multipliers,
                    bold, alignment, feed + cut. Needs no rendering library at all.
  * raster_job() -- a 1-bpp bit-image (GS v 0). The cap layer renders "nice" TTF
                    fonts and images into a monochrome bitmap (via Pillow) and hands
                    the packed rows here; this module just frames them in ESC/POS.

It also owns the line-fitting rules every printed feed depends on -- see
wrap_measured(): paper is 32 (58 mm) or 48 (80 mm) columns wide and NOTHING
downstream re-flows text, so anything not wrapped here is silently clipped at
the paper edge.

Consumers import uppercase (Vera.vera.printer.escpos_core); tests import lowercase
(vera.printer.escpos_core) so pytest binds to the worktree copy."""
from __future__ import annotations
from typing import Callable, Iterable, List

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


# ── Line fitting ─────────────────────────────────────────────────────────────
# A thermal head is 384 dots (58 mm) or 576 dots (80 mm) wide and neither the
# raster renderer nor the printer re-flows anything: a line wider than the paper
# is CLIPPED, not carried over. Every text path therefore fits its lines here
# first. `measure` decouples the rule from the font -- len() for the built-in
# monospace font, a Pillow text-width probe for rasterised TrueType.

_RULE_CHARS = "=-_~*."


def is_rule(line: str) -> bool:
    """A horizontal rule (`----`, `====`) -- a separator to be re-fitted to the
    paper rather than word-wrapped into a second, ragged row of dashes."""
    s = (line or "").strip()
    return len(s) >= 3 and len(set(s)) == 1 and s[0] in _RULE_CHARS


def fit_rule(line: str, measure: Callable[[str], int], max_width: int) -> str:
    """Repeat a rule's character to fill max_width as closely as possible."""
    ch = (line or "").strip()[:1] or "-"
    n = 1
    while n < 512 and measure(ch * (n + 1)) <= max_width:
        n += 1
    return ch * n


def _longest_prefix(word: str, measure: Callable[[str], int], max_width: int) -> int:
    """Length of the longest prefix of `word` that fits (>=1, so a word wider
    than the paper still makes progress instead of looping forever)."""
    lo, hi, best = 1, len(word), 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if measure(word[:mid]) <= max_width:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def _continuation_indent(line: str) -> str:
    """Indent for a wrapped line's continuations: the source line's own leading
    whitespace, plus the width of a leading bullet so `* a long headline` hangs
    under its text instead of restarting at the margin."""
    lead = line[:len(line) - len(line.lstrip())]
    rest = line[len(lead):]
    for marker in ("* ", "- ", "+ ", "> "):
        if rest.startswith(marker):
            return lead + " " * len(marker)
    return lead


def wrap_measured(lines: Iterable[str], measure: Callable[[str], int],
                  max_width: int, *, hang_indent: bool = True) -> List[str]:
    """Greedy word-wrap so every returned line measures <= max_width.

    Blank lines are preserved (deliberate spacing on a receipt), rules are
    re-fitted to the paper, and a single word too wide to fit -- a URL, a long
    hyphenless token -- is hard-split rather than clipped."""
    limit = max(1, int(max_width))
    out: List[str] = []
    for raw in lines:
        line = (raw or "").rstrip()
        if not line.strip():
            out.append("")
            continue
        if is_rule(line):
            out.append(fit_rule(line, measure, limit))
            continue
        if measure(line) <= limit:
            out.append(line)
            continue
        indent = _continuation_indent(line) if hang_indent else ""
        cur = ""
        for word in line.split(" "):
            if not word:
                continue
            cand = (cur + " " + word) if cur else word
            if measure(cand) <= limit:
                cur = cand
                continue
            if cur:
                out.append(cur)
            # Start a continuation row; only now does the hanging indent apply.
            cur = indent + word
            while measure(cur) > limit:
                # Never split inside the indent, and always consume >=1 real
                # character so an over-wide token cannot spin forever.
                cut = max(len(indent) + 1, _longest_prefix(cur, measure, limit))
                if cut >= len(cur):
                    break
                out.append(cur[:cut])
                cur = indent + cur[cut:]
        if cur.strip():
            out.append(cur)
    return out


def wrap_cols(text: str, cols: int, *, hang_indent: bool = True) -> List[str]:
    """wrap_measured() for the printer's built-in monospace font, where one
    character is one column."""
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return wrap_measured(body.split("\n"), len, max(1, int(cols)),
                         hang_indent=hang_indent)


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
