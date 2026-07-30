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
_launch_lock = asyncio.Lock()
# Bound concurrent live pages so a fan-out mission can't exhaust the host.
PAGE_SEM = asyncio.Semaphore(3)


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
            "url": self.meta.get("last_url", ""),
            "alive": self.page is not None,
            "policy": dict(self.policy or {}),
        }


_SESSIONS: Dict[str, OperatorSession] = {}


async def _get_browser():
    """Launch (once) and return the shared Chromium, relaunching if it died."""
    global _pw, _browser
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(INSTALL_HINT)
    async with _launch_lock:
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
                return existing
        except Exception:
            pass
        await close_session(sid)

    browser = await _get_browser()
    vp = dict(viewport or DEFAULT_VIEWPORT)
    context = await browser.new_context(
        viewport=vp,
        user_agent=_USER_AGENT,
        ignore_https_errors=ignore_https_errors,
        device_scale_factor=1,
    )
    page = await context.new_page()
    sess = OperatorSession(session_id=sid, base_url=base_url or "", viewport=vp,
                           target=dict(target or {}), context=context, page=page)
    _SESSIONS[sid] = sess
    log.info("operator: session %s started (base_url=%s)", sid, base_url or "-")
    return sess


def get_session(session_id: str) -> Optional[OperatorSession]:
    return _SESSIONS.get(session_id)


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
