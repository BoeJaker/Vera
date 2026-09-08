"""code.author repair-collapse guard (vera/dag/code_author_guards.py).

Pins the 2026-08-24 defect found on v7 run e2da05e9: a truncated HTML generation
(`missing </html>`) was "repaired" by DELETING the unfinished body, leaving a
97-byte `<html><head></head><body></body></html>`. That parses, so the cap
returned ok/syntax_ok true on an empty file and the loop burned two further
cycles discovering the stub.

Pure (no I/O, no app import), so it runs in the critical gate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import code_author_guards as G  # noqa: E402


# ── the incident itself ──────────────────────────────────────────────────────
def test_the_habit_tracker_collapse_is_rejected():
    """The exact shape that shipped: a real page reduced to an empty shell."""
    before = "<!DOCTYPE html>\n<html>\n<head><style>" + ("a{color:red}" * 200) + "</style></head>\n<body>\n<div id='app'>"
    after = '<!DOCTYPE html>\n<html lang="en">\n<head>\n    <meta charset="UTF-8">\n</head>\n<body></body>\n</html>'
    assert len(after) < 200          # the 97-byte-class stub
    assert G.repair_collapsed(before, after) is True


# ── legitimate repairs must NOT be blocked ───────────────────────────────────
def test_closing_the_document_is_allowed():
    """The fix that SHOULD have happened — append `</body></html>`. Grows the file."""
    before = "<!DOCTYPE html><html><head></head><body>" + ("<p>x</p>" * 100)
    after = before + "</body></html>"
    assert G.repair_collapsed(before, after) is False


def test_small_targeted_edit_is_allowed():
    before = "<html><body>" + ("<p>hello</p>" * 100) + "</body></html>"
    after = before.replace("<p>hello</p>", "<p>hullo</p>", 1)
    assert G.repair_collapsed(before, after) is False


def test_trimming_a_leaked_tail_is_allowed():
    """Removing a modest junk tail is a real repair, not a collapse."""
    body = "<html><body>" + ("<p>x</p>" * 200) + "</body></html>"
    before = body + "\n// stray editor JSON leak\n" + ("junk " * 20)
    assert G.repair_collapsed(before, body) is False


# ── boundary behaviour ───────────────────────────────────────────────────────
def test_short_files_are_never_flagged():
    """Below the floor the ratio is meaningless — never block a fix to a tiny file."""
    before = "x" * (G.COLLAPSE_MIN_BEFORE - 1)
    assert G.repair_collapsed(before, "") is False


def test_at_the_floor_a_total_deletion_is_flagged():
    before = "x" * G.COLLAPSE_MIN_BEFORE
    assert G.repair_collapsed(before, "") is True


def test_ratio_boundary_is_exclusive():
    """Exactly at the ratio is allowed; one char under is a collapse."""
    before = "x" * 1000
    assert G.repair_collapsed(before, "x" * 500, ratio=0.5, min_before=400) is False
    assert G.repair_collapsed(before, "x" * 499, ratio=0.5, min_before=400) is True


def test_whitespace_only_change_is_not_a_collapse():
    before = "<html>" + ("<p>x</p>" * 100) + "</html>"
    assert G.repair_collapsed(before, "\n\n  " + before + "  \n") is False


def test_handles_none_and_empty_safely():
    assert G.repair_collapsed(None, None) is False
    assert G.repair_collapsed("", "") is False
    assert G.repair_collapsed("y" * 500, None) is True


def test_thresholds_are_overridable():
    """Operators can retune without a code change (env-backed constants)."""
    before = "x" * 1000
    assert G.repair_collapsed(before, "x" * 800, ratio=0.9, min_before=400) is True
    assert G.repair_collapsed(before, "x" * 800, ratio=0.5, min_before=400) is False


# â”€â”€ the file changed under the model, and nobody told it â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Census 40, author-then-edit. code.edit anchored `let countdown = 60 * 60;`;
# the file held `countdown = 60 * 60;` - exactly the edit a repair pass makes
# for "Identifier 'countdown' has already been declared". code.author returns
# path/bytes/syntax_ok/checked_with and NEVER the saved content, and its own
# note tells the caller "do NOT read it back" - so the run was instructed into
# editing from memory, and the anchor missed.

def test_a_repaired_file_says_the_content_changed():
    note = G.repaired_note(1)
    assert "REPAIRED" in note
    assert "NOT what you wrote" in note
    assert "COPIED FROM THE FILE" in note


def test_the_pass_count_is_reported():
    assert "2 pass(es)" in G.repaired_note(2)


def test_a_file_that_was_never_repaired_says_nothing():
    """Telling every caller to re-read a file byte-identical to what it just
    wrote is the mistake the do-not-read-it-back clause exists to prevent."""
    assert G.repaired_note(0) == ""
    assert G.repaired_note(None) == ""


def test_an_unreadable_count_says_nothing_rather_than_guessing():
    assert G.repaired_note("banana") == ""
    assert G.repaired_note(-1) == ""
