"""Vera dialled 25 hosts that could not answer, every 60 seconds, forever.

Measured on prod 2026-09-04 with no census and no loops running: the host
process sat at 57% CPU, the log held 92,928 cumulative SSH opens and was adding
~64 a minute. obs.node_temps had 28 registered hosts:

    21  OSError: [Errno 113] No route to host
     4  PermissionDenied for user root
     3  reachable

_temp_probe_tick fans out to all of them every TEMP_PROBE_SEC and keeps no
memory of the last attempt. The module already backed the tool-INSTALL step off
six hours - the connection never got the same treatment, and the connection is
the expensive part.

Pure: no I/O, no network, no clock.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.workers import probe_backoff as B      # noqa: E402

pytestmark = pytest.mark.critical


# ── classifying what prod actually returned ────────────────────────────────

@pytest.mark.parametrize("err", [
    "OSError: [Errno 113] No route to host",
    "ConnectionRefusedError: [Errno 111] Connection refused",
    "timed out",
    "OSError: [Errno 101] Network is unreachable",
    "[Errno -2] Name or service not known",
])
def test_a_host_that_cannot_be_reached_is_unreachable(err):
    assert B.classify(err) == B.UNREACHABLE


@pytest.mark.parametrize("err", [
    "PermissionDenied: Permission denied for user root on host 192.168.0.250",
    "Auth failed for user root",
    "Authentication failed",
])
def test_wrong_credentials_are_refused(err):
    assert B.classify(err) == B.REFUSED


@pytest.mark.parametrize("err", ["", None, "   ", "no sensors available"])
def test_anything_unrecognised_is_transient(err):
    """An error we do not understand is not evidence a host is dead. Parking a
    healthy host would be worse than the storm."""
    assert B.classify(err) == B.TRANSIENT


def test_a_transient_failure_never_parks_a_host():
    st = B.record_failure({}, 1000.0, "no sensors available (smartmontools not installed)")
    assert B.should_skip(st, 1000.0) is False
    assert st == {}


# ── the backoff itself ─────────────────────────────────────────────────────

def test_the_first_unreachable_failure_stops_the_next_probes():
    st = B.record_failure({}, 1000.0, "no route to host")
    assert B.should_skip(st, 1000.0) is True
    assert B.should_skip(st, 1000.0 + B.BASE_SECONDS[B.UNREACHABLE] - 1) is True
    assert B.should_skip(st, 1000.0 + B.BASE_SECONDS[B.UNREACHABLE] + 1) is False


def test_repeated_failures_back_off_exponentially():
    st, now = {}, 0.0
    delays = []
    for _ in range(5):
        st = B.record_failure(st, now, "no route to host")
        delays.append(st["delay"])
        now = st["until"]
    assert delays == sorted(delays), "backoff must not shrink"
    assert delays[1] == delays[0] * 2


def test_unreachable_caps_at_an_hour_so_a_returning_host_is_noticed():
    st, now = {}, 0.0
    for _ in range(20):
        st = B.record_failure(st, now, "no route to host"); now = st["until"]
    assert st["delay"] == B.MAX_SECONDS[B.UNREACHABLE] == 3600.0


def test_refused_waits_longer_because_only_a_human_can_fix_it():
    """Wrong credentials cannot self-heal, so patience is cheap."""
    st = B.record_failure({}, 0.0, "PermissionDenied for user root")
    assert st["delay"] == B.BASE_SECONDS[B.REFUSED]
    assert B.BASE_SECONDS[B.REFUSED] > B.BASE_SECONDS[B.UNREACHABLE]
    for _ in range(20):
        st = B.record_failure(st, st["until"], "PermissionDenied for user root")
    assert st["delay"] == B.MAX_SECONDS[B.REFUSED] == 6 * 3600.0


def test_a_host_that_answers_is_forgiven_completely():
    """A machine down for a day and now up must not stay suspect - the backoff
    is a rate limit, not a punishment."""
    st = {}
    for _ in range(6):
        st = B.record_failure(st, st.get("until", 0.0), "no route to host")
    assert st["fails"] == 6
    st = B.record_success(st)
    assert st == {}
    assert B.should_skip(st, 10 ** 9) is False


def test_a_changed_failure_class_restarts_the_count():
    """'refused' after 'no route' means the host is answering its port now -
    new information, not more of the same."""
    st = B.record_failure({}, 0.0, "no route to host")
    st = B.record_failure(st, st["until"], "no route to host")
    assert st["fails"] == 2
    st = B.record_failure(st, st["until"], "PermissionDenied")
    assert st["fails"] == 1 and st["kind"] == B.REFUSED


def test_no_state_means_probe_normally():
    assert B.should_skip({}, 0.0) is False
    assert B.should_skip(None, 0.0) is False


def test_a_corrupt_state_does_not_park_a_host_forever():
    assert B.should_skip({"until": "soon"}, 0.0) is False


# ── what the estate view shows ─────────────────────────────────────────────

def test_a_skipped_host_says_so_rather_than_looking_empty():
    """A skipped host must not be indistinguishable from one that was probed
    and returned nothing, or the estate view quietly becomes a lie."""
    st = B.record_failure({}, 0.0, "OSError: [Errno 113] No route to host")
    d = B.describe(st, 0.0)
    assert "not probed" in d and "unreachable" in d
    assert "retrying in" in d
    assert "No route to host" in d


def test_describe_is_empty_while_a_host_is_being_probed_normally():
    assert B.describe({}, 0.0) == ""
    assert B.describe(None, 0.0) == ""


# ── the reduction, on the real estate shape ────────────────────────────────

def test_the_measured_estate_stops_being_dialled_every_minute():
    """21 unreachable + 4 refused + 3 healthy, TEMP_PROBE_SEC=60, one day."""
    TICK, DAY = 60.0, 86400.0
    hosts = ([("no route to host",)] * 21 + [("PermissionDenied",)] * 4
             + [(None,)] * 3)
    dials = 0
    state = [{} for _ in hosts]
    now = 0.0
    while now < DAY:
        for i, (err,) in enumerate(hosts):
            if B.should_skip(state[i], now):
                continue
            dials += 1
            state[i] = (B.record_success(state[i]) if err is None
                        else B.record_failure(state[i], now, err))
        now += TICK
    before = len(hosts) * (DAY / TICK)          # 28 * 1440 = 40,320
    assert before == 40320
    # the 3 healthy hosts must still be probed every single tick
    assert dials >= 3 * (DAY / TICK)
    # ...and the 25 dead ones must have collapsed to their cap rate:
    # 21 unreachable at the 1h cap = ~504/day, 4 refused at the 6h cap = ~16/day,
    # plus the warm-up before each reached its ceiling. ~595 against 36,000.
    dead_dials = dials - 3 * (DAY / TICK)
    assert dead_dials < 700, "dead hosts still dialled %d times/day" % dead_dials
    ceiling = (21 * DAY / B.MAX_SECONDS[B.UNREACHABLE]
               + 4 * DAY / B.MAX_SECONDS[B.REFUSED])
    assert dead_dials >= ceiling, "cannot beat the cap rate without going blind"
    # NOT a ratio of the total: the total is dominated by the three healthy
    # hosts, which SHOULD still be dialled every tick. The fix targets the
    # dead ones, so that is what is measured - 36,000/day down to ~595.
    dead_before = 25 * (DAY / TICK)
    assert dead_dials < dead_before * 0.02, \
        "dead-host dials only fell from %d to %d" % (dead_before, dead_dials)


# ── the wiring: the probe must actually consult the backoff ────────────────
#
# The pure module passing proves nothing if _probe_host_temp never asks it.
# Driven rather than grepped, because a source check cannot tell a live call
# from a dead one.

def test_the_temp_probe_skips_a_host_it_just_failed_to_reach():
    import asyncio
    nc = pytest.importorskip("vera.workers.nodes_capabilities")

    calls = {"n": 0}

    async def fake_ssh(host_id, command, timeout=60):
        calls["n"] += 1
        return {"ok": False, "error": "OSError: [Errno 113] No route to host",
                "rc": -1, "stdout": "", "stderr": ""}

    real_ssh = nc._ssh
    nc._ssh = fake_ssh
    nc._TEMP_BACKOFF.pop("h1", None)
    nc._TEMP_CACHE.pop("h1", None)
    try:
        host = {"id": "h1", "label": "dead-host"}
        asyncio.run(nc._probe_host_temp(host))
        assert calls["n"] == 1, "first probe should happen"
        # every subsequent tick inside the backoff window must NOT dial
        for _ in range(5):
            asyncio.run(nc._probe_host_temp(host))
        assert calls["n"] == 1, "host was dialled %d times despite backoff" % calls["n"]
        # and the estate view must say why, not look like an empty reading
        assert "not probed" in (nc._TEMP_CACHE["h1"].get("error") or "")
    finally:
        nc._ssh = real_ssh
        nc._TEMP_BACKOFF.pop("h1", None)
        nc._TEMP_CACHE.pop("h1", None)


def test_a_healthy_host_is_still_probed_every_tick():
    import asyncio
    nc = pytest.importorskip("vera.workers.nodes_capabilities")

    calls = {"n": 0}

    async def fake_ssh(host_id, command, timeout=60):
        calls["n"] += 1
        return {"ok": True, "error": "", "rc": 0,
                "stdout": "THERMAL_ZONE|x_thermal|41000\n", "stderr": ""}

    real_ssh = nc._ssh
    nc._ssh = fake_ssh
    nc._TEMP_BACKOFF.pop("h2", None)
    try:
        host = {"id": "h2", "label": "good-host"}
        for _ in range(4):
            asyncio.run(nc._probe_host_temp(host))
        assert calls["n"] == 4, "a healthy host must not be backed off"
        assert not nc._TEMP_BACKOFF.get("h2")
    finally:
        nc._ssh = real_ssh
        nc._TEMP_BACKOFF.pop("h2", None)
        nc._TEMP_CACHE.pop("h2", None)
