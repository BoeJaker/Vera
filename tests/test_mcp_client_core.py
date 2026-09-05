"""Tests for Vera's MCP client wire format (vera/mcp/mcp_client_core.py).

The weight is on the three things that fail *silently* rather than loudly:
SSE frame decoding across arbitrary chunk boundaries, pairing a JSON-RPC reply
to the request that asked for it, and unwrapping a tool result without
mangling it.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.mcp import mcp_client_core as mcc  # noqa: E402


# ── JSON-RPC framing ────────────────────────────────────────────────────────
def test_request_has_id_notification_does_not():
    req = mcc.jsonrpc_request("tools/list", {"cursor": "x"}, 7)
    assert req == {"jsonrpc": "2.0", "id": 7, "method": "tools/list",
                   "params": {"cursor": "x"}}
    # A notification carrying an id makes strict servers reply, which desyncs
    # the next request's response pairing.
    note = mcc.jsonrpc_notification("notifications/initialized")
    assert "id" not in note
    assert note["method"] == "notifications/initialized"


def test_omitted_params_are_not_sent_as_null():
    assert "params" not in mcc.jsonrpc_request("ping", None, 1)


def test_is_response_to_pairs_on_id_only():
    assert mcc.is_response_to({"jsonrpc": "2.0", "id": 3, "result": {}}, 3)
    assert mcc.is_response_to({"jsonrpc": "2.0", "id": 3, "error": {}}, 3)
    # Another request's reply must not be mistaken for ours.
    assert not mcc.is_response_to({"jsonrpc": "2.0", "id": 4, "result": {}}, 3)
    # A server-initiated notification has no id and is not anyone's reply.
    assert not mcc.is_response_to({"jsonrpc": "2.0", "method": "x"}, 3)
    # An id match without result/error is a *request* from the server, not a reply.
    assert not mcc.is_response_to({"jsonrpc": "2.0", "id": 3, "method": "x"}, 3)


def test_rpc_error_text():
    msg = {"error": {"code": -32601, "message": "Method not found"}}
    assert "32601" in mcc.rpc_error_text(msg)
    assert "Method not found" in mcc.rpc_error_text(msg)
    assert mcc.rpc_error_text({"result": {}}) == ""


# ── SSE decoding ────────────────────────────────────────────────────────────
def test_sse_decoder_basic_event():
    dec = mcc.SSEDecoder()
    out = dec.feed("event: endpoint\ndata: /messages?sessionId=abc\n\n")
    assert out == [("endpoint", "/messages?sessionId=abc")]


def test_sse_decoder_defaults_event_name_to_message():
    dec = mcc.SSEDecoder()
    assert dec.feed('data: {"a":1}\n\n') == [("message", '{"a":1}')]


def test_sse_decoder_survives_split_across_chunks():
    """Network chunks split wherever they like — including mid-line."""
    dec = mcc.SSEDecoder()
    assert dec.feed("event: mess") == []
    assert dec.feed("age\ndata: {\"x\"") == []
    assert dec.feed(":1}\n") == []
    assert dec.feed("\n") == [("message", '{"x":1}')]


def test_sse_decoder_joins_multiple_data_lines():
    dec = mcc.SSEDecoder()
    out = dec.feed("data: line1\ndata: line2\n\n")
    assert out == [("message", "line1\nline2")]


def test_sse_decoder_ignores_comments_and_handles_crlf():
    dec = mcc.SSEDecoder()
    out = dec.feed(": keep-alive\r\nevent: message\r\ndata: ok\r\n\r\n")
    assert out == [("message", "ok")]


def test_sse_decoder_handles_value_without_leading_space():
    dec = mcc.SSEDecoder()
    assert dec.feed("data:tight\n\n") == [("message", "tight")]


def test_parse_sse_recovers_final_event_without_trailing_blank_line():
    """A streamable-http reply body often ends right after the last data line;
    dropping that event would lose the actual response."""
    events = mcc.parse_sse('event: message\ndata: {"id":1,"result":{}}')
    assert events == [("message", '{"id":1,"result":{}}')]


def test_sse_json_messages_skips_non_json_frames():
    events = [("endpoint", "/m"), ("message", '{"id":1}'), ("message", "")]
    assert mcc.sse_json_messages(events) == [{"id": 1}]


# ── endpoint resolution ─────────────────────────────────────────────────────
def test_resolve_endpoint_event_relative_path():
    assert mcc.resolve_endpoint_event(
        "https://n8n.example.com/mcp/vera",
        "/mcp/vera/messages?sessionId=1"
    ) == "https://n8n.example.com/mcp/vera/messages?sessionId=1"


def test_resolve_endpoint_event_absolute_url_passes_through():
    url = "https://other.host/mcp/messages?sessionId=1"
    assert mcc.resolve_endpoint_event("https://n8n.example.com/mcp/vera", url) == url


def test_resolve_endpoint_event_keeps_proxy_subpath():
    """Behind a reverse proxy on a subpath, a bare query must stay under it."""
    got = mcc.resolve_endpoint_event(
        "https://host/n8n/mcp/vera", "?sessionId=9")
    assert got == "https://host/n8n/mcp/vera?sessionId=9"


def test_resolve_endpoint_event_empty():
    assert mcc.resolve_endpoint_event("https://h/mcp", "  ") == ""


# ── tools → capabilities ────────────────────────────────────────────────────
def test_cap_name_sanitises_hostile_tool_names():
    # Spaces/slashes/dashes would produce a cap name the DAG runner can't resolve.
    assert mcc.cap_name("n8n", "Send Slack Message") == "n8n.send_slack_message"
    assert mcc.cap_name("n8n", "get/thing-v2") == "n8n.get_thing_v2"
    assert mcc.cap_name("", "") == "mcp.tool"


def test_tool_input_schema_coerces_non_object_schema():
    assert mcc.tool_input_schema({"inputSchema": {"type": "string"}}) == \
        {"type": "object", "properties": {}}
    assert mcc.tool_input_schema({}) == {"type": "object", "properties": {}}


def test_tool_input_schema_accepts_snake_case_variant():
    schema = {"type": "object", "properties": {"a": {"type": "string"}},
              "required": ["a"]}
    got = mcc.tool_input_schema({"input_schema": schema})
    assert got["properties"] == {"a": {"type": "string"}}
    assert got["required"] == ["a"]


def test_tool_description_marks_required_args():
    tool = {"name": "run", "description": "Run it",
            "inputSchema": {"type": "object",
                            "properties": {"month": {"type": "string"},
                                           "dry": {"type": "boolean"}},
                            "required": ["month"]}}
    desc = mcc.tool_description(tool, "n8n")
    assert "month (string!)" in desc
    assert "dry (boolean)" in desc
    assert "n8n" in desc


# ── result unwrapping ───────────────────────────────────────────────────────
def test_unwrap_parses_json_text_block():
    """n8n workflows return JSON as text; handing a DAG a JSON *string* would
    force every downstream node to re-parse it."""
    result = {"content": [{"type": "text", "text": '{"pong": true}'}]}
    assert mcc.unwrap_tool_result(result) == {"pong": True}


def test_unwrap_keeps_plain_text_as_text():
    result = {"content": [{"type": "text", "text": "hello world"}]}
    assert mcc.unwrap_tool_result(result) == "hello world"


def test_unwrap_joins_multiple_text_blocks():
    result = {"content": [{"type": "text", "text": "a"},
                          {"type": "text", "text": "b"}]}
    assert mcc.unwrap_tool_result(result) == "a\nb"


def test_unwrap_prefers_structured_content():
    result = {"structuredContent": {"n": 1},
              "content": [{"type": "text", "text": "ignored"}]}
    assert mcc.unwrap_tool_result(result) == {"n": 1}


def test_unwrap_preserves_non_text_blocks():
    img = {"type": "image", "data": "…", "mimeType": "image/png"}
    assert mcc.unwrap_tool_result({"content": [img]}) == [img]


def test_unwrap_passes_through_non_dict():
    assert mcc.unwrap_tool_result("raw") == "raw"


def test_is_tool_error():
    assert mcc.is_tool_error({"isError": True, "content": []})
    assert not mcc.is_tool_error({"content": []})
    assert not mcc.is_tool_error("nope")


# ── transport helpers ───────────────────────────────────────────────────────
def test_guess_transport():
    assert mcc.guess_transport("https://h/mcp/x/sse") == "sse"
    assert mcc.guess_transport("https://h/mcp/x") == "streamable_http"


def test_auth_headers():
    assert mcc.auth_headers(token="t") == {"Authorization": "Bearer t"}
    assert mcc.auth_headers(header_name="X-Key", header_value="v") == {"X-Key": "v"}
    assert mcc.auth_headers() == {}
    # A header name with no value must not produce a header with an empty value.
    assert mcc.auth_headers(header_name="X-Key") == {}


def test_initialize_params_shape():
    p = mcc.initialize_params()
    assert p["protocolVersion"] == mcc.PROTOCOL_VERSION
    assert p["clientInfo"]["name"] == "vera"
    assert "tools" in p["capabilities"]
    # Must be serialisable — it goes straight onto the wire.
    json.dumps(p)
