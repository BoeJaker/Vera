"""One place for a run's files (5 Oct 2026, user: "if a sandbox is active it should be the
only place files can go or be looked for - otherwise vera could clobber its own files").

Before: code.author/code.edit wrote the code store, then mirrored to the sandbox (a failed
mirror was ignored); shell/python wrote the sandbox only; and _v5_load_current read the
STORE first. A run that fixed timer.html with `sed` then had its editor, critic, reviewers
and answer check read the store's older copy, and code.edit wrote that copy back over the
fix. Now the workspace (sandbox) is the content; the store is the version history."""
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
EXSRC = (ROOT / "vera" / "execution" / "exec_capabilities.py").read_text(encoding="utf-8")

try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None
try:
    from vera.execution import exec_capabilities as EX
except Exception:                                    # pragma: no cover
    EX = None
needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")
needs_ex = pytest.mark.skipif(EX is None, reason="exec module not importable here")

pytestmark = pytest.mark.critical


class FakeExec:
    """Stands in for exec_capabilities: a workspace dict + a has-sandbox flag."""
    def __init__(self, files, sandboxed=True):
        self.files, self.sandboxed, self.reads = dict(files), sandboxed, []

    async def read_artifact_file(self, session_id="", relpath="", max_bytes=60000):
        self.reads.append(relpath)
        return self.files.get(relpath)

    async def session_has_sandbox(self, session_id=""):
        return self.sandboxed


class FakeStore:
    def __init__(self, latest=None):
        self.latest, self.saves, self.gets = latest, [], 0

    def get(self, scope, path, version):
        self.gets += 1
        return dict(self.latest) if self.latest else None

    def save(self, scope, path, content, *, lang, message, session_id):
        self.saves.append((path, content, message))
        self.latest = {"version": (self.latest or {}).get("version", 0) + 1, "content": content,
                       "lang": lang}
        return {"version": self.latest["version"], "unchanged": False, "sha": "x"}


def _wire(monkeypatch, fx, st):
    monkeypatch.setattr(M, "_v5_exec_mod", lambda: fx)
    monkeypatch.setattr(M, "_code_get_sync", st.get)
    monkeypatch.setattr(M, "_code_save_sync", st.save)


@needs_app
def test_the_workspace_wins_and_an_outside_change_is_recorded(monkeypatch):
    fx = FakeExec({"timer.html": "let t = 90;"})
    st = FakeStore({"version": 5, "content": "let t = 60;", "lang": "html"})
    _wire(monkeypatch, fx, st)
    out = asyncio.run(M._v5_load_current("/workspace/timer.html", session_id="s1"))
    assert out == "let t = 90;"
    assert fx.reads == ["timer.html"]                       # one normalised path
    assert st.saves and st.saves[0][1] == "let t = 90;" and "outside the code tools" in st.saves[0][2]


@needs_app
def test_untracked_files_are_not_pulled_into_the_store(monkeypatch):
    fx = FakeExec({"notes.txt": "hello"})
    st = FakeStore(None)
    _wire(monkeypatch, fx, st)
    assert asyncio.run(M._v5_load_current("notes.txt", session_id="s1")) == "hello"
    assert st.saves == []


@needs_app
def test_a_sandboxed_session_never_reads_the_store_for_content(monkeypatch):
    fx = FakeExec({}, sandboxed=True)
    st = FakeStore({"version": 2, "content": "stale", "lang": "py"})
    _wire(monkeypatch, fx, st)
    assert asyncio.run(M._v5_load_current("gone.py", session_id="s1")) == ""


@needs_app
def test_without_a_sandbox_the_store_is_the_fallback(monkeypatch):
    fx = FakeExec({}, sandboxed=False)
    st = FakeStore({"version": 2, "content": "from the store", "lang": "py"})
    _wire(monkeypatch, fx, st)
    assert asyncio.run(M._v5_load_current("a.py", repo="r1")) == "from the store"


