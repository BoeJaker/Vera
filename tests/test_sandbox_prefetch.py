"""The loop creates the session sandbox while it plans, and one creation serves every
caller that races for it (live 2026-09-30: creation took 30-54 s before tiering)."""

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

try:
    from Vera.vera.remote import session_sandbox_capabilities as SB
except Exception:                                    # pragma: no cover
    SB = None


def test_the_loop_launches_the_sandbox_and_waits_only_before_execution():
    i = SRC.index("_artifact_task = asyncio.ensure_future(_exec_mod.artifact_dir_async(session_id=sid))")
    tier = SRC.index("_tier_catalog_brief = ", i)
    ready = SRC.index("    artifact_dir_path = await _artifact_dir_ready()\n", i)
    execute = SRC.index("# ── Execute over a shared ledger with an adaptive controller", i)
    assert i < tier < ready < execute
    # nothing between the launch and execution awaits the old blocking call
    assert "await _exec_mod.artifact_dir_async(" not in SRC[i:execute]


@pytest.mark.skipif(SB is None or not hasattr(SB, "_create_lock"),
                    reason="app module (with the lock) not importable here")
def test_racing_callers_share_one_creation(monkeypatch):
    state = {"rec": None, "starts": 0}

    async def get_rec(sid):
        return state["rec"]

    async def resolve(sid):
        return sid

    async def get_cfg():
        return {"auto_create": True}

    async def touch(rec):
        return None

    async def start(session_id="", enable=True):
        state["starts"] += 1
        await asyncio.sleep(0.05)                    # creation takes time
        state["rec"] = {"container": "vera-sbx-x", "active": True, "backend": "docker"}
        return {"ok": True}

    monkeypatch.setattr(SB, "_get_rec", get_rec)
    monkeypatch.setattr(SB, "_resolve_sid", resolve)
    monkeypatch.setattr(SB, "_get_cfg", get_cfg)
    monkeypatch.setattr(SB, "_touch", touch)
    monkeypatch.setattr(SB, "cap_sbx_start", start)
    monkeypatch.setattr(SB, "_is_local_rec", lambda rec: False)
    monkeypatch.setattr(SB, "_dk", lambda: None)     # no docker probe once a record exists
    SB._CREATE_LOCKS.pop("race-sid", None)

    async def go():
        return await asyncio.gather(*[SB._ensure_routable("race-sid", create=True) for _ in range(4)])
    recs = asyncio.run(go())
    assert state["starts"] == 1
    assert all(r and r.get("container") == "vera-sbx-x" for r in recs)
