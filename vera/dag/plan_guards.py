"""Pure plan-shape guards for the v5/v7 planner.

REDUNDANT VERIFY STEPS (2026-08-24). `code.author`/`code.edit` run a real parser
on what they write and report the verdict, so a step that exists only to read
that file back and confirm it "is valid / complete / parses" decides nothing. It
cannot fail usefully and it burns cycles.

Three separate prompt instructions used to ORDER exactly that (the BUILD intent
directive's "you MAY add ONE final step to verify", the STEP PHASES rule's
"[act, verify] for any step that CREATES something", and an unconstrained
`success` criterion). Those were fixed — and it helped: plans went from always
carrying such a step to usually not. But it did NOT eliminate them: sampled
plans at BOTH temperature 0.2 and 0.6 still sometimes emit
"Verify JavaScript syntax by reading the file". Prompt guidance alone is not
reliable here, hence this deterministic guard.

Deliberately CONSERVATIVE — it drops a step only when every signal agrees:
  * something earlier in the plan actually authors a file, AND
  * the step has no capability that could observe real BEHAVIOUR, AND
  * its wording is about the artifact's form (syntax/validity/completeness), AND
  * its wording is NOT about behaviour.

So "Exercise behavior by loading in browser" (operator.run) survives, and so does
"Verify JavaScript syntax and runtime behavior" — mentioning behaviour is enough
to keep a step. Verifying BEHAVIOUR is legitimate and must never be pruned; only
re-checking what the parser already settled is not.
"""

import re
from typing import Any, Dict, List

# Caps that author a file — their output is what gets redundantly re-checked.
AUTHOR_CAPS = ("code.author", "code.edit")

# Caps that can only look at an artifact's bytes. A step limited to these cannot
# observe behaviour, so a "verify" framed in artifact terms is redundant.
INSPECT_ONLY_CAPS = {
    "code.author", "code.edit", "ide.fs.read", "code.read", "ide.code.read_lines",
    "exec.bash.run", "exec.python.run", "text.grep", "text.extract", "text.json",
    "text.slice", "text.fields", "text.uniq",
}

_VERIFY_VERB = re.compile(
    r"\b(verif\w*|validat\w*|check\w*|confirm\w*|ensur\w*|inspect\w*|review\w*)\b", re.I)

# The artifact's FORM — what the parser already settled.
_FORM_NOUN = re.compile(
    r"\b(syntax|valid(?:ity)?|complete(?:ness)?|pars\w*|well[- ]?formed|"
    r"structur\w*|deliverable|exists?|existence|file state|final state|"
    r"correctly formed|no errors?)\b", re.I)

# Real BEHAVIOUR keeps the step, even alongside form words. These must be
# EXERCISING phrases, not words that merely describe what the code contains —
# a first attempt matched bare "render"/"display"/"user", which kept a step
# whose criterion was "...functions for rendering/updating", i.e. a description
# of the file's contents, not a behavioural test. Note the primary gate is
# _FORM_NOUN below: a step has to be about the artifact's FORM to be prunable at
# all, so this list only has to catch genuinely behavioural wording.
_BEHAVIOUR = re.compile(
    r"(\bbehaviou?r\w*|\bend[- ]?to[- ]?end\b|\be2e\b|\bin (?:the )?browser\b|"
    r"\bscreenshot\w*|\bclick\w*|\binteract\w*|\bactually (?:works|does|runs)\b|"
    r"\bworks? (?:correctly|as expected|properly)\b|\bopen(?:s|ing)? it\b|"
    r"\bload(?:s|ing)? (?:it )?in\b)", re.I)


def _text_of(step: Dict[str, Any]) -> str:
    return " ".join(str(step.get(k) or "") for k in ("title", "goal", "success"))


def is_redundant_verify_step(step: Dict[str, Any],
                             prior_steps: List[Dict[str, Any]]) -> bool:
    """True when `step` only re-checks a file an earlier step already authored."""
    if not isinstance(step, dict):
        return False
    authored_earlier = any(
        any(c in AUTHOR_CAPS for c in (p.get("caps") or []))
        for p in (prior_steps or []) if isinstance(p, dict))
    if not authored_earlier:
        return False
    caps = [c for c in (step.get("caps") or []) if isinstance(c, str)]
    if not caps or any(c not in INSPECT_ONLY_CAPS for c in caps):
        return False                      # can observe behaviour — never prune
    text = _text_of(step)
    if _BEHAVIOUR.search(text):
        return False                      # claims to check behaviour — keep it
    return bool(_VERIFY_VERB.search(text) and _FORM_NOUN.search(text))


def prune_redundant_verify_steps(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop redundant verify steps, renumber, and repair `needs` references.

    Returns {steps, dropped:[{id,title}]}. Never drops the last remaining step —
    a plan must always have something to do.
    """
    out: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for st in (steps or []):
        if isinstance(st, dict) and is_redundant_verify_step(st, out):
            dropped.append({"id": st.get("id"), "title": st.get("title")})
            continue
        out.append(st)
    if not out:                            # never prune a plan down to nothing
        return {"steps": list(steps or []), "dropped": []}
    old_to_new = {}
    for new_i, st in enumerate(out, start=1):
        old_to_new[st.get("id")] = new_i
    for new_i, st in enumerate(out, start=1):
        st["id"] = new_i
        if isinstance(st.get("needs"), list):
            st["needs"] = [old_to_new[n] for n in st["needs"] if n in old_to_new]
    return {"steps": out, "dropped": dropped}
