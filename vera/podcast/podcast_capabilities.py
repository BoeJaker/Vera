# ============================================================================
# podcast_capabilities.py — multi-voice podcast generation
# ============================================================================
#
# Turns fabric data feeds / capability results / free text into a produced,
# multi-voice podcast episode:
#
#   sources ──> gather (fabric.query / caps / urls / text)
#           ──> script  (llm.generate → {title, segments:[{speaker,text}]})
#           ──> voices  (GPU inference /tts per segment, one voice per speaker)
#           ──> stitch  (WAV concat + inter-segment gaps, optional ffmpeg mp3)
#           ──> episode (wav/mp3 + json sidecar, fabric record, /podcast/audio/<id>)
#
# There is NO separate UI panel — the chat panel embeds a podcast composer
# (chat_panel.html) and the agent loop can call these caps directly.
#
# Caps: podcast.script, podcast.generate, podcast.status, podcast.list,
#       podcast.get, podcast.delete, podcast.settings.get/set
# ============================================================================

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
import wave
from typing import Any, Dict, List, Optional

import httpx

from Vera.vera.capability_orchestration import (
    APP,
    capability,
    emit_event,
    media_base,
    now_iso,
)
from Vera.vera.config import cfg

log = logging.getLogger("vera.podcast")

GPU_INFER_URL = cfg.GPU_INFER_URL

_HERE = os.path.dirname(os.path.abspath(__file__))
_EP_DIR = os.path.join(_HERE, "generated")
os.makedirs(_EP_DIR, exist_ok=True)
_SETTINGS_PATH = os.path.join(_EP_DIR, "settings.json")

# Object-store mirror (Garage/S3): finished episodes are written to _EP_DIR (the
# durable local library) AND, when the object store is configured, mirrored to
# it under podcasts/<episode_id>.<fmt> so they are durable + portable and the
# audio route can fall back to the blob when the local file is missing.
_BLOB_PREFIX = "podcasts"


def _obj_store():
    import sys as _sys
    for name, mod in list(_sys.modules.items()):
        if mod is not None and name.endswith("data_fabric") \
                and hasattr(mod, "OBJECT_STORE"):
            store = mod.OBJECT_STORE
            if store is not None and getattr(store, "mode", "none") != "none":
                return store
            return None
    return None


def _ep_blob_key(episode_id: str, fmt: str) -> str:
    eid = re.sub(r"[^a-f0-9]", "", episode_id or "")
    return f"{_BLOB_PREFIX}/{eid}.{ 'mp3' if fmt == 'mp3' else 'wav' }"

# In-memory job table (episodes themselves are durable on disk)
_JOBS: Dict[str, Dict[str, Any]] = {}
_MAX_JOBS = 50

DEFAULT_SPEAKERS = [
    {"name": "Nova", "voice": "af_heart",   "speed": 1.0,
     "persona": "warm, curious host who frames the topic and asks sharp questions"},
    {"name": "Rex",  "voice": "am_michael", "speed": 1.0,
     "persona": "analytical co-host who digs into details and adds context"},
]

DEFAULT_SETTINGS = {
    "speakers":  DEFAULT_SPEAKERS,
    "style":     "conversational",   # conversational|interview|news|deep-dive|debate|story
    "minutes":   3,
    "gap_ms":    350,
    "engine":    "",                 # "" = GPU server default (kokoro)
    "format":    "mp3_if_possible",  # mp3_if_possible | wav
    "show_name": "Vera Signal",
    "intro":     True,               # scripted intro/outro lines
}

STYLE_HINTS = {
    "conversational": "a relaxed two-way conversation with natural back-and-forth, light humour and follow-up questions",
    "interview":      "an interview: the first speaker asks probing questions, the others answer as domain experts",
    "news":           "a tight news briefing: crisp factual segments, clear hand-offs, no filler",
    "deep-dive":      "a structured deep dive: define the topic, explore 3-4 angles in depth, close with implications",
    "debate":         "a friendly debate: speakers take opposing stances, challenge each other, then find common ground",
    "story":          "narrative storytelling: scene-setting, tension and pay-off, speakers trade narration",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _call_cap(name: str, **kwargs) -> Any:
    """Call another capability by registry name (lazy import, kwargs filtered)."""
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"unknown_cap:{name}"}
    try:
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        filtered = {k: v for k, v in kwargs.items() if k in accepted}
        return await cap["func"](**filtered)
    except Exception as e:
        log.warning("podcast _call_cap %s: %s", name, e)
        return {"error": f"{type(e).__name__}: {e}"}


