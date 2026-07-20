"""
babblefish_capabilities.py — the `babblefish.*` capability group.
================================================================================

Babblefish makes Vera speak arbitrary network protocols through pluggable
protocol modules (see modules.py). Named after the Babel fish from *The
Hitchhiker's Guide to the Galaxy* — instant universal translation, but for the
wire instead of the ear.

Capabilities
────────────
  babblefish.modules          list the available protocol modules
  babblefish.encode           dry-run: turn a request into wire bytes (no send)
  babblefish.decode           parse raw wire bytes with a protocol module
  babblefish.speak            full round-trip: connect, send, decode the reply
  babblefish.listen           receive-only: read a banner / datagram (no request)
  babblefish.probe            identify the likely protocol on host[:port]
  babblefish.register_module  teach Babblefish a new protocol from a JSON spec

Panel
─────
  GET /babblefish/panel   a small console to try protocols by hand.

Safety
──────
This is a general-purpose protocol I/O toolkit for operating Vera's own network.
It performs ordinary client socket exchanges (the same thing curl/redis-cli do).
It is NOT a mass scanner — probe touches one host and a short, bounded port list.
"""

from __future__ import annotations

import asyncio
import binascii
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.responses import HTMLResponse

from Vera.vera.capability_orchestration import (
    APP, capability, register_ui,
)
# Absolute import — this file is loaded by basename (no package context) by the
# capability loader, so a relative `from . import modules` would fail. Importing
# the package by its full name runs babblefish/__init__.py in a proper package
# context, where ITS relative imports resolve correctly.
from Vera.vera.babblefish import modules as bf

log = logging.getLogger("vera.babblefish")
_HERE = Path(__file__).parent

# Common ports probe walks when no port is supplied. (protocol hint, port, transport)
_COMMON_PORTS = [
    ("http", 80, "tcp"), ("http", 8080, "tcp"), ("redis", 6379, "tcp"),
    ("line", 25, "tcp"), ("line", 22, "tcp"), ("whois", 43, "tcp"),
    ("dns", 53, "udp"),
]


def _wire_out(data: bytes) -> Dict[str, Any]:
    """Uniform representation of raw bytes for JSON transport."""
    return {
        "bytes": len(data),
        "hex": binascii.hexlify(data[:2048]).decode("ascii"),
        "text": data[:2048].decode("utf-8", "replace"),
    }


def _resolve_port(mod: bf.ProtocolModule, port: Optional[int]) -> Optional[int]:
    if port:
        return int(port)
    if mod and mod.default_port:
        return mod.default_port
    return None


async def _exchange(mod: bf.ProtocolModule, host: str, port: int, payload: bytes,
                    timeout: float, transport: str, expect_reply: bool = True) -> bytes:
    if transport == "udp":
        return await asyncio.to_thread(bf.udp_roundtrip, host, port, payload, timeout)
    return await asyncio.to_thread(bf.tcp_roundtrip, host, port, payload, timeout,
                                   65536, expect_reply)


# ─────────────────────────────────────────────────────────────────────────────
#  Introspection
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "babblefish.modules",
    http_method="GET", http_path="/babblefish/modules", http_tags=["babblefish"],
    memory="off", silent=True,
    description="List the pluggable protocol modules Babblefish can speak. Each: "
                "{name, aliases, transport(tcp|udp), default_port, description, "
                "example, declarative}. Call this FIRST to pick a protocol.",
)
async def cap_babblefish_modules(trace_id=None) -> Dict:
    mods = bf.list_modules()
    return {"count": len(mods), "modules": mods}


@capability(
    "babblefish.encode",
    http_method="POST", http_path="/babblefish/encode", http_tags=["babblefish"],
    memory="off", silent=True,
    description="Dry-run encoder: turn a high-level request into wire bytes for a "
                "protocol WITHOUT sending anything. Input: {protocol, request}. "
                "Output: {protocol, wire:{bytes,hex,text}}. Use to inspect exactly "
                "what would go on the wire before babblefish.speak.",
)
async def cap_babblefish_encode(protocol: str = "", request: Any = None,
                                trace_id=None) -> Dict:
    mod = bf.get_module(protocol)
    if not mod:
        return {"error": f"unknown protocol '{protocol}'",
                "known": [m["name"] for m in bf.list_modules()]}
    try:
        wire = mod.encode(request if request is not None else {})
    except Exception as e:
        return {"error": f"encode failed: {type(e).__name__}: {e}"}
    return {"protocol": mod.name, "transport": mod.transport, "wire": _wire_out(wire)}


