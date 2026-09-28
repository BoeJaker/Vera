"""
ollama_tap.py  -  see every request a node's Ollama serves, without changing any
=================================================================================

A transparent reverse proxy that sits on a node's PUBLIC Ollama port and
forwards to Ollama on loopback. It exists so the Estate's node-activity pane can
show EVERYTHING a node is doing - Vera's own calls, Loop Lab sandboxes, and any
external caller - with the prompt, the response and the stats (user,
2026-09-28: "prompts + responses via capture proxy").

    python ollama_tap.py --listen 0.0.0.0:11435 --upstream http://127.0.0.1:11445 --node cpu-246

THE RETIRED WRAPPER IS WHY THIS IS SHAPED THE WAY IT IS
--------------------------------------------------------
edge/ollama_wrapper.sh was removed (2026-09-11) for: fighting the unit that owns
the port, a watchdog that fell back to killing every ollama process, a reaper
that killed legitimate CPU inference, and injected `num_predict` / stop tokens
that truncated Vera's output. So this proxy:

  * NEVER changes a request or a response. The body goes upstream byte for byte;
    the response streams back chunk by chunk as it arrives. Capture is a tee.
  * Owns no process but its own. No watchdog, no reaper, no kill.
  * Has no timeout of its own on a generation (they run for many minutes).
  * Survives Redis being down: records then stay in the local ring and file.

WHAT IT RECORDS
---------------
For generate/chat/embed (native and OpenAI-compatible) one record per request:
caller ip, X-Vera-Origin (who in Vera asked - instance, job type, request id),
method, path, model, start/end, duration, status, stream flag, prompt text,
response text, token counts and tok/s from Ollama's own final chunk. Embeddings
record their input count and vector size, never the vectors. Everything else
(/api/tags, /api/ps, blob uploads, pulls) passes through uncaptured.

Records go (a) to a shared Redis stream, `vera:node_activity`, text truncated
to TEXT_CAP per field so the stream stays small; (b) to a rotating JSONL file on
the node with the FULL text, fetched on demand via /vera-tap/record/<id>;
(c) to an in-memory ring. In-flight requests are listed at /vera-tap/inflight.

NO `from __future__ import annotations` - not needed here, kept for parity with
the other edge servers.
"""

import argparse
import asyncio
import json
import os
import socket
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Dict, Optional

#: Paths whose request/response are captured (everything else passes through).
CAPTURE = {
    "/api/generate": "generate", "/api/chat": "chat",
    "/api/embed": "embed", "/api/embeddings": "embed",
    "/v1/chat/completions": "chat", "/v1/completions": "generate",
    "/v1/embeddings": "embed",
}
#: Largest request body captured in memory; a bigger one passes through untapped.
MAX_CAPTURE_BODY = 8 * 1024 * 1024
#: Text kept per field in the shared stream (the node file keeps it all).
TEXT_CAP = 16 * 1024


def _source_version() -> str:
    """This file's own hash, as the host computes it for the copy it would
    install (ollama_tap_core.source_version) - so a release can tell a node
    running an old tap from a current one. '' when unreadable."""
    import hashlib
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return "tap-" + hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return ""


SOURCE_VERSION = _source_version()
#: Largest text kept in the node file per field.
FILE_TEXT_CAP = 1024 * 1024
STREAM = "vera:node_activity"
STREAM_MAXLEN = 5000
INFLIGHT_KEY = "vera:node_activity:inflight:{node}"
#: Headers that are about ONE hop and must not be forwarded as-is.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


# ── capture helpers (pure) ────────────────────────────────────────────────────
def prompt_text(kind: str, body: Dict[str, Any]) -> str:
    """The human-readable prompt of a generate/chat/embed request."""
    if kind == "embed":
        inp = body.get("input", body.get("prompt", ""))
        if isinstance(inp, list):
            return f"[{len(inp)} inputs] " + " | ".join(str(x)[:200] for x in inp[:5])
        return str(inp)
    if "messages" in body:
        parts = []
        for m in body.get("messages") or []:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
            parts.append(f"[{m.get('role', '?')}] {c}")
        return "\n".join(parts)
    sys_p = body.get("system")
    return (f"[system] {sys_p}\n" if sys_p else "") + str(body.get("prompt", ""))