def _load_settings() -> Dict[str, Any]:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        out = dict(DEFAULT_SETTINGS)
        out.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        return out
    except Exception:
        return dict(DEFAULT_SETTINGS)


def _save_settings(s: Dict[str, Any]) -> None:
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def _ep_paths(episode_id: str) -> Dict[str, str]:
    return {
        "meta": os.path.join(_EP_DIR, f"{episode_id}.json"),
        "wav":  os.path.join(_EP_DIR, f"{episode_id}.wav"),
        "mp3":  os.path.join(_EP_DIR, f"{episode_id}.mp3"),
    }


def _job_update(job_id: str, **fields) -> None:
    j = _JOBS.get(job_id)
    if j:
        j.update(fields)
        j["updated"] = now_iso()


async def _emit(job_id: str, stage: str, message: str, pct: int, **extra) -> None:
    _job_update(job_id, stage=stage, message=message, pct=pct)
    await emit_event({"type": "podcast.progress", "job_id": job_id,
                      "stage": stage, "message": message, "pct": pct, **extra})


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Source gathering — fabric feeds, caps, urls, raw text
# ─────────────────────────────────────────────────────────────────────────────

def _classify_source_str(s: str) -> Dict[str, Any]:
    """Turn a bare source string into a typed spec: a URL, a FILE path (read from
    the session workspace), or plain TEXT. Lets the agentic loop pass what it
    naturally has — `./output_script_7.txt`, `https://…`, or notes — without
    knowing the structured-spec schema."""
    t = (s or "").strip()
    low = t.lower()
    if low.startswith(("http://", "https://")):
        return {"type": "url", "url": t}
    if low.startswith("www.") and " " not in t:
        return {"type": "url", "url": "https://" + t}
    looks_path = (t.startswith(("./", "../", "/", "~"))
                  or (" " not in t and "\n" not in t
                      and re.search(r"\.[A-Za-z0-9]{1,8}$", t) is not None))
    if looks_path:
        return {"type": "file", "path": t}
    return {"type": "text", "text": t}


