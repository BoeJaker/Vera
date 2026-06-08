#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ollama_wrapper.sh  v3 (hardened)
#
# ADDED in v3:
#   1. Hard concurrency caps   (global + per-model semaphores)
#   2. Request watchdog        (kills stuck inference after REQUEST_TIMEOUT)
#   3. Automatic worker reaper (kills CPU-pinned ollama processes)
#   4. Per-model safety limits (num_predict cap, stop tokens, temp/top_p)
#   5. Systemd unit file       (see ollama.service — apply separately)
#
# Ports (all overridable via env):
#   OLLAMA_PORT          → 11435  (proxy, what clients connect to)
#   OLLAMA_INTERNAL_PORT → 8888   (actual ollama serve)
#   STATUS_PORT          → 11436  (dashboard)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

OLLAMA_PORT="${OLLAMA_PORT:-11435}"
OLLAMA_INTERNAL_PORT="${OLLAMA_INTERNAL_PORT:-8888}"
STATUS_PORT="${STATUS_PORT:-11436}"
LOG_FILE="${LOG_FILE:-/tmp/ollama_wrapper.log}"

python ./StableDiffustionWhisper/GPU_inference.py &

cleanup() {
    echo "[wrapper] Shutting down..."
    kill "$OLLAMA_PID" 2>/dev/null || true
    kill "$PROXY_PID"  2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# ─── Start ollama on internal port ───────────────────────────────────────────
echo "[wrapper] Starting ollama serve on internal port ${OLLAMA_INTERNAL_PORT}"
OLLAMA_HOST="127.0.0.1:${OLLAMA_INTERNAL_PORT}" ollama serve 2>&1 | tee -a "$LOG_FILE" &
OLLAMA_PID=$!
echo "[wrapper] ollama PID: $OLLAMA_PID"
sleep 2

# ─── Python proxy + dashboard ────────────────────────────────────────────────
python3 - "$OLLAMA_PORT" "$OLLAMA_INTERNAL_PORT" "$STATUS_PORT" "$LOG_FILE" << 'PYEOF' &
import sys, json, time, threading, uuid, os, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
from collections import deque, defaultdict
import signal

PROXY_PORT   = int(sys.argv[1])
OLLAMA_PORT  = int(sys.argv[2])
STATUS_PORT  = int(sys.argv[3])
LOG_FILE     = sys.argv[4]
OLLAMA_BASE  = f"http://127.0.0.1:{OLLAMA_PORT}"

# ═══════════════════════════════════════════════════════════════════════════════
# 1. HARD CONCURRENCY CAPS  (global + per-model)
# ═══════════════════════════════════════════════════════════════════════════════
GLOBAL_MAX_CONCURRENCY = 2
PER_MODEL_MAX_CONCURRENCY = 1

global_sem  = threading.BoundedSemaphore(GLOBAL_MAX_CONCURRENCY)
model_sems  = defaultdict(lambda: threading.BoundedSemaphore(PER_MODEL_MAX_CONCURRENCY))
model_sems_lock = threading.Lock()

def acquire_slots(model: str):
    global_sem.acquire()
    if model:
        with model_sems_lock:
            sem = model_sems[model]
        sem.acquire()

def release_slots(model: str):
    try:
        global_sem.release()
    except ValueError:
        pass
    if model:
        with model_sems_lock:
            sem = model_sems.get(model)
        if sem:
            try:
                sem.release()
            except ValueError:
                pass

# ═══════════════════════════════════════════════════════════════════════════════
# 2. REQUEST WATCHDOG  (kills stuck generations)
# ═══════════════════════════════════════════════════════════════════════════════
REQUEST_TIMEOUT = 120   # seconds — hard kill after this

watchdog: dict[str, float] = {}   # req_id → start timestamp
watchdog_lock = threading.Lock()

def watchdog_loop():
    while True:
        time.sleep(5)
        now = time.time()
        expired = []

        with lock:
            for rid, entry in list(active.items()):
                with watchdog_lock:
                    start = watchdog.get(rid, entry["ts"])
                if now - start > REQUEST_TIMEOUT:
                    expired.append((rid, entry.get("model", "")))

        for rid, model in expired:
            print(f"[watchdog] req {rid} exceeded {REQUEST_TIMEOUT}s → killing Ollama runners", flush=True)
            # Ollama exposes no per-request cancel — hard reset inference workers
            os.system("pkill -f 'ollama runner' 2>/dev/null || pkill -f ollama 2>/dev/null || true")
            with lock:
                if rid in active:
                    active[rid]["status"] = "timeout"
                    active[rid]["duration"] = round(now - active[rid]["ts"], 2)
                    del active[rid]
            with watchdog_lock:
                watchdog.pop(rid, None)
            # Release any held semaphore slots so the next request isn't blocked
            release_slots(model)

threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()

# ═══════════════════════════════════════════════════════════════════════════════
# 3. AUTOMATIC STUCK-WORKER REAPER  (CPU safety net)
# ═══════════════════════════════════════════════════════════════════════════════
MAX_CPU = 300    # % — any single ollama process above this is suspect
MAX_AGE = 1800   # seconds — only kill processes older than 30 min

def reap_workers():
    while True:
        time.sleep(30)
        try:
            out = subprocess.check_output(
                "ps -eo pid,etimes,%cpu,cmd | grep ollama | grep -v grep",
                shell=True, stderr=subprocess.DEVNULL
            ).decode(errors="replace")

            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    pid   = int(parts[0])
                    et    = int(parts[1])
                    cpu   = float(parts[2])
                except ValueError:
                    continue

                if cpu > MAX_CPU and et > MAX_AGE:
                    print(f"[reaper] killing stuck worker PID={pid} cpu={cpu:.0f}% age={et}s", flush=True)
                    os.system(f"kill -9 {pid} 2>/dev/null || true")

        except subprocess.CalledProcessError:
            pass   # no matching processes — that's fine
        except Exception as e:
            print(f"[reaper] error: {e}", flush=True)

threading.Thread(target=reap_workers, daemon=True, name="reaper").start()

# ═══════════════════════════════════════════════════════════════════════════════
# 4. PER-MODEL SAFETY LIMITS  (prevents runaway generation loops)
# ═══════════════════════════════════════════════════════════════════════════════
# These are DEFAULTS — they will not override values the caller explicitly set.
DEFAULT_NUM_PREDICT = 256
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TOP_P       = 0.9
HARD_STOP_TOKENS    = [
    "</s>",
    "<|end|>",
    "<|eot_id|>",
    "<|im_end|>",
    "<|endoftext|>",
    "User:",
    "Assistant:",
    "###",
]

def enforce_limits(payload: dict) -> dict:
    """Inject safe defaults into an /api/generate or /api/chat payload."""
    payload.setdefault("num_predict", DEFAULT_NUM_PREDICT)
    payload.setdefault("temperature", DEFAULT_TEMPERATURE)
    payload.setdefault("top_p",       DEFAULT_TOP_P)

    # Merge stop tokens without duplicating any the caller already provided
    existing_stops = set(payload.get("stop", []))
    extra = [t for t in HARD_STOP_TOKENS if t not in existing_stops]
    payload["stop"] = list(existing_stops) + extra

    return payload

INFERENCE_ENDPOINTS = {"/api/generate", "/api/chat"}

# ═══════════════════════════════════════════════════════════════════════════════
# Shared request store
# ═══════════════════════════════════════════════════════════════════════════════
lock = threading.Lock()
requests_log: deque = deque(maxlen=200)
active: dict = {}

def new_entry(method, path, body_bytes):
    req_id = str(uuid.uuid4())[:8]
    body_str = ""
    prompt_preview = ""
    model = ""
    endpoint = path

    try:
        if body_bytes:
            data = json.loads(body_bytes)
            model = data.get("model", "")

            if "messages" in data:
                msgs = data["messages"]
                last_user = next(
                    (m["content"] for m in reversed(msgs) if m.get("role") == "user"), ""
                )
                prompt_preview = last_user[:300] if isinstance(last_user, str) else "[multimodal]"
            elif "prompt" in data:
                prompt_preview = str(data["prompt"])[:300]
            elif "input" in data:
                inp = data["input"]
                prompt_preview = (inp if isinstance(inp, str) else str(inp))[:200]

            body_str = json.dumps(data, indent=2)
    except Exception:
        body_str = body_bytes.decode("utf-8", errors="replace")[:2000]

    entry = {
        "id":               req_id,
        "ts":               time.time(),
        "method":           method,
        "endpoint":         endpoint,
        "model":            model,
        "prompt":           prompt_preview,
        "body":             body_str,
        "status":           "active",
        "duration":         None,
        "response_preview": "",
        "tokens":           None,
    }
    with lock:
        active[req_id] = entry
        requests_log.appendleft(entry)
    with watchdog_lock:
        watchdog[req_id] = time.time()
    return req_id, entry


def finish_entry(req_id, response_chunks, http_status, duration):
    combined = b"".join(response_chunks)
    preview  = ""
    tokens   = None
    try:
        lines = [l for l in combined.split(b"\n") if l.strip()]
        parts = []
        for line in lines:
            try:
                obj = json.loads(line)
                if "response" in obj:
                    parts.append(obj["response"])
                elif "message" in obj and "content" in obj.get("message", {}):
                    parts.append(obj["message"]["content"])
                if obj.get("done") and "eval_count" in obj:
                    tokens = obj.get("eval_count")
            except Exception:
                pass
        preview = "".join(parts)[:500]
        if not preview:
            preview = combined.decode("utf-8", errors="replace")[:500]
    except Exception:
        preview = combined.decode("utf-8", errors="replace")[:500]

    with lock:
        if req_id in active:
            active[req_id]["status"]           = "done" if http_status < 400 else "error"
            active[req_id]["duration"]         = round(duration, 2)
            active[req_id]["response_preview"] = preview
            active[req_id]["tokens"]           = tokens
            del active[req_id]
    with watchdog_lock:
        watchdog.pop(req_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Reverse Proxy Handler
# ═══════════════════════════════════════════════════════════════════════════════
class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_request(self):
        body_bytes = b""
        length = int(self.headers.get("Content-Length", 0))
        if length:
            body_bytes = self.rfile.read(length)

        # ── 4. Enforce safety limits on inference endpoints ──────────────────
        path_clean = self.path.split("?")[0].rstrip("/")
        if self.command == "POST" and path_clean in INFERENCE_ENDPOINTS:
            try:
                payload = json.loads(body_bytes) if body_bytes else {}
                payload = enforce_limits(payload)
                body_bytes = json.dumps(payload).encode()
            except Exception as e:
                print(f"[limits] failed to enforce limits: {e}", flush=True)

        req_id, entry = new_entry(self.command, self.path, body_bytes)
        model = entry.get("model", "")
        t0 = time.time()

        # ── 1. Acquire concurrency slots BEFORE forwarding ───────────────────
        acquire_slots(model)

        try:
            target_url = f"http://127.0.0.1:{OLLAMA_PORT}{self.path}"

            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "content-length")}
            if body_bytes:
                fwd_headers["Content-Length"] = str(len(body_bytes))

            req = Request(
                target_url,
                data=body_bytes or None,
                headers=fwd_headers,
                method=self.command,
            )
            resp = urlopen(req, timeout=REQUEST_TIMEOUT + 10)

            self.send_response(resp.status)
            for h, v in resp.headers.items():
                if h.lower() not in ("transfer-encoding",):
                    self.send_header(h, v)
            self.end_headers()

            chunks = []
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()

            finish_entry(req_id, chunks, resp.status, time.time() - t0)

        except URLError as e:
            try:
                self.send_error(502, f"Ollama unreachable: {e}")
            except Exception:
                pass
            finish_entry(req_id, [], 502, time.time() - t0)

        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass
            finish_entry(req_id, [], 500, time.time() - t0)

        finally:
            # ── 1. ALWAYS release slots — even on exception ──────────────────
            release_slots(model)

    do_GET    = do_request
    do_POST   = do_request
    do_DELETE = do_request


