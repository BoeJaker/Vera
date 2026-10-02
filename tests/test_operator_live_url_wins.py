"""operator target kind='live' with an explicit url opens the url (live 2026-10-02:
the loop passed the session's preview url WITH kind='live'; the url was dropped and the
operator drove Vera's own dashboard - 8 of 10 live-target runs failed)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from vera.operator import targets as T
except Exception:                                    # pragma: no cover
    T = None

import pytest

needs = pytest.mark.skipif(T is None, reason="operator.targets not importable here")
PREVIEW = "https://localhost:8999/remote/sandbox/preview/ee1ff5f6-8815-40ba-b950-0/timer.html"


@needs
def test_live_with_an_explicit_url_starts_on_the_url():
    r = T.resolve_target({"kind": "live", "url": PREVIEW}, "https://localhost:8999")
    assert r["kind"] == "live" and r["start_url"] == PREVIEW


@needs
def test_live_without_a_url_still_opens_the_dashboard():
    r = T.resolve_target({"kind": "live"}, "https://localhost:8999")
    assert r["start_url"] == "https://localhost:8999"


@needs
def test_a_panel_still_wins_over_the_dashboard_root():
    r = T.resolve_target({"kind": "live", "panel_id": "census"}, "https://localhost:8999")
    assert "census" in r["start_url"]


@needs
def test_the_other_kinds_are_unchanged():
    assert T.resolve_target({"kind": "url", "url": PREVIEW})["start_url"] == PREVIEW
    assert T.resolve_target({"kind": "sandbox", "url": PREVIEW})["start_url"] == PREVIEW
