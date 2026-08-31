"""The editor must not be free to spend the whole context window on a 2KB file.

Figures measured 2026-08-31 against the real index.html from census run 20 (74
lines, 2154 bytes) and the real coder the loop routes to (qwen2.5-coder:14b).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import editor_output_bound as eob  # noqa: E402

pytestmark = pytest.mark.critical


CENSUS_FILE_CHARS = 2154          # the real index.html
CENSUS_RUNON_TOKENS = 16384       # what the third attempt actually spent
LEGIT_ANSWER_CHARS = 5364         # a usable 8-edit batch from the same model


def test_the_runon_would_have_been_stopped():
    """1073 seconds of run 20's wall cap went into one 16384-token edit."""
    assert eob.edit_num_predict(CENSUS_FILE_CHARS) < CENSUS_RUNON_TOKENS


def test_the_legitimate_answer_still_fits():
    """The bound is worthless if it truncates the answers that do work."""
    legit_tokens = LEGIT_ANSWER_CHARS / eob.CHARS_PER_TOKEN
    assert eob.edit_num_predict(CENSUS_FILE_CHARS) > legit_tokens


def test_the_longest_observed_answer_still_fits():
    """The largest output seen across 21 runs was 7043 characters."""
    assert eob.edit_num_predict(CENSUS_FILE_CHARS) > 7043 / eob.CHARS_PER_TOKEN


def test_a_mid_sized_file_gets_room_for_a_real_batch():
    """Between the floor and the ceiling it is HEADROOM that decides, and the
    other tests here are all satisfied by the floor alone - so this is the only
    one that actually holds it. The observed usable batch on the 2154-byte file
    carried 4334 bytes of replacement and 5364 characters of output, i.e. two to
    two-and-a-half times the file - 2.49 characters of output per byte of file.
    A 6000-byte file scaled the same way needs ~14,900 characters, which is
    ~4,270 tokens at 3.5 characters each. The figure below is that with slack,
    written as a LITERAL: deriving it from the module's own constants would let
    a wrong constant satisfy its own test."""
    assert eob.edit_num_predict(6000) >= 5000


def test_a_big_file_lands_on_the_existing_ceiling():
    """The bound must never make a large file WORSE than today: llm.generate
    already allows 16384, so a big file has to arrive at exactly that."""
    assert eob.edit_num_predict(200_000) == eob.CEILING_TOKENS


def test_a_tiny_file_gets_the_floor_not_a_useless_allowance():
    """3 bytes / 3.5 * 6 is 5 tokens - not enough to answer at all."""
    assert eob.edit_num_predict(3) == eob.FLOOR_TOKENS
    assert eob.edit_num_predict(0) == eob.FLOOR_TOKENS


def test_the_bound_never_shrinks_as_the_file_grows():
    sizes = [0, 1, 100, 2154, 10_000, 60_000, 200_000, 10 ** 7]
    got = [eob.edit_num_predict(n) for n in sizes]
    assert got == sorted(got)


def test_junk_sizes_do_not_raise():
    for bad in (None, "", "abc", -50, [1]):
        assert eob.edit_num_predict(bad) == eob.FLOOR_TOKENS


def test_describe_states_the_bound_and_the_size():
    line = eob.describe(CENSUS_FILE_CHARS)
    assert str(eob.edit_num_predict(CENSUS_FILE_CHARS)) in line
    assert "2154" in line


# ── the call site ───────────────────────────────────────────────────────────

def _workshop_source():
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "vera", "dag", "dag_workshop_capabilities.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_editor_generation_is_actually_bounded():
    src = _workshop_source()
    assert "options=_edit_gen_options" in src
    assert "_editor_output_bound.edit_num_predict(len(current))" in src


def test_the_module_is_imported_before_it_is_used():
    """The ordering trap that unregistered the operator subsystem."""
    src = _workshop_source()
    first_import = src.index("import editor_output_bound as _editor_output_bound")
    assert src.index("_editor_output_bound.edit_num_predict") > first_import


def test_the_redirect_still_hands_the_task_over_unchanged():
    """Guard on a DELIBERATE non-change. Reframing the author task at the
    redirect was implemented, measured over eight runs, shown to make no
    difference (median 100.5s / 5408 chars against 86.6s / 5986 for the task as
    it ships - marginally slower, same range), and removed. If someone adds it
    back, it should be because they have new evidence - not because it reads
    like it ought to help."""
    src = _workshop_source()
    rule1 = src[src.index('"kind": "proven_author"'):]
    rule1 = rule1[:rule1.index("# RULE 2")]
    assert '"task": str(args.get("task") or args.get("requirements") or "")' in rule1
