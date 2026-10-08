"""The loop-census harness in tools/loop-census (moved into the repo 4 Oct 2026): it parses,
it keeps code and data apart (data from VERA_CENSUS_DIR), it names no host, and its probe
helpers treat an unreadable state as unknown, not empty."""
import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
D = ROOT / "tools" / "loop-census"
PY_FILES = sorted(D.rglob("*.py"))
SH_FILES = sorted(D.rglob("*.sh"))


def test_the_harness_is_here():
    for name in ("run_census.py", "census_all.sh", "ingest_census.py", "run_operator_census.py",
                 "probes/probe_layout.py", "probes/probe_batch.sh", "probes/rev.py", "README.md"):
        assert (D / name).exists(), name


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(D)))
def test_python_parses(path):
    ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
@pytest.mark.parametrize("path", SH_FILES, ids=lambda p: str(p.relative_to(D)))
def test_shell_parses(path):
    r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_host_specific_paths_or_addresses():
    bad = re.compile(r"/home/[a-z]+|192\.168\.\d+\.\d+|langchain/bin")
    for f in PY_FILES + SH_FILES:
        text = f.read_text(encoding="utf-8")
        hits = [m.group(0) for m in bad.finditer(text)]
        # the probe runner names the default python as $HOME/langchain/bin/python3 (env-overridable)
        hits = [h for h in hits if not (f.name == "probe_batch.sh" and h == "langchain/bin")]
        assert not hits, (f.name, hits)


def test_data_comes_from_vera_census_dir_and_code_from_here():
    for name in ("run_census.py", "run_operator_census.py", "ingest_census.py", "fix_reroutes.py"):
        t = (D / name).read_text(encoding="utf-8")
        assert 'HERE = os.path.expanduser(os.environ.get("VERA_CENSUS_DIR", "").strip() or "~/loop-census")' in t, name
        assert "CODE = os.path.dirname(os.path.abspath(__file__))" in t, name
    for name in ("census_all.sh", "census_ingest_watch.sh"):
        t = (D / name).read_text(encoding="utf-8")
        assert 'HERE="${VERA_CENSUS_DIR:-$HOME/loop-census}"' in t, name
        assert not re.search(r'"\$HERE/[a-z_]+\.py"', t), name     # sibling scripts come from CODE
    assert '"$PY" "$CODE/run_census.py"' in (D / "census_all.sh").read_text(encoding="utf-8")
    assert "sys.path.insert(0, CODE)" in (D / "fix_reroutes.py").read_text(encoding="utf-8")


def test_run_census_lists_templates_from_the_data_dir(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "probe-tpl.json").write_text(
        '{"name":"probe-tpl","description":"x","goals":[{"id":"g1","goal":"say hi"}]}', encoding="utf-8")
    env = dict(os.environ, VERA_CENSUS_DIR=str(tmp_path), CENSUS_BASE="https://127.0.0.1:1")
    r = subprocess.run([sys.executable, str(D / "run_census.py"), "--list"], capture_output=True,
                       text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    assert "probe-tpl" in r.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == ["templates"]     # --list writes nothing


def test_the_repo_root_is_found_from_the_script_not_the_host():
    t = (D / "run_census.py").read_text(encoding="utf-8")
    assert 'sys.path.insert(0, os.path.abspath(os.path.join(CODE, "..", "..")))' in t
    assert (D / ".." / ".." / "vera" / "census" / "quality.py").resolve().exists()


def test_probes_resolve_their_own_folder():
    for name in ("probe_goal.py", "probe_layout.py"):
        t = (D / "probes" / name).read_text(encoding="utf-8")
        assert 'P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")' in t, name
    b = (D / "probes" / "probe_batch.sh").read_text(encoding="utf-8")
    assert 'T="$(cd "$(dirname "$0")" && pwd)"' in b and 'PY="${CENSUS_PY:-' in b


def test_rev_never_crashes_finding_redis_and_honours_the_override(monkeypatch):
    sys.path.insert(0, str(D / "probes"))
    try:
        import rev
    finally:
        sys.path.pop(0)
    monkeypatch.setenv("CENSUS_REDIS_URL", "redis://example:6379/3")
    assert rev._redis_url() == "redis://example:6379/3"
    monkeypatch.delenv("CENSUS_REDIS_URL")
    monkeypatch.setattr(rev.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("pgrep")))
    assert rev._redis_url() == "redis://localhost:6379/0"


