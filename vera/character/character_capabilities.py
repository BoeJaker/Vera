"""
character_capabilities.py — Animated character / companion for Vera agents
==========================================================================
A "character" is an embodiment of an existing agent: a set of generated
expression frames (idle / talking / thinking / happy / …) plus animation and
voice config, so an agent can appear as an animated companion instead of just
an emoji `avatar`. The same `<vera-character>` web component renders it in the
web UI and (via a thin pywebview host) on the desktop.

Design — layered tiers with graceful fallback (user-configurable)
─────────────────────────────────────────────────────────────────
  SD generation tier:  img2img (consistent frames)  →  txt2img (seed-locked)
  Animation tier:      talkinghead → spritesheet → procedural → emoji
Each tier is probed (`character.capabilities` → `image.sd_capabilities`) and
the best available is used; the renderer degrades cleanly when assets/models
are missing.

Storage
───────
  • Live config:   Redis hash  vera:characters:{agent_id}
  • Durable:       data-fabric dataset "characters" (id char-{agent_id})
  • Frame images:  vera/character/_chars/{agent_id}/{state}.png
                   served read-only via GET /character/asset

A character is keyed by `agent_id`, so the agent registry is never modified
— any agent can gain (or lose) a character without a schema change.

Capabilities
────────────
  character.capabilities   — which generation / animation tiers are live
  character.describe       — LLM: freeform description → structured SD prompts
  character.generate       — generate expression frames for an agent
  character.get            — get a character by agent_id
  character.set            — update character config (no regeneration)
  character.list           — list all characters
  character.delete         — remove a character (frames + record)
  character.narrate        — LLM: recent events → one in-persona status line

UI
──
  register_ui("character-studio", mode="tab")   — Studio + Companion panel
  register_ui("vera-character",  mode="inject") — the <vera-character> element
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import FileResponse, JSONResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso,
    ollama_generate, register_ui,
)

log = logging.getLogger("vera.character")

_HERE       = Path(__file__).parent
_CHARS_DIR  = _HERE / "_chars"
_CHARS_DIR.mkdir(parents=True, exist_ok=True)

_REDIS_PREFIX   = "vera:characters:"
_FABRIC_DATASET = "characters"

# ── State / expression vocabulary ────────────────────────────────────────────
# Each state maps to a default prompt suffix. `talking` (mouth open) + `neutral`
# (mouth closed) are the minimum needed for procedural lip-flap during speech.
DEFAULT_STATES = ["neutral", "talking", "thinking", "happy"]
ALL_STATES     = ["neutral", "talking", "thinking", "happy",
                  "working", "error", "listening"]

# Verbose, emotionally DISTINCT per-state descriptions. Vague/short suffixes all
# came out looking the same; these push exaggerated face + body language so each
# state reads clearly (the user found "emotional" especially effective).
DEFAULT_EXPRESSION_SUFFIX = {
    "neutral":   "calm resting expression, relaxed neutral face, mouth closed, "
                 "soft steady forward gaze, composed and still",
    "talking":   "mouth wide open mid-sentence, actively talking and gesturing, "
                 "eyebrows raised, animated lively chatter, engaged and expressive",
    "thinking":  "deep in thought, eyes glancing upward, one hand to the chin, "
                 "furrowed brow, pensive curious pondering look",
    "happy":     "huge joyful open grin, beaming radiant smile, eyes squeezed bright "
                 "and crinkled, cheeks raised, overflowing with delight and excitement",
    "working":   "intense focused concentration, narrowed determined eyes, jaw set, "
                 "leaning forward hard at work, absorbed and busy",
    "error":     "alarmed and worried, wide anxious eyes, mouth open in dismay, "
                 "frowning, flustered panicked expression, hands up in concern",
    "listening": "attentive and curious, head tilted to one side, eyebrows raised "
                 "with interest, warm open inviting gaze, leaning in to listen",
}

STYLE_DESCRIPTOR = {
    "pixel":   "pixel art, 16-bit retro game sprite, crisp clean pixels",
    "painted": "polished digital painting, soft shading, character portrait",
    "anime":   "anime style, clean line art, cel shaded",
    "cartoon": "friendly cartoon mascot, bold outlines, flat colors",
    "3d":      "stylized 3d render, soft studio lighting",
}

# Framing suffix appended to every prompt so frames stay consistent and overlay
# cleanly in the rounded character card.
_FRAMING = ("centered head-and-shoulders character portrait, "
            "single character, plain flat solid background, consistent design")

_DEFAULT_NEGATIVE = ("blurry, low quality, distorted, deformed, extra limbs, "
                     "watermark, text, multiple characters, cropped")


# ─────────────────────────────────────────────────────────────────────────────
# RECORD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CharacterRecord:
    agent_id:        str = ""
    display_name:    str = ""
    description:     str = ""

    # Animation tier the user prefers; renderer falls back if assets are missing.
    render_mode:     str = "procedural"   # procedural | spritesheet | talkinghead | auto
    style:           str = "pixel"        # pixel | painted | anime | cartoon | 3d
    palette:         str = ""
    transparent:     bool = False         # chroma-key the background out to alpha
    bg_color:        str = ""             # chroma-key colour (hex); "" = default green

    # Generation config
    base_prompt:     str = ""
    negative_prompt: str = _DEFAULT_NEGATIVE
    expression_prompts: Dict[str, str] = field(default_factory=dict)
    seed:            int = -1
    steps:           int = 22
    guidance:        float = 7.5
    width:           int = 512
    height:          int = 512
    loras:           str = ""
    sd_tier_used:    str = ""             # img2img | txt2img | ""

    # Produced assets
    states:          List[str] = field(default_factory=list)   # which frames exist
    frames:          Dict[str, str] = field(default_factory=dict)  # state -> filename
    sheet:           Dict[str, Any] = field(default_factory=dict)  # spritesheet meta

    # Voice / animation behaviour
    voice:           str = ""             # defaults to the agent's voice
    tts_speed:       float = 1.0
    idle_bob:        bool = True
    idle_blink:      bool = True

    created_at:      str = field(default_factory=now_iso)
    updated_at:      str = field(default_factory=now_iso)
    archived:        bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "CharacterRecord":
        return CharacterRecord(**{k: v for k, v in (d or {}).items()
                                  if k in CharacterRecord.__dataclass_fields__})


# ─────────────────────────────────────────────────────────────────────────────
# STORE — Redis (live) + data-fabric (durable)
# ─────────────────────────────────────────────────────────────────────────────

class CharacterStore:
    _CACHE: Dict[str, CharacterRecord] = {}
    _warmed = False

    @staticmethod
    def _redis():
        return _orch.REDIS

    async def save(self, rec: CharacterRecord) -> CharacterRecord:
        rec.updated_at = now_iso()
        self._CACHE[rec.agent_id] = rec
        r = self._redis()
        if r:
            try:
                await r.set(f"{_REDIS_PREFIX}{rec.agent_id}",
                            json.dumps(rec.to_dict()))
            except Exception as e:
                log.warning("character save redis: %s", e)
        asyncio.ensure_future(self._save_to_fabric(rec))
        return rec

    async def get(self, agent_id: str) -> Optional[CharacterRecord]:
        if agent_id in self._CACHE:
            return self._CACHE[agent_id]
        r = self._redis()
        if r:
            try:
                raw = await r.get(f"{_REDIS_PREFIX}{agent_id}")
                if raw:
                    d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    rec = CharacterRecord.from_dict(d)
                    self._CACHE[agent_id] = rec
                    return rec
            except Exception as e:
                log.debug("character get redis: %s", e)
        await self._warm_from_fabric()
        return self._CACHE.get(agent_id)

    async def list(self) -> List[CharacterRecord]:
        out: Dict[str, CharacterRecord] = {}
        r = self._redis()
        if r:
            try:
                keys = await r.keys(f"{_REDIS_PREFIX}*")
                for k in keys:
                    raw = await r.get(k)
                    if not raw:
                        continue
                    d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                    rec = CharacterRecord.from_dict(d)
                    if not rec.archived:
                        out[rec.agent_id] = rec
            except Exception as e:
                log.warning("character list redis: %s", e)
        if not out:
            await self._warm_from_fabric()
            for rec in self._CACHE.values():
                if not rec.archived:
                    out[rec.agent_id] = rec
        return sorted(out.values(), key=lambda x: x.display_name or x.agent_id)

    async def delete(self, agent_id: str) -> bool:
        rec = await self.get(agent_id)
        if not rec:
            return False
        rec.archived = True
        await self.save(rec)
        self._CACHE.pop(agent_id, None)
        r = self._redis()
        if r:
            try:
                await r.delete(f"{_REDIS_PREFIX}{agent_id}")
            except Exception:
                pass
        # Remove frame files (best-effort)
        try:
            d = _CHARS_DIR / _safe(agent_id)
            if d.is_dir():
                for p in d.glob("*.png"):
                    p.unlink(missing_ok=True)
        except Exception as e:
            log.debug("character delete files: %s", e)
        return True

    # ── Fabric durability (mirrors the agents.py pattern, trimmed) ───────────
    async def _save_to_fabric(self, rec: CharacterRecord):
        try:
            fabric = sys.modules.get("data_fabric")
            if not fabric:
                return
            payload = rec.to_dict()
            row = {
                "id":         f"char-{rec.agent_id}",
                "dataset_id": _FABRIC_DATASET,
                "text":       " ".join(filter(None, [rec.display_name, rec.description])),
                "data":       payload,
                "source_id":  rec.agent_id,
                "tags":       ["character", rec.agent_id],
                "created_at": rec.updated_at or now_iso(),
            }
            ins = getattr(fabric, "_sqlite_insert_record", None)
            if ins:
                await ins(row)
        except Exception as e:
            log.debug("character fabric save: %s", e)

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
                if not isinstance(data, dict) or not data.get("agent_id"):
                    continue
                rec = CharacterRecord.from_dict(data)
                # newest wins
                ex = self._CACHE.get(rec.agent_id)
                if ex is None or (rec.updated_at or "") >= (ex.updated_at or ""):
                    self._CACHE[rec.agent_id] = rec
        except Exception as e:
            log.debug("character warm fabric: %s", e)


STORE = CharacterStore()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", s or "")


async def _call_cap(cap_name: str, **kwargs) -> dict:
    # NB: first arg is `cap_name`, not `name` — callers pass a `name=` kwarg
    # through to caps like agent.get, which would collide with a `name` param.
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"capability {cap_name} not available"}
    try:
        res = await cap["func"](**kwargs)
        return res if isinstance(res, dict) else {"result": res}
    except Exception as e:
        log.warning("character: call %s failed: %s", cap_name, e)
        return {"error": str(e)}


async def _get_agent(agent_id: str = "", name: str = "") -> dict:
    res = await _call_cap("agent.get", id=agent_id, name=name)
    if res and not res.get("error"):
        return res
    return {}


_CHROMA_DEFAULT = "#1bd12a"   # bright green key colour for transparent sprites


def _color_word(bg_color: str) -> str:
    h = (bg_color or _CHROMA_DEFAULT).strip().lower()
    return {
        "#1bd12a": "bright chroma green", "#00ff00": "bright green",
        "#ff00ff": "magenta", "#0000ff": "deep blue",
    }.get(h, "bright solid green")


def _build_prompt(rec: CharacterRecord, state: str, *,
                  transparent: bool = False, bg_color: str = "") -> str:
    suffix = (rec.expression_prompts.get(state)
              or DEFAULT_EXPRESSION_SUFFIX.get(state)
              or DEFAULT_EXPRESSION_SUFFIX["neutral"])
    style = STYLE_DESCRIPTOR.get(rec.style, rec.style or "")
    # Foreground the expression and push for a strong, clearly-readable emotion
    # so seed-/img2img-locked frames don't all collapse to the same neutral look.
    parts = [rec.base_prompt.strip(), suffix,
             "strongly expressive emotional face, exaggerated clear distinct expression",
             style]
    if rec.palette:
        parts.append(f"{rec.palette} color palette")
    if transparent:
        parts.append(f"isolated full subject on a plain solid flat {_color_word(bg_color)} "
                     "background, clean chroma-key backdrop, no scenery")
    parts.append(_FRAMING)
    return ", ".join(p for p in parts if p)


def _write_frame(agent_id: str, state: str, image_b64: str) -> Optional[str]:
    if not image_b64:
        return None
    try:
        d = _CHARS_DIR / _safe(agent_id)
        d.mkdir(parents=True, exist_ok=True)
        fname = f"{_safe(state)}.png"
        (d / fname).write_bytes(base64.b64decode(image_b64))
        return fname
    except Exception as e:
        log.warning("character write frame %s/%s: %s", agent_id, state, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "character.capabilities", memory="off", silent=True,
    http_method="GET", http_path="/character/capabilities", http_tags=["character"],
    description="Report which character generation/animation tiers are live right now "
                "(probes the GPU SD endpoint). Output: {sd:{txt2img,img2img,...}, "
                "render_modes:[{id,live}], styles:[...], states:[...]}.",
)
async def character_capabilities(trace_id=None):
    sd = await _call_cap("image.sd_capabilities")
    img2img = bool(sd.get("img2img"))
    talking = bool(sd.get("talking_head"))
    return {
        "sd": sd,
        "render_modes": [
            {"id": "procedural",  "live": True,
             "note": "expression-still swap + idle bob/blink + TTS mouth-flap"},
            {"id": "spritesheet", "live": True,
             "note": "canvas frame-cycle animation from a generated sheet"},
            {"id": "talkinghead", "live": talking,
             "note": "lifelike lip-sync (needs a talking-head model on the GPU)"},
        ],
        "consistency": {"img2img": img2img, "txt2img": True},
        "styles": list(STYLE_DESCRIPTOR.keys()),
        "states": ALL_STATES,
    }


@capability(
    "character.describe", memory="off",
    http_method="POST", http_path="/character/describe", http_tags=["character"],
    description="Expand a freeform character description into structured Stable "
                "Diffusion prompts via the LLM. Input: description (str!), style "
                "(pixel|painted|anime|cartoon|3d). Output: {base_prompt, "
                "expression_prompts:{state:suffix}, negative_prompt, palette, style}.",
)
async def character_describe(description: str = "", style: str = "pixel", trace_id=None):
    if not description.strip():
        return {"error": "description required"}
    states_csv = ", ".join(ALL_STATES)
    sys_prompt = (
        "You are an art director writing Stable Diffusion prompts for a single "
        "recurring character. Respond ONLY with compact JSON, no prose.")
    prompt = (
        f"Character brief: {description}\n"
        f"Target visual style: {style}.\n\n"
        "Produce JSON with keys:\n"
        '  "base_prompt": a vivid comma-separated description of the character\'s '
        "fixed identity (species, clothing, colors, distinguishing features) — "
        "NO expression or background words;\n"
        '  "palette": short color-palette phrase;\n'
        '  "negative_prompt": things to avoid;\n'
        f'  "expression_prompts": an object mapping each of these states '
        f"[{states_csv}] to a VIVID, EXAGGERATED description of that emotion — "
        "include BOTH the facial expression AND body language (eyes, brow, mouth, "
        "hands, posture). Make each state clearly and obviously DIFFERENT from the "
        "others and lean hard into the emotion. 12-25 words each."
    )
    try:
        raw = await ollama_generate(prompt=prompt, system=sys_prompt,
                                    json_mode=True, job_type="chat")
    except Exception as e:
        # Never let an LLM hiccup abort generation — fall back to using the
        # raw description as the base prompt with default expression suffixes.
        log.warning("character.describe: LLM call failed (%s); using description as-is", e)
        raw = ""
    data = _parse_json(raw)
    if not isinstance(data, dict):
        data = {}
    # Defensive defaults so generation can always proceed.
    base = (data.get("base_prompt") or description).strip()
    exprs = data.get("expression_prompts")
    if not isinstance(exprs, dict) or not exprs:
        exprs = dict(DEFAULT_EXPRESSION_SUFFIX)
    return {
        "base_prompt":        base,
        "palette":            (data.get("palette") or "").strip(),
        "negative_prompt":    (data.get("negative_prompt") or _DEFAULT_NEGATIVE).strip(),
        "expression_prompts": {k: str(v) for k, v in exprs.items()},
        "style":              style,
    }


def _parse_json(text: str):
    if not text:
        return None
    t = text.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # Pull the first {...} block
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


@capability(
    "character.generate", memory="on",
    http_method="POST", http_path="/character/generate", http_tags=["character", "gpu"],
    description="Generate expression frames for an agent's character with Stable "
                "Diffusion, using img2img for cross-frame consistency when available "
                "(falls back to seed-locked txt2img). Inputs: agent_id (str!), "
                "description or base_prompt, style, render_mode, states (csv, default "
                "neutral,talking,thinking,happy), seed, steps, keep_existing (bool). "
                "Emits character.generate.progress events. Output: the saved character "
                "record + frame URLs. NOTE: SD runs on CPU here, so expect ~30-60s per "
                "frame.",
)
async def character_generate(
    agent_id:     str = "",
    description:  str = "",
    base_prompt:  str = "",
    style:        str = "pixel",
    render_mode:  str = "procedural",
    palette:      str = "",
    negative_prompt: str = "",
    states:       str = "",          # csv; empty → DEFAULT_STATES
    seed:         int = -1,
    steps:        int = 22,
    guidance:     float = 7.5,
    width:        int = 512,
    height:       int = 512,
    loras:        str = "",
    consistency:  str = "auto",      # auto | img2img | txt2img
    transparent:  bool = False,      # chroma-key the background out to alpha
    bg_color:     str = "",          # chroma-key colour (hex); "" = default green
    expr_strength: float = 0.62,     # img2img strength for non-neutral frames
    keep_existing: bool = True,
    trace_id=None,
):
    if not agent_id:
        return {"error": "agent_id required"}

    agent = await _get_agent(agent_id=agent_id)
    if not agent:
        return {"error": f"agent not found: {agent_id}"}

    # Load or start a record, merging with any existing one.
    rec = await STORE.get(agent_id) or CharacterRecord(agent_id=agent_id)
    rec.display_name = agent.get("label") or agent.get("name") or rec.display_name
    rec.style        = style or rec.style
    rec.render_mode  = render_mode or rec.render_mode
    rec.palette      = palette or rec.palette
    rec.seed         = int(seed)
    rec.steps        = int(steps)
    rec.guidance     = float(guidance)
    rec.width        = int(width)
    rec.height       = int(height)
    rec.loras        = loras or rec.loras
    rec.transparent  = bool(transparent)
    rec.bg_color     = bg_color or rec.bg_color
    if not rec.voice:
        rec.voice = agent.get("voice", "") or ""

    # Resolve prompts. If we don't have a base prompt yet, ask the LLM.
    if base_prompt:
        rec.base_prompt = base_prompt
    if not rec.base_prompt:
        try:
            described = await character_describe(description=description or rec.description
                                                 or rec.display_name, style=rec.style)
        except Exception as e:
            return {"error": f"prompt design failed: {e}"}
        if described.get("error"):
            return described
        rec.base_prompt = described["base_prompt"]
        rec.palette = rec.palette or described.get("palette", "")
        rec.negative_prompt = negative_prompt or described.get("negative_prompt") or rec.negative_prompt
        rec.expression_prompts = {**described.get("expression_prompts", {}),
                                  **rec.expression_prompts}
    if negative_prompt:
        rec.negative_prompt = negative_prompt
    if description:
        rec.description = description

    want = [s.strip() for s in states.split(",") if s.strip()] or DEFAULT_STATES
    # Always include neutral (the base / lip-flap-closed frame).
    if "neutral" not in want:
        want = ["neutral"] + want

    # Probe SD tier once.
    caps = await _call_cap("image.sd_capabilities")
    img2img_live = bool(caps.get("img2img")) and consistency != "txt2img"

    total = len(want)
    await emit_event({"type": "character.generate.start", "agent_id": agent_id,
                      "states": want, "total": total})

    if not keep_existing:
        rec.frames = {}
        rec.states = []

    base_b64 = None          # neutral frame bytes, reused as img2img init
    tier_used = "txt2img"
    render_device = ""       # 'cuda' | 'cpu' — device the GPU node actually used

    for i, state in enumerate(want):
        prompt = _build_prompt(rec, state, transparent=rec.transparent, bg_color=rec.bg_color)
        await emit_event({"type": "character.generate.progress", "agent_id": agent_id,
                          "state": state, "index": i, "total": total})
        try:
            if state == "neutral" or base_b64 is None or not img2img_live:
                res = await _call_cap(
                    "image.generate",
                    prompt=prompt, negative_prompt=rec.negative_prompt,
                    width=rec.width, height=rec.height, steps=rec.steps,
                    guidance=rec.guidance, seed=rec.seed, loras=rec.loras,
                    transparent=rec.transparent, bg_color=rec.bg_color,
                )
            else:
                # Derive this expression from the neutral frame for consistency,
                # but at higher strength so the expression actually changes.
                res = await _call_cap(
                    "image.img2img",
                    prompt=prompt, init_image_b64=base_b64, strength=float(expr_strength),
                    negative_prompt=rec.negative_prompt,
                    width=rec.width, height=rec.height, steps=rec.steps,
                    guidance=rec.guidance, seed=rec.seed, loras=rec.loras,
                    transparent=rec.transparent, bg_color=rec.bg_color,
                )
                tier_used = "img2img"
        except Exception as e:
            res = {"error": str(e)}

        b64 = (res or {}).get("image_b64", "")
        if not b64:
            await emit_event({"type": "character.generate.error", "agent_id": agent_id,
                              "state": state, "error": (res or {}).get("error", "no image")})
            continue
        render_device = (res or {}).get("device") or render_device
        if state == "neutral":
            base_b64 = b64
        fname = _write_frame(agent_id, state, b64)
        if fname:
            rec.frames[state] = fname
            if state not in rec.states:
                rec.states.append(state)

    if rec.frames:
        rec.sd_tier_used = tier_used
    await STORE.save(rec)
    await emit_event({"type": "character.generate.done", "agent_id": agent_id,
                      "frames": list(rec.frames.keys()), "tier": tier_used,
                      "device": render_device})

    out = _record_with_urls(rec)
    out["render_device"] = render_device
    if not rec.frames:
        # Generation ran but the image endpoint returned nothing for every state.
        # Surface it instead of pretending success — almost always means SD isn't
        # actually loaded on the GPU node (e.g. the diffusers/xformers import
        # error). Probe with GET /image/sd_capabilities.
        out["error"] = ("No frames were generated — the image endpoint returned no "
                        "image for any expression. Is Stable Diffusion loaded on the "
                        "GPU node? Check GET /image/sd_capabilities.")
    return out


def _record_with_urls(rec: CharacterRecord) -> dict:
    d = rec.to_dict()
    ver = int(time.time())
    d["frame_urls"] = {
        st: f"/character/asset?agent_id={rec.agent_id}&state={st}&v={ver}"
        for st in rec.frames.keys()
    }
    return d


@capability(
    "character.preview", memory="off",
    http_method="POST", http_path="/character/preview", http_tags=["character", "gpu"],
    description="Generate ONE expression as a preview WITHOUT saving it — for the "
                "regenerate/accept/reject loop. Inputs: agent_id (str!), state, "
                "prompt_override (per-state expression text), base_prompt (identity "
                "override), style, seed, steps, guidance, width, height, transparent, "
                "bg_color, edit (bool: img2img-from-current for prompt-edits), strength. "
                "Output: {image_b64, state, prompt, device}. Not archived to the fabric.",
)
async def character_preview(
    agent_id:        str = "",
    state:           str = "neutral",
    prompt_override: str = "",
    base_prompt:     str = "",
    style:           str = "",
    seed:            int = -1,
    steps:           int = 22,
    guidance:        float = 7.5,
    width:           int = 512,
    height:          int = 512,
    transparent:     bool = False,
    bg_color:        str = "",
    edit:            bool = False,
    strength:        float = 0.6,
    trace_id=None,
):
    if not agent_id:
        return {"error": "agent_id required"}
    rec = await STORE.get(agent_id) or CharacterRecord(agent_id=agent_id)

    # Build an effective prompt from a throwaway record so nothing is persisted.
    base = base_prompt or rec.base_prompt or rec.description or rec.display_name \
        or "a friendly original character"
    tmp = CharacterRecord(
        agent_id=agent_id, base_prompt=base, style=(style or rec.style),
        palette=rec.palette, negative_prompt=rec.negative_prompt,
        expression_prompts=dict(rec.expression_prompts),
    )
    if prompt_override:
        tmp.expression_prompts[state] = prompt_override
    prompt = _build_prompt(tmp, state, transparent=transparent, bg_color=bg_color)

    # `edit` derives from the current frame (img2img) so prompt tweaks refine the
    # existing look; otherwise a fresh txt2img gives the expression the most room.
    init_b64 = None
    if edit:
        cur = rec.frames.get(state) or rec.frames.get("neutral")
        if cur:
            try:
                init_b64 = base64.b64encode(
                    (_CHARS_DIR / _safe(agent_id) / cur).read_bytes()).decode()
            except Exception:
                init_b64 = None

    common = dict(prompt=prompt, negative_prompt=tmp.negative_prompt,
                  width=int(width), height=int(height), steps=int(steps),
                  guidance=float(guidance), seed=int(seed), loras=rec.loras,
                  transparent=transparent, bg_color=bg_color, store=False)
    if init_b64:
        res = await _call_cap("image.img2img", init_image_b64=init_b64,
                              strength=float(strength), **common)
    else:
        res = await _call_cap("image.generate", **common)

    b64 = (res or {}).get("image_b64", "")
    if not b64:
        return {"error": (res or {}).get("error", "generation failed"), "state": state}
    return {"image_b64": b64, "state": state, "prompt": prompt,
            "device": (res or {}).get("device", "")}


@capability(
    "character.commit_frame", memory="off",
    http_method="POST", http_path="/character/commit_frame", http_tags=["character"],
    description="Accept a previewed image as an agent's frame for a state: writes it, "
                "saves the per-state prompt, and archives it to the fabric. Inputs: "
                "agent_id (str!), state, image_b64 (str!), prompt_override, base_prompt, "
                "style, transparent, bg_color. Output: the updated character record.",
)
async def character_commit_frame(
    agent_id:        str = "",
    state:           str = "neutral",
    image_b64:       str = "",
    prompt_override: str = "",
    base_prompt:     str = "",
    style:           str = "",
    transparent:     Optional[bool] = None,
    bg_color:        str = "",
    trace_id=None,
):
    if not agent_id or not image_b64:
        return {"error": "agent_id and image_b64 required"}
    rec = await STORE.get(agent_id) or CharacterRecord(agent_id=agent_id)
    if not rec.display_name:
        agent = await _get_agent(agent_id=agent_id)
        rec.display_name = agent.get("label") or agent.get("name") or rec.display_name
    if base_prompt:
        rec.base_prompt = base_prompt
    if style:
        rec.style = style
    if prompt_override:
        rec.expression_prompts = {**rec.expression_prompts, state: prompt_override}
    if transparent is not None:
        rec.transparent = bool(transparent)
    if bg_color:
        rec.bg_color = bg_color

    fname = _write_frame(agent_id, state, image_b64)
    if not fname:
        return {"error": "failed to write frame"}
    rec.frames[state] = fname
    if state not in rec.states:
        rec.states.append(state)
    await STORE.save(rec)

    # Archive the accepted frame to the fabric history (rich metadata).
    store_cap = CAPABILITY_REGISTRY.get("images.store")
    if store_cap:
        try:
            asyncio.ensure_future(store_cap["func"](
                image_b64=image_b64,
                prompt=_build_prompt(rec, state, transparent=rec.transparent,
                                     bg_color=rec.bg_color),
                source="character", agent_id=agent_id, state=state,
                width=rec.width, height=rec.height, seed=rec.seed))
        except Exception as e:
            log.debug("commit_frame archive: %s", e)

    await emit_event({"type": "character.frame.committed",
                      "agent_id": agent_id, "state": state})
    return _record_with_urls(rec)


@capability(
    "character.get", memory="off",
    http_method="GET", http_path="/character/get", http_tags=["character"],
    description="Get an agent's character record + frame URLs. Input: agent_id (str!). "
                "Output: the record, or {error} if none.",
)
async def character_get(agent_id: str = "", trace_id=None):
    if not agent_id:
        return {"error": "agent_id required"}
    rec = await STORE.get(agent_id)
    if not rec or rec.archived:
        return {"error": "no character for this agent", "agent_id": agent_id}
    return _record_with_urls(rec)


@capability(
    "character.set", memory="off",
    http_method="POST", http_path="/character/set", http_tags=["character"],
    description="Update a character's config (animation/voice/render_mode) without "
                "regenerating frames. Input: agent_id (str!), then any of render_mode, "
                "style, voice, tts_speed, idle_bob, idle_blink, palette, display_name. "
                "Empty/None values are ignored. Output: the saved record.",
)
async def character_set(
    agent_id:     str = "",
    render_mode:  str = "",
    style:        str = "",
    voice:        str = "",
    tts_speed:    float = -1.0,
    idle_bob:     Optional[bool] = None,
    idle_blink:   Optional[bool] = None,
    palette:      str = "",
    display_name: str = "",
    trace_id=None,
):
    if not agent_id:
        return {"error": "agent_id required"}
    rec = await STORE.get(agent_id) or CharacterRecord(agent_id=agent_id)
    if render_mode:        rec.render_mode = render_mode
    if style:              rec.style = style
    if voice:              rec.voice = voice
    if tts_speed and tts_speed > 0: rec.tts_speed = float(tts_speed)
    if idle_bob is not None:   rec.idle_bob = bool(idle_bob)
    if idle_blink is not None: rec.idle_blink = bool(idle_blink)
    if palette:            rec.palette = palette
    if display_name:       rec.display_name = display_name
    await STORE.save(rec)
    return _record_with_urls(rec)


@capability(
    "character.list", memory="off",
    http_method="GET", http_path="/character/list", http_tags=["character"],
    description="List all characters (one per agent). Output: {characters:[...], count}.",
)
async def character_list(trace_id=None):
    recs = await STORE.list()
    return {"characters": [_record_with_urls(r) for r in recs], "count": len(recs)}


@capability(
    "character.delete", memory="off",
    http_method="POST", http_path="/character/delete", http_tags=["character"],
    description="Delete an agent's character (record + frame files). Input: agent_id (str!).",
)
async def character_delete(agent_id: str = "", trace_id=None):
    ok = await STORE.delete(agent_id)
    return {"deleted": ok, "agent_id": agent_id}


@capability(
    "character.narrate", memory="off",
    http_method="POST", http_path="/character/narrate", http_tags=["character"],
    description="Turn recent system events into ONE short, friendly, in-persona status "
                "line the character can speak. Inputs: agent_id (str), events (JSON list "
                "of strings/objects), mood (str). Output: {text, mood}.",
)
async def character_narrate(agent_id: str = "", events: str = "[]",
                            mood: str = "", trace_id=None):
    try:
        ev = json.loads(events) if isinstance(events, str) else (events or [])
    except Exception:
        ev = []
    persona = ""
    label = "Your assistant"
    if agent_id:
        agent = await _get_agent(agent_id=agent_id)
        if agent:
            label = agent.get("label") or agent.get("name") or label
            persona = (agent.get("system_prompt") or "")[:400]
    if not ev:
        return {"text": "", "mood": mood or "idle"}

    lines = []
    for e in ev[:12]:
        if isinstance(e, dict):
            lines.append(e.get("type", "") + " " + str(e.get("name") or e.get("detail") or ""))
        else:
            lines.append(str(e))
    sys_prompt = (
        f"You are {label}, an AI companion embodied as an animated character. "
        f"{persona}\n"
        "Give a SINGLE short spoken sentence (max 20 words) updating the user on "
        "what the system is doing. Warm and conversational, no markdown, no lists.")
    prompt = "Recent system events:\n- " + "\n- ".join(lines) + "\n\nYour one-line update:"
    text = (await ollama_generate(prompt=prompt, system=sys_prompt,
                                  job_type="summarize") or "").strip()
    text = text.split("\n")[0][:240]
    return {"text": text, "mood": mood or "working"}


# ─────────────────────────────────────────────────────────────────────────────
# ASSET SERVING — read-only PNG frames (path-traversal guarded)
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/character/asset", include_in_schema=False)
async def character_asset(agent_id: str = "", state: str = "neutral"):
    aid = _safe(agent_id)
    st = _safe(state) or "neutral"
    if not aid:
        return JSONResponse({"error": "agent_id required"}, status_code=400)
    target = (_CHARS_DIR / aid / f"{st}.png").resolve()
    try:
        target.relative_to(_CHARS_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(target), media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


# ─────────────────────────────────────────────────────────────────────────────
# UI REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────

# The Studio + Companion panel. Served from its own route and embedded as an
# IFRAME (mode="tab"), exactly like every other full-page panel (markets,
# accounts, …). This is REQUIRED: the harness inlines a panel's html directly
# into the main document unless it contains an <iframe>, so handing it a full
# HTML document would leak that document's global CSS (*, html/body, and class
# names like .main/.tab) into the harness and break its flex layout. The
# <vera-character> element is served at /ui/elements/character.js and pulled in
# by this panel (and the agent editor / desktop host) via a <script> include —
# it is intentionally NOT registered as a global inject panel, so no live
# element/WS runs in the harness document itself.

@APP.get("/character/panel", include_in_schema=False)
async def _character_panel_route():
    from fastapi.responses import HTMLResponse
    p = _HERE / "character_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:red'>character_panel.html not found</p>")


register_ui(
    panel_id="character-studio",
    label="Companion",
    icon="🧚",
    mode="tab",
    tab_order=58,
    html=('<div style="height:100%;display:flex;flex-direction:column;">'
          '<iframe src="/character/panel" '
          'style="flex:1;border:none;width:100%;height:100%" '
          'allow="microphone; autoplay; clipboard-read; clipboard-write"></iframe>'
          '</div>'),
    ui_caps=["character.capabilities", "character.describe", "character.generate",
             "character.get", "character.set", "character.list", "character.delete",
             "character.narrate", "agent.list", "agent.get"],
)

log.info("character_capabilities: ready (assets=%s)", _CHARS_DIR)
