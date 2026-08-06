"""Capture: GIF assembly (real Pillow when present) + guards."""

import os
import tempfile

import pytest

from vera.operator import capture as C


def test_pil_available_is_bool():
    assert isinstance(C.pil_available(), bool)


def test_no_frames_errors():
    assert "error" in C.assemble_gif([], os.path.join(tempfile.gettempdir(), "x.gif"))
    assert "error" in C.assemble_gif(["/nope/none.png"], os.path.join(tempfile.gettempdir(), "x.gif"))


@pytest.mark.skipif(not C.pil_available(), reason="Pillow not installed")
def test_assemble_gif_real():
    from PIL import Image
    d = tempfile.mkdtemp()
    frames = []
    for i in range(5):
        p = os.path.join(d, f"f{i}.png")
        Image.new("RGB", (200, 150), (i * 40, 60, 120)).save(p)
        frames.append(p)
    out = os.path.join(d, "a.gif")
    r = C.assemble_gif(frames, out, duration_ms=100, max_width=120)
    assert r["ok"] and r["frames"] == 5 and os.path.exists(out)
    g = Image.open(out)
    assert getattr(g, "n_frames", 1) == 5
    assert g.width == 120


@pytest.mark.skipif(not C.pil_available(), reason="Pillow not installed")
def test_assemble_gif_subsamples():
    from PIL import Image
    d = tempfile.mkdtemp()
    frames = []
    for i in range(20):
        p = os.path.join(d, f"f{i}.png")
        Image.new("RGB", (80, 60), (0, 0, 0)).save(p)
        frames.append(p)
    r = C.assemble_gif(frames, os.path.join(d, "a.gif"), max_frames=6)
    assert r["ok"] and r["frames"] == 6


def test_capture_summary_shape():
    cap = C.Capture("c1", "s1", tempfile.gettempdir(), interval_ms=500, max_frames=10)
    s = cap.summary()
    assert s["capture_id"] == "c1" and s["frames"] == 0 and s["running"] is True
