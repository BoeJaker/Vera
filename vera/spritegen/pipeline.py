"""pipeline.py — spritegen stage orchestration (tier-probing + graceful fallback)
================================================================================
The engine that ties the pure stages together and talks to the GPU via the
``image.*`` capabilities. Each stage probes which tier is live and degrades
cleanly (the project's established philosophy):

  base:    image.generate (+ rembg → chroma-key for transparency)
  frames:  image.pose(ControlNet) → image.ipadapter → image.img2img(seed-lock)
           → image.generate(seed-lock)
  post:    pixelize + palette  (pure PIL, always)
  sheet:   pack_frames + atlas + gif   (pure PIL, always)
  upscale: image.upscale(ESRGAN) → (skipped; Lanczos downscale covers sizing)

Progress is broadcast with ``emit_event`` (``spritegen.*`` types) so the panel
can show a live stage tracker. Raw hi-res frames are kept so the user can
re-pixelize at a different size/palette without re-running diffusion.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, emit_event
from Vera.vera.spritegen import definition as _def
from Vera.vera.spritegen import prompts as _prompts
from Vera.vera.spritegen import pixelize as _pix
from Vera.vera.spritegen import palette as _pal
from Vera.vera.spritegen import providers as _prov
from Vera.vera.spritegen import skeleton as _skel
from Vera.vera.spritegen import spritesheet as _sheet
from Vera.vera.spritegen import package as _pkg
from Vera.vera.spritegen.definition import STORE, char_dir, _safe

log = logging.getLogger("vera.spritegen")


# ── cap + io helpers ──────────────────────────────────────────────────────────

async def _call_cap(cap_name: str, **kwargs) -> dict:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"capability {cap_name} not available"}
    try:
        res = await cap["func"](**kwargs)
        return res if isinstance(res, dict) else {"result": res}
    except Exception as e:
        log.warning("spritegen: call %s failed: %s", cap_name, e)
        return {"error": str(e)}


def _save_b64(char_root: Path, rel: str, b64: str) -> str:
    p = char_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(b64))
    return rel


def _save_img(char_root: Path, rel: str, img: Image.Image) -> str:
    p = char_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(p, format="PNG")
    return rel


def _load_ref_b64(defn) -> Optional[str]:
    if not defn.reference:
        return None
    p = char_dir(defn.char_id) / defn.reference
    return base64.b64encode(p.read_bytes()).decode() if p.is_file() else None


def _palette_fn(img, colors, dither):
    return _pal.reduce_palette(img, colors=colors, dither=dither)


async def _ensure_seed(defn) -> None:
    """Ensure a concrete seed is set. seed=-1 is passed through as 'no seed' to the
    GPU node, which then randomizes EVERY frame — the single biggest cause of
    frame-to-frame flicker. Frames within one animation therefore share this seed;
    it is only ever *rerolled* at base-generation time (see `_reroll_seed`)."""
    if int(getattr(defn, "seed", -1)) < 0:
        defn.seed = random.randint(1, 2**31 - 1)
        await STORE.save(defn)


async def _reroll_seed(defn) -> None:
    """Pick a FRESH random seed for a new base render, unless the user pinned one
    (`seed_lock`). Same seed + same prompt/params = bit-identical diffusion output
    by design, so without this a 'regenerate base' just reproduces the same picture
    forever. Frame generation never calls this — it reuses whatever seed is set so an
    animation's frames stay coherent."""
    if getattr(defn, "seed_lock", False):
        await _ensure_seed(defn)              # pinned → keep (or establish) the seed
        return
    defn.seed = random.randint(1, 2**31 - 1)
    await STORE.save(defn)


def _control_frame(defn, anim: str, index: int, total: int,
                   tiers: Dict) -> Optional[str]:
    """OpenPose control image (b64) for one frame, when the skeleton pipeline
    applies: ControlNet must be live, the definition must allow it, and the
    animation must have a procedural cycle (walk/run/idle/jump). This is what
    actually articulates arms and legs — pose text alone can't."""
    src = (getattr(defn, "pose_source", "auto") or "auto").lower()
    if src in ("none", "reference"):
        return None
    if not tiers.get("controlnet") or not _skel.supports(anim):
        return None
    try:
        return _skel.render_pose_b64(anim, index, max(1, total),
                                     defn.gen_width, defn.gen_height)
    except Exception as e:
        log.warning("spritegen: skeleton %s[%d/%d] failed: %s", anim, index, total, e)
        return None


