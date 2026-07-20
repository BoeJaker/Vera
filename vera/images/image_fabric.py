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


# Object-store mirror (Garage/S3). Generated images live in the on-disk _store
# (the always-available gallery source of truth) AND, when the object store is
# configured, are mirrored to it under images/<id>.png so they are durable,
# portable across hosts, and survive a wiped local store. The file route falls
# back to the blob when the local copy is missing.
_BLOB_PREFIX = "images"


def _obj_store():
    fab = _fabric()
    store = getattr(fab, "OBJECT_STORE", None) if fab else None
    if store is None:
        for name, mod in list(sys.modules.items()):
            if mod is not None and name.endswith("data_fabric") \
                    and hasattr(mod, "OBJECT_STORE"):
                store = mod.OBJECT_STORE
                break
    if store is not None and getattr(store, "mode", "none") != "none":
        return store
    return None


def _blob_key(img_id: str) -> str:
    return f"{_BLOB_PREFIX}/{_safe(img_id)}.png"


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
        raw = base64.b64decode(image_b64)
        (_STORE / fname).write_bytes(raw)
    except Exception as e:
        return {"error": f"write failed: {e}"}

    # Mirror to the object store (Garage/S3) so the image is durable + portable.
    # Best-effort: the on-disk copy is always the primary; the blob is a backup
    # the file route falls back to.
    import asyncio as _asyncio
    blob_key = ""
    store = _obj_store()
    if store is not None:
        try:
            k = _blob_key(img_id)
            if await _asyncio.to_thread(store.put, k, raw, "image/png"):
                blob_key = k
        except Exception as e:
            log.debug("images.store blob mirror: %s", e)

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
        "blob_key":        blob_key,       # object-store key when mirrored
        "created_at":      now_iso(),
    }
    tags = ["image", source] + ([f"agent:{agent_id}"] if agent_id else [])

    # Sidecar metadata next to the PNG: the on-disk store is the gallery's
    # durable source of truth. The fabric ingest below is best-effort (it can
    # be unavailable at generation time), which is why the gallery used to
    # "forget" images — the file was written but no queryable record existed.
    try:
        (_STORE / f"{img_id}.json").write_text(
            json.dumps(item, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("images.store sidecar: %s", e)

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
                      "source": source, "device": device, "fabric": ingested,
                      "blob": bool(blob_key)})
    return {"id": img_id, "url": url, "filename": fname,
            "dataset": _DATASET, "fabric": ingested, "blob_key": blob_key}


def _disk_records() -> list:
    """Gallery records from the on-disk store: every PNG, enriched by its
    sidecar JSON when one exists (older files fall back to the file's mtime).
    This is what makes the gallery durable — it lists what is actually stored,
    whether or not the fabric ingest succeeded at generation time."""
    from datetime import datetime, timezone
    out = []
    try:
        for p in _STORE.glob("*.png"):
            rec = {k: None for k in _GALLERY_FIELDS}
            rec["url"] = f"/images/file/{p.name}"
            side = p.with_suffix(".json")
            if side.is_file():
                try:
                    meta = json.loads(side.read_text(encoding="utf-8"))
                    if isinstance(meta, dict):
                        for k in _GALLERY_FIELDS:
                            if meta.get(k) is not None:
                                rec[k] = meta[k]
                        rec["url"] = meta.get("url") or rec["url"]
                except Exception:
                    pass
            if not rec.get("created_at"):
                try:
                    rec["created_at"] = datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc).isoformat()
                except Exception:
                    rec["created_at"] = ""
            out.append(rec)
    except Exception as e:
        log.debug("images.list disk scan: %s", e)
    return out


