"""missing_path_hint.py -- when a run names a file that is not there, say what is.

Census 36, research-web. Step 6 wrote a script and then spent four cycles trying
to run files that never existed:

    exec.python.run  python3: can't open file '/workspace/inspect_and_extract_browsers.py'
    exec.python.run  python3: can't open file '/workspace/inspect_and_extract_browsers.py'
    code.edit        132s, editing that same missing file, no changes made
    exec.python.run  python3: can't open file '/workspace/extract_browser_support_details_fixed.py'

The workspace contained exactly one script the whole time:

    extract_browser_support_details_from_mdn.py

So code.author wrote one name and the executor invented two others. The run was
cancelled at the wall cap having never reached step 4, the step that writes the
summary the goal actually asked for.

exec_capabilities.artifact_list_files already exists for this, and its docstring
names the failure precisely: "the invented-path failure mode is a model filling a
gap in its knowledge of what actually exists". The agentic loop already puts that
listing in the specialist's prompt UP FRONT (_v5_workdir_files) - and this run
shows that is not enough on its own. A listing offered before the mistake is easy
to skim past; the same listing attached TO the failure arrives exactly when the
model is wrong and is about to retry.

The second invented name is one word different from the real one
(..._fixed.py vs ..._from_mdn.py), so the near-miss is worth calling out
explicitly rather than leaving it to be spotted in a list.

Pure: the caller supplies the listing. No I/O, no container, no clock.
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Any, Dict, List, Optional, Sequence

#: Interpreter/shell phrasings for "that path is not there". Matched
#: case-insensitively against stderr and error text.
MISSING_MARKERS = (
    "no such file or directory",
    "can't open file",
    "cannot open file",
    "cannot find the file",
    "not found",
)

#: The path, anchored on the WORD that introduces it.
#:
#: A plain "quoted token" regex cannot be used here, and the reason is the
#: message this module exists for: python3 says
#:
#:     python3: can't open file '/workspace/x.py': [Errno 2] ...
#:
#: and the apostrophe in "can't" is the first quote in the string, so a
#: non-overlapping quoted-token match pairs it with the path's OPENING quote and
#: captures "t open file " - swallowing the quote the path needed. Anchoring on
#: file/directory/path steps over every contraction in the prose before it.
_NAMED = re.compile(r"(?:file|directory|path)\s*:?\s+['\"]([^'\"\n]{1,400})['\"]", re.I)

#: Fallback for messages that give the path unquoted. Runs to whitespace or a
#: quote, so a trailing "': [Errno 2]" is not dragged in.
_PATHY = re.compile(r"(?:^|[\s'\"(])((?:/|\./|\.\./)[^\s'\"()]{1,400})")

#: How similar an existing name must be before it is offered as "did you mean".
#: 0.6 is difflib's own default cut-off; the observed near-miss
#: (extract_browser_support_details_fixed.py vs ..._from_mdn.py) scores far above it.
CLOSE_ENOUGH = 0.6


def _text_of(result: Any) -> str:
    """Everything a failure might have written its message into."""
    if not isinstance(result, dict):
        return str(result or "")
    parts = [str(result.get(k) or "") for k in ("stderr", "error", "stdout")]
    return "\n".join(p for p in parts if p)


def looks_missing(text: str) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in MISSING_MARKERS)


def missing_path(text: str) -> str:
    """The path the message complained about, or "".

    Prefers a quoted token that looks like a path; interpreters quote it and
    that is far more reliable than parsing the sentence around it.
    """
    s = str(text or "")
    if not looks_missing(s):
        return ""
    for rx in (_NAMED, _PATHY):
        for m in rx.finditer(s):
            cand = m.group(1).strip().rstrip(":,")
            if not cand:
                continue
            # Must look like a file rather than a fragment of the sentence.
            if "/" in cand or "\\" in cand or re.search(r"\.[A-Za-z0-9]{1,8}$", cand):
                return cand
    return ""


def did_you_mean(path: str, names: Sequence[str]) -> str:
    """The existing name closest to the one that was asked for, or ""."""
    base = os.path.basename(str(path or "").rstrip("/"))
    if not base or not names:
        return ""
    pool = [str(n).rstrip("/") for n in names if str(n).strip()]
    hit = difflib.get_close_matches(base, pool, n=1, cutoff=CLOSE_ENOUGH)
    return hit[0] if hit else ""


def hint(path: str, names: Optional[Sequence[str]]) -> str:
    """The sentence to hand back, or "".

    ``names`` None means the listing could NOT be determined - say nothing
    rather than assert an empty directory, which is the contract
    artifact_list_files documents and the opposite claim would be a lie.
    """
    p = str(path or "").strip()
    if not p or names is None:
        return ""
    listed = [str(n) for n in names if str(n).strip()]
    if not listed:
        return ("%s does not exist, and the working directory is empty. Nothing "
                "has been written yet - create the file before running it."
                % p)
    near = did_you_mean(p, listed)
    out = "%s does not exist. The working directory contains: %s." % (
        p, ", ".join(listed[:40]))
    if near:
        out += " Did you mean %s?" % near
    out += (" Use one of those exact names - do not retry the same path or "
            "invent another one.")
    return out


def is_missing_path_failure(result: Any) -> bool:
    """True when ``result`` is a FAILURE about a path that is not there.

    The predicate the caller should gate on, so it only pays for a container
    listing when the hint could actually be produced.
    """
    if not isinstance(result, dict) or result.get("ok"):
        return False
    return bool(missing_path(_text_of(result)))


def augment(result: Dict[str, Any], names: Optional[Sequence[str]]) -> Dict[str, Any]:
    """Attach the hint to a failed result. Returns the SAME dict for convenience.

    A successful result is never touched, and neither is a failure that is not
    about a missing path - this must add signal to the one case it understands
    and stay out of the way everywhere else.
    """
    if not isinstance(result, dict):
        return result
    if result.get("ok"):
        return result
    p = missing_path(_text_of(result))
    if not p:
        return result
    h = hint(p, names)
    if not h:
        return result
    result["missing_path"] = p
    result["missing_path_hint"] = h
    # Appended to the field the agentic loop actually surfaces. stderr is what
    # result_failure_reason reads for an exec failure, so a hint that is not
    # there is a hint the model never sees.
    for field in ("stderr", "error"):
        if str(result.get(field) or "").strip():
            result[field] = "%s\n\n%s" % (str(result[field]).rstrip(), h)
            return result
    result["error"] = h
    return result


def workspace_names(listing: Any) -> Optional[List[str]]:
    """Normalise whatever artifact_list_files returned into names, or None."""
    if listing is None:
        return None
    try:
        return [str(n) for n in listing]
    except Exception:
        return None
