"""Code does not survive being posted through a JSON string.

Census 29, build-simple-code: three consecutive code.edit calls lost, each
reported as "the editor's reply was not the requested JSON object" on a reply
that was COMPLETE and well formed at both ends. Reproduced against the live
parser at the time:

    valid JSON, braces + ESCAPED quotes in a string  -> parses
    one UNESCAPED " inside a string value            -> total loss
    one raw newline inside a string value            -> total loss

The payload was a JS block with document.getElementById("clock") in it. So the
code moves out of the JSON (edit_blocks), and the JSON path stays for models
that already answer correctly.

Also here: the operator budget floor, and finding the file a goal names.

Pure: no LLM, no browser, no Redis.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import edit_blocks as EB               # noqa: E402
from vera.operator import operator_budget as OB      # noqa: E402
from vera.operator.goal_file import filename_in_goal  # noqa: E402

pytestmark = pytest.mark.critical


# -- the payload that could not be posted through JSON ----------------------

CENSUS_29 = """<<<EDIT>>>
<<<FIND>>>
let is24Hour = true;
<<<REPLACE>>>
let is24Hour = true;
const el = document.getElementById("clock");
const pomodoro = { state: 'stopped', remaining: 1500 };
<<<END>>>
<<<NOTE>>>
Added pomodoro state management.
"""


def test_the_payload_that_broke_json_survives_verbatim():
    r = EB.parse(CENSUS_29)
    assert r["error"] == ""
    assert len(r["edits"]) == 1
    rep = r["edits"][0]["replace"]
    assert 'document.getElementById("clock")' in rep, "unescaped quotes must survive"
    assert "{ state: 'stopped', remaining: 1500 }" in rep, "braces must survive"
    assert r["note"] == "Added pomodoro state management."


def test_find_is_exact_including_indentation():
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\n    if (x) {\n        go();\n    }\n"
                 "<<<REPLACE>>>\n    if (y) {\n        stop();\n    }\n<<<END>>>\n")
    assert r["edits"][0]["find"] == "    if (x) {\n        go();\n    }"
    assert r["edits"][0]["replace"] == "    if (y) {\n        stop();\n    }"


def test_several_edits_keep_their_order():
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\na\n<<<REPLACE>>>\nA\n<<<END>>>\n"
                 "<<<EDIT>>>\n<<<FIND>>>\nb\n<<<REPLACE>>>\nB\n<<<END>>>\n")
    assert [e["find"] for e in r["edits"]] == ["a", "b"]
    assert [e["replace"] for e in r["edits"]] == ["A", "B"]


def test_an_empty_replace_is_a_deletion_not_a_mistake():
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\ndrop me\n<<<REPLACE>>>\n<<<END>>>\n")
    assert r["edits"] == [{"find": "drop me", "replace": ""}]


def test_a_block_with_no_replace_section_is_skipped_not_guessed():
    """An edit whose replacement we invented is worse than one that did not
    happen."""
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\nonly a find\n<<<END>>>\n")
    assert r["edits"] == []
    assert "REPLACE" in r["error"]


def test_a_missing_final_END_still_yields_the_edit():
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\na\n<<<REPLACE>>>\nA\n")
    assert r["edits"] == [{"find": "a", "replace": "A"}]


def test_blocks_wrapped_in_a_fence_are_still_read():
    """Being strict about a fence is what started all this."""
    r = EB.parse("```\n<<<EDIT>>>\n<<<FIND>>>\na\n<<<REPLACE>>>\nA\n<<<END>>>\n```")
    assert r["edits"] == [{"find": "a", "replace": "A"}]


def test_a_marker_must_be_the_whole_line_not_merely_present():
    """Source containing the marker text inline must stay content."""
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\nx = '<<<END>>> inside a string'\n"
                 "<<<REPLACE>>>\ny = 1\n<<<END>>>\n")
    assert r["edits"][0]["find"] == "x = '<<<END>>> inside a string'"


def test_prose_around_the_blocks_is_ignored():
    r = EB.parse("Sure, here are the edits:\n<<<EDIT>>>\n<<<FIND>>>\na\n"
                 "<<<REPLACE>>>\nA\n<<<END>>>\nHope that helps!\n")
    assert r["edits"] == [{"find": "a", "replace": "A"}]


# -- routing between the two formats ----------------------------------------

def test_json_is_not_mistaken_for_blocks():
    """The JSON path must keep working - no regression for a model that is
    already answering correctly."""
    assert EB.looks_like_blocks('{"edits":[{"find":"a","replace":"b"}]}') is False


def test_prose_describing_the_format_is_not_mistaken_for_a_reply():
    assert EB.looks_like_blocks("Use the <<<EDIT>>> marker to start.") is False


def test_a_real_block_reply_is_recognised():
    assert EB.looks_like_blocks(CENSUS_29) is True


def test_the_instructions_and_the_parser_cannot_drift():
    """Every marker the prompt SHOWS must be one the parser accepts.

    One-directional on purpose: FIND is accepted but no longer advertised,
    because the model collapsed EDIT+FIND and showing both invited the reply
    that parsed as neither. Accepting more than we ask for is the point.
    """
    txt = EB.format_instructions()
    for marker in (EB.EDIT, EB.REPLACE, EB.END, EB.NOTE):
        assert marker in txt, "instructions show a marker set the parser drifted from"
    assert EB.looks_like_blocks(txt) is True
    # a reply following the shown format exactly must parse
    demo = (EB.EDIT + "\nold\n" + EB.REPLACE + "\nnew\n" + EB.END +
            "\n" + EB.NOTE + "\nwhy\n")
    got = EB.parse(demo)
    assert got["edits"] == [{"find": "old", "replace": "new"}]
    assert got["note"] == "why"


# -- the operator budget floor ----------------------------------------------

def test_a_model_supplied_budget_can_no_longer_starve_a_run():
    """Census 29: three runs stopped on 187s/200s against a 180s floor after
    9 and 5 steps. Every caller value observed has come from the model and
    every one has been too small."""
    assert OB.budget_kwargs(95) == {"max_seconds": float(OB.DEFAULT_MAX_SECONDS)}
    assert OB.budget_kwargs(180) == {"max_seconds": float(OB.DEFAULT_MAX_SECONDS)}


def test_a_caller_may_still_extend_the_budget():
    assert OB.budget_kwargs(900) == {"max_seconds": 900.0}


def test_no_preference_still_passes_nothing():
    assert OB.budget_kwargs(0) == {}
    assert OB.budget_kwargs(None) == {}


# -- the file a goal is talking about ---------------------------------------

def test_the_census_29_goal_names_its_file():
    assert filename_in_goal(
        "Observe the clock.html page loads, check if #clock div shows a time"
    ) == "clock.html"


def test_a_goal_that_names_no_file_is_left_alone():
    """Two of the three census-29 failures said 'the timer' and named nothing.
    This does not help those, and must not invent a target for them."""
    assert filename_in_goal("Observe the <div id='clock'></div> updating") is None
    assert filename_in_goal("Observe that the timer starts at 90:00") is None


def test_a_goal_carrying_a_full_url_is_left_alone():
    """Turning an absolute target into a relative guess would be a regression."""
    assert filename_in_goal(
        "Navigate to https://localhost:8999/remote/sandbox/preview/x/clock.html "
        "and confirm it ticks") is None


def test_a_non_web_file_is_not_opened_as_a_page():
    assert filename_in_goal("Run app.py and check the output") is None
    assert filename_in_goal("Read notes.txt and summarise") is None


def test_the_first_named_file_is_the_subject():
    assert filename_in_goal("Open index.html and confirm it links to about.html") \
        == "index.html"


def test_a_filename_is_not_matched_from_inside_a_longer_one():
    assert filename_in_goal("Check notmyclock.html renders") == "notmyclock.html"


def test_an_indented_marker_is_still_a_marker():
    """Models indent things. A marker is recognised by what the line SAYS once
    stripped, not by where it starts - while a marker inside a line of code
    stays content (see the whole-line test above)."""
    r = EB.parse("  <<<EDIT>>>\n    <<<FIND>>>\na\n   <<<REPLACE>>>\nA\n  <<<END>>>\n")
    assert r["edits"] == [{"find": "a", "replace": "A"}]


def test_a_trailing_fence_does_not_end_up_inside_the_note():
    """Where fence-stripping actually earns its place: after NOTE, the closing
    fence is INSIDE a section and would otherwise be captured as note text.
    (A leading fence is ignored anyway - it sits outside every section.)"""
    r = EB.parse("```\n<<<EDIT>>>\n<<<FIND>>>\na\n<<<REPLACE>>>\nA\n<<<END>>>\n"
                 "<<<NOTE>>>\nChanged a to A.\n```")
    assert r["note"] == "Changed a to A."


def test_a_url_plus_a_bare_filename_is_too_ambiguous_to_guess():
    """The negative lookbehind already stops a filename being pulled out of a
    path. This pins the OTHER case: a goal that carries a url AND names a
    separate file. Guessing which one is the target would be worse than
    leaving the caller's own target alone."""
    assert filename_in_goal(
        "Go to https://example.com/dashboard and then open clock.html") is None


