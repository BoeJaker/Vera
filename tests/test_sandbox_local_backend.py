"""Local (in-container) session-sandbox backend — used when docker is absent.

This same code runs inside a Loop-Lab dev sandbox that has NO docker socket and
no docker CLI, where `docker run` is impossible. Rather than nest containers, the
dev container uses ITSELF as the session sandbox: exec runs as a local subprocess
and files live on its own filesystem under a workspace dir. These tests force the
"docker structurally absent" condition and verify the primitives the agentic loop
relies on work end-to-end: sandbox.session.start creates a local record, exec
runs locally, and fs write/read/list round-trip.

The whole path is INERT wherever docker exists (structural-absence gate), so prod
never takes it — a property these tests also pin (present-socket ⇒ not absent).

Imports the module directly; monkeypatches Redis + docker detection so nothing
real is touched.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.remote import session_sandbox_capabilities as S  # noqa: E402


class _FakeRedis:
    """Minimal async Redis hash stand-in for the sandbox record store."""
    def __init__(self):
        self.h = {}

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, val):
        self.h.setdefault(key, {})[field] = val

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def get(self, key):
        return None


def _force_local(monkeypatch, tmp_ws):
    monkeypatch.setattr(S, "_redis", lambda: _FakeRedis.instance)
    S._FakeRedis = _FakeRedis
    _FakeRedis.instance = _FakeRedis()
    # Force "docker structurally absent" and a known writable workspace.
    monkeypatch.setattr(S, "_docker_structurally_absent", lambda: True)
    S._LOCAL_WS_CACHE = tmp_ws
    S._DOCKER_ABSENT_CACHE = True


def test_structural_absence_gate_respects_real_env():
    # A present socket / DOCKER_HOST / docker binary must read as NOT absent, so
    # prod never diverts to the local backend. Probe the pure predicate with the
    # cache cleared.
    S._DOCKER_ABSENT_CACHE = None
    try:
        os.environ["DOCKER_HOST"] = "tcp://example:2375"
        assert S._docker_structurally_absent() is False
    finally:
        os.environ.pop("DOCKER_HOST", None)
        S._DOCKER_ABSENT_CACHE = None


def test_local_backend_ok_only_for_local_host(monkeypatch):
    monkeypatch.setattr(S, "_docker_structurally_absent", lambda: True)
    assert S._local_backend_ok("") is True
    assert S._local_backend_ok("local") is True
    # A session pinned to a REMOTE docker host must still use docker.
    assert S._local_backend_ok("prod-gpu-box") is False


def test_start_creates_local_record(monkeypatch, tmp_path):
    _force_local(monkeypatch, str(tmp_path))
    res = asyncio.run(S.cap_sbx_start(session_id="sid-1"))
    assert res["ok"] is True
    assert res["backend"] == S._LOCAL_BACKEND
    assert res["workdir"] == str(tmp_path)
    rec = asyncio.run(S._get_rec("sid-1"))
    assert S._is_local_rec(rec) and rec["active"] is True


def test_exec_runs_locally(monkeypatch, tmp_path):
    _force_local(monkeypatch, str(tmp_path))
    asyncio.run(S.cap_sbx_start(session_id="sid-2"))
    out = asyncio.run(S._exec_in("sid-2", "echo hello-local && pwd"))
    assert out is not None and out["ok"] is True
    assert "hello-local" in out["stdout"]
    assert str(tmp_path) in out["stdout"]        # ran in the workspace dir
    assert out.get("backend") == S._LOCAL_BACKEND


def test_fs_write_read_list_roundtrip(monkeypatch, tmp_path):
    _force_local(monkeypatch, str(tmp_path))
    asyncio.run(S.cap_sbx_start(session_id="sid-3"))
    w = asyncio.run(S.route_fs_write("sid-3", "notes.md", "# Requirements\n- timer\n"))
    assert w is not None and w.get("path") == "notes.md" and not w.get("error"), w
    # The file really landed in the workspace on disk.
    assert (tmp_path / "notes.md").read_text().startswith("# Requirements")
    r = asyncio.run(S.route_fs_read("sid-3", "notes.md"))
    assert r is not None and "# Requirements" in r["content"], r
    lst = asyncio.run(S.route_fs_list("sid-3", ""))
    assert lst is not None and not lst.get("error"), lst
    names = [e["name"] for e in lst["entries"]]
    assert "notes.md" in names


def test_artifact_dir_is_the_local_workspace(monkeypatch, tmp_path):
    _force_local(monkeypatch, str(tmp_path))
    asyncio.run(S.cap_sbx_start(session_id="sid-4"))
    d = asyncio.run(S.route_artifact_dir("sid-4"))
    assert d == str(tmp_path)


def test_run_code_executes_locally(monkeypatch, tmp_path):
    _force_local(monkeypatch, str(tmp_path))
    asyncio.run(S.cap_sbx_start(session_id="sid-5"))
    out = asyncio.run(S._run_code_in("sid-5", "python", "print(6*7)"))
    assert out is not None and out["ok"] is True, out
    assert "42" in out["stdout"]
