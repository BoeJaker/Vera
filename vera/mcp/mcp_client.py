"""
mcp_client.py — Vera's real MCP (Model Context Protocol) client
===============================================================

Speaks the actual MCP JSON-RPC 2.0 protocol to third-party MCP servers, over
both transports currently in the wild. Wire-format details live in
`mcp_client_core.py` (pure, unit-tested); this file is the I/O around them.

Deliberately depends on **httpx only** — no Vera app import — so it stays a
reusable library and can be exercised without booting the orchestrator. The
code that turns discovered tools into Vera capabilities lives with the callers
(`vera/n8n/n8n_capabilities.py`, `mcp.catalog.connect`).

    async with MCPClient("https://n8n.example.com/mcp/vera", token="…") as c:
        tools = await c.list_tools()
        out   = await c.call_tool("run_report", {"month": "2026-09"})

Transport is auto-detected: streamable-HTTP is tried first (it is the current
spec and a single round trip), falling back to SSE when the server rejects it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from .mcp_client_core import (
    FALLBACK_PROTOCOL_VERSION, PROTOCOL_VERSION, SSEDecoder,
    auth_headers, initialize_params, is_response_to, is_tool_error,
    jsonrpc_notification, jsonrpc_request, parse_sse, resolve_endpoint_event,
    rpc_error_text, sse_json_messages, unwrap_tool_result,
)

log = logging.getLogger("vera.mcp.client")

DEFAULT_TIMEOUT = 60.0
# The SSE transport holds a GET open for the whole session, so it must have no
# read timeout — a normal timeout would tear down a healthy idle connection.
SSE_CONNECT_TIMEOUT = 20.0


class MCPError(RuntimeError):
    """An MCP-level failure (protocol error, transport error, or tool error)."""


class MCPClient:
    """A connected MCP session against one server."""

    def __init__(self, url: str, *, transport: str = "auto",
                 token: str = "", header_name: str = "", header_value: str = "",
                 timeout: float = DEFAULT_TIMEOUT, verify: bool = True) -> None:
        self.url = (url or "").strip()
        self.transport = transport or "auto"
        self.timeout = timeout
        self.verify = verify
        self._headers = auth_headers(token, header_name, header_value)

        self._client: Optional[httpx.AsyncClient] = None
        self._session_id = ""            # streamable-http: Mcp-Session-Id
        self._message_url = ""           # sse: POST target from the endpoint event
        self._protocol = PROTOCOL_VERSION
        self._server_info: Dict[str, Any] = {}
        self._next_id = 0

        # SSE plumbing: replies arrive on a background stream, not as the POST
        # response, so requests park on futures until the reader wakes them.
        self._pending: Dict[Any, "asyncio.Future[Dict[str, Any]]"] = {}
        self._reader: Optional[asyncio.Task] = None
        self._endpoint_ready = asyncio.Event()
        self._reader_error: Optional[BaseException] = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _base_headers(self) -> Dict[str, str]:
        h = dict(self._headers)
        h["Content-Type"] = "application/json"
        # Accept both: the server picks, and we handle whichever comes back.
        h["Accept"] = "application/json, text/event-stream"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def connect(self) -> Dict[str, Any]:
        """Open the session and complete the MCP initialize handshake."""
        if not self.url:
            raise MCPError("no MCP server URL")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=SSE_CONNECT_TIMEOUT),
            verify=self.verify, follow_redirects=True)

        order = ([self.transport] if self.transport in ("sse", "streamable_http")
                 else ["streamable_http", "sse"])
        last: Optional[Exception] = None
        for t in order:
            try:
                if t == "sse":
                    await self._connect_sse()
                else:
                    await self._connect_streamable()
                self.transport = t
                log.info("mcp client connected: %s via %s (server=%s)",
                         self.url, t, self._server_info.get("name", "?"))
                return self._server_info
            except Exception as e:                      # try the other transport
                last = e
                log.debug("mcp %s transport failed for %s: %s", t, self.url, e)
                await self._reset_between_attempts()

        await self.close()
        raise MCPError(f"could not connect to MCP server {self.url}: {last}")

    async def _reset_between_attempts(self) -> None:
        """Drop per-transport state so a fallback attempt starts clean."""
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        self._pending.clear()
        self._endpoint_ready = asyncio.Event()
        self._reader_error = None
        self._session_id = ""
        self._message_url = ""

    async def close(self) -> None:
        await self._reset_between_attempts()
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ── streamable-HTTP transport (2025-03-26) ───────────────────────────────

    async def _connect_streamable(self) -> None:
        init = jsonrpc_request("initialize", initialize_params(self._protocol),
                               self._new_id())
        msg = await self._post_streamable(init, capture_session=True)
        if msg.get("error"):
            # Older servers reject the newer protocol revision by version, not
            # by transport — retry once at the older revision before giving up.
            self._protocol = FALLBACK_PROTOCOL_VERSION
            init = jsonrpc_request("initialize",
                                   initialize_params(self._protocol), self._new_id())
            msg = await self._post_streamable(init, capture_session=True)
            if msg.get("error"):
                raise MCPError(rpc_error_text(msg))
        result = msg.get("result") or {}
        self._server_info = result.get("serverInfo") or {}
        self._protocol = result.get("protocolVersion") or self._protocol
        # The spec requires this notification before any other request.
        await self._post_streamable(
            jsonrpc_notification("notifications/initialized"), notify=True)

    async def _post_streamable(self, payload: Dict[str, Any], *,
                               capture_session: bool = False,
                               notify: bool = False) -> Dict[str, Any]:
        assert self._client is not None
        r = await self._client.post(self.url, headers=self._base_headers(),
                                    content=json.dumps(payload))
        if capture_session:
            sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code} from {self.url}: {r.text[:300]}")
        if notify:
            return {}                       # notifications carry no reply

        ctype = (r.headers.get("content-type") or "").lower()
        req_id = payload.get("id")
        if "text/event-stream" in ctype:
            for msg in sse_json_messages(parse_sse(r.text)):
                if is_response_to(msg, req_id):
                    return msg
            raise MCPError("no JSON-RPC reply in the server's event stream")
        body = r.text.strip()
        if not body:
            raise MCPError("empty reply from MCP server")
        msg = json.loads(body)
        if isinstance(msg, list):           # batched reply
            for m in msg:
                if is_response_to(m, req_id):
                    return m
            raise MCPError("no matching reply in batch")
        return msg

    # ── SSE transport (2024-11-05) ───────────────────────────────────────────

    async def _connect_sse(self) -> None:
        self._reader = asyncio.create_task(self._sse_reader())
        try:
            await asyncio.wait_for(self._endpoint_ready.wait(),
                                   timeout=SSE_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            if self._reader_error:
                raise MCPError(f"SSE stream failed: {self._reader_error}")
            raise MCPError("server sent no MCP 'endpoint' event — not SSE transport")

        msg = await self._request_sse(
            jsonrpc_request("initialize", initialize_params(self._protocol),
                            self._new_id()))
        if msg.get("error"):
            raise MCPError(rpc_error_text(msg))
        result = msg.get("result") or {}
        self._server_info = result.get("serverInfo") or {}
        self._protocol = result.get("protocolVersion") or self._protocol
        await self._send_sse(jsonrpc_notification("notifications/initialized"))

    async def _sse_reader(self) -> None:
        """Hold the SSE stream open, routing replies to their waiting futures."""
        assert self._client is not None
        headers = dict(self._headers)
        headers["Accept"] = "text/event-stream"
        headers["Cache-Control"] = "no-store"
        try:
            # read=None: an idle MCP session is normal, not a stalled connection.
            timeout = httpx.Timeout(None, connect=SSE_CONNECT_TIMEOUT)
            async with self._client.stream("GET", self.url, headers=headers,
                                           timeout=timeout) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:300].decode("utf-8", "replace")
                    raise MCPError(f"HTTP {resp.status_code} on SSE GET: {body}")
                dec = SSEDecoder()
                async for chunk in resp.aiter_text():
                    for evt, data in dec.feed(chunk):
                        self._handle_sse_event(evt, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._reader_error = e
            self._fail_pending(e)
            self._endpoint_ready.set()      # unblock connect() so it can report

    def _handle_sse_event(self, evt: str, data: str) -> None:
        if evt == "endpoint":
            self._message_url = resolve_endpoint_event(self.url, data)
            self._endpoint_ready.set()
            return
        try:
            msg = json.loads(data)
        except (ValueError, TypeError):
            return                          # keep-alives and comments
        if not isinstance(msg, dict):
            return
        fut = self._pending.get(msg.get("id"))
        if fut is not None and not fut.done() and \
                ("result" in msg or "error" in msg):
            fut.set_result(msg)

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(MCPError(f"MCP stream closed: {exc}"))
        self._pending.clear()

    async def _send_sse(self, payload: Dict[str, Any]) -> None:
        assert self._client is not None
        if not self._message_url:
            raise MCPError("no MCP message endpoint (handshake incomplete)")
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        r = await self._client.post(self._message_url, headers=headers,
                                    content=json.dumps(payload))
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code} posting to MCP endpoint: "
                           f"{r.text[:300]}")

    async def _request_sse(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        req_id = payload["id"]
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send_sse(payload)
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"timed out after {self.timeout}s waiting for "
                           f"{payload.get('method')}")
        finally:
            self._pending.pop(req_id, None)

    # ── requests ─────────────────────────────────────────────────────────────

    async def _request(self, method: str,
                       params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = jsonrpc_request(method, params, self._new_id())
        if self.transport == "sse":
            return await self._request_sse(payload)
        return await self._post_streamable(payload)

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Every tool the server exposes, following pagination to the end."""
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(50):                 # bound: a cursor loop must not hang
            params = {"cursor": cursor} if cursor else {}
            msg = await self._request("tools/list", params)
            if msg.get("error"):
                raise MCPError(rpc_error_text(msg))
            result = msg.get("result") or {}
            batch = result.get("tools")
            if isinstance(batch, list):
                tools.extend(t for t in batch if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                        *, raw: bool = False) -> Any:
        """Invoke a tool. Returns the unwrapped value unless `raw`."""
        msg = await self._request("tools/call",
                                  {"name": name, "arguments": arguments or {}})
        if msg.get("error"):
            raise MCPError(rpc_error_text(msg))
        result = msg.get("result") or {}
        if is_tool_error(result):
            raise MCPError(f"tool '{name}' failed: "
                           f"{json.dumps(unwrap_tool_result(result))[:400]}")
        return result if raw else unwrap_tool_result(result)

    @property
    def server_info(self) -> Dict[str, Any]:
        return dict(self._server_info)

    @property
    def protocol_version(self) -> str:
        return self._protocol


async def probe(url: str, *, token: str = "", header_name: str = "",
                header_value: str = "", transport: str = "auto",
                verify: bool = True, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Connect, list tools, disconnect — the "does this server work?" check.

    Used by the catalog's connect path and the n8n panel so an operator gets a
    real answer (tool names included) rather than a stored config they have no
    way to validate.
    """
    async with MCPClient(url, transport=transport, token=token,
                         header_name=header_name, header_value=header_value,
                         verify=verify, timeout=timeout) as c:
        tools = await c.list_tools()
        return {
            "ok": True,
            "transport": c.transport,
            "protocol": c.protocol_version,
            "server": c.server_info,
            "tools": [{"name": t.get("name", ""),
                       "description": (t.get("description") or "")[:300]}
                      for t in tools],
            "tool_count": len(tools),
        }
