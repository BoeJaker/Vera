"""A plan may hold only the work the goal asked for (plan item 22).

Census run70-73 (24 Sep 2026), 40 plans:

  * 17 carried a step titled "Verify ..." - a read-back of a file just
    authored ("Verify document content", prose.author + fs.read), a re-import
    of a package code.author had just checked, a re-run of an analysis that
    had just printed its result, or a browser step for a goal that never
    asked for the page's behaviour. The controller verifies EVERY step
    against its own success criterion already; a separate verify step
    re-checks a settled fact, costs a specialist, and the browser ones cost
    5-30 minutes each.
  * 9 success criteria added requirements the goal never stated: a goal
    asking for a 60-second countdown was planned "displaying '60', starting
    at 59, pausing correctly, resetting to 60, persisting in localStorage";
    "Make the timer better" became start/pause/reset in a headless browser.
    The run then builds those, or fails on them - either way it is no longer
    the goal.
  * "Report the disk usage of /workspace and list the five largest files"
    was planned as 2-3 steps twice (run du; list by size; "parse and format
    the results into a report" with prose.author): 1,800 s and q=0.0 each
    time, against one exec step. The imperative "Report ..." was read as a
    document deliverable - by the planner, and again by the completion gate,
    which appended "Write and save the report".

Pure functions, no app imports. The runner applies them after the plan is
parsed and before it is emitted (one re-plan with `hygiene_note` when
criteria added requirements; then the deterministic drop and merge); the
completion gate's document override uses `implies_document`.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: A step whose title says it only checks earlier work.
VERIFY_TITLE_RE = re.compile(
    r"^\s*(?:re-?)?(?:verify|verification|validate|validation|confirm|check|double-?check|test)\b", re.I)
#: A goal that asks for behaviour to be verified or tested may keep ONE such step.
GOAL_ASKS_CHECK_RE = re.compile(
    r"\b(?:verif\w*|test\w*|check\w*|confirm\w*|in a browser|works|working|behav\w*)\b", re.I)
#: Steps whose criteria describe a deliverable's FEATURES - the only place an
#: added requirement can hide.
AUTHOR_CAPS = frozenset({"code.author", "code.edit", "prose.author", "operator.run", "llm.generate"})
#: Features the planner keeps adding to timer/clock/form goals that never named them.
FEATURE_TERMS: Tuple[Tuple[str, str], ...] = (
    ("reset", r"\breset\w*"),
    ("pause", r"\bpaus\w*"),
    ("resume", r"\bresum\w*"),
    ("reload", r"\bre-?load\w*|\brefresh\w*"),
    ("keyboard", r"\bkeyboard\b|\bshortcut\w*|\bhotkey\w*"),
    ("progress bar", r"\bprogress[- ]bar\w*"),
    ("localStorage", r"\blocal ?storage\b|\bpersist\w*"),
    ("sound", r"\bsound\w*|\balarm\w*|\bbeep\w*|\bchime\w*"),
    ("notification", r"\bnotif\w*"),
    ("dark mode", r"\bdark[- ]mode\b"),
    ("responsive", r"\bresponsive\b|\bmobile\b"),
    ("MM:SS", r"\bmm:ss\b"),
    ("lap", r"\blaps?\b"),
)
EXEC_CAPS = frozenset({"exec.bash.run", "exec.python.run"})
#: Caps a "parse / format / report the output" step reaches for.
FORMAT_CAPS = frozenset({"prose.author", "llm.generate", "sandbox.session.fs.read", "ide.fs.read"})
FILE_DELIVERABLE_RE = re.compile(
    r"\b(?:save|write|creat\w*|produc\w*|generat\w*|output)\b[^.]{0,60}?\b(?:file|folder|directory)\b"
    r"|\.(?:md|txt|csv|json|html|htm|py|js|ts|sh|yaml|yml)\b", re.I)
DOC_NOUN_RE = re.compile(
    r"\b(report|summary|summaries|document|article|write-?up|blog ?post|"
    r"narrative|synopsis|essay|briefing)\b", re.I)
#: "Report the disk usage", "then report their mean", "summarise what you find":
#: the word is a VERB - an instruction to tell the user - not a document.
IMPERATIVE_RE = re.compile(
    r"(?:^|[.!?;:]\s*|\b(?:and|then|also|please|now|finally)\s+)"
    r"(report|summari[sz]e|document)\s+"
    r"(?:the|on|what|how|whether|which|if|back|any|all|every|each|its|their|your|it|them|to|a|an)\b", re.I)


def goal_asks_for_check(goal: str) -> bool:
    return bool(GOAL_ASKS_CHECK_RE.search(goal or ""))


def _caps(step: Dict[str, Any]) -> List[str]:
    return [str(c) for c in (step.get("caps") or []) if c]


def verify_steps(goal: str, steps: Sequence[Dict[str, Any]]) -> List[Tuple[int, str]]:
    """(index, why) for every step that only re-checks settled work.

    When the goal itself asks for a check ("verify in a browser that...",
    "then run the tests"), the FIRST verify-titled step is that check and
    stays; every other one - and every one for a goal that never asked - is
    a re-check of a fact the controller has already verified.
    """
    out: List[Tuple[int, str]] = []
    asks = goal_asks_for_check(goal)
    kept = False
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "")
        if not VERIFY_TITLE_RE.search(title):
            continue
        if asks and not kept:
            kept = True
            continue
        caps = _caps(s)
        if "operator.run" in caps:
            why = ("a browser check the goal never asked for" if not asks
                   else "a second browser check")
        elif any(c.endswith(".fs.read") for c in caps):
            why = "a read-back of a file this run authored and already verified"
        else:
            why = ("re-checks work the controller already verified against its own criterion"
                   if not asks else "a second check of the same work")
        out.append((i, why))
    return out


def drop_verify_steps(goal: str, steps: Sequence[Dict[str, Any]]
                      ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The plan without its re-check steps (ids renumbered, `needs` remapped), and notes."""
    found = verify_steps(goal, steps)
    if not found or len(found) >= len(steps):
        return list(steps), []
    drop = {i for i, _ in found}
    old_ids = [str((s or {}).get("id")) for s in steps]
    dropped_ids = {old_ids[i] for i in drop}
    kept_idx = [i for i in range(len(steps)) if i not in drop]
    remap = {old_ids[i]: n + 1 for n, i in enumerate(kept_idx)}
    out: List[Dict[str, Any]] = []
    for n, i in enumerate(kept_idx):
        s2 = dict(steps[i])
        s2["id"] = n + 1
        s2["needs"] = [remap[str(x)] for x in (steps[i].get("needs") or [])
                       if str(x) in remap and str(x) not in dropped_ids]
        out.append(s2)
    notes = [f"dropped step {old_ids[i]} '{str(steps[i].get('title') or '')[:60]}': {why}"
             for i, why in found]
    return out, notes