@capability(
    "babblefish.decode",
    http_method="POST", http_path="/babblefish/decode", http_tags=["babblefish"],
    memory="off", silent=True,
    description="Decode raw wire bytes with a protocol module into a structured, "
                "readable result. Input: {protocol, data} where data is a string "
                "(hex or text) or {hex|b64|text}. Output: {protocol, decoded}.",
)
async def cap_babblefish_decode(protocol: str = "", data: Any = None,
                                trace_id=None) -> Dict:
    mod = bf.get_module(protocol)
    if not mod:
        return {"error": f"unknown protocol '{protocol}'",
                "known": [m["name"] for m in bf.list_modules()]}
    try:
        raw = bf.coerce_wire(data if data is not None else b"")
        decoded = mod.decode(raw)
    except Exception as e:
        return {"error": f"decode failed: {type(e).__name__}: {e}"}
    return {"protocol": mod.name, "decoded": decoded}


# ─────────────────────────────────────────────────────────────────────────────
#  Conversation
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "babblefish.speak",
    http_method="POST", http_path="/babblefish/speak", http_tags=["babblefish"],
    description="Speak a protocol: connect to host[:port], send an encoded "
                "request, and return the decoded reply. Input: {protocol, host, "
                "port?(module default), request, timeout?=5, transport?(module "
                "default)}. Output: {protocol, host, port, request_wire, decoded, "
                "reply_wire}. Call babblefish.modules first to learn each "
                "protocol's request shape.",
)
async def cap_babblefish_speak(protocol: str = "", host: str = "", port: int = 0,
                               request: Any = None, timeout: float = 5.0,
                               transport: str = "", profile: Any = None,
                               trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    mod = bf.get_module(protocol)
    if not mod:
        return {"error": f"unknown protocol '{protocol}'",
                "known": [m["name"] for m in bf.list_modules()]}
    p = _resolve_port(mod, port)
    if not p:
        return {"error": f"port required (protocol '{mod.name}' has no default)"}
    tr = (transport or mod.transport).lower()
    req = request if request is not None else {}
    # One-shot mimicry: apply a persona without opening a session by running the
    # module's contextualize() against a throwaway (never-opened) context.
    if profile is not None:
        try:
            _ctx = bf.ConnectionContext(mod, host, p, tr, float(timeout), profile)
            req = mod.contextualize(req, _ctx)
        except Exception:
            pass
    try:
        payload = mod.encode(req)
    except Exception as e:
        return {"error": f"encode failed: {type(e).__name__}: {e}"}
    try:
        reply = await _exchange(mod, host, p, payload, float(timeout), tr,
                                expect_reply=mod.expect_reply)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "protocol": mod.name,
                "host": host, "port": p}
    try:
        decoded = mod.decode(reply)
    except Exception as e:
        decoded = {"decode_error": f"{type(e).__name__}: {e}", **_wire_out(reply)}
    return {"protocol": mod.name, "host": host, "port": p, "transport": tr,
            "request_wire": _wire_out(payload), "decoded": decoded,
            "reply_wire": _wire_out(reply)}


@capability(
    "babblefish.listen",
    http_method="POST", http_path="/babblefish/listen", http_tags=["babblefish"],
    description="Receive-only: connect and read whatever the peer sends first (a "
                "banner) without sending a request — good for SMTP/SSH/FTP-style "
                "greeting grabs. Input: {host, port, protocol?(for decoding), "
                "timeout?=5, transport?=tcp}. Output: {decoded, reply_wire}.",
)
async def cap_babblefish_listen(host: str = "", port: int = 0, protocol: str = "",
                                timeout: float = 5.0, transport: str = "tcp",
                                trace_id=None) -> Dict:
    if not host or not port:
        return {"error": "host and port required"}
    tr = (transport or "tcp").lower()
    mod = bf.get_module(protocol) if protocol else None
    try:
        if tr == "udp":
            # No unsolicited receive for UDP without a send; send an empty datagram.
            reply = await asyncio.to_thread(bf.udp_roundtrip, host, int(port), b"", float(timeout))
        else:
            reply = await asyncio.to_thread(bf.tcp_roundtrip, host, int(port), b"",
                                            float(timeout), 65536, True)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "host": host, "port": port}
    decoded = None
    if mod:
        try:
            decoded = mod.decode(reply)
        except Exception as e:
            decoded = {"decode_error": str(e)}
    return {"host": host, "port": int(port), "protocol": (mod.name if mod else None),
            "decoded": decoded, "reply_wire": _wire_out(reply)}