def _center_mode(defn) -> str:
    return (getattr(defn, "auto_center", "centroid") or "centroid").lower()


def _negative(defn) -> str:
    """Definition negatives + the style's anti-realism additions (see prompts)."""
    return _prompts.build_negative(defn)


def _sched(defn) -> str:
    """Per-job sampler override for the GPU node ('' = model default)."""
    return (getattr(defn, "scheduler", "") or "").strip()


def _use_chroma(defn, tiers: Dict) -> bool:
    """Should this run request the flat key-colour backdrop + GPU chroma key?

    Only as a LAST resort. When rembg is live (and the definition allows it),
    we generate on a plain background and cut the character out with rembg —
    the key colour never enters the image, so it can't smear into the
    character or its palette. The chroma-key path remains the fallback for
    nodes without rembg."""
    if not getattr(defn, "transparent", True):
        return False
    method = (getattr(defn, "bg_removal", "auto") or "auto").lower()
    if method in ("auto", "rembg") and tiers.get("rembg"):
        return False
    return method != "none"


# ── tier probe ────────────────────────────────────────────────────────────────

async def probe_tiers() -> Dict:
    """Probe the GPU node for live tiers (extends image.sd_capabilities)."""
    sd = await _call_cap("image.sd_capabilities")
    return {
        "txt2img":    bool(sd.get("txt2img", True)),
        "img2img":    bool(sd.get("img2img")),
        "rembg":      bool(sd.get("rembg")),
        "controlnet": bool(sd.get("controlnet")),
        "openpose":   bool(sd.get("openpose")),
        "ipadapter":  bool(sd.get("ipadapter")),
        "upscale":    bool(sd.get("upscale")),
        "long_prompts": bool(sd.get("long_prompts")),
        "schedulers": list(sd.get("schedulers") or []),
        "model":      sd.get("model", ""),
        "device":     sd.get("device", ""),
    }


# ── stage: base reference ─────────────────────────────────────────────────────

async def generate_base(defn, *, job_id: str = "") -> Dict:
    tiers = await probe_tiers()
    await _reroll_seed(defn)
    await emit_event({"type": "spritegen.stage.progress", "char_id": defn.char_id,
                      "stage": "base", "index": 0, "total": 1, "job_id": job_id})
    b64, bg_tier, base_tier = "", "", ""
    # Purpose-built tier first: PixelLab renders native pixel art with a real
    # transparent background — no chroma key involved at all.
    if _prov.use_for(defn):
        b64 = await _prov.generate_base(defn) or ""
        if b64:
            base_tier, bg_tier = "pixellab", "native"
        else:
            log.warning("spritegen: pixellab base failed — using local GPU chain")
    if not b64:
        prompt = _prompts.build_base_prompt(defn)
        chroma = _use_chroma(defn, tiers)
        res = await _call_cap(
            "image.generate", prompt=prompt, negative_prompt=_negative(defn),
            width=defn.gen_width, height=defn.gen_height, steps=defn.steps,
            guidance=defn.guidance, seed=defn.seed, loras=defn.loras,
            transparent=chroma, bg_color=defn.bg_color,
            chroma_tol=defn.chroma_tol, store=True, job_id=job_id,
            scheduler=_sched(defn))
        b64 = (res or {}).get("image_b64", "")
        if not b64:
            return {"error": (res or {}).get("error", "no image — is SD loaded on the GPU node?")}
        b64, bg_tier = await remove_bg(defn, b64, tiers)
        if chroma:
            b64 = _purged(defn, b64)
    # Optional: ESRGAN-upscale the reference so img2img/IP-Adapter pose from a
    # sharper base. Never upscale PixelLab output — ESRGAN smooths the crisp
    # native pixels it exists to provide.
    if (getattr(defn, "do_upscale", False) and tiers.get("upscale")
            and base_tier != "pixellab"):
        up = await _call_cap("image.upscale", image_b64=b64, scale=2)
        if (up or {}).get("image_b64"):
            b64 = up["image_b64"]
            defn.tiers_used["upscale"] = "esrgan"
    char_root = char_dir(defn.char_id)
    _save_b64(char_root, "reference.png", b64)
    defn.reference = "reference.png"
    defn.tiers_used["base"] = base_tier or (
        "ipadapter-ready" if tiers.get("ipadapter") else "txt2img")
    defn.tiers_used["bg"] = bg_tier
    await STORE.save(defn)
    await emit_event({"type": "spritegen.stage.done", "char_id": defn.char_id,
                      "stage": "base", "tier": bg_tier})
    return {"reference": "reference.png", "device": ("pixellab" if base_tier == "pixellab"
                                                     else (res or {}).get("device", "")),
            "bg_tier": bg_tier}


