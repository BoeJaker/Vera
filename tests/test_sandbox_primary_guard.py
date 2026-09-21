import asyncio

import pytest

from vera.evolve import evolve_capabilities as evolve


pytestmark = pytest.mark.critical


def test_sandbox_up_refuses_before_worktree_or_docker_mutation(monkeypatch):
    calls = []

    async def ownership():
        return {"branch": "feat/owned", "worktree": "/wt/owned"}, "running"

    async def audit(*args, **kwargs):
        calls.append(("audit", args, kwargs))

    async def emit(event):
        calls.append(("event", event))

    async def must_not_create(*args, **kwargs):
        raise AssertionError("guard ran after worktree mutation boundary")

    monkeypatch.setattr(evolve, "_primary_ownership", ownership)
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
        return {"branch": "feat/primary", "port": 8998}, "running"

    async def pool():
        return {"feat-spawned": spawned}

    async def shell(argv, timeout=0):
        commands.append(argv)
        if argv[:2] == ["docker", "restart"]:
            return {"ok": True, "out": argv[2], "err": ""}
        return {"ok": True, "out": "200", "err": ""}

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(evolve, "_primary_ownership", primary)
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
    assert result["dry_run"] is False
    assert result["mutated"] is True


def test_spawned_restart_dry_run_preserves_descriptor_without_shell(monkeypatch):
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "port": 8995, "redis_db": 4, "worktree": "/wt/spawned"}

    async def primary():
        return {"branch": "feat/primary", "port": 8998}, "running"

    async def pool():
        return {"feat-spawned": spawned}

    async def must_not_shell(*args, **kwargs):
        raise AssertionError("dry run invoked Docker or curl")

    monkeypatch.setattr(evolve, "_primary_ownership", primary)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sh", must_not_shell)

    result = asyncio.run(evolve.evolve_sandbox_restart.__wrapped__(
        branch="feat/spawned", dry_run=True))

    assert result == {
        "ok": True, "dry_run": True, "mutated": False, "action": "restart",
        "role": "spawned", "name": "vera-dev-feat-spawned",
        "branch": "feat/spawned", "port": 8995, "redis_db": 4,
        "worktree": "/wt/spawned", "reachable": None, "url": "",
    }


def test_primary_up_dry_run_reuses_same_branch_without_mutation(monkeypatch):
    async def ownership():
        return {"branch": "feat/owned", "worktree": "/wt/owned"}, "running"

    async def must_not_mutate(*args, **kwargs):
        raise AssertionError("dry run crossed a mutation boundary")

    monkeypatch.setattr(evolve, "_primary_ownership", ownership)
    monkeypatch.setattr(evolve, "_ensure_worktree", must_not_mutate)
    monkeypatch.setattr(evolve, "_sh", must_not_mutate)

    result = asyncio.run(evolve.evolve_sandbox_up.__wrapped__(
        branch="feat/owned", snapshot=True, dry_run=True))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["allowed"] is True
    assert result["action"] == "reuse_primary"


@pytest.mark.parametrize("target,expected", [
    ("bleeding-edge", evolve.BLEEDING_EDGE_MIRROR_BRANCH),
    ("main", evolve.MAINLINE_MIRROR_BRANCH),
])
def test_default_target_up_dry_run_does_not_refresh_mirror(monkeypatch, target, expected):
    async def ownership():
        return {}, ""

    async def must_not_refresh(*args, **kwargs):
        raise AssertionError("dry run refreshed a mirror")

    monkeypatch.setattr(evolve, "_primary_ownership", ownership)
    monkeypatch.setattr(evolve, "_refresh_bleeding_edge_mirror", must_not_refresh)
    monkeypatch.setattr(evolve, "_refresh_mainline_mirror", must_not_refresh)

    result = asyncio.run(evolve.evolve_sandbox_up.__wrapped__(
        target=target, dry_run=True))

    assert result["mutated"] is False
    assert result["requested_branch"] == expected


def test_primary_ownership_falls_back_to_shared_docker_mount(monkeypatch):
    async def empty_descriptor():
        return {}

    async def shell(argv, timeout=0):
        assert argv[:2] == ["docker", "inspect"]
        if "{{.State.Status}}" in argv:
            return {"ok": True, "out": "running\n", "err": ""}
        return {"ok": True, "out": "/home/vera/.loop-lab-worktrees/feat-owned\n",
                "err": ""}

    async def worktrees():
        return [{"path": "/home/vera/.loop-lab-worktrees/feat-owned",
                 "branch": "feat/owned", "is_main": False}]

    async def port():
        return 8998

    monkeypatch.setattr(evolve, "_get_sandbox", empty_descriptor)
    monkeypatch.setattr(evolve, "_sh", shell)
    monkeypatch.setattr(evolve, "_list_worktrees", worktrees)
    monkeypatch.setattr(evolve, "_dev_port", port)

    descriptor, status = asyncio.run(evolve._primary_ownership())

    assert status == "running"
    assert descriptor == {
        "branch": "feat/owned", "worktree": "/home/vera/.loop-lab-worktrees/feat-owned",
        "ownership_source": "docker_mount", "port": 8998,
        "redis_db": evolve.DEV_REDIS_DB,
    }


