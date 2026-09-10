"""A sandbox's redis sidecar follows its app - frozen with it, woken with it,
never on its own.

Found 2026-09-10: the idle reaper lists every `vera-dev*` container and
pauses any running one it does not exempt; the sidecars carry the prefix,
have no activity record (idle from their StartedAt) and are never pinned,
so the PINNED standing mirror kept running while its Redis was frozen -
"Redis reconnect failed: Timeout connecting to server" every 5 s, every
store-backed view empty. The fix: the reaper skips sidecars, pausing an
app pauses its sidecar, waking an app wakes its sidecar first, and a
refreshed standing container restarts with its store awake.

The pure naming is unit-tested; the docker paths are pinned at source
level (they shell out).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import sandbox_redis as sr  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "vera")


def _src():
    with open(os.path.join(ROOT, "evolve", "evolve_capabilities.py"), encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    start = src.index("async def " + name + "(")
    return src[start:src.index("\n\n\n", start)]


def test_a_sidecar_is_known_by_its_name():
    assert sr.is_sidecar(sr.sidecar_name("vera-dev-loop-lab-bleeding-edge-mirror"))
    assert sr.is_sidecar("vera-dev-feat-x-redis") and not sr.is_sidecar("vera-dev-feat-x")
    assert not sr.is_sidecar("") and not sr.is_sidecar(None)
    assert sr.app_of("vera-dev-feat-x-redis") == "vera-dev-feat-x"
    assert sr.app_of("vera-dev-feat-x") == "" and sr.app_of("") == ""
    assert sr.app_of(sr.sidecar_name("vera-dev")) == "vera-dev", "the pair is the same both ways"


def test_the_reaper_never_picks_a_sidecar_and_pauses_it_with_its_app():
    reap = _fn(_src(), "_sandbox_reap")
    assert "if _sbx_redis is not None and _sbx_redis.is_sidecar(name):\n            continue" in reap
    assert 'p["sidecar_paused"] = await _sidecar_set_paused(p["name"], True)' in reap
    assert reap.index('"docker", "pause", p["name"]') < reap.index("_sidecar_set_paused(p[\"name\"], True)"), \
        "the app is frozen first, then its store"


def test_waking_a_container_wakes_its_sidecar_first():
    src = _src()
    wake = _fn(src, "_sandbox_unpause_if_paused")
    assert "await _sidecar_set_paused(name, False)" in wake
    assert wake.index("_sidecar_set_paused(name, False)") < wake.index('"docker", "unpause", name'), \
        "the store is awake before the app runs"
    primary = _fn(src, "_sandbox_ensure_unpaused")
    assert "await _sidecar_set_paused(_SANDBOX_CONTAINER, False)" in primary


def test_a_refreshed_standing_container_restarts_with_its_store_awake():
    refresh = _fn(_src(), "_refresh_standing_bleeding_edge_container")
    assert "await _sidecar_set_paused(name, False)" in refresh
    assert refresh.index("_sidecar_set_paused(name, False)") < refresh.index('"docker", "restart", name')


def test_the_sidecar_helper_only_moves_between_the_two_states():
    helper = _fn(_src(), "_sidecar_set_paused")
    assert 'if paused and cur == "running":' in helper and 'if not paused and cur == "paused":' in helper
    assert "sc = _sbx_redis.sidecar_name(name)" in helper
