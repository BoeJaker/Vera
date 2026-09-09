"""Planning STYLES â€” additive. The existing planner is not touched by any of this.

Vera's loop has exactly one way of producing a plan: one prompt, one model, one
shot, and whatever comes back is the plan. That is a style, not a law, and it is
a different style from the one the RESEARCH pipeline uses for briefs â€” which
fans several questions across nodes at once and merges the answers host-locally
before anything downstream sees them.

This module adds styles ALONGSIDE that, as a registry. Nothing here modifies,
wraps, monkey-patches or re-enters `_v5_orchestrate_plan`; a style is a
self-contained function that takes a goal and returns a plan, and callers choose
one by name. Adding the next style means adding an entry here and nothing else.

    STYLES["single"]    the loop's existing shape, described for comparison
    STYLES["detailed"]  several concurrent lenses, merged on the host

A style returns the SAME dict shape the loop's own planner returns, so its
output can be handed to a loop, stored, diffed against another style's, or
simply read:

    {"steps": [{"id", "title", "goal", "caps", "skills", "needs",
                "complex", "phases", "success"}],
     "reason": str, "complexity": str, "recon": [], "done_when": str}

Pure except `plan_detailed`, which takes its generate function as an argument
rather than importing one â€” so styles are testable with no model, and this
module can never import the loop module back.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: One lens = one question about the goal, asked on its own.
#:
#: Deliberately NOT "plan it five times and vote". Asking one question five
#: times returns the same blind spot five times; asking five different questions
#: is what surfaces what a single pass misses â€” most often the artifact nobody
#: named and the criterion nothing can settle.
LENSES: Tuple[Tuple[str, str], ...] = (
    ("decompose",
     "List the concrete steps needed to satisfy the GOAL, in order. One per "
     "line, starting with a verb. No preamble, no numbering, no commentary."),
    ("artifacts",
     "List every FILE or concrete output that must exist when the GOAL is "
     "done. One per line as `path - what it must contain`. If the goal needs "
     "no file at all, reply with the single line `none`."),
    ("risks",
     "List the ways an automated agent typically FAILS at a goal of this kind. "
     "One per line. Be specific to this goal, not generic advice."),
    ("criteria",
     "List what would prove the GOAL was actually achieved. One per line. Each "
     "must be checkable by looking at a file or at a command's output. NEVER "
     "state an expected numeric RESULT you worked out yourself â€” say what to "
     "look at, not what the answer will be."),
    ("caps",
     "Which of the listed CAPABILITIES does this goal need, and for what? One "
     "per line as `cap.name - why`. Use only names from the list."),
)

_BULLET_ORDER = ("decompose", "artifacts", "criteria", "risks", "caps")
_LINE_SPLIT = re.compile(r"[\r\n]+")
_BULLET_STRIP = re.compile(r"^\s*(?:[-*â€¢]|\d+[.)])\s*")
_WORD_RE = re.compile(r"[a-z0-9_.]{3,}")
_CAP_RE = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\b")
_NUM_RE = re.compile(r"\d[\d,_]*(?:\.\d+)?")

#: A line longer than this is prose, not a bullet. A lens that ignored "one per
#: line" produces a paragraph, and pasting a paragraph into a plan is how a
#: planning prompt doubles in size for no extra information.
MAX_BULLET_CHARS = 240
MAX_BULLETS_PER_LENS = 12

#: Numeric literals with at least this many digits are treated as possible
#: COMPUTED results. Below it a number is nearly always shape â€” "exit code 0",
#: "5 lines", "200 words", "HTTP 200" â€” and flagging those would caveat most
#: well-written criteria.
MIN_RESULT_DIGITS = 4


# â”€â”€ line handling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def clean_lines(text: Any) -> List[str]:
    """A lens's raw reply as bullet lines, in order, deduped.

    Order is kept: `decompose` is a SEQUENCE, and sorting it would destroy the
    only thing that lens produces.
    """
    out: List[str] = []
    seen = set()
    for raw in _LINE_SPLIT.split(str(text or "")):
        line = _BULLET_STRIP.sub("", raw).strip()
        if not line or len(line) > MAX_BULLET_CHARS:
            continue
        if line.lower() in ("none", "n/a", "nothing"):
            continue
        if line.lower() in seen:
            continue
        seen.add(line.lower())
        out.append(line)
        if len(out) >= MAX_BULLETS_PER_LENS:
            break
    return out


def caps_mentioned(lines: Iterable[str],
                   known: Optional[Iterable[str]] = None) -> List[str]:
    """Dotted cap names in these lines, filtered to ones that actually exist.

    Filtering is the point: a lens will happily invent `file.write`. An invented
    cap in a plan is worse than a vague one, because a step gets built around
    something that cannot run.
    """
    allow = set(known or [])
    out: List[str] = []
    for line in lines or []:
        for m in _CAP_RE.finditer(str(line)):
            name = m.group(1)
            if allow and name not in allow:
                continue
            if name not in out:
                out.append(name)
    return out


def _words(line: str) -> set:
    return set(_WORD_RE.findall(str(line or "").lower()))


def agreements(per_lens: Dict[str, List[str]], *, min_lenses: int = 2) -> List[str]:
    """Points more than one lens reached independently.

    Word co-occurrence, the same cheap trick the research analyst's clustering
    uses â€” not embeddings, which would need a model call and a GPU slot to tell
    us two lines are about the same file.
    """
    items: List[Tuple[str, str, set]] = []
    for lens, lines in (per_lens or {}).items():
        for line in lines:
            w = _words(line)
            if len(w) >= 3:
                items.append((lens, line, w))
    out: List[str] = []
    used = set()
    for i, (lens_a, line_a, wa) in enumerate(items):
        if i in used:
            continue
        lenses = {lens_a}
        for j in range(i + 1, len(items)):
            if j in used:
                continue
            lens_b, _lb, wb = items[j]
            if len(wa & wb) / float(len(wa | wb) or 1) >= 0.5:
                lenses.add(lens_b)
                used.add(j)
        if len(lenses) >= min_lenses:
            out.append("%s  [%s]" % (line_a, ", ".join(sorted(lenses))))
    return out


# â”€â”€ success criteria the planner did not invent â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _canon(tok: str) -> str:
    return re.sub(r"[,_]", "", str(tok or "")).rstrip(".")


def numbers_in(text: Any) -> List[str]:
    return [_canon(m.group(0)) for m in _NUM_RE.finditer(str(text or ""))]


def invented_values(criterion: Any, *grounds: Any) -> List[str]:
    """Numbers a criterion asserts that no ground text contains.

    Observed live (census exec-family run 1, session 4a524636): for a goal whose
    answer is 42925, a plan asserted "returns the integer 207085". The loop then
    computed 42925 â€” correctly â€” and threw it away three times for not matching,
    burning the whole wall cap. A criterion may describe SHAPE; it may not
    smuggle in a RESULT its author worked out in its head.
    """
    ground: set = set()
    for g in grounds:
        ground.update(numbers_in(g))
    out: List[str] = []
    for tok in numbers_in(criterion):
        if len(re.sub(r"\D", "", tok)) < MIN_RESULT_DIGITS:
            continue
        if tok in ground or tok in out:
            continue
        out.append(tok)
    return out


def drop_invented_criteria(lines: Sequence[str], goal: Any) -> Tuple[List[str], List[str]]:
    """Split criteria into (usable, rejected).

    This style REJECTS such a criterion rather than annotating it. It can afford
    to: it asked for criteria five ways and has others to fall back on, so it
    does not have to keep a bad one and hope a judge reads a caveat.
    """
    keep, drop = [], []
    for ln in (lines or []):
        (drop if invented_values(ln, goal) else keep).append(ln)
    return keep, drop


# â”€â”€ the brief â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def merge_brief(results: Dict[str, Any], *, goal: Any = "",
                known_caps: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Every lens's reply, merged host-locally. No LLM, no embeddings, no GPU."""
    per_lens: Dict[str, List[str]] = {}
    for name, _q in LENSES:
        lines = clean_lines((results or {}).get(name))
        if lines:
            per_lens[name] = lines
    rejected: List[str] = []
    if per_lens.get("criteria"):
        per_lens["criteria"], rejected = drop_invented_criteria(per_lens["criteria"], goal)
        if not per_lens["criteria"]:
            per_lens.pop("criteria")
    return {
        "lenses": per_lens,
        "answered": sorted(per_lens),
        # Named, so a thin brief is visibly thin rather than being mistaken for
        # a goal that had little to say.
        "missing": [n for n, _ in LENSES if n not in per_lens],
        "caps": caps_mentioned(per_lens.get("caps", []), known_caps),
        "agreed": agreements(per_lens),
        "rejected_criteria": rejected,
    }


