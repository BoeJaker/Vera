"""What to tell a step about the working directory it can already see.

Census 36, `underspecified` ("Make the timer better", empty workspace). Thirteen
executor cycles went on discovering that the directory was empty:

    step 1  exec.bash.run x10   (only ONE flagged a duplicate - ten different
                                 probes, so the duplicate-call guard was right
                                 not to fire)
    step 5  exec.bash.run x3    controller-inserted "Discover existing files"
    step 4  exec.bash.run       failure-recovery "Discover and Read ... via Shell"

4.6 seconds of actual shell, paid for with thirteen LLM cycles, in a run that
wall-capped at 1802s.

The step context was not silent about the directory - it said

    "It is EMPTY so far - nothing has been written yet this run."

and then, eleven lines later:

    "Need to know what's on disk? Run `ls -la` with exec.bash.run"

Both sentences, every time, whatever the listing said. Given a flat contradiction
between a fact and an instruction, the model followed the instruction. The fix is
not to say the fact more loudly; it is to stop issuing the instruction when there
is nothing left to find out.

THE THREE STATES ARE NOT TWO. `_v5_workdir_files` is careful to distinguish:

    [...]  the directory holds these files      -> ground truth
    []     the directory is genuinely empty     -> ground truth
    None   the probe could not determine it     -> nothing is known

Only None leaves anything to discover. `[]` is an ANSWER, and treating it as
"say nothing" is what let an empty workspace look identical to an unknown one -
the same collapse the code.author block made with `if _wfiles`, while the
controller and verifier blocks had always kept them apart.

Pure: no I/O. The caller supplies the listing it already fetched.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

#: Offered ONLY when the listing is unknown - the one state where looking is
#: still worth a cycle.
LOOK_IT_UP = ("  • Need to know what's on disk? Run `ls -la` with exec.bash.run — never guess a "
              "filename.\n")

#: The listing is ground truth; re-deriving it is the waste this exists to stop.
TRUST_THE_LISTING = ("  • The listing above is this directory's CURRENT contents, read live for "
                     "this step. Do not run `ls`/`find` to re-check it and do not guess a "
                     "filename that is not in it.\n")


def disk_advice(workdir_files: Optional[Sequence[str]], has_bash: bool,
                read_cap: str = "the read capability") -> str:
    """The path-rules bullet about finding out what is on disk.

    ``workdir_files`` None means the listing could not be determined; a list
    (including an empty one) means it could.
    """
    if workdir_files is None:
        if has_bash:
            return LOOK_IT_UP
        return ("  • Never guess a filename — read one of the files above with %s.\n"
                % (read_cap or "the read capability"))
    return TRUST_THE_LISTING


#: Phrasings that TELL a step to list the directory.
_INVITES = ("ls -la", "run `ls`", "run ls ")
#: ...and phrasings that forbid it. Checked FIRST, because the advice that stops
#: the listing necessarily has to name the command it is stopping - the first
#: version of this function read "do not run `ls`/`find`" as an invitation and
#: reported the fix as the bug.
_FORBIDS = ("do not run", "don't run", "never run", "do not re-check")


def invites_listing(text: str) -> bool:
    """True when ``text`` tells the step to go and list the directory.

    Exists so the contradiction can be asserted on meaning rather than on one
    exact sentence a later edit would silently drift away from. Deliberately
    simple: it judges the advice strings THIS module produces, and a negation
    anywhere in them settles it.
    """
    low = str(text or "").lower()
    if any(n in low for n in _FORBIDS):
        return False
    return any(p in low for p in _INVITES)


def author_files_block(workdir_files: Optional[Sequence[str]], limit: int = 40) -> str:
    """What the file-writing specialist is told already exists.

    An empty directory is stated as a fact - it is the difference between "write
    something self-contained, there is nothing here to lean on" and no guidance
    at all.
    """
    if workdir_files is None:
        return ""
    names: List[str] = [str(n) for n in workdir_files if str(n).strip()]
    if not names:
        return ("\nTHE WORKSPACE IS EMPTY — no file exists yet. Write this one to be "
                "self-contained: do not <script src>/<link href> or import any other "
                "local file, because there are none.\n")
    return ("\nFILES ALREADY IN THE WORKSPACE (real): " + ", ".join(names[:max(1, limit)])
            + ". Do not recreate these; reference one by its exact name only if THIS "
            "file genuinely needs it, and never reference any OTHER local file that is "
            "not in this list (it will not exist).\n")
