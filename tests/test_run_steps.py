"""Progress steps: vera/run_steps.emit_step + its use in the netscan scans.

The helper is tested by running it against a fake event bus and a fake clock.
The scans are checked by SOURCE INSPECTION (ast): running them needs the aux
graph, SSH and the full capability registry, so we assert each scan calls
emit_step with a start stage, a progress stage and a final "done" stage.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def rs(monkeypatch):
    """vera.run_steps with a recording emit_event and a fake clock."""
    events = []

    async def fake_emit(ev):
        events.append(ev)

    fake_mod = types.ModuleType("Vera.vera.capability_orchestration")
    fake_mod.emit_event = fake_emit
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", fake_mod)

    import vera.run_steps as mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod, "_clock", lambda: clock["t"])
    mod._last.clear()
    mod.events = events
    mod.clock = clock
    mod.fake_mod = fake_mod
    yield mod
    mod._last.clear()


def run(coro):
    return asyncio.run(coro)


def test_event_shape(rs):
    run(rs.emit_step("netscan.lan.scan", "t1", "probing", done=3, total=10,
                     text="x" * 500, found=2))
    assert len(rs.events) == 1
    ev = rs.events[0]
    assert ev["type"] == "netscan.lan.scan.step"
    assert ev["cap"] == "netscan.lan.scan"
    assert ev["trace_id"] == "t1"
    assert ev["stage"] == "probing"
    assert ev["done"] == 3 and ev["total"] == 10
    assert ev["found"] == 2
    assert len(ev["text"]) == 300
    assert isinstance(ev["ts"], str) and "T" in ev["ts"]


def test_none_values_omitted(rs):
    run(rs.emit_step("c", "t1", "starting", text="hi", extra_none=None))
    ev = rs.events[0]
    assert "done" not in ev and "total" not in ev and "extra_none" not in ev
    assert ev["text"] == "hi"


def test_no_trace_no_event(rs):
    run(rs.emit_step("c", None, "starting"))
    run(rs.emit_step("c", "", "starting"))
    assert rs.events == []
    assert len(rs._last) == 0


def test_throttle_collapses_same_stage(rs):
    async def go():
        for i in range(1, 50):
            await rs.emit_step("c", "t1", "probing", done=i, total=100)
            rs.clock["t"] += 0.01
    run(go())
    # 49 steps over 0.49 s at one per 0.25 s -> 2 go out
    assert len(rs.events) == 2
    rs.clock["t"] += 0.3
    run(rs.emit_step("c", "t1", "probing", done=60, total=100))
    assert len(rs.events) == 3


def test_stage_change_and_final_always_go(rs):
    async def go():
        await rs.emit_step("c", "t1", "starting", total=5)
        await rs.emit_step("c", "t1", "probing", done=1, total=5)   # stage change
        await rs.emit_step("c", "t1", "probing", done=2, total=5)   # throttled
        await rs.emit_step("c", "t1", "probing", done=5, total=5)   # final
        await rs.emit_step("c", "t1", "done", done=5, total=5)      # final
    run(go())
    assert [(e["stage"], e.get("done")) for e in rs.events] == [
        ("starting", None), ("probing", 1), ("probing", 5), ("done", 5)]
    # state for a finished trace is dropped
    assert ("c", "t1") not in rs._last


def test_traces_throttled_independently(rs):
    async def go():
        await rs.emit_step("c", "t1", "probing", done=1, total=9)
        await rs.emit_step("c", "t2", "probing", done=1, total=9)
        await rs.emit_step("d", "t1", "probing", done=1, total=9)
    run(go())
    assert len(rs.events) == 3


def test_state_dict_is_capped(rs, monkeypatch):
    monkeypatch.setattr(rs, "MAX_TRACKED", 8)

    async def go():
        for i in range(30):
            await rs.emit_step("c", f"t{i}", "probing", done=0, total=5)
    run(go())
    assert len(rs._last) <= 8
    assert len(rs.events) == 30


def test_emit_failure_never_raises(rs):
    async def boom(ev):
        raise RuntimeError("bus down")
    rs.fake_mod.emit_event = boom
    run(rs.emit_step("c", "t1", "starting", total=3))   # must not raise


def test_import_failure_never_raises(rs, monkeypatch):
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", None)
    run(rs.emit_step("c", "t1", "starting", total=3))   # must not raise


# ── scans: source inspection ────────────────────────────────────────────────
EXEC = ROOT / "vera" / "execution" / "exec_capabilities.py"


def _emit_step_stages(func_name: str):
    tree = ast.parse(EXEC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == func_name)
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "emit_step"):
            assert isinstance(node.args[1], ast.Name) and node.args[1].id == "trace_id"
            stage = node.args[2]
            assert isinstance(stage, ast.Constant)
            kws = {k.arg for k in node.keywords}
            out.append((node.lineno, stage.value, kws))
    return sorted(out)


@pytest.mark.parametrize("func,progress", [
    ("cap_netscan_lan", {"probing"}),
    ("cap_netscan_docker", {"host", "containers"}),
    ("cap_netscan_proxmox", {"node", "guests"}),
])
def test_scans_emit_start_progress_done(func, progress):
    steps = _emit_step_stages(func)
    stages = [s for _, s, _ in steps]
    assert "starting" in stages and "done" in stages, stages
    assert progress <= set(stages), stages
    first_start = min(l for l, s, _ in steps if s == "starting")
    last_done = max(l for l, s, _ in steps if s == "done")
    for line, s, kws in steps:
        if s in progress:
            assert first_start < line < last_done
            assert {"done", "total", "found"} <= kws, (s, kws)
    done_kws = next(k for _, s, k in steps if s == "done")
    assert {"done", "total", "found"} <= done_kws


def test_exec_capabilities_parses_and_imports_helper():
    src = EXEC.read_text(encoding="utf-8")
    ast.parse(src)
    assert "from Vera.vera.run_steps import emit_step" in src
    for ev in ("netscan.lan.done", "netscan.docker.done", "netscan.proxmox.done"):
        assert ev in src
