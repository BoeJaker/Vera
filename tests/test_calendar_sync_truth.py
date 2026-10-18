"""S23 / D24-4: the calendar sync tells the truth.

C-1 a weekly ICS event appears every week, a moved instance does not replace the series, EXDATEs and cancelled
instances are left out; C-2 an event deleted upstream leaves the diary at the next sync (only inside the window, never
on an empty feed); C-4 a sync never recreates a source deleted while it ran; C-3 "today" is the diary's own day.
Fixtures are recorded ICS text; Redis and HTTP are faked. Runs in the gate container (Vera.vera resolves to the branch)."""
import asyncio
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from Vera.vera.calendar import ical  # noqa: E402  (fails loudly, never skips)
import Vera.vera.calendar.calendar_capabilities as cc  # noqa: E402

LONDON = ZoneInfo("Europe/London")


def _ics(*vevents):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(
        "BEGIN:VEVENT\r\n" + "\r\n".join(v) + "\r\nEND:VEVENT\r\n" for v in vevents) + "END:VCALENDAR\r\n"


def _d(day, hour=9):                      # an ICS local time
    return day.strftime("%Y%m%d") + f"T{hour:02d}0000"


MON = dt.date(2026, 10, 5)                # a Monday; the clocks go back on 25 Oct 2026
SERIES = ["UID:standup", "SUMMARY:Standup", f"DTSTART;TZID=Europe/London:{_d(MON)}",
          f"DTEND;TZID=Europe/London:{_d(MON, 10)}", "RRULE:FREQ=WEEKLY;COUNT=6",
          f"EXDATE;TZID=Europe/London:{_d(MON + dt.timedelta(weeks=2))}"]
MOVED = ["UID:standup", "SUMMARY:Standup (moved)", f"RECURRENCE-ID;TZID=Europe/London:{_d(MON + dt.timedelta(weeks=1))}",
         f"DTSTART;TZID=Europe/London:{_d(MON + dt.timedelta(weeks=1, days=1), 11)}",
         f"DTEND;TZID=Europe/London:{_d(MON + dt.timedelta(weeks=1, days=1), 12)}"]
CANCELLED = ["UID:standup", "SUMMARY:Standup", "STATUS:CANCELLED",
             f"RECURRENCE-ID;TZID=Europe/London:{_d(MON + dt.timedelta(weeks=3))}",
             f"DTSTART;TZID=Europe/London:{_d(MON + dt.timedelta(weeks=3))}"]
ONE_OFF = ["UID:dentist", "SUMMARY:Dentist", "DTSTART:20261014T140000Z", "DTEND:20261014T143000Z"]
WS = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
WE = dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc)


def test_a_weekly_series_appears_every_week_with_its_exceptions():
    occ = ical.occurrences(ical.parse(_ics(SERIES, MOVED, CANCELLED, ONE_OFF)), WS, WE)
    standups = sorted((o for o in occ if o["uid"].startswith("standup_")), key=lambda o: o["start"])
    # 6 weeks: week 2 is an EXDATE, week 3 is cancelled, week 1 moved to Tuesday 11:00
    assert len(standups) == 4
    starts = [dt.datetime.fromisoformat(o["start"]) for o in standups]
    assert [s.astimezone(LONDON).strftime("%a %H:%M") for s in starts] == ["Mon 09:00", "Tue 11:00", "Mon 09:00", "Mon 09:00"]
    moved = standups[1]
    assert moved["title"] == "Standup (moved)" and moved["uid"] == "standup_" + _d(MON + dt.timedelta(weeks=1))
    # across the clock change the wall time stays 09:00 (BST, then GMT)
    assert {s.utcoffset() for s in starts} == {dt.timedelta(hours=1), dt.timedelta(0)}
    assert all(o["end"] and dt.datetime.fromisoformat(o["end"]) - dt.datetime.fromisoformat(o["start"]) == dt.timedelta(hours=1)
               for o in standups)
    assert [o["title"] for o in occ if o["uid"] == "dentist"] == ["Dentist"]


def test_instance_ids_are_stable_across_syncs():
    a = {o["uid"] for o in ical.occurrences(ical.parse(_ics(SERIES)), WS, WE)}
    b = {o["uid"] for o in ical.occurrences(ical.parse(_ics(SERIES)), WS, WE)}
    assert a == b and len(a) == 5


def test_a_series_that_cannot_expand_stays_one_event():
    bad = ["UID:odd", "SUMMARY:Odd", "DTSTART:20261005T090000Z", "RRULE:FREQ=NONSENSE"]
    assert [o["uid"] for o in ical.occurrences(ical.parse(_ics(bad)), WS, WE)] == ["odd"]