def test_primary_ownership_fails_closed_when_docker_is_unobservable(monkeypatch):
    async def empty_descriptor():
        return {}

    async def unavailable(*args, **kwargs):
        return {"ok": False, "out": "", "err": "permission denied opening docker socket"}

    monkeypatch.setattr(evolve, "_get_sandbox", empty_descriptor)
    monkeypatch.setattr(evolve, "_sh", unavailable)

    descriptor, status = asyncio.run(evolve._primary_ownership())
    conflict = evolve._primary_replacement_conflict(
        descriptor, "feat/incoming", status)

    assert status == "unknown"
    assert conflict["code"] == "primary_occupied"
    assert conflict["current_branch"] == "(unknown)"


def test_spawned_down_dry_run_preserves_worktree_by_default(tmp_path, monkeypatch):
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "compose": "docker-compose.dev-feat-spawned.yml",
               "worktree": "/wt/spawned"}

    async def pool():
        return {"feat-spawned": spawned}

    async def must_not_mutate(*args, **kwargs):
        raise AssertionError("teardown dry run crossed a mutation boundary")

    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(evolve, "_sh", must_not_mutate)
    monkeypatch.setattr(evolve, "_remove_worktree_robust", must_not_mutate)

    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(
        branch="feat/spawned", dry_run=True))

    assert result == {
        "ok": True, "dry_run": True, "mutated": False, "role": "spawned",
        "name": "vera-dev-feat-spawned", "branch": "feat/spawned",
        "worktree": "/wt/spawned",
        "compose": "docker-compose.dev-feat-spawned.yml",
        "container_action": "stop_remove",
        "container_method": "compose_project_label",
        "worktree_action": "preserve",
    }


def test_spawned_down_missing_compose_uses_exact_project_label(tmp_path, monkeypatch):
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "compose": "docker-compose.dev-feat-spawned.yml",
               "worktree": "/wt/spawned"}
    calls = []
    deleted = []

    class Redis:
        async def hdel(self, key, slug):
            deleted.append((key, slug))

    async def pool():
        return {"feat-spawned": spawned}

    async def sh(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "ps", "-aq"]:
            return {"ok": True, "out": "container-a\ncontainer-b\n", "err": ""}
        if argv[:3] == ["docker", "network", "ls"]:
            return {"ok": True, "out": "network-a\n", "err": ""}
        if argv[:3] == ["docker", "rm", "-f"]:
            return {"ok": True, "out": "removed", "err": ""}
        if argv[:3] == ["docker", "network", "rm"]:
            return {"ok": True, "out": "removed", "err": ""}
        raise AssertionError(argv)

    monkeypatch.setattr(evolve, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sh", sh)
    monkeypatch.setattr(evolve, "_redis", lambda: Redis())

    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(
        branch="feat/spawned"))

    assert result["ok"] is True
    assert calls[0] == ["docker", "ps", "-aq", "--filter",
                        "label=com.docker.compose.project=vera-dev-feat-spawned"]
    assert calls[1] == ["docker", "network", "ls", "-q", "--filter",
                        "label=com.docker.compose.project=vera-dev-feat-spawned"]
    assert calls[2] == ["docker", "rm", "-f", "container-a", "container-b"]
    assert calls[3] == ["docker", "network", "rm", "network-a"]
    assert deleted == [(evolve.KEY_SANDBOX_POOL, "feat-spawned")]


def test_spawned_down_preserves_descriptor_when_docker_is_unobservable(
        tmp_path, monkeypatch):
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "compose": "docker-compose.dev-feat-spawned.yml",
               "worktree": "/wt/spawned"}
    deleted = []

    class Redis:
        async def hdel(self, key, slug):
            deleted.append((key, slug))

    async def pool():
        return {"feat-spawned": spawned}

    async def unavailable(*args, **kwargs):
        return {"ok": False, "out": "", "err": "permission denied"}

    monkeypatch.setattr(evolve, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sh", unavailable)
    monkeypatch.setattr(evolve, "_redis", lambda: Redis())

    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(
        branch="feat/spawned"))

    assert result["ok"] is False
    assert result["code"] == "docker_unobservable"
    assert result["mutated"] is False
    assert deleted == []


