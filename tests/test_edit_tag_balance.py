"""Naming the edit that unbalanced the markup.

The batch below is the real one: qwen2.5-coder:14b editing the real index.html
from census run 20 on 2026-08-31. Applying it turns a file with one <script>
and one </script> into a file with one and two - which is byte-for-byte the
error the census recorded, three times, for 1728 seconds.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import edit_tag_balance as etb  # noqa: E402

pytestmark = pytest.mark.critical


# The culprit, verbatim in shape: an 8-character anchor on the opening tag,
# replaced by a block that carries its own closing tag.
CENSUS_CULPRIT = {
    "find": "<script>",
    "replace": "<script>\n    const beep = new Audio();\n    function tick() {}\n</script>",
}
CENSUS_INNOCENT = {
    "find": '<button onclick="resetTimer()">Reset</button>',
    "replace": '<button onclick="resetTimer()">Reset</button>\n'
               '<button id="sound">Sound</button>',
}


def test_the_census_culprit_is_identified():
    rows = etb.unbalancing_edits([CENSUS_INNOCENT, CENSUS_CULPRIT])
    assert [r["edit_no"] for r in rows] == [2]
    # It leaves one closing tag too many, which is exactly what the census saw.
    assert rows[0]["delta"] == {"script": -1}


def test_the_innocent_edits_are_left_alone():
    """Naming every edit would be no better than naming none."""
    assert etb.unbalancing_edits([CENSUS_INNOCENT]) == []


def test_adding_a_whole_balanced_block_is_not_flagged():
    edit = {"find": "</body>",
            "replace": "<script>const x = 1;</script>\n</body>"}
    assert etb.edit_tag_delta(edit["find"], edit["replace"]) == {}


def test_deleting_a_whole_balanced_block_is_not_flagged():
    edit = {"find": "<style>body{}</style>", "replace": ""}
    assert etb.edit_tag_delta(edit["find"], edit["replace"]) == {}


def test_dropping_a_closing_tag_is_flagged_the_other_way():
    """The control arm's failure: anchored on </script>, gave back an opening
    one. The file went from 1/1 to 2/0."""
    delta = etb.edit_tag_delta("</script>", "<script>\n  more();\n")
    assert delta == {"script": 2}


def test_style_tags_count_too():
    delta = etb.edit_tag_delta("<style>", "<style>\n  body{}\n</style>")
    assert delta == {"style": -1}


def test_attributes_on_the_opening_tag_still_count():
    delta = etb.edit_tag_delta('<script src="a.js"></script>', "")
    assert delta == {}
    delta = etb.edit_tag_delta("</script>", '<script type="module">')
    assert delta == {"script": 2}


def test_closing_tags_with_whitespace_count():
    assert etb.edit_tag_delta("</script >", "") == {"script": 1}


def test_case_is_ignored():
    assert etb.edit_tag_delta("<SCRIPT>", "</Script>") == {"script": -2}


# ── the message ─────────────────────────────────────────────────────────────

def test_the_message_names_the_edit_and_its_anchor():
    msg = etb.describe([CENSUS_INNOCENT, CENSUS_CULPRIT])
    assert "edit 2" in msg
    assert "<script>" in msg


def test_the_message_says_what_to_do_instead():
    """A message that only restates the fault is what we already had."""
    msg = etb.describe([CENSUS_CULPRIT])
    assert "closing tag" in msg
    assert "anchor" in msg.lower()


def test_the_message_distinguishes_the_two_directions():
    too_many_closes = etb.describe([CENSUS_CULPRIT])
    too_many_opens = etb.describe([{"find": "</script>", "replace": "<script>"}])
    assert too_many_closes != too_many_opens
    assert "</script> with nothing opening it" in too_many_closes


def test_nothing_to_say_when_the_balance_is_fine():
    """Silence matters: the caller falls back to the generic parse error, and a
    confident wrong accusation would send the retry chasing the wrong edit."""
    assert etb.describe([CENSUS_INNOCENT]) == ""
    assert etb.describe([]) == ""
    assert etb.describe(None) == ""


def test_junk_edits_do_not_raise():
    assert etb.describe(["not a dict", None, 7]) == ""
    assert etb.unbalancing_edits([{"find": None, "replace": None}]) == []


def test_several_culprits_are_all_named():
    msg = etb.describe([CENSUS_CULPRIT, CENSUS_INNOCENT, CENSUS_CULPRIT])
    assert "edit 1" in msg and "edit 3" in msg
    assert "edit 2" not in msg


# ── the call site ───────────────────────────────────────────────────────────

def test_code_edit_appends_the_attribution_to_its_parse_error():
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "vera", "dag", "dag_workshop_capabilities.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "_edit_tag_balance" in src
    first_import = src.index("import edit_tag_balance as _edit_tag_balance")
    call = src.index("_edit_tag_balance.describe")
    assert call > first_import
    # Computing the attribution and then dropping it is the failure this whole
    # module exists to end - it must reach the error the retry is given.
    tail = src[call:call + 400]
    assert "last_err" in tail and "_culprit" in tail
    assert "last_err +=" in tail
