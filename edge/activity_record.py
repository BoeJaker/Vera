"""activity_record.py - node services report their calls to the Estate Activity pane.

The Ollama servers are covered by the tap in front of them (ollama_tap.py).
The node's own Python services - the NLP server, the media server - are ours,
so instead of another proxy they carry this pure-ASGI recorder: it wraps the
request and response streams, passes every byte through untouched, and writes
one record per captured call to the same shared stream the taps use,
`vera:node_activity` (and its in-flight key). The pane then shows NLP, STT,
TTS and image calls beside the LLM ones, from every caller.

What a record holds: caller ip, X-Vera-Origin, method, path, service, start /
end / duration, status, and a SUMMARY of the request and response - text as
text, base64 payloads (audio, images) replaced by their size, capped.

Stdlib only; Redis is optional (`redis` if importable, else records stay in the
ring). The Redis URL comes from VERA_ACTIVITY_REDIS_URL, else the tap's 0600
credential file (/etc/vera-tap/redis.env) the tap installer writes on every node.
"""

import json
import os
import re
import socket
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, Iterable, Optional

STREAM = "vera:node_activity"
STREAM_MAXLEN = 5000
INFLIGHT_KEY = "vera:node_activity:inflight:{node}"
TEXT_CAP = 16 * 1024
_B64 = re.compile(r"^[A-Za-z0-9+/=\s]{512,}$")
REDIS_ENV_FILE = "/etc/vera-tap/redis.env"


