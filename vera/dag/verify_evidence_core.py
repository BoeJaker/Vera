"""What the step verifier may treat as evidence (plan item 18).

Census run70-73 (24 Sep 2026): 11 verifier reasons cited "the summary
states..." - the executor's own account - rather than a tool result; one
read "confirmed by the grep command despite producing no stdout" (no stdout
from grep IS the no-match answer); and build-multifile's step "Run unit
tests to verify implementation" was verified met on a `cat` of the test
file after pytest had reported three failures - the goal scored q=1.0 with
tests that never passed.

Two pure rules:
- a criterion that asks for a TEST OUTCOME (tests pass, exit code 0, suite
  completes) is settled by the last test-shaped run in the step's history:
  none -> not met; failures -> not met; only a clean run lets the judge rule.
- the judge's reason must quote tool evidence, not the summary (prompt rule;
  the prose lives here so the prompt and its test agree).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

_TEST_WORD_RE = re.compile(r"\b(tests?|pytest|unittest|test suite|test run)\b", re.I)
_OUTCOME_WORD_RE = re.compile(r"\b(pass|passes|passed|passing|exit code|rc\s*=?\s*0|no fail|all tests|succe)", re.I)
_RUN_MARK_RE = re.compile(r"(collected \d+ items?|\d+ passed|\d+ failed|\bFAILED\b|\bFAIL: |\bRan \d+ tests?\b|^OK$|"
                          r"test session starts|===+ .*(passed|failed|error).* ===+)", re.I | re.M)
_FAIL_MARK_RE = re.compile(r"(\d+ failed|\bFAILED\b|\bFAIL: |\bERROR: test|\d+ errors?\b|Traceback \(most recent)", re.I)
_PASS_MARK_RE = re.compile(r"(\d+ passed|^OK\b|\bOK$)", re.I | re.M)

EVIDENCE_RULE = ("Your reason must QUOTE, verbatim and in quotes, one line from the TOOL CALLS or the MOST "
                 "RECENT ACTION above. The RESULT SUMMARY is the executor's own account of itself and is "
                 "NOT evidence: where it disagrees with a tool result, the tool result wins. A shell "
                 "command that exited non-zero and printed nothing (grep, test, diff) answered NO - that "
                 "is not a confirmation.")


def _plain(preview: object) -> str:
    """A preview is a JSON-encoded tool result: its newlines arrive as the two
    characters backslash-n, which hide word boundaries (`nFAIL:`) and defeat
    line splitting. Read it as text."""
    return (str(preview or "")
            .replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t"))


def wants_test_outcome(criterion: object) -> bool:
    """True when the criterion is about tests PASSING / a run's exit code -
    not merely about a test file existing or containing assertions."""
    c = str(criterion or "")
    return bool(_TEST_WORD_RE.search(c) and _OUTCOME_WORD_RE.search(c))


def is_test_run(preview: object) -> bool:
    return bool(_RUN_MARK_RE.search(_plain(preview)))


def last_test_run(calls: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """The last call in `calls` whose preview looks like a test run, judged:
    {found, passed (True/False/None), line}. `line` is the quoted evidence."""
    found: Optional[Dict[str, Any]] = None
    for h in calls:
        tool = str(h.get("tool") or "")
        if tool.startswith("(") or not tool.startswith("exec."):
            continue
        if is_test_run(h.get("preview")):
            found = h
    if found is None:
        return {"found": False, "passed": None, "line": ""}
    pv = _plain(found.get("preview"))
    fail = _FAIL_MARK_RE.search(pv)
    if fail:
        line = next((l.strip() for l in pv.splitlines() if fail.group(0) in l), fail.group(0))
        return {"found": True, "passed": False, "line": line[:200]}
    ok = _PASS_MARK_RE.search(pv)
    if ok:
        line = next((l.strip() for l in pv.splitlines() if ok.group(0) in l), ok.group(0))
        return {"found": True, "passed": True, "line": line[:200]}
    return {"found": True, "passed": None, "line": _first_line(pv)}


AUTHOR_TOOLS = ("code.author", "code.edit")


def edit_after_last_test_run(calls: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The last successful author/edit call that came AFTER the step's last test
    run, or None. run75 build-multifile (25 Sep 2026): pytest failed, code.edit
    changed stats.py, and the verifier ruled the step met on the edit's parser
    verdict - the tests never ran again on the file that was delivered."""
    calls = list(calls)
    last_run = -1
    for i, h in enumerate(calls):
        tool = str(h.get("tool") or "")
        if tool.startswith("(") or not tool.startswith("exec."):
            continue
        if is_test_run(h.get("preview")):
            last_run = i
    if last_run < 0:
        return None
    edit = None
    for h in calls[last_run + 1:]:
        if str(h.get("tool") or "") in AUTHOR_TOOLS and h.get("ok"):
            edit = h
    return edit


def settle_test_criterion(criterion: object, calls: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A deterministic verdict for a test-outcome criterion, or None when the
    judge should decide (the run passed, or the criterion is not about tests).

    Two rules. (1) A criterion about tests passing is settled by the last
    test-shaped run: none, or a failing one, is NOT met. (2) An author/edit
    call AFTER the last test run leaves the step unverified - the file that
    would be delivered is not the file the tests ran on - unless the run had
    passed and the criterion is not about tests at all."""
    calls = list(calls)
    wants = wants_test_outcome(criterion)
    r = last_test_run(calls)
    if wants:
        if not r["found"]:
            return {"met": False, "reason": "the criterion asks for a test outcome but no test run happened in this step"}
        if r["passed"] is False:
            return {"met": False, "reason": f'the last test run in this step reported failures: "{r["line"]}"'}
    edit = edit_after_last_test_run(calls)
    if edit is not None and (wants or r["passed"] is not True):
        path = str((edit.get("args") or {}).get("path") or "the file")
        return {"met": False,
                "reason": (f'{edit.get("tool")} changed {path} AFTER the last test run '
                           f'("{r["line"] or "outcome not shown"}") - the fix is unverified until the '
                           "tests run again on the edited file; run them now")}
    return None


def _first_line(s: str, n: int = 200) -> str:
    t = s.strip()
    return (t.splitlines()[0].strip() if t else "")[:n]
