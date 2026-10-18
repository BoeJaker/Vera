"""A release never leaves the census parked for nobody to lift (6-8 Oct 2026: no census ran
for two days). Release 8061bbc5 (census=goal) wrote a yield over an IDLE census, and back-to-
back releases replaced the node-sync record that owed the census its resume."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.evolve import release_core as core  # noqa: E402

SRC = (ROOT / "vera" / "evolve" / "release_capabilities.py").read_text(encoding="utf-8")

pytestmark = pytest.mark.critical


def test_goal_mode_yields_only_a_census_in_flight():
    for st in ("running", "yielding", "pausing"):
        assert core.goal_yield_needed({"state": st})
    for st in ("done", "yield", "paused", "stale", "none", ""):
        assert not core.goal_yield_needed({"state": st})
    assert not core.goal_yield_needed({})
    assert not core.goal_yield_needed(None)


def test_a_replacing_sync_record_inherits_the_resume_it_owes():
    old = {"release_id": "8061bbc5", "resume_census": True}
    new = core.new_node_sync(release_id="849d9e64", commit="c", now=core.parse_iso("2026-10-06T20:18:51+00:00"),
                             resume_census=False)
    got = core.carry_census_resume(old, new)
    assert got["resume_census"] is True and got["resume_inherited_from"] == "8061bbc5"
    assert new["resume_census"] is False                      # the input is not mutated
    assert core.carry_census_resume({"resume_census": False}, new)["resume_census"] is False
    assert core.carry_census_resume(None, new) is new
    own = dict(new, resume_census=True)
    assert core.carry_census_resume(old, own) is own


def test_both_paths_are_wired():
    # the release body is shared by evolve.release.prod and the release lane (B1.0, 18 Oct 2026)
    opener = SRC[SRC.index("async def _open_release("):SRC.index('@capability("evolve.release.prod"')]
    assert 'if mode == "goal" and core.goal_yield_needed(summary):' in opener
    assert "return await _open_release(" in SRC[SRC.index("async def cap_release_prod("):]
    q = SRC[SRC.index("async def _queue_node_sync("):]
    q = q[:q.index("\ndef ")]
    assert "core.carry_census_resume(await _node_sync_rec(), rec)" in q
    assert q.index("carry_census_resume") < q.index("await _node_sync_save(rec)")
