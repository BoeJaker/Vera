"""Half-life per kind of memory, renewed by use (vera/fabric/memory_strength_core.py; context-and-ui CU-M.3).

Owner, 2026-10-04: useless session memories fade, useful ones stay.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.fabric import memory_strength_core as s  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400.0


def rec(kind="message", age_days=30, importance=0.5):
    return {"record_type": kind, "importance": importance, "created_at": NOW - age_days * DAY}


def test_a_used_memory_outlives_an_unused_one_of_the_same_age():
    unused = s.strength(rec(), None, NOW)
    used = s.strength(rec(), {"used_n": 3, "injected_n": 3, "last_used": NOW - 2 * DAY}, NOW)
    assert used > unused * 3


def test_one_half_life_halves_it():
    assert s.strength(rec(age_days=14), None, NOW) == pytest.approx(0.25, rel=1e-3)


def test_given_and_ignored_fades_sooner():
    plain = s.strength(rec(age_days=10), None, NOW)
    ignored = s.strength(rec(age_days=10), {"used_n": 0, "injected_n": 15}, NOW)
    assert ignored < plain / 2


def test_a_revived_memory_returns():
    faded = rec(age_days=200)
    assert s.fades(faded, None, NOW)
    assert not s.fades(faded, {"used_n": 1, "injected_n": 1, "last_used": NOW - 3600}, NOW)


def test_facts_and_preferences_do_not_decay():
    assert s.strength(rec("fact", age_days=900, importance=0.8), None, NOW) == 0.8
    assert not s.fades(rec("preference", age_days=5000), None, NOW)


@pytest.mark.parametrize("record, cls", [
    ({"record_type": "message"}, "conversation"), ({"record_type": "dream"}, "dream"),
    ({"record_type": "cap_call"}, "tool"), ({"source_type": "telemetry"}, "estate"),
    ({"text": "[Claude Code] did a thing"}, "transcript"), ({}, "conversation"),
])
def test_class_of(record, cls):
    assert s.class_of(record) == cls


def test_timestamps_in_every_form_the_stores_give():
    iso = s.strength({"record_type": "message", "created_at": "2027-01-15T08:00:00Z"}, None, NOW)
    assert 0 < iso <= 0.5
    assert s.strength({"record_type": "message"}, None, NOW) == 0.5      # no date: treated as new, never as ancient


def test_the_policy_says_which_table_is_in_force():
    assert s.POLICY_VERSION and set(s.HALF_LIFE) >= {"conversation", "session", "fact", "dream", "tool", "estate"}


def test_a_transcript_line_stored_as_a_message_is_a_transcript():
    # prod: 76,130 of 82,137 record_type "message" rows are imported coding-agent transcript lines
    assert s.class_of({"record_type": "message", "text": "[Claude Code · repo] [tool_use: Edit]"}) == "transcript"
    assert s.class_of({"record_type": "message", "text": "  [Codex] ran tests"}) == "transcript"
    assert s.class_of({"record_type": "message", "text": "what did we decide about the census"}) == "conversation"


# -- the user's controls (owner, 2026-10-05: "add controls for the user to select what gets forgotten and how quickly")
def test_the_policy_lays_the_users_settings_over_the_defaults():
    P = s.policy_from({"conversation": {"half_life_days": 60}, "tool": {"enabled": False}, "bogus": {"enabled": True}})
    assert P["conversation"]["half_life_days"] == 60 and P["conversation"]["enabled"]
    assert P["tool"]["enabled"] is False and "bogus" not in P
    assert P["fact"]["enabled"] is False                     # no decay, nothing to fade by


def test_settings_are_clamped_so_nothing_can_fade_everything_overnight():
    P = s.policy_from({"dream": {"half_life_days": 0.001, "floor": 0.99}, "session": {"half_life_days": "x"}})
    assert P["dream"]["half_life_days"] == s.HALF_LIFE_RANGE[0] and P["dream"]["floor"] == s.FLOOR_RANGE[1]
    assert P["session"]["half_life_days"] == 7.0               # a bad value keeps the default


def test_a_class_the_user_keeps_never_fades_and_a_slower_one_fades_later():
    old_chat = rec(age_days=100)
    assert s.fades(old_chat, None, NOW)
    assert not s.fades(old_chat, None, NOW, s.policy_from({"conversation": {"half_life_days": 60}}))
    assert not s.fades(old_chat, None, NOW, s.policy_from({"conversation": {"enabled": False}}))
    assert not s.fades(rec("cap_call", age_days=300), None, NOW, s.policy_from({"tool": {"enabled": False}}))
