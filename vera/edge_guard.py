"""Edge guard: a pure ASGI middleware that refuses cross-site requests and
WebSocket handshakes before they reach routing.

Pure ASGI, not ``@APP.middleware("http")``, for two reasons:
- an HTTP middleware never sees WebSocket handshakes (``/ws/mcp``, the
  terminals, the consoles), which are the most dangerous route class;
- it buffers streaming bodies, which would break SSE and chunked responses.

The rules are in ``edge_guard_core`` (pure, unit-tested). This module adds:
- the allowed-host list for this machine;
- the mode switch;
- refusal responses;
- rate-limited evidence events;
- in-memory counters for ``auth.edge.status``.

Mode, from ``VERA_EDGE_GUARD`` (default ``enforce`` since 2026-10-17, after an
observe phase that saw no legitimate request refused):
  observe   evaluate every request and record what WOULD be refused; refuse nothing
  enforce   refuse (HTTP 403, or 421 for a foreign Host; WebSocket close 1008)
  off       do nothing
Break-glass: a temporary override can be set out of band on the host's Redis,
``SET vera:auth:mode observe EX 3600`` (always with a TTL; read through a 5 s
cache). No capability can switch the guard off.

The Host rule has its own switch, ``VERA_EDGE_GUARD_HOST`` (default
``observe``). A host name the owner uses but this module does not know would
otherwise lock the owner out; list such names in ``VERA_ALLOWED_HOSTS``.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections import Counter, deque
from typing import Any, Awaitable, Callable, Iterable, Optional

try:  # the app's identity; the pure core is stateless, so a second copy is harmless
    from Vera.vera import edge_guard_core as core
except ImportError:  # tests that put the worktree root on sys.path
    from vera import edge_guard_core as core  # type: ignore

log = logging.getLogger("vera.edge_guard")

MODES = ("observe", "enforce", "off")
_CURRENT: Optional["EdgeGuard"] = None


def current() -> Optional["EdgeGuard"]:
    """The guard instance Starlette built for the app, once the first request arrived."""
    return _CURRENT
_EVENT_WINDOW_S = 60.0
_REDIS_CACHE_S = 5.0


def _env_list(name: str) -> list:
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


def _env_mode(name: str, default: str) -> str:
    v = (os.environ.get(name) or default).strip().lower()
    return v if v in MODES else default


def local_hosts(backend_host: str = "") -> set:
    """Names and addresses that mean 'this machine' in a Host header."""
    names = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    if backend_host:
        names.add(backend_host.lower())
    try:
        hn = socket.gethostname()
        names.update({hn.lower(), socket.getfqdn().lower()})
        for fam in (socket.AF_INET, socket.AF_INET6):
            try:
                for info in socket.getaddrinfo(hn, None, fam):
                    names.add(str(info[4][0]).lower())
            except OSError:
                pass
    except OSError:
        pass
    try:  # every interface address, where the platform exposes them
        import psutil  # type: ignore
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family in (socket.AF_INET, socket.AF_INET6):
                    names.add(str(a.address).split("%")[0].lower())
    except Exception:
        pass
    names.update(h.lower() for h in _env_list("VERA_ALLOWED_HOSTS"))
    return {n for n in names if n}


class EdgeGuard:
    """ASGI middleware. ``on_event(dict)`` (async or sync) receives evidence."""

    def __init__(self, app, *, backend_host: str = "", listen_port: Optional[int] = None,
                 on_event: Optional[Callable[[dict], Any]] = None,
                 redis_getter: Optional[Callable[[], Any]] = None,
                 allowed_hosts: Optional[Iterable[str]] = None,
                 allowed_origins: Optional[Iterable[str]] = None,
                 trusted_proxies: Optional[Iterable[str]] = None,
                 mode: Optional[str] = None, host_mode: Optional[str] = None):
        self.app = app
        self.listen_port = listen_port
        self.on_event = on_event
        self.redis_getter = redis_getter
        self.allowed_hosts = set(allowed_hosts) if allowed_hosts is not None else local_hosts(backend_host)
        self.allowed_origins = list(allowed_origins) if allowed_origins is not None else _env_list("VERA_ALLOWED_ORIGINS")
        self.trusted_proxies = set(trusted_proxies) if trusted_proxies is not None else set(_env_list("VERA_TRUSTED_PROXIES"))
        self._mode = mode or _env_mode("VERA_EDGE_GUARD", "enforce")
        self._host_mode = host_mode or _env_mode("VERA_EDGE_GUARD_HOST", "observe")
        self._override: Optional[str] = None
        self._override_at = 0.0
        self._last_event: dict = {}
        self.counts: Counter = Counter()
        self.seen_origins: Counter = Counter()
        self.seen_hosts: Counter = Counter()
        self.recent: deque = deque(maxlen=50)
        global _CURRENT
        _CURRENT = self      # Starlette builds the instance itself; status() reads it here

    # ── mode ────────────────────────────────────────────────────────────────
    async def mode(self) -> str:
        now = time.monotonic()
        if self.redis_getter is not None and now - self._override_at > _REDIS_CACHE_S:
            self._override_at = now
            try:
                r = self.redis_getter()
                v = await r.get("vera:auth:mode") if r is not None else None
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                self._override = v.strip().lower() if isinstance(v, str) and v.strip().lower() in MODES else None
            except Exception:
                self._override = None
        return self._override or self._mode

    # ── evidence ────────────────────────────────────────────────────────────
    async def _emit(self, kind: str, verdict: core.Verdict, method: str, path: str,
                    headers: dict, peer: str) -> None:
        origin = headers.get("origin", "")
        self.counts[f"{kind}:{verdict.rule}"] += 1
        self.recent.append({"t": round(time.time(), 1), "event": kind, "rule": verdict.rule,
                            "method": method, "path": path[:200], "origin": origin[:200],
                            "host": headers.get("host", "")[:200], "peer": peer,
                            "site": headers.get("sec-fetch-site", "")})
        key = (kind, verdict.rule, origin, path)
        now = time.monotonic()
        if now - self._last_event.get(key, 0.0) < _EVENT_WINDOW_S:
            return
        self._last_event[key] = now
        if len(self._last_event) > 2000:
            self._last_event.clear()
        log.warning("edge guard %s: rule=%s %s %s origin=%r host=%r peer=%s (%s)", kind, verdict.rule,
                    method, path[:200], origin[:200], headers.get("host", "")[:200], peer, verdict.reason)
        if self.on_event is None:
            return
        evt = {"type": kind, "rule": verdict.rule, "reason": verdict.reason, "method": method,
               "path": path[:200], "origin": origin[:200], "host": headers.get("host", "")[:200],
               "peer": peer}
        try:
            res = self.on_event(evt)
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass

    def status(self) -> dict:
        return {"mode": self._override or self._mode, "configured_mode": self._mode,
                "override": self._override, "host_mode": self._host_mode,
                "allowed_hosts": sorted(self.allowed_hosts),
                "allowed_origins": list(self.allowed_origins),
                "trusted_proxies": sorted(self.trusted_proxies),
                "counts": dict(self.counts),
                "seen_origins": dict(self.seen_origins.most_common(30)),
                "seen_hosts": dict(self.seen_hosts.most_common(30)),
                "recent": list(self.recent)}

    # ── ASGI ────────────────────────────────────────────────────────────────
    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        mode = await self.mode()
        if mode == "off":
            return await self.app(scope, receive, send)
        headers = {}
        for k, v in scope.get("headers") or []:
            try:
                headers[k.decode("latin-1").lower()] = v.decode("latin-1")
            except Exception:
                continue
        client = scope.get("client") or ("", 0)
        peer = str(client[0] if client else "")
        method = "GET" if kind == "websocket" else str(scope.get("method") or "GET")
        path = str(scope.get("path") or "")
        if headers.get("origin"):
            self.seen_origins[headers["origin"][:120]] += 1
        if headers.get("host"):
            self.seen_hosts[headers["host"][:120]] += 1

        verdict = core.decide(kind=kind, method=method, path=path, headers=headers,
                              allowed_hosts=self.allowed_hosts, allowed_origins=self.allowed_origins,
                              trusted_proxy=peer in self.trusted_proxies,
                              default_port=self.listen_port, check_host=True)
        if verdict.allow:
            return await self.app(scope, receive, send)

        enforce = mode == "enforce" and (verdict.rule != "host" or self._host_mode == "enforce")
        if not enforce:
            await self._emit("auth.would_refuse", verdict, method, path, headers, peer)
            return await self.app(scope, receive, send)

        await self._emit("auth.refused", verdict, method, path, headers, peer)
        if kind == "websocket":
            msg = await receive()          # websocket.connect
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        status = 421 if verdict.rule == "host" else 403
        body = json.dumps({"error": "cross-site request refused", "rule": verdict.rule,
                           "hint": "open Vera from its own address"}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
