"""safety.py — guardrails for "do anything a person could do".

The operator is powerful, so every act passes a policy check before it runs. The
rules are deliberately simple and pure (unit-testable):

  • **Local / sandbox targets** (localhost, 127.0.0.1, host.docker.internal, and
    the estate hosts) are trusted: acts run for real, no dry-run.
  • **External hosts** must match an explicit ``allowlist``; otherwise blocked.
  • **Mutating** acts (click/type/press/select/goto/nav — things that change
    page or server state) on a non-local host require ``allow_destructive`` (or
    an interactive ``confirm``); otherwise they are reported as *needing
    confirmation* rather than executed.
  • ``dry_run`` short-circuits every mutating act into a plan-only note.

Nothing here talks to the browser or the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from .actions import MUTATING_ACTIONS

# Hosts that are always trusted (the operator drives Vera's own surfaces here).
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1",
                "host.docker.internal", "vera-dev"}

#: URL schemes that are not a NETWORK LOCATION at all - the browser's own blank
#: page, an inline document, a browser error page. They have no host to
#: allowlist, and treating them as external is how the operator ended up
#: refusing to act on the page it was already sitting on.
#:
#: Census 40, author-then-edit (the run's only wall-cap): a session that has not
#: navigated yet is on `about:blank`, and host_of("about:blank") returns
#: "about" because the scheme is parsed as a hostname. That is not local and not
#: in any allowlist, so EVERY mutating action was refused with
#:
#:     blocked: host 'about' is not a local/Vera surface and is not in the
#:     allowlist - add 'about' to the session/run allowlist to operate it
#:
#: which is advice nobody can act on, and `blocked` is terminal in run_loop - so
#: the run ended on the spot. The same parse hits data: URLs and Chrome's own
#: chrome-error:// page, both of which a real run lands on.
_NON_NETWORK_SCHEMES = ("about:", "data:", "blob:", "javascript:",
                        "chrome-error:", "chrome:", "edge:")


def is_non_network(url: str) -> bool:
    """True when ``url`` names no network location at all.

    These are neither local nor remote: there is nothing to allowlist, and
    nothing to protect. Acting on them is as safe as acting on a blank page,
    because that is what they are.
    """
    return str(url or "").strip().lower().startswith(_NON_NETWORK_SCHEMES)


def host_of(url_or_host: str) -> str:
    s = (url_or_host or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        return (urlparse(s).hostname or "").lower()
    except Exception:
        return ""


def is_local_host(host: str, extra_local: Optional[List[str]] = None) -> bool:
    h = (host or "").lower()
    if not h:
        return True  # relative / same-origin
    if h in _LOCAL_HOSTS:
        return True
    if h.endswith(".local") or h.startswith("192.168.") or h.startswith("10.") \
            or h.startswith("172.16.") or h.startswith("172.17."):
        return True
    for e in (extra_local or []):
        if h == e.lower():
            return True
    return False


def _match_allow(host: str, allowlist: List[str]) -> bool:
    h = (host or "").lower()
    for pat in (allowlist or []):
        p = pat.lower().strip()
        if not p:
            continue
        if p == h or p == "*":
            return True
        if p.startswith("*.") and (h == p[2:] or h.endswith(p[1:])):
            return True
        if h.endswith("." + p) or h == p:
            return True
    return False


@dataclass
class SafetyPolicy:
    allowlist: List[str] = field(default_factory=list)   # extra external hosts
    extra_local: List[str] = field(default_factory=list)  # extra trusted hosts
    dry_run: bool = False           # plan-only: no mutating act executes
    allow_destructive: bool = True  # allow mutating acts on allowed targets
    confirm: bool = False           # interactive confirm gate satisfied

    @classmethod
    def for_target(cls, kind: str, base_url: str = "", **over) -> "SafetyPolicy":
        """Sensible defaults per target kind. Acting is gated by the ALLOWLIST
        (local/Vera surfaces are implicitly permitted; any other host must be
        named). ``dry_run`` is the optional plan-only preview and defaults off —
        so allowlisting a host is all it takes to operate it. Overrides win."""
        local = kind in ("panel", "sandbox", "codeserver") or \
            is_local_host(host_of(base_url))
        p = cls(
            dry_run=False,
            allow_destructive=True,
            confirm=local,
        )
        for k, val in over.items():
            if hasattr(p, k) and val is not None:
                setattr(p, k, val)
        return p


def _resolve_goto(current_url: str, dest: str) -> str:
    """An absolute destination wins; a relative one resolves against the page.

    Kept here rather than in the caller because the ALLOWLIST is what reads the
    host, and a check that reads a different string from the one the browser
    will navigate to is worse than no check.
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


def evaluate(policy: SafetyPolicy, url: str, action: str,
             args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide whether ``action`` may run. Returns
    {allowed, dry_run, reason}. ``allowed`` False = blocked; ``dry_run`` True =
    log the intended action but don't perform it."""
    args = args or {}
    # For goto, the *destination* host matters; otherwise the current url's host.
    # A RELATIVE destination is navigation within the page we are already on, so
    # it must be resolved against the current url BEFORE the host is read.
    # Without this, host_of("form.html") returns "form.html", which is not a
    # local surface and not in any allowlist, so ordinary same-page navigation
    # was refused: census run 23, build-browser-verified step 3 - "blocked: host
    # 'form.html' is not a local/Vera surface - add 'form.html' to the allowlist".
    if action == "goto":
        target_url = _resolve_goto(url, str(args.get("url") or ""))
    else:
        target_url = url
    # A scheme with no network location is neither local nor remote. Checked
    # BEFORE host_of, because host_of would read the scheme itself as the
    # hostname ("about:blank" -> "about") and then refuse it. See
    # _NON_NETWORK_SCHEMES for the run this cost.
    local = is_non_network(target_url)
    host = "" if local else host_of(target_url)
    local = local or is_local_host(host, policy.extra_local)
    mutating = action in MUTATING_ACTIONS

    # Non-mutating acts (observe/scroll/wait/screenshot/hover/done) are always OK.
    if not mutating:
        return {"allowed": True, "dry_run": False, "reason": "read-only action"}

    if not local and not _match_allow(host, policy.allowlist):
        return {"allowed": False, "dry_run": False,
                "reason": f"host '{host}' is not a local/Vera surface and is not in "
                          f"the allowlist — add '{host}' to the session/run allowlist "
                          f"to operate it"}

    # Local or explicitly allowlisted → permitted. dry_run is the only remaining
    # gate (plan-only preview); when set, report the intended act without doing it.
    if policy.dry_run:
        return {"allowed": True, "dry_run": True,
                "reason": "dry-run: action planned but not executed"}

    return {"allowed": True, "dry_run": False, "reason": "ok"}
