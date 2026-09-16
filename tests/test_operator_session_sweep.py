"""A cancelled operator run must not leak its browser context.

Pins vera.operator.browser_engine session lifecycle. The regression
(2026-09-16, prod): `operator.run` closed its session on the line AFTER
run_loop returned, not in a `finally` -- so every run that raised, hit its wall
cap, or was CANCELLED (run_loop takes a should_cancel callback precisely
because cancellation is routine) skipped the close and left a live Chromium
context+page in the shared browser. Nothing else collected them: _SESSIONS had
no reaper, no TTL and no cap, and the shared browser lived for the whole
process. prod's headless_shell was measured at 8.4GB RSS / 110% CPU on a host
at 71.5% memory.

These tests use fakes, not Playwright: the rule is what needs pinning, and the
suite must run on a box with no browser installed.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.operator import browser_engine as be  # noqa: E402

IDLE = 1800.0
NOW = 10_000_000.0


class FakeHandle:
    """Stands in for a Playwright context or page."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


def _session(sid, *, last_used=None, page=True):
    s = be.OperatorSession(session_id=sid)
    s.context = FakeHandle()
    s.page = FakeHandle() if page else None
    if last_used is not None:
        s.last_used = last_used
    return s


# ── the reap rule (pure) ─────────────────────────────────────────────────────

def test_idle_session_is_collectable():
    sessions = {"old": _session("old", last_used=NOW - IDLE - 1)}
    assert be.idle_session_ids(sessions, now=NOW, idle_s=IDLE) == ["old"]


def test_recently_used_session_is_left_alone():
    sessions = {"live": _session("live", last_used=NOW - 5)}
    assert be.idle_session_ids(sessions, now=NOW, idle_s=IDLE) == []


def test_a_long_mission_between_steps_is_not_reaped():
    """An LLM call's duration is unbounded; a thinking session is still in use."""
    sessions = {"thinking": _session("thinking", last_used=NOW - (IDLE / 2))}
    assert be.idle_session_ids(sessions, now=NOW, idle_s=IDLE) == []


def test_session_with_no_page_is_always_collectable():
    """The shape a half-failed start leaves behind."""
    sessions = {"dead": _session("dead", last_used=NOW, page=False)}
    assert be.idle_session_ids(sessions, now=NOW, idle_s=IDLE) == ["dead"]


def test_touch_protects_a_session_from_the_next_sweep():
    s = _session("s", last_used=NOW - IDLE - 1)
    sessions = {"s": s}
    assert be.idle_session_ids(sessions, now=NOW, idle_s=IDLE) == ["s"]
    s.touch()
    assert be.idle_session_ids(sessions, now=time.time(), idle_s=IDLE) == []


def test_get_session_touches_so_an_active_mission_survives():
    """Every observe/act goes through get_session; that is what 'in use' means."""
    be._SESSIONS.clear()
    s = _session("m", last_used=NOW - IDLE - 1)
    be._SESSIONS["m"] = s
    try:
        assert be.get_session("m") is s
        assert be.idle_session_ids(be._SESSIONS, now=time.time(), idle_s=IDLE) == []
    finally:
        be._SESSIONS.clear()


# ── the sweep (closes handles, then the browser) ─────────────────────────────

def test_sweep_closes_the_abandoned_context_and_page():
    async def main():
        be._SESSIONS.clear()
        be._browser = None
        s = _session("abandoned", last_used=time.time() - IDLE - 1)
        ctx, page = s.context, s.page
        be._SESSIONS["abandoned"] = s
        res = await be.sweep_idle(idle_s=IDLE, linger_s=1e9)
        assert res["closed_sessions"] == ["abandoned"]
        assert ctx.closed and page.closed, "the browser handles must be released"
        assert be._SESSIONS == {}
    asyncio.run(main())