def render_brief(brief: Dict[str, Any], *, max_chars: int = 4000) -> str:
    """The brief as readable markdown."""
    b = brief or {}
    lenses = b.get("lenses") or {}
    if not lenses:
        return ""
    titles = {"decompose": "Steps the goal appears to need",
              "artifacts": "Concrete outputs that must exist",
              "criteria": "What would settle it",
              "risks": "How this kind of goal usually fails",
              "caps": "Capabilities named"}
    parts = ["PLANNING BRIEF (several independent looks at this goal, merged):"]
    for name in _BULLET_ORDER:
        for i, ln in enumerate(lenses.get(name) or []):
            if i == 0:
                parts.append("\n%s:" % titles.get(name, name))
            parts.append("  - %s" % ln)
    if b.get("agreed"):
        parts.append("\nReached by more than one look (weigh highest):")
        parts.extend("  - %s" % ln for ln in b["agreed"][:8])
    if b.get("rejected_criteria"):
        parts.append("\nRejected â€” asserted a value the goal never gave:")
        parts.extend("  - %s" % ln for ln in b["rejected_criteria"][:5])
    if b.get("missing"):
        parts.append("\nNot answered: %s. Absence here is a lost look, not "
                     "'nothing to do there'." % ", ".join(b["missing"]))
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n  ... (brief truncated)"
    return text


