"""Pure rules for the edge guard: may this HTTP request or WebSocket handshake
reach Vera?

Vera's API had no authentication and answered CORS with ``*``. A page on any
other site the owner opened could therefore POST to ``/mcp/call`` (a
``text/plain`` body needs no CORS preflight) or open ``/ws/mcp`` (browsers do
not apply CORS to WebSockets) and run any capability. These rules refuse
browser requests that come from another origin, while every non-browser caller
(the MCP bridge, sandboxes, n8n, the census tools, node workers) keeps working:
they send neither ``Origin`` nor ``Sec-Fetch-*``.

No I/O here; ``vera/edge_guard.py`` is the ASGI wrapper that applies them.

Rules, in order:

1. host:     the ``Host`` header names this machine (DNS-rebinding defence).
2. origin:   a state-changing request (POST/PUT/PATCH/DELETE) or any WebSocket
             handshake that carries ``Origin`` must come from Vera's own origin:
             the Origin's host:port equals the request's Host (or a trusted
             proxy's X-Forwarded-Host), or is listed in VERA_ALLOWED_ORIGINS.
             ``Origin: null`` is refused.
3. fetch:    without ``Origin``, a browser still says where it came from:
             ``Sec-Fetch-Site`` of ``cross-site`` or ``same-site`` (another
             port on the same host is same-site but a different origin) is
             refused for state-changing requests and WebSockets.
4. read:     a cross-site or same-site GET/HEAD that is not a top-level
             navigation and not a static asset is refused, so a foreign page
             cannot trigger GET side effects or probe the API. Reads whose
             Origin is one of this machine's own host names (any port) pass:
             Vera's panels in sandboxes and mirrors read prod that way.

Everything else is allowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CROSS = frozenset({"cross-site", "same-site"})

# Paths a foreign page may load as a subresource without harm: static files
# and the PWA shell. Anything else that is not a navigation counts as an API
# read for rule 4.
STATIC_PREFIXES: Tuple[str, ...] = ("/ui/", "/static/", "/assets/", "/pwa/", "/favicon",
                                    "/manifest", "/sw.js", "/service-worker")
STATIC_SUFFIXES: Tuple[str, ...] = (".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg",
                                    ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2",
                                    ".ttf", ".otf", ".webmanifest")


@dataclass(frozen=True)
class Verdict:
    allow: bool
    rule: str = ""          # host | origin | fetch | read, when refused
    reason: str = ""


ALLOW = Verdict(True)


def _hostport(value: str) -> Tuple[str, Optional[int]]:
    """'Example.org:8999' -> ('example.org', 8999); '[::1]:80' -> ('::1', 80)."""
    v = (value or "").strip().lower()
    if not v:
        return "", None
    if v.startswith("["):
        end = v.find("]")
        host = v[1:end] if end != -1 else v.strip("[]")
        rest = v[end + 1:] if end != -1 else ""
        port = rest[1:] if rest.startswith(":") else ""
    elif v.count(":") == 1:
        host, port = v.split(":", 1)
    else:
        host, port = v, ""
    try:
        return host.rstrip("."), (int(port) if port else None)
    except ValueError:
        return host.rstrip("."), None


def origin_hostport(origin: str) -> Tuple[str, Optional[int], str]:
    """'https://llm.int:8999' -> ('llm.int', 8999, 'https'); default ports filled in."""
    try:
        u = urlsplit((origin or "").strip())
    except ValueError:
        return "", None, ""
    scheme = (u.scheme or "").lower()
    host = (u.hostname or "").lower().rstrip(".")
    port = u.port
    if port is None and scheme in ("http", "ws"):
        port = 80
    elif port is None and scheme in ("https", "wss"):
        port = 443
    return host, port, scheme


def _same_authority(origin: str, host_header: str, default_port: Optional[int]) -> bool:
    oh, op, _ = origin_hostport(origin)
    hh, hp = _hostport(host_header)
    if not oh or not hh or oh != hh:
        return False
    if hp is None:
        # No port in Host: the browser used its scheme's default port (a front
        # such as the netctl portal on 443), or this listener's own port.
        return op in (80, 443) or (default_port is not None and op == default_port)
    return op == hp


def host_allowed(host_header: str, allowed_hosts: Iterable[str]) -> bool:
    h, _ = _hostport(host_header)
    if not h:
        return True        # no Host header: HTTP/1.0 or a raw client; nothing to rebind
    allowed = {a.strip().lower().rstrip(".") for a in allowed_hosts if a and a.strip()}
    return h in allowed


def is_static_path(path: str) -> bool:
    p = (path or "").lower()
    return p.startswith(STATIC_PREFIXES) or p.endswith(STATIC_SUFFIXES)


def decide(*, kind: str, method: str, path: str, headers: Mapping[str, str],
           allowed_hosts: Iterable[str] = (), allowed_origins: Iterable[str] = (),
           trusted_proxy: bool = False, default_port: Optional[int] = None,
           check_host: bool = True) -> Verdict:
    """The verdict for one request.

    ``kind`` is the ASGI scope type ('http' or 'websocket'); ``headers`` are
    lower-cased names. ``trusted_proxy`` says the TCP peer is a configured
    reverse proxy, so its X-Forwarded-Host counts as the request's host.
    ``default_port`` is the port this listener serves, used when the Host
    header carries none.
    """
    host = headers.get("host", "")
    if trusted_proxy and headers.get("x-forwarded-host"):
        host = headers.get("x-forwarded-host", "").split(",")[0].strip()

    if check_host and not host_allowed(host, allowed_hosts):
        return Verdict(False, "host", f"host {host!r} is not this machine")

    m = (method or "").upper()
    ws = kind == "websocket"
    origin = headers.get("origin")
    site = (headers.get("sec-fetch-site") or "").lower()
    mode = (headers.get("sec-fetch-mode") or "").lower()
    extra = {o.strip().lower().rstrip("/") for o in allowed_origins if o and o.strip()}

    if ws or m in STATE_CHANGING:
        if origin is not None:
            o = origin.strip()
            if o.lower() == "null":
                return Verdict(False, "origin", "Origin: null")
            if o.lower().rstrip("/") in extra or _same_authority(o, host, default_port):
                return ALLOW
            return Verdict(False, "origin", f"origin {o!r} is not Vera's own")
        if site in CROSS:
            return Verdict(False, "fetch", f"Sec-Fetch-Site: {site}")
        return ALLOW            # no Origin, no Sec-Fetch-*: a non-browser caller

    if m in ("GET", "HEAD") and site in CROSS and mode != "navigate" and not is_static_path(path):
        if origin is not None:
            o = origin.strip().lower().rstrip("/")
            if o in extra:
                return ALLOW
            # Vera's own panels served by a sandbox or mirror on another port of
            # this machine read prod's API by design (evolve_panel.html `_PROD`,
            # capability_orchestration.html `ask`). Reads from this machine's own
            # host names are allowed; cross-port WRITES stay refused above.
            oh, _, _ = origin_hostport(o)
            if oh and host_allowed(oh, allowed_hosts):
                return ALLOW
        return Verdict(False, "read", f"cross-site {mode or 'subresource'} read")
    return ALLOW
