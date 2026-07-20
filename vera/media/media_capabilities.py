"""
media_capabilities.py  —  Images for conversations & dreams + machine vision
============================================================================
Three families of capability, all designed to be called from the chat loop,
the V5 agentic loop, or a dream pipeline:

  media.image.search   — reference-image search (SearXNG images → DuckDuckGo
                         images fallback; no API key needed)
  media.illustrate     — high-level "get me a picture of X": generates with
                         Stable Diffusion (image.generate) and/or searches the
                         web, stores results in the image fabric, and can push
                         them straight into the calling chat session via the
                         panel-dispatch bridge (__chat_render__ pseudo-action)
  vision.models        — list vision-capable (multimodal) Ollama models
  vision.describe      — "eyes" for text-only models: send an image to the
                         best available VL model (qwen-vl / llava / minicpm-v /
                         moondream / llama3.2-vision …) and return the text.
                         This is how a qwen3.5 agent can parse screenshots,
                         photos and generated images without being multimodal
                         itself.

Nothing here hard-requires the GPU node or a search backend — every path
degrades gracefully and returns {"error": …} rather than raising.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY,
    OLLAMA_INSTANCES,
    capability,
    emit_event,
    now_iso,
)
from Vera.vera.config import cfg

log = logging.getLogger("vera.media")

DEFAULT_SEARXNG = os.getenv("VERA_SEARXNG_URL",
                            f"http://{cfg.BACKEND_HOST}:8888").rstrip("/")
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36")}
_TIMEOUT = 20.0


def _cap(name: str):
    """Resolve another capability's callable from the registry (None if absent)."""
    entry = CAPABILITY_REGISTRY.get(name)
    return entry.get("func") if isinstance(entry, dict) else None


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE SEARCH — SearXNG images → DuckDuckGo images fallback
# ─────────────────────────────────────────────────────────────────────────────

async def _image_search_searxng(query: str, limit: int, host: str = "") -> List[Dict[str, Any]]:
    host = (host or DEFAULT_SEARXNG).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=HEADERS) as c:
            r = await c.get(f"{host}/search", params={
                "q": query, "format": "json", "categories": "images",
                "language": "en", "safesearch": 1,
            })
            r.raise_for_status()
            data = r.json()
        out: List[Dict[str, Any]] = []
        for item in (data.get("results") or []):
            src = item.get("img_src", "") or ""
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            thumb = item.get("thumbnail_src", "") or src
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            out.append({
                "title":         item.get("title", ""),
                "image_url":     src,
                "thumbnail_url": thumb,
                "page_url":      item.get("url", ""),
                "engine":        "searxng",
            })
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log.debug("image search searxng [%s]: %s", query[:40], e)
        return []


