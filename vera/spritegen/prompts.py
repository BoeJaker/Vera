"""prompts.py — prompt + pose construction for the spritegen pipeline
=====================================================================
Pure functions (stdlib only). Turns a CharacterDefinition + (animation, frame)
into the text prompt and the textual pose hint. The pose hints double as:
  • the img2img / txt2img pose description in the fallback path, and
  • the OpenPose hint when the ControlNet tier is live.

`STYLE_DESCRIPTOR` covers the spec's style list; `POSE_LIBRARY` holds per-frame
phase descriptions for each animation, fitted to any requested frame count.
"""

from __future__ import annotations

import os
from typing import Dict, List

# Art-style → SD descriptor.
#
# IMPORTANT: SD does NOT generate pixel art here — pixelize.py does that
# afterwards. SD's only job is a clean hi-res character render that SURVIVES
# k-centroid downscaling: flat cel shading, bold shapes, clean lineart. Asking
# SD for "pixel art / limited palette" degrades the render and, on chroma-key
# runs, drags the key colour into the character's palette (the green-smear
# bug). So the pixel styles below describe flat 2D character art at different
# detail levels — never pixels. The whole style portion is also editable per
# character via defn.style_prompt (see style_text).
STYLE_DESCRIPTOR: Dict[str, str] = {
    "pixel":   ("clean 2d game character art, cel shaded, flat colors, bold simple "
                "shapes, thick clean outlines, even lighting"),
    "retro16": ("very simple cartoon game character, flat colors, minimal detail, "
                "chunky bold shapes, thick outlines"),
    "snes":    ("clean 2d game character art, cel shaded, flat colors, bold shapes, "
                "soft harmonious palette, clean outlines"),
    "jrpg":    ("detailed 2d JRPG character concept art, cel shaded, flat colors, "
                "vibrant palette, clean lineart"),
    "hdpixel": ("detailed 2d game character concept art, cel shaded, flat colors, "
                "rich palette, clean lineart, subtle flat shading"),
    "hd2d":    ("HD-2D hand-painted game character, octopath-style, painterly, "
                "rich rim lighting, crisp silhouette"),
    "anime":   "anime style, clean line art, cel shaded, visual novel character art",
    "chibi":   "chibi style, big head small body, cute cartoon, bold outlines",
    "painted": "polished digital painting, soft shading, character art",
    "cartoon": "friendly cartoon mascot, bold outlines, flat colors",
    "comic":   "comic book style, inked outlines, halftone shading",
    "cel":     "cel shaded, flat color regions, clean outlines",
    "3d":      "stylized 3d render, soft studio lighting",
}

# Styles whose output is destined for the pixelizer (used for negatives only).
PIXEL_STYLES = {"pixel", "retro16", "snes", "jrpg", "hdpixel"}

# Per-style negative additions. For pixelizer-bound styles the enemy is what
# downscales badly: photorealism, airbrushed gradients, blur, clutter.
_FLAT_NEGATIVE = ("photo, photorealistic, photograph, 3d render, realistic skin texture, "
                  "airbrushed shading, heavy gradients, blurry, soft focus, depth of "
                  "field, motion blur, film grain, cluttered detail, complex background")
STYLE_NEGATIVE: Dict[str, str] = {
    "pixel":   _FLAT_NEGATIVE,
    "retro16": _FLAT_NEGATIVE + ", fine detail, intricate",
    "snes":    _FLAT_NEGATIVE,
    "jrpg":    _FLAT_NEGATIVE,
    "hdpixel": _FLAT_NEGATIVE,
    "hd2d":    "photo, photorealistic, 3d render, blurry, soft focus",
    "anime":   "photo, photorealistic, 3d render, blurry",
    "chibi":   "photo, photorealistic, realistic proportions, blurry",
    "cartoon": "photo, photorealistic, blurry",
    "comic":   "photo, photorealistic, blurry",
    "cel":     "photo, photorealistic, blurry",
}


# Appended to EVERY generation. SD's training data is full of character
# reference/turnaround sheets (front + side views of the same character in one
# image), and "sprite"/"game character" vocabulary pulls that convention in.
# The whole pipeline (rembg → anchor → pixelize → sheet packing) assumes ONE
# character at ONE angle per image, so multi-view output is always wrong.
# Lives here rather than only in the definition default so characters saved
# before this guard existed are covered too.
_ANTI_TURNAROUND = ("multiple views, character sheet, reference sheet, turnaround, "
                    "model sheet, two angles, split view, side by side, "
                    "multiple characters, duplicate character")


