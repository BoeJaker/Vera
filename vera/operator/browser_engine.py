"""browser_engine.py — Playwright session/page lifecycle for the operator.

Host-side headless Chromium. One shared browser process; one *context + page*
per operator **session** so a mission can span many observe/act calls while
keeping cookies, scroll position and a stable per-session **ref map** (element
ref → locator info, filled by ``perception``).

Everything degrades gracefully when Playwright isn't installed: the module still
imports (so the capability registry loads), and any call that needs a browser
returns a clear error with an install hint instead of raising at import time —
exactly the pattern research/researcher_api uses.

Nothing here imports the orchestrator, so it is trivially unit-testable and can
be driven by the standalone CLI without booting Vera.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.operator.browser")

# ── Lazy Playwright import (never at call-time inside a hot path; see
#    researcher_api for why concurrent first-imports blow the recursion limit) ──
_async_playwright = None
_PLAYWRIGHT_AVAILABLE = False
_IMPORT_ERROR = ""
try:
    from playwright.async_api import async_playwright as _async_playwright  # type: ignore
    _PLAYWRIGHT_AVAILABLE = True
except Exception as e:  # pragma: no cover - depends on host env
    _IMPORT_ERROR = str(e)

INSTALL_HINT = (
    "Playwright is not installed in this environment. Install the operator's "
    "browser extra:  pip install -r requirements-operator.txt && "
    "playwright install chromium"
)

# Same hardening flags research uses — headless, no-sandbox (containers), no GPU.
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-blink-features=AutomationControlled",
]

DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# One shared browser; contexts are cheap and isolate sessions from each other.
_pw = None
_browser = None
# Async primitives are created LAZILY (on first use inside a running loop), never
# at import time — binding a loop at import is fragile (on Python <3.10 it grabs
# whatever loop is current then, which breaks if the module is first imported
# after an asyncio.run() has closed the loop, e.g. mid-test-suite).
_launch_lock: "Optional[asyncio.Lock]" = None
_page_sem: "Optional[asyncio.Semaphore]" = None


def _get_launch_lock() -> "asyncio.Lock":
    global _launch_lock
    if _launch_lock is None:
        _launch_lock = asyncio.Lock()
    return _launch_lock


def page_sem() -> "asyncio.Semaphore":
    """Bound concurrent live pages so a fan-out mission can't exhaust the host."""
    global _page_sem
    if _page_sem is None:
        _page_sem = asyncio.Semaphore(3)
    return _page_sem


def playwright_available() -> bool:
    return _PLAYWRIGHT_AVAILABLE


@dataclass
class OperatorSession:
    """A live browser context+page plus the state acts/observations need."""
    session_id: str
    target: Dict[str, Any] = field(default_factory=dict)
    base_url: str = ""
    viewport: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_VIEWPORT))
    created_ts: float = field(default_factory=time.time)
    # Last time anything touched this session. A context+page is a live chunk of
    # a shared Chromium, so an abandoned session costs real memory until the
    # process exits — see sweep_idle for the leak this bounds.
    last_used: float = field(default_factory=time.time)
    # ref map: "e12" -> {"role","name","selector","bbox",...} (filled by perception)
    ref_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    # Operating policy for this session (set at start): allowlist of external
    # hosts + allow_destructive/dry_run. Every act/run on the session reads it,
    # so you allowlist a site ONCE at connect, not on every action.
    policy: Dict[str, Any] = field(default_factory=dict)
    # Playwright handles — Any so the module imports without playwright types.
    context: Any = None
    page: Any = None

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target": self.target,
            "base_url": self.base_url,
            "viewport": self.viewport,
            "refs": len(self.ref_map),
            "steps": len(self.history),
            "age_s": round(time.time() - self.created_ts, 1),
            "idle_s": round(time.time() - self.last_used, 1),
            "url": self.meta.get("last_url", ""),
            "alive": self.page is not None,
            "policy": dict(self.policy or {}),
        }

    def touch(self) -> None:
        self.last_used = time.time()


_SESSIONS: Dict[str, OperatorSession] = {}


async def _get_browser():
    """Launch (once) and return the shared Chromium, relaunching if it died."""
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(INSTALL_HINT)
    async with _get_launch_lock():
        if _browser is not None:
            try:
                if _browser.is_connected():
                    return _browser
            except Exception:
                pass
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        log.info("operator: launching headless Chromium…")
        _pw = await _async_playwright().start()
        _browser = await _pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        log.info("operator: browser ready (connected=%s)", _browser.is_connected())
        return _browser


async def start_session(session_id: str = "", base_url: str = "",
                        viewport: Optional[Dict[str, int]] = None,
                        target: Optional[Dict[str, Any]] = None,
                        ignore_https_errors: bool = True) -> OperatorSession:
    """Create a browser context+page and register it as a session.

    Reuses an existing session_id if it is still alive (idempotent start).
    """
    sid = session_id or f"op-{uuid.uuid4().hex[:10]}"
    existing = _SESSIONS.get(sid)
    if existing and existing.page is not None:
        try:
            if existing.context and existing.page and not existing.page.is_closed():
                existing.touch()
                return existing
        except Exception:
            pass
        await close_session(sid)

    global _browser
    vp = dict(viewport or DEFAULT_VIEWPORT)
    context = page = None
    # Two attempts, because the shared browser can legitimately go away between
    # _get_browser() returning and new_context() being called: sweep_idle closes
    # it once the last session ends, and a browser can also die on its own. A
    # relaunch is cheap next to failing the caller's whole run.
    for attempt in (1, 2):
        browser = await _get_browser()
        try:
            context = await browser.new_context(
                viewport=vp,
                user_agent=_USER_AGENT,
                ignore_https_errors=ignore_https_errors,
                device_scale_factor=1,
            )
            page = await context.new_page()
            break
        except Exception as e:
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            context = page = None
            if attempt == 2:
                raise
            log.warning("operator: browser was gone when starting session %s (%s) "
                        "— relaunching once", sid, e)
            async with _get_launch_lock():
                _browser = None
    sess = OperatorSession(session_id=sid, base_url=base_url or "", viewport=vp,
                           target=dict(target or {}), context=context, page=page)
    _SESSIONS[sid] = sess
    log.info("operator: session %s started (base_url=%s)", sid, base_url or "-")
    return sess


