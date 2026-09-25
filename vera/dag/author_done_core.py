"""An authoring-only step is answered by its author call (plan item 17c).

Census run74 (25 Sep 2026, every fix through 25 landed): after a parser-verified
code.author the executor still spent turns re-checking the file - two goals
read it back (0.7 s, then 0 ms from the registry), three goals tried to "serve
it" or "run it" (build-browser-verified hit a Traceback on `python -m
http.server` twice). Ten re-checks, each an executor TURN of ~20 s: the model
call, not the tool, is the cost. Item 17b had already put the authored content
in front of the executor with "do NOT read it back"; the turns persisted.

The verifier already takes the author's own verdict deterministically ("a real
parser verified its syntax - no LLM re-check"). So when a step's planned work
IS the authoring - its caps are authoring (and reading) caps only, its text
names at most one file and has no sequencing seam - the author's ok result is
the step's answer, and the step ends there.

Pure: step fields and the call result in, a verdict and its reason out.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

AUTHOR_TOOLS = frozenset({"code.author", "prose.author"})
#: A step planned with only these caps has nothing to do after the author call.
STEP_CAPS_OK = frozenset({"code.author", "prose.author", "code.edit",
                          "sandbox.session.fs.read", "code.read", "ide.fs.read"})
FILE_RE = re.compile(r"\b[\w./-]+\.(?:html?|py|js|ts|css|md|txt|json|sh|yaml|yml|csv|rst)\b", re.I)
SEAM_RE = re.compile(r"\b(?:then|after that|afterwards?|followed by|next,|and then)\b", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)
#: The runner appends the controller's steer and the run journal to a step's
#: goal before execution. run76 research-web (25 Sep 2026): that tail carried
#: source URLs and other steps' files, so the one-file test refused a step
#: whose own text named one file - and prose.author then rewrote it twice more
#: (1,016 s). Only the step's OWN text is judged.
TAIL_MARKERS = ("\n\nCONTROLLER STEER", "\n\nRUN JOURNAL", "\nCONTROLLER STEER (", "\nRUN JOURNAL (",
                "\n\n[[", "\n\nRECON FINDINGS", "\n\nPRIOR STEP")


def own_text(step_text: str) -> str:
    """The step's own title/goal, without the steer/journal tail and without URLs."""
    t = str(step_text or "")
    cut = len(t)
    for m in TAIL_MARKERS:
        i = t.find(m)
        if i >= 0:
            cut = min(cut, i)
    return URL_RE.sub(" ", t[:cut])


def files_named(text: str) -> list:
    out = []
    for m in FILE_RE.finditer(text or ""):
        name = m.group(0).rsplit("/", 1)[-1].lower()
        if name not in out:
            out.append(name)
    return out


def reauthor_note(tool: str, path: str, earlier: str, *, failed_since: bool) -> str:
    """The note that replaces a second author call for a file this step already
    wrote successfully - '' when the call should run (no earlier write, or a
    call has failed since it, so the file may well need rewriting).

    run74 research-report c19, run76 research-web c7 and c8 (25 Sep 2026):
    prose.author rewrote the same file with the identical thought each time
    ("I now need to create the markdown report") - the deterministic-sampling
    fixation the duplicate short-circuit cannot see because every rewrite's
    content differs. 200-400 s per rewrite on the writer role."""
    if not path or not earlier or failed_since:
        return ""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return (f"you have ALREADY written {name} with `{tool}` in this step and it succeeded - the "
            "result is shown below. Writing it again from scratch produces another whole draft, "
            "not progress. If something in it must change, make a TARGETED change with code.edit; "
            "otherwise the file is done - emit `done` with what it contains.\n\n" + str(earlier)[:1500])


def step_is_answered(step_caps: Iterable[str], step_text: str, tool: str, result: Any,
                     *, has_seam: Optional[Callable[[str], bool]] = None) -> Tuple[bool, str]:
    """(True, why) when this author call answers the whole step."""
    tool = str(tool or "")
    if tool not in AUTHOR_TOOLS:
        return False, ""
    caps = {str(c) for c in (step_caps or []) if c}
    if not caps or not caps <= STEP_CAPS_OK:
        return False, "the step plans more than authoring"
    if not isinstance(result, dict) or not result.get("ok"):
        return False, "the author call did not succeed"
    path = str(result.get("path") or result.get("fs_path") or "").strip()
    if not path:
        return False, "the author call named no file"
    if tool == "code.author" and not result.get("syntax_ok"):
        return False, "the file was not parser-verified"
    text = own_text(step_text)
    seam = has_seam(text) if has_seam is not None else bool(SEAM_RE.search(text))
    if seam:
        return False, "the step has a second part after the authoring"
    named = files_named(text)
    if len(named) > 1:
        return False, "the step names more than one file"
    if named and named[0] != path.rsplit("/", 1)[-1].lower():
        return False, "the authored file is not the one the step names"
    checked = str(result.get("checked_with") or "a parser") if tool == "code.author" else "the author"
    return True, (f"{tool} wrote {path.rsplit('/', 1)[-1]} and it is verified by {checked}; "
                  "that IS this step's deliverable - the verifier takes the author's verdict, "
                  "so no read-back, serve or trial run is needed")