def build_negative(defn) -> str:
    """Full negative prompt for a generation: the definition's own negatives plus
    the style's anti-realism additions plus the universal anti-turnaround guard
    (deduplicated, order preserved)."""
    base = (getattr(defn, "negative_prompt", "") or "").strip().strip(",")
    extra = STYLE_NEGATIVE.get(getattr(defn, "style", "") or "", "")
    seen, parts = set(), []
    for token in (t.strip() for t in f"{base}, {extra}, {_ANTI_TURNAROUND}".split(",")):
        if token and token.lower() not in seen:
            seen.add(token.lower())
            parts.append(token)
    return ", ".join(parts)

# Framing suffixes are kept SHORT on purpose: prompts are ordered identity → pose
# → style → framing so the words that matter most always lead. (The GPU node lifts
# CLIP's 77-token window via compel when installed, so length is no longer a hard
# limit — but concise framing still steers better.) The definition's optional
# `framing` field overrides _FRAMING_FULL. Deliberately NO "sprite" here — that
# word biases SD toward sprite/turnaround sheets; "solo, one character" plus the
# _ANTI_TURNAROUND negatives keep each render to a single character, single angle.
_FRAMING_FULL     = ("solo, one character only, full body, front view, whole body "
                     "visible head to toe with margin, plain background")
_FRAMING_SIDE     = ("solo, one character only, full body, side view facing right, "
                     "whole body visible head to toe, plain background")
_FRAMING_PORTRAIT = "solo, head and shoulders, front view, plain background"

# Animations that render in profile by default (the sheet is flipped for the
# other direction, so we always generate facing RIGHT). A definition's animation
# spec can override with "view": "front" | "side".
SIDE_VIEW_ANIMS = {"walk", "run"}

# Reference stance when the definition doesn't set base_pose. An A-pose with
# all four limbs separated and fully in frame is what ControlNet/OpenPose and
# img2img need to re-pose reliably: arms hanging against the torso merge into
# the silhouette and the skeleton can't articulate what it can't see. It also
# gives rembg clean limb boundaries and centres the character for the anchor.
DEFAULT_BASE_POSE = ("A-pose, standing straight facing the camera, arms held straight "
                     "out from the sides at 45 degrees away from the body, hands open, "
                     "legs apart, all four limbs fully visible and separated, "
                     "whole body in frame head to toe, neutral expression")

# Optional safety word-cap on a composed prompt. Default 0 = NO CAP: the GPU node
# lifts the old token limit when `compel` is installed (long-prompt weighting), and
# otherwise the encoder truncates with a harmless warning. We still order the
# important parts first (identity → pose → style → framing) so that a build
# without compel drops framing rather than identity. Override with
# SPRITEGEN_MAX_PROMPT_WORDS (e.g. 60) to force a cap.
MAX_PROMPT_WORDS = int(os.getenv("SPRITEGEN_MAX_PROMPT_WORDS", "0") or "0")


def _compose(parts) -> str:
    s = ", ".join(p for p in (str(x).strip() for x in parts if x) if p)
    if MAX_PROMPT_WORDS and MAX_PROMPT_WORDS > 0:
        words = s.split()
        if len(words) > MAX_PROMPT_WORDS:
            s = " ".join(words[:MAX_PROMPT_WORDS])
    return s