def redis_url() -> str:
    url = os.getenv("VERA_ACTIVITY_REDIS_URL", "")
    if url:
        return url
    try:
        with open(REDIS_ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k == "VERA_TAP_REDIS_URL":
                    return v
    except OSError:
        pass
    return ""


def summarize(obj: Any, cap: int = TEXT_CAP) -> str:
    """JSON text of a request/response with base64 blobs replaced by their size."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items()}
        if isinstance(o, list):
            if len(o) > 16 and all(isinstance(v, (int, float)) for v in o[:16]):
                return f"[vector {len(o)}]"                     # embeddings, scores
            return [strip(v) for v in o[:50]] + (["…%d more" % (len(o) - 50)] if len(o) > 50 else [])
        if isinstance(o, str) and len(o) >= 512 and _B64.match(o[:4096]):
            return f"[base64 {len(o)} chars]"
        if isinstance(o, float):
            return round(o, 5)
        return o
    try:
        s = json.dumps(strip(obj), ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= cap else s[:cap] + f"…[{len(s) - cap} more chars]"


class Recorder:
    def __init__(self, service: str, node: str = "", port: int = 0):
        self.service = service
        self.node = node or os.getenv("VERA_ACTIVITY_NODE", "") or socket.gethostname()
        self.port = port
        self.ring: deque = deque(maxlen=300)
        self.inflight: Dict[str, Dict[str, Any]] = {}
        self._r = None
        self._lock = threading.Lock()
        url = redis_url()
        if url:
            try:
                import redis  # type: ignore
                self._r = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
            except Exception:
                self._r = None

    def _push(self, fn: Callable):
        """Redis writes off the request path, in a thread - never block a call."""
        if self._r is None:
            return
        def run():
            try:
                fn(self._r)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _inflight_key(self):
        return INFLIGHT_KEY.format(node=f"{self.node}:{self.service}")

    def start(self, rec: Dict[str, Any]):
        with self._lock:
            self.inflight[rec["id"]] = {k: rec.get(k) for k in (
                "id", "node", "port", "service", "kind", "path", "caller", "origin", "model",
                "start")} | {"prompt_preview": str(rec.get("prompt") or "")[:300]}
            snap = json.dumps(list(self.inflight.values()))
        self._push(lambda r: r.set(self._inflight_key(), snap, ex=120))

    def finish(self, rec: Dict[str, Any]):
        with self._lock:
            self.inflight.pop(rec["id"], None)
            snap = json.dumps(list(self.inflight.values()))
            self.ring.append(rec)
        payload = json.dumps(rec)
        def go(r):
            r.set(self._inflight_key(), snap, ex=120)
            r.xadd(STREAM, {"r": payload}, maxlen=STREAM_MAXLEN, approximate=True)
        self._push(go)


class ActivityMiddleware:
    """Pure ASGI: tees the request and response bodies of `paths` (prefixes)
    into a Recorder. Bodies pass through exactly as they arrive."""

    def __init__(self, app, recorder: Recorder, paths: Iterable[str],
                 kind_of: Optional[Callable[[str], str]] = None, max_body: int = 32 * 1024 * 1024):
        self.app, self.rec = app, recorder
        self.paths = tuple(paths)
        self.kind_of = kind_of or (lambda p: p.strip("/").split("/")[0] or "call")
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" \
                or not scope.get("path", "").startswith(self.paths):
            return await self.app(scope, receive, send)
        hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        rec: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:16], "node": self.rec.node, "port": self.rec.port,
            "service": self.rec.service, "kind": self.kind_of(scope["path"]), "path": scope["path"],
            "caller": (scope.get("client") or ("", 0))[0], "origin": hdrs.get("x-vera-origin", ""),
            "model": "", "start": time.time()}
        req_chunks, resp_chunks, sizes = [], [], {"req": 0, "resp": 0, "status": 0}
        ctype = hdrs.get("content-type", "")

        async def rcv():
            msg = await receive()
            if msg.get("type") == "http.request":
                b = msg.get("body", b"")
                sizes["req"] += len(b)
                if sizes["req"] <= self.max_body:
                    req_chunks.append(b)
            return msg

        async def snd(msg):
            if msg.get("type") == "http.response.start":
                sizes["status"] = msg.get("status", 0)
            elif msg.get("type") == "http.response.body":
                b = msg.get("body", b"")
                sizes["resp"] += len(b)
                if sizes["resp"] <= self.max_body:
                    resp_chunks.append(b)
            await send(msg)

        self.rec.start(rec)
        err = ""
        try:
            await self.app(scope, rcv, snd)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
            raise
        finally:
            rec["end"] = time.time()
            rec["duration_s"] = round(rec["end"] - rec["start"], 3)
            rec["status"] = sizes["status"] or (500 if err else 0)
            rec["request_bytes"], rec["response_bytes"] = sizes["req"], sizes["resp"]
            if err:
                rec["error"] = err
            if "multipart" in ctype:
                # sized from the header: a handler that never reads its upload
                # still sent one
                n = max(sizes["req"], int(hdrs.get("content-length") or 0))
                rec["prompt"] = f"[multipart upload {n} bytes]"
            else:
                rec["prompt"] = self._body_text(b"".join(req_chunks), ctype, sizes["req"])
            rec["response"] = self._body_text(b"".join(resp_chunks), "application/json", sizes["resp"])
            try:
                model = json.loads(b"".join(req_chunks) or b"{}").get("model", "")
                rec["model"] = str(model or "")
            except (ValueError, AttributeError):
                pass
            self.rec.finish(rec)

    @staticmethod
    def _body_text(raw: bytes, ctype: str, size: int) -> str:
        if not raw:
            return ""
        if "multipart" in ctype:
            return f"[multipart upload {size} bytes]"
        try:
            return summarize(json.loads(raw))
        except ValueError:
            t = raw[:TEXT_CAP].decode("utf-8", "replace")
            return t if raw[:64].isascii() else f"[binary {size} bytes]"


def install(app, service: str, paths: Iterable[str], port: int = 0,
            kind_of: Optional[Callable[[str], str]] = None) -> Recorder:
    """Wrap a FastAPI/Starlette app. Returns the Recorder (its ring backs a
    /vera-activity/recent endpoint if the app wants one)."""
    rec = Recorder(service, port=port or int(os.getenv("SERVER_PORT", "0") or 0))
    app.add_middleware(ActivityMiddleware, recorder=rec, paths=paths, kind_of=kind_of)
    return rec
