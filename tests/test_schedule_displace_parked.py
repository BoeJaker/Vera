"""A census parked on ANOTHER schedule's window-end yield does not block the next due
census (1 Oct 2026: operator-family parked at 07:00 and blocked the whole day)."""

import asyncio
import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import schedule_core as S  # noqa: E402

NOW = datetime(2026, 10, 1, 6, 5, tzinfo=timezone.utc)          # 07:05 Europe/London
BASELINE = {"id": "base", "kind": "census", "enabled": True, "days": list(range(7)),
            "start": "07:00", "end": "09:30", "timezone": "Europe/London",
            "repeat": "once_per_window", "target": {"template": "default"}}


def _state(**kw):
    st = {"census_running": True, "census_parked_by_us": True, "window_end_applied": True,
          "census_owner": "operator", "loops_running": 0, "harness_blocked": ""}
    st.update(kw)
    return st


def test_a_due_census_displaces_one_parked_on_another_schedules_window_end():
    assert S.is_due(BASELINE, NOW, _state()) == (True, S.DISPLACE)


def test_its_own_parked_run_is_resumed_not_displaced():
    assert S.is_due(BASELINE, NOW, _state(census_owner="base")) == (True, "resume")


def test_a_running_census_still_blocks():
    assert S.is_due(BASELINE, NOW, _state(census_parked_by_us=False)) == (False, "census in flight")


def test_a_persons_pause_is_never_displaced():
    # parked_by_us is False when the control on file is not the scheduler's own
    assert S.is_due(BASELINE, NOW, _state(census_parked_by_us=False, window_end_applied=False))[0] is False


def test_a_park_that_was_not_a_window_end_is_not_displaced():
    assert S.is_due(BASELINE, NOW, _state(window_end_applied=False)) == (False, "census in flight")


def test_the_tick_drops_the_parked_run_without_counting_a_start(monkeypatch):
    try:
        from Vera.vera.evolve import schedule_capabilities as SK
    except Exception:                                # pragma: no cover
        pytest.skip("app module not importable here")
    if not hasattr(SK.core, "DISPLACE"):
        pytest.skip("app module resolves to a checkout without this change")
    calls, saved = [], []

    async def get_config():
        return {"enabled": True}

    async def box_state():
        return _state()

    async def finalize(state):
        return None

    async def load_all():
        return [dict(BASELINE)]

    async def call(name, **kw):
        calls.append((name, kw.get("action")))
        return {"ok": True}

    async def save(rec):
        saved.append(rec)

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(SK, "_get_config", get_config)
    monkeypatch.setattr(SK, "box_state", box_state)
    monkeypatch.setattr(SK, "_finalize_census_if_done", finalize)
    monkeypatch.setattr(SK, "_load_all", load_all)
    monkeypatch.setattr(SK, "_call", call)
    monkeypatch.setattr(SK, "_save", save)
    monkeypatch.setattr(SK, "_append_run", nothing)
    monkeypatch.setattr(SK, "_owner", nothing)
    monkeypatch.setattr(SK, "_set_owner", nothing)
    monkeypatch.setattr(SK.core, "window_end_action", lambda rec, now, state: None)
    out = asyncio.run(SK.run_tick(now=NOW))
    assert ("census.control.set", "drop") in calls
    assert out["started"] == [] and saved == []            # not a start: still due next tick
    assert out["controls"][0]["action"] == "drop-parked"
