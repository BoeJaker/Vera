"""
vera_capabilities.py  –  Vera capability library  v3
=====================================================
All capabilities register themselves on import.
Each capability may declare a `ui` block that the harness
renders as a built-in panel widget.

Start standalone:
    python vera_capabilities.py        (runs on :8000)

Or import into your own app:
    from Vera.Orchestration.config import cfg
from Vera.Orchestration.capability_orchestration import APP  # noqa
    import vera_capabilities           # registers all caps
    import uvicorn; uvicorn.run(APP, ...)
"""

import asyncio, base64, hashlib, json, logging, math, os, re, textwrap, time, uuid
from datetime import datetime, timezone
from typing import Optional, Any
from urllib.parse import urlparse

import httpx

from Vera.Orchestration.capability_orchestration import (
    APP,                          # noqa re-exported
    OLLAMA_INSTANCES, OLLAMA_MODEL,
    UI_PANELS, register_ui,       # panel registry lives in orchestrator
    capability, emit_event, emit_stream,
    now_iso, ollama_generate, pick_instance, schedule,
)

from Vera.Orchestration.config import cfg

log = logging.getLogger("vera.caps")


# Coerce timeout into an int, accepting formats like 10s, 60m, 1h, etc.
def parse_timeout(t: Any) -> int:
    if isinstance(t, (int, float)):
        return int(t)
    s = str(t).strip().lower()
    if not s:
        return _DEFAULT_TIMEOUT
    # Try to extract a number and unit
    import re
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])?$", s)
    if not m:
        return _DEFAULT_TIMEOUT  # fallback on unknown format
    num, unit = int(m.group(1)), m.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(num * multipliers.get(unit, 1))


# ─────────────────────────────────────────────────────────────────────────────
#  ██  GPU INFERENCE SERVER  (192.168.0.250:8765)
#
#  Actual server endpoints (gpu_inference_server.py):
#    POST /stt                   — Whisper transcription (multipart file)
#    POST /tts                   — Kokoro/Coqui TTS → WAV b64
#    POST /tts/stream            — streaming PCM audio
#    POST /imagine               — Stable Diffusion txt2img
#    GET  /tts/voices            — voice catalogue
#    GET  /sd/loras              — LoRA file list
#    GET  /health                — GPU server health
#    POST /chat/speak            — Ollama LLM + TTS fan-out
#    GET  /chat/text/{sid}       — SSE text token stream
#    POST /duplex/start          — create duplex session
#    POST /duplex/query          — submit query (text or audio)
#    POST /duplex/interrupt/{id} — interrupt current response
#    GET  /duplex/audio/{id}     — persistent PCM audio stream
#    GET  /duplex/text/{id}      — persistent SSE text stream
#    DELETE /duplex/session/{id} — close session
# ─────────────────────────────────────────────────────────────────────────────

GPU_INFER_URL = cfg.GPU_INFER_URL


@capability(
    "gpu.health",
    http_method="GET", http_path="/gpu/health", http_tags=["gpu", "obs"],
    memory="off",
    description="Health status of the GPU inference server. "
                "Output: {status, whisper, stable_diffusion, tts, tts_engine, sample_rate, cuda, gpu}. CUDA).",
)
async def gpu_health(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{GPU_INFER_URL}/health")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "status": "unreachable", "url": GPU_INFER_URL}


# ── STT ───────────────────────────────────────────────────────────────────────

@capability(
    "stt.transcribe",
    http_method="POST", http_path="/stt/transcribe", http_tags=["gpu", "stt"],
    memory="auto",
    description="Transcribe audio to text using Whisper on the GPU node. "
                "Input: audio_b64 (base64 WAV/WebM), language (optional, ISO code), task (transcribe|translate). "
                "Output: {text, language, duration_s}. "
                "Pass audio_b64 (base64 audio bytes), mime_type, optional language and translate flag.",
)
async def stt_transcribe(
    audio_b64: str,
    mime_type: str  = "audio/webm",
    language:  str  = "",
    translate: bool = False,
    trace_id=None,
):
    audio_bytes = base64.b64decode(audio_b64)
    files  = {"file": ("audio.webm", audio_bytes, mime_type)}
    data   = {}
    if language:  data["language"] = language
    if translate: data["task"]     = "translate"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{GPU_INFER_URL}/stt", files=files, data=data)
            r.raise_for_status()
            resp = r.json()
        return {
            "text":     resp.get("text", ""),
            "language": resp.get("language", ""),
        }
    except Exception as e:
        log.error("stt.transcribe: %s", e)
        return {"error": str(e), "text": ""}


# ── TTS ───────────────────────────────────────────────────────────────────────

@capability(
    "tts.synthesize",
    http_method="POST", http_path="/tts/synthesize", http_tags=["gpu", "tts"],
    memory="off",
    description="Synthesize text to speech on the GPU node. "
                "Input: text (str), voice (voice_id e.g. af_heart), speed (float 0.5-2.0), engine (kokoro|coqui). "
                "Output: {audio_b64, sample_rate, format}. "
                "Returns base64-encoded WAV audio and sample_rate.",
)
async def tts_synthesize(
    text:     str,
    voice:    str   = "af_heart",
    speed:    float = 1.0,
    engine:   str   = "",    # "kokoro" | "coqui" | "" = server default
    language: str   = "",
    trace_id=None,
):
    body: dict = {"text": text, "voice": voice, "speed": speed}
    if engine:   body["engine"]   = engine
    if language: body["language"] = language
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{GPU_INFER_URL}/tts", json=body)
            r.raise_for_status()
            data = r.json()
        return {
            "audio_b64":   data.get("audio_b64", ""),
            "mime_type":   "audio/wav",
            "voice":       voice,
            "sample_rate": data.get("sample_rate", 22050),
            "format":      data.get("format", "wav"),
        }
    except Exception as e:
        log.error("tts.synthesize: %s", e)
        return {"error": str(e), "audio_b64": ""}


@capability(
    "tts.voices",
    http_method="GET", http_path="/tts/voices", http_tags=["gpu", "tts"],
    memory="off",
    description="List available TTS voices from the GPU inference server. "
                "Output: {engine, voices: [{id, name, lang, gender}]}.",
)
async def list_tts_voices(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{GPU_INFER_URL}/tts/voices")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "voices": [
            {"id": "af_heart",    "name": "Heart",    "lang": "en-us", "gender": "F"},
            {"id": "af_bella",    "name": "Bella",    "lang": "en-us", "gender": "F"},
            {"id": "af_sarah",    "name": "Sarah",    "lang": "en-us", "gender": "F"},
            {"id": "am_adam",     "name": "Adam",     "lang": "en-us", "gender": "M"},
            {"id": "am_michael",  "name": "Michael",  "lang": "en-us", "gender": "M"},
            {"id": "bf_emma",     "name": "Emma",     "lang": "en-gb", "gender": "F"},
            {"id": "bf_isabella", "name": "Isabella", "lang": "en-gb", "gender": "F"},
            {"id": "bm_george",   "name": "George",   "lang": "en-gb", "gender": "M"},
            {"id": "bm_lewis",    "name": "Lewis",    "lang": "en-gb", "gender": "M"},
        ]}


# ── Stable Diffusion ──────────────────────────────────────────────────────────

