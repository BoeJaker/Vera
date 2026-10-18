"""Single-use tickets for the terminal WebSockets (pure, no I/O).

A terminal socket (``/remote/docker/term/ws/...``, ``/remote/ssh/term/ws/...``)
is a shell on an estate host. Before 2026-10-17 any client could open one by
knowing the path. Now a socket opens only with a ticket that a capability call
minted seconds earlier (``conn.open``, ``sandbox.session.terminal`` or
``remote.term.ticket``). A capability call is a same-origin POST (the edge
guard refuses cross-site ones) and leaves an audit record, so a terminal can no
longer be opened by a page or client that never asked Vera for it.

The shape follows ``proxmox.console.ticket``: 128 bits, 90 seconds, held in
memory only (Vera serves from one process), bound to the target, and spent on
first use, whether or not it matched.
"""
from __future__ import annotations

import secrets
import time
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

TTL_S = 90.0
MAX_OPEN = 512          # outstanding tickets kept; the oldest is dropped beyond this

DOCKER_PREFIX = "/remote/docker/term/ws/"
SSH_PREFIX = "/remote/ssh/term/ws/"

Target = Tuple[str, str, str]   # (kind, host_id, container); container is "" for ssh


def target_of_path(ws_path: str) -> Optional[Target]:
    """The target a terminal socket path names, or None if it is not one.

    '/remote/docker/term/ws/local/web%20app?shell=sh' -> ('docker', 'local', 'web app')
    '/remote/ssh/term/ws/pve01?shell='              -> ('ssh', 'pve01', '')
    """
    path = urlsplit(ws_path or "").path
    if path.startswith(DOCKER_PREFIX):
        parts = path[len(DOCKER_PREFIX):].split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return "docker", unquote(parts[0]), unquote(parts[1])
        return None
    if path.startswith(SSH_PREFIX):
        rest = path[len(SSH_PREFIX):]
        if rest and "/" not in rest:
            return "ssh", unquote(rest), ""
    return None


def with_ticket(ws_path: str, ticket: str) -> str:
    """``ws_path`` with its query's ``ticket`` set to ``ticket`` (any old one removed)."""
    u = urlsplit(ws_path or "")
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "ticket"]
    q.append(("ticket", ticket))
    return f"{u.path}?{urlencode(q)}"


class TicketBook:
    """Outstanding tickets. ``clock`` is injectable for tests."""

    def __init__(self, ttl: float = TTL_S, clock: Callable[[], float] = time.monotonic,
                 max_open: int = MAX_OPEN):
        self.ttl = float(ttl)
        self.clock = clock
        self.max_open = int(max_open)
        self._open: Dict[str, Tuple[Target, float]] = {}

    def _prune(self) -> None:
        now = self.clock()
        for t in [t for t, (_, exp) in self._open.items() if exp <= now]:
            self._open.pop(t, None)
        while len(self._open) >= self.max_open:
            self._open.pop(next(iter(self._open)))       # dicts keep insertion order

    def mint(self, kind: str, host_id: str, container: str = "") -> str:
        self._prune()
        ticket = secrets.token_urlsafe(16)               # 128 bits
        self._open[ticket] = ((str(kind), str(host_id), str(container or "")), self.clock() + self.ttl)
        return ticket

    def consume(self, ticket: str, kind: str, host_id: str, container: str = "") -> Tuple[bool, str]:
        """Spend ``ticket`` for this target. Returns (ok, reason)."""
        if not ticket:
            return False, "no ticket"
        entry = self._open.pop(ticket, None)             # spent even when it does not match
        if entry is None:
            return False, "unknown or already used ticket"
        target, exp = entry
        if exp <= self.clock():
            return False, "ticket expired"
        if target != (str(kind), str(host_id), str(container or "")):
            return False, "ticket is for another target"
        return True, ""

    def __len__(self) -> int:
        return len(self._open)


# The one book. It lives here, not in a capability module: those load under
# bare names, and a second import by package path would give a second book.
BOOK = TicketBook()
