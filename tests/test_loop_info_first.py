"""info-first (owner 10 Oct 2026, compute-roles P1a / intelligence SK-1.4): with the run flag on, a
goal that needs the outside world gets info.seek - Vera's front door for dated, numbered evidence
(it calls intel.lookup itself) - first in its guaranteed catalogue. Off, nothing changes."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

try:
    from vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None
needs_app = pytest.mark.skipif(M is None, reason="app module not importable here")

pytestmark = pytest.mark.critical

WEB_GOAL = "Produce a short report on the latest news about the Redis licence change, with citations."


@needs_app
def test_off_by_default_and_on_puts_info_seek_first(monkeypatch):
    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "info.seek", {"name": "info.seek"})
    tok = M._INFO_FIRST_CV.set(False)
    try:
        off = M._v5_seed_caps_for(WEB_GOAL)
        assert "info.seek" not in off
        M._INFO_FIRST_CV.set(True)
        on = M._v5_seed_caps_for(WEB_GOAL)
        assert on[0] == "info.seek"
        assert [c for c in on if c != "info.seek"] == [c for c in off if c not in ("web.research", "web.search")]
        assert "info.seek" not in M._v5_seed_caps_for("write hello.html with a heading")  # not webby
    finally:
        M._INFO_FIRST_CV.reset(tok)


def test_seed_list_and_research_set():
    assert '_V5_INFO_FIRST_CAPS = ("info.seek",)' in SRC
    rc = SRC[SRC.index("_V5_RESEARCH_CAPS = {"):]
    rc = rc[:rc.index("}")]
    assert '"info.seek"' in rc and '"intel.lookup"' in rc


def test_every_run_sets_the_flag_and_the_route_passes_it():
    sig = SRC[SRC.index("async def cap_dag_agent_loop_v6("):]
    sig = sig[:sig.index("\n):")]
    assert "info_first:         bool = False," in sig
    assert "_INFO_FIRST_CV.set(bool(info_first))" in SRC
    assert 'v6_info_first        = bool(body.get("info_first", False))' in SRC
    assert "info_first=v6_info_first," in SRC


def test_info_seek_deep_is_overview_in_a_loop():
    i = SRC.index('if tool == "info.seek" and isinstance(args, dict)')
    blk = SRC[i:i + 400]
    assert '== "deep"' in blk and 'args = dict(args, need="overview")' in blk
    assert i < SRC.index('"agent_loop_v5.tool_call", "stream_id": stream_id,', i)


@needs_app
def test_info_first_replaces_web_research_not_just_leads(monkeypatch):
    monkeypatch.setitem(M.CAPABILITY_REGISTRY, "info.seek", {"name": "info.seek"})
    tok = M._INFO_FIRST_CV.set(True)
    try:
        on = M._v5_seed_caps_for(WEB_GOAL)
        assert on[0] == "info.seek" and "web.research" not in on and "web.search" not in on
    finally:
        M._INFO_FIRST_CV.reset(tok)
