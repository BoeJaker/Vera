"""The idle-sleep tick must sleep the container it judged — never an alias's target.

Pins vera.remote.sandbox_idle_core. The regression (2026-09-12, prod): a chat
session linked to a goal's container kept its own record (own container still
running, last_used never bumped again), the tick judged that record idle and
slept the GOAL's container through the alias — every 120 s, each time waking
it first to package context. Also pins the container-name rule that let two
goals differing only after character 48 share one container.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.remote.sandbox_idle_core import (  # noqa: E402
    IdlePlan, container_name, idle_plan, own_container_to_retire,
)

NOW = 1_000_000.0
IDLE = 1800.0


def _rec(sid, container, *, active=True, last_used=None, host="local"):
    r = {"session_id": sid, "container": container, "active": active,
         "docker_host_id": host}
    if last_used is not None:
        r["last_used"] = last_used
    return r


def _ids(recs):
    return sorted(r["session_id"] for r in recs)


def test_alias_is_never_a_sleep_target_and_its_leftover_container_is_an_orphan():
    goal = _rec("goal-x", "vera-sbx-goal-x", last_used=NOW - 10)      # in use
    alias = _rec("chat-1", "vera-sbx-chat-1", last_used=NOW - 99999)  # stale, linked
    plan = idle_plan([goal, alias], {"chat-1": "goal-x"}, now=NOW, idle_s=IDLE)
    assert _ids(plan.due) == []                     # goal is fresh; alias never judged
    assert _ids(plan.orphans) == ["chat-1"]         # its own container must be retired
    assert plan.unstamped == []


def test_alias_that_already_points_at_the_targets_container_is_not_an_orphan():
    goal = _rec("goal-x", "vera-sbx-goal-x", last_used=NOW - 99999)
    alias = _rec("dream-1", "vera-sbx-goal-x", last_used=NOW - 99999)
    plan = idle_plan([goal, alias], {"dream-1": "goal-x"}, now=NOW, idle_s=IDLE)
    assert _ids(plan.orphans) == []
    assert _ids(plan.due) == ["goal-x"]             # the OWNER is judged, once


def test_owner_idle_fresh_and_unstamped():
    idle = _rec("a", "vera-sbx-a", last_used=NOW - IDLE)        # exactly at threshold
    fresh = _rec("b", "vera-sbx-b", last_used=NOW - IDLE + 1)
    new = _rec("c", "vera-sbx-c")                                # no stamp → grace
    plan = idle_plan([idle, fresh, new], {}, now=NOW, idle_s=IDLE)
    assert _ids(plan.due) == ["a"]
    assert _ids(plan.unstamped) == ["c"]


def test_inactive_or_containerless_records_are_ignored():
    off = _rec("off", "vera-sbx-off", active=False, last_used=NOW - 99999)
    stub = {"session_id": "stub", "active": True, "last_used": NOW - 99999}
    plan = idle_plan([off, stub], {}, now=NOW, idle_s=IDLE)
    assert plan == IdlePlan()


def test_two_records_sharing_one_container_yield_one_sleep():
    a = _rec("goal-...-of", "vera-sbx-goal-obtain-recent", last_used=NOW - 99999)
    b = _rec("goal-...-of-your", "vera-sbx-goal-obtain-recent", last_used=NOW - 99999)
    other_host = _rec("c", "vera-sbx-goal-obtain-recent", last_used=NOW - 99999, host="h2")
    plan = idle_plan([a, b, other_host], {}, now=NOW, idle_s=IDLE)
    assert len(plan.due) == 2
    assert {(r["docker_host_id"], r["container"]) for r in plan.due} == {
        ("local", "vera-sbx-goal-obtain-recent"), ("h2", "vera-sbx-goal-obtain-recent")}


def test_self_alias_is_treated_as_an_owner():
    a = _rec("a", "vera-sbx-a", last_used=NOW - 99999)
    plan = idle_plan([a], {"a": "a"}, now=NOW, idle_s=IDLE)
    assert _ids(plan.due) == ["a"]


# ── container naming ────────────────────────────────────────────────────────

def test_short_ids_keep_their_historical_spelling():
    assert container_name("chat-1788088763932") == "vera-sbx-chat-1788088763932"
    assert container_name("a b/c", suffix="-ws") == "vera-sbx-a-b-c-ws"
    sid48 = "x" * 48
    assert container_name(sid48) == "vera-sbx-" + sid48


def test_long_ids_that_differ_late_get_distinct_bounded_names():
    base = "goal-obtain-recent-daily-price-history-for-a-stock-symbol-of"
    a, b = container_name(base), container_name(base + "-your")
    assert a != b
    for n in (a, b):
        assert n.startswith("vera-sbx-")
        assert len(n) <= len("vera-sbx-") + 48
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+", n)
    # deterministic, and the volume is always the container name + "-ws"
    assert container_name(base) == a
    assert container_name(base, suffix="-ws") == a + "-ws"


# ── what a link must retire ────────────────────────────────────────────────

def test_own_container_to_retire():
    target = {"session_id": "goal-x", "container": "vera-sbx-goal-x"}
    assert own_container_to_retire(None, target) is None
    assert own_container_to_retire({"session_id": "s"}, target) is None
    assert own_container_to_retire({"session_id": "s", "container": "local:vera-sbx-s"}, target) is None
    assert own_container_to_retire({"session_id": "s", "container": "vera-sbx-goal-x"}, target) is None
    assert own_container_to_retire({"session_id": "s", "container": "vera-sbx-s"}, target) == "vera-sbx-s"
    assert own_container_to_retire({"session_id": "s", "container": "vera-sbx-s"}, None) == "vera-sbx-s"