def test_the_suffix_guard_holds_even_if_the_pattern_widens():
    """WEB_SUFFIXES is the single source of truth for 'worth opening in a
    browser'. A goal naming a script must never become a page load."""
    from vera.operator import goal_file as GF
    assert all(s.startswith(".") for s in GF.WEB_SUFFIXES)
    for bad in ("app.py", "notes.txt", "data.csv", "Makefile"):
        assert filename_in_goal("Check %s please" % bad) is None


# -- the wiring: code.edit must actually accept both formats ----------------
#
# The pure parser passing proves nothing about whether code.edit calls it. An
# earlier fix in this codebase validated a repaired payload and then executed
# the raw one; the lesson was to pin the wiring, not just the helper.

def test_code_edit_accepts_the_block_format():
    from vera.dag.dag_workshop_capabilities import _editor_obj_from_reply
    obj = _editor_obj_from_reply(CENSUS_29)
    assert obj.get("edits"), "blocks were not accepted by code.edit's parser"
    assert 'document.getElementById("clock")' in obj["edits"][0]["replace"]


def test_code_edit_still_accepts_plain_json():
    """No regression for a model that already answers correctly."""
    from vera.dag.dag_workshop_capabilities import _editor_obj_from_reply
    obj = _editor_obj_from_reply(
        '```json\n{"edits":[{"find":"a","replace":"b"}],"note":"n"}\n```')
    assert obj["edits"] == [{"find": "a", "replace": "b"}]
    assert obj.get("note") == "n"