@capability(
    "babblefish.probe",
    http_method="POST", http_path="/babblefish/probe", http_tags=["babblefish"],
    description="Identify the likely protocol on a host. Input: {host, port?, "
                "timeout?=3}. With a port it tries the modules whose default port "
                "matches (plus a banner grab) and ranks guesses by evidence. "
                "Without a port it checks a SHORT bounded list of common ports. "
                "Output: {host, findings:[{port, transport, guess, open, evidence}]}.",
)
async def cap_babblefish_probe(host: str = "", port: int = 0, timeout: float = 3.0,
                               trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    to = float(timeout)
    targets: List[Any] = []
    if port:
        # Prefer modules whose default port matches; always include a raw banner.
        matched = [m for m in bf.REGISTRY.values() if m.default_port == int(port)]
        seen = set()
        for m in matched:
            if m.name not in seen:
                targets.append((m.name, int(port), m.transport)); seen.add(m.name)
        targets.append(("__banner__", int(port), "tcp"))
    else:
        targets = list(_COMMON_PORTS)

    findings = []
    for hint, p, tr in targets:
        f = {"port": p, "transport": tr, "guess": None, "open": False, "evidence": ""}
        try:
            if hint == "__banner__":
                data = await asyncio.to_thread(bf.tcp_roundtrip, host, p, b"", to, 4096, True)
                f["open"] = True
                f["guess"] = _guess_from_banner(data) or "unknown"
                f["evidence"] = _preview(data)
            else:
                mod = bf.get_module(hint)
                payload = _probe_payload(mod)
                data = await _exchange(mod, host, p, payload, to, tr, expect_reply=True)
                f["open"] = True
                ok, ev = _score(mod, data)
                f["guess"] = mod.name if ok else (_guess_from_banner(data) or "unknown")
                f["evidence"] = ev
        except Exception as e:
            f["evidence"] = f"{type(e).__name__}: {e}"
        findings.append(f)
    return {"host": host, "findings": findings}


def _probe_payload(mod: bf.ProtocolModule) -> bytes:
    try:
        if mod.name == "http":
            return mod.encode({"method": "GET", "path": "/", "host": "probe"})
        if mod.name == "redis":
            return mod.encode(["PING"])
        if mod.name == "dns":
            return mod.encode({"name": "example.com", "type": "A"})
        if mod.name == "whois":
            return mod.encode("example.com")
        if mod.name == "line":
            return b""            # many line protocols greet first
    except Exception:
        pass
    return b""


def _score(mod: bf.ProtocolModule, data: bytes):
    """Return (looks_like_this_protocol, short_evidence)."""
    if not data:
        return False, "(no reply)"
    try:
        d = mod.decode(data)
    except Exception:
        return False, _preview(data)
    if mod.name == "http":
        return (d.get("status") is not None), d.get("status_line") or _preview(data)
    if mod.name == "redis":
        rep = d.get("reply")
        return (rep == "PONG" or isinstance(rep, dict) and "error" in rep), str(rep)[:80]
    if mod.name == "dns":
        return (d.get("answer_count") is not None), f"rcode={d.get('rcode')}"
    if mod.name == "whois":
        return (d.get("bytes", 0) > 0), _preview(data)
    return (len(data) > 0), _preview(data)


def _guess_from_banner(data: bytes) -> Optional[str]:
    head = data[:64].decode("iso-8859-1", "replace")
    up = head.upper()
    if up.startswith("HTTP/"):
        return "http"
    if up.startswith("SSH-"):
        return "ssh"
    if head.startswith("220") and "SMTP" in up:
        return "smtp"
    if head.startswith("220"):
        return "ftp/smtp"
    if head.startswith("+OK"):
        return "pop3"
    if head.startswith("* OK"):
        return "imap"
    if head.startswith("-") or head.startswith("+"):
        return "redis"
    return None


def _preview(data: bytes, n: int = 120) -> str:
    return data[:n].decode("utf-8", "replace").replace("\r", "\\r").replace("\n", "\\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Stateful connections — carry context across a live session
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "babblefish.connect",
    http_method="POST", http_path="/babblefish/connect", http_tags=["babblefish"],
    description="Open a PERSISTENT connection and keep it (unlike one-shot speak). "
                "Returns a session_id you then drive with babblefish.send / recv / "
                "close. The session carries connection context — cookies, negotiated "
                "features, the greeting banner — and a persona to mimic a real "
                "client. Input: {protocol, host, port?, timeout?=5, transport?, "
                "profile?(persona name or dict)}. Output: {session_id, info, greeting?}.",
)
async def cap_babblefish_connect(protocol: str = "", host: str = "", port: int = 0,
                                 timeout: float = 5.0, transport: str = "",
                                 profile: Any = None, trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    mod = bf.get_module(protocol)
    if not mod:
        return {"error": f"unknown protocol '{protocol}'",
                "known": [m["name"] for m in bf.list_modules()]}
    p = _resolve_port(mod, port)
    if not p:
        return {"error": f"port required (protocol '{mod.name}' has no default)"}
    try:
        conn = await asyncio.to_thread(bf.open_session, mod, host, p, transport,
                                       float(timeout), profile)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "host": host, "port": p}
    return {"session_id": conn.id, "info": conn.info(),
            "greeting": conn.state.get("greeting")}


@capability(
    "babblefish.send",
    http_method="POST", http_path="/babblefish/send", http_tags=["babblefish"],
    description="Send a request on an OPEN session (from babblefish.connect/adopt) "
                "and decode the reply. The module contextualises the request first "
                "(persona headers, carried cookies, session state). Input: "
                "{session_id, request, expect_reply?=true}. Output: {decoded, "
                "reply_wire, state_keys}.",
)
async def cap_babblefish_send(session_id: str = "", request: Any = None,
                              expect_reply: bool = True, trace_id=None) -> Dict:
    conn = bf.get_session(session_id)
    if not conn:
        return {"error": f"no such session '{session_id}' (open one with babblefish.connect)"}
    try:
        res = await asyncio.to_thread(conn.send, request if request is not None else {},
                                      bool(expect_reply))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "session_id": session_id}
    res["state_keys"] = sorted(conn.state.keys())
    return res


