"""Caller context at the edge: a pure ASGI middleware that reads the surface header into a context variable.

Hosts in the UI (the desk, the dashboard, panels, the chat, the LHM) send

    X-Vera-Surface: <surface>; trigger=<auto|person>

on the capability calls they make. A browser WebSocket cannot set headers, so a WebSocket may pass
`?surface=` and `&trigger=` instead. The value is parsed by caller_context_core.parse_surface and held in the
context variable given to the middleware for the duration of the request, where the dispatcher's usage counter
reads it. Pure ASGI (not @APP.middleware) so it sees WebSocket handshakes and never buffers a streamed body.
"""
from __future__ import annotations

from contextvars import ContextVar
from urllib.parse import parse_qs

try:
    from Vera.vera import caller_context_core as core
except ImportError:  # tests that put the worktree root on sys.path
    from vera import caller_context_core as core  # type: ignore

HEADER = b"x-vera-surface"


class CallerSurface:
    def __init__(self, app, var: ContextVar):
        self.app = app
        self.var = var

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        value = ""
        for k, v in scope.get("headers") or []:
            if k.lower() == HEADER:
                value = v.decode("latin-1", "replace")
                break
        if not value and kind == "websocket":
            q = parse_qs((scope.get("query_string") or b"").decode("latin-1", "replace"))
            if q.get("surface"):
                value = q["surface"][0] + (f"; trigger={q['trigger'][0]}" if q.get("trigger") else "")
        if not value:
            return await self.app(scope, receive, send)
        token = self.var.set(core.parse_surface(value))
        try:
            return await self.app(scope, receive, send)
        finally:
            self.var.reset(token)
