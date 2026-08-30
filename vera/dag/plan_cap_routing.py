"""A step that changes a file an earlier step created is an EDIT, not an author.

Observed live 2026-08-30 (session `chat-1788039334205`, goal "create a pokedex in
html js and css that looks like a pokedex"). The planner split a SINGLE-FILE
deliverable into four sequential `code.author` steps against `index.html` — and
contradicted itself while doing it:

    [1] caps=['code.author']  "Create self-contained Pokedex HTML file"
    [2] caps=['code.author']  success: "index.html is updated by CODE.EDIT to include a JS array…"
    [3] caps=['code.author']  success: "index.html is updated by CODE.EDIT to include a text input…"
    [4] caps=['code.author']  success: "index.html is updated by CODE.EDIT to include an HTML5 Audio element…"

So this is not a model that doesn't know the right cap. It names `code.edit` in
one field of the step and emits `code.author` in the other. That contradiction is
the signal this module acts on.

WHY IT MATTERS. The cost is already written down at `_V5_CORE_SEED_CAPS`:
"code.edit rides alongside code.author: without it in the catalog the only route
to changing an existing file is a full re-emit, **which is what loses unrelated
work**." Four author steps on one file is four full re-emits, each able to drop
the previous step's work — the same family as the code.author-repair-gutting
incident already guarded in the critical tier. It degrades output QUIETLY rather
than failing, which is why it went unnoticed.

WHY THIS IS DETERMINISTIC AND NOT A PROMPT CHANGE. The guidance already exists —
`loop_prompt_rules.CAP_ROUTING` says "code.author (creates) / code.edit (surgical
change)" — and the planner half-followed it. Another sentence of prompt is not
the lever, and `loop_prompt_rules` states its texts are byte-for-byte extractions
held by a golden test, with rewrites requiring their own before/after runs.
Repairing the PARSED PLAN also means this holds for both planner prompt variants
(full and minimal) and both runners, rather than for whichever prompt someone
remembered to edit — the exact drift class `test_planner_rule_parity` exists to
catch.

WHY IT IS SAFE TO RE-ROUTE. `code.author` and `code.edit` are BOTH universal
essentials, seeded into every loop toolkit regardless of a step's declared caps.
So this changes only what the step STEERS toward; it can never leave a step
unable to author. That is what makes replacing the cap (rather than merely
appending) the honest fix: the plan then says what it means.

THE RULE. A step is an edit when it carries `code.author` and either
  (a) its own text names `code.edit` — the self-contradiction above, or
  (b) every code file it names was already created by an EARLIER step.
A step that names no file, or names any file nothing has created yet, is left
alone: it may genuinely be creating something.

Pure: no app imports, no I/O.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Sequence, Set, Tuple

CREATE_CAP = "code.author"
EDIT_CAP = "code.edit"

# Extensions the coding specialist owns. Prose (.md/.txt/.rst) is deliberately
# absent: those route to prose.author and are not this module's business.
_CODE_EXT = (
    "html|htm|css|js|mjs|cjs|jsx|ts|tsx|py|sh|bash|go|rs|java|rb|php|"
    "c|h|cpp|hpp|cs|swift|kt|sql|json|yml|yaml|toml|ini|cfg|vue|svelte"
)
_FILE_RE = re.compile(r"[\w./\\-]+\.(?:" + _CODE_EXT + r")\b", re.I)

# Fields of a step that describe what it does. `success` matters most: that is
# where the planner named code.edit while caps said code.author.
_TEXT_FIELDS = ("title", "goal", "success", "description")


def _step_text(step: Dict[str, Any]) -> str:
    return " ".join(str(step.get(f) or "") for f in _TEXT_FIELDS)


def files_named(step: Dict[str, Any]) -> Set[str]:
    """Code files a step mentions, as bare lowercase basenames.

    Basenames, because the planner is inconsistent about paths — "index.html",
    "./index.html" and "/workspace/index.html" are the same deliverable, and
    treating them as different files is how a real edit step gets missed.
    """
    out: Set[str] = set()
    for m in _FILE_RE.findall(_step_text(step)):
        base = os.path.basename(str(m).replace("\\", "/")).strip().lower()
        if base and not base.startswith("."):
            out.add(base)
    return out


def _caps(step: Dict[str, Any]) -> List[str]:
    c = step.get("caps")
    return [str(x) for x in c] if isinstance(c, (list, tuple)) else []


def names_edit_cap(step: Dict[str, Any]) -> bool:
    """True if the step's own prose names code.edit — it already knows."""
    return EDIT_CAP in _step_text(step).lower()


def classify(steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-step verdict, in plan order. Does not mutate anything.

    Returns one record per step needing a change: {index, id, files, reason}.
    """
    created: Set[str] = set()
    verdicts: List[Dict[str, Any]] = []
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict):
            continue
        caps = _caps(step)
        files = files_named(step)
        reason = ""
        if CREATE_CAP in caps and EDIT_CAP not in caps:
            if names_edit_cap(step):
                # The strongest signal: the step contradicts itself.
                reason = (f"step names {EDIT_CAP} in its own success/goal text but "
                          f"carries {CREATE_CAP}")
            elif files and files <= created:
                known = ", ".join(sorted(files))
                reason = (f"every file it names ({known}) is already created by an "
                          f"earlier step — this changes a file, it does not create one")
        if reason:
            verdicts.append({"index": i, "id": step.get("id"),
                             "files": sorted(files), "reason": reason})
        # A step contributes its files to `created` whichever way it is routed:
        # after this step runs, those files exist.
        created |= files
    return verdicts


def route_edit_steps(steps: Sequence[Dict[str, Any]]
                     ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Re-route edit steps from code.author to code.edit.

    Returns (steps, changes). Steps are copied, never mutated in place, so a
    caller holding the original plan keeps it. `changes` is what the run should
    record — a silent repair would just move the blind spot.
    """
    out: List[Dict[str, Any]] = [dict(s) if isinstance(s, dict) else s
                                 for s in (steps or [])]
    changes: List[Dict[str, Any]] = []
    for v in classify(steps or []):
        step = out[v["index"]]
        before = _caps(step)
        # Replace rather than append. Both caps stay in the toolkit either way
        # (they are universal essentials), so this only changes the steer — and
        # leaving code.author in place is what produced the full re-emits.
        after = [EDIT_CAP if c == CREATE_CAP else c for c in before]
        seen: Set[str] = set()
        after = [c for c in after if not (c in seen or seen.add(c))]
        step["caps"] = after
        changes.append({"id": v["id"], "step_index": v["index"],
                        "files": v["files"], "from": before, "to": after,
                        "reason": v["reason"]})
    return out, changes
