"""Save a template, seed it, shrink it — the round trip, against a fake Redis.

`census_seed` is pure and covered on its own. What is NOT covered by that is
the part that actually touches state: does `save` persist, does `seed` write
the tasks under the right ids, and does a template that LOSES a goal remove the
task it used to have?

That last one is the reason this file exists. A stale task keeps its tag, so it
keeps running and keeps scoring, and the census's question set silently widens
by one — the timeline then compares runs that asked different questions, which
is the single failure this whole migration is meant to prevent.

No dev sandbox can reach Redis (it listens on 127.0.0.1 only, while containers
resolve host.docker.internal to the docker bridge at 172.17.0.1 — verified
2026-09-07), so this cannot be checked live outside prod. A fake is not a
substitute for prod, but it exercises the real capability functions rather than
their description, and it runs in the gate.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EC = pytest.importorskip("vera.evolve.evolve_capabilities",
                         reason="app module not importable here")


class FakeRedis:
    """Just the operations these capabilities use."""

    def __init__(self):
        self.h = {}          # hashes
        self.lists = {}
        self.sets = {}

    async def hlen(self, key):
        return len(self.h.get(key, {}))

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, a, b):
        self.lists[key] = self.lists.get(key, [])[a:b + 1]

    async def sadd(self, key, *vals):
        self.sets.setdefault(key, set()).update(vals)


@pytest.fixture
def redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(EC, "_redis", lambda: r)
    # The harness directory does not exist on a test host; make that explicit
    # rather than depending on it.
    monkeypatch.setenv("VERA_CENSUS_DIR", "/nonexistent-census-dir")
    return r


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


TPL = {
    "name": "unit", "description": "two goals",
    "goals": [
        {"id": "alpha", "goal": "do alpha", "intent": "build", "tier": "simple",
         "checks": [{"file": "a.txt", "exists": True}]},
        {"id": "beta", "goal": "do beta", "intent": "fix", "tier": "simple",
         "checks": [{"type": "final_nonempty"}]},
    ],
}


def _tasks(redis):
    return {k: json.loads(v) for k, v in redis.h.get(EC.KEY_TASKS, {}).items()}


def _census_tasks(redis):
    """Only the seeded ones. Reading the task hash calls _ensure_seeded, which
    auto-populates the 14 in-code default tasks when it is empty — they are
    supposed to be there, and the census set lives alongside them."""
    return {k: v for k, v in _tasks(redis).items() if k.startswith("census-")}


# ── the round trip ──────────────────────────────────────────────────────────
def test_a_saved_template_comes_back(redis):
    assert run(EC.evolve_census_template_save(template=TPL))["ok"] is True
    got = run(EC.evolve_census_templates(name="unit"))
    assert got["template"]["name"] == "unit"
    assert got["tag"] == "census-unit"
    assert got["problems"] == []


def test_seeding_writes_one_task_per_goal(redis):
    run(EC.evolve_census_template_save(template=TPL))
    res = run(EC.evolve_census_template_seed(name="unit"))
    assert res["tag"] == "census-unit"
    assert sorted(res["seeded"]) == ["census-unit-alpha", "census-unit-beta"]
    saved = _tasks(redis)
    assert saved["census-unit-alpha"]["goal"] == "do alpha"
    assert saved["census-unit-alpha"]["profile"] == "planning"
    assert saved["census-unit-alpha"]["timeout_s"] == 1800


def test_the_tag_selects_exactly_this_template(redis):
    run(EC.evolve_census_template_save(template=TPL))
    run(EC.evolve_census_template_seed(name="unit"))
    listed = run(EC.evolve_tasks(tag="census-unit"))
    assert listed["count"] == 2
    assert all("census-unit" in t["tags"] for t in listed["tasks"])


def test_reseeding_is_idempotent(redis):
    run(EC.evolve_census_template_save(template=TPL))
    run(EC.evolve_census_template_seed(name="unit"))
    run(EC.evolve_census_template_seed(name="unit"))
    assert len(_census_tasks(redis)) == 2


# ── the one that matters ────────────────────────────────────────────────────
def test_a_goal_removed_from_the_template_loses_its_task(redis):
    """Otherwise it keeps running, keeps scoring, and the question set widens
    without anyone editing it."""
    run(EC.evolve_census_template_save(template=TPL))
    run(EC.evolve_census_template_seed(name="unit"))
    smaller = {"name": "unit", "goals": [TPL["goals"][0]]}
    run(EC.evolve_census_template_save(template=smaller, force=True))
    res = run(EC.evolve_census_template_seed(name="unit"))
    assert res["pruned"] == ["census-unit-beta"]
    assert set(_census_tasks(redis)) == {"census-unit-alpha"}


def test_pruning_can_be_declined(redis):
    run(EC.evolve_census_template_save(template=TPL))
    run(EC.evolve_census_template_seed(name="unit"))
    smaller = {"name": "unit", "goals": [TPL["goals"][0]]}
    run(EC.evolve_census_template_save(template=smaller, force=True))
    res = run(EC.evolve_census_template_seed(name="unit", prune=False))
    assert res["pruned"] == []
    assert "census-unit-beta" in _tasks(redis)


def test_seeding_never_touches_another_templates_tasks(redis):
    run(EC.evolve_census_template_save(template=TPL))
    run(EC.evolve_census_template_seed(name="unit"))
    other = {"name": "other", "goals": [TPL["goals"][0]]}
    run(EC.evolve_census_template_save(template=other))
    run(EC.evolve_census_template_seed(name="other"))
    assert set(_census_tasks(redis)) == {"census-unit-alpha", "census-unit-beta",
                                         "census-other-alpha"}


def test_seeding_leaves_the_hand_written_loop_lab_tasks_alone(redis):
    """The census set moves in next door; it does not evict the tenants."""
    run(EC.evolve_tasks())          # _ensure_seeded populates the defaults
    before = set(_tasks(redis)) - set(_census_tasks(redis))
    run(EC.evolve_census_template_save(template=TPL))
    run(EC.evolve_census_template_seed(name="unit"))
    smaller = {"name": "unit", "goals": [TPL["goals"][0]]}
    run(EC.evolve_census_template_save(template=smaller, force=True))
    run(EC.evolve_census_template_seed(name="unit"))
    assert before and set(_tasks(redis)) - set(_census_tasks(redis)) == before


# ── refusing to make meaningless numbers ────────────────────────────────────
def test_a_bad_template_is_not_saved(redis):
    bad = {"name": "bad", "goals": [{"id": "a", "goal": "g", "checks": []}]}
    res = run(EC.evolve_census_template_save(template=bad))
    assert res.get("error") and res["problems"]
    assert EC.KEY_CENSUS_TEMPLATES not in redis.h


def test_force_saves_it_anyway(redis):
    bad = {"name": "bad", "goals": [{"id": "a", "goal": "g", "checks": []}]}
    assert run(EC.evolve_census_template_save(template=bad, force=True))["ok"]


def test_a_forced_bad_template_still_cannot_be_seeded(redis):
    """Saving a draft is one thing; spending half a day of GPU on it is
    another."""
    bad = {"name": "bad", "goals": [{"id": "a", "goal": "g", "checks": []}]}
    run(EC.evolve_census_template_save(template=bad, force=True))
    assert run(EC.evolve_census_template_seed(name="bad")).get("error")


def test_seeding_an_unknown_template_says_what_it_knows(redis):
    run(EC.evolve_census_template_save(template=TPL))
    res = run(EC.evolve_census_template_seed(name="nope"))
    assert res.get("error") and "unit" in res.get("known", [])


# ── an edit that rebases the timeline says so ───────────────────────────────
def test_replacing_a_template_reports_what_it_breaks(redis):
    run(EC.evolve_census_template_save(template=TPL))
    changed = {"name": "unit", "goals": TPL["goals"], "model": "llama3:8b"}
    res = run(EC.evolve_census_template_save(template=changed))
    assert res["replaced"] is True
    assert any("model" in b for b in res["breaks_comparability"])
    assert res["note"]


def test_an_unchanged_resave_reports_nothing_broken(redis):
    run(EC.evolve_census_template_save(template=TPL))
    res = run(EC.evolve_census_template_save(template=dict(TPL)))
    assert res["breaks_comparability"] == [] and res["note"] == ""


# ── the model pin ───────────────────────────────────────────────────────────
def test_a_model_can_be_pinned_at_seed_time(redis):
    run(EC.evolve_census_template_save(template=TPL))
    res = run(EC.evolve_census_template_seed(name="unit", model="llama3:8b"))
    assert res["model"] == "llama3:8b"
    assert _tasks(redis)["census-unit-alpha"]["overrides"] == {"model": "llama3:8b"}


def test_no_pin_leaves_the_live_routing_alone(redis):
    run(EC.evolve_census_template_save(template=TPL))
    res = run(EC.evolve_census_template_seed(name="unit"))
    assert res["model"] == "live routing default"
    assert "overrides" not in _tasks(redis)["census-unit-alpha"]


# ── dry run ─────────────────────────────────────────────────────────────────
def test_a_dry_run_writes_nothing(redis):
    run(EC.evolve_census_template_save(template=TPL))
    res = run(EC.evolve_census_template_seed(name="unit", dry_run=True))
    assert res["dry_run"] is True
    assert sorted(res["would_seed"]) == ["census-unit-alpha", "census-unit-beta"]
    assert _census_tasks(redis) == {}
