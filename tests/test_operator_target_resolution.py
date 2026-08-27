"""An explicit url must reach the browser, whatever `kind` says.

Session 5be4562e, 2026-08-27. The loop asked the operator to verify a countdown
page and passed BOTH a correct preview url and kind="sandbox". In the operator's
target vocabulary "sandbox" means a LOOP LAB dev container, not the session's
workspace preview - so resolve_target returned that base with an EMPTY start_url,
_open_session skipped its goto, and the browser opened on about:blank.

The operator then pursued "click the Start button and watch it count down" on a
blank page: it tried timeanddate.com and stopwatch.net (both allowlist-blocked),
then drove Vera's own UI at localhost:8999 clicking "Pause", and reported
reason="too_many_errors". Every call returned ok=true, so the step retried twice,
fell back to lsof/netstat probing (neither installed) and spawned a third step
that timed out on render.html. Roughly fifteen cycles from one dropped argument.

Pure: resolve_target does no I/O.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator.targets import resolve_target  # noqa: E402

PREVIEW = "https://localhost:8999/remote/sandbox/preview/abc123/countdown.html"


def test_an_explicit_url_survives_kind_sandbox():
    """The exact call the loop made."""
    r = resolve_target({"kind": "sandbox", "url": PREVIEW})
    assert r["start_url"] == PREVIEW, \
        "the url was dropped again - the browser would open on about:blank"


def test_the_sandbox_kind_still_works_without_a_url():
    """Its original meaning is untouched: a Loop Lab surface with no start page."""
    r = resolve_target({"kind": "sandbox"})
    assert r["kind"] == "sandbox"
    assert r["start_url"] == ""


def test_a_panel_id_still_wins_for_sandbox():
    """panel_id addresses a specific surface and keeps precedence."""
    r = resolve_target({"kind": "sandbox", "panel_id": "chat", "url": PREVIEW})
    assert r["start_url"] and r["start_url"] != PREVIEW
    assert "chat" in r["start_url"]


def test_plain_url_targets_are_unchanged():
    r = resolve_target({"url": PREVIEW})
    assert r["kind"] == "url" and r["start_url"] == PREVIEW


@pytest.mark.parametrize("kind", ["live", "panel", "codeserver"])
def test_other_kinds_still_resolve_a_start_url(kind):
    """Only `sandbox` could return an empty start_url; the others must not regress."""
    r = resolve_target({"kind": kind, "base_url": "http://localhost:8999",
                        "panel_id": "chat", "id": "cs1"})
    assert str(r.get("start_url") or "").strip(), "%s lost its start_url" % kind


def test_a_sandbox_url_sets_a_usable_base():
    """base_url is what the allowlist is derived from - a wrong base blocks the page."""
    r = resolve_target({"kind": "sandbox", "url": PREVIEW})
    assert str(r.get("base_url") or "").strip(), "no base_url to allowlist against"
