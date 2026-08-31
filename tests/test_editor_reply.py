"""An editor that declines has told you something.

`code.edit` collapsed three different outcomes into "the editor returned no
edits" and retried all three up to three times.

Measured 2026-08-31 against the real prompt and model (qwen2.5-coder:14b, temp
0.7), task "Make the timer better." on a 58-line file: SIX of eight identical
runs through the cap failed that way. The raw reply for one:

    {"edits": [], "note": "No specific changes requested for making the timer
     better."}

eval_count=28, done=stop, well-formed JSON. A considered decline, written into
the field the prompt asked for - then discarded and re-run.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.dag.editor_reply import (          # noqa: E402
    DECLINED, EDITS, MALFORMED, UNPARSEABLE, classify,
)

# The literal reply captured from the model on 2026-08-31.
REAL_DECLINE = {"edits": [], "note": "No specific changes requested for making the timer better."}


# --- the measured case ------------------------------------------------------

def test_the_real_decline_is_recognised_and_carries_its_reason():
    v = classify(REAL_DECLINE)
    assert v["kind"] == DECLINED
    assert "No specific changes requested" in v["error"]
    assert v["note"] == REAL_DECLINE["note"]


def test_a_decline_is_not_retried():
    """Asking the identical question again is a repetition, not a retry - and
    it costs a generation each time."""
    assert classify(REAL_DECLINE)["retry_worthwhile"] is False


def test_the_decline_error_says_what_to_do_instead():
    v = classify(REAL_DECLINE)
    assert "specific" in v["error"].lower()


# --- the cases that ARE worth retrying --------------------------------------

def test_unparseable_output_is_still_retried():
    """There the model may simply have fumbled the JSON."""
    v = classify({}, raw_text="Sure! Here are some improvements you could make:")
    assert v["kind"] == UNPARSEABLE and v["retry_worthwhile"] is True
    assert "Sure! Here are some" in v["error"]


def test_a_json_object_without_an_edits_list_is_retried():
    v = classify({"note": "I changed the colours"})
    assert v["kind"] == MALFORMED and v["retry_worthwhile"] is True


def test_edits_of_the_wrong_type_is_malformed_not_a_decline():
    v = classify({"edits": "a string"})
    assert v["kind"] == MALFORMED and v["retry_worthwhile"] is True


# --- the happy path is untouched -------------------------------------------

def test_real_edits_pass_through_unchanged():
    edits = [{"find": "a", "replace": "b"}, {"find": "c", "replace": "d"}]
    v = classify({"edits": edits, "note": "renamed two things"})
    assert v["kind"] == EDITS
    assert v["edits"] == edits
    assert v["error"] == ""
    assert v["retry_worthwhile"] is False


# --- shape ------------------------------------------------------------------

def test_an_empty_decline_without_a_note_still_explains_itself():
    v = classify({"edits": []})
    assert v["kind"] == DECLINED
    assert "too vague" in v["error"] or "no reason" in v["error"]
    assert v["retry_worthwhile"] is False


@pytest.mark.parametrize("junk", [None, "", 0, [], "not a dict"])
def test_junk_is_unparseable_not_a_crash(junk):
    v = classify(junk)
    assert v["kind"] == UNPARSEABLE and v["edits"] == []


# --- the callsite -----------------------------------------------------------

def _src():
    p = os.path.join(os.path.dirname(__file__), "..", "vera", "dag",
                     "dag_workshop_capabilities.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_code_edit_uses_the_classifier_and_stops_retrying_a_decline():
    src = _src()
    assert "_editor_reply.classify(" in src
    assert 'if not _verdict["retry_worthwhile"]:' in src
    imp = src.index("import editor_reply as _editor_reply")
    use = src.index("_editor_reply.classify(")
    assert imp < use


def test_the_old_contentless_message_is_no_longer_assigned():
    """It told the loop nothing at all. Matched as the ASSIGNMENT, not as any
    mention - the commit comment quotes the old string deliberately, to record
    what was replaced."""
    src = _src()
    assert 'last_err = "the editor returned no edits"' not in src
    assert "the editor returned no edits" in src, (
        "the old wording should survive in the comment as a record")


def test_the_decline_is_surfaced_to_the_caller():
    src = _src()
    assert '"declined": True' in src
