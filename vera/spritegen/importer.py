"""importer.py — turn existing sprite-sheet images / zips into working characters
==================================================================================
Two entry points, both pure-PIL (no GPU):

  import_sheet(defn, anim, image_b64, ...)   one bare sheet PNG → sliced,
      alpha-keyed, centred frames + packed sheet + atlas + GIF, registered on
      the definition exactly like generated animations.

  import_package(zip_bytes, ...)             a zip — either our exported
      Character/ package (metadata.json drives precise slicing) or any folder
      of sheet PNGs (each optionally paired with an atlas .json) → a whole
      character with every animation imported.

Grid detection for bare sheets, in order of preference:
  1. explicit frame_width/frame_height,
  2. explicit columns/rows,
  3. transparent gutters (fully-clear pixel columns/rows between cells),
  4. square-cell heuristic (cols = round(W/H), one row).

Sheets without an alpha channel are keyed on the corner colour when the four
corners agree (magenta/green/white checkered-era sheets), so imports still get
clean silhouettes.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

from Vera.vera.spritegen import pixelize as _pix
from Vera.vera.spritegen import palette as _pal
from Vera.vera.spritegen.definition import (
    STORE, CharacterDefinition, DEFAULT_ANIMATIONS, char_dir, new_char_id, _safe,
)

log = logging.getLogger("vera.spritegen")


# ── alpha / keying ────────────────────────────────────────────────────────────

def _corner_key(img: Image.Image, tol: int = 28) -> Image.Image:
    """If the sheet has no useful alpha, key out the background colour sampled
    from the corners (when at least 3 corners agree). Returns RGBA."""
    rgba = img.convert("RGBA")
    a = rgba.split()[3]
    lo, hi = a.getextrema()
    if lo < 250:                       # already has real transparency
        return rgba
    w, h = rgba.size
    corners = [rgba.getpixel(p)[:3] for p in
               ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]

    def close(c1, c2):
        return sum((x - y) ** 2 for x, y in zip(c1, c2)) <= tol * tol

    for cand in corners:
        if sum(1 for c in corners if close(c, cand)) >= 3:
            if _pix._HAS_NP:
                np = _pix._np
                arr = np.array(rgba, dtype=np.int16)
                dist2 = ((arr[..., :3] - np.array(cand, dtype=np.int16)) ** 2).sum(axis=-1)
                arr[..., 3] = np.where(dist2 <= tol * tol, 0, arr[..., 3])
                return Image.fromarray(arr.astype("uint8"), "RGBA")
            px = rgba.load()
            for y in range(h):
                for x in range(w):
                    p = px[x, y]
                    if close(p[:3], cand):
                        px[x, y] = (p[0], p[1], p[2], 0)
            return rgba
    return rgba


# ── grid detection / slicing ──────────────────────────────────────────────────

def _gutter_runs(clear: List[bool]) -> List[Tuple[int, int]]:
    """Runs of consecutive True (fully transparent lines) → [(start, end)]."""
    runs, start = [], None
    for i, c in enumerate(clear):
        if c and start is None:
            start = i
        elif not c and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(clear)))
    return runs


def _detect_axis(img: Image.Image, axis: int) -> List[Tuple[int, int]]:
    """Content spans along one axis, split on fully-transparent gutters.
    axis 0 = columns (x spans), 1 = rows (y spans)."""
    a = img.convert("RGBA").split()[3]
    w, h = img.size
    n = w if axis == 0 else h
    clear = []
    for i in range(n):
        box = (i, 0, i + 1, h) if axis == 0 else (0, i, w, i + 1)
        clear.append(a.crop(box).getextrema()[1] == 0)
    gutters = [g for g in _gutter_runs(clear)]
    spans, pos = [], 0
    for g0, g1 in gutters:
        if g0 > pos:
            spans.append((pos, g0))
        pos = g1
    if pos < n:
        spans.append((pos, n))
    return spans


def detect_grid(img: Image.Image) -> Tuple[int, int, int, int]:
    """(frame_w, frame_h, cols, rows) for a bare sheet. Prefers transparent
    gutters; falls back to square cells on a single row."""
    w, h = img.size
    col_spans = _detect_axis(img, 0)
    row_spans = _detect_axis(img, 1)
    if 1 < len(col_spans) <= 64 and 1 <= len(row_spans) <= 16:
        cols, rows = len(col_spans), len(row_spans)
        return (w + cols - 1) // cols, (h + rows - 1) // rows, cols, rows
    # square-cell heuristic
    cols = max(1, round(w / h)) if h else 1
    return (w // cols) or w, h, cols, 1


def slice_frames(img: Image.Image, frame_w: int = 0, frame_h: int = 0,
                 columns: int = 0, rows: int = 0) -> List[Image.Image]:
    """Cut a sheet into cell images (empty cells dropped)."""
    img = img.convert("RGBA")
    W, H = img.size
    if frame_w and frame_h:
        fw, fh = int(frame_w), int(frame_h)
        cols, rws = max(1, W // fw), max(1, H // fh)
    elif columns:
        cols = max(1, int(columns))
        rws = max(1, int(rows) or 1)
        fw, fh = W // cols, H // rws
    else:
        fw, fh, cols, rws = detect_grid(img)
    frames = []
    for r in range(rws):
        for c in range(cols):
            cell = img.crop((c * fw, r * fh, min(W, (c + 1) * fw), min(H, (r + 1) * fh)))
            if cell.split()[3].getbbox():          # skip fully-empty cells
                frames.append(cell)
    return frames


# ── frame processing + registration ──────────────────────────────────────────

def _process_frames(defn, frames: List[Image.Image], process: str,
                    center: bool = True) -> List[Image.Image]:
    """'keep' leaves the pixels alone (just clean alpha), 'pixelize' runs the
    normal downscale+palette path, 'auto' keeps small cells and pixelizes big
    ones (a 512px pre-render is not yet a sprite; a 32px cell already is).
    center=False skips the silhouette re-anchor — hand-drawn frames position
    their content deliberately, so re-anchoring would destroy the drawing."""
    mode = (process or "auto").lower()
    if mode == "auto":
        big = max(max(f.size) for f in frames) > 160
        mode = "pixelize" if big else "keep"
    if mode == "pixelize":
        outs = [_pix.pixelize(f, defn.sprite_size, colors=0, outline=False,
                              dither=False, method=getattr(defn, "downscale", "kcentroid"))
                for f in frames]
        if defn.colors and defn.colors > 0 and len(outs) > 1:
            pal = _pal.build_shared_palette(outs, defn.colors)
            outs = [_pal.apply_palette(o, pal, dither=defn.dither) for o in outs]
    else:
        outs = [_pix.threshold_alpha(f) for f in frames]
    if not center:
        return outs
    return _pix.center_frames(outs, mode=(getattr(defn, "auto_center", "centroid") or "centroid"))


async def import_frames(defn, anim: str, frames: List[Image.Image], *,
                        fps: int = 8, loop: bool = True, process: str = "auto",
                        center: bool = True) -> Dict:
    """Register already-sliced frames as animation `anim` on `defn`: save raws
    + processed frames, update the definition, pack the sheet."""
    from Vera.vera.spritegen import pipeline as P     # late import (cycle-free)
    if not frames:
        return {"error": "no frames found in the sheet"}
    char_root = char_dir(defn.char_id)
    raw_files, fin_files = [], []
    outs = _process_frames(defn, frames, process, center=center)
    import shutil
    for sub in ("raw", "frames"):
        d = char_root / sub / _safe(anim)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    for i, (raw, fin) in enumerate(zip(frames, outs)):
        rp = char_root / "raw" / _safe(anim) / f"{i:03d}.png"
        rp.parent.mkdir(parents=True, exist_ok=True)
        raw.save(rp, format="PNG")
        raw_files.append(f"raw/{_safe(anim)}/{i:03d}.png")
        fp = char_root / "frames" / _safe(anim) / f"{i:03d}.png"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fin.save(fp, format="PNG")
        fin_files.append(f"frames/{_safe(anim)}/{i:03d}.png")
    defn.raw_files[anim] = raw_files
    defn.frame_files[anim] = fin_files
    spec = dict(defn.animations.get(anim) or DEFAULT_ANIMATIONS.get(anim)
                or {"desc": f"imported {anim}"})
    spec.update({"frames": len(fin_files), "fps": int(fps) or spec.get("fps", 8),
                 "loop": bool(loop)})
    defn.animations[anim] = spec
    defn.tiers_used[anim] = "import"
    await STORE.save(defn)
    # One shared cell across every animation — stops the character teleporting
    # when an imported pack switches between animations with different extents.
    try:
        await P.align_cells(defn)
    except Exception as e:
        log.warning("spritegen import: align_cells failed: %s", e)
    sheet = await P.build_sheet(defn, anim)
    return {"anim": anim, "count": len(fin_files), "sheet": sheet.get("sheet"),
            "gif": sheet.get("gif"), "tier": "import"}


async def import_sheet(defn, anim: str, image_b64: str, *,
                       frame_width: int = 0, frame_height: int = 0,
                       columns: int = 0, rows: int = 0,
                       crop: Optional[Tuple[int, int, int, int]] = None,
                       fps: int = 8, loop: bool = True,
                       process: str = "auto") -> Dict:
    try:
        img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
    except Exception as e:
        return {"error": f"could not decode image: {e}"}
    # Optional region select: crop to (x, y, w, h) BEFORE keying/slicing so the
    # user can pull one animation out of a busy sheet the auto-grid can't parse.
    if crop:
        x, y, w, h = (int(v) for v in crop)
        if w > 0 and h > 0:
            W, H = img.size
            x = max(0, min(x, W)); y = max(0, min(y, H))
            img = img.crop((x, y, min(W, x + w), min(H, y + h)))
    img = _corner_key(img)
    frames = slice_frames(img, frame_width, frame_height, columns, rows)
    if not frames:
        return {"error": "no non-empty cells found — check the grid / selection"}
    return await import_frames(defn, anim, frames, fps=fps, loop=loop, process=process)


async def splice_frames(defn, new_anim: str, picks: List[Dict], *,
                        fps: int = 8, loop: bool = True,
                        reverse: bool = False, pingpong: bool = False,
                        process: str = "keep") -> Dict:
    """Assemble a NEW animation from frames of the character's EXISTING animations
    (no diffusion). `picks` is an ordered list of {"anim": name, "frame": index};
    frames are pulled from the already-pixelized frame files, so 'keep' leaves the
    pixels untouched. `reverse` flips play order; `pingpong` appends the reverse of
    the interior frames for a seamless there-and-back cycle."""
    char_root = char_dir(defn.char_id)
    frames: List[Image.Image] = []
    for p in picks or []:
        a = (p or {}).get("anim")
        try:
            i = int((p or {}).get("frame", 0))
        except (TypeError, ValueError):
            continue
        files = (defn.frame_files or {}).get(a) or []
        if not files or i < 0 or i >= len(files):
            continue
        fp = char_root / files[i]
        if fp.is_file():
            try:
                frames.append(Image.open(fp).convert("RGBA"))
            except Exception:
                continue
    if not frames:
        return {"error": "no valid frames selected (need existing, built animations)"}
    if reverse:
        frames = list(reversed(frames))
    if pingpong and len(frames) > 2:
        frames = frames + list(reversed(frames[1:-1]))
    return await import_frames(defn, new_anim, frames, fps=fps, loop=loop, process=process)


# ── zip package import ────────────────────────────────────────────────────────

def _atlas_grid(atlas: dict) -> Tuple[int, int, int, int]:
    """(fw, fh, fps, count) from our atlas JSON (or a TexturePacker-ish one)."""
    frames = atlas.get("frames") or {}
    fw = fh = 0
    dur = 125
    if isinstance(frames, dict) and frames:
        f0 = next(iter(frames.values()))
        fr = f0.get("frame") or {}
        fw, fh = int(fr.get("w", 0)), int(fr.get("h", 0))
        dur = int(f0.get("duration", 125)) or 125
    tags = atlas.get("frameTags") or []
    fps = int(tags[0].get("fps", 0)) if tags else 0
    if not fps:
        fps = max(1, round(1000 / dur))
    return fw, fh, fps, len(frames)


async def import_package(zip_bytes: bytes, *, char_id: str = "",
                         name: str = "", process: str = "auto") -> Dict:
    """Import a zip of sprite sheets. Handles our exported Character/ package
    (metadata.json present) and generic zips of sheet PNGs (+ optional paired
    atlas .json files). Creates the definition when char_id is empty."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:
        return {"error": f"not a readable zip: {e}"}

    names = [n for n in zf.namelist() if not n.endswith("/")]

    def read_json(n):
        try:
            return json.loads(zf.read(n).decode("utf-8"))
        except Exception:
            return None

    meta_name = next((n for n in names if n.split("/")[-1] == "metadata.json"), None)
    metadata = read_json(meta_name) if meta_name else None

    defn = (await STORE.get(char_id)) if char_id else None
    if defn is None:
        defn = CharacterDefinition(char_id=char_id or new_char_id(),
                                   name=name or (metadata or {}).get("name", "")
                                   or "imported", animations={})
        defn.do_pixelize = False           # imported pixels are already final
    if name:
        defn.name = name
    await STORE.save(defn)

    imported: Dict[str, Dict] = {}
    errors: Dict[str, str] = {}

    if metadata and isinstance(metadata.get("animations"), dict):
        # Our package layout: sprite/<anim>.png sliced exactly by the manifest.
        root = meta_name.rsplit("/", 1)[0] + "/" if "/" in meta_name else ""
        if metadata.get("sprite_size"):
            defn.sprite_size = int(metadata["sprite_size"])
        for anim, spec in metadata["animations"].items():
            png = f"{root}sprite/{anim}.png"
            if png not in names:
                errors[anim] = "sheet missing from zip"
                continue
            try:
                img = Image.open(io.BytesIO(zf.read(png))).convert("RGBA")
                frames = slice_frames(img, int(spec.get("frame_width", 0)),
                                      int(spec.get("frame_height", 0)),
                                      int(spec.get("columns", 0)),
                                      int(spec.get("rows", 0)))
                res = await import_frames(defn, anim, frames,
                                          fps=int(spec.get("fps", 8)),
                                          loop=bool(spec.get("loop", True)),
                                          process=process)
                (errors if res.get("error") else imported)[anim] = \
                    res.get("error") if res.get("error") else res
            except Exception as e:
                errors[anim] = str(e)
        # bring the reference along if present
        ref = f"{root}reference.png"
        if ref in names:
            p = char_dir(defn.char_id) / "reference.png"
            p.write_bytes(zf.read(ref))
            defn.reference = "reference.png"
            await STORE.save(defn)
    else:
        # Generic zip: every PNG is a sheet; a sibling .json (same stem) may
        # carry the grid; otherwise auto-detect. Animation name = file stem.
        for n in names:
            if not n.lower().endswith(".png"):
                continue
            stem = n.rsplit("/", 1)[-1][:-4]
            anim = _safe(stem.lower()) or "idle"
            atlas = read_json(n[:-4] + ".json")
            fw = fh = 0
            fps = int((DEFAULT_ANIMATIONS.get(anim) or {}).get("fps", 8))
            if atlas:
                fw, fh, fps, _cnt = _atlas_grid(atlas)
            try:
                img = _corner_key(Image.open(io.BytesIO(zf.read(n))))
                frames = slice_frames(img, fw, fh)
                res = await import_frames(defn, anim, frames, fps=fps,
                                          loop=bool((DEFAULT_ANIMATIONS.get(anim)
                                                     or {}).get("loop", True)),
                                          process=process)
                (errors if res.get("error") else imported)[anim] = \
                    res.get("error") if res.get("error") else res
            except Exception as e:
                errors[anim] = str(e)

    if not imported and not errors:
        return {"error": "no sheet PNGs found in the zip"}
    return {"char_id": defn.char_id, "imported": imported, "errors": errors}