@capability(
    "images.list", memory="off",
    http_method="GET", http_path="/images/list", http_tags=["images", "fabric"],
    description="List recently generated images, newest first — merged from the "
                "on-disk image store (durable, always available) and the fabric "
                "'images' dataset. Inputs: limit (int, default 60), agent_id (filter). "
                "Output: {images:[{url,prompt,model,device,seed,…}], count}.",
)
async def images_list(limit: int = 60, agent_id: str = "", trace_id=None):
    by_url = {}
    for rec in _disk_records():
        by_url[rec["url"]] = rec

    fabric = _fabric()
    if fabric and hasattr(fabric, "query_dataset"):
        try:
            results = await fabric.query_dataset(
                dataset_id=_DATASET,
                query={"limit": max(1, min(int(limit) * 4, 2000)), "include_data": True},
            )
            for r in (results or []):
                d = r.get("data") or {}
                if isinstance(d, str):
                    try: d = json.loads(d)
                    except Exception: d = {}
                if not isinstance(d, dict) or not d.get("url"):
                    continue
                rec = {k: d.get(k) for k in _GALLERY_FIELDS}
                # Fabric metadata is richer than a bare disk scan; let it win.
                ex = by_url.get(rec["url"])
                if ex is None or ex.get("prompt") in (None, ""):
                    by_url[rec["url"]] = {**(ex or {}), **{k: v for k, v in rec.items()
                                                           if v is not None}}
        except Exception as e:
            log.debug("images.list fabric query: %s", e)

    out = list(by_url.values())
    if agent_id:
        out = [r for r in out if r.get("agent_id") == agent_id]
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"images": out[:int(limit)], "count": len(out)}


@APP.get("/images/file/{name}", include_in_schema=False)
async def images_file(name: str):
    safe = _safe(name)
    if not safe or not safe.lower().endswith(".png"):   # sidecar .json stays private
        return JSONResponse({"error": "invalid name"}, status_code=400)
    target = (_STORE / safe).resolve()
    try:
        target.relative_to(_STORE.resolve())
    except ValueError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.is_file():
        # Fall back to the object-store mirror (local store wiped / different
        # host). Restore it to disk so the gallery scan re-discovers it.
        store = _obj_store()
        if store is not None:
            import asyncio as _asyncio
            img_id = safe[:-4]   # strip .png
            try:
                data = await _asyncio.to_thread(store.get, _blob_key(img_id))
            except Exception:
                data = None
            if data:
                try:
                    target.write_bytes(data)
                except Exception:
                    pass
                from fastapi.responses import Response
                return Response(content=data, media_type="image/png",
                                headers={"Cache-Control": "public, max-age=86400"})
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


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE STUDIO — single hub tab that groups every image-gen surface (Generate,
# LoRA marketplace, Thumbnails, Sprites, Companion, Gallery) under sub-tabs. The
# individual panels keep their own routes (iframed by the hub); their standalone
# top-level tabs are folded into this hub below.
# ─────────────────────────────────────────────────────────────────────────────

@APP.get("/imagestudio/panel", include_in_schema=False)
async def _image_studio_panel_route():
    from fastapi.responses import HTMLResponse
    p = _HERE / "image_studio_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:red'>image_studio_panel.html not found</p>")


register_ui(
    panel_id="image-studio",
    label="Image Studio",
    icon="◨",
    mode="tab",
    tab_order=18,
    html=('<div style="height:100%;display:flex;flex-direction:column;">'
          '<iframe src="/imagestudio/panel" '
          'style="flex:1;border:none;width:100%;height:100%" '
          'allow="microphone; autoplay; clipboard-read; clipboard-write"></iframe></div>'),
    ui_caps=["image.generate", "image.img2img", "image.ipadapter", "image.pose",
             "image.upscale", "image.rembg", "image.sd_capabilities", "image.thumbnail",
             "sd.loras", "sd.lora_search", "sd.lora_install", "sd.lora_delete",
             "sd.lora_store", "sd.lora_store_delete",
             "images.list", "spritegen.from_image"],
)

# Thumbnails now live inside the Image Studio hub (route /images/thumbnail_panel is
# kept for the hub iframe). Re-enable this block to restore the standalone tab.
# register_ui(panel_id="thumbnail-studio", label="Thumbnails", icon="🖼", mode="tab",
#             tab_order=59, html=..., ui_caps=["image.thumbnail"])

log.info("image_fabric: ready (store=%s, dataset=%s)", _STORE, _DATASET)
