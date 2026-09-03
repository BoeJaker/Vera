"""A reused browser session is still pointed at the last run's page.

Census 30, run ce4b0a4725. The loop passed the RIGHT url -

    https://localhost:8999/remote/sandbox/preview/79d473ec-.../timer.html

- and the run opened on https://localhost:8999/, the Vera dashboard. It then
spent all twelve of its steps trying to reach /timer.html, which resolves
against the orchestrator root and 404s, reloading and navigating back and
forth until it ran out.

cap_run only navigates when it OPENS a session::

    s = _be.get_session(session_id) if session_id else None
    if not s:
        start = await _open_session(url=url, ...)

When session_id already maps to a live session, _open_session is skipped and
nothing ever visits `url`. The run inherits whatever page the previous run
left behind - so the very first operator.run of a session works and every
later one silently starts somewhere else. That is the opposite of the
intuition ("passing a url points the browser at it"), which is why it survived
this long: the url IS being passed, and IS being ignored.

Pure: no I/O.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def _norm(url: str):
    """(scheme, netloc, path) with a trailing slash and fragment ignored."""
    parts = urlsplit(str(url or "").strip())
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return (parts.scheme.lower(), parts.netloc.lower(), path, parts.query)


def same_page(current: str, requested: str) -> bool:
    """Whether the browser is already showing `requested`.

    Compares scheme, host, path and query; a fragment is ignored because it
    never changes which document is loaded. A trailing slash is ignored
    because "/x" and "/x/" are the same page to every server we drive.
    """
    if not str(current or "").strip() or not str(requested or "").strip():
        return False
    return _norm(current) == _norm(requested)


def needs_navigation(current: str, requested: str) -> bool:
    """Whether a REUSED session must be moved before the run starts.

    False when no url was requested - a caller that named no target is happy
    wherever the session already is, and navigating would undo a deliberate
    hand-off between steps.

    False for a RELATIVE requested url: it cannot be compared to the current
    page here, and _open_session resolves those against base_url. Leaving it
    alone keeps this from turning a working relative target into a guess.
    """
    req = str(requested or "").strip()
    if not req:
        return False
    if "://" not in req:
        return False
    return not same_page(current, req)