@capability(
    "image.generate",
    http_method="POST", http_path="/image/generate", http_tags=["gpu", "sd", "image"],
    memory="on",
    description="Generate an image with Stable Diffusion on the GPU node. "
                "Input: prompt (str), negative_prompt (str), steps (int 10-50), guidance (float), "
                "width/height (int, multiples of 64), seed (int|-1), loras (list of {name,weight}). "
                "Output: {image_b64, format}. "
                "Returns base64-encoded PNG. Use loras as comma-separated 'name:weight' pairs.",
)
async def image_generate(
    prompt:          str,
    negative_prompt: str   = "blurry, low quality, distorted",
    width:           int   = 512,
    height:          int   = 512,
    steps:           int   = 20,
    guidance:        float = 7.5,
    seed:            int   = -1,
    loras:           str   = "",   # e.g. "add_detail:0.8,skin_texture:0.6"
    trace_id=None,
):
    # Parse loras string → list of {name, weight} dicts
    lora_list = []
    for part in loras.split(","):
        part = part.strip()
        if not part: continue
        if ":" in part:
            name, _, weight = part.partition(":")
            lora_list.append({"name": name.strip(), "weight": float(weight.strip() or 1.0)})
        else:
            lora_list.append({"name": part, "weight": 1.0})

    body = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width, "height": height,
        "steps": steps, "guidance": guidance,
        "loras": lora_list,
    }
    if seed >= 0: body["seed"] = seed

    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{GPU_INFER_URL}/imagine", json=body)
            r.raise_for_status()
            data = r.json()
        return {
            "image_b64": data.get("image_b64", ""),
            "mime_type": "image/png",
            "format":    data.get("format", "png"),
            "seed":      data.get("seed"),
            "steps":     steps,
            "width":     width,
            "height":    height,
        }
    except Exception as e:
        log.error("image.generate: %s", e)
        return {"error": str(e), "image_b64": ""}