def get_session(session_id: str) -> Optional[OperatorSession]:
    sess = _SESSIONS.get(session_id)
    if sess is not None:
        # Every observe/act goes through here, so this is the one place that
        # reliably means "still in use" — without it the idle sweep would reap
        # a session in the middle of a long mission.
        sess.touch()
    return sess


def list_sessions() -> List[Dict[str, Any]]:
    return [s.summary() for s in _SESSIONS.values()]


async def close_session(session_id: str) -> bool:
    sess = _SESSIONS.pop(session_id, None)
    if not sess:
        return False
    for h in (sess.page, sess.context):
        try:
            if h is not None:
                await h.close()
        except Exception:
            pass
    sess.page = sess.context = None
    log.info("operator: session %s closed", session_id)
    return True


#: A session untouched for this long is considered abandoned. Generous on
#: purpose: a mission can legitimately sit between steps while a model thinks,
#: and an LLM call's duration is unbounded.
DEFAULT_SESSION_IDLE_S = 1800.0
#: Once the last session ends, hold the browser this long before closing it, so
#: a burst of back-to-back runs reuses one Chromium instead of relaunching.
DEFAULT_BROWSER_LINGER_S = 300.0

_browser_idle_since: Optional[float] = None


def idle_session_ids(sessions: Dict[str, "OperatorSession"], *,
                     now: float, idle_s: float) -> List[str]:
    """Session ids untouched for at least `idle_s`. Pure — no Playwright, no
    clock of its own — so the reap rule is unit-testable (test_operator_session_sweep).

    A session with no page is already dead and is always collectable; that is
    the shape a half-failed start leaves behind.
    """
    out: List[str] = []
    for sid, sess in sessions.items():
        if sess is None:
            out.append(sid)
            continue
        if getattr(sess, "page", None) is None:
            out.append(sid)
            continue
        last = float(getattr(sess, "last_used", 0) or 0)
        if not last or now - last >= idle_s:
            out.append(sid)
    return out


async def sweep_idle(idle_s: float = DEFAULT_SESSION_IDLE_S,
                     linger_s: float = DEFAULT_BROWSER_LINGER_S) -> Dict[str, Any]:
    """Close abandoned sessions, and the shared browser once none are left.

    Why this exists (2026-09-16): `_SESSIONS` had no reaper and no cap, and the
    shared Chromium lived for the whole process. Every operator run that raised,
    timed out on its wall cap, or was CANCELLED skipped its close_session (the
    call sat after run_loop rather than in a finally), so its context and page
    stayed resident. Prod's headless_shell was observed at 8.4GB RSS / 110% CPU
    on a host at 71.5% memory, next to the event-loop stalls that host
    contention was feeding.

    The browser close is the half that actually returns the memory: Chromium
    does not hand much back while it lives, so reaping contexts alone would not
    have shrunk that 8.4GB. start_session relaunches on demand and retries once
    if it loses the race, so closing an idle browser is safe.
    """
    global _browser, _browser_idle_since
    now = time.time()
    closed = [sid for sid in idle_session_ids(_SESSIONS, now=now, idle_s=idle_s)]
    for sid in closed:
        try:
            await close_session(sid)
        except Exception as e:
            log.debug("operator: idle sweep could not close %s: %s", sid, e)
    if closed:
        log.info("operator: idle sweep closed %d abandoned session(s) "
                 "(idle >%.0fs): %s", len(closed), idle_s, ", ".join(closed[:6]))

    browser_closed = False
    if _SESSIONS:
        _browser_idle_since = None
    elif _browser is not None:
        if _browser_idle_since is None:
            _browser_idle_since = now
        elif now - _browser_idle_since >= linger_s:
            async with _get_launch_lock():
                b, _browser = _browser, None
            try:
                if b is not None:
                    await b.close()
                    browser_closed = True
                    log.info("operator: no sessions for %.0fs — shared browser "
                             "closed, it will relaunch on demand", now - _browser_idle_since)
            except Exception as e:
                log.debug("operator: could not close idle browser: %s", e)
            _browser_idle_since = None
    return {"closed_sessions": closed, "browser_closed": browser_closed,
            "live_sessions": len(_SESSIONS)}


async def shutdown() -> None:
    """Close every session + the shared browser (best-effort; for tests/exit)."""
    for sid in list(_SESSIONS.keys()):
        await close_session(sid)
    global _browser, _pw
    for h in (_browser, _pw):
        try:
            if h is not None:
                await (h.close() if hasattr(h, "close") else h.stop())
        except Exception:
            pass
    _browser = _pw = None