def test_a_reply_that_is_neither_yields_nothing_rather_than_junk():
    from vera.dag.dag_workshop_capabilities import _editor_obj_from_reply
    assert _editor_obj_from_reply("I cannot do that.") == {}


def test_code_edit_is_wired_to_the_dual_format_parser():
    """The helper passing proves nothing if the cap stops calling it. Pins the
    CALL SITE, which a direct test of the helper cannot see."""
    import inspect
    from vera.dag import dag_workshop_capabilities as DW
    src = inspect.getsource(DW.cap_code_edit)
    assert "_editor_obj_from_reply(_raw_text)" in src
    assert "_extract_json(_raw_text)" not in src, \
        "code.edit went back to JSON-only parsing"


# -- what the real coder actually emits -------------------------------------
#
# Measured against the live coder 2026-09-03, with the first draft of the
# format. It ignored FIND entirely, used EDIT as the find delimiter, and
# copied the line-number gutter back despite being told not to. The format
# now follows the model instead of fighting it.

LIVE_REPLY = """<<<EDIT>>>
   98 | function setMode(newMode) {
   99 |     if (newMode !== mode) {
  100 |         resetTimer();
  101 |     }
  102 | }

<<<REPLACE>>>
   98 | function setMode(newMode) {
   99 |     if (newMode === mode) return;
  100 |     resetTimer();
  101 | }

<<<END>>>
"""