async def _gather_sources(sources: List[dict], topic: str,
                          job_id: str = "", max_chars: int = 9000,
                          session_id: str = "") -> List[Dict[str, str]]:
    """Resolve heterogeneous source specs into [{label, text}] context blocks.

    Source spec shapes (a bare STRING is auto-classified as url|file|text):
      {"type":"text",    "text":"...", "label"?}
      {"type":"file",    "path":"./notes.md"}               # read from the workspace
      {"type":"dataset", "dataset_id":"mesh.node1.temp", "query"?, "top_k"?}
      {"type":"fabric",  "query":"...", "top_k"?}            # fabric-wide search
      {"type":"cap",     "name":"web.search", "args":{...}}  # any capability
      {"type":"url",     "url":"https://..."}
    """
    blocks: List[Dict[str, str]] = []
    per_src = max(1200, max_chars // max(1, len(sources or [])))

    for i, src in enumerate(sources or []):
        if isinstance(src, str):
            src = _classify_source_str(src)
        elif not isinstance(src, dict):
            src = {"type": "text", "text": str(src)}
        stype = (src.get("type") or "text").lower()
        label = src.get("label") or ""
        text = ""
        try:
            if stype == "text":
                text = str(src.get("text") or "")
                label = label or "provided notes"

            elif stype == "file":
                fpath = str(src.get("path") or src.get("file") or "").strip()
                label = label or fpath or "file"
                r = await _call_cap("ide.fs.read", path=fpath,
                                    session_id=session_id, max_bytes=200000)
                if isinstance(r, dict) and not r.get("error"):
                    text = r.get("content") or ""
                else:
                    # Not a real file after all → fall back to treating it as text.
                    text = fpath if not (isinstance(r, dict) and r.get("error")) else ""

            elif stype in ("dataset", "fabric"):
                q: Dict[str, Any] = {"top_k": int(src.get("top_k") or 12)}
                query = src.get("query") or topic
                if query:
                    q["text"] = query
                    q["vector"] = query
                if stype == "dataset" and src.get("dataset_id"):
                    q["dataset_id"] = src["dataset_id"]
                    label = label or f"fabric dataset {src['dataset_id']}"
                else:
                    label = label or "data fabric search"
                r = await _call_cap("fabric.query", query=q)
                rows = (r or {}).get("results") or []
                parts = []
                for row in rows:
                    s = row.get("summary") or ""
                    d = row.get("data")
                    if not s and d is not None:
                        s = json.dumps(d, default=str)[:400]
                    if s:
                        parts.append(f"- {s}")
                text = "\n".join(parts)

            elif stype == "cap":
                name = src.get("name") or ""
                args = src.get("args") or {}
                label = label or f"capability {name}"
                r = await _call_cap(name, **args) if name else {"error": "no cap name"}
                if isinstance(r, dict) and r.get("error"):
                    text = ""
                else:
                    text = r if isinstance(r, str) else json.dumps(r, default=str, indent=1)

            elif stype == "url":
                url = src.get("url") or ""
                label = label or url
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                    resp = await c.get(url)
                    ctype = resp.headers.get("content-type", "")
                    text = _strip_html(resp.text) if "html" in ctype else resp.text
        except Exception as e:
            log.warning("podcast source %d (%s) failed: %s", i, stype, e)
            text = ""

        text = (text or "").strip()
        if text:
            blocks.append({"label": label or f"source {i + 1}", "text": text[:per_src]})
        if job_id:
            await _emit(job_id, "gather", f"gathered {i + 1}/{len(sources)} sources", 5 + int(15 * (i + 1) / max(1, len(sources))))

    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# Script generation
# ─────────────────────────────────────────────────────────────────────────────

def _parse_script(raw: str, speaker_names: List[str]) -> Optional[Dict[str, Any]]:
    """Tolerant JSON extraction; falls back to Speaker: line parsing."""
    if not raw:
        return None
    txt = raw.strip()
    txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
    txt = re.sub(r"```\s*$", "", txt).strip()
    a, b = txt.find("{"), txt.rfind("}")
    if a >= 0 and b > a:
        for candidate in (txt[a:b + 1],):
            try:
                doc = json.loads(candidate)
                segs = doc.get("segments") or []
                good = [{"speaker": str(s.get("speaker") or speaker_names[0]),
                         "text": str(s.get("text") or "").strip()}
                        for s in segs if isinstance(s, dict) and str(s.get("text") or "").strip()]
                if good:
                    return {"title": str(doc.get("title") or "").strip(),
                            "description": str(doc.get("description") or "").strip(),
                            "segments": good}
            except Exception:
                pass
    # Fallback: "Name: line" transcript format
    segs = []
    pat = re.compile(r"^\s*\**\s*([A-Za-z][\w .'-]{0,24})\s*\**\s*:\s*(.+)$")
    for line in txt.splitlines():
        m = pat.match(line)
        if m and m.group(2).strip():
            segs.append({"speaker": m.group(1).strip(), "text": m.group(2).strip()})
    if segs:
        return {"title": "", "description": "", "segments": segs}
    return None


async def _generate_script(topic: str, blocks: List[Dict[str, str]],
                           speakers: List[dict], style: str, minutes: float,
                           show_name: str, intro: bool,
                           extra_instructions: str = "") -> Dict[str, Any]:
    names = [s.get("name") or f"Speaker{i+1}" for i, s in enumerate(speakers)]
    words = max(120, int(minutes * 150))
    style_hint = STYLE_HINTS.get(style, style or STYLE_HINTS["conversational"])

    roster = "\n".join(
        f'- {s.get("name")}: {s.get("persona") or "co-host"}' for s in speakers)
    ctx = ""
    if blocks:
        ctx = "\n\nSOURCE MATERIAL (ground every claim in this; quote numbers exactly):\n"
        for b in blocks:
            ctx += f"\n── {b['label']} ──\n{b['text']}\n"

    intro_rule = (f'Open with a short show intro for "{show_name}" and close with a sign-off. '
                  if intro else "No show intro/outro — dive straight in. ")
    system = (
        "You are a professional podcast script writer. You write tight, natural spoken "
        "dialogue — contractions, rhythm, no stage directions, no markdown, no emojis, "
        "nothing that cannot be read aloud verbatim by a TTS engine. Numbers and "
        "abbreviations written out the way a person says them."
    )
    prompt = (
        f"Write a podcast script about: {topic}\n\n"
        f"Format: {style_hint}.\n"
        f"Speakers ({len(names)}):\n{roster}\n\n"
        f"Length: about {words} words total (~{minutes} min spoken). "
        f"Segments of 1-4 sentences each, alternating naturally between speakers. "
        f"{intro_rule}"
        f"{extra_instructions + ' ' if extra_instructions else ''}"
        f"{ctx}\n\n"
        'Respond with ONLY this JSON (no code fences, no commentary):\n'
        '{"title": "...", "description": "one-sentence episode summary", '
        '"segments": [{"speaker": "' + names[0] + '", "text": "..."}, ...]}\n'
        f"Valid speaker values: {json.dumps(names)}."
    )

    r = await _call_cap("llm.generate", prompt=prompt, system=system,
                        prefer_gpu=True, caller="podcast.script")
    script = _parse_script((r or {}).get("text") or "", names)
    if not script:
        # one strict retry
        r = await _call_cap("llm.generate",
                            prompt=prompt + "\n\nIMPORTANT: output ONLY the JSON object.",
                            system=system, prefer_gpu=True, caller="podcast.script")
        script = _parse_script((r or {}).get("text") or "", names)
    if not script:
        return {"error": "script generation failed — LLM did not return a usable script"}

    # normalise speaker names to the roster (LLMs love inventing variants)
    lower = {n.lower(): n for n in names}
    for i, seg in enumerate(script["segments"]):
        seg["speaker"] = lower.get(seg["speaker"].lower(), names[i % len(names)])
    if not script.get("title"):
        script["title"] = topic[:80] or "Untitled episode"
    return script


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis + stitching
# ─────────────────────────────────────────────────────────────────────────────

async def _tts_segment(text: str, voice: str, speed: float, engine: str) -> bytes:
    """Synthesize one segment on the GPU node → WAV bytes."""
    body: Dict[str, Any] = {"text": text, "voice": voice, "speed": speed}
    if engine:
        body["engine"] = engine
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{media_base('tts')}/tts", json=body)
        r.raise_for_status()
        data = r.json()
    b64 = data.get("audio_b64") or ""
    if not b64:
        raise RuntimeError("TTS returned no audio")
    return base64.b64decode(b64)


def _read_wav(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        frames = w.readframes(w.getnframes())
    return params, frames


def _resample(frames: bytes, params, target_rate: int) -> bytes:
    """Best-effort sample-rate conversion (mixed engines only)."""
    ch, sw, rate = params
    if rate == target_rate:
        return frames
    try:
        import audioop  # stdlib ≤3.12
        out, _ = audioop.ratecv(frames, sw, ch, rate, target_rate, None)
        return out
    except Exception:
        log.warning("podcast: no audioop — naive resample %d→%d", rate, target_rate)
        # crude frame duplication/drop; fine for speech at close rates
        n = len(frames) // (sw * ch)
        step = rate / target_rate
        out = bytearray()
        i = 0.0
        while int(i) < n:
            o = int(i) * sw * ch
            out += frames[o:o + sw * ch]
            i += step
        return bytes(out)


def _stitch_wavs(parts: List[bytes], gap_ms: int) -> (bytes, float):
    """Concatenate WAV blobs (normalising sample-rate) with silence gaps."""
    if not parts:
        raise RuntimeError("no audio segments to stitch")
    (ch, sw, rate), _ = _read_wav(parts[0])
    gap = b"\x00" * int(rate * (gap_ms / 1000.0)) * sw * ch
    pcm = bytearray()
    for i, blob in enumerate(parts):
        params, frames = _read_wav(blob)
        if params[0] != ch or params[1] != sw:
            raise RuntimeError(f"segment {i}: channel/width mismatch {params} vs {(ch, sw, rate)}")
        pcm += _resample(frames, params, rate)
        if i < len(parts) - 1:
            pcm += gap
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    duration = len(pcm) / (rate * sw * ch)
    return buf.getvalue(), duration


async def _maybe_mp3(episode_id: str) -> bool:
    """Convert the stitched WAV to MP3 if ffmpeg is on the PATH."""
    if not shutil.which("ffmpeg"):
        return False
    p = _ep_paths(episode_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", p["wav"], "-codec:a", "libmp3lame", "-b:a", "96k", p["mp3"],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode == 0 and os.path.exists(p["mp3"]):
            os.remove(p["wav"])
            return True
    except Exception as e:
        log.warning("podcast mp3 convert failed: %s", e)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# The pipeline job
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline(job_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    topic     = args["topic"]
    speakers  = args["speakers"]
    style     = args["style"]
    minutes   = args["minutes"]
    gap_ms    = args["gap_ms"]
    engine    = args["engine"]
    fmt       = args["format"]
    sources   = args["sources"]
    script_in = args.get("script")
    show_name = args["show_name"]
    intro     = args["intro"]
    extra     = args.get("instructions") or ""

    t0 = time.time()
    try:
        # 1) sources
        blocks: List[Dict[str, str]] = []
        if sources:
            await _emit(job_id, "gather", f"gathering {len(sources)} sources", 5)
            blocks = await _gather_sources(sources, topic, job_id=job_id,
                                           session_id=args.get("session_id", ""))

        # 2) script
        if script_in and script_in.get("segments"):
            script = script_in
            await _emit(job_id, "script", "using provided script", 25)
        else:
            await _emit(job_id, "script", "writing script with the LLM", 22)
            script = await _generate_script(topic, blocks, speakers, style,
                                            minutes, show_name, intro, extra)
            if script.get("error"):
                raise RuntimeError(script["error"])
        segs = script["segments"]
        _job_update(job_id, script=script)
        await _emit(job_id, "script", f"script ready — {len(segs)} segments", 30,
                    title=script.get("title", ""))

        # 3) per-segment TTS
        vmap = {s.get("name"): s for s in speakers}
        parts: List[bytes] = []
        for i, seg in enumerate(segs):
            sp = vmap.get(seg["speaker"]) or speakers[0]
            pct = 30 + int(55 * (i + 1) / len(segs))
            await _emit(job_id, "voice",
                        f"synthesizing {i + 1}/{len(segs)} ({seg['speaker']} · {sp.get('voice')})", pct)
            parts.append(await _tts_segment(seg["text"], sp.get("voice") or "af_heart",
                                            float(sp.get("speed") or 1.0), engine))

        # 4) stitch + encode
        await _emit(job_id, "stitch", "stitching episode audio", 88)
        wav_bytes, duration = _stitch_wavs(parts, gap_ms)
        episode_id = uuid.uuid4().hex[:12]
        p = _ep_paths(episode_id)
        with open(p["wav"], "wb") as f:
            f.write(wav_bytes)
        out_fmt = "wav"
        if fmt != "wav" and await _maybe_mp3(episode_id):
            out_fmt = "mp3"
        audio_path = p[out_fmt]

        # 5) sidecar + fabric record
        meta = {
            "episode_id":  episode_id,
            "title":       script.get("title") or topic,
            "description": script.get("description", ""),
            "topic":       topic,
            "style":       style,
            "minutes_req": minutes,
            "duration_s":  round(duration, 1),
            "speakers":    speakers,
            "segments":    segs,
            "sources":     [b["label"] for b in blocks],
            "format":      out_fmt,
            "size_bytes":  os.path.getsize(audio_path),
            "audio_url":   f"/podcast/audio/{episode_id}",
            "session_id":  args.get("session_id", ""),
            "created":     now_iso(),
        }
        # Mirror the finished audio to the object store (best-effort backup).
        store = _obj_store()
        if store is not None:
            try:
                bkey = _ep_blob_key(episode_id, out_fmt)
                ctype = "audio/mpeg" if out_fmt == "mp3" else "audio/wav"
                if await asyncio.to_thread(store.upload_file, bkey, audio_path, ctype):
                    meta["blob_key"] = bkey
            except Exception as e:
                log.debug("podcast blob mirror %s: %s", episode_id, e)

        # Deliver the DELIVERABLES into the session artifact dir (the sandbox
        # /workspace when sandboxed) — the finished AUDIO and a readable SCRIPT —
        # so the agentic loop gets real files it can use directly and does NOT
        # bolt on a doomed "save to disk" step. The cap owns persistence; steps
        # don't. Best-effort; the object-store + episode store remain the durable
        # copies regardless.
        sid = args.get("session_id", "")
        if sid:
            _slug = re.sub(r"[^\w.\-]+", "-", (meta["title"] or "episode").lower()).strip("-")[:48] or "episode"
            try:
                import importlib as _il
                _ex = _il.import_module("Vera.vera.execution.exec_capabilities")
                _ap = await _ex.copy_file_into_artifacts(
                    session_id=sid, host_path=audio_path, dest_name=f"{_slug}.{out_fmt}")
                if _ap:
                    meta["audio_file"] = _ap
                _transcript = "\n\n".join(f"{sg.get('speaker', '')}: {sg.get('text', '')}"
                                          for sg in segs)
                _script_body = (f"# {meta['title']}\n"
                                + (f"_{meta['description']}_\n" if meta.get("description") else "")
                                + f"\n{_transcript}\n")
                _sp = await _ex.write_artifact_file(
                    relpath=f"{_slug}.script.md", content=_script_body, session_id=sid)
                if _sp:
                    meta["script_file"] = _sp
            except Exception as e:
                log.debug("podcast artifact export failed for %s: %s", episode_id, e)

        with open(p["meta"], "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        rec = {k: meta[k] for k in ("episode_id", "title", "description", "topic",
                                    "style", "duration_s", "format", "audio_url", "created")}
        await _call_cap("fabric.ingest", dataset_id="podcasts.episodes",
                        records=json.dumps([rec]), source="podcast", tags="podcast,audio")

        _job_update(job_id, status="done", episode=meta)
        await _emit(job_id, "done",
                    f"episode ready — {meta['title']} ({meta['duration_s']}s, {out_fmt})",
                    100, episode_id=episode_id, audio_url=meta["audio_url"],
                    title=meta["title"], duration_s=meta["duration_s"])
        log.info("podcast %s done in %.1fs → %s", job_id, time.time() - t0, audio_path)
        return meta
    except Exception as e:
        log.error("podcast job %s failed: %s", job_id, e, exc_info=True)
        _job_update(job_id, status="error", error=str(e))
        await _emit(job_id, "error", str(e), 100)
        return {"error": str(e)}


def _normalize_args(topic, sources, speakers, style, minutes, gap_ms, engine,
                    fmt, script, show_name, intro, instructions, session_id) -> Dict[str, Any]:
    s = _load_settings()

    def _j(v, default):
        if v is None or v == "":
            return default
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return default
        return v

    speakers = _j(speakers, None) or s["speakers"]
    if not isinstance(speakers, list) or not speakers:
        speakers = DEFAULT_SPEAKERS
    speakers = [sp if isinstance(sp, dict) else {"name": str(sp), "voice": "af_heart"}
                for sp in speakers][:6]
    # Sources: accept a JSON list of specs OR the loose shapes the agentic loop
    # actually passes — a bare file path, a URL, plain text, or a newline/comma list
    # of those. Coerce everything to a list; _gather_sources classifies each item.
    if isinstance(sources, str):
        st = sources.strip()
        if st[:1] in ("[", "{"):
            try:
                sources = json.loads(st)
            except Exception:
                sources = [st]
        elif st:
            # Split a multi-LINE list, or a comma list that clearly holds paths/URLs
            # (not prose — "Llama 3.1, EU rules" must stay one text block).
            if "\n" in st or (("," in st) and ("http" in st or "/" in st)):
                sources = [p.strip() for p in re.split(r"[\n,]+", st) if p.strip()]
            else:
                sources = [st]
        else:
            sources = []
    sources = _j(sources, sources if isinstance(sources, list) else [])
    if isinstance(sources, dict):
        sources = [sources]
    if not isinstance(sources, list):
        sources = []
    script = _j(script, None)
    return {
        "topic":        (topic or "").strip(),
        "sources":      sources,
        "speakers":     speakers,
        "style":        style or s["style"],
        "minutes":      float(minutes or s["minutes"]),
        "gap_ms":       int(gap_ms if gap_ms is not None else s["gap_ms"]),
        "engine":       engine if engine is not None else s["engine"],
        "format":       fmt or s["format"],
        "script":       script,
        "show_name":    show_name or s["show_name"],
        "intro":        s["intro"] if intro is None else bool(intro),
        "instructions": instructions or "",
        "session_id":   session_id or "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "podcast.script",
    http_method="POST", http_path="/podcast/script", http_tags=["podcast"],
    memory="on",
    description="Write a multi-speaker podcast script (no audio) about a topic, optionally "
                "grounded in data sources. Use to preview/edit before podcast.generate. "
                "Input: topic (str!), sources (JSON list — see podcast.generate), "
                "speakers (JSON list [{name,voice,speed,persona}]), style "
                "(conversational|interview|news|deep-dive|debate|story), minutes (float), "
                "instructions (str — extra creative direction). "
                "Output: {title, description, segments:[{speaker,text}], speakers}.",
)
async def cap_podcast_script(topic: str = "", sources=None, speakers=None,
                             style: str = "", minutes: float = 0,
                             instructions: str = "", show_name: str = "",
                             session_id: str = "", trace_id=None) -> dict:
    if not (topic or "").strip():
        return {"error": "topic is required"}
    a = _normalize_args(topic, sources, speakers, style, minutes, None, None,
                        None, None, show_name, None, instructions, session_id)
    blocks = await _gather_sources(a["sources"], a["topic"],
                                   session_id=a["session_id"]) if a["sources"] else []
    script = await _generate_script(a["topic"], blocks, a["speakers"], a["style"],
                                    a["minutes"], a["show_name"], a["intro"], instructions)
    if script.get("error"):
        return script
    script["speakers"] = a["speakers"]
    script["sources"] = [b["label"] for b in blocks]
    return script


@capability(
    "podcast.generate",
    http_method="POST", http_path="/podcast/generate", http_tags=["podcast"],
    memory="on",
    streams=["podcast.progress"],
    description="Generate a complete multi-voice podcast episode from files, URLs, data, or "
                "text: gathers the source material, writes a script with the LLM, voices each "
                "speaker on the GPU TTS engine, stitches the audio, AND saves the finished audio "
                "+ a readable script INTO this session's artifact workspace — so you do NOT need a "
                "separate step to save anything. "
                "WHEN TO USE: the user asks for a podcast / audio briefing / spoken digest. "
                "Input: topic (str!). sources — the material to base it on; pass whatever you "
                "have: a FILE PATH (e.g. './script.txt' — read from the workspace), a URL, plain "
                "TEXT, or a JSON list mixing those and richer specs "
                "({type:text|file|url|dataset|fabric|cap,...}); a bare string or newline/comma "
                "list is auto-classified, so a file reference from a prior step Just Works. "
                "script (JSON pre-written {segments:[{speaker,text}]} skips the LLM). "
                "speakers (JSON list [{name,voice,speed,persona}] — voices from tts.voices), "
                "style (conversational|interview|news|deep-dive|debate|story), minutes (float), "
                "gap_ms (int silence between turns), format (mp3_if_possible|wav), "
                "wait (bool — true blocks until done, false returns a job_id to poll with "
                "podcast.status). "
                "Output: the finished episode {episode_id, title, duration_s, audio_url, "
                "audio_file (path in your workspace), script_file (path in your workspace), "
                "segments}; wait=false → {job_id} to poll. The audio_file/script_file ARE the "
                "deliverables — reference them directly, don't re-save.",
)
async def cap_podcast_generate(topic: str = "", sources=None, speakers=None,
                               style: str = "", minutes: float = 0,
                               gap_ms: int = None, engine: str = None,
                               format: str = "", script=None,
                               show_name: str = "", intro: bool = None,
                               instructions: str = "", session_id: str = "",
                               wait: bool = False, trace_id=None) -> dict:
    a = _normalize_args(topic, sources, speakers, style, minutes, gap_ms, engine,
                        format, script, show_name, intro, instructions, session_id)
    if not a["topic"] and not (a["script"] and a["script"].get("segments")):
        return {"error": "topic is required (or provide script.segments)"}

    job_id = uuid.uuid4().hex[:10]
    _JOBS[job_id] = {"job_id": job_id, "status": "running", "stage": "queued",
                     "message": "queued", "pct": 0, "topic": a["topic"],
                     "started": now_iso(), "updated": now_iso()}
    while len(_JOBS) > _MAX_JOBS:
        _JOBS.pop(next(iter(_JOBS)))

    if wait:
        meta = await _run_pipeline(job_id, a)
        if meta.get("error"):
            return {"error": meta["error"], "job_id": job_id}
        return {**meta, "job_id": job_id}

    asyncio.create_task(_run_pipeline(job_id, a))
    return {"job_id": job_id, "status": "running",
            "hint": "poll podcast.status(job_id=...) — stages: gather→script→voice→stitch→done"}


@capability(
    "podcast.status",
    http_method="GET", http_path="/podcast/status", http_tags=["podcast"],
    memory="off", silent=True,
    description="Progress of a podcast.generate job. Input: job_id (str!). "
                "Output: {status: running|done|error, stage, message, pct, script?, episode?}.",
)
async def cap_podcast_status(job_id: str = "", trace_id=None) -> dict:
    j = _JOBS.get(job_id)
    if not j:
        return {"error": f"unknown job_id {job_id}"}
    return j


@capability(
    "podcast.list",
    http_method="GET", http_path="/podcast/list", http_tags=["podcast"],
    memory="off", silent=True,
    description="List generated podcast episodes (newest first). Input: limit (int). "
                "Output: {episodes:[{episode_id,title,duration_s,format,created,audio_url}], count}.",
)
async def cap_podcast_list(limit: int = 50, trace_id=None) -> dict:
    eps = []
    try:
        for fn in os.listdir(_EP_DIR):
            if fn.endswith(".json") and fn != "settings.json":
                try:
                    with open(os.path.join(_EP_DIR, fn), encoding="utf-8") as f:
                        m = json.load(f)
                    eps.append({k: m.get(k) for k in
                                ("episode_id", "title", "description", "topic", "style",
                                 "duration_s", "format", "size_bytes", "created", "audio_url")})
                except Exception:
                    continue
    except Exception as e:
        return {"error": str(e)}
    eps.sort(key=lambda e: e.get("created") or "", reverse=True)
    return {"episodes": eps[:max(1, limit)], "count": len(eps)}


@capability(
    "podcast.get",
    http_method="GET", http_path="/podcast/get", http_tags=["podcast"],
    memory="off", silent=True,
    description="Full detail of one episode incl. the script. Input: episode_id (str!). "
                "Output: episode metadata {title, segments, speakers, audio_url, ...}.",
)
async def cap_podcast_get(episode_id: str = "", trace_id=None) -> dict:
    p = _ep_paths(re.sub(r"[^a-f0-9]", "", episode_id or ""))
    try:
        with open(p["meta"], encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"error": f"unknown episode {episode_id}"}


@capability(
    "podcast.delete",
    http_method="POST", http_path="/podcast/delete", http_tags=["podcast"],
    memory="on",
    description="Delete a generated episode (audio + metadata). Input: episode_id (str!). "
                "Output: {ok, deleted}.",
)
async def cap_podcast_delete(episode_id: str = "", trace_id=None) -> dict:
    eid = re.sub(r"[^a-f0-9]", "", episode_id or "")
    if not eid:
        return {"error": "episode_id required"}
    p = _ep_paths(eid)
    deleted = []
    for k in ("meta", "wav", "mp3"):
        if os.path.exists(p[k]):
            try:
                os.remove(p[k])
                deleted.append(os.path.basename(p[k]))
            except Exception as e:
                return {"error": f"delete failed: {e}"}
    if not deleted:
        return {"error": f"unknown episode {episode_id}"}
    return {"ok": True, "deleted": deleted}


@capability(
    "podcast.settings.get",
    http_method="GET", http_path="/podcast/settings", http_tags=["podcast"],
    memory="off", silent=True,
    description="Get the persisted podcast defaults (speakers/voices, style, minutes, gap_ms, "
                "engine, format, show_name, intro). Output: the settings object + available styles.",
)
async def cap_podcast_settings_get(trace_id=None) -> dict:
    s = _load_settings()
    s["styles"] = list(STYLE_HINTS.keys())
    return s


@capability(
    "podcast.settings.set",
    http_method="POST", http_path="/podcast/settings/set", http_tags=["podcast"],
    memory="on",
    description="Persist podcast defaults used when podcast.generate args are omitted. "
                "Input (all optional): speakers (JSON list [{name,voice,speed,persona}]), "
                "style, minutes (float), gap_ms (int), engine, format (mp3_if_possible|wav), "
                "show_name, intro (bool). Output: {ok, settings}.",
)
async def cap_podcast_settings_set(speakers=None, style: str = None,
                                   minutes: float = None, gap_ms: int = None,
                                   engine: str = None, format: str = None,
                                   show_name: str = None, intro: bool = None,
                                   trace_id=None) -> dict:
    s = _load_settings()
    if speakers is not None:
        if isinstance(speakers, str):
            try:
                speakers = json.loads(speakers)
            except Exception:
                return {"error": "speakers must be a JSON list"}
        if not isinstance(speakers, list) or not speakers:
            return {"error": "speakers must be a non-empty list"}
        s["speakers"] = speakers[:6]
    if style is not None:     s["style"] = style
    if minutes is not None:   s["minutes"] = float(minutes)
    if gap_ms is not None:    s["gap_ms"] = int(gap_ms)
    if engine is not None:    s["engine"] = engine
    if format is not None:    s["format"] = format
    if show_name is not None: s["show_name"] = show_name
    if intro is not None:     s["intro"] = bool(intro)
    try:
        _save_settings(s)
    except Exception as e:
        return {"error": f"save failed: {e}"}
    return {"ok": True, "settings": s}


# ─────────────────────────────────────────────────────────────────────────────
# Raw audio route (binary — outside @capability like /mesh/firmware/bin)
# ─────────────────────────────────────────────────────────────────────────────

from fastapi.responses import FileResponse, JSONResponse  # noqa: E402


@APP.get("/podcast/audio/{episode_id}", include_in_schema=False)
async def _podcast_audio(episode_id: str):
    eid = re.sub(r"[^a-f0-9]", "", episode_id or "")
    p = _ep_paths(eid)
    if os.path.exists(p["mp3"]):
        return FileResponse(p["mp3"], media_type="audio/mpeg",
                            filename=f"podcast_{episode_id}.mp3")
    if os.path.exists(p["wav"]):
        return FileResponse(p["wav"], media_type="audio/wav",
                            filename=f"podcast_{episode_id}.wav")
    # Fall back to the object-store mirror (local library wiped / different host);
    # restore it to disk so the library re-discovers it.
    store = _obj_store()
    if store is not None and eid:
        from fastapi.responses import Response
        for fmt, ctype in (("mp3", "audio/mpeg"), ("wav", "audio/wav")):
            try:
                data = await asyncio.to_thread(store.get, _ep_blob_key(eid, fmt))
            except Exception:
                data = None
            if data:
                try:
                    with open(p[fmt], "wb") as f:
                        f.write(data)
                except Exception:
                    pass
                return Response(content=data, media_type=ctype)
    return JSONResponse({"error": "not found"}, status_code=404)