@capability(
    "sd.loras",
    http_method="GET", http_path="/sd/loras", http_tags=["gpu", "sd"],
    memory="off",
    description="List available Stable Diffusion LoRA adapters on the GPU server. "
                "Output: {loras: [{name, filename, size_mb}], lora_dir}.",
)
async def list_loras(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{GPU_INFER_URL}/sd/loras")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e), "loras": []}


# ── Chat + Speak (LLM → TTS fan-out) ─────────────────────────────────────────

@capability(
    "gpu.chat_speak",
    http_method="POST", http_path="/gpu/chat/speak", http_tags=["gpu", "tts", "llm"],
    memory="on",
    description="Send a prompt to Ollama on the GPU server; response streams as PCM audio + SSE text. "
                "Input: prompt (str), model (str), voice (voice_id), speed (float), engine (str), session_id (str). "
                "Output: {url, text_url, body, note}. "
                "Returns session_id for GET /chat/text/{session_id}.",
)
async def gpu_chat_speak(
    prompt:     str,
    model:      str   = "llama3.2",
    voice:      str   = "af_heart",
    speed:      float = 1.0,
    engine:     str   = "",
    session_id: str   = "",
    trace_id=None,
):
    body: dict = {"prompt": prompt, "model": model, "voice": voice, "speed": speed}
    if engine:     body["engine"]     = engine
    if session_id: body["session_id"] = session_id
    try:
        # This endpoint returns streaming PCM — we just start it and return the session_id
        # The caller should separately connect to /chat/text/{session_id} for text tokens
        # and stream audio from /chat/speak directly
        async with httpx.AsyncClient(timeout=10) as c:
            # HEAD request to validate connectivity first
            h = await c.head(f"{GPU_INFER_URL}/health")
        return {
            "url":        f"{GPU_INFER_URL}/chat/speak",
            "text_url":   f"{GPU_INFER_URL}/chat/text/{{session_id}}",
            "body":       body,
            "note":       "POST body to url for audio stream; GET text_url for SSE tokens",
        }
    except Exception as e:
        return {"error": str(e)}


# ── Duplex voice session ──────────────────────────────────────────────────────

@capability(
    "gpu.duplex_start",
    http_method="POST", http_path="/gpu/duplex/start", http_tags=["gpu", "tts", "voice"],
    memory="off",
    description="Create a persistent duplex voice session on the GPU server. "
                "Output: {session_id, audio_url, text_url, query_url}. "
                "Returns session_id for use with gpu.duplex_query and stream endpoints.",
)
async def gpu_duplex_start(trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{GPU_INFER_URL}/duplex/start")
            r.raise_for_status()
            data = r.json()
        sid = data.get("session_id", "")
        return {
            "session_id": sid,
            "audio_url":  f"{GPU_INFER_URL}/duplex/audio/{sid}",
            "text_url":   f"{GPU_INFER_URL}/duplex/text/{sid}",
            "query_url":  f"{GPU_INFER_URL}/duplex/query",
        }
    except Exception as e:
        return {"error": str(e)}


@capability(
    "gpu.duplex_query",
    http_method="POST", http_path="/gpu/duplex/query", http_tags=["gpu", "tts", "voice"],
    memory="on",
    description="Submit text or audio to a live duplex voice session. "
                "Input: session_id (str), text (str), audio_b64 (base64 WebM — triggers Whisper STT), "
                "model (str), voice (voice_id), speed (float). "
                "Output: {session_id, query, status}. "
                "Interrupts any in-progress response. audio_b64 triggers Whisper STT first.",
)
async def gpu_duplex_query(
    session_id: str,
    text:       str   = "",
    audio_b64:  str   = "",
    model:      str   = "llama3.2",
    voice:      str   = "af_heart",
    speed:      float = 1.0,
    trace_id=None,
):
    body: dict = {"session_id": session_id, "model": model, "voice": voice, "speed": speed}
    if text:      body["text"]      = text
    if audio_b64: body["audio_b64"] = audio_b64
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{GPU_INFER_URL}/duplex/query", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


@capability(
    "gpu.duplex_interrupt",
    http_method="POST", http_path="/gpu/duplex/interrupt", http_tags=["gpu", "tts", "voice"],
    memory="off",
    description="Immediately interrupt the current TTS response in a duplex session. "
                "Input: session_id (str). Output: {status}.",
)
async def gpu_duplex_interrupt(session_id: str, trace_id=None):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{GPU_INFER_URL}/duplex/interrupt/{session_id}")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}





register_ui(
    "whisper-stt",
    "Speech → Text",
    "mic",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <button id="wRecBtn" class="btn primary" onclick="whisperToggle(,
    ui_caps=['stt.transcribe'])">⏺ Record</button>
    <button class="btn" onclick="whisperUpload()">📂 Upload File</button>
    <input type="file" id="wFile" accept="audio/*,video/*" style="display:none" onchange="whisperFromFile(this)">
    <select id="wLang" style="width:120px">
      <option value="">Auto-detect</option>
      <option value="en">English</option>
      <option value="fr">French</option>
      <option value="de">German</option>
      <option value="es">Spanish</option>
      <option value="ja">Japanese</option>
      <option value="zh">Chinese</option>
    </select>
    <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--dim2)">
      <input type="checkbox" id="wTranslate"> Translate to EN
    </label>
  </div>
  <div id="wViz" style="height:40px;background:var(--bg0);border:1px solid var(--border);border-radius:4px;display:flex;align-items:center;justify-content:center">
    <span style="color:var(--dim);font-size:11px;font-family:var(--mono)">idle</span>
  </div>
  <div id="wStatus" class="status-bar"></div>
  <div style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">Transcript</div>
  <textarea id="wResult" style="min-height:120px;font-size:12px" placeholder="Transcript will appear here…" readonly></textarea>
  <div style="display:flex;gap:8px">
    <button class="btn sm" onclick="navigator.clipboard.writeText(document.getElementById('wResult').value)">Copy</button>
    <button class="btn sm" onclick="document.getElementById('wResult').value=''">Clear</button>
    <button class="btn sm teal" onclick="whisperSendToLLM()">→ Send to LLM</button>
  </div>
</div>
""",
    """
(function(){
  let recorder=null, chunks=[], stream=null, recording=false;
  const vizEl = ()=>document.getElementById('wViz');
  const statEl= ()=>document.getElementById('wStatus');

  window.whisperToggle = async function() {
    if (!recording) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({audio:true});
        recorder = new MediaRecorder(stream);
        chunks = [];
        recorder.ondataavailable = e=>{ if(e.data.size>0) chunks.push(e.data); };
        recorder.onstop = whisperProcess;
        recorder.start(200);
        recording = true;
        document.getElementById('wRecBtn').textContent='⏹ Stop';
        document.getElementById('wRecBtn').classList.add('danger');
        vizEl().innerHTML='<div style="display:flex;gap:3px;align-items:center" id="wBars">'
          +Array(16).fill(0).map((_,i)=>`<div style="width:4px;height:20px;background:var(--err);border-radius:2px;animation:wbar .6s ${i*.04}s infinite alternate"></div>`).join('')
          +'</div>';
        if(!document.getElementById('wBarStyle')){
          const s=document.createElement('style');
          s.id='wBarStyle';
          s.textContent='@keyframes wbar{0%{height:4px}100%{height:32px}}';
          document.head.appendChild(s);
        }
        statEl().textContent='Recording…';
        statEl().className='status-bar warn';
      } catch(e){ statEl().textContent='Mic error: '+e.message; statEl().className='status-bar err'; }
    } else {
      recorder.stop();
      stream.getTracks().forEach(t=>t.stop());
      recording=false;
      document.getElementById('wRecBtn').textContent='⏺ Record';
      document.getElementById('wRecBtn').classList.remove('danger');
      vizEl().innerHTML='<span style="color:var(--dim);font-size:11px;font-family:var(--mono)">processing…</span>';
      statEl().textContent='Processing…';
      statEl().className='status-bar';
    }
  };

  window.whisperUpload = function(){ document.getElementById('wFile').click(); };

  window.whisperFromFile = async function(inp) {
    const file = inp.files[0]; if(!file) return;
    statEl().textContent='Reading file…'; statEl().className='status-bar';
    const ab = await file.arrayBuffer();
    const b64 = btoa(String.fromCharCode(...new Uint8Array(ab)));
    await whisperSubmit(b64, file.type||'audio/webm');
  };

  async function whisperProcess() {
    const blob = new Blob(chunks, {type:'audio/webm'});
    const ab   = await blob.arrayBuffer();
    const b64  = btoa(String.fromCharCode(...new Uint8Array(ab)));
    await whisperSubmit(b64, 'audio/webm');
  }

  async function whisperSubmit(b64, mime) {
    statEl().textContent='Transcribing…'; statEl().className='status-bar';
    const lang = document.getElementById('wLang').value;
    const trans= document.getElementById('wTranslate').checked;
    try {
      const res = await callCapRaw('stt.transcribe', {
        audio_b64: b64, mime_type: mime,
        language: lang, translate: trans
      });
      const text = res?.text||res?.content?.text||'';
      document.getElementById('wResult').value = text;
      statEl().textContent = `✓ Done${res?.language?' ['+res.language+']':''}`+(res?.duration?` · ${res.duration.toFixed(1)}s`:'');
      statEl().className='status-bar ok';
    } catch(e){
      statEl().textContent='Error: '+e.message; statEl().className='status-bar err';
    }
    vizEl().innerHTML='<span style="color:var(--dim);font-size:11px;font-family:var(--mono)">idle</span>';
  }

  window.whisperSendToLLM = function() {
    const text = document.getElementById('wResult').value;
    if(!text) return;
    const el = document.getElementById('llmPrompt');
    if(el){ el.value=text; switchTab('dashboard'); }
  };
})();
""",
    ui_caps=['stt.transcribe'],
    mode="inject",
    tab_order=10,
)


register_ui(
    "stable-diffusion",
    "Image Gen",
    "art",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <div class="g2">
    <div style="display:flex;flex-direction:column;gap:9px">
      <div>
        <div style="font-size:10px;color:var(--dim,
    ui_caps=['image.generate', 'sd.loras']);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px">Prompt</div>
        <textarea id="sdPrompt" style="height:80px;font-size:12px" placeholder="A cinematic photo of…"></textarea>
      </div>
      <div>
        <div style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px">Negative Prompt</div>
        <textarea id="sdNeg" style="height:50px;font-size:11px">blurry, low quality, distorted, watermark</textarea>
      </div>
      <div class="g2">
        <div class="row"><label>W</label><input id="sdW" type="number" value="512" step="64" min="256" max="1024" style="flex:1"></div>
        <div class="row"><label>H</label><input id="sdH" type="number" value="512" step="64" min="256" max="1024" style="flex:1"></div>
        <div class="row"><label>Steps</label><input id="sdSteps" type="number" value="20" min="5" max="50" style="flex:1"></div>
        <div class="row"><label>CFG</label><input id="sdCfg" type="number" value="7.5" step="0.5" min="1" max="20" style="flex:1"></div>
      </div>
      <div class="row"><label>Seed</label><input id="sdSeed" value="-1" style="flex:1"><button class="btn sm" onclick="document.getElementById('sdSeed').value=Math.floor(Math.random()*99999999)">🎲</button></div>
      <div class="row"><label>LoRAs</label><input id="sdLoras" placeholder="name:0.8,other:0.6" style="flex:1"><button class="btn sm" onclick="sdLoadLoras()">↻</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn primary" onclick="sdGenerate()">🎨 Generate</button>
        <button class="btn sm" onclick="sdDownload()">⬇</button>
      </div>
    </div>
    <div>
      <div id="sdImgWrap" style="background:var(--bg0);border:1px solid var(--border);border-radius:6px;min-height:260px;display:flex;align-items:center;justify-content:center;overflow:hidden">
        <span style="color:var(--dim);font-size:11px">Image will appear here</span>
      </div>
      <div id="sdStatus" class="status-bar" style="margin-top:7px"></div>
      <div id="sdSeedOut" style="font-family:var(--mono);font-size:10px;color:var(--dim2);margin-top:4px"></div>
    </div>
  </div>
</div>
""",
    r"""
(function(){
  let lastB64='';

  window.sdGenerate = async function() {
    const prompt = document.getElementById('sdPrompt').value.trim();
    if (!prompt) return;
    sdSt('⟳ Generating…','');
    sdImg('');
    const res = await callCapRaw('image.generate',{
      prompt, negative_prompt: document.getElementById('sdNeg').value,
      width:  +document.getElementById('sdW').value,
      height: +document.getElementById('sdH').value,
      steps:  +document.getElementById('sdSteps').value,
      guidance: +document.getElementById('sdCfg').value,
      seed:   +document.getElementById('sdSeed').value,
      loras:  document.getElementById('sdLoras').value,
    });
    if (res.error) { sdSt('✗ '+res.error,'err'); return; }
    lastB64 = res.image_b64;
    sdImg(lastB64);
    sdSt('✓ Done · seed:'+res.seed,'ok');
    document.getElementById('sdSeedOut').textContent = 'seed: '+res.seed;
  };

  window.sdDownload = function() {
    if (!lastB64) return;
    const a=document.createElement('a'); a.download='vera_sd.png';
    a.href='data:image/png;base64,'+lastB64; a.click();
  };

  window.sdLoadLoras = async function() {
    try {
      const r=await fetch(window._veraBase+'/sd/loras');
      const d=await r.json();
      // Show available loras as hint text
      const inp=document.getElementById('sdLoras');
      const names=(d.loras||[]).map(l=>l.name||l).join(', ');
      inp.placeholder = names ? 'Available: '+names.slice(0,40) : 'name:weight,...';
    } catch(e){ console.warn('loras',e); }
  };

  function sdSt(msg,cls){ const el=document.getElementById('sdStatus'); el.textContent=msg; el.className='status-bar'+(cls?' '+cls:''); }
  function sdImg(b64){ const w=document.getElementById('sdImgWrap'); w.innerHTML=b64 ? `<img src="data:image/png;base64,${b64}" style="max-width:100%;max-height:380px;object-fit:contain">` : '<span style="color:var(--dim);font-size:11px">Image will appear here</span>'; }
})();
""",
    ui_caps=['image.generate', 'sd.loras'],
    mode="inject",
    tab_order=20,
)


