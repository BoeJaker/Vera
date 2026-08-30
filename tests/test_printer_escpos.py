"""Pure ESC/POS core for the Vera thermal-printer service."""
from vera.printer.escpos_core import text_job, raster_job, INIT, CUT


def test_text_job_basic():
    j = text_job("Hello", align="center", width=2, height=2, bold=True, cut=True)
    assert j.startswith(INIT)
    assert b"Hello" in j
    assert j.endswith(CUT)
    assert b"\x1ba\x01" in j          # ESC a 1 = center
    assert b"\x1bE\x01" in j          # ESC E 1 = bold on


def test_text_job_no_cut_and_encoding():
    j = text_job("x", cut=False)
    assert not j.endswith(CUT)
    # unmappable unicode is replaced, not raised
    text_job("café — ☃", cut=False)


def test_raster_job_frames_bitmap():
    rows = [b"\xff\x00", b"\x00\xff"]           # 16px wide, 2 rows
    j = raster_job(16, 2, rows, cut=True)
    assert j.startswith(INIT)
    i = j.index(b"\x1dv0\x00")                  # GS v 0, mode 0
    assert j[i + 4:i + 8] == bytes([2, 0, 2, 0])  # xL,xH,yL,yH for bpr=2,h=2
    assert j.endswith(CUT)


def test_raster_pads_short_rows():
    j = raster_job(16, 1, [b"\xff"], cut=False)  # row shorter than bpr=2 -> padded
    i = j.index(b"\x1dv0\x00")
    assert j[i + 4:i + 8] == bytes([2, 0, 1, 0])
