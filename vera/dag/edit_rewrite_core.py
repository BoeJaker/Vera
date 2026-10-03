"""When anchored edits keep missing, a small file is revised whole - and checked.

Prod 30 Sep - 3 Oct: code.edit failure parts were mostly anchors the editor RETYPED
instead of copying - paraphrased middle lines, an old value after an earlier edit had
changed it (`formatTime(60)` in a file that says `formatTime(90)`), `typing.List` for
`list` - 17 "not present" plus 19 "matches several places". The hint already shows the
closest real lines; three attempts still miss, and the step loses minutes (one failed call
ran 2224 s). For a small file the robust move is to stop asking for anchors and ask for
the complete revised file once - accepted only if it keeps most of the original and stays
about the same size, so a rewrite can neither silently drop the file nor invent a new one.
Pure.
"""

from __future__ import annotations

from typing import Iterable, Tuple

#: Files larger than this are never rewritten whole.
MAX_CHARS = 16000
MIN_SIZE_RATIO = 0.5
MAX_SIZE_RATIO = 2.0
#: Share of the original's non-blank lines the revision must keep verbatim (stripped).
MIN_KEPT = 0.5


def anchor_failure(errors: Iterable[str]) -> bool:
    """True when every error is an anchor miss (absent or ambiguous) - the case a whole-file
    revision fixes. Anything else (a parse failure, a malformed reply) is not."""
    errs = [str(e) for e in errors or [] if str(e).strip()]
    return bool(errs) and all(("not present" in e) or ("matches" in e and "places" in e) for e in errs)


def eligible(current: str, errors: Iterable[str]) -> bool:
    return bool(current.strip()) and len(current) <= MAX_CHARS and anchor_failure(errors)


def accept(before: str, after: str) -> Tuple[bool, str]:
    """(ok, reason) for a whole-file revision of `before`."""
    if not after.strip():
        return False, "the revision was empty"
    if after == before:
        return False, "the revision changed nothing"
    r = len(after) / max(1, len(before))
    if r < MIN_SIZE_RATIO or r > MAX_SIZE_RATIO:
        return False, "the revision is %.0f%% of the original's size" % (r * 100)
    orig = [ln.strip() for ln in before.splitlines() if ln.strip()]
    if len(orig) >= 6:
        have = {ln.strip() for ln in after.splitlines()}
        kept = sum(1 for ln in orig if ln in have) / len(orig)
        if kept < MIN_KEPT:
            return False, "the revision kept only %.0f%% of the original lines" % (kept * 100)
    return True, ""


def prompt(path: str, lang: str, current: str, task: str, last_err: str) -> str:
    return ("FILE: %s (%s, %d lines)\nCURRENT CONTENT (exactly as it is now):\n%s\n\n"
            "CHANGE REQUESTED:\n%s\n\n"
            "Anchored edits for this change failed three times (%s). Instead, return the "
            "COMPLETE revised file: every line of the current content, with ONLY the requested "
            "change made. Do not reformat, reorder or 'improve' anything else.\n"
            "Output ONE fenced block, opened exactly ```%s file=%s, and nothing else."
            % (path, lang, len(current.splitlines()), current, task,
               " ".join(str(last_err).split())[:300], lang, path))
