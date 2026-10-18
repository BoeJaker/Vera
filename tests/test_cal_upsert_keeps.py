"""cal.event.upsert edits an existing event in place.

Moving an event that came from a synced calendar (Google, CalDAV, ICS) used to
relabel it `local` and drop every field the caller did not send (uid, calendar
layer, created, ...). Now an upsert naming a stored id keeps the event's source
and its unsent fields; a new or unknown id still creates a local event.

The calendar module is loaded from this checkout with its app imports stubbed
and a dict-backed fake Redis - the real diary is never touched.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

HERE = os.path.dirname(os.path.abspath(__file__))
MOD_PATH = os.path.join(HERE, "..", "vera", "calendar", "calendar_capabilities.py")


class FakeRedis:
    def __init__(self):
        self.h, self.z = {}, {}

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def get(self, key):
        return None


def _load():
    def _identity_deco(*a, **k):
        return lambda f: f

    orch = types.ModuleType("Vera.vera.capability_orchestration")

    class _App:
        get = post = staticmethod(_identity_deco)

    async def _emit(*a, **k):
        return None

    orch.APP = _App()
    orch.capability = _identity_deco
    orch.emit_event = _emit
    orch.now_iso = lambda: "2026-10-17T00:00:00Z"
    orch.ollama_generate = None
    orch.register_ui = lambda *a, **k: None
    orch.schedule = lambda *a, **k: None
    orch.REDIS = FakeRedis()

    config = types.ModuleType("Vera.vera.config")
    config.cfg = types.SimpleNamespace(GOOGLE_OAUTH_REDIRECT_BASE="http://x")
    ical = types.ModuleType("Vera.vera.calendar.ical")
    effect = types.ModuleType("Vera.vera.calendar.effect_inventory")
    effect.calendar_effect_inventory = lambda *a, **k: {}
    secrets = types.ModuleType("Vera.vera.security.secrets")
    stubs = {
        "Vera": types.ModuleType("Vera"),
        "Vera.vera": types.ModuleType("Vera.vera"),
        "Vera.vera.capability_orchestration": orch,
        "Vera.vera.config": config,
        "Vera.vera.calendar": types.ModuleType("Vera.vera.calendar"),
        "Vera.vera.calendar.ical": ical,
        "Vera.vera.calendar.effect_inventory": effect,
        "Vera.vera.security": types.ModuleType("Vera.vera.security"),
        "Vera.vera.security.secrets": secrets,
    }
    stubs["Vera.vera.calendar"].ical = ical
    stubs["Vera.vera.calendar"].effect_inventory = effect
    stubs["Vera.vera.security"].secrets = secrets
    stubs["Vera.vera"].capability_orchestration = orch
    stubs["Vera.vera"].config = config
    stubs["Vera.vera"].calendar = stubs["Vera.vera.calendar"]
    stubs["Vera.vera"].security = stubs["Vera.vera.security"]
    stubs["Vera"].vera = stubs["Vera.vera"]
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("_cal_upsert_under_test", MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod, orch.REDIS


@pytest.fixture
def cal():
    return _load()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _stored(mod, redis, eid):
    return json.loads(redis.h[mod.KEY_EVENTS][eid])


def _seed_google(mod, redis):
    ev = {
        "id": "g1:abc", "title": "Dentist", "start": "2026-10-20T09:00:00",
        "end": "2026-10-20T10:00:00", "all_day": False, "location": "High St",
        "description": "check-up", "source": "g1", "color": "#ef5b5b",
        "uid": "abc", "calendar": "primary", "calendar_label": "Me",
        "attendees": ["a@example.com"], "recurrence": "RRULE:FREQ=YEARLY",
        "tags": ["health"], "created": "2026-01-01T00:00:00Z",
    }
    redis.h.setdefault(mod.KEY_EVENTS, {})[ev["id"]] = json.dumps(ev)
    return ev


def test_moving_a_synced_event_keeps_source_and_unsent_fields(cal):
    mod, redis = cal
    orig = _seed_google(mod, redis)
    # what the desk sends when it moves an event
    res = _run(mod.cap_event_upsert(
        id="g1:abc", title="Dentist", start="2026-10-21T11:00:00",
        end="2026-10-21T12:00:00", all_day=False, location="High St",
        color="", description="check-up", tags=["health"]))
    assert res["ok"] is True
    ev = _stored(mod, redis, "g1:abc")
    assert res["event"] == ev
    assert ev["source"] == "g1"
    assert ev["start"] == "2026-10-21T11:00:00" and ev["end"] == "2026-10-21T12:00:00"
    for k in ("uid", "calendar", "calendar_label", "attendees", "recurrence",
              "created", "color", "tags"):
        assert ev[k] == orig[k], k
    assert len(redis.h[mod.KEY_EVENTS]) == 1          # edited, not duplicated


def test_a_sent_field_changes(cal):
    mod, redis = cal
    _seed_google(mod, redis)
    _run(mod.cap_event_upsert(
        id="g1:abc", title="Dentist (moved)", start="2026-10-20T09:00:00",
        location="Low St", color="#123456", tags=[]))
    ev = _stored(mod, redis, "g1:abc")
    assert ev["title"] == "Dentist (moved)"
    assert ev["location"] == "Low St"
    assert ev["color"] == "#123456"
    assert ev["tags"] == []
    assert ev["source"] == "g1" and ev["uid"] == "abc"


def test_tags_not_sent_are_kept(cal):
    mod, redis = cal
    _seed_google(mod, redis)
    _run(mod.cap_event_upsert(id="g1:abc", title="Dentist",
                              start="2026-10-20T09:00:00"))
    assert _stored(mod, redis, "g1:abc")["tags"] == ["health"]


def test_new_event_is_local(cal):
    mod, redis = cal
    res = _run(mod.cap_event_upsert(title="Lunch", start="2026-10-22T12:00:00"))
    ev = _stored(mod, redis, res["event"]["id"])
    assert ev["source"] == "local"
    assert ev["color"] == mod.SOURCE_COLORS["local"]
    assert ev["tags"] == []


def test_unknown_id_creates_local_event_with_that_id(cal):
    mod, redis = cal
    res = _run(mod.cap_event_upsert(id="nope", title="Gym",
                                    start="2026-10-23T07:00:00", tags=None))
    assert res["event"]["id"] == "nope"
    ev = _stored(mod, redis, "nope")
    assert ev["source"] == "local"
    assert ev["color"] == mod.SOURCE_COLORS["local"]
    assert "uid" not in ev


def test_required_fields_still_enforced(cal):
    mod, _ = cal
    assert _run(mod.cap_event_upsert(id="x", start="2026-10-23")) == {"error": "title is required"}
    assert _run(mod.cap_event_upsert(id="x", title="t")) == {"error": "start is required"}
