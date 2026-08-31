"""Our own self-signed certificate is not a stranger's.

Vera serves HTTPS with a certificate it generates for itself
(`_ensure_self_signed_cert`). A default TLS context refuses it, so any fetch of
a URL Vera itself published fails with

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed

Census run 19, build-browser-verified: `operator.run` could not get a browser,
so the executor did the sensible thing and tried to fetch the page instead -
`http.get` once and `web.fetch` three times, all four killed by this, all four
against Vera's own sandbox-preview URL. It then spent 532 seconds trying to
`apt-get install` xvfb and firefox-esr inside the sandbox to get a browser of
its own. Four wasted cycles and a third of the goal's budget, because we would
not trust a certificate we issued to ourselves.

The codebase already reasons this way elsewhere: the research WebSocket skips
verification for `wss://` into this same process, on the grounds that it is a
loopback connection into ourselves.

DELIBERATELY NARROW. This relaxes verification for our OWN origin and nothing
else - the open internet keeps full verification, which is the entire point of
having it. "Our own origin" means loopback (localhost/127.0.0.1/::1) or the
host this orchestrator is configured to serve as, and only on the port it
serves. A URL that merely mentions localhost in a query string, or hits a
different port, is a stranger.

Pure: no I/O, no imports from the app. The caller supplies its own identity.
"""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlparse

#: Hostnames that can only ever mean this machine.
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def _norm_host(host: str) -> str:
    h = str(host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        return h
    return h


def is_own_origin(url: str, *, own_hosts: Optional[Iterable[str]] = None,
                  own_port: Optional[int] = None) -> bool:
    """True when `url` points at THIS orchestrator.

    `own_hosts` are the extra names this instance answers to (e.g. "llm.int");
    `own_port` is the port it serves. A missing port on the URL means the
    scheme default, which for https is 443 - so an https URL with no port only
    counts if this instance genuinely serves 443.
    """
    try:
        u = urlparse(str(url or "").strip())
    except Exception:
        return False
    if u.scheme not in ("https", "http", "wss", "ws"):
        return False

    host = _norm_host(u.hostname or "")
    if not host:
        return False

    known = {_norm_host(h) for h in (own_hosts or []) if _norm_host(h)}
    if host not in LOOPBACK and host not in known:
        return False

    if own_port is None:
        return True
    port = u.port
    if port is None:
        port = 443 if u.scheme in ("https", "wss") else 80
    try:
        return int(port) == int(own_port)
    except Exception:
        return False


def verify_for(url: str, *, own_hosts: Optional[Iterable[str]] = None,
               own_port: Optional[int] = None) -> bool:
    """What to pass as httpx's `verify` for this URL.

    False ONLY for our own origin; True - full verification - for everything
    else, so this can never quietly weaken a request to the open internet.
    """
    return not is_own_origin(url, own_hosts=own_hosts, own_port=own_port)


def own_identity():
    """(hosts, port) for THIS orchestrator, read from its config.

    Impure by necessity and deliberately forgiving: if anything cannot be
    determined we return no hosts, and `is_own_origin` then matches loopback
    only - the conservative answer.
    """
    hosts, port = [], None
    try:
        import os
        import Vera.vera.capability_orchestration as _orch     # noqa
        cfg = getattr(_orch, "cfg", None)
        if cfg is not None:
            for attr in ("PUBLIC_HOST", "HOSTNAME", "ORCH_HOST", "HOST"):
                v = getattr(cfg, attr, "") or ""
                if v:
                    hosts.append(str(v))
            port = getattr(cfg, "ORCHESTRATOR_PORT", None)
        port = int(os.environ.get("VERA_ORCH_PORT") or port or 8999)
    except Exception:
        port = port or 8999
    return hosts, port
