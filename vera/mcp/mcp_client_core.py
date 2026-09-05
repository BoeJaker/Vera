"""
mcp_client_core.py — pure protocol logic for Vera's MCP *client*
================================================================

Vera has always been an MCP *server* (`/mcp/tools`, `/mcp/call`, and the stdio
shim in `vera/ide/vera_mcp_bridge.py`). The peer proxy in
capability_orchestration.py (`register_mcp_server`) talks Vera's own REST
dialect — it is for wiring one Vera to another Vera, and it cannot speak to a
third-party MCP server at all.

This module is the missing half: the wire format of *real* MCP, the JSON-RPC 2.0
protocol every ecosystem server actually speaks (n8n's MCP Server Trigger,
GitHub's, Slack's, …).

Everything here is **pure** — no I/O, no httpx, no app import — so it is
testable without booting Vera (`tests/test_mcp_client_core.py` imports it as
`vera.mcp.mcp_client_core`). The transports that use it live in
`mcp_client.py`.

Two transports are covered, because servers in the wild are split between them:

  sse             The 2024-11-05 transport. Client opens a long-lived GET on the
                  SSE URL; the server's first frame is an `endpoint` event
                  naming a *second*, session-scoped URL to POST requests to.
                  Replies arrive back on the original SSE stream, NOT as the
                  POST response — which is why a naive request/response client
                  hangs forever against these servers.

  streamable_http The 2025-03-26 transport. One URL; POST JSON-RPC and the reply
                  comes back either as plain JSON or as a short SSE stream, the
                  server's choice, signalled by the response Content-Type.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# The protocol revision we advertise. Servers negotiate down when they are
# older; we accept whatever they answer with rather than insisting.
PROTOCOL_VERSION = "2025-03-26"
FALLBACK_PROTOCOL_VERSION = "2024-11-05"

CLIENT_INFO = {"name": "vera", "version": "1.0.0"}

_CAP_SAFE = re.compile(r"[^a-z0-9_]+")


# ═════════════════════════════════════════════════════════════════════════════
#  JSON-RPC framing
# ═════════════════════════════════════════════════════════════════════════════

def jsonrpc_request(method: str, params: Optional[Dict[str, Any]],
                    req_id: Any) -> Dict[str, Any]:
    """A JSON-RPC 2.0 *request* (expects a reply keyed by the same id)."""
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def jsonrpc_notification(method: str,
                         params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A JSON-RPC 2.0 *notification* — no id, and no reply is expected.

    Sending an id here is a real bug against strict servers: they will answer,
    and a client waiting on the next id gets the wrong message.
    """
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def initialize_params(protocol_version: str = PROTOCOL_VERSION) -> Dict[str, Any]:
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "clientInfo": dict(CLIENT_INFO),
    }


def is_response_to(msg: Any, req_id: Any) -> bool:
    """True if `msg` is the JSON-RPC reply for `req_id`.

    Servers interleave their own requests and notifications on the same stream,
    so a client that assumes "next message is my answer" mis-pairs results.
    """
    return isinstance(msg, dict) and "id" in msg and msg.get("id") == req_id \
        and ("result" in msg or "error" in msg)


def rpc_error_text(msg: Dict[str, Any]) -> str:
    """Human-readable text for a JSON-RPC error object, or '' if not an error."""
    err = msg.get("error")
    if not isinstance(err, dict):
        return ""
    code = err.get("code", "?")
    text = err.get("message", "unknown error")
    data = err.get("data")
    out = f"MCP error {code}: {text}"
    if data not in (None, "", {}, []):
        out += f" ({json.dumps(data)[:200]})"
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  SSE decoding
# ═════════════════════════════════════════════════════════════════════════════

