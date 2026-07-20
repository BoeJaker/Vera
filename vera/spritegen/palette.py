"""palette.py — palette reduction (pure PIL)
============================================
Reduce a frame to N colours (or snap to a fixed retro palette) while keeping the
cut-out alpha crisp. PIL's quantize ignores alpha, so we split it off, quantize
the RGB, then reattach a thresholded alpha.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PIL import Image

try:
    import numpy as _np
    _HAS_NP = True
except Exception:                       # pragma: no cover - numpy is normally present
    _np = None
    _HAS_NP = False

# A few well-known retro palettes (RGB triples) the panel can offer by name.
FIXED_PALETTES = {
    "pico8": [
        (0, 0, 0), (29, 43, 83), (126, 37, 83), (0, 135, 81),
        (171, 82, 54), (95, 87, 79), (194, 195, 199), (255, 241, 232),
        (255, 0, 77), (255, 163, 0), (255, 236, 39), (0, 228, 54),
        (41, 173, 255), (131, 118, 156), (255, 119, 168), (255, 204, 170),
    ],
    "gameboy": [
        (15, 56, 15), (48, 98, 48), (139, 172, 15), (155, 188, 15),
    ],
    "cga": [
        (0, 0, 0), (85, 255, 255), (255, 85, 255), (255, 255, 255),
    ],
}


def _palette_image(triples: List[Tuple[int, int, int]]) -> Image.Image:
    flat: List[int] = []
    for r, g, b in triples[:256]:
        flat += [int(r), int(g), int(b)]
    flat += [0] * (768 - len(flat))
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def neutralize_hidden(img: Image.Image) -> Image.Image:
    """Replace the RGB hiding under transparent pixels with the mean opaque
    colour. Median-cut palette building and Floyd–Steinberg dithering read
    EVERY pixel, including invisible ones — a keyed-out chroma backdrop
    (alpha 0, RGB still bright green) otherwise dominates the palette and
    diffuses green error into visible edges: the green-palette / green-smear
    bug. Returns RGBA."""
    from PIL import ImageStat
    img = img.convert("RGBA")
    mask = img.split()[3].point(lambda v: 255 if v >= 128 else 0)
    extrema = mask.getextrema()
    if extrema[0] == 255:                       # fully opaque — nothing hidden
        return img
    if extrema[1] == 0:                         # fully transparent — nothing visible
        return img
    mean = tuple(int(round(v)) for v in ImageStat.Stat(img.convert("RGB"), mask=mask).mean[:3])
    solid = Image.new("RGBA", img.size, mean + (0,))
    out = Image.composite(img, solid, mask)
    out.putalpha(img.split()[3])                # keep the original alpha untouched
    return out


def reduce_palette(img: Image.Image, colors: int = 32, dither: bool = False,
                   fixed: Optional[str] = None) -> Image.Image:
    """Quantize to `colors` (median-cut over the VISIBLE pixels only) or snap
    to a named fixed palette.

    Transparency is preserved by quantizing only the RGB and reattaching a
    thresholded alpha (so palette reduction never introduces fuzzy edges).
    The palette is built from opaque pixels only, and hidden RGB is
    neutralized first — otherwise an invisible keyed-out backdrop dominates
    the palette (see neutralize_hidden)."""
    img = img.convert("RGBA")
    alpha = img.split()[3].point(lambda v: 255 if v >= 128 else 0)
    rgb = neutralize_hidden(img).convert("RGB")

    dmode = Image.FLOYDSTEINBERG if dither else Image.NONE
    if fixed and fixed in FIXED_PALETTES:
        palimg = _palette_image(FIXED_PALETTES[fixed])
        q = rgb.quantize(palette=palimg, dither=dmode)
    else:
        q = rgb.quantize(palette=build_shared_palette([img], colors), dither=dmode)

    out = q.convert("RGB").convert("RGBA")
    out.putalpha(alpha)
    return out


# ── shared palette across frames ─────────────────────────────────────────────
# Quantizing each frame separately gives every frame a DIFFERENT palette, so an
# animation shimmers like static even when the drawings barely change. Build ONE
# palette from all frames' opaque pixels, then snap every frame to it.

def build_shared_palette(imgs: List[Image.Image], colors: int = 32,
                         max_samples: int = 200_000) -> Image.Image:
    """One median-cut palette from the opaque pixels of ALL frames. Returns a
    'P'-mode palette image usable with Image.quantize(palette=...)."""
    colors = max(2, min(256, int(colors or 32)))
    if _HAS_NP:
        chunks = []
        for im in imgs:
            arr = _np.asarray(im.convert("RGBA"))
            opq = arr[arr[..., 3] >= 128][:, :3]
            if opq.shape[0]:
                chunks.append(opq)
        if chunks:
            pix = _np.concatenate(chunks, axis=0)
            if pix.shape[0] > max_samples:
                idx = _np.linspace(0, pix.shape[0] - 1, max_samples).astype(int)
                pix = pix[idx]
            strip = Image.fromarray(pix.reshape(1, -1, 3), "RGB")
            return strip.quantize(colors=colors, method=Image.MEDIANCUT)
    # numpy-less fallback: montage every frame side by side, pasting only the
    # OPAQUE pixels (masked) so hidden backdrop colour can't reach the palette.
    fw = sum(im.width for im in imgs) or 1
    fh = max((im.height for im in imgs), default=1)
    strip = Image.new("RGB", (fw, fh), (0, 0, 0))
    x = 0
    for im in imgs:
        rgba = im.convert("RGBA")
        mask = rgba.split()[3].point(lambda v: 255 if v >= 128 else 0)
        strip.paste(rgba.convert("RGB"), (x, 0), mask)
        x += im.width
    return strip.quantize(colors=colors, method=Image.MEDIANCUT)


def apply_palette(img: Image.Image, palette_img: Image.Image,
                  dither: bool = False) -> Image.Image:
    """Snap one frame to a prebuilt palette, keeping its crisp alpha. Hidden
    RGB is neutralized first so dither error diffusion can't smear invisible
    backdrop colour into the visible pixels."""
    img = img.convert("RGBA")
    alpha = img.split()[3].point(lambda v: 255 if v >= 128 else 0)
    q = neutralize_hidden(img).convert("RGB").quantize(
        palette=palette_img,
        dither=Image.FLOYDSTEINBERG if dither else Image.NONE)
    out = q.convert("RGB").convert("RGBA")
    out.putalpha(alpha)
    return out