def test_probe_running_state_unknown_is_busy():
    t = (D / "probes" / "probe.py").read_text(encoding="utf-8")
    assert "return None" in t[t.index("def running():"):t.index("def box_free():")]
    assert "running() == []" in t[t.index("def box_free():"):]


class _FakeR:
    def __init__(self, h):
        self.h = h

    def hget(self, key, field):
        v = self.h.get(key, {}).get(field)
        return v.encode() if isinstance(v, str) else v


def test_rev_status_terminal_history_wins_and_unknown_is_not_finished():
    sys.path.insert(0, str(D / "probes"))
    import rev
    sid = "s1"
    done_then_cancel = _FakeR({"vera:loop:history:run:s1": {"status": "done"},
                               "vera:loop:run:s1": {"status": "cancelled"}})
    assert rev.status(sid, done_then_cancel) == "done" and rev.finished(sid, done_then_cancel)
    live = _FakeR({"vera:loop:history:run:s1": {"status": "running"},
                   "vera:loop:run:s1": {"status": "running"}})
    assert rev.status(sid, live) == "running" and not rev.finished(sid, live)
    killed = _FakeR({"vera:loop:history:run:s1": {"status": "running"},
                     "vera:loop:run:s1": {"status": "cancelled"}})
    assert rev.finished(sid, killed)

    class Broken:
        def hget(self, *a):
            raise ConnectionError("down")
    assert rev.status(sid, Broken()) is None and not rev.finished(sid, Broken())


def test_probes_never_cancel_a_finished_run():
    p = (D / "probes" / "probe.py").read_text(encoding="utf-8")
    stop = p[p.index("def stop(sid, proc):"):p.index("def start(")]
    assert stop.index("_rev.finished(sid)") < stop.index("/workshop/agent_loop/cancel")
    lay = (D / "probes" / "probe_layout.py").read_text(encoding="utf-8")
    assert "capped = not _rev.finished(sid)" in lay
    assert 'if not _rev.finished(sid) and sid in (running() or [sid]):\n    http("POST", "/workshop/agent_loop/cancel"' in lay
    assert "tasks?match=agent_loop" not in lay and "/sys/debug/tasks?limit=200" in lay


def test_probes_score_with_the_census_evaluator():
    for name in ("probe_layout.py", "probe_goal.py"):
        t = (D / "probes" / name).read_text(encoding="utf-8")
        assert '"vera", "census", "quality.py"' in t and "_quality.evaluate(" in t, name
        assert 'elif "any_of" in c:' not in t, name          # no hand copy left
    q = (D / "probes" / ".." / ".." / ".." / "vera" / "census" / "quality.py").resolve()
    assert q.is_file()
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("census_quality_t", q)
    m = ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    checks = [{"file": "stats.py", "exists": True},
              {"file": "stats.py", "all_of": ["def mean", "def median", "def mode"]}]
    r = m.evaluate(checks, {"statkit/stats.py": "def mean(x):\ndef median(x):\ndef mode(x):\n"}, "")
    assert r["score"] == 1.0


def _restart_check():
    """restart_check() from run_census.py, without importing the harness (it runs on import)."""
    import ast as _ast
    src = (D / "run_census.py").read_text(encoding="utf-8")
    fn = next(n for n in _ast.parse(src).body
              if isinstance(n, _ast.FunctionDef) and n.name == "restart_check")
    ns = {"RESTART_GRACE_S": 60}
    exec(compile(_ast.Module(body=[fn], type_ignores=[]), "run_census.py", "exec"), ns)
    return ns["restart_check"]


