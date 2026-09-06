"""nav_pin.py -- keep a run that was pointed at ONE file on that file.

Census 35, author-then-edit run b71aa5cb70. The operator was opened correctly on

    https://localhost:8999/remote/sandbox/preview/<session>/timer.html

and its first decision was to leave:

     1 goto {"url": "https://localhost:8999/timer.html"}   <- invented
     2 nav  {"direction": "reload"}
     3 nav  {"direction": "back"}
     4 goto {"url": "/timer.html"}
     ...  goto -> reload -> back, three times round, twelve steps, 384s

`/timer.html` on the orchestrator root is not where the file lives; nothing was
ever going to be there. The run ended at max_steps having never returned to the
page it started on, and reported that as its outcome.

This is a KNOWN shape, already written down in _open_session: "the model then
GUESSES its own goto target (a bare relative filename) instead of retrying the
URL it was actually given". The note was there; the guard was not.

Scope is deliberately narrow. A pin applies only when the operator was aimed at
a specific file served out of a sandbox -- the case where the whole job IS that
one page, so leaving it is always a mistake. A goal like "find X on wikipedia"
passes a url it is expected to navigate away from, and must not be pinned;
``is_pinnable`` is what separates the two.

The guard hands the model an ERROR rather than blocking the run. A hard block
ends the run outright (operator_loop treats a safety block as terminal), which
trades twelve wasted steps for zero useful ones; an error is visible in the next
prompt's history, names the right URL, and leaves the run able to recover.

Pure: no browser, no I/O.
"""

from __future__ import annotations

from urllib.parse import urljoin

#: The route that serves one file out of a session sandbox. sandbox_file_target
#: builds these; this is the marker that a target is a single file rather than a
#: site to be explored.
PREVIEW_MARKER = "/remote/sandbox/preview/"


def is_pinnable(url: str) -> bool:
    """True when ``url`` addresses one file in a sandbox, not a site to browse."""
    return PREVIEW_MARKER in str(url or "")


def _dir_of(url: str) -> str:
    """The pinned file's directory, so siblings it may legitimately reference
    (a stylesheet, another page the run wrote) stay reachable."""
    u = str(url or "")
    base = u.split("#", 1)[0].split("?", 1)[0]
    cut = base.rfind("/")
    return base[:cut + 1] if cut >= 0 else base


def resolve(current_url: str, dest: str) -> str:
    """Absolute destination wins; a relative one resolves against the page.

    Mirrors safety._resolve_goto deliberately: a check that reads a different
    string from the one the browser will navigate to is worse than no check.
    """
    d = str(dest or "").strip()
    cur = str(current_url or "").strip()
    if not d:
        return cur
    if "://" in d:
        return d
    if not cur:
        return d
    try:
        return urljoin(cur, d)
    except Exception:
        return d


def off_pin(pin_url: str, current_url: str, action: str, args=None) -> str:
    """Return an error message when ``action`` would leave the pinned file.

    Empty string means allowed. Only ``goto`` is judged: that is how run
    b71aa5cb70 left, and it is the only action that names an arbitrary
    destination. reload/back/forward move within history the run already has,
    and refusing those would strand a run whose first step was legitimate.
    """
    pin = str(pin_url or "").strip()
    if not pin or not is_pinnable(pin):
        return ""
    if str(action or "") != "goto":
        return ""
    dest = resolve(current_url or pin, str((args or {}).get("url") or ""))
    if not dest:
        return ""
    allowed = _dir_of(pin)
    if dest.split("#", 1)[0].split("?", 1)[0].startswith(allowed):
        return ""
    return ("that URL is not where this file is served from. You were opened on "
            "%s and the goal concerns THAT page - %s does not exist. Do not "
            "navigate away again; act on the page you are already on (or use "
            "goto with the URL above if you have left it)."
            % (pin, dest))


def describe(pin_url: str) -> str:
    """One line for the prompt, so the model is told before it guesses."""
    if not is_pinnable(pin_url or ""):
        return ""
    name = str(pin_url).rstrip("/").rsplit("/", 1)[-1] or str(pin_url)
    return ("You are already on %s, which is where %s is served from. It is the "
            "only page this goal concerns - do not navigate anywhere else."
            % (pin_url, name))
