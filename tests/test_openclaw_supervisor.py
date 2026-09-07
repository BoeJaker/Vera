"""Two costs the bridge kept paying, once each connection existed (2026-09-05).

1. Every prompt spent a round-trip being told `agentId` is unexpected before
   the retry that worked. The gateway's own log showed the pair on each send:
       ⇄ res ✗ chat.send … unexpected property 'agentId'
   A refusal is worth learning once, per gateway version.

2. `_maybe_autostart` was scheduled with `interval=0`, which the scheduler
   reads as "every tick" — a new reconnect loop every second, each orphaning
   the last, all of them ready to dial simultaneously. A periodic starter has
   to be a supervisor.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.openclaw import openclaw_device_core as dev  # noqa: E402

REFUSAL = ("invalid chat.send params: at root: unexpected property 'agentId'")


# ── learning a refusal once ──────────────────────────────────────────────────

def test_a_known_bad_property_is_never_sent_again():
    support = dev.ParamSupport()
    support.reset_for_version("2026.4.29")
    params = {"sessionKey": "s", "message": "m", "idempotencyKey": "k",
              "agentId": "main"}

    first = support.strip("chat.send", params)
    assert "agentId" in first                      # nothing learned yet

    support.record("chat.send", dev.unexpected_properties(REFUSAL))
    second = support.strip("chat.send", params)
    assert "agentId" not in second
    assert second["sessionKey"] == "s" and second["idempotencyKey"] == "k"


def test_stripping_does_not_mutate_the_callers_params():
    support = dev.ParamSupport()
    support.record("chat.send", ["agentId"])
    original = {"sessionKey": "s", "agentId": "main"}
    support.strip("chat.send", original)
    assert original == {"sessionKey": "s", "agentId": "main"}


def test_a_refusal_is_scoped_to_the_method_that_earned_it():
    support = dev.ParamSupport()
    support.record("chat.send", ["agentId"])
    assert support.strip("sessions.list", {"agentId": "main"}) == {"agentId": "main"}


def test_only_news_is_reported_as_learned():
    support = dev.ParamSupport()
    assert support.record("chat.send", ["agentId"]) == ["agentId"]
    assert support.record("chat.send", ["agentId"]) == []
    assert support.record("chat.send", ["thinking", "agentId"]) == ["thinking"]


def test_blank_property_names_are_ignored():
    support = dev.ParamSupport()
    assert support.record("chat.send", ["", None]) == []
    assert support.dropped("chat.send") == []


# ── forgetting it when the gateway changes ───────────────────────────────────

def test_an_upgraded_gateway_is_probed_again():
    support = dev.ParamSupport()
    support.reset_for_version("2026.4.29")
    support.record("chat.send", ["agentId"])

    assert support.reset_for_version("2026.9.1") is True
    assert support.dropped("chat.send") == []       # 2026.9.x accepts agentId
    assert support.version == "2026.9.1"


def test_reconnecting_to_the_same_version_keeps_what_was_learned():
    support = dev.ParamSupport()
    support.reset_for_version("2026.4.29")
    support.record("chat.send", ["agentId"])
    assert support.reset_for_version("2026.4.29") is False
    assert support.dropped("chat.send") == ["agentId"]


def test_a_version_change_with_nothing_learned_reports_no_clear():
    support = dev.ParamSupport()
    assert support.reset_for_version("2026.4.29") is False


def test_a_gateway_that_names_no_version_costs_us_nothing():
    """Silence is not an upgrade — an unknown version must not wipe the memo."""
    support = dev.ParamSupport()
    support.reset_for_version("2026.4.29")
    support.record("chat.send", ["agentId"])
    assert support.reset_for_version("") is False
    assert support.dropped("chat.send") == ["agentId"]
    assert support.version == "2026.4.29"


def test_the_memo_is_visible_from_outside():
    support = dev.ParamSupport()
    support.record("chat.send", ["agentId"])
    assert support.snapshot() == {"chat.send": ["agentId"]}
    assert dev.ParamSupport().snapshot() == {}


# ── one reconnect loop, not one per tick ─────────────────────────────────────

def test_a_live_loop_is_never_duplicated():
    assert dev.should_start_supervisor(enabled=True, task_alive=True) is False


def test_a_dead_loop_is_restarted():
    assert dev.should_start_supervisor(enabled=True, task_alive=False) is True


def test_nothing_starts_while_the_bridge_is_disabled():
    assert dev.should_start_supervisor(enabled=False, task_alive=False) is False
    assert dev.should_start_supervisor(enabled=False, task_alive=True) is False


def test_repeated_ticks_start_exactly_one_loop():
    """What the scheduler actually does: call it again, and again, and again."""
    started = 0
    alive = False
    for _ in range(100):
        if dev.should_start_supervisor(enabled=True, task_alive=alive):
            started += 1
            alive = True          # the loop it just started is now running
    assert started == 1

    alive = False                 # the loop dies
    for _ in range(10):
        if dev.should_start_supervisor(enabled=True, task_alive=alive):
            started += 1
            alive = True
    assert started == 2           # exactly one replacement
