"""A Docker host whose SSH record is gone (2026-09-28): 192.168.0.250-(vera-worker)
pointed at a deleted SSH id from July, and every poll ran exec.ssh.run to fail as a
bare "HTTP 502". It must fail fast, name the problem, and never reach SSH."""

import asyncio
import json

import pytest

import Vera.vera.workers.docker_capabilities as docker

pytestmark = pytest.mark.critical

SSH_REC = {"id": "h-ssh", "kind": "ssh", "ssh_host_id": "gone-id",
           "socket": "/var/run/docker.sock"}


class FakeExec:
    def __init__(self, known):
        self.known = known
        self.ran = []

    async def _resolve_host_record(self, host_id):
        return {"id": host_id} if host_id in self.known else None

    async def cap_ssh_run(self, **kw):
        self.ran.append(kw)
        return {"ok": True, "stdout": "[]"}


def test_a_dangling_ssh_record_fails_fast_without_ssh(monkeypatch):
    fx = FakeExec(known=set())
    monkeypatch.setattr(docker, "_exec_mod", lambda: fx)
    status, body, _ = asyncio.run(docker._engine_request(SSH_REC, "GET", "/containers/json"))
    assert status == 424
    msg = json.loads(body)["message"]
    assert "gone-id" in msg and "no longer exists" in msg
    assert fx.ran == []


def test_a_live_ssh_record_still_runs(monkeypatch):
    fx = FakeExec(known={"gone-id"})
    monkeypatch.setattr(docker, "_exec_mod", lambda: fx)
    status, body, _ = asyncio.run(docker._engine_request(SSH_REC, "GET", "/containers/json"))
    assert status == 200 and len(fx.ran) == 1


def test_errors_carry_the_engine_message_not_a_bare_status():
    body = json.dumps({"message": "host_id not found: x"}).encode()
    assert docker._engine_error(502, body) == "HTTP 502: host_id not found: x"
    assert docker._engine_error(500, b"") == "HTTP 500"
    assert docker._engine_error(503, b"plain text failure") == "HTTP 503: plain text failure"
    src = open(docker.__file__, encoding="utf-8").read()
    assert 'f"HTTP {status}"' not in src


def test_hosts_list_marks_a_broken_host(monkeypatch):
    fx = FakeExec(known=set())
    monkeypatch.setattr(docker, "_exec_mod", lambda: fx)
    monkeypatch.setattr(docker, "_load_hosts", lambda: {"h-ssh": dict(SSH_REC)})
    out = asyncio.run(docker.cap_docker_hosts_list())
    rows = {r["id"]: r for r in out["hosts"]}
    assert "no longer exists" in rows["h-ssh"]["broken"]
    assert "broken" not in rows.get("local", {})
