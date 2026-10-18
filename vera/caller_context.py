"""Caller context at the edge: a pure ASGI middleware that reads the surface header into a context variable.

Hosts in the UI (the desk, the dashboard, panels, the chat, the LHM) send

    X-Vera-Surface: <surface>; trigger=<auto|person>

on the capability calls they make (B0.11 R0). Non-browser callers may also send a caller token,
`Authorization: Bearer vat_...` (B0.3b R2): when a token service is given, a valid token sets the token
variable to {id, kind, subject, scopes} for the request. Nothing is refused here: an invalid or missing token is
only counted (auth_tokens). A browser WebSocket cannot set headers, so a WebSocket may pass
`?surface=` and `&trigger=` instead. The value is parsed by caller_context_core.parse_surface and held in the
context variable given to the middleware for the duration of the request, where the dispatcher's usage counter
reads it. Pure ASGI (not @APP.middleware) so it sees WebSocket handshakes and never buffers a streamed body.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional
from urllib.parse import parse_qs

try:
    from Vera.vera import caller_context_core as core
except ImportError:  # tests that put the worktree root on sys.path
    from vera import caller_context_core as core  # type: ignore

HEADER = b"x-vera-surface"


class CallerSurface:
    def __init__(self, app, var: ContextVar, token_var: Optional[ContextVar] = None, tokens: Any = None):
        self.app = app
        self.var = var
        self.token_var = token_var
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        value = ""
        headers = {}
        for k, v in scope.get("headers") or []:
            try:
                headers[k.decode("latin-1").lower()] = v.decode("latin-1", "replace")
            except Exception:
                continue
        value = headers.get(HEADER.decode(), "")
        ttoken = None
        if self.tokens is not None and self.token_var is not None:
            client = scope.get("client") or ("", 0)
            info, _ = await self.tokens.check(headers, str(client[0] if client else ""))
            if info:
                ttoken = self.token_var.set(info)
        if not value and kind == "websocket":
            q = parse_qs((scope.get("query_string") or b"").decode("latin-1", "replace"))
            if q.get("surface"):
                value = q["surface"][0] + (f"; trigger={q['trigger'][0]}" if q.get("trigger") else "")
        token = self.var.set(core.parse_surface(value)) if value else None
        try:
            return await self.app(scope, receive, send)
        finally:
            if token is not None:
                self.var.reset(token)
            if ttoken is not None:
                self.token_var.reset(ttoken)
