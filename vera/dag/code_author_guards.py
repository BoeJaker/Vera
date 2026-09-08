"""Pure guards for `code.author`'s automatic repair loops.

Kept out of `dag_workshop_capabilities.py` (~24k lines, imports the whole app) so
the decision can be unit-tested directly — the pure-core pattern used by
`board_core.py` / `ollama_gate.py`.

THE INCIDENT (2026-08-24, v7 habit-tracker run e2da05e9). `code.author` generated
an HTML page that never closed (`missing </html>`). Its syntax-repair loop asked
the editor model for "the smallest possible fix"; the edits it applied instead
DELETED the unfinished body, leaving a 97-byte
`<html><head></head><body></body></html>`. That parses perfectly, so every check
downstream passed and the cap returned `ok:true, syntax_ok:true, truncated:false`
on what is an empty file. The loop then burned two more cycles discovering the
file was a stub.

The rule: an automatic repair must fix the error the checker named, never satisfy
the parser by removing content. Deleting most of the file is therefore a REJECTED
edit, not a successful repair. This applies only to the automatic repair paths,
where "smallest possible fix" is the stated contract — NOT to `code.edit` or
`prose.author`'s ungrounded-reference removal, where shrinking the file is the
caller's actual intent.
"""

import os
from typing import Any

# A repair that shrinks the file below this fraction of its pre-repair size is a
# collapse. A real minimal fix (adding `</html>`, closing a brace) only ever grows
# the file or leaves it near-identical.
COLLAPSE_RATIO = float(os.getenv("VERA_CODE_AUTHOR_REPAIR_COLLAPSE_RATIO", "0.5") or 0.5)

# Below this size the ratio is meaningless — a genuinely tiny file can legitimately
# change size a lot, and we must not block a real fix to a short file.
COLLAPSE_MIN_BEFORE = int(os.getenv("VERA_CODE_AUTHOR_REPAIR_COLLAPSE_MIN", "400") or 400)


def repair_collapsed(before: str, after: str,
                     ratio: float = None, min_before: int = None) -> bool:
    """True when one applied repair edit deleted most of the file.

    `before`/`after` are the file contents either side of the edit. Whitespace-only
    differences never count: both sides are stripped before measuring.
    """
    _r = COLLAPSE_RATIO if ratio is None else ratio
    _m = COLLAPSE_MIN_BEFORE if min_before is None else min_before
    b = len((before or "").strip())
    a = len((after or "").strip())
    if b < _m:
        return False
    return a < int(b * _r)


def repaired_note(passes: Any) -> str:
    """What to tell the caller when the file changed after it was generated.

    Census 40, author-then-edit. `code.edit` anchored on

        let countdown = 60 * 60;

    and the file on disk held

        countdown = 60 * 60;

    which is exactly the edit a repair pass makes for "Identifier 'countdown'
    has already been declared". code.author returns `path`, `bytes`,
    `syntax_ok`, `checked_with` - and NEVER the saved content - so the model had
    no way to know the file had changed under it. Worse, the same note tells it
    "do NOT read it back", which is right for verification and wrong for
    anchoring: it was instructed into editing from memory.

    So the note is conditional on a repair having actually happened. Saying it
    every time would be the same mistake in reverse - telling every caller to
    re-read a file that is byte-identical to what it just wrote is what the
    do-not-read-it-back clause exists to prevent.
    """
    try:
        n = int(passes or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return ("\u26a0 THIS FILE WAS REPAIRED after you generated it (%d pass(es)), so "
            "what is on disk is NOT what you wrote. Before any code.edit on it, read "
            "it and anchor `find` on text COPIED FROM THE FILE - an anchor typed from "
            "memory will not match. " % n)
