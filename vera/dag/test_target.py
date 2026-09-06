"""Which file a test file is a test OF.

Census 39, build-multifile ("create a small package with stats.py, plus a test
file, then run the tests"). The loop wrote the tests BEFORE reading the
implementation - `code.author` produced test_stats.py at cycle 4 and only read
stats.py at cycle 5 - so the test author guessed a contract:

    def test_calculate_mode_mixed_integers():
    >       assert calculate_mode([-1, 2, -3, 4, -5, 2]) == [2]
    E       assert 2 == [2]

stats.py returns the mode as a SCALAR; the tests assert a LIST. Seven failures,
thirteen passes, every failure that same mismatch - and the run then spent about
twenty cycles re-running pytest, authoring throwaway runner scripts, and never
reconciling the two halves. It could not converge, because nothing in the run
was wrong except an assumption made in the dark.

prose.author already solves the equivalent problem: when the caller names no
`context_files`, it auto-attaches the run's data files, because "a document task
whose sources are sitting in the working directory should be grounded in them by
default, not by luck". Writing tests for a module sitting in the same directory
is exactly that shape, and code.author had no such rule.

Deliberately NARROW. This attaches a file only when the name says the target
plainly - test_x.py -> x.py, x_test.py -> x.py - and only when a file with that
name actually exists in the working directory. It never guesses at "probably
related" code: attaching the wrong file is worse than attaching none, because it
grounds the author in something irrelevant while looking like it worked.

Pure: the caller supplies the directory listing.
"""

from __future__ import annotations

import posixpath
from typing import List, Optional, Sequence

#: Extensions whose tests this understands. Kept to the languages where the
#: test-file naming convention is unambiguous.
TESTABLE_SUFFIXES = (".py",)

#: How many files may be auto-attached. One is the honest answer for a test
#: file; the cap exists so a future rule cannot quietly turn into a bulk read.
MAX_ATTACHED = 2


def _basename(path: str) -> str:
    return posixpath.basename(str(path or "").replace("\\", "/").strip())


def is_test_path(path: str) -> bool:
    """True when this path names a test file by convention."""
    name = _basename(path)
    if not name.endswith(TESTABLE_SUFFIXES):
        return False
    stem = name.rsplit(".", 1)[0]
    return stem.startswith("test_") or stem.endswith("_test")


def module_under_test(path: str) -> str:
    """The FILE NAME this test is a test of, or "".

    test_stats.py -> stats.py ; stats_test.py -> stats.py
    """
    name = _basename(path)
    if not is_test_path(name):
        return ""
    stem, _, ext = name.rpartition(".")
    if stem.startswith("test_"):
        target = stem[len("test_"):]
    elif stem.endswith("_test"):
        target = stem[:-len("_test")]
    else:
        return ""
    if not target:
        return ""
    return "%s.%s" % (target, ext)


def pick_context(path: str, workdir_files: Optional[Sequence[str]]) -> List[str]:
    """Files to ground a test author on, given what really exists.

    Matched on BASENAME so it works whether the listing is flat or carries
    directories, and whether the test lives beside the module (statkit/) or in a
    tests/ directory. Returns [] when the target is not actually there - a
    guess is worse than nothing.
    """
    target = module_under_test(path)
    if not target or workdir_files is None:
        return []
    out: List[str] = []
    for f in workdir_files:
        name = str(f or "").strip()
        if not name or name.endswith("/"):
            continue
        if _basename(name) == target and name not in out:
            out.append(name)
            if len(out) >= MAX_ATTACHED:
                break
    return out


def describe(path: str, attached: Sequence[str]) -> str:
    """One line for the prompt, naming what was attached and why."""
    if not attached:
        return ""
    return ("\nTHE FILE UNDER TEST is included above as context: %s. Write the "
            "tests against the ACTUAL functions and return types in it - their "
            "real names, arguments and what they really return - not against "
            "what a module with this name would usually look like. If a "
            "function returns a scalar, do not assert a list.\n"
            % ", ".join(attached))
