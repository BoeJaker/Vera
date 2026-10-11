"""A large CPU model reviews the WHOLE plan while the GPU runs its first steps (roadmap A3).

Compute-roles A3 (27 Sep plan, picked up 11 Oct 2026): plans still miss the write-up step, over-
decompose, or set a success bar no step can settle - and the adaptive controller only sees one step
at a time. With per-run `plan_critique` on, the long-horizon model (enrich role, CPU node) reads the
merged plan once, beside the run, and its findings reach the controller at the first assessment
after they land - advice, never a blocking gate (the 11 Oct probes: blocking stages on the CPU 35B
cost 20%+ wall; off-critical-path ones cost nothing).

Pure: prompt in, findings / a controller block out.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Sequence

KINDS = ("gap", "missing_deliverable", "over_scope", "unsettleable", "order")
MAX_FINDINGS = 5
NOTE_CHARS = 300
MIN_STEPS = 2          # a one-step plan (stepwise bootstrap, fast path) has nothing to review

SYSTEM = (
    "You review an AI agent's PLAN for a goal before it runs. Report only REAL problems that would "
    "make the run fail the goal or waste steps:\n"
    "gap - a part of the goal no step covers\n"
    "missing_deliverable - the goal asks for an output (file, report, answer) no step produces\n"
    "over_scope - a step does work the goal did not ask for\n"
    "unsettleable - a step's success criterion cannot be checked from what the step does\n"
    "order - a step needs something a LATER step produces\n"
    "Name the step id when a finding is about one step. At most %d findings; an empty list when the "
    "plan is sound - do not invent problems.\n"
    'Respond ONLY with JSON: {"findings": [{"kind": "...", "step": <id or null>, "note": '
    '"one sentence: what is wrong and what to do"}]}' % MAX_FINDINGS)


def worth(steps: Sequence[Dict[str, Any]]) -> bool:
    """Whether a plan is worth a review (at least MIN_STEPS steps)."""
    return len([s for s in steps or [] if isinstance(s, dict)]) >= MIN_STEPS


def prompt(goal: str, done_when: str, steps: Sequence[Dict[str, Any]]) -> str:
    lines = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        row = "%s. %s" % (s.get("id"), str(s.get("title") or "")[:160])
        if s.get("caps"):
            row += "  [tools: %s]" % ", ".join(str(c) for c in (s.get("caps") or [])[:6])
        if s.get("success"):
            row += "\n   success: %s" % str(s.get("success"))[:240]
        lines.append(row)
    return ("GOAL:\n%s\n\nDONE WHEN:\n%s\n\nPLAN:\n%s" %
            (str(goal or "")[:3000], str(done_when or "(not stated)")[:1500], "\n".join(lines) or "(no steps)"))


def parse(raw: str, step_ids: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """[{kind, step, note}] from the model's reply; [] when it is not usable. A step id the plan
    does not have becomes None."""
    text = str(raw or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    ids = {str(i) for i in step_ids or ()}
    out: List[Dict[str, Any]] = []
    for f in (data.get("findings") if isinstance(data, dict) else None) or []:
        if not isinstance(f, dict):
            continue
        kind = str(f.get("kind") or "").strip().lower().replace(" ", "_")
        note = " ".join(str(f.get("note") or "").split())[:NOTE_CHARS]
        if kind not in KINDS or len(note) < 8:
            continue
        step = f.get("step")
        step = step if (step is not None and str(step) in ids) else None
        out.append({"kind": kind, "step": step, "note": note})
        if len(out) >= MAX_FINDINGS:
            break
    return out


def block(findings: Sequence[Dict[str, Any]]) -> str:
    """The findings as a controller block ("" when there are none)."""
    rows = []
    for f in findings or []:
        where = " (step %s)" % f["step"] if f.get("step") is not None else ""
        rows.append("- [%s]%s %s" % (f.get("kind"), where, f.get("note")))
    if not rows:
        return ""
    return ("PLAN REVIEW (a large model read the whole plan while the first steps ran - ADVICE: act "
            "on a finding only where the run's evidence agrees; a gap or a missing deliverable is a "
            "reason to insert a step):\n" + "\n".join(rows))
