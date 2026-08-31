"""Take the code fence off a model's reply without taking the reply with it.

`_extract_json` tries a fenced block first, and when that candidate does not
parse it falls back to

    if s.startswith("```"):
        s = s.split("```", 2)[-1].strip()

For a COMPLETE fence that is the text AFTER the closing fence - almost always
the empty string:

    "```json\\n{...}\\n```".split("```", 2)  ->  ['', 'json\\n{...}\\n', '']
                                                                     ^^ [-1]

So the moment a fenced reply's JSON is even slightly malformed, every remaining
recovery path in `_extract_json` - the whole-payload parse, the balanced-object
scanner, the first-brace-to-last-brace last ditch - is handed "" and the reply
is a total loss. The scanner exists precisely to salvage this case and never
gets the chance.

That is reachable, and it was reached. Census run 21, build-browser-verified
step 2:

    code.edit FAILED - the editor's reply was not the requested JSON object
    (it began '```json\\n{\\n  "edits": [\\n    {\\n      "find": "<script>",\\n     ')

a reply whose visible opening is well-formed. The malformed-JSON-inside-a-fence
shape is not hypothetical either: measured the same day against the real coder,
one reply closed its edits array and then opened a stray brace before "note",

    ..."replace":""}]\\n  },\\n  "note": "..."

which fails `json.loads` on the fenced candidate and lands in exactly this path.

The fence regex above this fallback is fine and is left alone: `\\{.*?\\}` looks
too lazy for a nested object, but the trailing ```` \\s*``` ```` forces it out to
the last brace before the closing fence, so nesting survives. I checked that
before changing anything, because "obviously wrong" regexes usually aren't.
"""

from __future__ import annotations

import re

# ```json / ```JSON / ```  - the opener, with an optional language tag.
_OPEN = re.compile(r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n?")


def strip_fence(text: str) -> str:
    """The content INSIDE a leading code fence, or `text` unchanged.

    Handles the unterminated case too - a reply cut off mid-fence still has
    everything worth parsing before the missing close.
    """
    s = str(text or "").strip()
    m = _OPEN.match(s)
    if not m:
        return s
    body = s[m.end():]
    close = body.rfind("```")
    return (body[:close] if close != -1 else body).strip()


def looks_fenced(text: str) -> bool:
    """Whether `strip_fence` would do anything."""
    return bool(_OPEN.match(str(text or "").strip()))
