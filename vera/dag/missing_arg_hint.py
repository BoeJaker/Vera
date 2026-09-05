"""A missing `path` when the run knows perfectly well which files exist.

Census 34 lost four calls across three goals to this:

    code.edit FAILED               - missing required argument: path
    sandbox.session.fs.read FAILED - path required            (x3)

The executor already refuses the call and re-prompts, so nothing is wasted on
the cap itself - but the model is told only that an argument is missing, and
it retries by guessing again. Meanwhile the run's artifact registry holds the
exact list of files it has written.

This does NOT fill the argument in. Choosing a file on the model's behalf can
edit the wrong one, and a wrong edit to a working file is far worse than a
rejected call - the same reasoning that keeps _v5_apply_edits refusing an
ambiguous anchor. It names the candidates instead, which is precisely what
made the anchor hint work: "Closest text actually in the file" turned a dead
end into a next move, where echoing the model's own input back at it did not.

Pure: no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: Arguments this module can say something useful about. Anything else falls
#: back to the plain message - inventing help for an argument we know nothing
#: about would be noise.
PATHLIKE = ("path", "file", "filename", "filepath")

MAX_SHOWN = 8


def _artifact_names(artifacts: Optional[Dict[str, Any]]) -> List[str]:
    """Files the run has produced, newest-looking first, de-duplicated."""
    out: List[str] = []
    for key, rec in (artifacts or {}).items():
        name = ""
        if isinstance(rec, dict):
            name = str(rec.get("rel") or rec.get("path") or rec.get("fs_path") or "")
        if not name:
            name = str(key or "")
        name = name.strip()
        # The registry also holds non-file bookkeeping entries (url caches and
        # similar); a candidate the model cannot open is worse than none.
        if not name or name.startswith("url:") or name.endswith("/"):
            continue
        if name not in out:
            out.append(name)
    return out


def describe(tool: str, missing: Iterable[str],
             artifacts: Optional[Dict[str, Any]] = None) -> str:
    """The error line for a call refused for missing required arguments."""
    names = [str(m) for m in (missing or [])]
    head = "missing required argument: " + ", ".join(names)
    if not names:
        return head

    pathlike = [n for n in names if n.lower() in PATHLIKE]
    if not pathlike:
        return head

    files = _artifact_names(artifacts)
    if not files:
        return (head + ". This run has not written any files yet, so there is "
                "nothing to name - create the file first, or read the workspace "
                "to find out what is there.")

    shown = ", ".join(files[:MAX_SHOWN])
    more = "" if len(files) <= MAX_SHOWN else " (+%d more)" % (len(files) - MAX_SHOWN)
    if len(files) == 1:
        return (head + ". This run has written exactly one file, %s - if that is "
                "the one you meant, pass it as `%s`." % (files[0], pathlike[0]))
    return (head + ". Files this run has written: %s%s. Pass one of those as "
            "`%s` - do not guess a name." % (shown, more, pathlike[0]))
