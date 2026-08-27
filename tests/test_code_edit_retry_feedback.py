"""A rejected edit batch must tell the model what was FINE, not only what broke.

Measured on prod 2026-08-27 with a seeded file:
  single change          -> 1 edit,  ok
  three-part change      -> FAILED all 3 attempts: "edit 3: empty `find`"
  rename across sites    -> 6 edits, ok
  prepend a docstring    -> 1 edit,  ok  (alone, it succeeds)
  append a function      -> 1 edit,  ok

So batches of up to six edits are normal and succeed, and each individual piece
succeeds in isolation - but ONE malformed edit inside a batch invalidates all of
them (`ok = bool(applied) and not errors`), and the retry carried only the error
string. The model therefore re-derived every edit blind against a full re-send of
the file, three times, and the call then failed outright: 23s for no change.

The atomic save is DELIBERATE and is kept. Applying a subset of that 6-edit
rename would leave a file that parses and is semantically half-renamed, which is
worse than a clean retry. What changes is the FEEDBACK.

Imports the app module, so it runs in-container and skips on a host venv.
"""
import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")

SRC = "alpha = 1\nbeta = 2\ngamma = 3\n"


def test_this_module_actually_imported_the_app():
    assert M is not None and hasattr(M, "_v5_apply_edits")


def test_partial_batch_still_reports_which_edits_applied():
    """The data the retry needs must survive a failed batch."""
    res = M._v5_apply_edits(SRC, [
        {"find": "alpha = 1", "replace": "alpha = 10"},
        {"find": "beta = 2", "replace": "beta = 20"},
        {"find": "", "replace": "nope"},                 # malformed
    ])
    assert res["ok"] is False, "one bad edit must still fail the batch"
    assert len(res["applied"]) == 2, "the two good edits must be reported as applied"
    assert len(res["errors"]) == 1
    previews = " ".join(a.get("find_preview", "") for a in res["applied"])
    assert "alpha" in previews and "beta" in previews


def test_the_batch_is_not_saved_partially():
    """Atomicity is the protection against a half-applied rename."""
    res = M._v5_apply_edits(SRC, [
        {"find": "alpha = 1", "replace": "alpha = 10"},
        {"find": "nonexistent anchor", "replace": "x"},
    ])
    assert res["ok"] is False, "a batch with any failure must not be saved"


def test_empty_find_error_explains_how_to_insert():
    """The observed failure. 'empty `find`' alone told the model nothing usable."""
    res = M._v5_apply_edits(SRC, [{"find": "", "replace": "# header\n"}])
    assert res["ok"] is False
    msg = " ".join(res["errors"]).lower()
    assert "insert" in msg, "the error must name the thing the model was trying to do"
    assert "nearest existing line" in msg, "it must say HOW to anchor an insertion"


def test_absent_anchor_error_still_shows_the_text_it_looked_for():
    res = M._v5_apply_edits(SRC, [{"find": "delta = 4", "replace": "delta = 40"}])
    assert res["ok"] is False
    assert "delta = 4" in " ".join(res["errors"]), \
        "a missing anchor must echo what it searched for"


def test_ambiguous_anchor_is_still_refused():
    """Guard the existing protection: >1 match must never be guessed at."""
    res = M._v5_apply_edits("x = 1\nx = 1\n", [{"find": "x = 1", "replace": "x = 2"}])
    assert res["ok"] is False
    assert "2 places" in " ".join(res["errors"]) or "unique" in " ".join(res["errors"])


def test_a_clean_batch_still_applies_all_of_it():
    """Control - the failure paths must not have broken the happy path."""
    res = M._v5_apply_edits(SRC, [
        {"find": "alpha = 1", "replace": "alpha = 10"},
        {"find": "gamma = 3", "replace": "gamma = 30"},
    ])
    assert res["ok"] is True
    assert len(res["applied"]) == 2
    assert "alpha = 10" in res["content"] and "gamma = 30" in res["content"]
    assert "beta = 2" in res["content"], "untouched lines must survive"
