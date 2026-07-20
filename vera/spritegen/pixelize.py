"""pixelize.py — high-res frame → clean pixel-art frame (pure PIL/+numpy)
========================================================================
Never generate tiny sprites directly: render large, then downscale in steps and
clean the edges. This module does the "Downscale → Frame cleanup → Outline" boxes
of the pipeline. All functions are pure (no cap calls), so they're unit-testable.

numpy is used where it helps (alpha thresholding, floating-pixel removal) and
guarded — everything degrades to a PIL-only path if numpy is absent.
"""

from __future__ import annotations

import base64
import io
from typing import Optional, Tuple

from PIL import Image, ImageFilter

try:
    import numpy as _np
    _HAS_NP = True
except Exception:                       # pragma: no cover - numpy is normally present
    _np = None
    _HAS_NP = False


# ── codec helpers ─────────────────────────────────────────────────────────────

def b64_to_img(b64: str) -> Image.Image:
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def img_to_b64(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


# ── stages ────────────────────────────────────────────────────────────────────

def _target_size(w: int, h: int, target: int) -> Tuple[int, int]:
    """Output size with `target` on the long edge, aspect preserved."""
    if w >= h:
        return target, max(1, round(h * target / w))
    return max(1, round(w * target / h)), target


def _premultiply(img: Image.Image) -> Image.Image:
    """RGB × alpha. Zeroes the RGB hiding under transparent pixels so resampling
    can't mix it into visible edges. No-op without numpy."""
    if not _HAS_NP:
        return img
    arr = _np.asarray(img.convert("RGBA"), dtype=_np.float32)
    arr[..., :3] *= arr[..., 3:4] / 255.0
    return Image.fromarray(_np.round(arr).astype(_np.uint8), "RGBA")


def _unpremultiply(img: Image.Image) -> Image.Image:
    if not _HAS_NP:
        return img
    arr = _np.asarray(img.convert("RGBA"), dtype=_np.float32)
    a = arr[..., 3:4]
    rgb = _np.where(a > 0, arr[..., :3] * 255.0 / _np.maximum(a, 1.0), 0.0)
    arr[..., :3] = _np.clip(_np.round(rgb), 0, 255)
    return Image.fromarray(arr.astype(_np.uint8), "RGBA")


def progressive_downscale(img: Image.Image, target: int,
                          resample=Image.LANCZOS) -> Image.Image:
    """Halve the image repeatedly down to ~target on the long edge, then a final
    resize. Stepped downscaling preserves detail far better than one big jump.

    Resizes in PREMULTIPLIED-alpha space: a straight-alpha resize mixes the RGB
    hiding under transparent pixels (the chroma backdrop the keyer cleared, the
    original background under a rembg cutout) into every visible edge pixel —
    THE source of the green smears on pixelized frames. Premultiplying zeroes
    hidden RGB so only visible colour is ever resampled. Note the k-centroid
    path pre-shrinks large inputs through here, so it inherited the same bug."""
    img = img.convert("RGBA")
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= target:
        return img
    out = _premultiply(img)
    while long_edge > target * 2:
        w, h = max(1, w // 2), max(1, h // 2)
        out = out.resize((w, h), resample)
        long_edge = max(w, h)
    nw, nh = _target_size(w, h, target)
    out = out.resize((nw, nh), resample)
    return _unpremultiply(out)


def kcentroid_downscale(img: Image.Image, target: int, centroids: int = 2,
                        iters: int = 4) -> Image.Image:
    """k-centroid downscale (the de-facto AI-art → pixel-art technique): each
    output pixel becomes the DOMINANT colour cluster of its source cell instead
    of the cell average. This keeps edges hard and colours flat where Lanczos
    produces the muddy anti-aliased blend that later quantizes into noise.

    Alpha is decided by majority vote per cell (crisp silhouette, no fuzz).
    Falls back to progressive Lanczos when numpy is missing."""
    if not _HAS_NP:
        return progressive_downscale(img, target)
    img = img.convert("RGBA")
    W, H = img.size
    tw, th = _target_size(W, H, max(8, int(target)))
    if W <= tw and H <= th:
        return img
    # Pre-shrink very large inputs (cells stay >= ~4px across) so the per-cell
    # k-means loop below stays fast without changing the result meaningfully.
    max_pre = max(tw, th) * 8
    if max(W, H) > max_pre:
        img = progressive_downscale(img, max_pre)
        W, H = img.size
    arr = _np.asarray(img, dtype=_np.float32)
    xs = _np.linspace(0, W, tw + 1).astype(int)
    ys = _np.linspace(0, H, th + 1).astype(int)
    out = _np.zeros((th, tw, 4), dtype=_np.uint8)
    k = max(1, int(centroids))
    for j in range(th):
        rows = arr[ys[j]:ys[j + 1]]
        for i in range(tw):
            cell = rows[:, xs[i]:xs[i + 1]].reshape(-1, 4)
            a = cell[:, 3]
            opaque = cell[a >= 128]
            # Majority-vote alpha: cell is transparent unless mostly opaque.
            if opaque.shape[0] * 2 <= cell.shape[0]:
                continue
            rgb = opaque[:, :3]
            if rgb.shape[0] <= k:
                out[j, i, :3] = rgb.mean(axis=0).round()
                out[j, i, 3] = 255
                continue
            # Tiny k-means on the cell's opaque pixels; dominant cluster wins.
            cents = rgb[_np.linspace(0, rgb.shape[0] - 1, k).astype(int)].copy()
            lab = _np.zeros(rgb.shape[0], dtype=_np.int64)
            for _ in range(max(1, iters)):
                d = ((rgb[:, None, :] - cents[None, :, :]) ** 2).sum(axis=2)
                lab = d.argmin(axis=1)
                for c in range(k):
                    m = rgb[lab == c]
                    if m.shape[0]:
                        cents[c] = m.mean(axis=0)
            counts = _np.bincount(lab, minlength=k)
            out[j, i, :3] = cents[counts.argmax()].round()
            out[j, i, 3] = 255
    return Image.fromarray(out, "RGBA")


def _hex_rgb(hex_color: str, default=(0x1b, 0xd1, 0x2a)):
    h = (hex_color or "").lstrip("#")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return default


def purge_key_color(img: Image.Image, key_hex: str = "", tol: int = 40) -> Image.Image:
    """Clear any surviving chroma-key-coloured pixels from a cutout.

    Both background removers can leave key colour behind: the GPU chroma key
    used to skip backdrop pockets enclosed by the character (between the legs,
    under an arm), and rembg's saliency mask often keeps those same pockets as
    'foreground'. Since transparent generations explicitly ask SD to paint the
    backdrop in this exact colour, any near-exact match left opaque is backdrop
    — clear it. The tolerance is tight so genuine costume colours survive; a
    truly key-green character should switch bg_color. No-op without numpy."""
    if not _HAS_NP:
        return img
    img = img.convert("RGBA")
    kr, kg, kb = _hex_rgb(key_hex)
    arr = _np.array(img)
    d2 = ((arr[..., :3].astype(_np.int16)
           - _np.array([kr, kg, kb], dtype=_np.int16)) ** 2).sum(axis=-1)
    hit = (d2 <= int(tol) * int(tol)) & (arr[..., 3] > 0)
    if hit.any():
        arr[hit, 3] = 0
        return Image.fromarray(arr, "RGBA")
    return img


def threshold_alpha(img: Image.Image, cutoff: int = 128) -> Image.Image:
    """Snap alpha to 0/255 so there are no semi-transparent (fuzzy) edges."""
    img = img.convert("RGBA")
    r, g, b, a = img.split()
    a = a.point(lambda v: 255 if v >= cutoff else 0)
    return Image.merge("RGBA", (r, g, b, a))


def remove_floaters(img: Image.Image, min_neighbors: int = 2) -> Image.Image:
    """Drop isolated opaque pixels that have fewer than `min_neighbors` opaque
    4/8-neighbours — kills the speckle diffusion leaves around a cutout. No-op
    without numpy."""
    if not _HAS_NP:
        return img
    img = img.convert("RGBA")
    arr = _np.array(img)
    alpha = (arr[..., 3] > 0).astype(_np.uint8)
    if alpha.sum() == 0:
        return img
    # Count 8-neighbours via shifted sums (padded).
    p = _np.pad(alpha, 1, mode="constant")
    neigh = (
        p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
        p[1:-1, :-2]               + p[1:-1, 2:] +
        p[2:, :-2]  + p[2:, 1:-1]  + p[2:, 2:]
    )
    lonely = (alpha == 1) & (neigh < min_neighbors)
    arr[lonely, 3] = 0
    return Image.fromarray(arr, "RGBA")


def drop_specks(img: Image.Image, min_frac: float = 0.02) -> Image.Image:
    """Remove small disconnected opaque islands (chroma-key / diffusion residue —
    the 'static' speckle). Components smaller than `min_frac` of the LARGEST
    component are cleared; big detached parts (projectiles, motion lines) stay.
    Cheap BFS labelling — sprites are tiny by the time this runs."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    seen = [[False] * w for _ in range(h)]
    comps: list = []
    for y0 in range(h):
        for x0 in range(w):
            if seen[y0][x0] or px[x0, y0][3] == 0:
                continue
            stack = [(x0, y0)]
            seen[y0][x0] = True
            comp = []
            while stack:
                x, y = stack.pop()
                comp.append((x, y))
                for nx, ny in ((x-1, y), (x+1, y), (x, y-1), (x, y+1),
                               (x-1, y-1), (x+1, y-1), (x-1, y+1), (x+1, y+1)):
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] \
                            and px[nx, ny][3] > 0:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            comps.append(comp)
    if not comps:
        return img
    largest = max(len(c) for c in comps)
    cutoff = max(3, int(largest * min_frac))
    for comp in comps:
        if len(comp) < cutoff:
            for x, y in comp:
                px[x, y] = (0, 0, 0, 0)
    return img


def alpha_bbox(img: Image.Image) -> Tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1) of the opaque silhouette (whole image if none)."""
    img = img.convert("RGBA")
    box = img.split()[3].getbbox()
    return box if box else (0, 0, img.width, img.height)


def _centroid_x(img: Image.Image, box: Tuple[int, int, int, int]) -> float:
    """Horizontal centre of mass of the alpha inside `box`. The centroid is far
    more stable across a walk cycle than the bbox centre (an extended front foot
    stretches the bbox but barely moves the mass), so anchoring on it keeps the
    torso planted frame to frame."""
    if _HAS_NP:
        a = _np.asarray(img.convert("RGBA"))[..., 3].astype(_np.float64)
        total = a.sum()
        if total > 0:
            xs = _np.arange(a.shape[1], dtype=_np.float64)
            return float((a.sum(axis=0) * xs).sum() / total)
    return (box[0] + box[2]) / 2.0


def center_frames(frames: list, *, mode: str = "centroid",
                  pad: int = 1) -> list:
    """Re-anchor every frame of an animation onto ONE uniform cell so the
    character sits at the same spot in each: silhouette centre (centroid or bbox
    middle) at the cell's horizontal centre, silhouette bottom on the cell floor.

    This is what stops the sprite 'teleporting' when a walk cycle is flipped
    horizontally for left/right movement — a flip mirrors the whole canvas, so
    any off-centre placement doubles into a visible jump. With the anchor at the
    exact centre, flipping is a no-op positionally. `mode`: 'centroid' | 'bbox'
    | 'none' (returns frames untouched)."""
    if not frames or (mode or "centroid").lower() == "none":
        return frames
    use_centroid = (mode or "centroid").lower() != "bbox"
    info = []
    for f in frames:
        f = f.convert("RGBA")
        box = alpha_bbox(f)
        cx = _centroid_x(f, box) if use_centroid else (box[0] + box[2]) / 2.0
        cx = min(max(cx, box[0]), box[2])          # keep the anchor inside the bbox
        info.append((f, box, cx))
    # Cell sized so every frame fits with its anchor exactly at the centre line.
    half = max(max(cx - b[0], b[2] - cx) for _, b, cx in info)
    height = max(b[3] - b[1] for _, b, _ in info)
    cw = int(2 * (half + pad) + 1)
    ch = int(height + pad)
    out = []
    for f, b, cx in info:
        crop = f.crop(b)
        cell = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ox = int(round(cw / 2 - (cx - b[0])))
        oy = ch - crop.height                      # feet planted on the cell floor
        cell.alpha_composite(crop, (max(0, ox), max(0, oy)))
        out.append(cell)
    return out


def add_outline(img: Image.Image, color: Tuple[int, int, int, int] = (0, 0, 0, 255),
                width: int = 1) -> Image.Image:
    """Paint a solid outline of `width` px around the opaque silhouette."""
    img = img.convert("RGBA")
    alpha = img.split()[3]
    grown = alpha
    for _ in range(max(1, width)):
        grown = grown.filter(ImageFilter.MaxFilter(3))
    # Outline region = grown silhouette minus the original silhouette.
    ring = Image.new("L", img.size, 0)
    if _HAS_NP:
        g = _np.array(grown) > 0
        a = _np.array(alpha) > 0
        ring = Image.fromarray(((g & ~a) * 255).astype("uint8"), "L")
    else:
        # PIL-only difference
        from PIL import ImageChops
        ring = ImageChops.subtract(grown, alpha)
    outline_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    solid = Image.new("RGBA", img.size, tuple(color))
    outline_layer.paste(solid, (0, 0), ring)
    return Image.alpha_composite(outline_layer, img)


def pixelize(img: Image.Image, sprite_size: int, *,
             colors: int = 0, outline: bool = False, dither: bool = False,
             clean_edges: bool = True, palette_fn=None,
             method: str = "kcentroid") -> Image.Image:
    """Full pixel-art conversion: downscale → clean edges → (palette) → outline.

    `method` picks the downscaler: 'kcentroid' (dominant-colour cells — crisp,
    game-quality pixels) or 'lanczos' (soft, painterly). `palette_fn(img, colors,
    dither)` is injected by the pipeline (palette.py) to keep this module free of
    cross-imports; if None and colors>0, a plain PIL quantize is used."""
    target = max(8, int(sprite_size))
    if (method or "kcentroid").lower() == "lanczos":
        out = progressive_downscale(img, target)
    else:
        out = kcentroid_downscale(img, target)
    if clean_edges:
        out = threshold_alpha(out)
        out = remove_floaters(out)
        out = drop_specks(out)
    if colors and colors > 0:
        if palette_fn is not None:
            out = palette_fn(out, colors, dither)
        else:
            out = _basic_quantize(out, colors, dither)
    if outline:
        out = add_outline(out, width=1)
        if clean_edges:
            out = threshold_alpha(out)
    return out


def _basic_quantize(img: Image.Image, colors: int, dither: bool) -> Image.Image:
    """Alpha-preserving median-cut quantize fallback (palette.py does the rich
    one). Hidden RGB is flattened onto the mean visible colour first so an
    invisible keyed-out backdrop can't dominate the palette."""
    from PIL import ImageStat
    img = img.convert("RGBA")
    alpha = img.split()[3]
    mask = alpha.point(lambda v: 255 if v >= 128 else 0)
    rgb = img.convert("RGB")
    lo, hi = mask.getextrema()
    if lo == 0 and hi == 255:                 # has both hidden and visible pixels
        mean = tuple(int(round(v)) for v in ImageStat.Stat(rgb, mask=mask).mean[:3])
        rgb = Image.composite(rgb, Image.new("RGB", img.size, mean), mask)
    q = rgb.quantize(colors=max(2, colors),
                     method=Image.MEDIANCUT,
                     dither=Image.FLOYDSTEINBERG if dither else Image.NONE).convert("RGB")
    out = q.convert("RGBA")
    out.putalpha(alpha)
    return out


def pixelize_b64(b64: str, sprite_size: int, **kw) -> str:
    return img_to_b64(pixelize(b64_to_img(b64), sprite_size, **kw))