register_ui(
    "kokoro-tts",
    "Text to Speech",
    "",
    """
<div style="display:flex;flex-direction:column;gap:12px">
  <textarea id="ttsText" style="min-height:100px;font-size:12px" placeholder="Enter text to synthesize…"></textarea>
  <div class="g2">
    <div>
      <div class="row"><label>Voice</label>
        <select id="ttsVoice" style="flex:1">
          <option value="af_heart">af_heart (F warm)</option>
          <option value="af_bella">af_bella (F soft)</option>
          <option value="af_sarah">af_sarah (F clear)</option>
          <option value="am_adam">am_adam (M deep)</option>
          <option value="am_michael">am_michael (M natural)</option>
          <option value="bf_emma">bf_emma (F British)</option>
          <option value="bf_isabella">bf_isabella (F British warm)</option>
          <option value="bm_george">bm_george (M British)</option>
          <option value="bm_lewis">bm_lewis (M British deep)</option>
        </select>
      </div>
      <div class="row">
        <label>Speed</label>
        <input type="range" id="ttsSpeed" min="0.5" max="2" step="0.05" value="1" style="flex:1;padding:0" oninput="document.getElementById('ttsSpeedVal').textContent=this.value">
        <span id="ttsSpeedVal" style="min-width:28px;text-align:right;font-family:var(--mono);font-size:11px;color:var(--acc)">1</span>
      </div>
    </div>
    <div>
      <div style="display:flex;gap:8px;margin-top:4px;flex-wrap:wrap">
        <button class="btn primary" onclick="ttsSynthesize()">🔊 Synthesize</button>
        <button class="btn sm" onclick="ttsLoadVoices()">↻ Voices</button>
        <button class="btn sm" onclick="ttsDownload()">⬇ WAV</button>
      </div>
      <div id="ttsStatus" class="status-bar" style="margin-top:8px"></div>
    </div>
  </div>
  <div id="ttsPlayerWrap" style="display:none">
    <audio id="ttsPlayer" controls style="width:100%;margin-top:4px"></audio>
  </div>
</div>
""",
    """
(function(){
  let lastAudioB64='', lastMime='audio/wav';

  window.ttsSynthesize = async function() {
    const text  = document.getElementById('ttsText').value.trim();
    if (!text) return;
    const voice = document.getElementById('ttsVoice').value;
    const speed = parseFloat(document.getElementById('ttsSpeed').value);
    const st    = document.getElementById('ttsStatus');
    st.textContent='Synthesizing…'; st.className='status-bar';
    document.getElementById('ttsPlayerWrap').style.display='none';
    try {
      const res = await callCapRaw('tts.synthesize',{text, voice, speed});
      if (res.error) throw new Error(res.error);
      if (!res.audio_b64) throw new Error('Server returned no audio data');
      lastAudioB64 = res.audio_b64;
      lastMime     = res.mime_type || 'audio/wav';
      const bytes  = Uint8Array.from(atob(lastAudioB64), c=>c.charCodeAt(0));
      const blob   = new Blob([bytes],{type:lastMime});
      const url    = URL.createObjectURL(blob);
      const player = document.getElementById('ttsPlayer');
      player.src   = url;
      document.getElementById('ttsPlayerWrap').style.display='block';
      player.play();
      st.textContent=`✓ Done · voice:${voice}`;
      st.className='status-bar ok';
    } catch(e){
      st.textContent='Error: '+e.message; st.className='status-bar err';
    }
  };

  window.ttsDownload = function() {
    if (!lastAudioB64) return;
    const bytes = Uint8Array.from(atob(lastAudioB64), c=>c.charCodeAt(0));
    const blob  = new Blob([bytes],{type:lastMime});
    const a     = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'vera_tts.wav';
    a.click();
  };

  window.ttsLoadVoices = async function() {
    try {
      const res  = await fetch(window._veraBase+'/tts/voices');
      const data = await res.json();
      const sel  = document.getElementById('ttsVoice');
      if (data.voices && data.voices.length) {
        sel.innerHTML = data.voices.map(v=>`<option value="${v}">${v}</option>`).join('');
      }
    } catch(e){ console.warn('voice list fetch failed', e); }
  };
})();
""",
    ui_caps=['tts.synthesize'],
    mode="inject",
    tab_order=30,
)

# ─────────────────────────────────────────────────────────────────────────────
#  ██  LLM GROUP
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "llm.generate",
    http_method="POST", http_path="/llm/generate", http_tags=["llm", "generate"],
    mode="distributed", streams=["tokens"],
    memory="on",
    description="Generate text via Ollama with cluster routing and token streaming. "
                "Input: prompt (str!), model (str), system (str), instance_id (str), prefer_gpu (bool). "
                "Output: {text, model, instance, has_gpu, tokens}. Streams: tokens stream.",
)
async def llm_generate(
    prompt:      str,
    model:       str   = None,
    system:      str   = "",
    instance_id: str   = None,
    prefer_gpu:  bool  = False,
    trace_id=None,
):
    tokens_collected = []
    async def _tok(t):
        tokens_collected.append(t)
        await emit_stream("tokens", trace_id, {"token": t}, "llm.generate")

    text   = await ollama_generate(prompt, system=system, model=model,
                                   instance_id=instance_id or None,
                                   prefer_gpu=prefer_gpu, stream_cb=_tok)
    chosen = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id or None, model=model)
    inst   = OLLAMA_INSTANCES.get(chosen or "", {})
    return {"text": text, "model": model or OLLAMA_MODEL,
            "instance": chosen, "instance_url": inst.get("url"),
            "has_gpu": inst.get("has_gpu", False), "tokens": len(tokens_collected)}


@capability("llm.summarize",
    http_method="POST", http_path="/llm/summarize", http_tags=["llm", "text"],
    memory="on",
    description="Summarise text. "
                "Input: text (str!), max_words (int, default 150), style (concise|bullet|executive), instance_id (str), prefer_gpu (bool). "
                "Output: {summary, style, src_chars}.")
