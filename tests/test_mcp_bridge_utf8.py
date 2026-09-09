"""The MCP stdio bridge must read and write UTF-8 whatever the launcher's locale.

Observed 2026-09-09: Claude Code on Windows launches vera_mcp_bridge.py with a
piped stdin, and Windows Python decodes a pipe with the ANSI code page (cp1252)
unless PYTHONUTF8 / PYTHONIOENCODING say otherwise. Every non-ASCII character
in a tool argument therefore arrived as its mojibake and was forwarded to Vera
that way: two source files written through the bridge landed on disk
double-encoded (U+2022 stored as three code points), and a heredoc carrying a
bullet failed to parse on the host. The bridge now forces UTF-8 on both streams
itself, because the protocol is UTF-8 by spec and the environment is not ours.
"""
import importlib.util
import io
import json
import sys
from pathlib import Path

_PATH = Path(__file__).parents[1] / "vera" / "ide" / "vera_mcp_bridge.py"
_SPEC = importlib.util.spec_from_file_location("vera_mcp_bridge_utf8_under_test", _PATH)
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

TEXT = "echo • é — ”"          # bullet, e-acute, em dash, right quote


def _piped_like_windows(raw: bytes) -> io.TextIOWrapper:
    """A stdin the way Windows Python hands it to a piped process: cp1252."""
    return io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252", errors="surrogateescape")


def test_the_defect_is_real_without_the_fix():
    # Documents the failure mode so nobody 'simplifies' the fix away: the same
    # UTF-8 bytes read through a cp1252 wrapper are not the text that was sent.
    line = _piped_like_windows((TEXT + "\n").encode("utf-8")).readline().rstrip("\n")
    assert line != TEXT


def test_force_utf8_stdio_reconfigures_a_cp1252_pipe(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _piped_like_windows((TEXT + "\n").encode("utf-8")))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    bridge._force_utf8_stdio()
    assert sys.stdin.encoding.lower().replace("-", "") == "utf8"
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    assert sys.stdin.readline().rstrip("\n") == TEXT


def test_serve_forwards_non_ascii_tool_arguments_intact(monkeypatch):
    """End to end through serve(): a cp1252-configured stdin, a real JSON-RPC
    tools/call carrying non-ASCII, and the Vera stub must receive the real text."""
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": "evolve_sandbox_exec", "arguments": {"cmd": TEXT}}}
    raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", _piped_like_windows(raw))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="cp1252"))

    class Stub:
        calls = []

        def call_tool(self, name, args):
            self.calls.append((name, args))
            return {"ok": True, "echo": args.get("cmd")}

    stub = Stub()
    bridge.serve(stub)                     # serve() forces UTF-8 itself
    assert stub.calls == [("evolve_sandbox_exec", {"cmd": TEXT})]
    sys.stdout.flush()
    reply = json.loads(out.getvalue().decode("utf-8").strip())
    assert reply["id"] == 7
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["echo"] == TEXT


def test_force_utf8_stdio_tolerates_a_non_reconfigurable_stream(monkeypatch):
    # pytest's capture, a StringIO, py<3.7: no reconfigure() - must not raise.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    bridge._force_utf8_stdio()
