"""spritesheet.py — pack frames into a sheet + atlas metadata + animated preview
================================================================================
Pure PIL. Produces:
  • a grid PNG sprite sheet (uniform cells, transparent padding),
  • atlas metadata (per-frame rects + animation tag), TexturePacker/Aseprite-ish,
  • an animated GIF (and optional WebP) preview.

All cells are normalised to the largest frame so engines can slice on a fixed
grid; each frame is centred + bottom-anchored so a walk/idle cycle doesn't jitter.
"""

from __future__ import annotations

import base64
import io
import math
from typing import Dict, List, Tuple

from PIL import Image


def _norm_cell(frames: List[Image.Image]) -> Tuple[int, int]:
    fw = max((f.width for f in frames), default=1)
    fh = max((f.height for f in frames), default=1)
    return fw, fh


def pack_frames(frames: List[Image.Image], columns: int = 0
                ) -> Tuple[Image.Image, Dict]:
    """Pack frames into a uniform grid. columns<=0 → single row.

    Returns (sheet_rgba, meta) where meta carries the cell size + layout so the
    atlas JSON and the engine agree on the slice grid."""
    frames = [f.convert("RGBA") for f in frames] or [Image.new("RGBA", (1, 1))]
    fw, fh = _norm_cell(frames)
    count = len(frames)
    cols = count if columns <= 0 else min(columns, count)
    rows = math.ceil(count / cols)
    sheet = Image.new("RGBA", (cols * fw, rows * fh), (0, 0, 0, 0))
    for i, fr in enumerate(frames):
        c, r = i % cols, i // cols
        # centre horizontally, bottom-anchor vertically (feet stay planted)
        ox = c * fw + (fw - fr.width) // 2
        oy = r * fh + (fh - fr.height)
        sheet.alpha_composite(fr, (ox, oy))
    meta = {"frame_width": fw, "frame_height": fh, "columns": cols,
            "rows": rows, "count": count}
    return sheet, meta


def atlas_json(anim: str, meta: Dict, fps: int = 8, loop: bool = True) -> Dict:
    """Build atlas metadata (frame rects + an animation tag) for one sheet."""
    fw, fh, cols = meta["frame_width"], meta["frame_height"], meta["columns"]
    frames = {}
    order = []
    for i in range(meta["count"]):
        c, r = i % cols, i // cols
        key = f"{anim}_{i}"
        frames[key] = {"frame": {"x": c * fw, "y": r * fh, "w": fw, "h": fh},
                       "duration": int(1000 / max(1, fps))}
        order.append(key)
    return {
        "frames": frames,
        "meta": {"app": "vera-spritegen", "format": "RGBA8888",
                 "size": {"w": cols * fw, "h": meta["rows"] * fh},
                 "scale": "1"},
        "animations": {anim: order},
        "frameTags": [{"name": anim, "from": 0, "to": meta["count"] - 1,
                       "direction": "forward" if loop else "pingpong",
                       "fps": fps, "loop": loop}],
    }


def _global_gif_palette(frames: List[Image.Image], colors: int = 255) -> Image.Image:
    """ONE adaptive palette for the whole animation (index 255 stays reserved for
    transparency). Per-frame adaptive palettes make a GIF shimmer like static
    even when the frames barely differ — the classic flickering-preview bug."""
    fw = sum(f.width for f in frames) or 1
    fh = max((f.height for f in frames), default=1)
    strip = Image.new("RGB", (fw, fh), (0, 0, 0))
    x = 0
    for f in frames:
        strip.paste(f.convert("RGB"), (x, 0), f.convert("RGBA").split()[3])
        x += f.width
    return strip.convert("P", palette=Image.ADAPTIVE, colors=colors)


def _to_transparent_p(frame: Image.Image, palette: Image.Image,
                      transp_index: int = 255) -> Image.Image:
    """RGBA → P against the SHARED palette, with one index reserved for
    transparency (for GIF)."""
    frame = frame.convert("RGBA")
    alpha = frame.split()[3]
    p = frame.convert("RGB").quantize(palette=palette, dither=Image.NONE)
    # Pixels with low alpha → the reserved transparent index.
    mask = alpha.point(lambda v: 255 if v < 128 else 0)
    p.paste(transp_index, mask)
    return p


def gif_bytes(frames: List[Image.Image], fps: int = 8, loop: bool = True) -> bytes:
    if not frames:
        return b""
    pal = _global_gif_palette(frames)
    ps = [_to_transparent_p(f, pal) for f in frames]
    buf = io.BytesIO()
    ps[0].save(buf, format="GIF", save_all=True, append_images=ps[1:],
               duration=int(1000 / max(1, fps)), loop=0 if loop else 1,
               transparency=255, disposal=2, optimize=False)
    return buf.getvalue()


def webp_bytes(frames: List[Image.Image], fps: int = 8, loop: bool = True) -> bytes:
    """Animated WebP keeps full alpha (nicer than GIF); returns b'' if unsupported."""
    if not frames:
        return b""
    try:
        rgba = [f.convert("RGBA") for f in frames]
        buf = io.BytesIO()
        rgba[0].save(buf, format="WEBP", save_all=True, append_images=rgba[1:],
                     duration=int(1000 / max(1, fps)), loop=0 if loop else 1,
                     lossless=True)
        return buf.getvalue()
    except Exception:
        return b""


def img_to_b64(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()
