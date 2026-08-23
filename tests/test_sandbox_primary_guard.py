import asyncio

import pytest

from vera.evolve import evolve_capabilities as evolve


pytestmark = pytest.mark.critical


def test_sandbox_up_refuses_before_worktree_or_docker_mutation(monkeypatch):
    calls = []

    async def current():
        return {"branch": "feat/owned", "worktree": "/wt/owned"}

    async def status():
        return "running"

    async def audit(*args, **kwargs):
        calls.append(("audit", args, kwargs))

    async def emit(event):
        calls.append(("event", event))

    async def must_not_create(*args, **kwargs):
        raise AssertionError("guard ran after worktree mutation boundary")

    monkeypatch.setattr(evolve, "_get_sandbox", current)
    monkeypatch.setattr(evolve, "_sandbox_container_status", status)
    monkeypatch.setattr(evolve, "_audit", audit)
    monkeypatch.setattr(evolve, "emit_event", emit)
    monkeypatch.setattr(evolve, "_ensure_worktree", must_not_create)

    result = asyncio.run(evolve.evolve_sandbox_up.__wrapped__(
        branch="feat/incoming", snapshot=False))

    assert result["code"] == "primary_occupied"
    assert result["current_branch"] == "feat/owned"
    assert result["requested_branch"] == "feat/incoming"
    assert [call[0] for call in calls] == ["audit", "event"]


def test_spawned_restart_uses_exact_container_without_primary_up(monkeypatch):
    commands = []
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "port": 8995, "redis_db": 4, "worktree": "/wt/spawned"}

    async def primary():
        return {"branch": "feat/primary", "port": 8998}

    async def pool():
        return {"feat-spawned": spawned}

    async def shell(argv, timeout=0):
        commands.append(argv)
        if argv[:2] == ["docker", "restart"]:
            return {"ok": True, "out": argv[2], "err": ""}
        return {"ok": True, "out": "200", "err": ""}

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(evolve, "_get_sandbox", primary)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sh", shell)
    monkeypatch.setattr(evolve, "_audit", quiet)
    monkeypatch.setattr(evolve, "emit_event", quiet)
    monkeypatch.setattr(evolve.asyncio, "sleep", quiet)

    result = asyncio.run(evolve.evolve_sandbox_restart.__wrapped__(
        branch="feat/spawned"))

    assert commands[0] == ["docker", "restart", "vera-dev-feat-spawned"]
    assert all("compose" not in command for command in commands)
    assert result["role"] == "spawned"
    assert result["branch"] == "feat/spawned"
    assert result["port"] == 8995
    assert result["redis_db"] == 4
    assert result["worktree"] == "/wt/spawned"
    assert result["reachable"] is True