class ResponseTee:
    """Accumulates a response's text and final stats from the chunks as they
    pass - NDJSON (Ollama native streaming), SSE (OpenAI streaming) or one JSON
    document - without holding up a single byte."""

    def __init__(self, kind: str, cap: int = FILE_TEXT_CAP):
        self.kind, self.cap = kind, cap
        self.buf = b""
        self.text: list = []
        self.size = 0
        self.stats: Dict[str, Any] = {}
        self.raw_tail = b""

    def _add_text(self, s: str):
        if s and self.size < self.cap:
            s = s[: self.cap - self.size]
            self.text.append(s)
            self.size += len(s)

    def _obj(self, o: Dict[str, Any]):
        if not isinstance(o, dict):
            return
        if "response" in o:
            self._add_text(str(o.get("response") or ""))
        msg = o.get("message")
        if isinstance(msg, dict):
            self._add_text(str(msg.get("content") or ""))
            if msg.get("thinking"):
                self.stats["thinking_chars"] = self.stats.get("thinking_chars", 0) + len(msg["thinking"])
        for ch in o.get("choices") or []:                  # OpenAI shape
            d = ch.get("delta") or ch.get("message") or {}
            self._add_text(str(d.get("content") or ch.get("text") or ""))
        if "embeddings" in o and isinstance(o["embeddings"], list):
            e = o["embeddings"]
            self.stats.update(vectors=len(e), dims=len(e[0]) if e and isinstance(e[0], list) else 0)
        if "embedding" in o and isinstance(o["embedding"], list):
            self.stats.update(vectors=1, dims=len(o["embedding"]))
        if isinstance(o.get("data"), list) and o["data"] and "embedding" in o["data"][0]:
            self.stats.update(vectors=len(o["data"]), dims=len(o["data"][0]["embedding"]))
        for k in ("eval_count", "prompt_eval_count", "eval_duration", "prompt_eval_duration",
                  "load_duration", "total_duration", "done_reason"):
            if k in o:
                self.stats[k] = o[k]
        u = o.get("usage")
        if isinstance(u, dict):
            self.stats.setdefault("prompt_eval_count", u.get("prompt_tokens"))
            self.stats.setdefault("eval_count", u.get("completion_tokens"))
        if o.get("error"):
            self.stats["error"] = str(o["error"])[:500]

    def feed(self, chunk: bytes):
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self._line(line)
        if len(self.buf) > 4 * 1024 * 1024:       # one enormous JSON document
            self.raw_tail = self.buf[-4096:]

    def _line(self, line: bytes):
        line = line.strip()
        if line.startswith(b"data:"):
            line = line[5:].strip()
        if not line or line == b"[DONE]":
            return
        try:
            self._obj(json.loads(line))
        except ValueError:
            pass

    def finish(self) -> Dict[str, Any]:
        if self.buf.strip():
            self._line(self.buf)
            self.buf = b""
        s = dict(self.stats)
        ev, ed = s.get("eval_count"), s.get("eval_duration")
        if ev and ed:
            s["tps"] = round(ev / (ed / 1e9), 2)
        return {"text": "".join(self.text), "stats": s}


def cut(s: str, cap: int) -> str:
    return s if len(s) <= cap else s[:cap] + f"\n…[{len(s) - cap} more chars]"


