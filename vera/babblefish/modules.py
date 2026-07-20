"""
babblefish/modules.py — the pluggable protocol-module framework + built-ins.
================================================================================

A *protocol module* teaches Babblefish one networking language. Each module knows
how to:

  • encode(request)  — turn a high-level request (dict/str) into wire bytes
  • decode(data)     — turn wire bytes back into a structured, human-readable dict
  • describe()       — advertise itself (name, transport, default port, examples)

Everything is stdlib-only and synchronous at the socket layer; the capability
layer wraps the blocking I/O in `asyncio.to_thread` so it never stalls the event
loop. TLS is intentionally out of scope for the built-ins (add a module for it).

Two ways to add a protocol:
  1. Subclass `ProtocolModule` (for real logic) and `register(MyModule())`.
  2. Register a DECLARATIVE spec at runtime via `register_declarative(spec)` —
     no code, just framing rules + a request template. This is what the
     `babblefish.register_module` capability uses, so the LLM can teach itself a
     new line/length/http-framed protocol on the fly without arbitrary code exec.
"""

from __future__ import annotations

import base64
import binascii
import re
import socket
import struct
from typing import Any, Dict, List, Optional, Union

Request = Union[str, bytes, Dict[str, Any]]


# ─────────────────────────────────────────────────────────────────────────────
#  Low-level socket round-trips (blocking — call via asyncio.to_thread)
# ─────────────────────────────────────────────────────────────────────────────
def tcp_roundtrip(host: str, port: int, payload: bytes, timeout: float = 5.0,
                  read_max: int = 65536, expect_reply: bool = True) -> bytes:
    """Open TCP, send `payload`, read up to `read_max` bytes, close. Returns the
    raw reply (possibly empty). `expect_reply=False` sends and returns at once."""
    data = bytearray()
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        if payload:
            s.sendall(payload)
        if not expect_reply:
            return bytes(data)
        try:
            while len(data) < read_max:
                chunk = s.recv(min(4096, read_max - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        except socket.timeout:
            pass                       # partial reads are normal for streamed protocols
    return bytes(data)


def udp_roundtrip(host: str, port: int, payload: bytes, timeout: float = 5.0,
                  read_max: int = 65536) -> bytes:
    """Send a UDP datagram and wait once for a reply."""
    fam = socket.AF_INET
    try:
        info = socket.getaddrinfo(host, int(port), 0, socket.SOCK_DGRAM)
        fam = info[0][0]
    except Exception:
        pass
    with socket.socket(fam, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(payload, (host, int(port)))
        try:
            data, _ = s.recvfrom(read_max)
            return data
        except socket.timeout:
            return b""


def _as_bytes(x: Any) -> bytes:
    if isinstance(x, bytes):
        return x
    return str(x).encode("utf-8", "replace")


def _hexdump(data: bytes, limit: int = 512) -> str:
    return binascii.hexlify(data[:limit]).decode("ascii")


def _preview_text(data: bytes, limit: int = 2048) -> str:
    return data[:limit].decode("utf-8", "replace")


def coerce_wire(value: Any) -> bytes:
    """Interpret a caller-supplied wire value: dict {hex|b64|text}, or a str that
    may be hex/base64/plain. Used by decode() and raw send paths."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, dict):
        if "hex" in value:
            return binascii.unhexlify(re.sub(r"\s+", "", str(value["hex"])))
        if "b64" in value or "base64" in value:
            return base64.b64decode(str(value.get("b64") or value.get("base64")))
        if "text" in value:
            return _as_bytes(value["text"])
        return _as_bytes(value)
    s = str(value).strip()
    # Heuristic: an even-length all-hex string is treated as hex bytes.
    if s and len(s) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", s):
        try:
            return binascii.unhexlify(s)
        except Exception:
            pass
    return _as_bytes(s)


# ─────────────────────────────────────────────────────────────────────────────
#  Base module
# ─────────────────────────────────────────────────────────────────────────────
class ProtocolModule:
    name: str = "base"
    aliases: List[str] = []
    transport: str = "tcp"                 # "tcp" | "udp"
    default_port: int = 0
    description: str = ""
    example: Optional[Dict[str, Any]] = None
    expect_reply: bool = True
    # True when the peer sends an unsolicited greeting/banner on connect (SMTP,
    # FTP, SSH, IRC…). A stateful Connection reads it into ctx on open so the
    # module "drops into" the conversation already aware of the environment.
    greets_first: bool = False

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "transport": self.transport,
            "default_port": self.default_port,
            "description": self.description,
            "example": self.example,
            "greets_first": self.greets_first,
            "stateful": type(self).contextualize is not ProtocolModule.contextualize
                        or type(self).fingerprint is not ProtocolModule.fingerprint,
            "declarative": isinstance(self, DeclarativeModule),
        }

    # Subclasses override these two.
    def encode(self, request: Request) -> bytes:
        return coerce_wire(request)

    def decode(self, data: bytes) -> Dict[str, Any]:
        return {"bytes": len(data), "text": _preview_text(data), "hex": _hexdump(data)}

    # ── Connection-context hooks (all optional; defaults are no-ops) ─────────
    # These are what let Babblefish MIMIC a real client and carry state across a
    # live connection instead of firing isolated one-shots.
    def contextualize(self, request: Request, ctx: "ConnectionContext") -> Request:
        """Adapt an outgoing request using the connection's persona + negotiated
        state (e.g. inject session cookies, a matching User-Agent, an auth token).
        Return the (possibly modified) request; default passes it through."""
        return request

    def on_response(self, ctx: "ConnectionContext", decoded: Dict[str, Any],
                    raw: bytes) -> None:
        """Update negotiated connection state from a decoded reply (e.g. capture
        Set-Cookie, server banner, advertised features). Default: no-op."""
        return None

    def fingerprint(self, ctx: "ConnectionContext") -> Dict[str, Any]:
        """Interrogate the peer over an OPEN connection and return an environment
        profile (banner, server, advertised features/extensions). Default reads
        whatever greeting is already buffered."""
        greeting = ctx.state.get("greeting", "")
        return {"protocol_guess": self.name, "banner": greeting}


# ─────────────────────────────────────────────────────────────────────────────
#  Built-in modules
# ─────────────────────────────────────────────────────────────────────────────
class RawModule(ProtocolModule):
    name = "raw"
    aliases = ["tcp"]
    transport = "tcp"
    default_port = 0
    description = ("Raw TCP. Send arbitrary bytes (as text, hex, or base64) and "
                   "read whatever comes back. The escape hatch when no framed "
                   "module fits.")
    example = {"protocol": "raw", "host": "example.com", "port": 13,
               "request": {"text": ""}}

    def encode(self, request: Request) -> bytes:
        if isinstance(request, dict):
            return coerce_wire(request)
        return coerce_wire(request)


class LineModule(ProtocolModule):
    name = "line"
    transport = "tcp"
    default_port = 0
    description = ("Line-oriented text protocols (SMTP/POP3/IRC/FTP-style). Sends "
                   "your text followed by CRLF and reads the text reply.")
    example = {"protocol": "line", "host": "localhost", "port": 25,
               "request": "EHLO vera"}

    def encode(self, request: Request) -> bytes:
        if isinstance(request, dict):
            line = str(request.get("line") or request.get("text") or "")
            term = request.get("terminator", "\r\n")
        else:
            line, term = str(request), "\r\n"
        if not line.endswith(term):
            line += term
        return line.encode("utf-8", "replace")

    def decode(self, data: bytes) -> Dict[str, Any]:
        text = _preview_text(data)
        lines = text.splitlines()
        return {"lines": lines, "text": text, "bytes": len(data)}

    def fingerprint(self, ctx: "ConnectionContext") -> Dict[str, Any]:
        # Many line protocols (SMTP/FTP/IRC) greet on connect — read the banner
        # even if greets_first wasn't set, so `adopt` learns the environment.
        banner = ctx.state.get("greeting")
        if not banner:
            raw = ctx._recv(min(ctx.timeout, 2.0))
            banner = raw.decode("utf-8", "replace")[:512] if raw else ""
            if banner:
                ctx.state["greeting"] = banner
        return {"protocol_guess": "line", "banner": banner}


class HTTPModule(ProtocolModule):
    name = "http"
    aliases = ["web"]
    transport = "tcp"
    default_port = 80
    description = ("Cleartext HTTP/1.1 (no TLS). request: {method?, path?, host?, "
                   "headers?, body?}. Decodes status line, headers and body.")
    example = {"protocol": "http", "host": "example.com", "port": 80,
               "request": {"method": "GET", "path": "/", "headers": {"Accept": "*/*"}}}

    def encode(self, request: Request) -> bytes:
        r: Dict[str, Any] = request if isinstance(request, dict) else {"path": str(request)}
        method = str(r.get("method", "GET")).upper()
        path = str(r.get("path", "/")) or "/"
        host = str(r.get("host", "")) or ""
        headers = dict(r.get("headers") or {})
        body = r.get("body", "")
        body_b = _as_bytes(body) if body else b""
        lines = [f"{method} {path} HTTP/1.1"]
        hkeys = {k.lower() for k in headers}
        if host and "host" not in hkeys:
            lines.append(f"Host: {host}")
        if "connection" not in hkeys:
            lines.append("Connection: close")
        if "user-agent" not in hkeys:
            lines.append("User-Agent: Babblefish/1.0")
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        if body_b and "content-length" not in hkeys:
            lines.append(f"Content-Length: {len(body_b)}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "replace") + body_b
        return raw

    def decode(self, data: bytes) -> Dict[str, Any]:
        head, _, body = data.partition(b"\r\n\r\n")
        head_txt = head.decode("iso-8859-1", "replace")
        parts = head_txt.split("\r\n")
        status_line = parts[0] if parts else ""
        status_code = None
        m = re.match(r"HTTP/\d\.\d\s+(\d+)", status_line)
        if m:
            status_code = int(m.group(1))
        headers: Dict[str, str] = {}
        for line in parts[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        return {
            "status_line": status_line,
            "status": status_code,
            "headers": headers,
            "body": _preview_text(body),
            "body_bytes": len(body),
        }

    # ── connection context: mimic a real client + carry cookies across a session
    def contextualize(self, request: Request, ctx: "ConnectionContext") -> Request:
        r = dict(request) if isinstance(request, dict) else {"path": str(request)}
        headers = dict(r.get("headers") or {})
        hk = {k.lower() for k in headers}
        persona = ((ctx.profile or {}).get("http") or {}).get("headers") or {}
        for k, v in persona.items():          # persona fills only what the caller omitted
            if k.lower() not in hk:
                headers[k] = v
        if "host" not in {k.lower() for k in headers} and not r.get("host"):
            r["host"] = ctx.host
        if "connection" not in {k.lower() for k in headers}:
            headers["Connection"] = "keep-alive"     # sessions stay open (vs one-shot close)
        cookies = ctx.state.get("cookies")
        if cookies and "cookie" not in {k.lower() for k in headers}:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        r["headers"] = headers
        return r

    def on_response(self, ctx: "ConnectionContext", decoded: Dict[str, Any], raw: bytes) -> None:
        hdrs = decoded.get("headers") or {}
        for k, v in hdrs.items():
            lk = k.lower()
            if lk == "set-cookie":
                first = str(v).split(";", 1)[0]
                if "=" in first:
                    n, val = first.split("=", 1)
                    ctx.state.setdefault("cookies", {})[n.strip()] = val.strip()
            elif lk == "server":
                ctx.state["server"] = v
        ctx.state["last_status"] = decoded.get("status")

    def fingerprint(self, ctx: "ConnectionContext") -> Dict[str, Any]:
        res = ctx.send({"method": "GET", "path": "/"})
        d = res.get("decoded", {}) or {}
        hdrs = d.get("headers") or {}
        return {"protocol_guess": "http", "status": d.get("status"),
                "server": hdrs.get("Server"), "powered_by": hdrs.get("X-Powered-By"),
                "content_type": hdrs.get("Content-Type"), "headers": hdrs}


class RedisModule(ProtocolModule):
    name = "redis"
    aliases = ["resp"]
    transport = "tcp"
    default_port = 6379
    description = ("Redis RESP. request: a command as a list ['PING'] or a string "
                   "'GET key'. Encodes a RESP array; decodes the RESP reply.")
    example = {"protocol": "redis", "host": "localhost", "port": 6379,
               "request": ["PING"]}

    def encode(self, request: Request) -> bytes:
        if isinstance(request, dict):
            args = request.get("command") or request.get("args") or []
            if isinstance(args, str):
                args = args.split()
        elif isinstance(request, (list, tuple)):
            args = list(request)
        else:
            args = str(request).split()
        args = [str(a) for a in args]
        out = [f"*{len(args)}\r\n".encode()]
        for a in args:
            ab = a.encode("utf-8", "replace")
            out.append(f"${len(ab)}\r\n".encode() + ab + b"\r\n")
        return b"".join(out)

    def decode(self, data: bytes) -> Dict[str, Any]:
        value, _ = _resp_parse(data, 0)
        return {"reply": value, "raw": _preview_text(data)}

    def fingerprint(self, ctx: "ConnectionContext") -> Dict[str, Any]:
        out: Dict[str, Any] = {"protocol_guess": "redis"}
        try:
            out["ping"] = ctx.send(["PING"]).get("decoded", {}).get("reply")
        except Exception:
            pass
        try:
            txt = ctx.send({"command": ["INFO", "server"]}).get("decoded", {}).get("reply")
            if isinstance(txt, str):
                for line in txt.splitlines():
                    for key in ("redis_version", "os", "redis_mode", "arch_bits"):
                        if line.startswith(key + ":"):
                            out[key] = line.split(":", 1)[1].strip()
        except Exception:
            pass
        return out


def _resp_parse(buf: bytes, i: int):
    """Minimal RESP parser → (value, next_index). Best-effort; partial-tolerant."""
    if i >= len(buf):
        return None, i
    t = buf[i:i + 1]
    nl = buf.find(b"\r\n", i)
    if nl < 0:
        return _preview_text(buf[i:]), len(buf)
    head = buf[i + 1:nl].decode("iso-8859-1", "replace")
    j = nl + 2
    if t == b"+":
        return head, j
    if t == b"-":
        return {"error": head}, j
    if t == b":":
        try:
            return int(head), j
        except ValueError:
            return head, j
    if t == b"$":
        n = int(head or "-1")
        if n < 0:
            return None, j
        return buf[j:j + n].decode("utf-8", "replace"), j + n + 2
    if t == b"*":
        n = int(head or "-1")
        if n < 0:
            return None, j
        arr = []
        for _ in range(n):
            v, j = _resp_parse(buf, j)
            arr.append(v)
        return arr, j
    return _preview_text(buf[i:]), len(buf)


_DNS_QTYPES = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "PTR": 12, "MX": 15,
               "TXT": 16, "AAAA": 28, "SRV": 33, "ANY": 255}
_DNS_QTYPES_INV = {v: k for k, v in _DNS_QTYPES.items()}


class DNSModule(ProtocolModule):
    name = "dns"
    transport = "udp"
    default_port = 53
    description = ("DNS over UDP. request: {name, type?} (type A/AAAA/MX/TXT/... "
                   "default A). Encodes a standard query; decodes the answers.")
    example = {"protocol": "dns", "host": "1.1.1.1", "port": 53,
               "request": {"name": "example.com", "type": "A"}}

    def encode(self, request: Request) -> bytes:
        if isinstance(request, dict):
            name = str(request.get("name") or request.get("qname") or "")
            qtype = request.get("type") or request.get("qtype") or "A"
        else:
            name, qtype = str(request), "A"
        qt = qtype if isinstance(qtype, int) else _DNS_QTYPES.get(str(qtype).upper(), 1)
        header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)  # RD=1, 1 question
        q = b""
        for lbl in name.split("."):
            if not lbl:
                continue
            lb = lbl.encode("ascii", "replace")[:63]
            q += bytes([len(lb)]) + lb
        q += b"\x00" + struct.pack(">HH", qt, 1)      # null root + QTYPE + QCLASS(IN)
        return header + q

    def decode(self, data: bytes) -> Dict[str, Any]:
        if len(data) < 12:
            return {"error": "short DNS response", "hex": _hexdump(data)}
        (tid, flags, qd, an, ns, ar) = struct.unpack(">HHHHHH", data[:12])
        rcode = flags & 0x0F
        i = 12
        name, i = _dns_read_name(data, i)
        i += 4                                        # QTYPE + QCLASS
        answers = []
        for _ in range(an):
            try:
                rname, i = _dns_read_name(data, i)
                rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
                i += 10
                rdata = data[i:i + rdlen]
                answers.append({"name": rname, "type": _DNS_QTYPES_INV.get(rtype, rtype),
                                "ttl": ttl, "data": _dns_rdata(rtype, rdata, data)})
                i += rdlen
            except Exception:
                break
        return {"query": name, "rcode": rcode, "answer_count": an, "answers": answers}


def _dns_read_name(buf: bytes, i: int):
    labels: List[str] = []
    jumped = False
    start_i = i
    while True:
        if i >= len(buf):
            break
        length = buf[i]
        if length & 0xC0 == 0xC0:                     # compression pointer
            ptr = ((length & 0x3F) << 8) | buf[i + 1]
            if not jumped:
                start_i = i + 2
            i = ptr
            jumped = True
            continue
        i += 1
        if length == 0:
            break
        labels.append(buf[i:i + length].decode("ascii", "replace"))
        i += length
    return ".".join(labels), (start_i if jumped else i)


def _dns_rdata(rtype: int, rdata: bytes, whole: bytes) -> Any:
    if rtype == 1 and len(rdata) == 4:
        return ".".join(str(b) for b in rdata)
    if rtype == 28 and len(rdata) == 16:
        return ":".join(f"{rdata[i]<<8 | rdata[i+1]:x}" for i in range(0, 16, 2))
    if rtype == 16:                                   # TXT
        out, i = [], 0
        while i < len(rdata):
            n = rdata[i]; out.append(rdata[i + 1:i + 1 + n].decode("utf-8", "replace")); i += 1 + n
        return out
    if rtype in (2, 5, 12):                            # NS/CNAME/PTR
        name, _ = _dns_read_name(whole, whole.find(rdata) if rdata in whole else 0)
        return name or _hexdump(rdata)
    return _hexdump(rdata)


class WhoisModule(ProtocolModule):
    name = "whois"
    transport = "tcp"
    default_port = 43
    description = ("WHOIS (RFC 3912). request: the query string (a domain/IP). "
                   "Sends 'query\\r\\n' and returns the text record.")
    example = {"protocol": "whois", "host": "whois.iana.org", "port": 43,
               "request": "example.com"}

    def encode(self, request: Request) -> bytes:
        q = request.get("query", "") if isinstance(request, dict) else str(request)
        return (str(q).strip() + "\r\n").encode("utf-8", "replace")

    def decode(self, data: bytes) -> Dict[str, Any]:
        return {"text": _preview_text(data, 8192), "bytes": len(data)}


# ─────────────────────────────────────────────────────────────────────────────
#  Declarative (no-code) modules — the pluggable escape hatch
# ─────────────────────────────────────────────────────────────────────────────
class DeclarativeModule(ProtocolModule):
    """A protocol taught at runtime from a JSON spec (no Python needed).

    spec = {
      name, transport?("tcp"|"udp"), default_port?, description?,
      framing?("line"|"raw"),        # how a request string is put on the wire
      terminator?("\\r\\n"),          # appended when framing == "line"
      request_template?,             # str with {field} placeholders filled from
                                     # the request dict, e.g. "GET {key}"
      response?("text"|"hex"|"lines")# how the reply is surfaced
    }
    """
    def __init__(self, spec: Dict[str, Any]):
        self.spec = dict(spec or {})
        self.name = str(self.spec.get("name") or "custom").strip()
        self.transport = "udp" if str(self.spec.get("transport", "tcp")).lower() == "udp" else "tcp"
        self.default_port = int(self.spec.get("default_port") or 0)
        self.description = str(self.spec.get("description") or f"Declarative module '{self.name}'.")
        self.aliases = list(self.spec.get("aliases") or [])
        self.example = self.spec.get("example")

    def encode(self, request: Request) -> bytes:
        framing = str(self.spec.get("framing", "line")).lower()
        template = self.spec.get("request_template")
        if isinstance(request, dict):
            if template:
                try:
                    text = str(template).format(**request)
                except Exception:
                    text = str(request.get("text") or request.get("line") or "")
            else:
                text = str(request.get("text") or request.get("line") or "")
                if "hex" in request or "b64" in request or "base64" in request:
                    return coerce_wire(request)
        else:
            text = str(request)
        if framing == "raw":
            return coerce_wire(text)
        term = self.spec.get("terminator", "\r\n")
        if term and not text.endswith(term):
            text += term
        return text.encode("utf-8", "replace")

    def decode(self, data: bytes) -> Dict[str, Any]:
        mode = str(self.spec.get("response", "text")).lower()
        if mode == "hex":
            return {"hex": _hexdump(data, 4096), "bytes": len(data)}
        if mode == "lines":
            txt = _preview_text(data)
            return {"lines": txt.splitlines(), "bytes": len(data)}
        return {"text": _preview_text(data), "bytes": len(data)}


# ─────────────────────────────────────────────────────────────────────────────
#  Personas — client fingerprints Babblefish can wear to MIMIC a real client
# ─────────────────────────────────────────────────────────────────────────────
# A persona is a set of protocol-specific defaults merged into outgoing requests
# by a module's contextualize(). It lets Vera present traffic that looks like a
# real curl / browser / bot instead of a bare Babblefish request — the
# difference between "poking" a service and "speaking its native dialect".
PROFILES: Dict[str, Dict[str, Any]] = {
    "vera": {
        "description": "Honest default — identifies as Babblefish.",
        "http": {"headers": {"User-Agent": "Babblefish/1.0", "Accept": "*/*"}},
    },
    "curl": {
        "description": "Looks like command-line curl.",
        "http": {"headers": {"User-Agent": "curl/8.4.0", "Accept": "*/*"}},
    },
    "chrome": {
        "description": "Looks like desktop Chrome on Windows.",
        "http": {"headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                       "image/webp,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",     # we don't decompress — ask for plain
            "Upgrade-Insecure-Requests": "1",
        }},
    },
    "firefox": {
        "description": "Looks like desktop Firefox on Linux.",
        "http": {"headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "identity",
        }},
    },
    "googlebot": {
        "description": "Looks like Googlebot.",
        "http": {"headers": {
            "User-Agent": ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
            "Accept": "*/*",
        }},
    },
}


def get_profile(name_or_spec: Any) -> Dict[str, Any]:
    """Resolve a persona: a known name, an inline dict, or fall back to 'vera'."""
    if isinstance(name_or_spec, dict):
        return name_or_spec
    if isinstance(name_or_spec, str) and name_or_spec in PROFILES:
        return PROFILES[name_or_spec]
    return PROFILES["vera"]


def learn_profile(name: str, fingerprint: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a reusable persona from an observed environment fingerprint (e.g.
    mirror a server's own Server/header style, or echo the client fields it
    seems to expect) and store it under `name`. Best-effort and additive."""
    prof: Dict[str, Any] = {"description": f"Learned from {fingerprint.get('host','?')}",
                            "http": {"headers": {}}}
    hdrs = fingerprint.get("headers") or {}
    # Mirror back a plausible client that matches the observed server family.
    server = str(hdrs.get("Server") or fingerprint.get("server") or "").lower()
    if "nginx" in server or "cloudflare" in server or "apache" in server:
        prof["http"]["headers"] = dict(PROFILES["chrome"]["http"]["headers"])
    else:
        prof["http"]["headers"] = dict(PROFILES["curl"]["http"]["headers"])
    PROFILES[name] = prof
    return prof


# ─────────────────────────────────────────────────────────────────────────────
#  ConnectionContext — a live, stateful session that carries connection context
# ─────────────────────────────────────────────────────────────────────────────
class ConnectionContext:
    """A persistent connection to one peer, holding the context needed to speak a
    protocol properly over time: the negotiated `state` (cookies, features, auth,
    the greeting banner), the `profile` (persona) used to mimic a real client, and
    the `history` of exchanges. Blocking at the socket layer — the capability
    layer drives it via asyncio.to_thread. This is what lets Babblefish DROP INTO
    an environment (open → fingerprint → handshake) and then converse statefully
    rather than firing disconnected one-shots."""

    _counter = 0

    def __init__(self, module: ProtocolModule, host: str, port: int,
                 transport: str = "", timeout: float = 5.0,
                 profile: Any = None):
        ConnectionContext._counter += 1
        self.id = f"bf_{ConnectionContext._counter}_{int(_now()*1000) & 0xffff:x}"
        self.module = module
        self.host = host
        self.port = int(port)
        self.transport = (transport or module.transport).lower()
        self.timeout = float(timeout)
        self.profile = get_profile(profile)
        self.state: Dict[str, Any] = {}
        self.history: List[Dict[str, Any]] = []
        self.sock: Optional[socket.socket] = None
        self.opened_at = _now()
        self.last_used = self.opened_at
        self.closed = False

    # ── lifecycle ───────────────────────────────────────────────────────────
    def open(self) -> "ConnectionContext":
        if self.transport == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(self.timeout)
        else:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            if self.module.greets_first:
                banner = self._recv(self.timeout)
                if banner:
                    self.state["greeting"] = banner.decode("utf-8", "replace")[:1024]
                    try:
                        self.module.on_response(self, self.module.decode(banner), banner)
                    except Exception:
                        pass
        self.last_used = _now()
        return self

    def _recv(self, timeout: float, idle: float = 0.4, read_max: int = 262144) -> bytes:
        """Read a message from an open connection. Uses a short idle gap after the
        first bytes to detect the end of a reply on a kept-alive socket."""
        if not self.sock:
            return b""
        data = bytearray()
        if self.transport == "udp":
            try:
                self.sock.settimeout(timeout)
                chunk, _ = self.sock.recvfrom(read_max)
                return chunk
            except socket.timeout:
                return b""
        first = True
        while len(data) < read_max:
            try:
                self.sock.settimeout(timeout if first else idle)
                chunk = self.sock.recv(min(8192, read_max - len(data)))
            except socket.timeout:
                break
            if not chunk:
                break
            data.extend(chunk)
            first = False
        return bytes(data)

    def send(self, request: Request, expect_reply: bool = True) -> Dict[str, Any]:
        """Contextualise → encode → send → recv → decode → update state. Returns
        {request_wire, decoded, reply_wire}. This is the stateful analogue of the
        one-shot babblefish.speak."""
        if self.closed or not self.sock:
            raise RuntimeError("connection is closed")
        req2 = self.module.contextualize(request, self)
        payload = self.module.encode(req2)
        if self.transport == "udp":
            self.sock.sendto(payload, (self.host, self.port))
        else:
            self.sock.sendall(payload)
        reply = self._recv(self.timeout) if (expect_reply and self.module.expect_reply) else b""
        decoded: Dict[str, Any] = {}
        if reply:
            try:
                decoded = self.module.decode(reply)
                self.module.on_response(self, decoded, reply)
            except Exception as e:
                decoded = {"decode_error": f"{type(e).__name__}: {e}"}
        self.last_used = _now()
        entry = {"sent": _preview_text(payload, 400), "recv_bytes": len(reply),
                 "decoded": decoded}
        self.history.append(entry)
        if len(self.history) > 100:
            self.history = self.history[-100:]
        return {"request_wire": {"bytes": len(payload), "text": _preview_text(payload)},
                "decoded": decoded,
                "reply_wire": {"bytes": len(reply), "text": _preview_text(reply),
                               "hex": _hexdump(reply)}}

    def recv_only(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        reply = self._recv(timeout or self.timeout)
        decoded = {}
        if reply:
            try:
                decoded = self.module.decode(reply)
                self.module.on_response(self, decoded, reply)
            except Exception as e:
                decoded = {"decode_error": str(e)}
        self.last_used = _now()
        return {"decoded": decoded, "reply_wire": {"bytes": len(reply),
                "text": _preview_text(reply), "hex": _hexdump(reply)}}

    def fingerprint(self) -> Dict[str, Any]:
        fp = {"host": self.host, "port": self.port, "transport": self.transport}
        try:
            fp.update(self.module.fingerprint(self) or {})
        except Exception as e:
            fp["error"] = f"{type(e).__name__}: {e}"
        return fp

    def close(self) -> None:
        self.closed = True
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def info(self) -> Dict[str, Any]:
        return {"id": self.id, "protocol": self.module.name, "host": self.host,
                "port": self.port, "transport": self.transport, "closed": self.closed,
                "profile": (self.profile.get("description") if isinstance(self.profile, dict) else None),
                "state_keys": sorted(self.state.keys()), "exchanges": len(self.history),
                "age_s": round(_now() - self.opened_at, 1),
                "idle_s": round(_now() - self.last_used, 1)}


def _now() -> float:
    import time as _t
    return _t.time()


# Session registry — bounded, idle-pruned.
SESSIONS: Dict[str, ConnectionContext] = {}
_MAX_SESSIONS = 64
_SESSION_TTL = 600.0             # seconds idle before a session is reaped


def _prune_sessions() -> None:
    now = _now()
    dead = [sid for sid, c in SESSIONS.items()
            if c.closed or (now - c.last_used) > _SESSION_TTL]
    for sid in dead:
        try:
            SESSIONS[sid].close()
        except Exception:
            pass
        SESSIONS.pop(sid, None)
    # Hard cap: evict the oldest-idle if still over the limit.
    while len(SESSIONS) > _MAX_SESSIONS:
        oldest = min(SESSIONS.values(), key=lambda c: c.last_used)
        oldest.close()
        SESSIONS.pop(oldest.id, None)


def open_session(module: ProtocolModule, host: str, port: int, transport: str = "",
                 timeout: float = 5.0, profile: Any = None) -> ConnectionContext:
    _prune_sessions()
    conn = ConnectionContext(module, host, port, transport, timeout, profile)
    conn.open()
    SESSIONS[conn.id] = conn
    return conn


def get_session(sid: str) -> Optional[ConnectionContext]:
    return SESSIONS.get(sid)


def close_session(sid: str) -> bool:
    conn = SESSIONS.pop(sid, None)
    if not conn:
        return False
    conn.close()
    return True


def list_sessions() -> List[Dict[str, Any]]:
    _prune_sessions()
    return [c.info() for c in SESSIONS.values()]


# ─────────────────────────────────────────────────────────────────────────────
#  Registry
# ─────────────────────────────────────────────────────────────────────────────
REGISTRY: Dict[str, ProtocolModule] = {}


def register(module: ProtocolModule) -> None:
    REGISTRY[module.name.lower()] = module
    for a in getattr(module, "aliases", []) or []:
        REGISTRY.setdefault(str(a).lower(), module)


def register_declarative(spec: Dict[str, Any]) -> ProtocolModule:
    mod = DeclarativeModule(spec)
    if not mod.name:
        raise ValueError("module spec needs a 'name'")
    register(mod)
    return mod


def get_module(name: str) -> Optional[ProtocolModule]:
    if not name:
        return None
    return REGISTRY.get(str(name).lower())


def list_modules() -> List[Dict[str, Any]]:
    seen, out = set(), []
    for mod in REGISTRY.values():
        if id(mod) in seen:
            continue
        seen.add(id(mod))
        out.append(mod.describe())
    return sorted(out, key=lambda d: d["name"])


for _m in (RawModule(), LineModule(), HTTPModule(), RedisModule(),
           DNSModule(), WhoisModule()):
    register(_m)
