"""A census pause/yield can END BY ITSELF (census.control.set hours=...; compute-roles, 11 Oct 2026):
a manual yield used to block every later census slot until someone lifted it by hand."""
import ast
import json
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.census import control as C  # noqa: E402

HARNESS = (ROOT / "tools" / "loop-census" / "run_census.py").read_text(encoding="utf-8")
CAPS = (ROOT / "vera" / "census" / "census_capabilities.py").read_text(encoding="utf-8")
pytestmark = pytest.mark.critical


def test_until_rides_on_pause_and_yield_only():
    y = C.make_control("yield", reason="owner evening", until="2026-10-11T23:00:00Z")
    assert y["until"] == "2026-10-11T23:00:00Z" and y["after_goal"]
    assert "until" not in C.make_control("drop", until="2026-10-11T23:00:00Z")
    assert "until" not in C.make_control("yield")


def test_state_turns_to_run_after_until():
    y = C.make_control("yield", until="2026-10-11T23:00:00Z")
    before = time.mktime((2026, 10, 11, 22, 0, 0, 0, 0, 0)) - time.timezone
    assert not C.hold_expired(y, now=before)
    assert C.hold_expired(y, now=before + 2 * 3600)
    old = C.make_control("yield", until="2000-01-01T00:00:00Z")
    assert C.control_state(old) == "run" and C.control_state(C.make_control("yield")) == "yield"
    assert C.control_state(dict(C.make_control("drop"), until="2000-01-01T00:00:00Z")) == "drop"


def test_harness_resumes_an_expired_hold(tmp_path):
    tree = ast.parse(HARNESS)
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_hold_expired", "read_control")]
    f = tmp_path / "census.control.json"
    ns = {"json": json, "time": time, "log": lambda *a: None, "CONTROL_FILE": str(f)}
    exec(compile(ast.Module(body=body, type_ignores=[]), "rc", "exec"), ns)
    f.write_text(json.dumps({"pause": True, "after_goal": True, "until": "2000-01-01T00:00:00Z"}))
    assert ns["read_control"]() == {}
    assert json.loads(f.read_text())["pause"] is False
    f.write_text(json.dumps({"pause": True, "after_goal": True}))
    assert ns["read_control"]()["pause"] is True


def test_cap_takes_hours():
    assert "wait_s: int = 0, hours: float = 0, trace_id=None" in CAPS
    assert "until=until)" in CAPS
