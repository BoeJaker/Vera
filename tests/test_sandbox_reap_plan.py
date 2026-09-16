"""The registry reaper must delete only rows with nothing left to point at.

Pins vera.remote.sandbox_idle_core.reap_plan. Context (2026-09-16, prod): the
KEY_SBX hash had grown to 1554 records because nothing ever removed one, and
four separate paths json.loads'd every record on the event loop (the UI poll
plus ticks at 60s, 120s and hourly), costing 418-840ms a scan and showing up in
the stall dumper at 1561ms and 2124ms.

The dangerous half of the fix is the delete rule, so these tests pin what must
NOT be reaped. Of those 1554 rows, 1366 named a container that no longer
existed — but 1273 of those carried a committed_image and were restorable
archives backed by real docker images, not garbage. Only 37 rows were actually
empty. A reaper that went by "container absent + stale" alone would have
destroyed 1273 restore points and orphaned their images.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.remote.sandbox_idle_core import reap_plan  # noqa: E402

NOW = 1_000_000.0
IDLE = 7 * 86400.0          # the archive threshold
STALE = NOW - IDLE - 1      # comfortably past it
FRESH = NOW - 60

# One host that answered and holds nothing, one that never answered.
ANSWERED = {"local": {}}
HOST_OK = {"local": True, "unreachable-host": False}


def _rec(sid, **kw):
    r = {"session_id": sid, "container": f"vera-sbx-{sid}",
         "docker_host_id": "local", "last_used": STALE}
    r.update(kw)
    return r


def test_reaps_a_row_with_no_container_and_no_archive():
    plan = reap_plan([_rec("gone")], now=NOW, idle_s=IDLE,
                     state_maps=ANSWERED, host_ok=HOST_OK)
    assert plan.remove == ["gone"]


def test_never_reaps_a_row_that_has_a_committed_image():
    """1273 of prod's 1366 'absent' rows were archives like this one."""
    plan = reap_plan([_rec("archived", committed_image="vera-session:archived")],
                     now=NOW, idle_s=IDLE, state_maps=ANSWERED, host_ok=HOST_OK)
    assert plan.remove == []
    assert plan.kept_archived == 1


def test_never_reaps_a_recently_used_row():
    plan = reap_plan([_rec("recent", last_used=FRESH)], now=NOW, idle_s=IDLE,
                     state_maps=ANSWERED, host_ok=HOST_OK)
    assert plan.remove == []
    assert plan.kept_fresh == 1


def test_never_reaps_a_row_with_no_last_used_stamp():
    rec = _rec("unstamped")
    rec.pop("last_used")
    plan = reap_plan([rec], now=NOW, idle_s=IDLE,
                     state_maps=ANSWERED, host_ok=HOST_OK)
    assert plan.remove == []


def test_never_reaps_a_row_whose_container_still_exists():
    plan = reap_plan([_rec("alive")], now=NOW, idle_s=IDLE,
                     state_maps={"local": {"vera-sbx-alive": "exited"}},
                     host_ok=HOST_OK)
    assert plan.remove == []
    assert plan.kept_present == 1


def test_a_silent_docker_host_never_licenses_a_delete():
    """The state query returns an EMPTY map for a host it could not reach, so
    'container not in the map' means nothing unless the host answered. There is
    a permanently unreachable host in the estate (192.168.0.250, HTTP 502);
    without this clause one outage would empty the registry."""
    recs = [_rec(f"s{i}", docker_host_id="unreachable-host") for i in range(50)]
    plan = reap_plan(recs, now=NOW, idle_s=IDLE,
                     state_maps={"unreachable-host": {}}, host_ok=HOST_OK)
    assert plan.remove == []
    assert plan.kept_unproven == 50


def test_local_backend_row_is_reaped_only_when_inactive():
    active = _rec("local-live", backend="local", active=True)
    idle = _rec("local-idle", backend="local", active=False)
    plan = reap_plan([active, idle], now=NOW, idle_s=IDLE,
                     state_maps=ANSWERED, host_ok=HOST_OK, local_backend="local")
    assert plan.remove == ["local-idle"]


def test_the_per_tick_cap_bounds_a_misconfigured_threshold():
    recs = [_rec(f"s{i}") for i in range(500)]
    plan = reap_plan(recs, now=NOW, idle_s=IDLE, state_maps=ANSWERED,
                     host_ok=HOST_OK, limit=200)
    assert len(plan.remove) == 200
    assert plan.capped is True


def test_the_prod_mix_reaps_only_the_genuinely_empty_rows():
    """Reproduces the shape of prod's registry: mostly archives, a few empties."""
    recs = ([_rec(f"arch{i}", committed_image=f"vera-session:arch{i}")
             for i in range(1273)]
            + [_rec(f"empty{i}") for i in range(37)]
            + [_rec(f"fresh{i}", last_used=FRESH) for i in range(100)])
    plan = reap_plan(recs, now=NOW, idle_s=IDLE,
                     state_maps=ANSWERED, host_ok=HOST_OK, limit=500)
    assert len(plan.remove) == 37
    assert all(s.startswith("empty") for s in plan.remove)
    assert plan.kept_archived == 1273
    assert plan.kept_fresh == 100


def test_malformed_rows_are_skipped_not_fatal():
    plan = reap_plan([None, "nonsense", {}, {"session_id": ""}, _rec("ok")],
                     now=NOW, idle_s=IDLE, state_maps=ANSWERED, host_ok=HOST_OK)
    assert plan.remove == ["ok"]
