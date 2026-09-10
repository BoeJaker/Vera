"""The Ship page's cap must load with the app and read the five stores
through the evolve module's own readers.

Runs in-container, where Vera.vera resolves to THIS checkout; skips on a
host venv. No Redis is needed: every reader is stubbed at the module the
cap reaches through sys.modules.
"""
import asyncio
import os

import pytest

try:
    from Vera.vera import capability_orchestration as ORCH
    from Vera.vera.evolve import evolve_capabilities as EV
    from Vera.vera.evolve import ship_capabilities as SH
except Exception:                                    # pragma: no cover
    ORCH = EV = SH = None

_HERE_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
_SAME_TREE = bool(SH is not None and
                  os.path.realpath(getattr(SH, "__file__", "")).startswith(_HERE_ROOT))
pytestmark = pytest.mark.skipif(not _SAME_TREE, reason="app module not importable from THIS checkout here")


def run(coro):
    return asyncio.run(coro)


def test_the_cap_is_registered():
    assert "evolve.ship.branches" in ORCH.CAPABILITY_REGISTRY


def test_the_table_reads_the_five_stores_in_one_call(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setitem(sys.modules, "ide_capabilities", None)
    calls = []

    def stub(name, value):
        async def _f(*a, **kw):
            calls.append((name, kw))
            return value
        monkeypatch.setattr(EV, name, _f)

    stub("evolve_pipeline_list", {"pipelines": [
        {"id": "p-live", "branch": "feat/live", "kind": "code", "decision": "pending", "live": True,
         "created_at": "2026-09-10T10:00:00Z", "gate_passed": None},
        {"id": "p-old", "branch": "feat/done", "kind": "code", "decision": "promoted", "live": False,
         "created_at": "2026-09-09T10:00:00Z", "gate_passed": True, "controller": "claude_code"}]})
    stub("evolve_sandbox_list", {"sandboxes": [{"name": "vera-dev-feat-done", "branch": "feat/done", "running": True,
                                                "role": "spawned", "port": 8983, "merged_to_bleeding_edge": True}],
                                 "capacity": {"available_slots": 1, "redis_db_capacity": 13}})
    stub("evolve_unittest_history", {"runs": [{"ts": "2026-09-09T10:05:00Z", "branch": "feat/done", "ok": True,
                                               "passed": 10, "failed": 0, "total": 10}],
                                     "lanes": [{"ts": "2026-09-09T10:05:00Z", "ok": True, "total": 10}],
                                     "trend": {"n": 1}, "race": None, "regressions": []})
    stub("evolve_bleeding_edge_list", {"edges": [{"name": "bleeding-edge", "branch": "bleeding-edge", "default": True,
                                                  "exists": True, "head": "abc", "main_state": "released"}], "main": "main"})
    stub("evolve_git_status", {"branch": "main", "dirty": True, "dirty_files": ["a.py"], "branches": ["loop-lab/x-mirror"], "repo": "vera"})
    stub("evolve_sandbox_status", {"up": False, "sandbox": {}, "dev_port": 8998})

    out = run(SH.cap_evolve_ship_branches())
    assert {"branches", "count", "total", "summary", "edges", "main", "git", "capacity", "runner", "proposals", "tests",
            "any_live"} <= set(out)
    names = [b["branch"] for b in out["branches"]]
    assert names == ["main", "bleeding-edge", "feat/live", "feat/done", "loop-lab/x-mirror"]
    assert out["any_live"] is True and out["summary"]["stages"]["live"] == 1
    assert out["git"] == {"branch": "main", "dirty": True, "dirty_files": 1, "branches": ["loop-lab/x-mirror"]}
    assert out["capacity"]["available_slots"] == 1 and out["runner"]["dev_port"] == 8998
    assert out["tests"]["lanes"] and out["tests"]["trend"] == {"n": 1}
    assert out["proposals"] == [], "no ide module here: no proposals, not an error"
    done = next(b for b in out["branches"] if b["branch"] == "feat/done")
    assert done["sandbox"]["port"] == 8983 and done["tests"]["total"] == 10 and done["stage"] == "merged"
    assert dict(calls)["evolve_pipeline_list"] == {"limit": SH.PIPELINES_N}
    assert dict(calls)["evolve_sandbox_list"] == {"detail": True}

    calls.clear()
    lean = run(SH.cap_evolve_ship_branches(detail="false", stage="live", hide_merged="true", limit=1))
    assert dict(calls)["evolve_sandbox_list"] == {"detail": False}, "a poll can skip the sandbox git probes"
    assert [b["branch"] for b in lean["branches"]] == ["feat/live"] and lean["count"] == 1 and lean["total"] == 5


def test_a_failing_reader_costs_its_column_not_the_page(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "evolve_capabilities", EV)
    monkeypatch.setitem(sys.modules, "ide_capabilities", None)

    async def boom(*a, **kw):
        raise RuntimeError("docker is away")

    async def ok(*a, **kw):
        return {"pipelines": [{"id": "p", "branch": "feat/x", "decision": "promoted", "created_at": "2026-09-10T10:00:00Z"}]}

    async def empty(*a, **kw):
        return {}
    monkeypatch.setattr(EV, "evolve_pipeline_list", ok)
    monkeypatch.setattr(EV, "evolve_sandbox_list", boom)
    monkeypatch.setattr(EV, "evolve_unittest_history", empty)
    monkeypatch.setattr(EV, "evolve_bleeding_edge_list", empty)
    monkeypatch.setattr(EV, "evolve_git_status", boom)
    monkeypatch.setattr(EV, "evolve_sandbox_status", boom)
    out = run(SH.cap_evolve_ship_branches())
    assert [b["branch"] for b in out["branches"]] == ["main", "feat/x"]
    assert out["capacity"] is None and out["runner"] == {} and out["git"]["branch"] == ""