def test_a_restart_under_a_goal_is_seen_and_a_silent_loop_called_dead_after_the_grace():
    rc = _restart_check()
    start = {"process_started_at": "2026-10-05T11:00:00Z"}
    same = lambda: {"process_started_at": "2026-10-05T11:00:00Z"}
    new = lambda: {"process_started_at": "2026-10-05T11:04:38Z"}
    down = lambda: {}
    assert rc(start, same, None, None, 10, 100.0) == {}
    assert rc(start, down, None, None, 10, 100.0) == {}          # prod restarting: no verdict
    seen = rc(start, new, None, None, 10, 100.0)
    assert seen["seen"] == 100.0 and seen["process"].endswith("04:38Z")
    assert rc(start, new, 100.0, 10, 10, 130.0) == {}            # inside the grace
    assert rc(start, new, 100.0, 10, 10, 161.0) == {"dead": True}
    assert rc(start, new, 100.0, 10, 14, 161.0) == {}            # events after it: alive
    assert rc(start, new, 100.0, -1, 10, 161.0) == {}            # unreadable: unknown
    assert rc({}, new, None, None, 10, 100.0) == {}              # no start record: no claim


def test_the_goal_loop_acts_on_a_dead_restart():
    src = (D / "run_census.py").read_text(encoding="utf-8")
    loop = src[src.index("def run_goal("):]
    loop = loop[:loop.index('if status == "interrupted" and not cancelled:')]
    assert "restart_check(code_start, code_snapshot" in loop
    dead = loop[loop.index('if _restart.get("dead"):'):]
    assert dead.index("cancel_loop(sid)") < dead.index('status = "interrupted"') < dead.index("break")


def test_probe_results_never_live_in_tmp():
    """8 Oct 2026: a host reboot wiped /tmp and two nights of probe results with it."""
    for name in ("probe.py", "probe_layout.py", "probe_goal.py", "probe_batch.sh"):
        t = (D / "probes" / name).read_text(encoding="utf-8")
        assert '"/tmp/' not in t and "'/tmp/" not in t and " /tmp/probe" not in t, name
    assert 'expanduser("~/loop-census/probe")' in (D / "probes" / "probe.py").read_text(encoding="utf-8")
    assert 'expanduser("~/loop-census/probe_layout")' in (D / "probes" / "probe_layout.py").read_text(encoding="utf-8")


def _probe_layout_fn(name):
    import ast as _ast
    src = (D / "probes" / "probe_layout.py").read_text(encoding="utf-8")
    fn = next(n for n in _ast.parse(src).body if isinstance(n, _ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(_ast.Module(body=[fn], type_ignores=[]), "probe_layout.py", "exec"), ns)
    return ns[name]


def test_loop_seconds_is_first_event_to_done():
    ls = _probe_layout_fn("loop_seconds")
    ev = [{"type": "agent_loop_v6.triage_start", "ts": "2026-10-08T00:31:00Z"},
          {"type": "agent_loop_v6.step_start", "ts": "2026-10-08T00:33:00Z"},
          {"type": "agent_loop_v6.done", "ts": "2026-10-08T00:38:20Z"}]
    assert ls(ev) == 440
    assert ls(ev[:2]) is None and ls([]) is None


def test_rescore_runs_nothing():
    t = (D / "probes" / "probe_layout.py").read_text(encoding="utf-8")
    assert 'sys.argv[1] == "--rescore"' in t
    assert "if not RESCORE and not box_free():" in t
    assert "proc = _Finished() if RESCORE else start(" in t
    assert "while not RESCORE and time.time() - t0 < budget:" in t
    assert "if not RESCORE:\n    stop(sid, proc)" in t
