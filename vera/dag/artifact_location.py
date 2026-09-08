"""A file the run has already made has a place. Write it back there.

The run's artifact registry is keyed by BASENAME - `_v5_art_key` says so
outright: "the run works in one dir". That holds until a goal builds a package,
and then `statkit/stats.py` and `stats.py` are the same key while being two
different files on disk.

The record itself is not confused. It carries

    rel      = "stats.py"                       <- flattened to the key
    fs_path  = "/workspace/statkit/stats.py"    <- where the file actually is

so the run knows the real location and simply does not consult it.

CENSUS 22, build-multifile, which wall-capped at 1807.5s over 39 calls:

    cyc14  code.author('statkit/stats.py')  -> /workspace/statkit/stats.py
    cyc15  code.edit('stats.py')            -> /workspace/stats.py       (!)
    cyc16  mv /workspace/stats.py /workspace/statkit/stats.py   rc=0
    cyc17  mv /workspace/stats.py /workspace/statkit/stats.py   cannot stat
    cyc18  mv /workspace/stats.py /workspace/statkit/stats.py   cannot stat
    cyc19  cp stats.py statkit/ && rm stats.py                  cannot stat
    cyc20-26  seven ls/cat cycles trying to work out what is where

The edit at cyc15 was handed a bare basename, judged "proven" (same registry
key), read the real file's content, and then SAVED IT AT THE WORKSPACE ROOT -
creating a second stats.py beside the package. Ten cycles then went on moving
it, including twice repeating a move that had already succeeded.

The same run also produced `code.author('statkit./test_stats.py')`, which
created a directory literally named `statkit.`. A path is a fact about the
filesystem; the run should not invent one when it already holds the answer.

This is the third instance tonight of one shape: a write landing somewhere the
read did not mean (workspace_path), a decision no one read (editor_reply), an
explanation no one carried (stop_explanation). Here the location is recorded and
unused.

Deliberately narrow: it corrects a path ONLY when the registry holds a
non-empty fs_path whose basename matches, and only when that implies a
different directory. It never invents a directory, never guesses between two
candidates, and leaves an unknown file alone - a wrong relocation would write
over something real.
"""

from __future__ import annotations

import posixpath
from typing import Any, Dict, Mapping, Optional

#: Where the sandbox roots a run's files. fs_path is absolute inside the
#: container; the caps want the path relative to this.
WORKSPACE_ROOT = "/workspace"


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip()


def workspace_rel(fs_path: str, root: str = WORKSPACE_ROOT) -> str:
    """`/workspace/statkit/stats.py` -> `statkit/stats.py`. "" if not under root."""
    p = _norm(fs_path)
    if not p:
        return ""
    r = _norm(root).rstrip("/") + "/"
    if not p.startswith(r):
        return ""
    return p[len(r):].strip("/")


def known_location(path: str, artifacts: Optional[Mapping[str, Any]],
                   root: str = WORKSPACE_ROOT) -> str:
    """The path this file ALREADY occupies in the run, when that differs.

    Returns "" when the registry has nothing to say - unknown file, no
    recorded fs_path, or the call already names the right place.
    """
    want = _norm(path).lstrip("/")
    if not want or not artifacts:
        return ""
    key = posixpath.basename(want)
    if not key:
        return ""
    rec = artifacts.get(key)
    if not isinstance(rec, (dict, Mapping)):
        return ""
    real = workspace_rel(str(rec.get("fs_path") or ""), root)
    if not real:
        return ""                                   # nothing recorded to trust
    if posixpath.basename(real) != key:
        return ""                                   # record is about another file
    return "" if real == want else real


def relocation_note(orig: str, real: str) -> str:
    """What to tell the executor, so the next call names the right path itself."""
    return (f"(`{orig}` was written to `{real}` - that is where this run already "
            f"put that file. The working directory is not flat: naming the bare "
            f"filename would have created a SECOND copy at the workspace root, "
            f"which is what the run then spends cycles moving. Use the full "
            f"relative path.)")


def dedupe_named_paths(paths):
    """Drop a BARE filename when a located one with the same basename is present.

    The completion gate extracts candidate files from the goal text, probes
    which are missing, and appends a "Create the missing file" step for each.
    A goal that says "save it at /workspace/statkit/stats.py" and later just
    "stats.py" yields BOTH, neither exists, and the run gets two remediation
    steps for one file. Census 45, build-multifile: the gate appended steps 2,
    3, 4 AND 5 - steps 4 and 5 being "Create the missing file:
    /workspace/statkit/stats.py" and "Create the missing file: stats.py".
    That goal then spent 17 cycles on one step.

    NOT a blanket basename dedupe, which is the very confusion this module
    exists to fix: `a/stats.py` and `b/stats.py` really are two files and both
    are kept. Only a candidate with NO directory part is dropped, and only when
    something that does name a directory shares its basename.
    """
    kept = []
    located = {posixpath.basename(_norm(str(p)))
               for p in (paths or []) if "/" in str(p)}
    for p in (paths or []):
        text = str(p)
        if "/" not in text and posixpath.basename(_norm(text)) in located:
            continue                       # the same file, named loosely
        if text not in kept:
            kept.append(text)
    return kept

