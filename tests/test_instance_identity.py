"""An estate write names its writer, and silence names the rest.

A stale sandbox running pre-2026-08-30 code spent a day deleting sandbox pool
descriptors. It was found only because prune's audit summary happened to change
FORMAT between versions - 23 old-format passes, 7 destructive; 21 new-format,
none. That was luck. Nothing in an entry said which process wrote it.

The design point these tests hold: a stale instance runs its own copy of the
code and will never call anything added here, so the absence of a stamp is the
signal. Tests that only checked "stamped entries are labelled" would miss the
half that matters.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.evolve import instance_identity as ii  # noqa: E402

pytestmark = pytest.mark.critical


CUR = "52b50eb"
OLD = "995666d"


def _e(action, by=None, ts="2026-09-01T05:58:34Z"):
    e = {"ts": ts, "action": action, "summary": "..."}
    if by is not None:
        e["by"] = by
    return e


STAMPED_CUR = {"host": "LLM", "pid": 1, "dev": False, "ver": CUR}
STAMPED_SANDBOX = {"host": "a1b2c3d4e5f6", "pid": 1, "dev": True, "ver": OLD}


# ── the half that matters: an unstamped write ───────────────────────────────

def test_an_unstamped_estate_write_is_flagged_stale():
    """The stale instance never runs this code, so it can only be identified by
    what it does NOT write."""
    plan = ii.classify_writers([_e("sandbox.prune")], current_ver=CUR)
    assert plan["unstamped"] == 1
    assert plan["stale_writers"] == ["UNSTAMPED"]


def test_the_note_explains_what_unstamped_means():
    """"UNSTAMPED x7" is only useful if it says what to do about it."""
    plan = ii.classify_writers([_e("sandbox.prune")] * 7, current_ver=CUR)
    note = ii.describe(plan)
    assert "UNSTAMPED x7" in note
    assert "older than" in note and "restarted" in note


def test_a_stamped_write_from_the_current_build_is_not_flagged():
    plan = ii.classify_writers([_e("sandbox.prune", STAMPED_CUR)], current_ver=CUR)
    assert plan["stale_writers"] == []
    assert "all on the current build" in ii.describe(plan)


def test_a_stamped_write_from_an_OLDER_build_is_flagged():
    """Once every instance stamps, this is how the next one gets caught - by
    version rather than by absence."""
    plan = ii.classify_writers([_e("sandbox.prune", STAMPED_SANDBOX)], current_ver=CUR)
    assert plan["stale_writers"] == ["a1b2c3d4e5f6@995666d (dev)"]


def test_a_dev_sandbox_writer_is_marked_as_one():
    """A container's hostname IS its id, which is what makes it traceable."""
    assert "(dev)" in ii.writer_key(_e("sandbox.prune", STAMPED_SANDBOX))
    assert "(dev)" not in ii.writer_key(_e("sandbox.prune", STAMPED_CUR))


# ── only estate actions count ───────────────────────────────────────────────

def test_reads_and_unrelated_actions_are_ignored():
    """Flagging every old audit line would bury the finding - an unstamped
    pipeline.adopt is merely old, not dangerous."""
    plan = ii.classify_writers(
        [_e("pipeline.adopt"), _e("sandbox.exec"), _e("config.set")], current_ver=CUR)
    assert plan["writers"] == [] and plan["unstamped"] == 0


def test_the_destructive_actions_are_all_covered():
    for a in ("sandbox.prune", "sandbox.down", "sandbox.reap", "branch.delete",
              "worktree.remove", "bleeding_edge.promote_to_main"):
        assert ii.is_estate_action(a), a


def test_counts_and_last_timestamp_are_kept_per_writer():
    plan = ii.classify_writers([
        _e("sandbox.prune", STAMPED_CUR, ts="2026-09-01T01:00:00Z"),
        _e("sandbox.down", STAMPED_CUR, ts="2026-09-01T03:00:00Z"),
        _e("sandbox.prune", ts="2026-09-01T02:00:00Z"),
    ], current_ver=CUR)
    by = {r["writer"]: r for r in plan["writers"]}
    assert by[f"LLM@{CUR}"]["count"] == 2
    assert by[f"LLM@{CUR}"]["last_ts"] == "2026-09-01T03:00:00Z"
    assert by[f"LLM@{CUR}"]["actions"] == ["sandbox.down", "sandbox.prune"]
    assert by["UNSTAMPED"]["count"] == 1


def test_stale_writers_sort_first():
    """The finding must not be below twenty healthy rows."""
    rows = [_e("sandbox.prune", STAMPED_CUR)] * 20 + [_e("sandbox.prune")]
    plan = ii.classify_writers(rows, current_ver=CUR)
    assert plan["writers"][0]["writer"] == "UNSTAMPED"


# ── stamping ────────────────────────────────────────────────────────────────

def test_stamp_adds_identity_without_overwriting_one():
    """An entry forwarded from elsewhere keeps its original writer."""
    kept = ii.stamp({"action": "sandbox.prune", "by": STAMPED_SANDBOX})
    assert kept["by"] == STAMPED_SANDBOX
    fresh = ii.stamp({"action": "sandbox.prune"})
    assert set(fresh["by"]) == {"host", "pid", "dev", "ver"}


def test_stamp_does_not_mutate_the_caller_entry():
    e = {"action": "sandbox.prune"}
    ii.stamp(e)
    assert "by" not in e


def test_identity_is_stable_within_a_process():
    """It cannot change without a restart, and re-shelling git per audit line
    would be absurd."""
    assert ii.identity() == ii.identity()


def test_junk_does_not_raise():
    assert ii.classify_writers(None)["writers"] == []
    assert ii.classify_writers(["not a dict", None, 7])["writers"] == []
    assert ii.writer_key({}) == "UNSTAMPED"
    assert ii.writer_key({"by": "not a mapping"}) == "UNSTAMPED"
    assert ii.describe({}) == "no estate writes recorded"


# ── the call site ───────────────────────────────────────────────────────────

def _src():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "vera", "evolve", "evolve_capabilities.py"),
              encoding="utf-8") as fh:
        return fh.read()


def test_audit_stamps_every_entry():
    """_audit is the single chokepoint every estate action goes through."""
    src = _src()
    body = src[src.index("async def _audit("):]
    body = body[:body.index("\n@capability")]
    assert "_instance_identity.stamp(entry)" in body


def test_the_readback_capability_exists_and_binds_to_its_own_function():
    """The decorator-theft trap: a @capability inserted before another def
    registers the WRONG function (it took down evolve.sandbox.prune once)."""
    import ast
    tree = ast.parse(_src())
    bound = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "capability":
                    if d.args and isinstance(d.args[0], ast.Constant):
                        bound[d.args[0].value] = n.name
    assert bound.get("evolve.estate.writers") == "evolve_estate_writers"
    assert bound.get("evolve.audit.list") == "evolve_audit_list"