async def _image_search_ddg(query: str, limit: int) -> List[Dict[str, Any]]:
    """DuckDuckGo image search — vqd-token flow, no API key. Structure is
    fragile across DDG releases; tolerate failure and return []."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get("https://duckduckgo.com/",
                            params={"q": query, "iax": "images", "ia": "images"})
            r.raise_for_status()
            m = (re.search(r"vqd=([\d-]+)&", r.text)
                 or re.search(r'vqd="([\d-]+)"', r.text)
                 or re.search(r"vqd='([\d-]+)'", r.text))
            if not m:
                return []
            vqd = m.group(1)
            r2 = await c.get("https://duckduckgo.com/i.js", params={
                "l": "us-en", "o": "json", "q": query, "vqd": vqd, "p": "1",
            }, headers={**HEADERS, "Referer": "https://duckduckgo.com/"})
            r2.raise_for_status()
            data = r2.json()
        out: List[Dict[str, Any]] = []
        for item in (data.get("results") or [])[:limit]:
            out.append({
                "title":         item.get("title", ""),
                "image_url":     item.get("image", ""),
                "thumbnail_url": item.get("thumbnail", ""),
                "page_url":      item.get("url", ""),
                "engine":        "ddg",
            })
        return out
    except Exception as e:
        log.debug("image search ddg [%s]: %s", query[:40], e)
        return []


@capability(
    "media.image.search",
    http_method="POST", http_path="/media/image/search", http_tags=["media", "images", "search"],
    memory="off",
    description="Search the web for REFERENCE images (photos, diagrams, product shots, "
                "artwork) and return direct image URLs with thumbnails. Tries SearXNG's "
                "image category first, then DuckDuckGo images — no API key required. "
                "WHEN TO USE: the user wants to SEE an example/reference of something "
                "that exists (an animal, a UI, a landmark, a chart style). To CREATE a "
                "new image use media.illustrate or image.generate instead. "
                "Input: query (str!), limit (int, default 6), engine (auto|searxng|ddg), "
                "session_id (str — chat session to push a gallery into, optional), "
                "title (str — gallery caption when pushing to chat). "
                "Output: {results:[{title,image_url,thumbnail_url,page_url,engine}], "
                "count, engine_used, markdown}.",
)
async def cap_media_image_search(
    query:      str = "",
    limit:      int = 6,
    engine:     str = "auto",
    session_id: str = "",
    title:      str = "",
    trace_id=None,
) -> Dict[str, Any]:
    if not (query or "").strip():
        return {"error": "query required", "results": [], "count": 0}
    limit = max(1, min(24, int(limit)))
    engine = (engine or "auto").lower()
    t0 = time.monotonic()

    results: List[Dict[str, Any]] = []
    used = "none"
    if engine in ("auto", "searxng"):
        results = await _image_search_searxng(query, limit)
        used = "searxng" if results else used
    if not results and engine in ("auto", "ddg"):
        results = await _image_search_ddg(query, limit)
        used = "ddg" if results else used

    md = "\n".join(f"![{(r['title'] or query)[:60]}]({r['image_url']})"
                   for r in results[:4])
    await emit_event({"type": "media.image.search.done", "query": query[:80],
                      "count": len(results), "engine_used": used,
                      "elapsed_ms": int((time.monotonic() - t0) * 1000)})

    # Optional: push a gallery card straight into the calling chat session.
    if session_id and results:
        await _push_chat_render(session_id, {
            "kind": "images",
            "title": title or f"Reference images — {query}",
            "images": [{"url": r["image_url"], "thumb": r["thumbnail_url"],
                        "caption": r["title"], "page_url": r["page_url"]}
                       for r in results],
        })

    return {"results": results, "count": len(results),
            "engine_used": used, "query": query, "markdown": md}


# ─────────────────────────────────────────────────────────────────────────────
# CHAT PUSH — shared helper: route a render payload into a chat session via the
# panel-dispatch bridge (handled client-side as the __chat_render__ pseudo-action)
# ─────────────────────────────────────────────────────────────────────────────

async def _push_chat_render(session_id: str, payload: Dict[str, Any],
                            timeout_secs: float = 8.0) -> Dict[str, Any]:
    dispatch = _cap("panel.dispatch")
    if not dispatch:
        return {"ok": False, "error": "panel.dispatch unavailable"}
    try:
        return await dispatch(session_id=session_id, action="__chat_render__",
                              payload=payload, timeout_secs=timeout_secs)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# ILLUSTRATE — one cap that either generates or finds an image, stores it, and
# (optionally) shows it in the chat that asked for it.
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "media.illustrate",
    http_method="POST", http_path="/media/illustrate", http_tags=["media", "images", "sd"],
    description="Produce a DEMONSTRATIVE / ILLUSTRATIVE image for a concept and "
                "(optionally) display it in the chat session that asked. "
                "mode='generate' renders a new image with Stable Diffusion on the GPU "
                "node (image.generate) and archives it in the image fabric; "
                "mode='search' finds real reference images on the web; mode='auto' "
                "tries generation first and falls back to search. "
                "Input: subject (str! — what to depict), style (str — e.g. 'clean "
                "technical diagram', 'watercolor', 'photorealistic'), "
                "mode (auto|generate|search), count (int, default 1, search only), "
                "width/height (int, generation only, default 768), "
                "negative_prompt (str), session_id (str — chat session to push the "
                "image into; leave blank to just return it), title (str — card title). "
                "Output: {ok, mode_used, images:[{url,image_b64?,caption}], markdown}.",
)
async def cap_media_illustrate(
    subject:         str = "",
    style:           str = "",
    mode:            str = "auto",
    count:           int = 1,
    width:           int = 768,
    height:          int = 768,
    negative_prompt: str = "",
    session_id:      str = "",
    title:           str = "",
    trace_id=None,
) -> Dict[str, Any]:
    subject = (subject or "").strip()
    if not subject:
        return {"error": "subject required"}
    mode = (mode or "auto").lower()
    if mode not in ("auto", "generate", "search"):
        mode = "auto"
    count = max(1, min(8, int(count)))

    await emit_event({"type": "media.illustrate", "stage": "start",
                      "message": f"illustrating {subject!r} (mode={mode})"})

    images: List[Dict[str, Any]] = []
    mode_used = ""
    gen_error = ""

    # ── Generation path ──────────────────────────────────────────────────
    if mode in ("auto", "generate"):
        gen = _cap("image.generate")
        if gen:
            prompt = subject if not style else f"{subject}, {style}"
            try:
                # store=False: we persist once via images.store below (which
                # returns the URL we need for the markdown / gallery). Letting
                # image.generate self-archive too would double-store.
                res = await gen(prompt=prompt,
                                negative_prompt=negative_prompt or
                                "text, watermark, blurry, low quality, deformed",
                                width=int(width), height=int(height), store=False)
                b64 = (res or {}).get("image_b64", "")
                if b64:
                    url = ""
                    store = _cap("images.store")
                    if store:
                        try:
                            sres = await store(image_b64=b64, prompt=prompt,
                                               source="illustrate",
                                               width=int(width), height=int(height))
                            url = (sres or {}).get("url", "")
                        except Exception as e:
                            log.debug("illustrate store: %s", e)
                    images.append({"url": url, "image_b64": ("" if url else b64),
                                   "caption": prompt})
                    mode_used = "generate"
                else:
                    gen_error = (res or {}).get("error", "no image returned")
            except Exception as e:
                gen_error = str(e)
        else:
            gen_error = "image.generate capability not loaded"
        if mode == "generate" and not images:
            return {"error": f"generation failed: {gen_error}", "images": []}

    # ── Search path (primary or fallback) ────────────────────────────────
    if not images and mode in ("auto", "search"):
        sres = await cap_media_image_search(query=subject, limit=count,
                                            trace_id=trace_id)
        for r in (sres.get("results") or [])[:count]:
            images.append({"url": r["image_url"], "caption": r["title"] or subject,
                           "page_url": r.get("page_url", ""),
                           "thumb": r.get("thumbnail_url", "")})
        if images:
            mode_used = "search"

    if not images:
        return {"error": f"no image produced (generation: {gen_error or 'skipped'}; "
                         f"search returned nothing)", "images": []}

    md = "\n".join(f"![{(i.get('caption') or subject)[:60]}]({i['url']})"
                   for i in images if i.get("url"))

    pushed = None
    if session_id:
        pushed = await _push_chat_render(session_id, {
            "kind": "images",
            "title": title or (f"Illustration — {subject}" if mode_used == "generate"
                               else f"Reference — {subject}"),
            "images": images,
        })

    await emit_event({"type": "media.illustrate", "stage": "done",
                      "message": f"{len(images)} image(s) via {mode_used}",
                      "mode_used": mode_used})
    return {"ok": True, "mode_used": mode_used, "images": images,
            "markdown": md,
            **({"pushed": bool((pushed or {}).get('ok'))} if session_id else {})}


# ─────────────────────────────────────────────────────────────────────────────
# VISION — multimodal model discovery + describe (eyes for text-only agents)
# ─────────────────────────────────────────────────────────────────────────────

# Ollama model-name fragments that indicate image input support.
_VISION_PATTERNS = re.compile(
    r"(?:^|[/:_-])(?:llava|moondream|bakllava|minicpm-v|granite.*vision)|"
    r"vl\b|vl:|vl-|vision|qwen[23][\.\d]*-?vl|gemma3|pixtral|internvl",
    re.I,
)


async def _list_vision_models() -> List[Dict[str, str]]:
    """[{instance, url, model}] for every vision-capable model on online instances."""
    found: List[Dict[str, str]] = []
    for iid, inst in (OLLAMA_INSTANCES or {}).items():
        if inst.get("status") not in (None, "online"):
            continue
        url = (inst.get("url") or "").rstrip("/")
        if not url:
            continue
        models = inst.get("models") or []
        if not models:
            try:
                async with httpx.AsyncClient(timeout=6) as c:
                    r = await c.get(f"{url}/api/tags")
                    r.raise_for_status()
                    models = [m.get("name", "") for m in r.json().get("models", [])]
            except Exception:
                continue
        for m in models:
            if m and _VISION_PATTERNS.search(m):
                found.append({"instance": iid, "url": url, "model": m})
    return found


@capability(
    "vision.models",
    http_method="GET", http_path="/vision/models", http_tags=["media", "vision"],
    memory="off", silent=True,
    description="List vision-capable (multimodal) models available across the Ollama "
                "instances — the models vision.describe can route an image to. "
                "Output: {models:[{instance,model}], count, default}.",
)
async def cap_vision_models(trace_id=None) -> Dict[str, Any]:
    found = await _list_vision_models()
    return {"models": [{"instance": f["instance"], "model": f["model"]} for f in found],
            "count": len(found),
            "default": (found[0]["model"] if found else "")}


async def _resolve_image_b64(image_b64: str, image_url: str) -> str:
    """Normalise the input image to raw base64 (Ollama's `images` format)."""
    if image_b64:
        # strip a data: URI prefix if present
        if image_b64.startswith("data:"):
            image_b64 = image_b64.split(",", 1)[-1]
        return image_b64.strip()
    if image_url:
        url = image_url
        if url.startswith("/"):  # our own store, e.g. /images/file/abc.png
            port = getattr(cfg, "ORCHESTRATOR_PORT", 8999)
            url = f"http://127.0.0.1:{port}{url}"
        async with httpx.AsyncClient(timeout=30, headers=HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            return base64.b64encode(r.content).decode("ascii")
    return ""


@capability(
    "vision.describe",
    http_method="POST", http_path="/vision/describe", http_tags=["media", "vision"],
    description="Look at an image and answer in text — the EYES for text-only agents "
                "(qwen3.5 etc.): routes the image to the best available multimodal "
                "model (qwen-VL / llava / minicpm-v / moondream / llama3.2-vision…) "
                "and returns its answer. WHEN TO USE: describing a photo/screenshot, "
                "reading text or charts out of an image, checking whether a generated "
                "image matches its prompt. "
                "Input: image_b64 (str — base64 or data: URI) OR image_url (str — "
                "http(s) URL or a fabric-store path like /images/file/x.png), "
                "prompt (str — the question, default 'Describe this image in detail.'), "
                "model (str — force a specific model tag, optional). "
                "Output: {text, model, instance, elapsed_ms} or {error, available:[…]}.",
)
async def cap_vision_describe(
    image_b64: str = "",
    image_url: str = "",
    prompt:    str = "Describe this image in detail.",
    model:     str = "",
    trace_id=None,
) -> Dict[str, Any]:
    try:
        b64 = await _resolve_image_b64(image_b64, image_url)
    except Exception as e:
        return {"error": f"could not load image: {e}"}
    if not b64:
        return {"error": "image_b64 or image_url required"}

    candidates = await _list_vision_models()
    if model:
        candidates = ([c for c in candidates if c["model"] == model]
                      or [{"instance": iid, "url": (inst.get("url") or "").rstrip("/"),
                           "model": model}
                          for iid, inst in (OLLAMA_INSTANCES or {}).items()
                          if inst.get("url")][:1])
    if not candidates:
        return {"error": "no vision-capable model found on any Ollama instance — "
                         "pull one (e.g. `ollama pull qwen2.5vl` or `ollama pull "
                         "llava`) or pass model= explicitly",
                "available": []}

    t0 = time.monotonic()
    last_err = ""
    for cand in candidates[:3]:
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(f"{cand['url']}/api/generate", json={
                    "model": cand["model"],
                    "prompt": prompt or "Describe this image in detail.",
                    "images": [b64],
                    "stream": False,
                })
                r.raise_for_status()
                data = r.json()
            text = (data.get("response") or "").strip()
            # native-thinking VL models may wrap output in <think>…</think>
            text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
            if text:
                await emit_event({"type": "vision.describe.done",
                                  "model": cand["model"],
                                  "elapsed_ms": int((time.monotonic() - t0) * 1000)})
                return {"text": text, "model": cand["model"],
                        "instance": cand["instance"],
                        "elapsed_ms": int((time.monotonic() - t0) * 1000)}
            last_err = "empty response"
        except Exception as e:
            last_err = str(e)
            continue
    return {"error": f"vision inference failed: {last_err}",
            "available": [c["model"] for c in candidates]}
