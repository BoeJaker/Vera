"""The idle-archive tick archives only containers that exist (prod, 2026-10-05).

A row that already holds a committed image is kept by cap_sbx_stop (it is the restore handle) and still names its
long-gone container; without the check every tick, and the first tick after every restart, "archived" the same
1,857 rows again - a snapshot cp and an rm against missing containers each, ~7 min of serial docker/SSH calls.
"""
import asyncio
import importlib
import time

import pytest


def _mod():
    try:
        return importlib.import_module("Vera.vera.remote.session_sandbox_capabilities")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")


def _run(M, monkeypatch, recs, states_by_host, listed_ok=True):
    stopped = []
    old = time.time() - 30 * 86400

    async def cfg():
        return {"idle_archive_days": 7}

    async def all_recs():
        return [dict(r, last_used=r.get("last_used", old)) for r in recs]

    async def docker_host(dk, host_id):
        return {"id": host_id} if host_id in states_by_host else None

    async def state_map_checked(dk, host, host_key, prefix="vera-sbx-"):
        return dict(states_by_host.get(host_key) or {}), listed_ok

    async def stop(session_id="", remove=True, **kw):
        stopped.append(session_id)
        return {"ok": True, "committed_image": ""}

    monkeypatch.setattr(M, "_get_cfg", cfg)
    monkeypatch.setattr(M, "_redis", lambda: object())
    monkeypatch.setattr(M, "_dk", lambda: object())
    monkeypatch.setattr(M, "_all_recs", all_recs)
    monkeypatch.setattr(M, "_docker_host", docker_host)
    monkeypatch.setattr(M, "_state_map_checked", state_map_checked)
    monkeypatch.setattr(M, "cap_sbx_stop", stop)
    asyncio.run(M._idle_archive_tick())
    return stopped


def test_only_a_container_that_exists_is_archived(monkeypatch):
    M = _mod()
    recs = [
        {"session_id": "live", "container": "vera-sbx-live", "docker_host_id": "local"},
        {"session_id": "gone", "container": "vera-sbx-gone", "docker_host_id": "local", "committed_image": "img:1"},
        {"session_id": "fresh", "container": "vera-sbx-fresh", "docker_host_id": "local", "last_used": time.time()},
        {"session_id": "far", "container": "vera-sbx-far", "docker_host_id": "unlisted-host"},
    ]
    stopped = _run(M, monkeypatch, recs, {"local": {"vera-sbx-live": "exited", "vera-sbx-fresh": "running"}})
    assert stopped == ["live"]


def test_a_host_that_cannot_be_listed_archives_nothing(monkeypatch):
    M = _mod()
    recs = [{"session_id": "live", "container": "vera-sbx-live", "docker_host_id": "local"}]
    assert _run(M, monkeypatch, recs, {"local": {"vera-sbx-live": "exited"}}, listed_ok=False) == []
