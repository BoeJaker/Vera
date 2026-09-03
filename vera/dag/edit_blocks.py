"""Edit instructions where the CODE is not inside a JSON string.

code.edit asked the editor for::

    {"edits":[{"find":"<exact text>","replace":"<new text>"}], "note":"..."}

which requires every quote, backslash and newline of a code payload to survive
JSON escaping. Census 29, build-simple-code: three consecutive code.edit calls
failed with "the editor's reply was not the requested JSON object" on replies
that were COMPLETE and well formed at both ends - they opened with the fence and
closed with the note and a closing brace. The fault was in the middle, and it
reproduces exactly against the live parser:

    valid JSON, braces + ESCAPED quotes in a string  -> parses
    one UNESCAPED " inside a string value            -> total loss
    one raw newline inside a string value            -> total loss

The replacement text was a JS block containing document.getElementById("clock")
and a `const pomodoro = {` literal. Asking a model to hand-escape that, inside a
string, for hundreds of characters, is asking for the one thing models are worst
at - and a single missed backslash discards the whole edit. Three attempts,
three generations, the goal's whole budget.

So the code moves OUT of the JSON. Delimiters carry it verbatim:

    <<<EDIT>>>
    <<<FIND>>>
    let is24Hour = true;
    <<<REPLACE>>>
    let is24Hour = true;
    const el = document.getElementById("clock");
    <<<END>>>
    <<<NOTE>>>
    Added the clock element lookup.

Nothing inside a block needs escaping, because nothing inside a block is parsed
- it is copied. The only text that can break it is a line that is itself a
delimiter, which is why the delimiters look like nothing that occurs in source.

This is a SECOND accepted format, not a replacement. The JSON path stays and is
still tried, because a model that already answers correctly must not be broken
by a change meant to help one that does not.

Pure: no I/O, no imports from the app.
"""

from __future__ import annotations

import re

from typing import Any, Dict, List

#: Block delimiters. Long, bracketed and upper case so they cannot collide with
#: real source: a line that EQUALS one of these is a marker; a line that merely
#: contains one is content.
EDIT = "<<<EDIT>>>"
FIND = "<<<FIND>>>"
REPLACE = "<<<REPLACE>>>"
END = "<<<END>>>"
NOTE = "<<<NOTE>>>"


def looks_like_blocks(text: str) -> bool:
    """Whether `text` is worth handing to :func:`parse`.

    Deliberately strict - an EDIT marker alone is not enough, or a model that
    merely DESCRIBED the format in prose would be routed here. A real reply
    has a FIND and a REPLACE too.
    """
    s = str(text or "")
    return (EDIT in s or FIND in s) and REPLACE in s


#: A line-number gutter as _v5_numbered emits it: "  98 | code".
_GUTTER_RE = re.compile(r"^\s*\d+\s*\|\s?")


def _strip_gutter(lines: List[str]) -> List[str]:
    """Remove a line-number gutter the model copied back.

    The prompt says not to include it. Measured against the real coder
    2026-09-03: it included it in every block anyway. Since the anchor has to
    match the file EXACTLY, a gutter makes every edit fail - so this is not a
    politeness, it is the difference between working and not.

    Only stripped when the block is UNIFORMLY guttered (every non-blank line),
    because a single "12 | x" among ordinary lines is far more likely to be
    real content - a markdown table row, a shell pipe - than a gutter.
    """
    real = [l for l in lines if l.strip()]
    if not real or not all(_GUTTER_RE.match(l) for l in real):
        return lines
    return [_GUTTER_RE.sub("", l) if l.strip() else l for l in lines]


#: Text from the INSTRUCTIONS that a model sometimes copies instead of filling
#: in. Measured 2026-09-03: given "<the exact text to replace>" as a
#: placeholder, the coder returned that exact string as an edit's replacement.
#: An edit built from a placeholder would write the placeholder into the file,
#: so it is dropped rather than applied - and the instructions now show a
#: worked example instead of angle brackets, which is the real fix.
_PLACEHOLDER_RE = re.compile(r"^<[^>\n]{3,60}>$")


def _is_placeholder(text: str) -> bool:
    """Whether a block's content is instruction boilerplate rather than code."""
    stripped = str(text or "").strip()
    return bool(_PLACEHOLDER_RE.match(stripped))


