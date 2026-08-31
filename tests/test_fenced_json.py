"""A fenced reply must survive having its fence removed.

The malformed-inside-a-fence payload below is the shape the real coder produced
on 2026-08-31 (qwen2.5-coder:14b): it closes the edits array and then opens a
stray brace before "note". `json.loads` rejects it, so `_extract_json` falls past
its fenced-block regex into the strip - and the old strip handed the salvage
scanner an empty string.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.dag import editor_reply, fenced_json as fj  # noqa: E402

pytestmark = pytest.mark.critical


GOOD = '{"edits": [{"find": "a", "replace": "b"}], "note": "ok"}'
FENCED = "```json\n" + GOOD + "\n```"
# The real malformed shape: an extra brace after the edits array.
MALFORMED_BODY = '{\n  "edits": [\n    {"find": "a", "replace": "b"}]\n  },\n  "note": "x"\n}'
FENCED_MALFORMED = "```json\n" + MALFORMED_BODY + "\n```"


# ── the bug ─────────────────────────────────────────────────────────────────

def test_a_complete_fence_is_not_reduced_to_nothing():
    """split("```",2)[-1] returns the text AFTER the close, i.e. "" - which is
    what starved every downstream recovery path."""
    assert FENCED.split("```", 2)[-1].strip() == ""      # the old behaviour
    assert fj.strip_fence(FENCED) == GOOD                # the new one


def test_a_malformed_fenced_reply_still_yields_its_body():
    """This is the case that mattered: the fenced-block regex cannot parse it,
    so what the strip returns is the only thing left to salvage from."""
    body = fj.strip_fence(FENCED_MALFORMED)
    assert body == MALFORMED_BODY
    assert '"edits"' in body and '"note"' in body
    with pytest.raises(ValueError):
        json.loads(body)                                  # still malformed - but present


def test_the_salvage_scanner_can_now_find_an_object():
    """The point of keeping the body: a balanced-object scan finds a real,
    parseable object in it. On the empty string it finds nothing, which is what
    turned a partially-recoverable reply into a total loss."""
    def balanced(s):
        out, depth, start = [], 0, None
        for i, ch in enumerate(s):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0:
                    out.append(s[start:i + 1])
        return out

    salvaged = [json.loads(o) for o in balanced(fj.strip_fence(FENCED_MALFORMED))
                if _parses(o)]
    assert salvaged, "nothing recoverable from the fenced body"
    # What comes back is the whole usable edits object - the stray brace that
    # broke json.loads sits after it, so the salvage keeps the part that matters.
    assert {"edits": [{"find": "a", "replace": "b"}]} in salvaged
    # the old strip left nothing to scan at all
    assert balanced(FENCED_MALFORMED.split("```", 2)[-1].strip()) == []


def _parses(s):
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


# ── fence forms ─────────────────────────────────────────────────────────────

def test_an_unterminated_fence_keeps_everything_after_the_opener():
    """A reply truncated mid-fence still has everything worth parsing."""
    assert fj.strip_fence("```json\n" + GOOD) == GOOD


def test_the_language_tag_is_removed():
    """'json{...}' does not parse; the tag has to go with the fence."""
    for tag in ("json", "JSON", "js", ""):
        assert fj.strip_fence("```" + tag + "\n" + GOOD + "\n```") == GOOD


def test_a_bare_fence_with_no_newline_after_the_tag():
    assert fj.strip_fence("```json " + GOOD + "\n```") == GOOD


def test_unfenced_text_is_returned_unchanged():
    assert fj.strip_fence(GOOD) == GOOD
    assert fj.strip_fence("  " + GOOD + "  ") == GOOD


def test_a_fence_that_does_not_start_the_reply_is_left_alone():
    """Prose then a fence is the balanced-scanner's job, not this one - and
    eating the prose could remove the only parseable object."""
    s = "Here are the edits:\n```json\n" + GOOD + "\n```"
    assert fj.strip_fence(s) == s


def test_only_the_LAST_close_ends_the_body():
    """A reply containing a fenced snippet inside its JSON must not be cut at
    the first inner close."""
    inner = '{"edits": [{"find": "a", "replace": "```py\\nx=1\\n```"}]}'
    assert fj.strip_fence("```json\n" + inner + "\n```") == inner


def test_looks_fenced_agrees_with_strip_fence():
    for s in (FENCED, "```\n" + GOOD, GOOD, "", "  "):
        changed = fj.strip_fence(s) != str(s or "").strip()
        assert fj.looks_fenced(s) or not changed


def test_junk_does_not_raise():
    for bad in (None, "", "   ", "```", "```json"):
        assert isinstance(fj.strip_fence(bad), str)


# ── the error that has to be readable next time ─────────────────────────────

def test_the_unparseable_error_quotes_both_ends():
    """60 leading characters could not tell a fence problem from malformed
    JSON: run 21 quoted an opening that looked perfectly well-formed while the
    stray brace sat near the end."""
    long_reply = "```json\n" + ('{"edits": [' + '{"find": "x", "replace": "y"},' * 20) + "}]\n},\n\"note\": \"x\"\n```"
    err = editor_reply.classify({}, long_reply)["error"]
    assert "began" in err and "ended" in err
    assert '"note"' in err                       # the tail, where the fault was


def test_a_short_reply_is_quoted_whole():
    err = editor_reply.classify({}, "nope")["error"]
    assert "it was 'nope'" in err


def test_no_reply_means_no_quote():
    err = editor_reply.classify({}, "   ")["error"]
    assert "began" not in err and "it was" not in err


# ── the call site ───────────────────────────────────────────────────────────

def test_extract_json_uses_the_shared_strip():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "vera", "dag", "dag_workshop_capabilities.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("def _extract_json("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_fenced_json.strip_fence(s)" in body
    # the old expression may remain ONLY as the import-failure fallback
    assert body.count('split("```", 2)[-1]') <= 1
    assert src.index("import fenced_json as _fenced_json") < src.index("def _extract_json(")