def test_sweep_leaves_a_live_session_open():
    async def main():
        be._SESSIONS.clear()
        be._browser = None
        be._SESSIONS["live"] = _session("live", last_used=time.time())
        res = await be.sweep_idle(idle_s=IDLE, linger_s=1e9)
        assert res["closed_sessions"] == []
        assert "live" in be._SESSIONS
        be._SESSIONS.clear()
    asyncio.run(main())


def test_browser_is_closed_only_after_the_linger_and_only_when_idle():
    """Chromium hands back little memory while it lives, so releasing the 8.4GB
    means actually closing it -- but not while a session still needs it, and not
    the instant one run ends."""
    async def main():
        be._SESSIONS.clear()
        be._browser_idle_since = None
        browser = FakeHandle()
        be._browser = browser

        # A live session pins the browser open.
        be._SESSIONS["live"] = _session("live", last_used=time.time())
        res = await be.sweep_idle(idle_s=IDLE, linger_s=0.0)
        assert res["browser_closed"] is False and not browser.closed
        be._SESSIONS.clear()

        # First idle sweep only starts the linger clock.
        res = await be.sweep_idle(idle_s=IDLE, linger_s=600.0)
        assert res["browser_closed"] is False and not browser.closed

        # Once the linger has elapsed it goes.
        be._browser_idle_since = time.time() - 601
        res = await be.sweep_idle(idle_s=IDLE, linger_s=600.0)
        assert res["browser_closed"] is True and browser.closed
        assert be._browser is None, "must be cleared so _get_browser relaunches"
    asyncio.run(main())


def test_a_new_session_resets_the_linger_clock():
    async def main():
        be._SESSIONS.clear()
        be._browser = FakeHandle()
        await be.sweep_idle(idle_s=IDLE, linger_s=600.0)
        assert be._browser_idle_since is not None
        be._SESSIONS["new"] = _session("new", last_used=time.time())
        await be.sweep_idle(idle_s=IDLE, linger_s=600.0)
        assert be._browser_idle_since is None
        be._SESSIONS.clear()
        be._browser = None
    asyncio.run(main())


# ── the leak itself: every run_loop must close its session on the way out ────

def _calls_named(node, name):
    """Does this subtree call something whose attribute/name is `name`?"""
    import ast
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == name:
                return True
            if isinstance(f, ast.Name) and f.id == name:
                return True
    return False


def test_every_run_loop_closes_its_session_in_a_finally():
    """THE regression. `close_session` sat after run_loop on the happy path, so
    a raised, timed-out or cancelled run never reached it. Assert structurally
    that each run_loop call is guarded by a try whose `finally` closes the
    session -- a future edit that moves the close back out will fail here."""
    import ast

    src_path = os.path.join(os.path.dirname(__file__), "..", "vera", "operator",
                            "operator_web_capabilities.py")
    with open(src_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    # Only functions that close a session OWN one. cap_step runs a single step
    # against a caller-supplied session and must NOT close it -- the caller does.
    owning_fns = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                  and _calls_named(n, "run_loop")
                  and _calls_named(n, "close_session")]
    assert owning_fns, "no run_loop call site closes a session - did the module move?"

    for fn in owning_fns:
        guarded = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try) or not node.finalbody:
                continue
            in_body = any(_calls_named(stmt, "run_loop") for stmt in node.body)
            closes = any(_calls_named(stmt, "close_session") for stmt in node.finalbody)
            if in_body and closes:
                guarded = True
                break
        assert guarded, (
            f"{fn.name}: run_loop is not wrapped in a try whose finally calls "
            "close_session - a cancelled or failed run will leak its browser "
            "context into the shared Chromium until the process exits"
        )


def test_close_session_is_idempotent_and_survives_a_broken_handle():
    async def main():
        be._SESSIONS.clear()

        class Boom(FakeHandle):
            async def close(self):
                raise RuntimeError("browser already gone")

        s = be.OperatorSession(session_id="x")
        s.context, s.page = Boom(), Boom()
        be._SESSIONS["x"] = s
        assert await be.close_session("x") is True     # must not raise
        assert await be.close_session("x") is False    # already gone
        be._SESSIONS.clear()
    asyncio.run(main())
