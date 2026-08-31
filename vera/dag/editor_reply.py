"""An editor that declines has told you something. Do not call it "no edits".

`code.edit` asks the model for
    {"edits":[...], "note":"<one line on what you changed>"}
and then reduces every non-useful answer to one string:

    if not isinstance(edits, list) or not edits:
        last_err = "the editor returned no edits"

Three genuinely different outcomes collapse into that sentence, and all three
are retried up to three times against essentially the same prompt.

Measured 2026-08-31 against the real prompt and model (qwen2.5-coder:14b,
temp 0.7), task "Make the timer better." on a 58-line file - through the cap,
SIX of eight identical runs failed this way. The raw reply for one of them:

    {"edits": [], "note": "No specific changes requested for making the timer
     better."}

eval_count=28, done=stop, well-formed JSON. The model did not malfunction: it
made a considered decision that the request was too vague to act on, and wrote
the reason into the very field the prompt asked it to fill. That reason was
then thrown away and replaced with "the editor returned no edits" - which tells
the loop nothing, and sends it round again.

This is the same failure as O21 (a silent success read as no information) and
O23 (a populated `reason` never read): the explanation exists and nobody looks
at it.

So: separate the three cases, carry the model's own words, and do not re-run a
DECLINE. A decline is an answer; asking the identical question again is not a
retry, it is a repetition. Unparseable output is a different matter and is
still worth retrying, because there the model may simply have fumbled the JSON.

Pure: dict in, verdict out. No I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: The model answered, in the requested shape, that it is making no changes.
DECLINED = "declined"
#: The reply could not be read as the requested JSON object at all.
UNPARSEABLE = "unparseable"
#: A JSON object, but without a usable `edits` list.
MALFORMED = "malformed"
#: Usable edits.
EDITS = "edits"


def _quote_reply(raw_text: str, head: int = 160, tail: int = 80) -> str:
    """Both ENDS of the reply. 60 leading characters could not tell a fence
    problem from malformed JSON - run 21's failure quoted an opening that
    looked perfectly well-formed, and the fault (a stray brace before "note")
    was near the end where nobody could see it."""
    s = str(raw_text or "").strip()
    if len(s) <= head + tail + 20:
        return f"it was {s!r}"
    return f"it began {s[:head]!r} and ended {s[-tail:]!r}"


def classify(obj: Any, raw_text: str = "") -> Dict[str, Any]:
    """What the editor actually said.

    Returns ``{kind, edits, note, retry_worthwhile, error}``.
    """
    if not isinstance(obj, dict) or not obj:
        return {"kind": UNPARSEABLE, "edits": [], "note": "",
                "retry_worthwhile": True,
                "error": ("the editor's reply was not the requested JSON object"
                          + (f" ({_quote_reply(raw_text)})" if raw_text.strip() else ""))}

    note = str(obj.get("note") or "").strip()
    edits = obj.get("edits")

    if isinstance(edits, list) and edits:
        return {"kind": EDITS, "edits": edits, "note": note,
                "retry_worthwhile": False, "error": ""}

    if isinstance(edits, list):          # explicitly empty - a decision
        if note:
            return {"kind": DECLINED, "edits": [], "note": note,
                    "retry_worthwhile": False,
                    "error": (f"the editor made no changes and said why: {note} "
                              "Re-run it with a SPECIFIC change (what to alter, "
                              "and where) rather than a general improvement.")}
        return {"kind": DECLINED, "edits": [], "note": "",
                "retry_worthwhile": False,
                "error": ("the editor returned an empty edit list and gave no "
                          "reason - the request may be too vague to act on. "
                          "Re-run it naming a specific change.")}

    return {"kind": MALFORMED, "edits": [], "note": note,
            "retry_worthwhile": True,
            "error": ("the editor's reply had no usable `edits` list"
                      + (f" (note: {note})" if note else ""))}