def brief_to_plan(brief: Dict[str, Any], *, max_steps: int = 8) -> Dict[str, Any]:
    """The merged brief as a plan, in the loop's own plan shape.

    Steps come from the `decompose` lens because that is the only lens that
    produces an ORDER. The other lenses are not thrown away â€” the artifacts and
    criteria they found become the steps' success criteria and the plan's
    done_when, which is the whole reason for asking them separately.
    """
    b = brief or {}
    lenses = b.get("lenses") or {}
    titles = list(lenses.get("decompose") or [])[:max_steps]
    caps = list(b.get("caps") or [])
    criteria = list(lenses.get("criteria") or [])
    steps: List[Dict[str, Any]] = []
    for i, title in enumerate(titles):
        steps.append({
            "id": i + 1,
            "title": str(title)[:120],
            "goal": str(title)[:400],
            # Every step may reach the whole named toolkit. Narrowing per step
            # from a keyword guess would be a restriction nobody asked for, and
            # a wrong one is far more expensive than a broad one.
            "caps": caps[:8],
            "skills": [],
            "needs": [i] if i else [],
            "complex": False,
            "phases": [],
            # One criterion per step while they last, then nothing rather than a
            # recycled one: a success criterion repeated across steps settles
            # none of them.
            "success": (criteria[i][:240] if i < len(criteria) else ""),
        })
    return {
        "steps": steps,
        "reason": "detailed style: %d of %d lenses answered%s"
                  % (len(b.get("answered") or []), len(LENSES),
                     (", rejected %d invented criterion/criteria"
                      % len(b.get("rejected_criteria") or []))
                     if b.get("rejected_criteria") else ""),
        "complexity": "",
        "recon": [],
        "done_when": (criteria[-1][:240] if criteria else ""),
        "brief": b,
    }


async def plan_detailed(goal: str, generate: Callable[..., Awaitable[str]], *,
                        catalog: Optional[Sequence[str]] = None,
                        max_steps: int = 8,
                        lenses: Sequence[Tuple[str, str]] = LENSES,
                        timeout_s: float = 180.0) -> Dict[str, Any]:
    """Run every lens concurrently, merge host-locally, return a plan.

    `generate(prompt, system=...)` is injected, not imported: it keeps the style
    testable with no model, and it lets the CALLER choose the routing role. Point
    it at a CPU-pinned role â€” the GPU gate is capacity 1, so a fan-out aimed at
    the GPU does not parallelise, it queues.

    A lens that fails or times out contributes nothing and is named in
    `missing`. One dead node must not cost the whole plan.
    """
    cat = list(catalog or [])
    cat_block = ("\n\nAVAILABLE CAPABILITIES:\n"
                 + "\n".join("- %s" % c for c in cat[:120])) if cat else ""

    async def one(name: str, question: str) -> Tuple[str, str]:
        sys_p = ("You are helping plan an automated agent's work. Answer ONLY "
                 "the question asked, as short lines. No preamble, no "
                 "explanation, no markdown headings.\n\n" + question)
        try:
            out = await asyncio.wait_for(
                generate("GOAL: %s%s" % (goal, cat_block if name == "caps" else ""),
                         system=sys_p),
                timeout=timeout_s)
            return name, str(out or "")
        except Exception:
            return name, ""

    pairs = await asyncio.gather(*(one(n, q) for n, q in lenses))
    brief = merge_brief(dict(pairs), goal=goal, known_caps=cat)
    return brief_to_plan(brief, max_steps=max_steps)


#: The registry. A style is {id, label, description, plan}. `single` is the
#: loop's existing behaviour, present so the set is honest about what already
#: exists â€” it has no `plan` here because the loop owns it and this module does
#: not reimplement, wrap or replace it.
STYLES: Dict[str, Dict[str, Any]] = {
    "single": {
        "id": "single",
        "label": "Single pass",
        "description": ("One planner call: one prompt, one model, one shot. The "
                        "loop's own planner and the default for every run."),
        "owner": "dag_workshop (dag.agent_loop_v5/v6/v7)",
        "plan": None,
    },
    "detailed": {
        "id": "detailed",
        "label": "Detailed (multi-lens)",
        "description": ("Five short lenses â€” decompose, artifacts, risks, "
                        "criteria, caps â€” asked concurrently and merged on the "
                        "host with no second model call. Modelled on the "
                        "research brief, but gated: the lenses run on a "
                        "CPU-pinned role so they parallelise instead of queuing "
                        "behind the capacity-1 GPU slot."),
        "owner": "vera.planning.planner_styles",
        "plan": plan_detailed,
    },
}


def style_ids() -> List[str]:
    return sorted(STYLES)


def get_style(style_id: Any) -> Optional[Dict[str, Any]]:
    return STYLES.get(str(style_id or "").strip().lower())