async def remove_bg(defn, b64: str, tiers: Optional[Dict] = None):
    """Background removal honouring defn.bg_removal: 'none' keeps the raw image,
    'chroma' relies on the flat-colour key already applied during transparent
    generation, 'rembg'/'auto' use the rembg tier when live (with the chosen model
    + alpha-matting aggressiveness) and otherwise fall back to chroma."""
    tiers = tiers or await probe_tiers()
    method = (getattr(defn, "bg_removal", "auto") or "auto").lower()
    if method == "none":
        return b64, "none"
    if method in ("auto", "rembg") and tiers.get("rembg"):
        res = await _call_cap("image.rembg", image_b64=b64,
                              model=(defn.rembg_model or "u2net"),
                              alpha_matting=bool(getattr(defn, "rembg_alpha_matting", False)))
        if (res or {}).get("image_b64"):
            return res["image_b64"], "rembg"
    return b64, ("chroma" if defn.transparent else "none")


def _purged(defn, b64: str) -> str:
    """Belt-and-braces key purge for transparent generations: clear any
    chroma-key-coloured pixels that survived background removal (enclosed
    pockets rembg keeps as 'foreground', blobs the GPU key missed). Only runs
    on transparent runs — an arbitrary imported photo is never purged."""
    if not getattr(defn, "transparent", True) or not b64:
        return b64
    try:
        img = _pix.purge_key_color(_pix.b64_to_img(b64),
                                   getattr(defn, "bg_color", "") or "")
        return _pix.img_to_b64(img)
    except Exception as e:
        log.debug("spritegen: key purge skipped: %s", e)
        return b64


def _alpha_coverage(b64: str) -> float:
    """Fraction of fully transparent pixels in a b64 PNG (0.0 = opaque image)."""
    try:
        img = _pix.b64_to_img(b64)
        a = img.split()[3]
        hist = a.histogram()
        return hist[0] / max(1, img.width * img.height)
    except Exception:
        return 0.0


async def ensure_frame_alpha(defn, b64: str, tiers: Dict):
    """Guarantee a frame actually HAS a transparent background before it reaches
    the sheet. The base reference always got a rembg pass but animation frames
    relied on the GPU chroma key alone — when that key failed the whole sheet
    shipped with a baked background (the opaque-box-in-chat bug). If the chroma
    key produced no meaningful alpha, run the definition's bg-removal path on the
    frame too. Returns (b64, tier)."""
    method = (getattr(defn, "bg_removal", "auto") or "auto").lower()
    if method == "none":
        return b64, "none"
    chroma = _use_chroma(defn, tiers)
    if _alpha_coverage(b64) >= 0.05:          # chroma key already did its job
        return (_purged(defn, b64) if chroma else b64), "chroma"
    b64, tier = await remove_bg(defn, b64, tiers)
    # Purge leftover key colour ONLY when this run actually painted a chroma
    # backdrop — a rembg-tier run never saw the key colour, and purging would
    # eat legitimately-green pixels on a green character.
    return (_purged(defn, b64) if chroma else b64), tier


# ── stage: per-frame generation ───────────────────────────────────────────────