# ── the sync, with Redis and HTTP faked ─────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.h, self.z, self.kv = {}, {}, {}

    async def hset(self, k, f, v): self.h.setdefault(k, {})[f] = v
    async def hget(self, k, f): return self.h.get(k, {}).get(f)
    async def hgetall(self, k): return dict(self.h.get(k, {}))
    async def hmget(self, k, *fs): return [self.h.get(k, {}).get(f) for f in fs]
    async def get(self, k): return self.kv.get(k)
    async def set(self, k, v): self.kv[k] = v

    async def hdel(self, k, f):
        return 1 if self.h.get(k, {}).pop(f, None) is not None else 0

    async def zadd(self, k, m): self.z.setdefault(k, {}).update(m)
    async def zrem(self, k, f): self.z.get(k, {}).pop(f, None)

    async def zrangebyscore(self, k, lo, hi):
        return [m for m, s in sorted(self.z.get(k, {}).items(), key=lambda x: x[1]) if lo <= s <= hi]


class FakeClient:
    feed = ""

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url):
        text = FakeClient.feed
        return type("R", (), {"text": text, "raise_for_status": lambda self: None})()


@pytest.fixture
def env(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(cc, "_redis", lambda: r)
    monkeypatch.setattr(cc.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(cc, "_sync_window", lambda: (WS, WE))
    src = {"id": "ics1", "type": "ics", "label": "Work", "url": "https://example.invalid/work.ics", "enabled": True}
    asyncio.run(cc._save_source(src))
    return r, src


def _ids(r):
    return set(r.h.get(cc.KEY_EVENTS, {}))


def test_an_event_deleted_upstream_leaves_the_diary(env):
    r, src = env
    legacy = {"id": "ics1:standup", "title": "Standup", "start": "2026-01-05T09:00:00", "source": "ics1"}
    asyncio.run(cc._store_event(legacy))                         # the one-row copy the old sync stored
    FakeClient.feed = _ics(SERIES, ONE_OFF)
    res = asyncio.run(cc._sync_one(dict(src)))
    assert res["errors"] == 0 and "ics1:dentist" in _ids(r) and "ics1:standup" not in _ids(r)
    assert sum(i.startswith("ics1:standup_") for i in _ids(r)) == 5
    FakeClient.feed = _ics(SERIES)                               # the dentist was cancelled upstream
    res = asyncio.run(cc._sync_one(dict(src)))
    assert "ics1:dentist" not in _ids(r) and res["removed"] == 1
    FakeClient.feed = _ics()                                     # an empty feed removes nothing
    res = asyncio.run(cc._sync_one(dict(src)))
    assert res["removed"] == 0 and sum(i.startswith("ics1:standup_") for i in _ids(r)) == 5


def test_an_event_outside_the_window_is_not_swept(env):
    r, src = env
    asyncio.run(cc._store_event({"id": "ics1:old", "title": "Old", "start": "2025-01-01T09:00:00Z", "source": "ics1"}))
    FakeClient.feed = _ics(ONE_OFF)
    asyncio.run(cc._sync_one(dict(src)))
    assert "ics1:old" in _ids(r)


def test_a_source_deleted_during_a_sync_stays_deleted(env, monkeypatch):
    r, src = env
    FakeClient.feed = _ics(ONE_OFF)

    async def sync_then_deleted(s):
        out = await cc._upsert_feed(s, ical.parse(FakeClient.feed), WS, WE, set())
        await cc._hash_delete(cc.KEY_SOURCES, s["id"])            # cal.source.delete ran meanwhile
        return {**out, "errors": 0}
    monkeypatch.setattr(cc, "_sync_ics", sync_then_deleted)
    res = asyncio.run(cc._sync_one(dict(src)))
    assert res.get("deleted_meanwhile") and asyncio.run(cc._get_source("ics1")) is None
    assert not any(i.startswith("ics1:") for i in _ids(r))


def test_an_edit_made_during_a_sync_is_kept(env, monkeypatch):
    r, src = env

    async def sync_then_edited(s):
        await cc._save_source({**s, "label": "Renamed"})
        return {"added": 0, "updated": 0, "errors": 0}
    monkeypatch.setattr(cc, "_sync_ics", sync_then_edited)
    asyncio.run(cc._sync_one(dict(src)))
    saved = asyncio.run(cc._get_source("ics1"))
    assert saved["label"] == "Renamed" and saved["last_sync"]


def test_the_caldav_query_carries_the_window():
    body = cc._caldav_report_body(WS, WE)
    assert '<C:time-range start="20260901T000000Z" end="20261231T000000Z"/>' in body


def test_today_is_the_diarys_own_day():
    s, e = cc._resolve_range("today", "", "Europe/London")
    a, b = dt.datetime.fromtimestamp(s, LONDON), dt.datetime.fromtimestamp(e, LONDON)
    assert (a.hour, a.minute, b.hour) == (0, 0, 0) and (b.date() - a.date()).days == 1
    s_utc, _ = cc._resolve_range("today", "", "UTC")
    assert dt.datetime.fromtimestamp(s_utc, dt.timezone.utc).hour == 0
    s_bad, _ = cc._resolve_range("today", "", "Not/AZone")         # an unknown zone falls back to UTC
    assert s_bad == s_utc
