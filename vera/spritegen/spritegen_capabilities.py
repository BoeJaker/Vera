"""spritegen_capabilities.py — capabilities + routes + UI for the sprite pipeline
================================================================================
Thin @capability wrappers over ``pipeline.py`` (the engine) and ``definition.py``
(the store), plus the read-only asset route and the Sprite Studio panel.

This is the ONLY spritegen file registered in ``_module_files`` (loaded by
basename), so the decorators run exactly once; the stage modules are imported via
the ``Vera.vera.spritegen`` package.

Capabilities
────────────
  spritegen.capabilities      — which generation tiers are live + the vocab
  spritegen.describe          — LLM: freeform brief → structured identity fields
  spritegen.define            — create/update a character definition
  spritegen.get/list/set/delete
  spritegen.generate_base     — generate the bg-removed reference image
  spritegen.generate_animation— render + pixelize one animation's frames
  spritegen.repixelize        — re-pixelize stored raw frames (no diffusion)
  spritegen.import_frames     — hand-drawn frame list → animation (drawing editor)
  spritegen.from_image        — any image → bg-removed, pixelized sprite frame
  spritegen.align_cells       — one shared frame cell across every animation
  spritegen.build_sheet       — pack an animation into a sheet + atlas + gif
  spritegen.build_package     — assemble + zip the Character/ package
  spritegen.run_pipeline      — base → animations → sheets → package (async)

UI
──
  register_ui("sprite-studio", mode="tab")
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
    ollama_generate, register_ui,
)
from Vera.vera.spritegen import importer as IMP
from Vera.vera.spritegen import pipeline as P
from Vera.vera.spritegen import prompts as _prompts
from Vera.vera.spritegen.definition import (
    STORE, CharacterDefinition, DEFAULT_ANIMATIONS, DEFAULT_VISEMES,
    _CHARS_DIR, char_dir, new_char_id, _safe,
)

log = logging.getLogger("vera.spritegen")
_HERE = Path(__file__).parent


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_json(text: str):
    if not text:
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


def _asset_url(char_id: str, rel: str, ver: int) -> str:
    return f"/spritegen/asset?char_id={char_id}&path={rel}&v={ver}"


def _record_with_urls(defn: CharacterDefinition) -> dict:
    d = defn.to_dict()
    ver = int(time.time())
    urls: Dict[str, Any] = {}
    if defn.reference:
        urls["reference"] = _asset_url(defn.char_id, defn.reference, ver)
    urls["sheets"] = {
        anim: {
            "png":  _asset_url(defn.char_id, m.get("file", ""), ver),
            "gif":  _asset_url(defn.char_id, m["gif"], ver) if m.get("gif") else "",
            "json": _asset_url(defn.char_id, m.get("json", ""), ver),
        } for anim, m in (defn.sheets or {}).items()
    }
    urls["frames"] = {
        anim: [_asset_url(defn.char_id, f, ver) for f in files]
        for anim, files in (defn.frame_files or {}).items()
    }
    if defn.package_dir:
        urls["package_zip"] = _asset_url(defn.char_id, "package/Character.zip", ver)
    d["urls"] = urls
    return d


async def _get_defn_or_err(char_id: str):
    if not char_id:
        return None, {"error": "char_id required"}
    defn = await STORE.get(char_id)
    if not defn or defn.archived:
        return None, {"error": "no such character", "char_id": char_id}
    return defn, None


def _coerce_json_obj(val) -> Optional[dict]:
    """Accept a JSON object string (or an already-parsed dict) → a plain dict, else
    None. Used for the control/buddy binding maps (keymap/dirmap/events/autonomy)."""
    if isinstance(val, dict):
        return val
    data = _parse_json(val)
    return data if isinstance(data, dict) else None


def _coerce_animations(animations: str) -> Optional[Dict[str, dict]]:
    """Accept a JSON object, a csv of names, or empty (→ defaults)."""
    if not animations:
        return None
    data = _parse_json(animations)
    if isinstance(data, dict):
        out = {}
        for name, spec in data.items():
            if isinstance(spec, dict):
                out[name] = {"frames": int(spec.get("frames", 4)),
                             "fps": int(spec.get("fps", 8)),
                             "loop": bool(spec.get("loop", True)),
                             "desc": str(spec.get("desc", ""))}
                # Preserve optional per-frame pose control (prompts.anim_poses):
                #   "poses": list/newline-string of per-frame phrases, or "prompt": one phrase.
                if spec.get("poses") is not None:
                    out[name]["poses"] = spec["poses"]
                if spec.get("prompt"):
                    out[name]["prompt"] = str(spec["prompt"])
                if spec.get("view"):
                    out[name]["view"] = str(spec["view"])
            else:
                out[name] = dict(DEFAULT_ANIMATIONS.get(name, {"frames": 4, "fps": 8, "loop": True}))
        return out
    # csv of names → pull defaults where known
    names = [a.strip() for a in animations.split(",") if a.strip()]
    if names:
        return {n: dict(DEFAULT_ANIMATIONS.get(n, {"frames": 4, "fps": 8, "loop": True}))
                for n in names}
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "spritegen.capabilities", memory="off", silent=True,
    http_method="GET", http_path="/spritegen/capabilities", http_tags=["spritegen"],
    description="Report which sprite-generation tiers are live on the GPU node "
                "(txt2img/img2img/rembg/controlnet/ipadapter/upscale) plus the style, "
                "animation and viseme vocabulary. Output: {tiers:{...}, styles:[...], "
                "animations:[...], visemes:[...]}.",
)
async def spritegen_capabilities(trace_id=None):
    from Vera.vera.spritegen import skeleton as _skel
    tiers = await P.probe_tiers()
    return {
        "tiers": tiers,
        "styles": list(_prompts.STYLE_DESCRIPTOR.keys()),
        # Preset style descriptors — the UI shows/edits these as the character's
        # style_prompt (the SD stage renders flat art; the pipeline pixelizes).
        "style_descriptors": dict(_prompts.STYLE_DESCRIPTOR),
        "animations": list(DEFAULT_ANIMATIONS.keys()),
        "skeleton_anims": list(_skel.SKELETON_ANIMS),
        "visemes": list(DEFAULT_VISEMES),
        # 96+ = "modern retro remake" territory (pair with the hdpixel style,
        # more colors and a larger gen size for genuinely finer detail).
        "sprite_sizes": [16, 32, 48, 64, 96, 128, 192, 256],
    }


@capability(
    "spritegen.describe", memory="off",
    http_method="POST", http_path="/spritegen/describe", http_tags=["spritegen"],
    description="Expand a freeform character brief into structured identity fields via "
                "the LLM. Input: brief (str!), style, animations (csv), notes (extra art "
                "direction the LLM must obey). Output: {name, base_prompt, "
                "layers:{hair,eyes,clothing,weapon,...}, palette, negative_prompt, "
                "animation_notes:{anim:note}, style}.",
)
async def spritegen_describe(brief: str = "", style: str = "pixel",
                             animations: str = "", notes: str = "", trace_id=None):
    if not brief.strip():
        return {"error": "brief required"}
    anims = [a.strip() for a in animations.split(",") if a.strip()] or list(DEFAULT_ANIMATIONS.keys())
    try:
        raw = await ollama_generate(prompt=_prompts.describe_user_prompt(brief, style, anims, notes),
                                    system=_prompts.describe_system_prompt(),
                                    json_mode=True, job_type="chat")
    except Exception as e:
        log.warning("spritegen.describe: LLM failed (%s); using brief as base", e)
        raw = ""
    data = _parse_json(raw) or {}
    return {
        "name":            (data.get("name") or "").strip(),
        "base_prompt":     (data.get("base_prompt") or brief).strip(),
        "layers":          data.get("layers") if isinstance(data.get("layers"), dict) else {},
        "palette":         (data.get("palette") or "").strip(),
        "negative_prompt": (data.get("negative_prompt") or "").strip(),
        "animation_notes": data.get("animation_notes") if isinstance(data.get("animation_notes"), dict) else {},
        "style":           style,
    }


@capability(
    "spritegen.define", memory="on",
    http_method="POST", http_path="/spritegen/define", http_tags=["spritegen"],
    description="Create or update a character definition (the pipeline's source of "
                "truth). Inputs: char_id (omit to create), name, brief, base_prompt "
                "(omit → LLM-expanded from brief), style, sprite_size (16/32/64/128), "
                "colors, outline (bool), dither (bool), downscale (kcentroid|lanczos), "
                "shared_palette (bool, one palette per animation — prevents flicker), "
                "palette, negative_prompt, framing "
                "(override the short framing suffix), layers (JSON), animations (JSON or csv; "
                "each may carry 'poses'/'prompt' for per-frame pose control), notes (extra "
                "art-direction passed to the LLM when expanding a brief), seed, steps, "
                "guidance, gen_width, gen_height, transparent (bool), bg_color, loras, "
                "agent_id, and pipeline stage controls: identity_tier "
                "(auto|ipadapter|img2img|txt2img), bg_removal (auto|rembg|chroma|none), "
                "chroma_tol (int, higher = removes more), rembg_model, rembg_alpha_matting "
                "(bool), do_pixelize (bool), do_upscale (bool). Output: record + asset URLs.",
)
async def spritegen_define(
    char_id:        str = "",
    name:           str = "",
    brief:          str = "",
    base_prompt:    str = "",
    base_pose:      str = "",       # reference stance ("" = neutral A-pose default)
    style:          str = "pixel",
    style_prompt:   str = "",       # editable style portion of the SD prompt
                                    # ("" = the preset for `style`)
    framing:        str = "",
    sprite_size:    int = 64,
    colors:         int = 32,
    outline:        bool = False,
    dither:         bool = False,
    downscale:      str = "",       # kcentroid | lanczos ("" keeps current)
    shared_palette: Optional[bool] = None,
    palette:        str = "",
    negative_prompt: str = "",
    layers:         str = "",      # JSON object
    animations:     str = "",      # JSON object or csv of names
    visemes:        str = "",      # csv
    notes:          str = "",      # extra art direction for the LLM expansion
    seed:           int = -1,
    steps:          int = 24,
    guidance:       float = 7.5,
    scheduler:      str = "",       # per-job sampler (dpmpp|euler|euler_a|unipc|ddim|lcm);
                                    # "" keeps current, "default" = the model's default
    gen_width:      int = 768,
    gen_height:     int = 768,
    transparent:    bool = True,
    bg_color:       str = "",
    loras:          str = "",
    agent_id:       str = "",
    # ── pipeline stage controls ──
    identity_tier:  str = "auto",   # auto | ipadapter | img2img | txt2img
    pose_source:    str = "auto",   # auto | skeleton | reference | none (ControlNet input)
    auto_center:    str = "centroid",  # centroid | bbox | none — silhouette re-anchoring
    bg_removal:     str = "auto",   # auto | rembg | chroma | none
    chroma_tol:     int = 80,       # chroma-key aggressiveness
    rembg_model:    str = "",       # u2net | u2netp | isnet-general-use | …
    rembg_alpha_matting: bool = False,
    do_pixelize:    bool = True,
    do_upscale:     bool = False,
    seed_lock:      Optional[bool] = None,   # pin the seed across regenerations
    trace_id=None,
):
    defn = (await STORE.get(char_id)) if char_id else None
    if defn is None:
        defn = CharacterDefinition(char_id=char_id or new_char_id())

    # Identity: expand the brief with the LLM if no base prompt is known yet.
    if base_prompt:
        defn.base_prompt = base_prompt
    if brief:
        defn.brief = brief
    if not defn.base_prompt:
        described = await spritegen_describe(brief=brief or name, style=style,
                                             animations=animations, notes=notes)
        if not described.get("error"):
            defn.base_prompt = described["base_prompt"]
            defn.name = defn.name or described.get("name") or name
            defn.layers = defn.layers or described.get("layers") or {}
            defn.palette = defn.palette or described.get("palette", "")
            if described.get("negative_prompt"):
                defn.negative_prompt = described["negative_prompt"]

    # Scalar fields (only overwrite when a non-empty/sentinel value is given).
    defn.name        = name or defn.name
    defn.style       = style or defn.style
    defn.style_prompt = style_prompt  # "" means "use the preset for the style"
    defn.framing     = framing      # "" means "use the default short framing"
    defn.base_pose   = base_pose    # "" means "use the neutral A-pose default"
    defn.sprite_size = int(sprite_size) or defn.sprite_size
    defn.colors      = int(colors)
    defn.outline     = bool(outline)
    defn.dither      = bool(dither)
    if downscale:               defn.downscale = downscale
    if shared_palette is not None: defn.shared_palette = bool(shared_palette)
    if palette:         defn.palette = palette
    if negative_prompt: defn.negative_prompt = negative_prompt
    defn.seed       = int(seed)
    if seed_lock is not None: defn.seed_lock = bool(seed_lock)
    defn.steps      = int(steps)
    defn.guidance   = float(guidance)
    if scheduler:   defn.scheduler = ("" if scheduler == "default" else scheduler)
    defn.gen_width  = int(gen_width)
    defn.gen_height = int(gen_height)
    defn.transparent = bool(transparent)
    if bg_color:    defn.bg_color = bg_color
    if loras:       defn.loras = loras
    if agent_id:    defn.agent_id = agent_id
    # pipeline stage controls
    defn.identity_tier       = identity_tier or defn.identity_tier
    defn.pose_source         = pose_source or defn.pose_source
    defn.auto_center         = auto_center or defn.auto_center
    defn.bg_removal          = bg_removal or defn.bg_removal
    defn.chroma_tol          = int(chroma_tol)
    defn.rembg_model         = rembg_model
    defn.rembg_alpha_matting = bool(rembg_alpha_matting)
    defn.do_pixelize         = bool(do_pixelize)
    defn.do_upscale          = bool(do_upscale)

    parsed_layers = _parse_json(layers)
    if isinstance(parsed_layers, dict):
        defn.layers = {**defn.layers, **{k: str(v) for k, v in parsed_layers.items()}}
    anims = _coerce_animations(animations)
    if anims:
        defn.animations = anims
    vis = [v.strip() for v in visemes.split(",") if v.strip()]
    if vis:
        defn.visemes = vis

    await STORE.save(defn)
    return _record_with_urls(defn)


@capability(
    "spritegen.get", memory="off",
    http_method="GET", http_path="/spritegen/get", http_tags=["spritegen"],
    description="Get a character definition + asset URLs. Input: char_id (str!).",
)
async def spritegen_get(char_id: str = "", trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    return _record_with_urls(defn)


@capability(
    "spritegen.list", memory="off",
    http_method="GET", http_path="/spritegen/list", http_tags=["spritegen"],
    description="List all character definitions. Output: {characters:[...], count}.",
)
async def spritegen_list(trace_id=None):
    recs = await STORE.list()
    return {"characters": [_record_with_urls(r) for r in recs], "count": len(recs)}


@capability(
    "spritegen.set", memory="off",
    http_method="POST", http_path="/spritegen/set", http_tags=["spritegen"],
    description="Update a definition's config WITHOUT regenerating assets (style, "
                "sprite_size, colors, outline, dither, downscale (kcentroid|lanczos), "
                "shared_palette, palette, agent_id, name, animations, seed, seed_lock, "
                "and the control/buddy binding maps keymap/dirmap/events/autonomy — each "
                "a JSON object string). Empty/sentinel values are ignored. seed=-1 clears "
                "the seed so the next base render re-rolls it. Input: char_id (str!). "
                "Output: record.",
)
async def spritegen_set(
    char_id:     str = "",
    name:        str = "",
    style:       str = "",
    framing:     str = "",
    sprite_size: int = 0,
    colors:      int = -1,
    outline:     Optional[bool] = None,
    dither:      Optional[bool] = None,
    downscale:   str = "",
    shared_palette: Optional[bool] = None,
    palette:     str = "",
    negative_prompt: str = "",
    animations:  str = "",
    agent_id:    str = "",
    identity_tier: str = "",
    pose_source: str = "",
    auto_center: str = "",
    base_pose:   str = "",
    bg_removal:  str = "",
    chroma_tol:  int = -1,
    do_pixelize: Optional[bool] = None,
    do_upscale:  Optional[bool] = None,
    seed:        int = -2,            # -2 sentinel = unchanged; -1 = re-roll next base
    scheduler:   str = "",            # "" unchanged; "default" clears to the model default
    seed_lock:   Optional[bool] = None,
    keymap:      str = "",           # JSON {key: anim}
    dirmap:      str = "",           # JSON {key: "up"|"down"|"left"|"right"}
    events:      str = "",           # JSON {slot: anim}
    autonomy:    str = "",           # JSON {enabled, wander, emote, idle_after, min_ms, max_ms}
    trace_id=None,
):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if name:               defn.name = name
    if style:              defn.style = style
    if framing:            defn.framing = framing
    if base_pose:          defn.base_pose = base_pose
    if pose_source:        defn.pose_source = pose_source
    if auto_center:        defn.auto_center = auto_center
    if sprite_size:        defn.sprite_size = int(sprite_size)
    if colors >= 0:        defn.colors = int(colors)
    if outline is not None: defn.outline = bool(outline)
    if dither is not None:  defn.dither = bool(dither)
    if downscale:          defn.downscale = downscale
    if shared_palette is not None: defn.shared_palette = bool(shared_palette)
    if palette:            defn.palette = palette
    if negative_prompt:    defn.negative_prompt = negative_prompt
    if agent_id:           defn.agent_id = agent_id
    if identity_tier:      defn.identity_tier = identity_tier
    if bg_removal:         defn.bg_removal = bg_removal
    if chroma_tol >= 0:    defn.chroma_tol = int(chroma_tol)
    if do_pixelize is not None: defn.do_pixelize = bool(do_pixelize)
    if do_upscale is not None:  defn.do_upscale = bool(do_upscale)
    if seed >= -1:              defn.seed = int(seed)
    if scheduler:               defn.scheduler = ("" if scheduler == "default" else scheduler)
    if seed_lock is not None:   defn.seed_lock = bool(seed_lock)
    for arg, attr in (("keymap", "keymap"), ("dirmap", "dirmap"),
                      ("events", "events"), ("autonomy", "autonomy")):
        val = locals().get(arg)
        if val:
            parsed = _coerce_json_obj(val)
            if parsed is not None:
                setattr(defn, attr, parsed)
    anims = _coerce_animations(animations)
    if anims:
        defn.animations = anims
    await STORE.save(defn)
    return _record_with_urls(defn)


@capability(
    "spritegen.delete", memory="off",
    http_method="POST", http_path="/spritegen/delete", http_tags=["spritegen"],
    description="Delete a character definition (record + all asset files). Input: char_id.",
)
async def spritegen_delete(char_id: str = "", trace_id=None):
    ok = await STORE.delete(char_id)
    return {"deleted": ok, "char_id": char_id}


@capability(
    "spritegen.generate_base", memory="on",
    http_method="POST", http_path="/spritegen/generate_base", http_tags=["spritegen", "gpu"],
    description="Generate the hi-res reference image and remove its background (rembg "
                "tier → chroma-key fallback). Inputs: char_id (str!), job_id (optional — "
                "poll live diffusion progress via image.progress with the same id). Emits "
                "spritegen.stage.* events. Output: {reference, bg_tier, device} or {error}. "
                "NOTE: SD may run on CPU (~30-60s).",
)
async def spritegen_generate_base(char_id: str = "", job_id: str = "", trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    res = await P.generate_base(defn, job_id=job_id)
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.generate_animation", memory="on",
    http_method="POST", http_path="/spritegen/generate_animation", http_tags=["spritegen", "gpu"],
    description="Render one animation's frames (best identity-preserving tier: ControlNet "
                "→ IP-Adapter → img2img seed-lock → txt2img) then pixelize + palette-reduce "
                "each. Inputs: char_id (str!), anim (str!), frames (int, 0=use definition), "
                "portrait (bool, head-and-shoulders framing for talk/blink). Emits progress. "
                "Output: {anim, count, tier, frame_urls}.",
)
async def spritegen_generate_animation(char_id: str = "", anim: str = "",
                                       frames: int = 0, portrait: bool = False, trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if not anim:
        return {"error": "anim required"}
    if not defn.reference:
        base = await P.generate_base(defn)
        if base.get("error"):
            return {"error": f"reference needed first and base failed: {base['error']}"}
    res = await P.generate_animation(defn, anim, frames=int(frames), portrait=bool(portrait))
    out = _record_with_urls(defn)
    out.update({"anim": res.get("anim"), "count": res.get("count"),
                "tier": res.get("tier"), "device": res.get("device")})
    return out


@capability(
    "spritegen.generate_frame", memory="off",
    http_method="POST", http_path="/spritegen/generate_frame", http_tags=["spritegen", "gpu"],
    description="Generate, pixelize and save ONE animation frame and return its image — so a UI "
                "can render frames one at a time with live progress and an ETA. Inputs: char_id "
                "(str!), anim (str!), index (int), total (int, frames in the cycle), clear (bool: "
                "wipe the animation's frames first; auto when index=0), portrait (bool), job_id "
                "(optional — poll live diffusion progress via image.progress with the same id). "
                "Output: {index, total, anim, frame_url, image_b64, tier, device}. Frames must be "
                "requested in order. Generates the reference first if none exists.",
)
async def spritegen_generate_frame(char_id: str = "", anim: str = "", index: int = 0,
                                   total: int = 0, clear: bool = False,
                                   portrait: bool = False, job_id: str = "", trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if not anim:
        return {"error": "anim required"}
    if not defn.reference:
        base = await P.generate_base(defn, job_id=job_id)
        if base.get("error"):
            return {"error": f"reference needed first and base failed: {base['error']}"}
    return await P.generate_one_frame(defn, anim, int(index), int(total or 1),
                                      clear=bool(clear), portrait=bool(portrait),
                                      job_id=job_id)


@capability(
    "spritegen.repixelize", memory="off",
    http_method="POST", http_path="/spritegen/repixelize", http_tags=["spritegen"],
    description="Re-run pixelize + palette reduction on an animation's stored raw frames "
                "WITHOUT re-running diffusion — for cheaply changing sprite_size / colors / "
                "outline. Inputs: char_id (str!), anim (str!). Output: {anim, count}.",
)
async def spritegen_repixelize(char_id: str = "", anim: str = "", trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if not anim:
        return {"error": "anim required"}
    res = await P.repixelize_animation(defn, anim)
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.edit_animation", memory="on",
    http_method="POST", http_path="/spritegen/edit_animation", http_tags=["spritegen", "gpu"],
    description="Edit an EXISTING animation's frames with Stable Diffusion (img2img), steered "
                "by a text instruction and seed-locked so the whole cycle changes coherently. "
                "Works on generated OR imported animations; rewrites the raw frames then "
                "re-pixelizes (shared palette) + repacks the sheet. Inputs: char_id (str!), "
                "anim (str!), instruction (str! — e.g. 'give them a red cape', 'make it "
                "night-time'), strength (0..1, default 0.45 — low tweaks, high restyles), "
                "frames (optional csv of frame indices to limit the edit), job_id (optional — "
                "poll image.progress). Output: {anim, edited, frames, sheet, gif} + URLs.",
)
async def spritegen_edit_animation(char_id: str = "", anim: str = "", instruction: str = "",
                                   strength: float = 0.45, frames: str = "", job_id: str = "",
                                   trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if not anim:
        return {"error": "anim required"}
    if not instruction.strip():
        return {"error": "instruction required (what should change?)"}
    idxs = [int(x) for x in re.findall(r"\d+", frames)] if frames else None
    res = await P.edit_animation(defn, anim, instruction.strip(),
                                 strength=float(strength), frame_indices=idxs, job_id=job_id)
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.import_sheet", memory="on",
    http_method="POST", http_path="/spritegen/import_sheet", http_tags=["spritegen"],
    description="Import a bare sprite-sheet IMAGE as one animation of a character — no "
                "diffusion. Slices the grid (explicit frame_width/frame_height or "
                "columns/rows, else auto-detected from transparent gutters / square cells), "
                "keys out a solid background when the sheet has no alpha, centres each "
                "frame's silhouette (so left/right flipping doesn't teleport), then packs "
                "sheet + atlas + GIF. Inputs: image_b64 (str!), anim (str, default 'idle'), "
                "char_id (omit to create a new character), name (for a new character), "
                "frame_width, frame_height, columns, rows (ints, 0 = auto), "
                "crop_x/crop_y/crop_w/crop_h (ints — select just a region of the image to "
                "import as this animation; 0 width/height = whole image), fps, loop, "
                "process ('auto'|'keep'|'pixelize' — keep leaves the pixels untouched). "
                "Output: {char_id, anim, count, sheet, gif} + record URLs.",
)
async def spritegen_import_sheet(image_b64: str = "", anim: str = "idle",
                                 char_id: str = "", name: str = "",
                                 frame_width: int = 0, frame_height: int = 0,
                                 columns: int = 0, rows: int = 0,
                                 crop_x: int = 0, crop_y: int = 0,
                                 crop_w: int = 0, crop_h: int = 0,
                                 fps: int = 0, loop: bool = True,
                                 process: str = "auto", trace_id=None):
    if not image_b64:
        return {"error": "image_b64 required"}
    anim = _safe(anim.lower()) or "idle"
    defn = (await STORE.get(char_id)) if char_id else None
    if defn is None:
        defn = CharacterDefinition(char_id=char_id or new_char_id(),
                                   name=name or "imported", animations={})
        defn.do_pixelize = False        # imported pixels are already final
        await STORE.save(defn)
    elif name:
        defn.name = name
    default_fps = int((DEFAULT_ANIMATIONS.get(anim) or {}).get("fps", 8))
    crop = (int(crop_x), int(crop_y), int(crop_w), int(crop_h)) if int(crop_w) and int(crop_h) else None
    res = await IMP.import_sheet(defn, anim, image_b64,
                                 frame_width=int(frame_width), frame_height=int(frame_height),
                                 columns=int(columns), rows=int(rows), crop=crop,
                                 fps=int(fps) or default_fps, loop=bool(loop),
                                 process=process)
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.splice", memory="on",
    http_method="POST", http_path="/spritegen/splice", http_tags=["spritegen"],
    description="Compose a NEW animation by splicing together frames from the character's "
                "EXISTING animations (no diffusion). Inputs: char_id (str!), anim (str! — the "
                "new animation name), picks (JSON list of {anim, frame} in play order), fps "
                "(int), loop (bool), reverse (bool — flip order), pingpong (bool — append the "
                "reverse of the interior frames). Output: {anim, count, sheet, gif} + URLs.",
)
async def spritegen_splice(char_id: str = "", anim: str = "", picks: str = "",
                           fps: int = 8, loop: bool = True,
                           reverse: bool = False, pingpong: bool = False, trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    anim = _safe(anim.lower())
    if not anim:
        return {"error": "anim (new animation name) required"}
    parsed = _parse_json(picks)
    if not isinstance(parsed, list) or not parsed:
        return {"error": "picks must be a non-empty JSON list of {anim, frame}"}
    res = await IMP.splice_frames(defn, anim, parsed, fps=int(fps) or 8, loop=bool(loop),
                                  reverse=bool(reverse), pingpong=bool(pingpong))
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.import_package", memory="on",
    http_method="POST", http_path="/spritegen/import_package", http_tags=["spritegen"],
    description="Import a ZIP of sprite sheets as a whole character. Understands the "
                "exported Character/ package (metadata.json drives exact slicing, the "
                "reference comes along) AND generic zips of sheet PNGs (each optionally "
                "paired with an atlas .json; otherwise the grid is auto-detected and the "
                "animation is named after the file). Inputs: zip_b64 (str!), char_id "
                "(omit to create), name, process ('auto'|'keep'|'pixelize'). Output: "
                "{char_id, imported:{anim:{count,...}}, errors:{anim:msg}} + record URLs.",
)
async def spritegen_import_package(zip_b64: str = "", char_id: str = "",
                                   name: str = "", process: str = "auto", trace_id=None):
    if not zip_b64:
        return {"error": "zip_b64 required"}
    import base64 as _b64
    try:
        blob = _b64.b64decode(zip_b64)
    except Exception as e:
        return {"error": f"bad base64: {e}"}
    res = await IMP.import_package(blob, char_id=char_id, name=name, process=process)
    if res.get("error"):
        return res
    defn = await STORE.get(res["char_id"])
    out = _record_with_urls(defn) if defn else {}
    out.update(res)
    return out


@capability(
    "spritegen.import_frames", memory="on",
    http_method="POST", http_path="/spritegen/import_frames", http_tags=["spritegen"],
    description="Import a LIST of individual frame images as one animation of a character "
                "— no diffusion. This is how the Sprite Studio's drawing editor saves "
                "hand-drawn animations. Inputs: frames_b64 (str! — JSON array of base64 "
                "PNGs, in play order), anim (str, default 'idle'), char_id (omit to create "
                "a new character), name (for a new character), fps, loop, process "
                "('keep'|'pixelize'|'auto' — keep leaves pixels untouched), center (bool, "
                "default false — hand-drawn frames position content deliberately, so the "
                "silhouette re-anchor is off unless asked for). Output: {char_id, anim, "
                "count, sheet, gif} + record URLs.",
)
async def spritegen_import_frames(frames_b64: str = "", anim: str = "idle",
                                  char_id: str = "", name: str = "",
                                  fps: int = 0, loop: bool = True,
                                  process: str = "keep", center: bool = False,
                                  trace_id=None):
    parsed = _parse_json(frames_b64) if isinstance(frames_b64, str) else frames_b64
    if not isinstance(parsed, list) or not parsed:
        return {"error": "frames_b64 must be a non-empty JSON array of base64 PNGs"}
    import base64 as _b64
    import io as _io
    from PIL import Image as _Image
    frames = []
    for i, fb in enumerate(parsed):
        try:
            s = str(fb or "")
            if "," in s and s.lstrip().lower().startswith("data:"):
                s = s.split(",", 1)[1]
            frames.append(_Image.open(_io.BytesIO(_b64.b64decode(s))).convert("RGBA"))
        except Exception as e:
            return {"error": f"frame {i} could not be decoded: {e}"}
    anim = _safe(anim.lower()) or "idle"
    defn = (await STORE.get(char_id)) if char_id else None
    if defn is None:
        defn = CharacterDefinition(char_id=char_id or new_char_id(),
                                   name=name or "drawn", animations={})
        defn.do_pixelize = False        # drawn pixels are already final
        await STORE.save(defn)
    elif name:
        defn.name = name
    default_fps = int((DEFAULT_ANIMATIONS.get(anim) or {}).get("fps", 8))
    res = await IMP.import_frames(defn, anim, frames, fps=int(fps) or default_fps,
                                  loop=bool(loop), process=process, center=bool(center))
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.from_image", memory="on",
    http_method="POST", http_path="/spritegen/from_image", http_tags=["spritegen"],
    description="Convert ANY image (photo, drawing, AI render) into a sprite: remove the "
                "background (rembg tier → corner-colour key fallback), pixelize to the "
                "character's sprite size + palette, and register it as one animation "
                "frame. Inputs: image_b64 (str!), char_id (omit to create a new "
                "character), name (for a new character), anim (default 'idle'), "
                "bg_removal ('auto'|'rembg'|'chroma'|'none', ''=definition setting), "
                "process ('pixelize'|'keep'|'auto'), sprite_size (0=keep current), "
                "colors (-1=keep current). Output: {char_id, anim, count, sheet, gif} + "
                "record URLs.",
)
async def spritegen_from_image(image_b64: str = "", char_id: str = "", name: str = "",
                               anim: str = "idle", bg_removal: str = "",
                               process: str = "pixelize", sprite_size: int = 0,
                               colors: int = -1, trace_id=None):
    if not image_b64:
        return {"error": "image_b64 required"}
    import base64 as _b64
    import io as _io
    from PIL import Image as _Image
    anim = _safe(anim.lower()) or "idle"
    defn = (await STORE.get(char_id)) if char_id else None
    if defn is None:
        defn = CharacterDefinition(char_id=char_id or new_char_id(),
                                   name=name or "converted", animations={})
        await STORE.save(defn)
    elif name:
        defn.name = name
    if bg_removal:
        defn.bg_removal = bg_removal
    if int(sprite_size) > 0:
        defn.sprite_size = int(sprite_size)
    if int(colors) >= 0:
        defn.colors = int(colors)
    s = str(image_b64)
    if "," in s and s.lstrip().lower().startswith("data:"):
        s = s.split(",", 1)[1]
    try:
        img = _Image.open(_io.BytesIO(_b64.b64decode(s))).convert("RGBA")
    except Exception as e:
        return {"error": f"could not decode image: {e}"}
    # Background removal: keep existing alpha; otherwise rembg tier when live,
    # then the importer's corner-colour key as the always-available fallback.
    method = (bg_removal or getattr(defn, "bg_removal", "auto") or "auto").lower()
    bg_tier = "alpha"
    alpha_lo = img.split()[3].getextrema()[0]
    if method != "none" and alpha_lo >= 250:          # fully opaque → needs keying
        cut_b64, bg_tier = await P.remove_bg(defn, s)
        if bg_tier == "rembg":
            img = _Image.open(_io.BytesIO(_b64.b64decode(cut_b64))).convert("RGBA")
        else:
            img = IMP._corner_key(img)
            bg_tier = "corner-key"
    res = await IMP.import_frames(defn, anim, [img], loop=True, process=process,
                                  fps=int((DEFAULT_ANIMATIONS.get(anim) or {}).get("fps", 8)))
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    out["bg_tier"] = bg_tier
    return out


@capability(
    "spritegen.align_cells", memory="off",
    http_method="POST", http_path="/spritegen/align_cells", http_tags=["spritegen"],
    description="Re-pad every animation of a character onto ONE shared frame cell "
                "(max width/height across all animations, bottom-centre anchored) and "
                "repack the sheets — fixes the position jump when switching between "
                "animations that were generated/imported with different cell sizes. "
                "Runs automatically after generation and imports; call it manually for "
                "characters made before this fix. Input: char_id (str!). Output: "
                "{aligned:[anims], cell:[w,h]}.",
)
async def spritegen_align_cells(char_id: str = "", trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    res = await P.align_cells(defn)
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.build_sheet", memory="off",
    http_method="POST", http_path="/spritegen/build_sheet", http_tags=["spritegen"],
    description="Pack an animation's pixelized frames into a grid sprite sheet + atlas JSON "
                "+ animated GIF preview. Inputs: char_id (str!), anim (str!), columns (int, "
                "0=single row). Output: {anim, sheet, gif, meta} + asset URLs.",
)
async def spritegen_build_sheet(char_id: str = "", anim: str = "", columns: int = 0, trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if not anim:
        return {"error": "anim required"}
    res = await P.build_sheet(defn, anim, columns=int(columns))
    if res.get("error"):
        return res
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.build_package", memory="on",
    http_method="POST", http_path="/spritegen/build_package", http_tags=["spritegen"],
    description="Assemble + zip the exportable Character/ package (definition, reference, "
                "sprite sheets, atlas JSON, GIF previews, visemes, metadata.json). Input: "
                "char_id (str!). Output: {package_dir, zip, files} + a package_zip URL.",
)
async def spritegen_build_package(char_id: str = "", trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    man = await P.build_package(defn)
    out = _record_with_urls(defn)
    out["package"] = man
    return out


@capability(
    "spritegen.run_pipeline", memory="on",
    http_method="POST", http_path="/spritegen/run_pipeline", http_tags=["spritegen", "gpu"],
    description="Run the whole pipeline: (reference) → for each animation (frames → pixelize "
                "→ sheet) → package. The existing reference is REUSED unless do_base=true (or "
                "none exists). Inputs: char_id (str!), animations (csv, empty=all in the "
                "definition), do_base (bool, default false = keep current reference), "
                "do_package (bool). Emits spritegen.run.* + stage.* events. Output: "
                "{animations:{...}, package} + URLs. SLOW on CPU SD.",
)
async def spritegen_run_pipeline(char_id: str = "", animations: str = "",
                                 do_base: bool = False, do_package: bool = True, trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    anims = [a.strip() for a in animations.split(",") if a.strip()] or None
    res = await P.run_pipeline(defn, anims, do_base=bool(do_base), do_package=bool(do_package))
    out = _record_with_urls(defn)
    out.update(res)
    return out


@capability(
    "spritegen.preview_prompt", memory="off",
    http_method="POST", http_path="/spritegen/preview_prompt", http_tags=["spritegen"],
    description="Compose and return the EXACT prompt(s) the pipeline would send for a stage, "
                "without generating anything — so you can review/tune before spending GPU time. "
                "Inputs: char_id (str!), stage ('base' or an animation name), frames (int, for "
                "an animation; 0=its configured count). Output: {stage, negative_prompt, "
                "prompts:[...]} (one entry per frame for animations, one for base).",
)
async def spritegen_preview_prompt(char_id: str = "", stage: str = "base",
                                   frames: int = 0, trace_id=None):
    defn, err = await _get_defn_or_err(char_id)
    if err:
        return err
    if stage == "base":
        prompts = [_prompts.build_base_prompt(defn)]
    else:
        spec = (defn.animations or {}).get(stage, {})
        n = int(frames) or int(spec.get("frames", 4))
        poses = _prompts.anim_poses(defn, stage, n)
        prompts = [_prompts.build_frame_prompt(defn, stage, p) for p in poses]
    return {"stage": stage, "negative_prompt": defn.negative_prompt, "prompts": prompts}


# ─────────────────────────────────────────────────────────────────────────────
# ASSET SERVING — read-only, path-traversal guarded (mirrors character_asset)
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/spritegen/asset", include_in_schema=False)
async def spritegen_asset(char_id: str = "", path: str = ""):
    aid = _safe(char_id)
    if not aid or not path:
        return JSONResponse({"error": "char_id and path required"}, status_code=400)
    base = (_CHARS_DIR / aid).resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    media, _ = mimetypes.guess_type(str(target))
    return FileResponse(str(target), media_type=media or "application/octet-stream",
                        headers={"Cache-Control": "no-cache"})


@APP.get("/spritegen/panel", include_in_schema=False)
async def _spritegen_panel_route():
    p = _HERE / "spritegen_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:red'>spritegen_panel.html not found</p>")


# <vera-sprite> — reusable animated sprite player (canvas, keyboard controls,
# drag/roam). Used by the Sprite Studio playground, the companion widget below,
# and any panel that includes the script.
@APP.get("/ui/elements/sprite.js", include_in_schema=False)
async def _serve_sprite_js():
    from fastapi.responses import Response
    p = _HERE / "sprite_element.js"
    if p.exists():
        return Response(content=p.read_text(encoding="utf-8"),
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('sprite element JS not found');",
                    media_type="application/javascript")


# ─────────────────────────────────────────────────────────────────────────────
# UI — Sprite Studio is now a sub-tab of the Image Studio hub (which iframes the
# /spritegen/panel route above). Its standalone top-level tab is therefore folded
# into that hub; re-enable the register_ui below to restore it as its own tab.
# ─────────────────────────────────────────────────────────────────────────────

# register_ui(
#     panel_id="sprite-studio", label="Sprite Studio", icon="◳", mode="tab", tab_order=59,
#     html=('<div style="height:100%;display:flex;flex-direction:column;">'
#           '<iframe src="/spritegen/panel" style="flex:1;border:none;width:100%;height:100%" '
#           'allow="clipboard-read; clipboard-write"></iframe></div>'),
#     ui_caps=["spritegen.capabilities", "spritegen.define", "spritegen.generate_frame", ...])


# ─────────────────────────────────────────────────────────────────────────────
# COMPANION SPRITE — a tiny self-contained page (picker + <vera-sprite> stage
# with keyboard controls) that runs as a dashboard widget. The dashboard's
# generic per-widget pop-out (float on the page / new window) makes the sprite
# roam the whole UI without this module injecting live JS into the harness doc.
# ─────────────────────────────────────────────────────────────────────────────

_COMPANION_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:transparent;font-family:system-ui,sans-serif;font-size:11px;color:#ddd5c8;overflow:hidden}
.bar{display:flex;gap:6px;align-items:center;padding:5px 7px;background:#1f1d1acc;border-bottom:1px solid #3a3530}
select{background:#181614;border:1px solid #3a3530;color:#ddd5c8;padding:2px 5px;border-radius:3px;font-size:11px;max-width:150px}
label{display:inline-flex;gap:3px;align-items:center;color:#8a7e70;cursor:pointer;white-space:nowrap}
.stage{position:relative;height:calc(100% - 29px);overflow:hidden;
  background:conic-gradient(#22201d 25%,#1b1917 0 50%,#22201d 0 75%,#1b1917 0) 0 0/22px 22px}
.hint{position:absolute;left:8px;bottom:6px;color:#6a6058;font-size:10px;pointer-events:none}
</style></head><body>
<div class="bar">
  <select id="pick" onchange="mount(this.value)"><option value="">— sprite —</option></select>
  <label><input type="checkbox" id="kb" onchange="setKb(this.checked)"> keys</label>
  <label title="scale"><input id="sc" type="range" min="1" max="6" value="3"
    style="width:60px" oninput="if(S)S.setAttribute('scale',this.value)"></label>
</div>
<div class="stage" id="stage"><span class="hint" id="hint"></span></div>
<script src="/ui/elements/sprite.js"></script>
<script>
let S=null;
async function init(){
  try{
    const r=await fetch('/spritegen/list');const d=await r.json();
    const cs=(d&&d.characters||[]).filter(c=>Object.keys(c.sheets||{}).length);
    document.getElementById('pick').innerHTML='<option value=\\"\\">— sprite —</option>'+
      cs.map(c=>`<option value="${c.char_id}">${c.name||c.char_id}</option>`).join('');
    if(cs.length===1)  {document.getElementById('pick').value=cs[0].char_id;mount(cs[0].char_id);}
  }catch(e){}
}
function mount(id){
  const st=document.getElementById('stage');
  if(S){S.remove();S=null;}
  document.getElementById('hint').textContent='';
  if(!id)return;
  S=document.createElement('vera-sprite');
  S.setAttribute('char-id',id);S.setAttribute('scale',document.getElementById('sc').value);
  S.setAttribute('roam','');S.setAttribute('draggable','');
  if(document.getElementById('kb').checked)S.setAttribute('controls','');
  st.appendChild(S);
  document.getElementById('hint').textContent='drag me · arrows/WASD walk · shift run · space action';
}
function setKb(on){if(!S)return;if(on)S.setAttribute('controls','');else S.removeAttribute('controls');}
init();
</script></body></html>"""


@APP.get("/spritegen/companion", include_in_schema=False)
async def _spritegen_companion_route():
    return HTMLResponse(_COMPANION_HTML)


register_ui(
    panel_id="sprite-companion",
    label="Sprite Companion",
    icon="◳",
    mode="inject",   # dashboard widget (+ generic float / new-window pop-out)
    html=('<div style="height:100%;display:flex;flex-direction:column;">'
          '<iframe src="/spritegen/companion" '
          'style="flex:1;border:none;width:100%;height:100%;background:transparent">'
          '</iframe></div>'),
    ui_caps=["spritegen.list", "spritegen.get"],
)

log.info("spritegen_capabilities: ready (assets=%s)", _CHARS_DIR)