# ═══════════════════════════════════════════════════════════════════════════════
# Status / Dashboard Handler
# ═══════════════════════════════════════════════════════════════════════════════
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ollama Monitor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700&family=Syne:wght@700;800&display=swap');
  :root {
    --bg:#09090e; --surface:#111119; --border:#1c1c2e;
    --accent:#7c6af7; --accent2:#e07b54; --green:#4ade80;
    --yellow:#fbbf24; --red:#f87171; --blue:#60a5fa;
    --text:#e2e2f0; --muted:#6b6b8a;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden;background-image:radial-gradient(ellipse 50% 40% at 80% 5%,rgba(124,106,247,.06) 0%,transparent 60%)}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--border);flex-shrink:0}
  .logo{font-size:20px}
  h1{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;letter-spacing:-.5px}
  h1 span{color:var(--accent)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 7px var(--green);animation:pulse 2s infinite;margin-left:auto}
  .dot.off{background:var(--red);box-shadow:0 0 7px var(--red)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .stats{display:flex;gap:1px;background:var(--border);border-bottom:1px solid var(--border);flex-shrink:0}
  .stat{flex:1;background:var(--surface);padding:10px 16px}
  .stat-l{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
  .stat-v{font-family:'Syne',sans-serif;font-size:22px;font-weight:800}
  .stat-v.a{color:var(--accent)} .stat-v.g{color:var(--green)} .stat-v.y{color:var(--yellow)}
  .guard-bar{display:flex;gap:1px;background:var(--border);border-bottom:1px solid var(--border);flex-shrink:0;padding:0}
  .guard-item{flex:1;background:#0d0d14;padding:7px 14px;display:flex;align-items:center;gap:7px}
  .guard-label{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}
  .guard-val{font-size:11px;font-weight:600;color:var(--accent);margin-left:auto}
  .guard-dot{width:5px;height:5px;border-radius:50%;background:var(--green);box-shadow:0 0 5px var(--green)}
  .main{display:flex;flex:1;overflow:hidden;gap:1px;background:var(--border)}
  .panel{background:var(--bg);display:flex;flex-direction:column;overflow:hidden}
  .left{width:340px;flex-shrink:0}
  .right{flex:1}
  .panel-head{padding:10px 16px;border-bottom:1px solid var(--border);font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);flex-shrink:0;display:flex;align-items:center;gap:8px}
  .panel-body{flex:1;overflow-y:auto;padding:8px}
  .req-row{border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:6px;cursor:pointer;transition:border-color .15s,background .15s}
  .req-row:hover{border-color:var(--accent);background:rgba(124,106,247,.05)}
  .req-row.selected{border-color:var(--accent);background:rgba(124,106,247,.08)}
  .req-row.active-req{border-color:var(--yellow);animation:activePulse 1.5s ease-in-out infinite}
  .req-row.timeout-req{border-color:var(--red);opacity:.7}
  @keyframes activePulse{0%,100%{box-shadow:0 0 0 0 rgba(251,191,36,.2)}50%{box-shadow:0 0 0 4px rgba(251,191,36,0)}}
  .rr-top{display:flex;align-items:center;gap:6px;margin-bottom:4px}
  .rr-method{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(124,106,247,.2);color:var(--accent);letter-spacing:.5px}
  .rr-method.GET{background:rgba(96,165,250,.15);color:var(--blue)}
  .rr-endpoint{font-size:11px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rr-time{font-size:10px;color:var(--muted);margin-left:auto}
  .rr-model{font-size:11px;font-weight:600;color:var(--text);margin-bottom:3px}
  .rr-prompt{font-size:10px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:9px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-left:4px}
  .badge.active{background:rgba(251,191,36,.15);color:var(--yellow);border:1px solid rgba(251,191,36,.3)}
  .badge.done{background:rgba(74,222,128,.1);color:var(--green);border:1px solid rgba(74,222,128,.2)}
  .badge.error{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
  .badge.timeout{background:rgba(248,113,113,.15);color:var(--red);border:1px solid rgba(248,113,113,.4)}
  .detail{padding:16px;height:100%;overflow-y:auto}
  .detail-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px;text-align:center;line-height:2}
  .d-section{margin-bottom:20px}
  .d-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
  .d-meta{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
  .d-chip{background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:4px 10px;font-size:11px}
  .d-chip span{color:var(--muted);margin-right:4px}
  pre.d-body{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:10px;line-height:1.6;color:var(--text);overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:220px;overflow-y:auto}
  pre.d-response{background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:12px;font-size:11px;line-height:1.7;color:var(--green);overflow-x:auto;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto}
  ::-webkit-scrollbar{width:3px;height:3px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>
<header>
  <span class="logo">⬡</span>
  <h1>Ollama <span>Monitor</span></h1>
  <div id="ver" style="font-size:11px;color:var(--muted)">—</div>
  <div id="dot" class="dot off"></div>
</header>

<div class="stats">
  <div class="stat"><div class="stat-l">Active</div><div class="stat-v y" id="s-active">0</div></div>
  <div class="stat"><div class="stat-l">Total Reqs</div><div class="stat-v a" id="s-total">0</div></div>
  <div class="stat"><div class="stat-l">Models Loaded</div><div class="stat-v g" id="s-models">0</div></div>
  <div class="stat"><div class="stat-l">Avg Duration</div><div class="stat-v" id="s-avg" style="font-size:16px;margin-top:3px">—</div></div>
</div>

<!-- Guard-rail status bar -->
<div class="guard-bar">
  <div class="guard-item">
    <div class="guard-dot"></div>
    <div class="guard-label">Global Concurrency</div>
    <div class="guard-val" id="g-global">0 / 2</div>
  </div>
  <div class="guard-item">
    <div class="guard-dot"></div>
    <div class="guard-label">Watchdog</div>
    <div class="guard-val" id="g-wd">120s</div>
  </div>
  <div class="guard-item">
    <div class="guard-dot"></div>
    <div class="guard-label">Token Cap</div>
    <div class="guard-val" id="g-tok">256</div>
  </div>
  <div class="guard-item">
    <div class="guard-dot"></div>
    <div class="guard-label">Reaper</div>
    <div class="guard-val" id="g-reap">CPU&gt;300% / 30m</div>
  </div>
  <div class="guard-item">
    <div class="guard-dot"></div>
    <div class="guard-label">Timeouts</div>
    <div class="guard-val" id="g-timeouts">0</div>
  </div>
</div>

<div class="main">
  <div class="panel left">
    <div class="panel-head">
      <span>Requests</span>
      <span id="live-badge" style="font-size:9px;padding:1px 6px;border-radius:3px;background:rgba(74,222,128,.1);color:var(--green);border:1px solid rgba(74,222,128,.2)">LIVE</span>
    </div>
    <div class="panel-body" id="req-list"></div>
  </div>

  <div class="panel right">
    <div class="panel-head">Request Detail</div>
    <div class="panel-body">
      <div id="detail-pane" class="detail">
        <div class="detail-empty">← Select a request to inspect<br><span style="font-size:10px">or wait for the next one</span></div>
      </div>
    </div>
  </div>
</div>

<script>
let selected = null;
let allReqs = [];

function fmt(ts){ const d=new Date(ts*1000); return d.toLocaleTimeString(); }
function fmtDur(d){ return d===null?'…':`${d}s`; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function poll(){
  try{
    const r = await fetch('/api/status');
    const d = await r.json();
    allReqs = d.requests;

    document.getElementById('dot').className = 'dot';
    document.getElementById('ver').textContent = d.version || '—';
    document.getElementById('s-active').textContent = d.active_count;
    document.getElementById('s-total').textContent = d.total_count;
    document.getElementById('s-models').textContent = d.loaded_models.length;

    const durs = allReqs.filter(r=>r.duration!==null).map(r=>r.duration);
    document.getElementById('s-avg').textContent = durs.length
      ? (durs.reduce((a,b)=>a+b,0)/durs.length).toFixed(1)+'s' : '—';

    // Guard-rail bar
    document.getElementById('g-global').textContent = `${d.active_count} / 2`;
    const timeouts = allReqs.filter(r=>r.status==='timeout').length;
    document.getElementById('g-timeouts').textContent = timeouts;

    renderList();
    if(selected){
      const updated = allReqs.find(r=>r.id===selected);
      if(updated) renderDetail(updated);
    } else if(d.active_count>0){
      const first = allReqs.find(r=>r.status==='active');
      if(first){ selected=first.id; renderDetail(first); }
    }
  } catch(e){
    document.getElementById('dot').className='dot off';
  }
}

function renderList(){
  const el = document.getElementById('req-list');
  if(!allReqs.length){
    el.innerHTML='<div style="text-align:center;padding:32px;color:var(--muted);font-size:11px">No requests yet</div>';
    return;
  }
  el.innerHTML = allReqs.map(r=>{
    let cls = '';
    if(r.status==='active') cls='active-req';
    else if(r.status==='timeout') cls='timeout-req';
    const sel = r.id===selected?'selected':'';
    const methodCls = r.method==='GET'?'GET':'';
    return `<div class="req-row ${cls} ${sel}" onclick="selectReq('${r.id}')">
      <div class="rr-top">
        <span class="rr-method ${methodCls}">${esc(r.method)}</span>
        <span class="rr-endpoint">${esc(r.endpoint)}</span>
        <span class="rr-time">${fmt(r.ts)}</span>
      </div>
      ${r.model?`<div class="rr-model">${esc(r.model)}<span class="badge ${r.status}">${r.status}</span></div>`:''}
      ${r.prompt?`<div class="rr-prompt">${esc(r.prompt)}</div>`:''}
    </div>`;
  }).join('');
}

function selectReq(id){
  selected = id;
  const req = allReqs.find(r=>r.id===id);
  if(req) renderDetail(req);
  renderList();
}

function renderDetail(r){
  const dp = document.getElementById('detail-pane');
  const ago = ((Date.now()/1000)-r.ts).toFixed(0);
  dp.innerHTML = `
    <div class="d-section">
      <div class="d-label">Request Info</div>
      <div class="d-meta">
        <div class="d-chip"><span>method</span>${esc(r.method)}</div>
        <div class="d-chip"><span>endpoint</span>${esc(r.endpoint)}</div>
        <div class="d-chip"><span>status</span><span class="badge ${r.status}">${r.status}</span></div>
        ${r.model?`<div class="d-chip"><span>model</span>${esc(r.model)}</div>`:''}
        <div class="d-chip"><span>time</span>${fmt(r.ts)} (${ago}s ago)</div>
        <div class="d-chip"><span>duration</span>${fmtDur(r.duration)}</div>
        ${r.tokens!==null?`<div class="d-chip"><span>tokens out</span>${r.tokens}</div>`:''}
      </div>
    </div>
    ${r.prompt?`<div class="d-section"><div class="d-label">Prompt</div><pre class="d-body">${esc(r.prompt)}</pre></div>`:''}
    ${r.body?`<div class="d-section"><div class="d-label">Full Request Body</div><pre class="d-body">${esc(r.body)}</pre></div>`:''}
    ${r.response_preview?`<div class="d-section"><div class="d-label">Response${r.status==='active'?' (streaming…)':''}</div><pre class="d-response">${esc(r.response_preview)}</pre></div>`:''}
  `;
}

setInterval(poll, 800);
poll();
</script>
</body>
</html>
"""

class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

        elif self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            version = ""
            loaded_models = []
            try:
                r = urlopen(f"http://127.0.0.1:{OLLAMA_PORT}/api/version", timeout=2)
                version = json.loads(r.read()).get("version", "")
            except Exception:
                pass
            try:
                r = urlopen(f"http://127.0.0.1:{OLLAMA_PORT}/api/ps", timeout=2)
                loaded_models = json.loads(r.read()).get("models", [])
            except Exception:
                pass

            with lock:
                reqs = list(requests_log)
                ac   = len(active)

            payload = {
                "version":       version,
                "loaded_models": loaded_models,
                "active_count":  ac,
                "total_count":   len(reqs),
                "requests":      reqs,
            }
            self.wfile.write(json.dumps(payload).encode())

        elif self.path == '/logs':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            try:
                with open(LOG_FILE, 'r', errors='replace') as f:
                    self.wfile.write(f.read().encode())
            except Exception as e:
                self.wfile.write(f"unavailable: {e}".encode())

        else:
            self.send_error(404)


def start_server(handler, port, label):
    srv = HTTPServer(('0.0.0.0', port), handler)
    print(f"[wrapper] {label} → http://0.0.0.0:{port}", flush=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

proxy_srv  = start_server(ProxyHandler, PROXY_PORT,  "Proxy (Ollama API)")
status_srv = start_server(StatusHandler, STATUS_PORT, "Dashboard")

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(1)
PYEOF

PROXY_PID=$!

echo "────────────────────────────────────────────────────────────────────"
echo "  Ollama wrapper v3 (hardened)"
echo "  Clients connect to      → http://0.0.0.0:${OLLAMA_PORT}  (proxy)"
echo "  Ollama internals        → 127.0.0.1:${OLLAMA_INTERNAL_PORT}"
echo "  Dashboard               → http://0.0.0.0:${STATUS_PORT}"
echo "  Log file                → ${LOG_FILE}"
echo ""
echo "  Guard-rails active:"
echo "    ✔ Global concurrency  ≤ 2 simultaneous requests"
echo "    ✔ Per-model slot      ≤ 1 per model"
echo "    ✔ Request watchdog    kills after 120s"
echo "    ✔ Worker reaper       kills CPU>300% / age>30m"
echo "    ✔ Token cap           256 tokens (default)"
echo "    ✔ Stop tokens         </s> <|end|> <|eot_id|> <|im_end|> ..."
echo "────────────────────────────────────────────────────────────────────"

wait $OLLAMA_PID