class SSEDecoder:
    """Incremental text/event-stream decoder.

    Feed it arbitrary chunks (network boundaries fall wherever they like) and it
    yields complete `(event, data)` pairs. Per the SSE spec an event is
    terminated by a blank line, multiple `data:` lines are joined with newlines,
    and a missing `event:` field means the default event name `message`.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._event: Optional[str] = None
        self._data: List[str] = []

    def feed(self, chunk: str) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        self._buf += chunk
        # Normalise CRLF/CR so line splitting is uniform.
        self._buf = self._buf.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            done = self._handle_line(line)
            if done is not None:
                out.append(done)
        return out

    def close(self) -> List[Tuple[str, str]]:
        """Finish decoding a stream that has ended.

        A body may end straight after its last `data:` line with no trailing
        newline and no blank-line terminator. That final line is still sitting
        in the buffer, so flushing without consuming it would emit the event
        with empty data — losing exactly the reply the caller is waiting on.
        """
        out: List[Tuple[str, str]] = []
        if self._buf:
            line, self._buf = self._buf, ""
            done = self._handle_line(line)
            if done is not None:
                out.append(done)
        tail = self._flush()
        if tail is not None:
            out.append(tail)
        return out

    def _handle_line(self, line: str) -> Optional[Tuple[str, str]]:
        if line == "":
            return self._flush()               # blank line terminates an event
        if line.startswith(":"):
            return None                        # comment / keep-alive ping
        if ":" in line:
            field, value = line.split(":", 1)
            value = value[1:] if value.startswith(" ") else value
        else:
            field, value = line, ""
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        # `id` and `retry` carry no meaning for our use.
        return None

    def _flush(self) -> Optional[Tuple[str, str]]:
        if not self._data and self._event is None:
            return None
        evt = self._event or "message"
        data = "\n".join(self._data)
        self._event, self._data = None, []
        return (evt, data)


def parse_sse(text: str) -> List[Tuple[str, str]]:
    """Decode a complete SSE payload in one go (convenience for tests + the
    short streams a streamable-http POST returns)."""
    dec = SSEDecoder()
    events = dec.feed(text)
    events.extend(dec.close())
    return events


def sse_json_messages(events: Iterable[Tuple[str, str]]) -> List[Any]:
    """Pull decodable JSON payloads out of decoded SSE events.

    Non-JSON frames (keep-alives, the `endpoint` handshake) are skipped rather
    than raising — they are normal traffic, not corruption.
    """
    out = []
    for _evt, data in events:
        if not data.strip():
            continue
        try:
            out.append(json.loads(data))
        except (ValueError, TypeError):
            continue
    return out


def resolve_endpoint_event(sse_url: str, data: str) -> str:
    """Absolutise the URL carried by the SSE transport's `endpoint` event.

    Servers may send a bare path (`/mcp/x/messages?sessionId=…`), a path-less
    query, or a full URL. Resolving against the SSE URL handles all three, and
    keeps the client working when the server sits behind a reverse proxy on a
    subpath.
    """
    data = (data or "").strip()
    if not data:
        return ""
    if urlparse(data).scheme in ("http", "https"):
        return data
    return urljoin(sse_url, data)


# ═════════════════════════════════════════════════════════════════════════════
#  Tools → Vera capabilities
# ═════════════════════════════════════════════════════════════════════════════

def cap_name(server_id: str, tool_name: str) -> str:
    """`<server>.<tool>` in Vera's capability namespace.

    Tool names from the wild contain characters Vera's registry and the DAG
    node syntax do not accept (spaces, slashes, dashes); normalise rather than
    registering a name that later fails to resolve.
    """
    srv = _CAP_SAFE.sub("_", (server_id or "mcp").strip().lower()).strip("_")
    tool = _CAP_SAFE.sub("_", (tool_name or "tool").strip().lower()).strip("_")
    return f"{srv or 'mcp'}.{tool or 'tool'}"


def tool_input_schema(tool: Dict[str, Any]) -> Dict[str, Any]:
    """The JSON Schema for a tool's arguments, normalised to an object schema.

    MCP calls it `inputSchema`; a few servers emit `input_schema`. Vera's cap
    registry and signature builders assume `{"type":"object","properties":{}}`,
    so anything else is coerced instead of being passed through to break later.
    """
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    out = {"type": "object", "properties": dict(schema.get("properties") or {})}
    req = schema.get("required")
    if isinstance(req, list) and req:
        out["required"] = [r for r in req if isinstance(r, str)]
    return out


def tool_description(tool: Dict[str, Any], server_label: str) -> str:
    """A Vera-style cap description, including the argument summary the LLM
    signature builders expect."""
    desc = (tool.get("description") or "").strip() or \
        f"MCP tool '{tool.get('name', '?')}'"
    schema = tool_input_schema(tool)
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if props:
        parts = []
        for key, spec in props.items():
            typ = (spec or {}).get("type", "any") if isinstance(spec, dict) else "any"
            parts.append(f"{key} ({typ}{'!' if key in required else ''})")
        desc += " Input: " + ", ".join(parts) + "."
    return f"{desc} [via MCP server '{server_label}']"


def unwrap_tool_result(result: Any) -> Any:
    """Turn an MCP `tools/call` result into something Vera can hand around.

    MCP returns `{content:[{type:'text',text:'…'}], isError:bool}`. Callers want
    the value, not the envelope — and when the text is JSON (which is what n8n
    workflows return) they want the parsed object, not a JSON string that every
    downstream DAG node then has to re-parse.
    """
    if not isinstance(result, dict):
        return result

    # `structuredContent` is the spec's typed channel; prefer it when present.
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]

    blocks = result.get("content")
    if not isinstance(blocks, list):
        return result

    texts: List[str] = []
    others: List[Any] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            texts.append(b.get("text") or "")
        else:
            others.append(b)

    if texts and not others:
        joined = "\n".join(texts).strip()
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return joined
    if others and not texts:
        return others
    return {"text": "\n".join(texts), "content": others} if others else result


def is_tool_error(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("isError"))


# ═════════════════════════════════════════════════════════════════════════════
#  Transport helpers
# ═════════════════════════════════════════════════════════════════════════════

def guess_transport(url: str) -> str:
    """Best guess of a server's transport from its URL alone.

    Only a hint for pre-filling the UI — `mcp_client.connect()` probes for real,
    because guessing wrong here would otherwise strand a working server.
    """
    path = urlparse(url or "").path.rstrip("/").lower()
    if path.endswith("/sse"):
        return "sse"
    return "streamable_http"


def auth_headers(token: str = "", header_name: str = "",
                 header_value: str = "") -> Dict[str, str]:
    """Build request headers for the two auth shapes n8n's MCP trigger offers
    (Bearer Auth and Header Auth)."""
    out: Dict[str, str] = {}
    if token:
        out["Authorization"] = f"Bearer {token}"
    if header_name and header_value:
        out[header_name] = header_value
    return out
