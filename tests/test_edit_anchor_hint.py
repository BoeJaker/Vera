"""A rejected edit must leave the model something to correct.

`_v5_apply_edits` refuses an anchor matching zero or many times. The MANY case
already named the matching line numbers and told the editor what to do. The
ZERO case echoed the model's own `find` text back at it - the one thing it
already knew - so a retry had nothing new and repeated the mistake.

Measured: census run 16, `author-then-edit` step 3. code.edit failed that way at
cycle 10 (32.6s) and identically again at cycle 14 (32.0s).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.dag.edit_anchor_hint import (          # noqa: E402
    FLOOR, describe_missing_anchor, nearest_lines, whitespace_only_match,
)

FILE = """<!DOCTYPE html>
<html>
  <body>
    <script>
      let remaining = 60;
      const el = document.getElementById("t");
      function tick() {
        remaining -= 1;
      }
    </script>
  </body>
</html>
"""


# --- the whitespace case, which is the common one --------------------------

def test_a_multi_line_anchor_that_differs_only_in_indentation_is_named_as_such():
    """The realistic shape. A multi-line anchor carries the newline AND the
    indentation of every line after the first, so re-typing it from memory
    fails even though the code is right there."""
    find = "function tick() {\n  remaining -= 1;\n}"
    msg = describe_missing_anchor(FILE, find)
    assert "apart from whitespace" in msg
    assert "line 7" in msg
    assert "EXACTLY" in msg


def test_a_de_indented_SINGLE_line_anchor_never_needs_this_hint():
    """Pins why the hint targets multi-line anchors: `find` is matched as a
    SUBSTRING of the whole file, so a single line that merely lost its leading
    spaces still matches and never reaches the zero-match branch. Verified
    against the real _v5_apply_edits, which returned ok=True for exactly this.
    """
    assert "let remaining = 60;" in FILE          # substring match succeeds
    assert FILE.count("let remaining = 60;") == 1


def test_collapsed_inner_spacing_is_also_recognised():
    assert whitespace_only_match(FILE, "      let  remaining   =  60;") == [5]


def test_a_genuinely_absent_anchor_is_not_called_a_whitespace_problem():
    assert whitespace_only_match(FILE, "let elapsed = 0;") == []


def test_a_multi_line_anchor_can_match_on_whitespace_too():
    find = "function tick() {\nremaining -= 1;\n}"
    assert whitespace_only_match(FILE, find) == [7]


# --- otherwise, show what is really there ----------------------------------

def test_a_near_miss_is_told_which_real_lines_are_closest():
    msg = describe_missing_anchor(FILE, "let remaining = 90;")
    assert "Closest text actually in the file" in msg
    assert "let remaining = 60;" in msg
    assert "line 5" in msg


def test_the_suggestions_are_ordered_best_first():
    near = nearest_lines(FILE, "let remaining = 90;")
    assert near and near[0]["text"] == "let remaining = 60;"
    assert near == sorted(near, key=lambda d: (-d["ratio"], d["line"]))


def test_nothing_similar_says_so_rather_than_inventing_a_suggestion():
    """A wrong suggestion is worse than none - it sends the retry somewhere
    plausible and wrong."""
    msg = describe_missing_anchor(FILE, "SELECT * FROM customers WHERE id = 42")
    assert "Nothing in the file resembles it" in msg
    assert "wrong file" in msg


def test_a_weak_resemblance_is_below_the_floor():
    assert nearest_lines(FILE, "zzzzzzzzzzzz") == []


def test_suggestions_are_bounded_in_number_and_length():
    content = "\n".join(f"value_{i} = compute_something_long(x, y, z)" for i in range(40))
    near = nearest_lines(content, "value_99 = compute_something_long(x, y, z)")
    assert 0 < len(near) <= 3
    assert all(len(n["text"]) <= 120 for n in near)


# --- shape and safety -------------------------------------------------------

def test_the_message_still_identifies_which_edit_failed():
    msg = describe_missing_anchor(FILE, "nope nope nope", edit_no=3)
    assert msg.startswith("edit 3:")


def test_blank_lines_are_never_offered_as_anchors():
    assert nearest_lines("a = 1\n\n\n\nb = 2\n", "   ") == []


def test_an_empty_file_or_anchor_is_survivable():
    assert nearest_lines("", "anything") == []
    assert nearest_lines(FILE, "") == []
    assert whitespace_only_match("", "x") == []
    assert describe_missing_anchor("", "x").startswith("edit 1:")


@pytest.mark.parametrize("bad", [None, 0])
def test_non_string_input_does_not_raise(bad):
    assert nearest_lines(bad or "", "x") == []


def test_the_floor_is_a_real_threshold_not_zero():
    """If the floor were 0 every line would qualify and the hint becomes noise."""
    assert 0.0 < FLOOR < 1.0


# --- the callsite -----------------------------------------------------------

def test_apply_edits_actually_uses_the_hint():
    src_path = os.path.join(os.path.dirname(__file__), "..", "vera", "dag",
                            "dag_workshop_capabilities.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "_edit_anchor_hint.describe_missing_anchor(" in src
    # and it must be imported BEFORE the function that calls it
    imp = src.index("from Vera.vera.dag import edit_anchor_hint")
    use = src.index("_edit_anchor_hint.describe_missing_anchor(")
    assert imp < use, "the import must precede its use in file order"


def test_the_ambiguous_case_still_names_its_lines():
    """The n>1 branch was already good; this change must not disturb it."""
    src_path = os.path.join(os.path.dirname(__file__), "..", "vera", "dag",
                            "dag_workshop_capabilities.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "matches {n} places (lines " in src