@needs_app
def test_save_writes_the_workspace_first_and_records_nothing_when_that_fails(monkeypatch):
    st = FakeStore(None)
    monkeypatch.setattr(M, "_code_save_sync", st.save)

    async def no_mirror(session_id, path, content):
        return ""

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(M, "_code_mirror_to_fs", no_mirror)
    monkeypatch.setattr(M, "emit_event", nothing)
    monkeypatch.setattr(M, "_code_fabric_record", nothing)
    out = asyncio.run(M.code_store_save("a.py", "x = 1\n", session_id="s1"))
    assert out["ok"] is False and "nothing saved" in out["error"] and st.saves == []

    async def mirror_ok(session_id, path, content):
        return "/workspace/" + path
    monkeypatch.setattr(M, "_code_mirror_to_fs", mirror_ok)
    out = asyncio.run(M.code_store_save("a.py", "x = 1\n", session_id="s1"))
    assert out["ok"] is True and out["fs_path"] == "/workspace/a.py" and len(st.saves) == 1


def test_code_read_latest_comes_from_the_workspace():
    body = SRC[SRC.index("async def cap_code_read("):]
    body = body[:body.index("\n@capability(")]
    assert "if not int(version or 0):" in body and "_v5_load_current(path" in body
    assert '"source": "workspace"' in body


def test_no_host_fallback_for_a_sandboxed_session_in_the_source():
    rd = EXSRC[EXSRC.index("async def read_artifact_file("):]
    rd = rd[:rd.index("\nasync def ")]
    assert "if await session_has_sandbox(session_id):" in rd
    wr = EXSRC[EXSRC.index("async def write_artifact_file("):]
    wr = wr[:wr.index("\nasync def ")]
    assert "raise ArtifactSplitError(" in wr
    assert wr.index("raise ArtifactSplitError(") < wr.index('open(target, "w"')


class FakeSB:
    def __init__(self, read=None, status=None, route_dir=None):
        self._read, self._status, self._dir = read, status, route_dir
        self.writes = []

    async def route_fs_read(self, session_id, path, max_bytes=0):
        return self._read

    async def cap_sbx_status(self, session_id=""):
        return self._status

    async def route_artifact_dir(self, session_id, create=True):
        return self._dir

    async def route_fs_write(self, session_id, path, content):
        self.writes.append(path)
        return {"error": "write failed"}


@needs_ex
def test_read_never_falls_back_to_the_host_when_sandboxed(monkeypatch, tmp_path):
    (tmp_path / "f.txt").write_text("HOST COPY")
    monkeypatch.setattr(EX, "artifact_dir", lambda **k: str(tmp_path))
    monkeypatch.setattr(EX, "_sandbox_mod",
                        lambda: FakeSB(read={"error": "not found"}, status={"exists": True, "active": True}))
    assert asyncio.run(EX.read_artifact_file(session_id="s1", relpath="f.txt")) is None
    monkeypatch.setattr(EX, "_sandbox_mod",
                        lambda: FakeSB(read=None, status={"exists": True, "active": True}))
    assert asyncio.run(EX.read_artifact_file(session_id="s1", relpath="f.txt")) is None
    monkeypatch.setattr(EX, "_sandbox_mod", lambda: FakeSB(read=None, status={"exists": False}))
    assert asyncio.run(EX.read_artifact_file(session_id="s1", relpath="f.txt")) == "HOST COPY"
    monkeypatch.setattr(EX, "_sandbox_mod",
                        lambda: FakeSB(read=None, status={"exists": True, "active": False}))
    assert asyncio.run(EX.read_artifact_file(session_id="s1", relpath="f.txt")) == "HOST COPY"


@needs_ex
def test_write_refuses_the_host_when_the_sandbox_is_unreachable(monkeypatch, tmp_path):
    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(EX.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(EX, "artifact_dir", lambda **k: str(tmp_path))
    monkeypatch.setattr(EX, "_sandbox_mod",
                        lambda: FakeSB(status={"exists": True, "active": True}, route_dir=None))
    with pytest.raises(EX.ArtifactSplitError):
        asyncio.run(EX.write_artifact_file(relpath="out.md", content="x", session_id="s1"))
    assert not (tmp_path / "out.md").exists()
    assert issubclass(EX.ArtifactSplitError, ValueError)