@capability(
    "babblefish.recv",
    http_method="POST", http_path="/babblefish/recv", http_tags=["babblefish"],
    description="Read from an open session WITHOUT sending (drain a banner or a "
                "server-pushed message). Input: {session_id, timeout?}. Output: "
                "{decoded, reply_wire}.",
)
async def cap_babblefish_recv(session_id: str = "", timeout: float = 0.0,
                              trace_id=None) -> Dict:
    conn = bf.get_session(session_id)
    if not conn:
        return {"error": f"no such session '{session_id}'"}
    try:
        return await asyncio.to_thread(conn.recv_only, (float(timeout) or None))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@capability(
    "babblefish.close",
    http_method="POST", http_path="/babblefish/close", http_tags=["babblefish"],
    memory="off",
    description="Close an open session. Input: {session_id}. Output: {closed}.",
)
async def cap_babblefish_close(session_id: str = "", trace_id=None) -> Dict:
    return {"closed": bf.close_session(session_id), "session_id": session_id}


@capability(
    "babblefish.sessions",
    http_method="GET", http_path="/babblefish/sessions", http_tags=["babblefish"],
    memory="off", silent=True,
    description="List open Babblefish sessions with their protocol, peer, persona, "
                "negotiated state keys and exchange count. Output: {count, sessions}.",
)
async def cap_babblefish_sessions(trace_id=None) -> Dict:
    s = bf.list_sessions()
    return {"count": len(s), "sessions": s}


# ─────────────────────────────────────────────────────────────────────────────
#  Fingerprint & adopt — understand an environment, then drop into it
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "babblefish.fingerprint",
    http_method="POST", http_path="/babblefish/fingerprint", http_tags=["babblefish"],
    description="Interrogate a peer and return an environment profile (banner, "
                "server, version, advertised features) so Vera understands what it "
                "is talking to. Deeper than probe. Input: {host, port?, protocol? "
                "(else inferred from port), timeout?=4, transport?}. Output: the "
                "protocol's fingerprint dict.",
)
async def cap_babblefish_fingerprint(host: str = "", port: int = 0, protocol: str = "",
                                     timeout: float = 4.0, transport: str = "",
                                     trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    mod = bf.get_module(protocol) if protocol else _infer_module(port)
    if not mod:
        return {"error": "could not determine protocol — pass 'protocol' explicitly",
                "known": [m["name"] for m in bf.list_modules()]}
    p = _resolve_port(mod, port)
    if not p:
        return {"error": f"port required for protocol '{mod.name}'"}

    def _do():
        conn = bf.ConnectionContext(mod, host, p, transport, float(timeout))
        conn.open()
        try:
            return conn.fingerprint()
        finally:
            conn.close()
    try:
        return await asyncio.to_thread(_do)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "host": host, "port": p}