async def llm_summarize(
    text: str, max_words: int = 150, style: str = "concise",
    instance_id: str = None, prefer_gpu: bool = None, trace_id=None,
):
    if prefer_gpu is None: prefer_gpu = len(text) > 1000
    styles = {"concise":"Write a concise summary.","bullet":"Bullet-point summary (max 7).","executive":"One-paragraph executive summary."}
    system = f"You are a summarisation assistant. {styles.get(style,styles['concise'])} Target ≤{max_words} words. Reply with only the summary."
    out = await ollama_generate(f"Summarise:\n\n{text}", system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    return {"summary": out.strip(), "style": style, "src_chars": len(text)}


@capability("llm.analyze",
    http_method="POST", http_path="/llm/analyze", http_tags=["llm", "analysis"],
    memory="on",
    description="Analyse text for sentiment, topics, entities and readability. "
                "Input: text (str!), aspects (str, default sentiment,topics,entities,readability), model (str), instance_id (str), prefer_gpu (bool), system (str). "
                "Output: {analysis object as JSON}.")
async def llm_analyze(
    text: str, aspects: str = "sentiment,topics,entities,readability",
    instance_id: str = None, prefer_gpu: bool = True, trace_id=None,
):
    system = ('You are an expert text analyst. Return ONLY valid JSON: '
              '{"sentiment":"positive|negative|neutral|mixed","sentiment_score":0.0,'
              '"topics":["..."],"entities":[{"text":"...","type":"person|org|place|date"}],'
              '"readability":"simple|intermediate|advanced","key_phrases":["..."]}')
    raw = await ollama_generate(text, system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: result = json.loads(raw)
    except: result = {"raw": raw, "parse_error": True}
    result.update(char_count=len(text), word_count=len(text.split()))
    return result


@capability("llm.code_review",
    http_method="POST", http_path="/llm/code_review", http_tags=["llm", "code"],
    memory="on",
    description="Review code for bugs, security issues, style and performance. "
                "Input: code (str!), language (str), focus (str, default all), severity (str, default all), model (str), prefer_gpu (bool). "
                "Output: {issues:[{severity,category,line,message,fix}], summary}.")
async def llm_code_review(
    code: str, language: str = "python", focus: str = "bugs,security,style,performance",
    instance_id: str = None, prefer_gpu: bool = True, trace_id=None,
):
    system = (f'You are a senior {language} engineer. Focus on: {focus}. '
              'Return JSON: {"issues":[{"severity":"critical|high|medium|low","category":"...","message":"...","suggestion":"..."}],"overall_score":0-10,"summary":"..."}')
    raw = await ollama_generate(f"Review this {language} code:\n\n```{language}\n{code}\n```",
                                system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return json.loads(raw)
    except: return {"issues": [], "summary": raw, "parse_error": True}


@capability("llm.translate",
    http_method="POST", http_path="/llm/translate", http_tags=["llm", "text"],
    memory="on",
    description="Translate text to a target language. "
                "Input: text (str!), target_lang (str!, e.g. 'French'), source_lang (str), model (str), instance_id (str), prefer_gpu (bool). "
                "Output: {translated, source_lang, target_lang, original_len}.")
async def llm_translate(
    text: str, target_lang: str = "English", source_lang: str = "auto",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    src = f" from {source_lang}" if source_lang != "auto" else ""
    system = f"Translate the text{src} to {target_lang}. Reply with only the translated text."
    out = await ollama_generate(text, system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    return {"translated": out.strip(), "source_lang": source_lang, "target_lang": target_lang}


@capability("llm.classify",
    http_method="POST", http_path="/llm/classify", http_tags=["llm", "analysis"],
    memory="auto",
    description="Classify text into one or more of a provided category list. "
                "Input: text (str!), categories (str!, comma-separated), multi_label (bool), model (str), instance_id (str), prefer_gpu (bool), system (str). "
                "Output: {label, confidence, labels: [{category, confidence}]}.")
async def llm_classify(
    text: str, categories: str = "positive,negative,neutral", multi_label: bool = False,
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    cats = [c.strip() for c in categories.split(",")]
    system = f'Classify into {"one or more" if multi_label else "exactly one"} of {cats}. Return ONLY JSON: {{"label":"..."}} or {{"labels":[...]}}'
    raw = await ollama_generate(text, system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return {**json.loads(raw), "categories": cats}
    except: return {"raw": raw, "parse_error": True}


@capability("llm.explain",
    http_method="POST", http_path="/llm/explain", http_tags=["llm", "text"],
    memory="on",
    description="Explain a concept, error message, or code snippet in plain language. "
                "Input: topic (str!), level (beginner|intermediate|expert), model (str), instance_id (str), prefer_gpu (bool). "
                "Output: {explanation, level, topic}.")
async def llm_explain(
    content: str, level: str = "intermediate", format: str = "prose",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    fmts = {"prose":"Clear prose.","bullet":"Bullet points.","eli5":"Explain simply to a beginner."}
    system = f"You are a patient expert teacher. Target: {level}. {fmts.get(format,'Clear prose.')} Be accurate."
    out = await ollama_generate(f"Explain:\n\n{content}", system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    return {"explanation": out.strip(), "level": level, "format": format}


@capability("llm.brainstorm",
    http_method="POST", http_path="/llm/brainstorm", http_tags=["llm", "creative"],
    memory="on",
    description="Brainstorm ideas on a topic. "
                "Input: topic (str!), count (int, default 5), style (diverse|practical|creative|contrarian), model (str), prefer_gpu (bool). "
                "Output: {ideas: [str], topic, style}.")
async def llm_brainstorm(
    topic: str, count: int = 8, style: str = "diverse",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    styles = {"diverse":"Diverse ideas across angles.","practical":"Practical actionable ideas.",
              "creative":"Imaginative unconventional ideas.","contrarian":"Challenge assumptions."}
    system = (f"Brainstorming assistant. {styles.get(style,styles['diverse'])} "
              'Return ONLY JSON: {"ideas":[{"title":"...","description":"...","rationale":"..."}]}')
    raw = await ollama_generate(f"Brainstorm {count} ideas for: {topic}", system=system,
                                json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return {**json.loads(raw), "topic": topic, "style": style}
    except: return {"ideas": [], "raw": raw, "parse_error": True}


@capability("llm.rewrite",
    http_method="POST", http_path="/llm/rewrite", http_tags=["llm", "text"],
    memory="on",
    description="Rewrite text in a specified tone. "
                "Input: text (str!), tone (professional|casual|formal|friendly|concise|assertive), model (str), prefer_gpu (bool). "
                "Output: {rewritten, tone, original_len, new_len}.")
async def llm_rewrite(
    text: str, tone: str = "professional", target_len: str = "same",
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    system = f"Rewrite the text to be {tone} in tone. Target length: {target_len}. Reply with only the rewritten text."
    out = await ollama_generate(text, system=system, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    return {"rewritten": out.strip(), "tone": tone, "src_words": len(text.split()), "out_words": len(out.split())}


@capability("llm.qa",
    http_method="POST", http_path="/llm/qa", http_tags=["llm", "search"],
    memory="on",
    description="Answer a question using provided context text (RAG-style). "
                "Input: question (str!), context (str!), model (str), prefer_gpu (bool). "
                "Output: {answer, confidence, question}.")
async def llm_qa(
    question: str, context: str,
    instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    system = ('Answer only from context. If not found, say so. '
              'Return JSON: {"answer":"...","confidence":"high|medium|low","quote":"..."}')
    raw = await ollama_generate(f"Context:\n{context}\n\nQuestion: {question}",
                                system=system, json_mode=True, instance_id=instance_id or None, prefer_gpu=prefer_gpu)
    try: return {**json.loads(raw), "question": question}
    except: return {"answer": raw, "question": question, "parse_error": True}


@capability("llm.plan",
    http_method="POST", http_path="/llm/plan", http_tags=["llm", "dag"],
    memory="on",
    description="Produce a Vera DAG execution plan for a natural-language goal. "
                "Uses the dag-planner agent via the CapabilityIndex. "
                "Input: goal (str!), available_caps (list, optional — limits which caps are considered). "
                "Output: {dag, initial_state, rationale, warnings}.")
async def llm_plan_cap(goal: str, prefer_gpu: bool = True, trace_id=None):
    from Vera.Orchestration.capability_orchestration import plan_dag
    return await plan_dag(goal)


# ─────────────────────────────────────────────────────────────────────────────
#  ██  TEXT
# ─────────────────────────────────────────────────────────────────────────────

@capability("text.stats",
    http_method="POST", http_path="/text/stats", http_tags=["text"],
    memory="off",
    description="Count chars, words, sentences and paragraphs in text. "
                "Input: text (str!). Output: {chars, words, sentences, paragraphs, avg_word_len}.")
async def text_stats(text: str, trace_id=None):
    words = text.split(); sents = re.split(r'(?<=[.!?])\s+', text.strip()); paras = [p for p in text.split("\n\n") if p.strip()]
    return {"chars":len(text),"words":len(words),"sentences":len([s for s in sents if s]),"paragraphs":len(paras),
            "avg_word_len":round(sum(len(w) for w in words)/max(len(words),1),2)}

@capability("text.find_replace",
    http_method="POST", http_path="/text/find_replace", http_tags=["text"],
    memory="off",
    description="Find and replace within text, with optional regex support. "
                "Input: text (str!), find (str!), replace (str), use_regex (bool). "
                "Output: {result, replacements_made}.")
async def text_find_replace(text: str, find: str, replace: str = "", regex: bool = False, trace_id=None):
    try:
        if regex: new,n = re.sub(find,replace,text), len(re.findall(find,text))
        else: n,new = text.count(find),text.replace(find,replace)
        return {"result":new,"replacements":n}
    except re.error as e: return {"error":str(e),"result":text}

@capability("text.extract_urls",
    http_method="POST", http_path="/text/extract_urls", http_tags=["text"],
    memory="off",
    description="Extract all URLs from text. "
                "Input: text (str!). Output: {urls: [str], count}.")
async def text_extract_urls(text: str, trace_id=None):
    urls = re.findall(r'https?://[^\s<>"\'{}|\\^`\[\]]+', text)
    return {"urls":[{"url":u,"domain":urlparse(u).netloc} for u in urls],"count":len(urls)}

@capability("text.hash",
    http_method="POST", http_path="/text/hash", http_tags=["text"],
    memory="off",
    description="Hash text using a cryptographic algorithm. "
                "Input: text (str!), algorithm (md5|sha1|sha256|sha512, default sha256). "
                "Output: {hash, algorithm, input_len}.")
async def text_hash(text: str, algorithm: str = "sha256", trace_id=None):
    algos={"md5":hashlib.md5,"sha1":hashlib.sha1,"sha256":hashlib.sha256,"sha512":hashlib.sha512}
    fn=algos.get(algorithm)
    if not fn: return {"error":f"Unknown: {algorithm}","supported":list(algos)}
    return {"hash":fn(text.encode()).hexdigest(),"algorithm":algorithm}

@capability("text.split_chunks",
    http_method="POST", http_path="/text/split_chunks", http_tags=["text"],
    memory="off",
    description="Split text into overlapping chunks for embedding/pipeline use. "
                "Input: text (str!), chunk_size (int, default 500 chars), overlap (int, default 50). "
                "Output: {chunks: [str], count, chunk_size, overlap}.")
async def text_split_chunks(text: str, chunk_size: int = 800, overlap: int = 100, trace_id=None):
    chunks,i=[],0
    while i<len(text):
        c=text[i:i+chunk_size]; chunks.append({"index":len(chunks),"text":c,"start":i,"end":i+len(c)}); i+=chunk_size-overlap
    return {"chunks":chunks,"count":len(chunks)}


# ─────────────────────────────────────────────────────────────────────────────
#  ██  DATA / MATH / HTTP / SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

@capability("data.json_validate",
    http_method="POST", http_path="/data/json_validate", http_tags=["data"],
    memory="off",
    description="Validate a JSON string and report parse errors. "
                "Input: json_str (str!). Output: {valid, error, line, column}.")
async def data_json_validate(json_str: str, trace_id=None):
    try:
        p=json.loads(json_str)
        return {"valid":True,"type":type(p).__name__,"length":len(p) if isinstance(p,(dict,list)) else None}
    except json.JSONDecodeError as e: return {"valid":False,"error":str(e),"line":e.lineno}

@capability("data.json_flatten",
    http_method="POST", http_path="/data/json_flatten", http_tags=["data"],
    memory="off",
    description="Flatten a nested JSON object to dot-notation keys. "
                "Input: json_str (str!), separator (str, default '.'). "
                "Output: {flattened: {key: value, ...}, key_count}.")
async def data_json_flatten(json_str: str, separator: str = ".", trace_id=None):
    try: obj=json.loads(json_str)
    except json.JSONDecodeError as e: return {"error":str(e)}
    def flat(o,p=""):
        r={}
        if isinstance(o,dict):
            for k,v in o.items(): r.update(flat(v,f"{p}{separator}{k}" if p else k))
        elif isinstance(o,list):
            for i,v in enumerate(o): r.update(flat(v,f"{p}{separator}{i}" if p else str(i)))
        else: r[p]=o
        return r
    f=flat(obj); return {"flat":f,"keys":len(f)}

@capability("math.compute",
    http_method="POST", http_path="/math/compute", http_tags=["math"],
    memory="off",
    description="Safely evaluate a math expression using Python math functions. "
                "Input: expression (str!, e.g. 'sqrt(16) + sin(pi/2)'). "
                "Output: {result, expression}.")
async def math_compute(expression: str, trace_id=None):
    safe={k:getattr(math,k) for k in dir(math) if not k.startswith("_")}
    stripped=re.sub(r'[a-zA-Z_]+','',expression)
    if any(c not in set("0123456789+-*/()., eE%") for c in stripped):
        return {"error":"Disallowed characters","expression":expression}
    try: r=eval(expression,{"__builtins__":{}},safe); return {"expression":expression,"result":r,"type":type(r).__name__}
    except Exception as e: return {"error":str(e),"expression":expression}

@capability("math.stats",
    http_method="POST", http_path="/math/stats", http_tags=["math"],
    memory="off",
    description="Descriptive statistics for a list of numbers. "
                "Input: numbers (str!, comma-separated, e.g. '1,2,3,4,5'). "
                "Output: {mean, median, mode, std, variance, min, max, count, sum}.")
async def math_stats(numbers: str, trace_id=None):
    try: vals=[float(x.strip()) for x in numbers.split(",") if x.strip()]
    except ValueError as e: return {"error":str(e)}
    if not vals: return {"error":"No numbers"}
    n=len(vals); m=sum(vals)/n; srt=sorted(vals); mid=n//2
    med=(srt[mid-1]+srt[mid])/2 if n%2==0 else srt[mid]
    var=sum((x-m)**2 for x in vals)/n
    return {"count":n,"mean":round(m,6),"median":round(med,6),"min":min(vals),"max":max(vals),"std_dev":round(math.sqrt(var),6)}

@capability("http.get",
    http_method="POST", http_path="/http/get", http_tags=["http", "web"],
    memory="auto",
    description="HTTP GET request to an external URL. "
                "Input: url (str!), headers (dict). "
                "Output: {status, headers, body, content_type, url, elapsed_ms}.")
async def http_get(url: str, timeout: int = 15, trace_id=None):
    timeout = parse_timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout,follow_redirects=True) as c:
            t0=time.monotonic(); r=await c.get(url); ms=round((time.monotonic()-t0)*1000)
        return {"url":str(r.url),"status":r.status_code,"ok":r.is_success,"latency_ms":ms,
                "content_type":r.headers.get("content-type",""),"body":r.text[:65536]}
    except Exception as e: return {"url":url,"error":str(e),"ok":False}

@capability("http.post",
    http_method="POST", http_path="/http/post", http_tags=["http", "web"],
    memory="auto",
    description="HTTP POST request with JSON payload to an external URL. "
                "Input: url (str!), payload (dict!), headers (dict). "
                "Output: {status, body, content_type, elapsed_ms}.")
async def http_post(url: str, payload: str = "{}", timeout: int = 15, trace_id=None):
    timeout = parse_timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout,follow_redirects=True) as c:
            r=await c.post(url,json=json.loads(payload))
        return {"url":str(r.url),"status":r.status_code,"ok":r.is_success,"body":r.text[:32768]}
    except Exception as e: return {"url":url,"error":str(e),"ok":False}

@capability("system.timestamp",
    http_method="GET", http_path="/system/timestamp", http_tags=["system", "util"],
    memory="off",
    description="Return current timestamps in multiple formats. "
                "Output: {iso, unix, unix_ms, date, time, timezone}.")
async def system_timestamp(trace_id=None):
    now=datetime.now(timezone.utc)
    return {"utc":now.isoformat(),"unix":int(now.timestamp()),"date":now.strftime("%Y-%m-%d"),
            "time":now.strftime("%H:%M:%S"),"day":now.strftime("%A")}

@capability("system.ping",
    http_method="POST", http_path="/system/ping", http_tags=["system", "network"],
    memory="off",
    description="HTTP-ping a host and return reachability and latency. "
                "Input: host (str!, hostname or URL), timeout (int, default 5 seconds). "
                "Output: {reachable, latency_ms, status, host}.")
async def system_ping(host: str, timeout: int = 5, trace_id=None):
    timeout = parse_timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            t0=time.monotonic(); r=await c.get(f"http://{host}"); ms=round((time.monotonic()-t0)*1000)
        return {"host":host,"reachable":True,"latency_ms":ms,"status":r.status_code}
    except Exception as e: return {"host":host,"reachable":False,"error":str(e)}


# ─────────────────────────────────────────────────────────────────────────────
#  ██  OLLAMA MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@capability("ollama.list_models",
    http_method="GET", http_path="/ollama/models", http_tags=["ollama"],
    memory="off",
    description="List models available on one or all Ollama cluster nodes. "
                "Input: instance_id (str, optional — leave empty for all nodes). "
                "Output: {models: {instance_id: [model_name]}}.")
async def ollama_list_models(instance_id: str = None, trace_id=None):
    targets={instance_id:OLLAMA_INSTANCES[instance_id]} if instance_id and instance_id in OLLAMA_INSTANCES else OLLAMA_INSTANCES
    result={}
    async def _f(iid,inst):
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r=await c.get(f"{inst['url']}/api/tags"); r.raise_for_status()
                models=r.json().get("models",[])
                result[iid]={"models":[{"name":m["name"],"size_gb":round(m.get("size",0)/1e9,2)} for m in models],
                              "count":len(models),"status":"online","url":inst["url"],"has_gpu":inst["has_gpu"]}
        except Exception as e: result[iid]={"error":str(e),"status":"offline"}
    await asyncio.gather(*[_f(iid,inst) for iid,inst in targets.items()])
    return result

@capability("ollama.instances",
    http_method="GET", http_path="/ollama/cluster", http_tags=["ollama"],
    memory="off",
    description="Live status of all Ollama cluster nodes. Output: {instance_id: {url,status,models,in_use,latency_ms,has_gpu}}.")
async def ollama_instances_status(trace_id=None):
    return {iid:{"url":i["url"],"label":i["label"],"has_gpu":i["has_gpu"],"status":i["status"],
                 "latency_ms":i["latency_ms"],"models":i["models"],"in_use":i["in_use"],
                 "errors":i["errors"],"last_check":i["last_check"]}
            for iid,i in OLLAMA_INSTANCES.items()}

@capability("ollama.generate_raw",
    http_method="POST", http_path="/ollama/generate_raw", http_tags=["ollama", "llm"],
    memory="auto",
    description="Direct Ollama generation with full parameter control. "
                "Input: prompt (str!), model (str), system (str), instance_id (str), prefer_gpu (bool), "
                "temperature (float), top_p (float), top_k (int), repeat_penalty (float). "
                "Output: {text, model, instance, tokens}.")
async def ollama_generate_raw(
    prompt: str, model: str = None, system: str = "",
    temperature: float = 0.7, top_p: float = 0.9, num_predict: int = 512,
    stop: str = "", instance_id: str = None, prefer_gpu: bool = False, trace_id=None,
):
    import time as _time
    from Vera.Orchestration.capability_orchestration import (
        _ollama_log_append, _ollama_caller_info,
    )

    chosen=pick_instance(prefer_gpu=prefer_gpu,instance_id=instance_id or None,model=model)
    if not chosen: return {"error":"No available instance"}
    inst=OLLAMA_INSTANCES[chosen]; use_mdl=model or OLLAMA_MODEL
    opts={"temperature":temperature,"top_p":top_p,"num_predict":num_predict}
    if stop: opts["stop"]=[s.strip() for s in stop.split(",")]
    payload={"model":use_mdl,"prompt":prompt,"stream":False,"options":opts}
    if system: payload["system"]=system

    # ── Log the request ──────────────────────────────────────────────────────
    _req_id = str(uuid.uuid4())[:12]
    _t0 = _time.time()
    _prompt_preview = (prompt or "")[:120].replace("\n", " ")
    log.info("ollama_req [%s] model=%s inst=%s caller=capabilities:ollama_generate_raw prompt=%s",
             _req_id, use_mdl, chosen, _prompt_preview)
    try:
        await emit_event({
            "type": "ollama.request", "req_id": _req_id,
            "model": use_mdl, "instance_id": chosen,
            "instance_url": inst.get("url", ""),
            "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
            "caller_module": "capabilities", "cap_name": "ollama.generate_raw",
            "prompt_preview": _prompt_preview, "json_mode": False,
            "prefer_gpu": prefer_gpu, "streaming": False,
        })
    except Exception:
        pass

    inst["in_use"]+=1
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r=await c.post(f"{inst['url']}/api/generate",json=payload); r.raise_for_status()
            d=r.json()
        _elapsed = round(_time.time() - _t0, 2)
        log.info("ollama_done [%s] %.2fs caller=capabilities:ollama_generate_raw", _req_id, _elapsed)
        _ollama_log_append({
            "req_id": _req_id, "model": use_mdl, "instance": chosen,
            "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "done", "elapsed_s": _elapsed,
            "eval_count": d.get("eval_count", 0),
        })
        try:
            await emit_event({
                "type": "ollama.request_done", "req_id": _req_id,
                "model": use_mdl, "instance_id": chosen,
                "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
                "elapsed_s": _elapsed, "eval_count": d.get("eval_count", 0),
            })
        except Exception:
            pass
        return {"text":d.get("response",""),"model":use_mdl,"instance":chosen,"has_gpu":inst.get("has_gpu",False),
                "eval_count":d.get("eval_count"),"total_duration":d.get("total_duration")}
    except Exception as e:
        _elapsed = round(_time.time() - _t0, 2)
        log.error("ollama_generate_raw [%s] FAILED after %.2fs inst=%s err=%s",
                  _req_id, _elapsed, chosen, e)
        _ollama_log_append({
            "req_id": _req_id, "model": use_mdl, "instance": chosen,
            "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
            "prompt_preview": _prompt_preview, "ts": now_iso(),
            "status": "error", "elapsed_s": _elapsed, "error": str(e)[:200],
        })
        try:
            await emit_event({
                "type": "ollama.request_error", "req_id": _req_id,
                "model": use_mdl, "instance_id": chosen,
                "caller_file": "capabilities.py", "caller_func": "ollama_generate_raw",
                "elapsed_s": _elapsed, "error": str(e)[:200],
            })
        except Exception:
            pass
        return {"error":str(e),"instance":chosen}
    finally: inst["in_use"]=max(0,inst["in_use"]-1)


# ─────────────────────────────────────────────────────────────────────────────
#  ██  PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

@capability("pipeline.analyze_and_report",
    http_method="POST", http_path="/pipeline/analyze_report", http_tags=["pipeline", "llm"],
    memory="on", streams=["pipeline.progress"],
            description="Full pipeline: stats + analyze + classify + summarize → report.")
async def pipeline_analyze_and_report(
    text: str, categories: str = "technical,business,general",
    prefer_gpu: bool = True, trace_id=None,
):
    await emit_stream("pipeline.progress", trace_id, {"stage":"start"}, "pipeline.analyze_and_report")
    results = await asyncio.gather(
        text_stats(text=text,trace_id=trace_id),
        llm_analyze(text=text,prefer_gpu=prefer_gpu,trace_id=trace_id),
        llm_classify(text=text,categories=categories,prefer_gpu=prefer_gpu,trace_id=trace_id),
        llm_summarize(text=text,max_words=80,style="concise",prefer_gpu=prefer_gpu,trace_id=trace_id),
        return_exceptions=True,
    )
    await emit_stream("pipeline.progress", trace_id, {"stage":"done"}, "pipeline.analyze_and_report")
    keys=["stats","analysis","classify","summary"]
    return {k:(r if not isinstance(r,Exception) else {"error":str(r)}) for k,r in zip(keys,results)}


# ─────────────────────────────────────────────────────────────────────────────
#  ██  LLM STREAMING ENDPOINT
#  POST /llm/stream  — raw Ollama token SSE, no agent wrapper
#  Body: {prompt, system?, model?, instance_id?, prefer_gpu?}
#  Yields: text/event-stream  data: {"type":"token","text":"..."}
#           ...                data: {"type":"done","text":"<full>"}
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import Request as _Request
from fastapi.responses import StreamingResponse as _StreamingResponse

@APP.post("/llm/stream")
async def llm_stream_endpoint(request: _Request):
    """
    SSE streaming endpoint for raw LLM text generation.
    Yields one SSE event per token from Ollama /api/generate.
    No agent system, no memory — pure token stream.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt      = body.get("prompt", "")
    system      = body.get("system", "")
    model       = body.get("model") or OLLAMA_MODEL
    instance_id = body.get("instance_id") or None
    prefer_gpu  = bool(body.get("prefer_gpu", False))

    chosen = pick_instance(prefer_gpu=prefer_gpu, instance_id=instance_id, model=model) or "cpu-246"
    inst   = OLLAMA_INSTANCES.get(chosen, {})
    url    = inst.get("url", "http://192.168.0.246:11435")

    ollama_body: dict = {"model": model, "prompt": prompt, "stream": True}
    if system: ollama_body["system"] = system

    async def _generate():
        import time as _time
        from Vera.Orchestration.capability_orchestration import (
            emit_event as _emit_event, _ollama_log_append, now_iso as _now_iso,
        )
        _req_id = str(uuid.uuid4())[:12]
        _t0 = _time.monotonic()
        _prompt_preview = (prompt or "")[:120].replace("\n", " ")
        log.info("ollama_req [%s] model=%s inst=%s caller=capabilities:llm_stream prompt=%s",
                 _req_id, model, chosen, _prompt_preview)
        try:
            await _emit_event({
                "type": "ollama.request", "req_id": _req_id,
                "model": model, "instance_id": chosen, "instance_url": url,
                "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                "caller_module": "capabilities", "cap_name": "llm.stream",
                "prompt_preview": _prompt_preview, "json_mode": False,
                "prefer_gpu": prefer_gpu, "streaming": True,
            })
        except Exception:
            pass

        yield b": ping\n\n"
        full = []
        _error_text = ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as c:
                async with c.stream("POST", f"{url}/api/generate", json=ollama_body) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        _error_text = err.decode()[:200]
                        yield f"data: {json.dumps({'type':'error','text':_error_text})}\n\n".encode()
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            token = json.loads(line).get("response", "")
                        except Exception:
                            continue
                        if token:
                            full.append(token)
                            yield f"data: {json.dumps({'type':'token','text':token})}\n\n".encode()
        except Exception as e:
            _error_text = str(e)
            yield f"data: {json.dumps({'type':'error','text':_error_text})}\n\n".encode()
            return
        finally:
            _elapsed = round(_time.monotonic() - _t0, 2)
            if _error_text:
                log.error("ollama_generate [%s] FAILED after %.2fs caller=capabilities:llm_stream err=%s",
                          _req_id, _elapsed, _error_text[:120])
                _ollama_log_append({
                    "req_id": _req_id, "model": model, "instance": chosen,
                    "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                    "prompt_preview": _prompt_preview, "ts": _now_iso(),
                    "status": "error", "elapsed_s": _elapsed, "error": _error_text[:200],
                })
                try:
                    await _emit_event({
                        "type": "ollama.request_error", "req_id": _req_id,
                        "model": model, "instance_id": chosen,
                        "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                        "elapsed_s": _elapsed, "error": _error_text[:200],
                    })
                except Exception:
                    pass
            else:
                log.info("ollama_done [%s] %.2fs tokens=%d caller=capabilities:llm_stream",
                         _req_id, _elapsed, len(full))
                _ollama_log_append({
                    "req_id": _req_id, "model": model, "instance": chosen,
                    "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                    "prompt_preview": _prompt_preview, "ts": _now_iso(),
                    "status": "done", "elapsed_s": _elapsed, "tokens": len(full),
                })
                try:
                    await _emit_event({
                        "type": "ollama.request_done", "req_id": _req_id,
                        "model": model, "instance_id": chosen,
                        "caller_file": "capabilities.py", "caller_func": "llm_stream_endpoint",
                        "elapsed_s": _elapsed, "token_count": len(full),
                    })
                except Exception:
                    pass
        yield f"data: {json.dumps({'type':'done','text':''.join(full)})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return _StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ██  SCHEDULED
# ─────────────────────────────────────────────────────────────────────────────

async def _model_sync():
    for iid,inst in OLLAMA_INSTANCES.items():
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r=await c.get(f"{inst['url']}/api/tags"); r.raise_for_status()
                inst["models"]=[m["name"] for m in r.json().get("models",[])]
        except: pass
    await emit_event({"type":"caps.model_sync"})

schedule(_model_sync, interval=3600, name="model_sync")

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("vera_capabilities:APP", host="0.0.0.0", port=8000, reload=False)