# Per-animation, per-phase pose phrases. The fitter samples/repeats to match the
# requested frame count, so adding frames just interpolates the same cycle.
POSE_LIBRARY: Dict[str, List[str]] = {
    "idle": [
        "standing still, relaxed neutral stance, arms at sides",
        "standing, chest slightly raised mid-breath, weight settled",
        "standing still, subtle sway, calm",
        "standing, chest lowered exhaling, relaxed",
    ],
    "walk": [
        "walking in profile, front leg extended heel down, back arm swung forward",
        "walking in profile, legs passing under the body, arms at sides",
        "walking in profile, other leg extended heel down, other arm forward",
        "walking in profile, legs passing under the body, body lifted",
        "walking in profile, front leg reaching, back leg pushing off",
        "walking in profile, weight settling on the front foot",
        "walking in profile, opposite leg reaching, pushing off",
        "walking in profile, weight settling, arms mid-swing",
    ],
    "run": [
        "sprinting in profile, front leg fully extended, leaning forward",
        "sprinting in profile, airborne, both feet off the ground, knees bent",
        "sprinting in profile, opposite leg extended, arms pumping hard",
        "sprinting in profile, airborne recoil, leaning forward",
        "sprinting in profile, back leg driving off the ground",
        "sprinting in profile, mid-air stride, arms driving",
        "sprinting in profile, opposite leg driving off",
        "sprinting in profile, mid-air stride, tucked knees",
    ],
    "attack": [
        "anticipation, winding up the weapon back, coiled",
        "stepping in, raising the weapon high",
        "swinging the weapon forward, mid-strike, motion lines",
        "full extension strike, weapon at target, impact",
        "follow-through, weapon swung past, off balance",
        "recovering back to ready stance",
    ],
    "cast": [
        "raising both hands, beginning to gather energy",
        "hands glowing, magic energy building, focused",
        "arms wide, spell at peak charge, bright glow",
        "thrusting hands forward, releasing the spell, burst",
        "arms lowering, spell released, residual glow",
        "settling back to neutral stance",
    ],
    "hurt": [
        "flinching, head snapping back, recoil from a hit",
        "doubled over, clutching the wound, pained",
        "staggering back, regaining footing",
    ],
    "death": [
        "struck hard, body jolting, losing balance",
        "staggering, knees buckling",
        "falling backward, arms flailing",
        "collapsing to the ground, on knees",
        "crumpling down onto the ground",
        "lying defeated on the ground, motionless",
    ],
    "jump": [
        "crouching low, coiled to leap",
        "launching upward, legs extending, arms up",
        "at the apex, body tucked",
        "descending, legs reaching for the ground",
        "landing, knees bent absorbing impact",
    ],
    "blink": [
        "eyes fully open, neutral face",
        "eyes half closed",
        "eyes fully closed",
        "eyes half open",
    ],
    "talk": [
        "standing, mouth open speaking, one hand raised gesturing",
        "standing, mouth half open, hand mid-gesture",
        "standing, mouth open wide, both hands gesturing",
        "standing, mouth nearly closed, hands settling",
    ],
}

# Mouth shapes for lip-sync (used by the companion package). Each maps to a face
# instruction; the rest of the face stays neutral.
VISEME_DESC: Dict[str, str] = {
    "rest":   "mouth closed and relaxed, neutral resting face",
    "closed": "lips pressed together, mouth closed (M/B/P sound)",
    "wide":   "mouth open wide, jaw dropped (A/E sound)",
    "round":  "lips rounded into an O shape (O/U sound)",
    "teeth":  "upper teeth touching lower lip (F/V sound)",
    "smile":  "lips spread in a wide smile showing teeth (EE sound)",
}


def fit_poses(anim: str, n: int) -> List[str]:
    """Return exactly `n` pose phrases for `anim`, sampling/repeating the cycle.

    Unknown animations fall back to the animation name itself as the phrase so
    the pipeline never breaks on a custom animation the user typed."""
    base = POSE_LIBRARY.get(anim)
    if not base:
        return [f"{anim} pose, frame {i + 1}" for i in range(max(1, n))]
    if n <= 0:
        n = len(base)
    if n == len(base):
        return list(base)
    # Even resampling across the cycle (nearest-phase), keeps loops smooth.
    return [base[round(i * (len(base) - 1) / max(1, n - 1))] if n > 1 else base[0]
            for i in range(n)]


def style_descriptor(style: str) -> str:
    return STYLE_DESCRIPTOR.get(style, style or "")


def style_text(defn) -> str:
    """The style portion of the prompt — the definition's own editable
    style_prompt when set, else the preset for its named style. This is THE
    knob for tuning what SD renders (the pipeline pixelizes afterwards)."""
    s = (getattr(defn, "style_prompt", "") or "").strip()
    return s if s else style_descriptor(getattr(defn, "style", ""))


def framing_full(defn) -> str:
    return (getattr(defn, "framing", "") or "").strip() or _FRAMING_FULL


def anim_view(defn, anim: str) -> str:
    """'side' or 'front' for one animation (spec override → built-in default)."""
    spec = (getattr(defn, "animations", {}) or {}).get(anim) or {}
    v = str(spec.get("view", "") or "").lower()
    if v in ("side", "front"):
        return v
    return "side" if anim in SIDE_VIEW_ANIMS else "front"


