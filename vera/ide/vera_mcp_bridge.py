#!/usr/bin/env python3
"""
vera_mcp_bridge.py  —  Expose Vera's capabilities to Claude Code as an MCP server
=================================================================================
Vera speaks a *custom* REST MCP surface (GET /mcp/tools, POST /mcp/call), not the
native MCP stdio JSON-RPC protocol that Claude Code's `claude mcp add` expects.
This tiny, dependency-free shim bridges the two: Claude Code launches it over
stdio, and it forwards `tools/list` / `tools/call` to a running Vera server.

Deployed to remote hosts (~/.vera/vera_mcp_bridge.py) by ide.remote.bridge.install,
then registered with:

    claude mcp add vera -- python3 ~/.vera/vera_mcp_bridge.py \
        --url http://<vera-host>:<port> --allow ide.,fabric.,memory.,dream.,project.

so a remote Claude Code session can call back into Vera — record work, query
memory, run Vera IDE tools — i.e. "use the Vera IDE features from VSCode".

Stdlib only (urllib + json + sys) so it runs on any host with Python 3.7+.

Self-test (no stdio loop):
    python3 vera_mcp_bridge.py --url http://127.0.0.1:8000 --selftest
"""

import argparse
import json
import os
import ssl
import sys
import urllib.request
import urllib.error

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "vera", "version": "1.1.0"}


# ─────────────────────────────────────────────────────────────────────────────
# Vera REST client
# ─────────────────────────────────────────────────────────────────────────────
class Vera:
    def __init__(self, base_url, allow_prefixes, timeout=120, insecure=False):
        self.base = base_url.rstrip("/")
        self.allow = [p.strip() for p in (allow_prefixes or "").split(",") if p.strip()]
        self.timeout = timeout
        self._name_map = {}      # sanitized MCP name -> real Vera cap name
        # Vera serves HTTPS with an auto-generated self-signed cert when
        # TLS_ENABLED=1 — urllib rejects it by default. --insecure skips
        # verification (LAN use); it changes nothing for http:// URLs.
        self._ssl_ctx = None
        if insecure:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _open(self, req):
        return urllib.request.urlopen(req, timeout=self.timeout,
                                      context=self._ssl_ctx)

    def _get(self, path):
        req = urllib.request.Request(self.base + path, method="GET")
        with self._open(req) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with self._open(req) as r:
            return json.loads(r.read().decode("utf-8"))

    @staticmethod
    def _sanitize(cap_name):
        # MCP tool names should be [A-Za-z0-9_-]; Vera cap names use dots.
        return "".join(c if (c.isalnum() or c in "_-") else "_" for c in cap_name)

    def _allowed(self, name):
        if not self.allow:
            return True
        return any(name.startswith(p) for p in self.allow)

    def list_tools(self):
        """Return MCP-shaped tool descriptors; build the name map as a side effect."""
        raw = self._get("/mcp/tools")
        tools = []
        self._name_map = {}
        for entry in (raw or []):
            cap = entry.get("name", "")
            if not cap or not self._allowed(cap):
                continue
            mcp_name = self._sanitize(cap)
            # Avoid collisions after sanitising.
            if mcp_name in self._name_map and self._name_map[mcp_name] != cap:
                mcp_name = mcp_name + "_" + str(len(self._name_map))
            self._name_map[mcp_name] = cap
            schema = entry.get("schema") or {}
            if not isinstance(schema, dict) or not schema:
                schema = {"type": "object", "properties": {}}
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            desc = entry.get("description", "") or cap
            # Keep the original dotted name discoverable in the description.
            tools.append({
                "name": mcp_name,
                "description": ("[" + cap + "] " + desc)[:1024],
                "inputSchema": schema,
            })
        return tools

    def call_tool(self, mcp_name, arguments):
        cap = self._name_map.get(mcp_name)
        if not cap:
            # Map may be empty if tools/list wasn't called first — rebuild it.
            self.list_tools()
            cap = self._name_map.get(mcp_name, mcp_name)
        # caller_kind: "mcp" — the one honest, identifiable signal that this
        # call came from a Claude Code session rather than the browser chat
        # UI (which POSTs to this same /mcp/call endpoint but never sets
        # this). Read server-side into evolve.* run records' triggered_by.
        payload = {"name": cap, "arguments": arguments or {}, "caller_kind": "mcp"}
        # Best-effort Claude session id, if the launcher exposes one (env). Lets
        # server-side provenance stamp the EXACT session onto events (§5.1); when
        # absent, caller_kind alone still marks the call as Claude-driven.
        _sess = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("VERA_CLAUDE_SESSION") or ""
        if _sess:
            payload["session_id"] = _sess
        resp = self._post("/mcp/call", payload)
        # Vera wraps the real result under "content".
        if isinstance(resp, dict) and "content" in resp:
            return resp["content"]
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC / MCP stdio server (newline-delimited messages)
# ─────────────────────────────────────────────────────────────────────────────
def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(rpc_id, result):
    _send({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _error(rpc_id, code, message):
    _send({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def _as_text_content(value):
    """MCP tools/call must return {content: [{type:text,text}], isError}."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, default=str)
        except Exception:
            text = str(value)
    is_error = isinstance(value, dict) and bool(value.get("error"))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def serve(vera):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method", "")
        rpc_id = msg.get("id")
        is_notification = "id" not in msg

        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
                _result(rpc_id, {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                })
            elif method in ("notifications/initialized", "initialized"):
                pass  # notification — no response
            elif method == "ping":
                _result(rpc_id, {})
            elif method == "tools/list":
                _result(rpc_id, {"tools": vera.list_tools()})
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                try:
                    value = vera.call_tool(name, args)
                    _result(rpc_id, _as_text_content(value))
                except urllib.error.URLError as e:
                    _result(rpc_id, _as_text_content({"error": "Vera unreachable: %s" % e}))
                except Exception as e:
                    _result(rpc_id, _as_text_content({"error": "%s: %s" % (type(e).__name__, e)}))
            elif method in ("resources/list", "prompts/list"):
                key = "resources" if method.startswith("resources") else "prompts"
                _result(rpc_id, {key: []})
            elif is_notification:
                pass  # unknown notification — ignore
            else:
                _error(rpc_id, -32601, "Method not found: %s" % method)
        except BrokenPipeError:
            return
        except Exception as e:
            if not is_notification:
                _error(rpc_id, -32603, "Internal error: %s" % e)


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Vera MCP stdio bridge for Claude Code")
    ap.add_argument("--url", required=True, help="Base URL of the Vera server")
    ap.add_argument("--allow", default="",
                    help="CSV of cap-name prefixes to expose (empty = all), "
                         "e.g. 'ide.,fabric.,memory.'")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--insecure", action="store_true",
                    help="Skip TLS certificate verification (Vera's self-signed "
                         "https). No effect on http:// URLs.")
    ap.add_argument("--selftest", action="store_true",
                    help="Fetch the tool list and print a summary, then exit.")
    args = ap.parse_args()

    vera = Vera(args.url, args.allow, timeout=args.timeout, insecure=args.insecure)

    if args.selftest:
        try:
            tools = vera.list_tools()
        except Exception as e:
            print("SELFTEST FAILED: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            sys.exit(1)
        print("OK — %d tools exposed from %s (allow=%r)" % (len(tools), args.url, args.allow))
        for t in tools[:15]:
            print("  - %s  %s" % (t["name"], t["description"][:70]))
        if len(tools) > 15:
            print("  ... and %d more" % (len(tools) - 15))
        return

    serve(vera)


if __name__ == "__main__":
    main()
