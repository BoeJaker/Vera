"""One TLS context for Vera's outbound httpx clients.

`httpx.AsyncClient()` with no `verify=` builds a fresh SSL context every time a client is made - it loads the whole CA
bundle (certifi) from disk, tens of milliseconds of CPU - even when every URL it fetches is plain http. The modules
that open a short-lived client per call (warm models, the NLP node dispatch, the node agent) did that on the event
loop many times a minute: a py-spy profile of prod on 2026-10-03 put ~1.9 s of every 40 s of the main thread in
`ssl.create_default_context`, alongside the capability endpoints answering in seconds.

`shared_ssl_context()` is the same trust httpx uses by default (certifi, falling back to the system store), built
once and handed to every client as `verify=`. An `ssl.SSLContext` is safe to share between clients and connections.
Pure stdlib (+ certifi when present), so it imports anywhere - the app, a node, a test.
"""
from __future__ import annotations

import ssl
import threading
from typing import Optional

_CTX: Optional[ssl.SSLContext] = None
_LOCK = threading.Lock()


def _build() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def shared_ssl_context() -> ssl.SSLContext:
    """The process-wide verifying TLS context, built on first use."""
    global _CTX
    if _CTX is None:
        with _LOCK:
            if _CTX is None:
                _CTX = _build()
    return _CTX
