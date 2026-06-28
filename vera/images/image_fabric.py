"""
image_fabric.py — persist generated images into the data fabric
================================================================
Every Stable-Diffusion generation (txt2img / img2img, including character
frames) is archived here so it is browsable in the Fabric panel and reusable
elsewhere. Images are saved to a small on-disk store and served behind a
`.png` URL; a fabric record in the `images` dataset carries that URL plus the
generation metadata (prompt, model, device, seed, size, source, agent).

Why a `.png` URL: the Fabric panel already renders any record value that looks
like an image URL as an <img> (see fabric_panel.html), so storing the URL is
all that's needed for the images to *show up* in the fabric — no panel change.

Capabilities
────────────
  images.store   — save an image (base64) + ingest its fabric record
  images.list    — list recent stored images (newest first), optionally by agent

Route
─────
  GET /images/file/{name}.png   — serve a stored image (read-only)
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi.responses import FileResponse, JSONResponse

from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)

log = logging.getLogger("vera.images")

_HERE     = Path(__file__).parent
_STORE    = _HERE / "_store"
_STORE.mkdir(parents=True, exist_ok=True)
_DATASET  = "images"

_GALLERY_FIELDS = ("url", "prompt", "negative_prompt", "model", "device", "seed",
                   "steps", "guidance", "width", "height", "source", "agent_id",
                   "state", "created_at")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", name or "")


def _fabric():
    return sys.modules.get("data_fabric")


@capability(
    "images.store", memory="off",
    http_method="POST", http_path="/images/store", http_tags=["images", "fabric"],
    description="Save a generated image (base64 PNG) to the image store and ingest a "
                "record into the data fabric 'images' dataset so it is browsable in "
                "the Fabric panel. Inputs: image_b64 (str!), prompt, negative_prompt, "
                "model, device (cuda|cpu), seed, steps, guidance, width, height, "
                "source (txt2img|img2img), agent_id, state. Output: {id, url, filename}.",
)
async def images_store(
    image_b64:       str = "",
    prompt:          str = "",
    negative_prompt: str = "",
    model:           str = "",
    device:          str = "",
    seed:            int = -1,
    steps:           int = 0,
    guidance:        float = 0.0,
    width:           int = 0,
    height:          int = 0,
    source:          str = "txt2img",
    agent_id:        str = "",
    state:           str = "",
    trace_id=None,
):
    if not image_b64:
        return {"error": "image_b64 required"}

    img_id = uuid.uuid4().hex[:16]
    fname = f"{img_id}.png"
    try:
        (_STORE / fname).write_bytes(base64.b64decode(image_b64))
    except Exception as e:
        return {"error": f"write failed: {e}"}

    # URL ends in .png so the Fabric panel's image-URL detector renders it inline.
    url = f"/images/file/{fname}"
    item = {
        "text":            prompt or "(generated image)",
        "url":             url,          # picked up as an <img> by the fabric viewer
        "prompt":          prompt,
        "negative_prompt": negative_prompt,
        "model":           model,
        "device":          device,
        "seed":            seed,
        "steps":           steps,
        "guidance":        guidance,
        "width":           width,
        "height":          height,
        "source":          source,
        "agent_id":        agent_id,
        "state":           state,
        "mime_type":       "image/png",
        "created_at":      now_iso(),
    }
    tags = ["image", source] + ([f"agent:{agent_id}"] if agent_id else [])

    fabric = _fabric()
    ingested = False
    if fabric and hasattr(fabric, "ingest_dataset"):
        try:
            await fabric.ingest_dataset(dataset_id=_DATASET, data=[item],
                                        source="image", tags=tags, source_id=img_id)
            ingested = True
        except Exception as e:
            log.warning("images.store fabric ingest: %s", e)

    await emit_event({"type": "images.stored", "id": img_id, "url": url,
                      "source": source, "device": device, "fabric": ingested})
    return {"id": img_id, "url": url, "filename": fname,
            "dataset": _DATASET, "fabric": ingested}


@capability(
    "images.list", memory="off",
    http_method="GET", http_path="/images/list", http_tags=["images", "fabric"],
    description="List recently generated images from the fabric 'images' dataset, "
                "newest first. Inputs: limit (int, default 60), agent_id (filter). "
                "Output: {images:[{url,prompt,model,device,seed,…}], count}.",
)
async def images_list(limit: int = 60, agent_id: str = "", trace_id=None):
    fabric = _fabric()
    if not fabric or not hasattr(fabric, "query_dataset"):
        return {"images": [], "count": 0}
    try:
        results = await fabric.query_dataset(
            dataset_id=_DATASET,
            query={"limit": max(1, min(int(limit) * 4, 2000)), "include_data": True},
        )
    except Exception as e:
        return {"images": [], "count": 0, "error": str(e)}

    out = []
    for r in (results or []):
        d = r.get("data") or {}
        if isinstance(d, str):
            try: d = json.loads(d)
            except Exception: d = {}
        if not isinstance(d, dict) or not d.get("url"):
            continue
        if agent_id and d.get("agent_id") != agent_id:
            continue
        out.append({k: d.get(k) for k in _GALLERY_FIELDS})
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"images": out[:int(limit)], "count": len(out)}


@APP.get("/images/file/{name}", include_in_schema=False)
async def images_file(name: str):
    safe = _safe(name)
    if not safe:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    target = (_STORE / safe).resolve()
    try:
        target.relative_to(_STORE.resolve())
    except ValueError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(target), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@APP.get("/images/thumbnail_panel", include_in_schema=False)
async def _thumbnail_panel_route():
    from fastapi.responses import HTMLResponse
    p = _HERE / "thumbnail_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:red'>thumbnail_panel.html not found</p>")


register_ui(
    panel_id="thumbnail-studio",
    label="Thumbnails",
    icon="🖼",
    mode="tab",
    tab_order=59,
    html=('<div style="height:100%;display:flex;flex-direction:column;">'
          '<iframe src="/images/thumbnail_panel" '
          'style="flex:1;border:none;width:100%;height:100%" '
          'allow="clipboard-read; clipboard-write"></iframe></div>'),
    ui_caps=["image.thumbnail"],
)

log.info("image_fabric: ready (store=%s, dataset=%s)", _STORE, _DATASET)