async def _gen_frame(defn, frame_prompt: str, ref_b64: Optional[str], tiers: Dict,
                     control_b64: Optional[str] = None, job_id: str = ""):
    """Generate ONE hi-res frame with the best-available identity-preserving tier.
    Returns (image_b64, tier, device)."""
    common = dict(prompt=frame_prompt, negative_prompt=_negative(defn),
                  steps=defn.steps, guidance=defn.guidance, seed=defn.seed,
                  width=defn.gen_width, height=defn.gen_height, loras=defn.loras,
                  transparent=_use_chroma(defn, tiers), bg_color=defn.bg_color,
                  chroma_tol=defn.chroma_tol, store=False, job_id=job_id,
                  scheduler=_sched(defn))

    # Honour an explicit identity-tier preference; "auto" walks the whole chain.
    # A forced tier still falls back DOWN the chain if its tier isn't live, so the
    # frame always renders (just with less identity fidelity).
    pref = (getattr(defn, "identity_tier", "auto") or "auto").lower()
    allow_cn  = pref in ("auto", "controlnet")
    allow_ip  = pref in ("auto", "controlnet", "ipadapter")
    allow_i2i = pref in ("auto", "controlnet", "ipadapter", "img2img")

    b64, tier, device = "", "", ""

    # 1. ControlNet pose — needs a skeleton/control image (future pose library).
    if control_b64 and allow_cn and tiers.get("controlnet"):
        res = await _call_cap("image.pose", control_image_b64=control_b64,
                              ref_image_b64=(ref_b64 if tiers.get("ipadapter") else ""),
                              **common)
        if (res or {}).get("image_b64"):
            b64, tier, device = res["image_b64"], "controlnet", (res or {}).get("device", "")

    # 2. IP-Adapter — identity from the reference, pose from the prompt text.
    if not b64 and ref_b64 and allow_ip and tiers.get("ipadapter"):
        res = await _call_cap("image.ipadapter", ref_image_b64=ref_b64, scale=0.6, **common)
        if (res or {}).get("image_b64"):
            b64, tier, device = res["image_b64"], "ipadapter", (res or {}).get("device", "")

    # 3. img2img seed-lock from the reference (works today).
    if not b64 and ref_b64 and allow_i2i and tiers.get("img2img"):
        res = await _call_cap("image.img2img", init_image_b64=ref_b64, strength=0.5, **common)
        if (res or {}).get("image_b64"):
            b64, tier, device = res["image_b64"], "img2img", (res or {}).get("device", "")

    # 4. txt2img seed-lock (last resort — identity drifts more).
    if not b64:
        res = await _call_cap("image.generate", **common)
        b64, tier, device = (res or {}).get("image_b64", ""), "txt2img", (res or {}).get("device", "")

    # Every frame must end up with real alpha — not just the base reference.
    if b64:
        try:
            b64, bg_tier = await ensure_frame_alpha(defn, b64, tiers)
            defn.tiers_used["bg"] = bg_tier
        except Exception as e:
            log.warning("spritegen: frame bg cleanup failed: %s", e)
    return b64, tier, device


def _pixelize_to_final(defn, b64: str) -> Image.Image:
    """Quick SINGLE-frame conversion (live previews). Final animation frames go
    through finalize_animation instead so they share one palette."""
    return _pix.pixelize(_pix.b64_to_img(b64), defn.sprite_size,
                         colors=defn.colors, outline=defn.outline, dither=defn.dither,
                         palette_fn=_palette_fn,
                         method=getattr(defn, "downscale", "kcentroid"))


async def finalize_animation(defn, anim: str) -> List[str]:
    """Convert ALL of an animation's stored raw frames together: downscale +
    clean each, build ONE shared palette across the whole cycle and snap every
    frame to it, then outline. A per-frame median-cut palette differs frame to
    frame, which is what made finished animations shimmer like static."""
    char_root = char_dir(defn.char_id)
    raws: List[Image.Image] = []
    for rel in (defn.raw_files.get(anim) or []):
        p = char_root / rel
        if p.is_file():
            raws.append(Image.open(p).convert("RGBA"))
    if not raws:
        return []
    if getattr(defn, "do_pixelize", True):
        method = getattr(defn, "downscale", "kcentroid")
        outs = [_pix.pixelize(im, defn.sprite_size, colors=0, outline=False,
                              dither=False, method=method) for im in raws]
        if defn.colors and defn.colors > 0:
            if getattr(defn, "shared_palette", True) and len(outs) > 1:
                pal = _pal.build_shared_palette(outs, defn.colors)
                outs = [_pal.apply_palette(o, pal, dither=defn.dither) for o in outs]
            else:
                outs = [_pal.reduce_palette(o, colors=defn.colors, dither=defn.dither)
                        for o in outs]
        if defn.outline:
            outs = [_pix.threshold_alpha(_pix.add_outline(o, width=1)) for o in outs]
    else:
        outs = [r.copy() for r in raws]
    # Re-anchor every frame onto one uniform cell (silhouette centred, feet on
    # the floor). Without this the character drifts inside the canvas frame to
    # frame, and a horizontal flip (walking left) mirrors that offset into a
    # visible teleport.
    outs = _pix.center_frames(outs, mode=_center_mode(defn))
    d = char_root / "frames" / _safe(anim)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    fin_files = [_save_img(char_root, f"frames/{_safe(anim)}/{i:03d}.png", o)
                 for i, o in enumerate(outs)]
    defn.frame_files[anim] = fin_files
    await STORE.save(defn)
    # Snap the whole CHARACTER onto one shared cell so switching animations
    # doesn't resize the canvas (the cross-animation position jump).
    try:
        await align_cells(defn)
    except Exception as e:
        log.warning("spritegen: align_cells after %s failed: %s", anim, e)
    return fin_files