def test_the_real_coders_reply_parses():
    r = EB.parse(LIVE_REPLY)
    assert r["error"] == ""
    assert len(r["edits"]) == 1


def test_EDIT_works_as_the_find_delimiter():
    """The model never wrote FIND once - 'edit' then 'find' says the same
    thing twice, so it collapsed them."""
    r = EB.parse("<<<EDIT>>>\nold\n<<<REPLACE>>>\nnew\n<<<END>>>\n")
    assert r["edits"] == [{"find": "old", "replace": "new"}]


def test_an_explicit_FIND_after_EDIT_is_still_accepted():
    """Both spellings must work - callers and other models may use FIND."""
    r = EB.parse("<<<EDIT>>>\n<<<FIND>>>\nold\n<<<REPLACE>>>\nnew\n<<<END>>>\n")
    assert r["edits"] == [{"find": "old", "replace": "new"}]


def test_the_line_number_gutter_is_stripped():
    """The anchor must match the file EXACTLY, so a copied gutter is the
    difference between working and not."""
    r = EB.parse(LIVE_REPLY)
    assert r["edits"][0]["find"].startswith("function setMode(newMode) {")
    assert "|" not in r["edits"][0]["find"].splitlines()[0]
    assert r["edits"][0]["replace"].startswith("function setMode(newMode) {")


def test_a_gutter_is_only_stripped_when_the_block_is_uniformly_guttered():
    """One '12 | x' among ordinary lines is far more likely to be real content
    - a markdown table row, a shell pipe - than a gutter. Left alone, the
    anchor simply will not match, which fails safely."""
    r = EB.parse("<<<EDIT>>>\n  1 | a\nplain line\n<<<REPLACE>>>\nX\n<<<END>>>\n")
    assert r["edits"][0]["find"] == "  1 | a\nplain line"


def test_content_that_merely_contains_a_pipe_is_not_mangled():
    r = EB.parse("<<<EDIT>>>\ncat x | grep y\n<<<REPLACE>>>\ncat x | grep z\n<<<END>>>\n")
    assert r["edits"] == [{"find": "cat x | grep y", "replace": "cat x | grep z"}]


# -- placeholders ------------------------------------------------------------
#
# Second live measurement, 2026-09-03. Told the shape with angle-bracket
# placeholders, the coder returned "<the exact text to replace>" AS an edit's
# replacement. Applying that would have written the placeholder into the file.

def test_an_echoed_placeholder_is_never_applied():
    r = EB.parse("<<<EDIT>>>\nreal code\n<<<REPLACE>>>\n<the exact text to replace>\n"
                 "<<<END>>>\n")
    assert r["edits"] == []


def test_a_placeholder_as_the_anchor_is_also_refused():
    r = EB.parse("<<<EDIT>>>\n<the new text>\n<<<REPLACE>>>\nreal code\n<<<END>>>\n")
    assert r["edits"] == []


def test_real_code_in_angle_brackets_is_not_mistaken_for_a_placeholder():
    """HTML is angle brackets. A single tag on its own line is legitimate."""
    r = EB.parse("<<<EDIT>>>\n<div id=\"clock\"></div>\n<<<REPLACE>>>\n"
                 "<div id=\"clock\" class=\"big\"></div>\n<<<END>>>\n")
    assert len(r["edits"]) == 1
    assert r["edits"][0]["find"] == '<div id="clock"></div>'


def test_the_instructions_no_longer_offer_a_slot_to_echo():
    """The real fix for the echo - a worked example, not a placeholder."""
    txt = EB.format_instructions()
    assert "<the exact text to replace>" not in txt
    assert "<the new text>" not in txt
    assert "let is24Hour = true;" in txt, "the worked example is the point"


def test_a_multiline_block_is_not_treated_as_a_placeholder():
    r = EB.parse("<<<EDIT>>>\n<a>\n<b>\n<<<REPLACE>>>\n<c>\n<d>\n<<<END>>>\n")
    assert len(r["edits"]) == 1