def framing_for(defn, anim: str, portrait: bool = False) -> str:
    """Framing suffix for one animation frame. An explicit definition framing
    always wins; otherwise side-view animations get the profile framing."""
    if portrait:
        return _FRAMING_PORTRAIT
    explicit = (getattr(defn, "framing", "") or "").strip()
    if explicit:
        return explicit
    return _FRAMING_SIDE if anim_view(defn, anim) == "side" else _FRAMING_FULL


def anim_poses(defn, anim: str, n: int) -> List[str]:
    """Per-frame pose phrases for `anim`, honouring a definition override before
    falling back to POSE_LIBRARY. An animation spec may carry:
      • "poses": a list (or newline string) of per-frame phrases, or
      • "prompt"/"desc": one phrase used for every frame.
    Lists are resampled to `n` frames so the user controls the motion."""
    spec = (getattr(defn, "animations", {}) or {}).get(anim) or {}
    custom = spec.get("poses")
    if isinstance(custom, str):
        custom = [ln.strip() for ln in custom.splitlines() if ln.strip()]
    if isinstance(custom, list) and custom:
        if n <= 0:
            n = len(custom)
        if n == len(custom):
            return list(custom)
        return [custom[round(i * (len(custom) - 1) / max(1, n - 1))] if n > 1 else custom[0]
                for i in range(n)]
    single = (spec.get("prompt") or "").strip()
    if single:
        return [single for _ in range(max(1, n))]
    return fit_poses(anim, n)


def build_base_prompt(defn) -> str:
    """Identity prompt for the reference image. The reference DOES get an explicit
    stance (base_pose): an uncontrolled reference pose is what made every derived
    frame inherit weird arm/leg placement. Kept short (identity → layers → pose →
    style → palette → framing) for CLIP."""
    parts = [defn.base_prompt.strip()]
    if defn.layers:
        parts += [str(v).strip() for v in defn.layers.values() if str(v).strip()]
    parts.append((getattr(defn, "base_pose", "") or "").strip() or DEFAULT_BASE_POSE)
    parts.append(style_text(defn))
    if defn.palette:
        parts.append(f"{defn.palette} palette")
    parts.append(framing_full(defn))
    return _compose(parts)


def build_frame_prompt(defn, anim: str, pose_text: str, *, portrait: bool = False) -> str:
    """Prompt for one animation frame. The identity mostly comes from the reference
    image (img2img / IP-Adapter), so we lead with identity + pose and keep the rest
    short; per-character layers/palette are intentionally omitted here to leave CLIP
    token budget for the POSE, which is what must change frame to frame."""
    return _compose([defn.base_prompt.strip(), pose_text,
                     style_text(defn),
                     framing_for(defn, anim, portrait=portrait)])


def build_viseme_prompt(defn, viseme: str) -> str:
    """Face-edit prompt for a viseme/mouth shape (identity + mouth instruction)."""
    desc = VISEME_DESC.get(viseme, f"{viseme} mouth shape")
    return ", ".join(p for p in [
        defn.base_prompt.strip(), desc,
        "looking at viewer, clear face", style_text(defn)] if p)


# ── LLM brief → structured definition ────────────────────────────────────────

def describe_system_prompt() -> str:
    return ("You are an art director designing a single recurring game character. "
            "Respond ONLY with compact JSON, no prose.")


def describe_user_prompt(brief: str, style: str, anims: List[str], notes: str = "") -> str:
    anims_csv = ", ".join(anims)
    extra = f"\nExtra art direction (obey this): {notes.strip()}\n" if notes.strip() else ""
    return (
        f"Character brief: {brief}\n"
        f"Target visual style: {style}.{extra}\n"
        "Produce JSON with keys:\n"
        '  "name": a short character name;\n'
        '  "base_prompt": a comma-separated list (roughly 12-30 words) of ONLY the most '
        "distinctive FIXED identity tokens (species, build, hair, key colours, signature "
        "item, clothing) — NO pose, expression, palette or background words. Lead with "
        "the most important tokens;\n"
        '  "layers": an object with any of {hair, eyes, clothing, weapon, accessories} '
        "→ short phrase, for modular swapping;\n"
        '  "palette": short colour-palette phrase;\n'
        '  "negative_prompt": things to avoid;\n'
        f'  "animation_notes": object mapping each of [{anims_csv}] to a 5-12 word note '
        "on how THIS character performs that action."
    )