async def align_cells(defn) -> Dict:
    """Re-pad every animation's FINISHED frames onto ONE character-wide cell
    (the max cell width/height across all animations), preserving each frame's
    bottom-centre anchor.

    center_frames sizes each animation's cell independently, so an idle cell
    could be 40px wide while the walk cell is 55px. The <vera-sprite> canvas is
    sized per animation, so every animation switch resized the canvas and the
    character visibly shifted position — worst on imported packs whose actions
    have very different extents. With one global cell every sheet slices
    identically and switching animations is positionally a no-op. Padding only
    ever adds transparent margin (centre-x, bottom-anchored), so in-cell motion
    (a jump arc, a lunge) is preserved. Idempotent."""
    char_root = char_dir(defn.char_id)
    loaded: Dict[str, List] = {}
    for anim, files in (defn.frame_files or {}).items():
        pairs = []
        for rel in (files or []):
            p = char_root / rel if rel else None
            if p and p.is_file():
                pairs.append((rel, Image.open(p).convert("RGBA")))
        if pairs:
            loaded[anim] = pairs
    if len(loaded) < 2:
        return {"aligned": [], "animations": list(loaded.keys())}
    cell_w = max(im.width for pairs in loaded.values() for _, im in pairs)
    cell_h = max(im.height for pairs in loaded.values() for _, im in pairs)
    aligned: List[str] = []
    for anim, pairs in loaded.items():
        if all(im.size == (cell_w, cell_h) for _, im in pairs):
            continue
        for rel, im in pairs:
            cell = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
            cell.alpha_composite(im, ((cell_w - im.width) // 2, cell_h - im.height))
            _save_img(char_root, rel, cell)
        aligned.append(anim)
        # Repack already-built sheets so their atlas grid matches the new cell.
        if (defn.sheets or {}).get(anim):
            await build_sheet(defn, anim)
    if aligned:
        await STORE.save(defn)
    return {"aligned": aligned, "cell": [cell_w, cell_h]}


async def generate_animation(defn, anim: str, *, frames: int = 0, portrait: bool = False) -> Dict:
    """Render every frame of one animation, then pixelize + palette-reduce each.
    Keeps the raw hi-res frames for later re-pixelizing."""
    spec = defn.animations.get(anim, {})
    n = int(frames or spec.get("frames", 4))
    tiers = await probe_tiers()
    await _ensure_seed(defn)
    ref_b64 = _load_ref_b64(defn)
    poses = _prompts.anim_poses(defn, anim, n)
    char_root = char_dir(defn.char_id)

    # Clear any previous frames for this animation so we never mix old/new.
    for sub in ("raw", "frames"):
        d = char_root / sub / _safe(anim)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    raw_files: List[str] = []
    tier_used = ""
    device = ""
    # Purpose-built tier: ONE PixelLab call renders the whole cycle against the
    # reference identity — far more temporally consistent than per-frame diffusion.
    if _prov.use_for(defn) and not portrait:
        await emit_event({"type": "spritegen.stage.progress", "char_id": defn.char_id,
                          "stage": anim, "index": 0, "total": n,
                          "job_id": f"sg-{defn.char_id}-{_safe(anim)}-pixellab"})
        pl_frames = await _prov.animate(defn, anim, n, ref_b64 or "")
        if pl_frames:
            raw_files = [_save_b64(char_root, f"raw/{_safe(anim)}/{i:03d}.png", fb)
                         for i, fb in enumerate(pl_frames)]
            tier_used = device = "pixellab"
        else:
            log.warning("spritegen: pixellab animate(%s) failed — using local chain", anim)
    if not raw_files:
        for i, pose in enumerate(poses):
            job_id = f"sg-{defn.char_id}-{_safe(anim)}-{i}-{random.randint(0, 999999):06d}"
            await emit_event({"type": "spritegen.stage.progress", "char_id": defn.char_id,
                              "stage": anim, "index": i, "total": n, "job_id": job_id})
            fp = _prompts.build_frame_prompt(defn, anim, pose, portrait=portrait)
            control_b64 = None if portrait else _control_frame(defn, anim, i, n, tiers)
            b64, tier, dev = await _gen_frame(defn, fp, ref_b64, tiers,
                                              control_b64=control_b64, job_id=job_id)
            if not b64:
                continue
            tier_used, device = tier, (dev or device)
            raw_files.append(_save_b64(char_root, f"raw/{_safe(anim)}/{i:03d}.png", b64))

    defn.raw_files[anim] = raw_files
    defn.tiers_used[anim] = tier_used
    # Batch conversion with ONE shared palette across the cycle (no shimmer).
    fin_files = await finalize_animation(defn, anim)
    await STORE.save(defn)
    await emit_event({"type": "spritegen.stage.done", "char_id": defn.char_id,
                      "stage": anim, "frames": len(fin_files), "tier": tier_used,
                      "device": device})
    return {"anim": anim, "frames": fin_files, "count": len(fin_files),
            "tier": tier_used, "device": device}


async def generate_one_frame(defn, anim: str, index: int, total: int, *,
                             tiers: Optional[Dict] = None, ref_b64: Optional[str] = None,
                             clear: bool = False, portrait: bool = False,
                             job_id: str = "") -> Dict:
    """Generate + pixelize + save ONE animation frame and return its image, so a UI
    can render frames one at a time with live progress. `clear` (or index 0) wipes
    the animation's existing frames first. Frames must be requested in order."""
    tiers = tiers if tiers is not None else await probe_tiers()
    await _ensure_seed(defn)
    if ref_b64 is None:
        ref_b64 = _load_ref_b64(defn)
    char_root = char_dir(defn.char_id)
    if clear or index == 0:
        for sub in ("raw", "frames"):
            d = char_root / sub / _safe(anim)
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        defn.raw_files[anim] = []
        defn.frame_files[anim] = []

    poses = _prompts.anim_poses(defn, anim, max(1, total))
    pose = poses[index] if index < len(poses) else (poses[-1] if poses else anim)
    fp = _prompts.build_frame_prompt(defn, anim, pose, portrait=portrait)
    await emit_event({"type": "spritegen.stage.progress", "char_id": defn.char_id,
                      "stage": anim, "index": index, "total": total, "job_id": job_id})
    b64, tier, dev = "", "", ""
    if _prov.use_for(defn) and not portrait:
        # PixelLab renders the WHOLE cycle on the first frame request; later
        # indexes replay the stored frames (the UI asks for them in order).
        if index == 0:
            pl = await _prov.animate(defn, anim, total, ref_b64 or "")
            if pl:
                defn.raw_files[anim] = [
                    _save_b64(char_root, f"raw/{_safe(anim)}/{i:03d}.png", fb)
                    for i, fb in enumerate(pl)]
        rels = defn.raw_files.get(anim) or []
        if index < len(rels) and rels[index] and (char_root / rels[index]).is_file():
            b64 = base64.b64encode((char_root / rels[index]).read_bytes()).decode()
            tier = dev = "pixellab"
    if not b64:
        control_b64 = None if portrait else _control_frame(defn, anim, index, total, tiers)
        b64, tier, dev = await _gen_frame(defn, fp, ref_b64, tiers,
                                          control_b64=control_b64, job_id=job_id)
    if not b64:
        return {"error": "frame generation returned no image", "index": index, "total": total}

    rawrel = _save_b64(char_root, f"raw/{_safe(anim)}/{index:03d}.png", b64)
    fin_img = (_pixelize_to_final(defn, b64) if getattr(defn, "do_pixelize", True)
               else _pix.b64_to_img(b64))
    finrel = _save_img(char_root, f"frames/{_safe(anim)}/{index:03d}.png", fin_img)

    for store, rel in ((defn.raw_files, rawrel), (defn.frame_files, finrel)):
        lst = store.setdefault(anim, [])
        while len(lst) <= index:
            lst.append("")
        lst[index] = rel
    defn.tiers_used[anim] = tier
    await STORE.save(defn)
    # Last frame of the cycle → re-convert the WHOLE animation with one shared
    # palette (each live preview above used its own; the finished set must not).
    if index >= total - 1 and getattr(defn, "do_pixelize", True):
        try:
            fins = await finalize_animation(defn, anim)
            if fins:
                p = char_root / fins[-1]
                if p.is_file():
                    fin_img = Image.open(p).convert("RGBA")
                    finrel = fins[-1]
        except Exception as e:
            log.warning("spritegen: finalize %s failed: %s", anim, e)
    return {"index": index, "total": total, "anim": anim, "file": finrel,
            "frame_url": f"/spritegen/asset?char_id={defn.char_id}&path={finrel}",
            "image_b64": _pix.img_to_b64(fin_img), "tier": tier, "device": dev}


async def repixelize_animation(defn, anim: str) -> Dict:
    """Re-run pixelize + palette on the stored raw frames (no diffusion). Lets the
    user change sprite_size / colors / outline / downscale method cheaply."""
    raw = defn.raw_files.get(anim) or []
    if not raw:
        return {"error": f"no raw frames for {anim}; run generate_animation first"}
    fin_files = await finalize_animation(defn, anim)
    return {"anim": anim, "count": len(fin_files)}


def _raw_b64(char_root: Path, rel: str) -> str:
    p = char_root / rel
    return base64.b64encode(p.read_bytes()).decode() if p.is_file() else ""


async def edit_animation(defn, anim: str, instruction: str, *, strength: float = 0.45,
                         frame_indices: Optional[List[int]] = None, job_id: str = "") -> Dict:
    """Edit an EXISTING animation's frames with Stable Diffusion (img2img), steered
    by a text instruction and SEED-LOCKED so the whole cycle changes coherently
    rather than each frame drifting on its own. Works on the stored hi-res RAW
    frames (generated OR imported), rewrites them in place, then re-pixelizes with a
    shared palette and repacks the sheet. `strength` (0..1) is how far to move from
    the original — low (~0.3) tweaks colours/details, higher (~0.6) restyles;
    `frame_indices` limits the edit to specific frames (None = the whole cycle)."""
    tiers = await probe_tiers()
    if not (tiers.get("img2img") or tiers.get("txt2img")):
        return {"error": "SD img2img is not available on the GPU node"}
    await _ensure_seed(defn)
    char_root = char_dir(defn.char_id)
    raws = list(defn.raw_files.get(anim) or [])
    if not raws:
        return {"error": f"'{anim}' has no raw frames to edit — generate or import it first"}
    idxs = [i for i in (list(frame_indices) if frame_indices else range(len(raws)))
            if 0 <= i < len(raws)]
    if not idxs:
        return {"error": "no valid frame indices for this animation"}
    base = _prompts.build_base_prompt(defn)
    prompt = (f"{base}, {instruction}".strip().strip(",") if instruction else base)
    total, edited = len(idxs), 0
    for n, i in enumerate(idxs):
        await emit_event({"type": "spritegen.stage.progress", "char_id": defn.char_id,
                          "stage": "edit", "index": n, "total": total, "anim": anim,
                          "job_id": job_id})
        init_b64 = _raw_b64(char_root, raws[i])
        if not init_b64:
            continue
        res = await _call_cap(
            "image.img2img", init_image_b64=init_b64, strength=float(strength),
            prompt=prompt, negative_prompt=_negative(defn),
            steps=defn.steps, guidance=defn.guidance, seed=defn.seed,
            width=defn.gen_width, height=defn.gen_height, loras=defn.loras,
            transparent=_use_chroma(defn, tiers), bg_color=defn.bg_color,
            chroma_tol=defn.chroma_tol, store=False, job_id=job_id,
            scheduler=_sched(defn))
        b64 = (res or {}).get("image_b64", "")
        if not b64:
            continue
        b64, _ = await ensure_frame_alpha(defn, b64, tiers)
        _save_img(char_root, raws[i], _pix.b64_to_img(b64))   # overwrite the raw in place
        edited += 1
    if not edited:
        return {"error": "no frames were edited (img2img returned nothing — is SD loaded?)"}
    await finalize_animation(defn, anim)
    defn.tiers_used[anim] = "sd-edit"
    await STORE.save(defn)
    sheet = await build_sheet(defn, anim)
    await emit_event({"type": "spritegen.stage.done", "char_id": defn.char_id,
                      "stage": "edit", "anim": anim})
    return {"anim": anim, "edited": edited,
            "frames": len(defn.frame_files.get(anim) or []),
            "sheet": sheet.get("sheet"), "gif": sheet.get("gif"), "tier": "sd-edit"}


# ── stage: sheet + package ────────────────────────────────────────────────────

async def build_sheet(defn, anim: str, columns: int = 0) -> Dict:
    fin = defn.frame_files.get(anim) or []
    char_root = char_dir(defn.char_id)
    imgs = [Image.open(char_root / f).convert("RGBA")
            for f in fin if (char_root / f).is_file()]
    if not imgs:
        return {"error": f"no frames on disk for {anim}; generate it first"}
    spec = defn.animations.get(anim, {})
    fps = int(spec.get("fps", 8))
    loop = bool(spec.get("loop", True))
    sheet, meta = _sheet.pack_frames(imgs, columns=columns)
    atlas = _sheet.atlas_json(anim, meta, fps=fps, loop=loop)
    gif = _sheet.gif_bytes(imgs, fps=fps, loop=loop)

    sfile = _save_img(char_root, f"sheets/{_safe(anim)}.png", sheet)
    jfile = f"sheets/{_safe(anim)}.json"
    (char_root / jfile).write_text(json.dumps(atlas, indent=2), encoding="utf-8")
    gfile = ""
    if gif:
        gfile = f"sheets/{_safe(anim)}.gif"
        (char_root / gfile).write_bytes(gif)

    defn.sheets[anim] = {"file": sfile, "json": jfile, "gif": gfile,
                         "fps": fps, "loop": loop, **meta}
    await STORE.save(defn)
    await emit_event({"type": "spritegen.stage.done", "char_id": defn.char_id,
                      "stage": f"sheet:{anim}", "count": meta["count"]})
    return {"anim": anim, "sheet": sfile, "json": jfile, "gif": gfile,
            "meta": meta, "fps": fps}


async def build_package(defn) -> Dict:
    char_root = char_dir(defn.char_id)
    man = _pkg.build_package(defn, char_root)
    defn.package_dir = man.get("package_dir", "")
    await STORE.save(defn)
    await emit_event({"type": "spritegen.stage.done", "char_id": defn.char_id,
                      "stage": "package", "files": len(man.get("files", []))})
    return man


# ── full run ──────────────────────────────────────────────────────────────────

async def run_pipeline(defn, anims: Optional[List[str]] = None, *,
                       do_base: bool = False, do_package: bool = True) -> Dict:
    # Reuse an existing reference by default — only (re)generate it when explicitly
    # asked (do_base=True) or when none exists yet. Regenerating is expensive and
    # changes the character, so it must be opt-in.
    anims = anims or list(defn.animations.keys())
    await emit_event({"type": "spritegen.run.start", "char_id": defn.char_id,
                      "animations": anims, "regen_reference": bool(do_base)})
    if do_base or not defn.reference:
        r = await generate_base(defn)
        if r.get("error"):
            await emit_event({"type": "spritegen.run.error", "char_id": defn.char_id,
                              "error": r["error"]})
            return {"error": f"base generation failed: {r['error']}"}

    results: Dict[str, Dict] = {}
    for anim in anims:
        ga = await generate_animation(defn, anim)
        if ga.get("count"):
            bs = await build_sheet(defn, anim)
            results[anim] = {"frames": ga.get("count"), "tier": ga.get("tier"),
                             "sheet": bs.get("sheet"), "gif": bs.get("gif")}
        else:
            results[anim] = {"frames": 0, "error": "no frames generated"}

    package = await build_package(defn) if do_package else None
    await emit_event({"type": "spritegen.run.done", "char_id": defn.char_id,
                      "animations": list(results.keys())})
    return {"char_id": defn.char_id, "animations": results, "package": package}