def added_requirements(goal: str, steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{step, term, where}] for every feature a criterion names that the goal does not."""
    g = goal or ""
    out: List[Dict[str, Any]] = []
    for s in steps:
        if not isinstance(s, dict) or not (set(_caps(s)) & AUTHOR_CAPS):
            continue
        for field in ("success", "goal"):
            text = str(s.get(field) or "")
            for name, rx in FEATURE_TERMS:
                r = re.compile(rx, re.I)
                if r.search(text) and not r.search(g):
                    out.append({"step": s.get("id"), "term": name, "where": field})
    return out


def hygiene_note(added: Sequence[Dict[str, Any]]) -> str:
    """The one-shot re-plan instruction appended to the planner's user prompt."""
    terms = sorted({str(a.get("term")) for a in added if a.get("term")})
    return ("PLAN HYGIENE - your previous plan for this goal added requirements the goal never "
            "stated: " + ", ".join(terms) + ". A success criterion may test ONLY what the GOAL "
            "states; do not add features, checks or deliverables of your own, and do not add a "
            "step that only verifies earlier work - every step is verified against its own "
            "criterion already. Plan the goal as written.")


def implies_document(text: str) -> bool:
    """True when the text names a document as a DELIVERABLE, not as the verb 'report ...'."""
    t = text or ""
    verbs = {m.start(1) for m in IMPERATIVE_RE.finditer(t)}
    return any(m.start() not in verbs for m in DOC_NOUN_RE.finditer(t))


def is_single_command_goal(goal: str, steps: Sequence[Dict[str, Any]], *,
                           has_seam: Optional[Callable[[str], bool]] = None) -> bool:
    """A 2-3 step all-exec plan (plus parse/format steps) for a goal that is one
    command's output: no sequencing seam, no file or document deliverable."""
    if len(steps) < 2 or len(steps) > 3:
        return False
    if has_seam is not None and has_seam(goal or ""):
        return False
    if FILE_DELIVERABLE_RE.search(goal or "") or implies_document(goal or ""):
        return False
    caps = [set(_caps(s)) for s in steps if isinstance(s, dict)]
    if len(caps) != len(steps) or not all(c and c <= (EXEC_CAPS | FORMAT_CAPS) for c in caps):
        return False
    return any(c & EXEC_CAPS for c in caps)


def merge_exec_plan(goal: str, steps: Sequence[Dict[str, Any]]
                    ) -> Tuple[List[Dict[str, Any]], str]:
    """One exec step whose success is the command's output."""
    first = dict(steps[0])
    caps: List[str] = []
    for s in steps:
        for c in _caps(s):
            if c in EXEC_CAPS and c not in caps:
                caps.append(c)
    first.update({
        "id": 1, "needs": [], "caps": caps,
        "title": str(steps[0].get("title") or goal[:80]),
        "goal": goal,
        "success": ("The command output itself answers the goal - it states what the goal "
                    "asked to report: " + (goal or "")[:200]),
    })
    return [first], (f"merged {len(steps)} steps into one: the goal is one command's output, "
                     f"not a document to parse and format")
