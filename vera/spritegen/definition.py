"""definition.py — structured character definition + durable store for spritegen
================================================================================
A *character definition* is the source of truth for the whole pipeline: identity
(base prompt + layered features), target art style / sprite size, the animation
set to render, and the produced assets. It is decoupled from any agent — but may
optionally link to one (``agent_id``) so a companion can be backed by a generated
sprite sheet.

Storage mirrors ``character_capabilities.CharacterStore``:
  • Live config:  Redis hash   ``vera:spritegen:{char_id}``
  • Durable:      data-fabric dataset ``spritegen`` (id ``sprite-{char_id}``)
  • Assets:       ``vera/spritegen/_chars/{char_id}/...`` served via /spritegen/asset
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import now_iso

log = logging.getLogger("vera.spritegen")

_HERE       = Path(__file__).parent
_CHARS_DIR  = _HERE / "_chars"
_CHARS_DIR.mkdir(parents=True, exist_ok=True)

_REDIS_PREFIX   = "vera:spritegen:"
_FABRIC_DATASET = "spritegen"

# Animations the spec calls out, with sensible default frame/fps. Used when a
# brief doesn't specify its own set. `blink`/`talk` feed the companion package.
DEFAULT_ANIMATIONS: Dict[str, dict] = {
    "idle":   {"frames": 4, "fps": 6,  "loop": True,  "desc": "idle breathing"},
    # walk/run render in SIDE view: the sprite element flips the sheet for
    # left/right movement, and a profile walk actually reads as walking.
    "walk":   {"frames": 8, "fps": 10, "loop": True,  "desc": "walk cycle", "view": "side"},
    "run":    {"frames": 8, "fps": 14, "loop": True,  "desc": "run cycle",  "view": "side"},
    "attack": {"frames": 6, "fps": 14, "loop": False, "desc": "melee attack"},
    "jump":   {"frames": 5, "fps": 12, "loop": False, "desc": "jump arc"},
    "cast":   {"frames": 6, "fps": 10, "loop": False, "desc": "cast a spell"},
    "talk":   {"frames": 4, "fps": 8,  "loop": True,  "desc": "talking gesture loop"},
    "hurt":   {"frames": 3, "fps": 10, "loop": False, "desc": "take damage"},
    "death":  {"frames": 6, "fps": 8,  "loop": False, "desc": "collapse and die"},
}

# Mouth shapes for lip-sync (companion package); each is one frame.
DEFAULT_VISEMES = ["rest", "closed", "wide", "round", "teeth", "smile"]

# The framing negatives (cropped limbs, hidden hands, close-ups) protect the
# ControlNet-friendly reference: every limb must be visible for re-posing. The
# multiple-views/turnaround negatives stop SD rendering the character at two
# angles in one image (reference-sheet convention) — the pipeline needs one
# character at one angle per render. prompts.build_negative() also appends
# these unconditionally, so definitions saved with an older default stay covered.
_DEFAULT_NEGATIVE = ("blurry, low quality, deformed, extra limbs, extra fingers, watermark, "
                     "text, multiple characters, multiple views, character sheet, "
                     "reference sheet, turnaround, split view, cropped, out of frame, "
                     "close-up, arms crossed, hands in pockets, hands behind back, "
                     "occluded limbs, busy background")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", s or "")


def char_dir(char_id: str) -> Path:
    d = _CHARS_DIR / _safe(char_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# RECORD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CharacterDefinition:
    char_id:         str = ""
    name:            str = ""
    brief:           str = ""             # freeform user description

    # Identity (fixed across every frame)
    base_prompt:     str = ""
    negative_prompt: str = _DEFAULT_NEGATIVE
    palette:         str = ""
    layers:          Dict[str, str] = field(default_factory=dict)  # hair/eyes/clothing/weapon/...

    # Style / target
    style:           str = "pixel"        # see prompts.STYLE_DESCRIPTOR
    style_prompt:    str = ""             # editable style portion of the SD prompt
                                          # ("" = the preset for `style`). SD renders
                                          # clean flat character art; the PIPELINE
                                          # makes it pixel art — don't ask SD for pixels.
    framing:         str = ""             # override the short full-body framing suffix
    base_pose:       str = ""             # reference stance ("" = neutral A-pose default)
    sprite_size:     int = 64             # final px (16…256; 96+ = hi-res "modern retro")
    colors:          int = 32             # palette-reduction target (0 = no reduction)
    outline:         bool = False         # add a dark 1px outline on pixelize
    dither:          bool = False
    downscale:       str = "kcentroid"    # kcentroid (crisp pixel-art) | lanczos (soft)
    shared_palette:  bool = True          # one palette per animation (no shimmer)

    # Animation set: name -> {frames, fps, loop, desc}
    animations:      Dict[str, dict] = field(default_factory=lambda: dict(DEFAULT_ANIMATIONS))
    visemes:         List[str] = field(default_factory=lambda: list(DEFAULT_VISEMES))

    # ── Controls & buddy bindings ────────────────────────────────────────────
    # Map ANY imported animation to keyboard keys, movement and activity slots so
    # packs aren't limited to the prescribed names ("the limit on animations is the
    # limit on button combos"). Empty maps → the <vera-sprite> element falls back to
    # built-in defaults, filtered to whatever animations the pack actually has.
    keymap:   Dict[str, str] = field(default_factory=dict)   # key   -> anim (one-shot action)
    dirmap:   Dict[str, str] = field(default_factory=dict)   # key   -> direction (up|down|left|right)
    events:   Dict[str, str] = field(default_factory=dict)   # slot  -> anim; slots: idle, move, run,
                                                             # talk, think, work, happy, error, sleep,
                                                             # greet, emote (looped or once per the anim)
    autonomy: Dict[str, Any] = field(default_factory=dict)   # tamagotchi behaviour: {enabled, wander,
                                                             # emote, idle_after, min_ms, max_ms}

    # Generation config (hi-res, before pixelization)
    seed:            int = -1
    seed_lock:       bool = False         # pin `seed` across regenerations. Off =
                                          # reroll a fresh seed on every base gen so
                                          # "regenerate base" explores new compositions.
                                          # Frames within one animation still share the
                                          # seed (anti-flicker) — only base gen rerolls.
    steps:           int = 28
    guidance:        float = 7.5
    scheduler:       str = "dpmpp"        # per-job sampler on the GPU node (DPM++ 2M
                                          # Karras — the strong pixel-art default);
                                          # "" = the model's default sampler
    gen_width:       int = 768
    gen_height:      int = 768
    transparent:     bool = True          # generate on a flat key colour → alpha
    bg_color:        str = ""             # chroma key; "" = default green
    loras:           str = ""

    # ── Pipeline stage controls (what's enabled / which tier per stage) ──────
    provider:        str = "auto"         # auto | local | pixellab — external
                                          # purpose-built generator (PixelLab) is
                                          # used by auto whenever PIXELLAB_API_KEY
                                          # is set; errors fall back to local.
    identity_tier:   str = "auto"         # auto | ipadapter | img2img | txt2img
    pose_source:     str = "auto"         # auto | skeleton | reference | none — ControlNet input
    auto_center:     str = "centroid"     # centroid | bbox | none — silhouette re-anchoring
    bg_removal:      str = "auto"         # auto | rembg | chroma | none
    chroma_tol:      int = 80             # chroma-key aggressiveness (higher = more)
    rembg_model:     str = ""             # u2net | u2netp | isnet-general-use | …
    rembg_alpha_matting: bool = False     # refine soft/hair edges (slower)
    do_pixelize:     bool = True          # downscale+palette to sprite_size (off = keep hi-res)
    do_upscale:      bool = False         # ESRGAN-upscale the reference before posing

    # Optional companion link
    agent_id:        str = ""

    # Produced assets (filenames relative to _chars/{char_id}/)
    reference:       str = ""                                   # reference.png (bg-removed)
    raw_files:       Dict[str, List[str]] = field(default_factory=dict)   # anim -> hi-res frames
    frame_files:     Dict[str, List[str]] = field(default_factory=dict)   # anim -> pixelized frames
    sheets:          Dict[str, dict] = field(default_factory=dict)        # anim -> sheet meta
    package_dir:     str = ""

    # Last-known tier per stage (informational for the panel)
    tiers_used:      Dict[str, str] = field(default_factory=dict)

    created_at:      str = field(default_factory=now_iso)
    updated_at:      str = field(default_factory=now_iso)
    archived:        bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "CharacterDefinition":
        return CharacterDefinition(**{k: v for k, v in (d or {}).items()
                                      if k in CharacterDefinition.__dataclass_fields__})


def new_char_id() -> str:
    return uuid.uuid4().hex[:12]


# ─────────────────────────────────────────────────────────────────────────────
# STORE — Redis (live) + data-fabric (durable), mirrors CharacterStore
# ─────────────────────────────────────────────────────────────────────────────

class DefinitionStore:
    _CACHE: Dict[str, CharacterDefinition] = {}
    _warmed = False

    @staticmethod
    def _redis():
        return _orch.REDIS

    async def save(self, rec: CharacterDefinition) -> CharacterDefinition:
        rec.updated_at = now_iso()
        self._CACHE[rec.char_id] = rec
        r = self._redis()
        if r:
            try:
                await r.set(f"{_REDIS_PREFIX}{rec.char_id}", json.dumps(rec.to_dict()))
            except Exception as e:
                log.warning("spritegen save redis: %s", e)
        asyncio.ensure_future(self._save_to_fabric(rec))
        return rec

    async def get(self, char_id: str) -> Optional[CharacterDefinition]:
        if char_id in self._CACHE:
            return self._CACHE[char_id]
        r = self._redis()
        if r:
            try:
                raw = await r.get(f"{_REDIS_PREFIX}{char_id}")
                if raw:
                    d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    rec = CharacterDefinition.from_dict(d)
                    self._CACHE[char_id] = rec
                    return rec
            except Exception as e:
                log.debug("spritegen get redis: %s", e)
        await self._warm_from_fabric()
        return self._CACHE.get(char_id)

    async def list(self) -> List[CharacterDefinition]:
        out: Dict[str, CharacterDefinition] = {}
        r = self._redis()
        if r:
            try:
                keys = await r.keys(f"{_REDIS_PREFIX}*")
                for k in keys:
                    raw = await r.get(k)
                    if not raw:
                        continue
                    d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    rec = CharacterDefinition.from_dict(d)
                    if not rec.archived:
                        out[rec.char_id] = rec
            except Exception as e:
                log.warning("spritegen list redis: %s", e)
        if not out:
            await self._warm_from_fabric()
            for rec in self._CACHE.values():
                if not rec.archived:
                    out[rec.char_id] = rec
        return sorted(out.values(), key=lambda x: x.name or x.char_id)

    async def delete(self, char_id: str) -> bool:
        rec = await self.get(char_id)
        if not rec:
            return False
        rec.archived = True
        await self.save(rec)
        self._CACHE.pop(char_id, None)
        r = self._redis()
        if r:
            try:
                await r.delete(f"{_REDIS_PREFIX}{char_id}")
            except Exception:
                pass
        # Remove asset files (best-effort)
        try:
            import shutil
            d = _CHARS_DIR / _safe(char_id)
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            log.debug("spritegen delete files: %s", e)
        return True

    # ── Fabric durability (mirrors character_capabilities pattern) ───────────
    async def _save_to_fabric(self, rec: CharacterDefinition):
        try:
            fabric = sys.modules.get("data_fabric")
            if not fabric:
                return
            row = {
                "id":         f"sprite-{rec.char_id}",
                "dataset_id": _FABRIC_DATASET,
                "text":       " ".join(filter(None, [rec.name, rec.brief, rec.base_prompt])),
                "data":       rec.to_dict(),
                "source_id":  rec.char_id,
                "tags":       ["spritegen", rec.char_id] + ([rec.agent_id] if rec.agent_id else []),
                "created_at": rec.updated_at or now_iso(),
            }
            ins = getattr(fabric, "_sqlite_insert_record", None)
            if ins:
                await ins(row)
        except Exception as e:
            log.debug("spritegen fabric save: %s", e)

    async def _warm_from_fabric(self):
        if self._warmed:
            return
        self._warmed = True
        try:
            fabric = sys.modules.get("data_fabric")
            if not fabric:
                return
            results = await fabric.query_dataset(
                dataset_id=_FABRIC_DATASET,
                query={"limit": 2000, "include_data": True},
            )
            for rrow in (results or []):
                data = rrow.get("data") or {}
                if isinstance(data, str):
                    try: data = json.loads(data)
                    except Exception: continue
                if not isinstance(data, dict) or not data.get("char_id"):
                    continue
                rec = CharacterDefinition.from_dict(data)
                ex = self._CACHE.get(rec.char_id)
                if ex is None or (rec.updated_at or "") >= (ex.updated_at or ""):
                    self._CACHE[rec.char_id] = rec
        except Exception as e:
            log.debug("spritegen warm fabric: %s", e)


STORE = DefinitionStore()