@capability(
    "babblefish.adopt",
    http_method="POST", http_path="/babblefish/adopt", http_tags=["babblefish"],
    description="DROP INTO an environment: open a persistent session, fingerprint "
                "the peer, and (for HTTP) auto-derive a matching client persona so "
                "follow-up traffic mimics a real client. Leaves the session OPEN for "
                "babblefish.send. Input: {host, port?, protocol?(else inferred), "
                "timeout?=5, profile?(override the learned persona)}. Output: "
                "{session_id, fingerprint, profile, info}.",
)
async def cap_babblefish_adopt(host: str = "", port: int = 0, protocol: str = "",
                               timeout: float = 5.0, profile: Any = None,
                               trace_id=None) -> Dict:
    if not host:
        return {"error": "host required"}
    mod = bf.get_module(protocol) if protocol else _infer_module(port)
    if not mod:
        return {"error": "could not determine protocol — pass 'protocol' explicitly",
                "known": [m["name"] for m in bf.list_modules()]}
    p = _resolve_port(mod, port)
    if not p:
        return {"error": f"port required for protocol '{mod.name}'"}

    def _do():
        conn = bf.open_session(mod, host, p, "", float(timeout), profile)
        fp = conn.fingerprint()
        # Auto-mimicry: if the caller didn't pin a persona, learn one that matches
        # the fingerprinted environment and wear it for subsequent sends.
        if not profile:
            learned = bf.learn_profile(f"adopted:{host}", {**fp, "host": host})
            conn.profile = learned
        return conn, fp
    try:
        conn, fp = await asyncio.to_thread(_do)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "host": host, "port": p}
    return {"session_id": conn.id, "fingerprint": fp,
            "profile": conn.profile.get("description") if isinstance(conn.profile, dict) else None,
            "info": conn.info()}


@capability(
    "babblefish.profiles",
    http_method="GET", http_path="/babblefish/profiles", http_tags=["babblefish"],
    memory="off", silent=True,
    description="List the client personas Babblefish can wear to mimic real "
                "clients (curl, chrome, firefox, googlebot, learned:*). Output: "
                "{count, profiles:{name: description}}.",
)
async def cap_babblefish_profiles(trace_id=None) -> Dict:
    return {"count": len(bf.PROFILES),
            "profiles": {k: v.get("description", "") for k, v in bf.PROFILES.items()}}


@capability(
    "babblefish.learn_profile",
    http_method="POST", http_path="/babblefish/learn_profile", http_tags=["babblefish"],
    description="Derive and store a reusable client persona from an environment "
                "fingerprint so later traffic to similar peers blends in. Input: "
                "{name, fingerprint} (fingerprint from babblefish.fingerprint). "
                "Output: {registered, profile}.",
)
async def cap_babblefish_learn_profile(name: str = "", fingerprint: Dict = None,
                                       trace_id=None) -> Dict:
    if not name:
        return {"error": "name required"}
    prof = bf.learn_profile(name, fingerprint or {})
    return {"registered": name, "profile": prof}


def _infer_module(port: int):
    """Best-effort module pick from a port number alone (for fingerprint/adopt)."""
    if not port:
        return None
    for m in bf.REGISTRY.values():
        if m.default_port == int(port):
            return m
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Extensibility
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "babblefish.register_module",
    http_method="POST", http_path="/babblefish/register_module", http_tags=["babblefish"],
    description="Teach Babblefish a new protocol from a declarative JSON spec (no "
                "code). spec: {name, transport?(tcp|udp), default_port?, "
                "description?, framing?(line|raw), terminator?, request_template? "
                "(str with {field} placeholders), response?(text|hex|lines)}. "
                "Returns the registered module's description.",
)
async def cap_babblefish_register_module(spec: Dict = None, trace_id=None) -> Dict:
    if not spec or not isinstance(spec, dict) or not spec.get("name"):
        return {"error": "spec dict with a 'name' is required"}
    try:
        mod = bf.register_declarative(spec)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"registered": mod.name, "module": mod.describe()}


# ─────────────────────────────────────────────────────────────────────────────
#  PANEL
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/babblefish/panel", include_in_schema=False)
async def _babblefish_panel():
    p = _HERE / "babblefish_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>babblefish_panel.html not found</p>")


register_ui(
    "babblefish",
    "Babblefish",
    "🐟",
    """<div id="bf-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/babblefish/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#181614)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=["babblefish.modules", "babblefish.speak", "babblefish.listen",
             "babblefish.decode", "babblefish.encode", "babblefish.probe",
             "babblefish.register_module", "babblefish.connect", "babblefish.send",
             "babblefish.recv", "babblefish.close", "babblefish.sessions",
             "babblefish.fingerprint", "babblefish.adopt", "babblefish.profiles",
             "babblefish.learn_profile"],
    mode="inject",
    tab_order=58,
)

log.info("babblefish_capabilities ready — %d protocol modules", len(bf.list_modules()))