def _strip_fence_lines(lines: List[str]) -> List[str]:
    """Drop a wrapping code fence.

    A model told to emit blocks will often wrap them in a fence anyway. The
    blocks are perfectly readable inside one, so this is not worth failing over
    - which was the entire lesson of the JSON path.
    """
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    # Only the TRAILING fence matters. A leading fence line sits outside every
    # section and is ignored by the parser anyway, so stripping it changed
    # nothing that any test could tell apart - dead code, removed rather than
    # left to look load-bearing. A trailing fence is different: it arrives
    # after NOTE, inside a section, and would be captured as note text.
    if out and out[-1].strip().startswith("```"):
        out.pop()
    return out


def parse(text: str) -> Dict[str, Any]:
    """Parse delimited edit blocks. Returns ``{edits, note, error}``.

    ``edits`` is a list of ``{"find": ..., "replace": ...}`` in the order given,
    which is exactly what _v5_apply_edits already consumes - so this is a new
    way to SAY the same thing, not a new thing to say.

    A block with no REPLACE section is skipped rather than guessed at: an edit
    whose replacement we invented is worse than an edit that did not happen. An
    EMPTY replace is legitimate - it is how a deletion is expressed - so
    "absent" is tracked separately from "empty".
    """
    lines = _strip_fence_lines(str(text or "").splitlines())
    edits: List[Dict[str, str]] = []
    note_lines: List[str] = []

    section = None
    find_buf: List[str] = []
    repl_buf: List[str] = []
    saw_replace = False

    def flush():
        nonlocal find_buf, repl_buf, saw_replace
        find_text = "\n".join(_strip_gutter(find_buf))
        repl_text = "\n".join(_strip_gutter(repl_buf))
        if (find_text.strip() and saw_replace
                and not _is_placeholder(find_text) and not _is_placeholder(repl_text)):
            edits.append({"find": find_text, "replace": repl_text})
        find_buf, repl_buf, saw_replace = [], [], False

    for raw in lines:
        marker = raw.strip()
        if marker == EDIT:
            # EDIT opens a block. The model uses it AS the find delimiter -
            # measured 2026-09-03, it emitted EDIT / REPLACE / END and never
            # once wrote FIND, which is fair: "edit" then "find" back to back
            # says the same thing twice. So EDIT starts the find section, and
            # an explicit FIND after it simply re-opens the same section.
            flush()
            section = "find"
            continue
        if marker == FIND:
            section = "find"
            continue
        if marker == REPLACE:
            section = "replace"
            saw_replace = True
            continue
        if marker == END:
            flush()
            section = None
            continue
        if marker == NOTE:
            flush()
            section = "note"
            continue
        if section == "find":
            find_buf.append(raw)
        elif section == "replace":
            repl_buf.append(raw)
        elif section == "note":
            note_lines.append(raw)
        # anything outside a section is prose the model added - ignored

    flush()                                # a reply that omitted its final END

    note = "\n".join(note_lines).strip()
    if not edits:
        return {"edits": [], "note": note,
                "error": ("edit blocks were present but none was complete - each "
                          "needs a " + FIND + " section and a " + REPLACE +
                          " section")}
    return {"edits": edits, "note": note, "error": ""}


def format_instructions() -> str:
    """The block format, as shown to the editor.

    THREE markers, not four. The first draft had EDIT and FIND as separate
    delimiters; measured against the real coder, it collapsed them and wrote
    EDIT / REPLACE / END every time - the sensible reading - so the format
    follows the model rather than fighting it. FIND is still accepted.

    A WORKED EXAMPLE, not angle-bracket placeholders. The draft that said
    "<the exact text to replace>" got exactly that string back as an edit's
    replacement: a placeholder that looks like a slot invites being echoed,
    while a line of real code invites being replaced with other real code.

    Kept next to the parser so the two cannot drift - the defect this module
    exists for was, at bottom, a prompt and a parser disagreeing about a format.
    """
    return (
        "Return your edits as DELIMITED BLOCKS. Do NOT use JSON. Do NOT escape\n"
        "anything - text between the markers is copied verbatim. Example:\n"
        "\n"
        + EDIT + "\n"
        "let is24Hour = true;\n"
        + REPLACE + "\n"
        "let is24Hour = false;\n"
        + END + "\n"
        + NOTE + "\n"
        "Defaulted the clock to 12-hour format.\n"
        "\n"
        "That is an EXAMPLE of the shape. Put the real text from the file in\n"
        "the first section and your real replacement in the second - never\n"
        "copy the example, and never write a description of the text instead\n"
        "of the text. Repeat the block for each edit; end with one " + NOTE + ".\n"
        "Do NOT include the line-number gutter.\n"
        "Never write a marker line inside the text you are replacing.")