# ── the proxy ─────────────────────────────────────────────────────────────────
class Tap:
    def __init__(self, upstream: str, node: str, port: int, redis_url: str = "",
                 log_dir: str = "", ring: int = 500):
        self.upstream = upstream.rstrip("/")
        self.node, self.port = node, port
        self.redis_url = redis_url
        self.redis = None
        self.log_dir = log_dir
        self.ring: deque = deque(maxlen=ring)
        self.inflight: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.session = None
        self.counts = {"requests": 0, "captured": 0, "redis_errors": 0}

    async def start(self):
        import aiohttp
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None),
            auto_decompress=False)
        if self.redis_url:
            try:
                import redis.asyncio as aioredis
                self.redis = aioredis.from_url(self.redis_url)
                await self.redis.ping()
            except Exception as e:
                print(f"tap: redis unavailable ({type(e).__name__}); records stay local", flush=True)
                self.redis = None

    # ─ records ─
    async def _publish_inflight(self):
        if self.redis is None:
            return
        try:
            key = INFLIGHT_KEY.format(node=self.node)
            await self.redis.set(key, json.dumps(list(self.inflight.values())), ex=120)
        except Exception:
            self.counts["redis_errors"] += 1

    async def _record(self, rec: Dict[str, Any]):
        self.ring.append(rec)
        if self.log_dir:
            try:
                path = os.path.join(self.log_dir, f"tap-{self.port}.jsonl")
                if os.path.exists(path) and os.path.getsize(path) > 256 * 1024 * 1024:
                    os.replace(path, path + ".1")
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except OSError:
                pass
        if self.redis is not None:
            short = dict(rec, prompt=cut(rec.get("prompt", ""), TEXT_CAP),
                         response=cut(rec.get("response", ""), TEXT_CAP))
            try:
                await self.redis.xadd(STREAM, {"r": json.dumps(short)},
                                      maxlen=STREAM_MAXLEN, approximate=True)
            except Exception:
                self.counts["redis_errors"] += 1

    # ─ own endpoints ─
    def _own(self, path: str):
        from aiohttp import web
        if path == "/vera-tap/health":
            return web.json_response({"ok": True, "source": SOURCE_VERSION,
                                      "node": self.node, "port": self.port,
                                      "upstream": self.upstream, "redis": self.redis is not None,
                                      "inflight": len(self.inflight), **self.counts})
        if path == "/vera-tap/inflight":
            now = time.time()
            return web.json_response([dict(r, running_s=round(now - r["start"], 1))
                                      for r in self.inflight.values()])
        if path == "/vera-tap/recent":
            return web.json_response([dict(r, prompt=cut(r.get("prompt", ""), TEXT_CAP),
                                           response=cut(r.get("response", ""), TEXT_CAP))
                                      for r in list(self.ring)[-100:]])
        if path.startswith("/vera-tap/record/"):
            rid = path.rsplit("/", 1)[-1]
            for r in reversed(self.ring):
                if r["id"] == rid:
                    return web.json_response(r)
            for name in (f"tap-{self.port}.jsonl", f"tap-{self.port}.jsonl.1"):
                p = os.path.join(self.log_dir or "", name)
                if self.log_dir and os.path.exists(p):
                    with open(p, encoding="utf-8") as fh:
                        for line in fh:
                            if f'"id": "{rid}"' in line:
                                return web.json_response(json.loads(line))
            return web.json_response({"error": "not found"}, status=404)
        return None

    async def handle(self, request):
        from aiohttp import web
        own = self._own(request.path) if request.path.startswith("/vera-tap/") else None
        if own is not None:
            return own
        self.counts["requests"] += 1
        kind = CAPTURE.get(request.path) if request.method == "POST" else None
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        body: Optional[bytes] = None
        if kind and (request.content_length or 0) <= MAX_CAPTURE_BODY:
            body = await request.read()
        rec: Optional[Dict[str, Any]] = None
        if kind and body is not None:
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                parsed = {}
            rec = {"id": uuid.uuid4().hex[:16], "node": self.node, "port": self.port,
                   "service": "ollama", "kind": kind, "path": request.path,
                   "caller": request.remote or "",
                   "origin": request.headers.get("X-Vera-Origin", ""),
                   "model": str(parsed.get("model", "")),
                   "stream": bool(parsed.get("stream", kind != "embed")),
                   "options": {k: v for k, v in (parsed.get("options") or {}).items()
                               if k in ("num_ctx", "num_predict", "num_thread", "temperature",
                                        "num_gpu")},
                   "prompt": cut(prompt_text(kind, parsed), FILE_TEXT_CAP),
                   "start": time.time()}
            self.counts["captured"] += 1
            self.inflight[rec["id"]] = {k: rec[k] for k in (
                "id", "node", "port", "service", "kind", "path", "caller", "origin", "model",
                "start")} | {"prompt_preview": rec["prompt"][:300]}
            await self._publish_inflight()
        tee = ResponseTee(kind) if rec else None
        status = 502
        resp = None
        try:
            async with self.session.request(
                    request.method, self.upstream + request.path_qs, headers=headers,
                    data=body if body is not None else request.content,
                    allow_redirects=False) as up:
                status = up.status
                out_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP}
                resp = web.StreamResponse(status=up.status, headers=out_headers)
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                    if tee is not None:
                        tee.feed(chunk)
                await resp.write_eof()
                return resp
        except (ConnectionResetError, asyncio.CancelledError) as e:
            if rec is not None:
                rec["error"] = "client went away" if isinstance(e, ConnectionResetError) else "cancelled"
            raise
        except Exception as e:
            if rec is not None:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            if resp is None:
                return web.json_response({"error": f"upstream: {type(e).__name__}"}, status=502)
            return resp
        finally:
            if rec is not None:
                self.inflight.pop(rec["id"], None)
                got = tee.finish() if tee else {"text": "", "stats": {}}
                rec.update(end=time.time(), status=status, response=got["text"], **got["stats"])
                rec["duration_s"] = round(rec["end"] - rec["start"], 3)
                asyncio.ensure_future(self._finish(rec))

    async def _finish(self, rec):
        await self._publish_inflight()
        await self._record(rec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default=os.getenv("VERA_TAP_LISTEN", "0.0.0.0:11435"))
    ap.add_argument("--upstream", default=os.getenv("VERA_TAP_UPSTREAM", "http://127.0.0.1:11445"))
    ap.add_argument("--node", default=os.getenv("VERA_TAP_NODE") or socket.gethostname())
    ap.add_argument("--log-dir", default=os.getenv("VERA_TAP_LOG_DIR", "/var/log/vera-tap"))
    a = ap.parse_args()
    from aiohttp import web
    host, _, port = a.listen.rpartition(":")
    if a.log_dir:
        os.makedirs(a.log_dir, exist_ok=True)
    tap = Tap(a.upstream, a.node, int(port), os.getenv("VERA_TAP_REDIS_URL", ""), a.log_dir)

    async def on_start(_app):
        await tap.start()

    app = web.Application(client_max_size=0)
    app.on_startup.append(on_start)
    app.router.add_route("*", "/{tail:.*}", tap.handle)
    web.run_app(app, host=host or "0.0.0.0", port=int(port), access_log=None,
                print=lambda *_: print(f"tap {a.node} {a.listen} -> {a.upstream}", flush=True))


if __name__ == "__main__":
    main()