def test_spawned_down_preserves_descriptor_when_compose_teardown_fails(
        tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.dev-feat-spawned.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "compose": compose.name, "worktree": "/wt/spawned"}
    deleted = []

    class Redis:
        async def hdel(self, key, slug):
            deleted.append((key, slug))

    async def pool():
        return {"feat-spawned": spawned}

    async def failed(*args, **kwargs):
        return {"ok": False, "out": "", "err": "compose failed"}

    monkeypatch.setattr(evolve, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sh", failed)
    monkeypatch.setattr(evolve, "_redis", lambda: Redis())

    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(
        branch="feat/spawned"))

    assert result["ok"] is False
    assert result["code"] == "container_teardown_failed"
    assert result["mutated"] is True
    assert compose.exists()
    assert deleted == []


def test_spawned_down_reports_unavailable_descriptor_store(tmp_path, monkeypatch):
    spawned = {"branch": "feat/spawned", "name": "vera-dev-feat-spawned",
               "compose": "docker-compose.dev-feat-spawned.yml",
               "worktree": "/wt/spawned"}

    async def pool():
        return {"feat-spawned": spawned}

    async def empty_project(argv, **kwargs):
        if argv[:3] in (["docker", "ps", "-aq"],
                       ["docker", "network", "ls"]):
            return {"ok": True, "out": "", "err": ""}
        raise AssertionError(argv)

    monkeypatch.setattr(evolve, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sh", empty_project)
    monkeypatch.setattr(evolve, "_redis", lambda: None)

    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(
        branch="feat/spawned"))

    assert result["ok"] is False
    assert result["code"] == "descriptor_store_unavailable"
    assert result["mutated"] is True


def test_primary_down_dry_run_reports_explicit_worktree_removal(monkeypatch):
    async def primary():
        return {"branch": "feat/primary", "worktree": "/wt/primary"}

    async def must_not_mutate(*args, **kwargs):
        raise AssertionError("teardown dry run crossed a mutation boundary")

    monkeypatch.setattr(evolve, "_get_sandbox", primary)
    monkeypatch.setattr(evolve, "_sh", must_not_mutate)
    monkeypatch.setattr(evolve, "_remove_worktree_robust", must_not_mutate)

    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(
        remove_worktree=True, dry_run=True))

    assert result["role"] == "primary"
    assert result["branch"] == "feat/primary"
    assert result["worktree"] == "/wt/primary"
    assert result["worktree_action"] == "remove"
    assert result["mutated"] is False


def test_primary_down_dry_run_refuses_missing_descriptor(monkeypatch):
    async def missing():
        return {}

    monkeypatch.setattr(evolve, "_get_sandbox", missing)
    result = asyncio.run(evolve.evolve_sandbox_down.__wrapped__(dry_run=True))
    assert result == {"error": "no primary sandbox descriptor",
                      "code": "sandbox_not_found", "dry_run": True,
                      "mutated": False}


def test_preflight_returns_plan_without_mutation(monkeypatch):
    async def primary():
        return {"branch": "feat/landed", "worktree": "/wt/landed",
                "port": 8998, "owner": "codex", "session_id": "session-1"}, "exited"

    async def pool():
        return {}

    async def pinned():
        return set()

    async def observation(_target):
        return {"docker_observable": True, "container_status": "",
                "worktree_exists": True, "head_commit": "abc",
                "bleeding_edge_commit": "def", "merged_to_bleeding_edge": True,
                "dirty": False, "state": "stale_descriptor",
                "git_worktree": {"valid": True, "state": "healthy",
                                 "repair_plan": [], "automatic": False}}

    monkeypatch.setattr(evolve, "_primary_ownership", primary)
    monkeypatch.setattr(evolve, "_sandbox_pool", pool)
    monkeypatch.setattr(evolve, "_sandbox_pinned", pinned)
    monkeypatch.setattr(evolve, "_sandbox_observation", observation)

    result = asyncio.run(evolve.evolve_sandbox_preflight.__wrapped__(
        action="reconcile"))

    assert result["allowed"] is True
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["sandbox"]["owner"] == "codex"
    assert result["plan"] == [{"action": "reconcile", "target": "vera-dev",
                               "branch": "feat/landed"}]


def test_sandbox_list_labels_isolated_capacity_non_authoritative(monkeypatch):
    async def shell(argv, timeout=0):
        return {"ok": True, "out": "", "err": "", "code": 0}

    async def empty_set():
        return set()

    async def empty_pool():
        return {}

    monkeypatch.setattr(evolve, "_sh", shell)
    monkeypatch.setattr(evolve, "_sandbox_pinned", empty_set)
    monkeypatch.setattr(evolve, "_sandbox_pool", empty_pool)
    monkeypatch.setattr(evolve, "_redis", lambda: None)
    monkeypatch.setenv("VERA_IS_DEV_SANDBOX", "1")

    result = asyncio.run(evolve.evolve_sandbox_list.__wrapped__())

    assert result["capacity"]["scope"] == "sandbox_local_registry"
    assert result["capacity"]["authoritative"] is False
    assert "query production" in result["capacity"]["warning"]
