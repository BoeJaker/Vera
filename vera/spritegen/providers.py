"""providers.py — purpose-built external sprite generators (opt-in)
====================================================================
PixelLab (https://api.pixellab.ai/v1) is a pixel-art-native generation API:
character sprites with transparent backgrounds and whole animation cycles that
keep the reference identity (text- or skeleton-driven). It produces far more
consistent sprite animation than a generic SD img2img chain, so when a key is
configured the pipeline uses it as the TOP identity tier and falls back to the
local GPU chain on any error.

Enable by setting PIXELLAB_API_KEY (or PIXELLAB_TOKEN) in the environment.
A definition can force the choice with provider = auto | local | pixellab.

Everything here is defensive: unexpected schemas/log-in walls degrade to None
so the caller's local chain still renders the frame.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import List, Optional

import httpx

log = logging.getLogger("vera.spritegen")

PIXELLAB_BASE = os.getenv("PIXELLAB_API_BASE", "https://api.pixellab.ai/v1").rstrip("/")

# animate-with-text works on a fixed 64px cell; pixflux allows 16..400.
_ANIM_SIZE = 64
_TIMEOUT = float(os.getenv("PIXELLAB_TIMEOUT", "300"))


def _key() -> str:
    return (os.getenv("PIXELLAB_API_KEY", "") or os.getenv("PIXELLAB_TOKEN", "")).strip()


def available() -> bool:
    return bool(_key())


def use_for(defn) -> bool:
    """Should this definition render through PixelLab? provider=pixellab forces
    it (errors still fall back); provider=auto uses it whenever a key is set;
    provider=local never does."""
    pref = (getattr(defn, "provider", "auto") or "auto").lower()
    if pref == "local":
        return False
    if pref == "pixellab":
        return available()
    return available()


def _wrap_b64(b64: str) -> dict:
    return {"type": "base64", "base64": f"data:image/png;base64,{b64}"}


def _unwrap_b64(obj) -> str:
    """Accept {'type':'base64','base64':'data:image/png;base64,…'} or a bare
    base64/data-URL string; return plain base64."""
    if isinstance(obj, dict):
        obj = obj.get("base64") or obj.get("image") or ""
    s = str(obj or "")
    if "," in s and s.lstrip().lower().startswith("data:"):
        s = s.split(",", 1)[1]
    return s.strip()


async def _post(path: str, payload: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{PIXELLAB_BASE}{path}", json=payload,
                             headers={"Authorization": f"Bearer {_key()}"})
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json())[:300]
            except Exception:
                detail = r.text[:300]
            log.warning("pixellab %s → HTTP %s: %s", path, r.status_code, detail)
            return None
        return r.json()
    except Exception as e:
        log.warning("pixellab %s failed: %s", path, e)
        return None


def _identity_text(defn) -> str:
    parts = [defn.base_prompt.strip()]
    if getattr(defn, "layers", None):
        parts += [str(v).strip() for v in defn.layers.values() if str(v).strip()]
    if getattr(defn, "palette", ""):
        parts.append(f"{defn.palette} palette")
    return ", ".join(p for p in parts if p)


def base_size(defn) -> int:
    """Reference size: PixelLab outputs NATIVE pixel art, so render close to the
    final sprite size (2× for detail headroom, clamped to the API's range)."""
    return max(64, min(256, int(getattr(defn, "sprite_size", 64) or 64) * 2))


async def generate_base(defn) -> Optional[str]:
    """Reference sprite via /generate-image-pixflux (transparent background).
    Returns plain b64 PNG or None."""
    size = base_size(defn)
    payload = {
        "description": _identity_text(defn) + ", full body, standing, front view",
        "negative_description": (getattr(defn, "negative_prompt", "") or "")[:400],
        "image_size": {"width": size, "height": size},
        "no_background": True,
    }
    seed = int(getattr(defn, "seed", -1) or -1)
    if seed > 0:
        payload["seed"] = seed
    data = await _post("/generate-image-pixflux", payload)
    if not data:
        return None
    b64 = _unwrap_b64(data.get("image"))
    return b64 or None


def _ref_at_anim_size(ref_b64: str) -> str:
    """animate-with-text wants a 64px reference; nearest-resize the stored one."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(ref_b64))).convert("RGBA")
        if img.size != (_ANIM_SIZE, _ANIM_SIZE):
            img.thumbnail((_ANIM_SIZE, _ANIM_SIZE), Image.NEAREST)
            cell = Image.new("RGBA", (_ANIM_SIZE, _ANIM_SIZE), (0, 0, 0, 0))
            cell.alpha_composite(img, ((_ANIM_SIZE - img.width) // 2,
                                       _ANIM_SIZE - img.height))
            img = cell
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ref_b64


async def animate(defn, anim: str, n_frames: int, ref_b64: str) -> List[str]:
    """Whole animation cycle via /animate-with-text (identity from the reference,
    motion from the action text). Returns a list of plain b64 PNG frames
    ([] on failure so the caller falls back to the local per-frame chain)."""
    if not ref_b64:
        return []
    spec = (getattr(defn, "animations", {}) or {}).get(anim) or {}
    action = (spec.get("desc") or anim).strip() or anim
    payload = {
        "description": _identity_text(defn),
        "action": action,
        "image_size": {"width": _ANIM_SIZE, "height": _ANIM_SIZE},
        "reference_image": _wrap_b64(_ref_at_anim_size(ref_b64)),
        "view": "side",
        "direction": "east",
        "n_frames": max(2, min(20, int(n_frames or 4))),
    }
    seed = int(getattr(defn, "seed", -1) or -1)
    if seed > 0:
        payload["seed"] = seed
    data = await _post("/animate-with-text", payload)
    if not data:
        return []
    frames = [f for f in (_unwrap_b64(i) for i in (data.get("images") or [])) if f]
    if not frames:
        log.warning("pixellab animate(%s): response had no images", anim)
    return frames


async def balance() -> Optional[float]:
    """Account credit in USD (None when unavailable)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{PIXELLAB_BASE}/balance",
                            headers={"Authorization": f"Bearer {_key()}"})
        r.raise_for_status()
        return float((r.json() or {}).get("usd", 0))
    except Exception:
        return None
