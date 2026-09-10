"""Planning STYLES — additive. The existing planner is not touched by any of this.

Vera's loop has exactly one way of producing a plan: one prompt, one model, one
shot, and whatever comes back is the plan. That is a style, not a law, and it is
a different style from the one the RESEARCH pipeline uses for briefs — which
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
rather than importing one — so styles are testable with no model, and this
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
#: is what surfaces what a single pass misses — most often the artifact nobody
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
     "state an expected numeric RESULT you worked out yourself — say what to "
     "look at, not what the answer will be."),
    ("caps",
     "Which of the listed CAPABILITIES does this goal need, and for what? One "
     "per line as `cap.name - why`. Use only names from the list."),
)

_BULLET_ORDER = ("decompose", "artifacts", "criteria", "risks", "caps")
#: Lenses whose lines describe what must exist or be observed, and so may carry
#: a planner-computed RESULT. Their lines are checked against the goal.
_GROUNDED_LENSES = ("criteria", "artifacts")
_LINE_SPLIT = re.compile(r"[\r\n]+")
_BULLET_STRIP = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_WORD_RE = re.compile(r"[a-z0-9_.]{3,}")
_CAP_RE = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\b")
_NUM_RE = re.compile(r"\d[\d,_]*(?:\.\d+)?")

#: A line longer than this is prose, not a bullet. A lens that ignored "one per
#: line" produces a paragraph, and pasting a paragraph into a plan is how a
#: planning prompt doubles in size for no extra information.
MAX_BULLET_CHARS = 240
MAX_BULLETS_PER_LENS = 12

#: Numeric literals with at least this many digits are treated as possible
#: COMPUTED results. Below it a number is nearly always shape — "exit code 0",
#: "5 lines", "200 words", "HTTP 200" — and flagging those would caveat most
#: well-written criteria.
MIN_RESULT_DIGITS = 4


# ── line handling ────────────────────────────────────────────────────────────

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


#: How many capability names the caps lens is shown. A real registry holds
#: two thousand; a prompt cannot, and the first live run proved what a naive
#: cut does: `cat[:120]` of a SORTED registry is every cap from `agent.*` to
#: `bench.*` and nothing else, so the lens chose `bench.loop` for arithmetic
#: because `exec.python.run` was never on the page.
MAX_CATALOG_SHOWN = 150

_CAP_SPLIT = re.compile(r"[._]")


def select_catalog(catalog: Sequence[str], goal: Any,
                   limit: int = MAX_CATALOG_SHOWN) -> List[str]:
    """The slice of a large catalogue worth showing the caps lens.

    Ranked host-locally by word overlap between the goal and the cap name's
    segments, ties broken by name so the output is stable. A catalogue that
    already fits is returned whole, in order: a caller who curated it (a
    profile's allowed caps) said exactly what they meant.
    """
    cat = [str(c) for c in (catalog or []) if str(c or "").strip()]
    if len(cat) <= limit:
        return cat
    gw = {w for w in _words(goal) if len(w) >= 4}
    stems = {w[:5] for w in gw}

    def score(name: str) -> int:
        segs = {s for s in _CAP_SPLIT.split(name.lower()) if s}
        hit = sum(1 for s in segs if s in gw)
        near = sum(1 for s in segs if len(s) >= 4 and s[:5] in stems)
        return hit * 3 + near

    ranked = sorted(cat, key=lambda n: (-score(n), n))
    return ranked[:limit]


def agreements(per_lens: Dict[str, List[str]], *, min_lenses: int = 2) -> List[str]:
    """Points more than one lens reached independently.

    Word co-occurrence, the same cheap trick the research analyst's clustering
    uses — not embeddings, which would need a model call and a GPU slot to tell
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


# ── success criteria the planner did not invent ──────────────────────────────

def _canon(tok: str) -> str:
    return re.sub(r"[,_]", "", str(tok or "")).rstrip(".")


def numbers_in(text: Any) -> List[str]:
    return [_canon(m.group(0)) for m in _NUM_RE.finditer(str(text or ""))]


def invented_values(criterion: Any, *grounds: Any) -> List[str]:
    """Numbers a criterion asserts that no ground text contains.

    Observed live (census exec-family run 1, session 4a524636): for a goal whose
    answer is 42925, a plan asserted "returns the integer 207085". The loop then
    computed 42925 — correctly — and threw it away three times for not matching,
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


# ── the brief ────────────────────────────────────────────────────────────────

def merge_brief(results: Dict[str, Any], *, goal: Any = "",
                known_caps: Optional[Iterable[str]] = None,
                errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Every lens's reply, merged host-locally. No LLM, no embeddings, no GPU.

    `errors` names WHY a lens is missing (timeout, exception). A lens that
    replied but yielded no usable line is named too, so an all-missing brief
    can be told apart from a goal that had nothing to say.
    """
    per_lens: Dict[str, List[str]] = {}
    for name, _q in LENSES:
        lines = clean_lines((results or {}).get(name))
        if lines:
            per_lens[name] = lines
    rejected: List[str] = []
    # Both lenses that describe what must EXIST are checked: observed live, the
    # artifacts lens wrote "stdout - the integer result (42925)" - right, as it
    # happens, but a value the planner worked out is a value the planner worked
    # out, and the next one will be 207085.
    for name in _GROUNDED_LENSES:
        if per_lens.get(name):
            kept, dropped = drop_invented_criteria(per_lens[name], goal)
            rejected.extend(dropped)
            if kept:
                per_lens[name] = kept
            else:
                per_lens.pop(name)
    errs: Dict[str, str] = {k: str(v) for k, v in (errors or {}).items() if v}
    for name, _q in LENSES:
        if name in per_lens or name in errs or name not in (results or {}):
            continue
        raw = str((results or {}).get(name) or "").strip()
        if not raw:
            errs[name] = "empty reply"
        elif name in _GROUNDED_LENSES and rejected:
            errs[name] = "every line asserted a value the goal never gave"
        elif raw.lower() not in ("none", "n/a", "nothing"):
            errs[name] = "no usable lines"
    return {
        "lenses": per_lens,
        "answered": sorted(per_lens),
        # Named, so a thin brief is visibly thin rather than being mistaken for
        # a goal that had little to say.
        "missing": [n for n, _ in LENSES if n not in per_lens],
        "errors": errs,
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
        parts.append("\nRejected — asserted a value the goal never gave:")
        parts.extend("  - %s" % ln for ln in b["rejected_criteria"][:5])
    if b.get("missing"):
        errs = b.get("errors") or {}
        named = ["%s (%s)" % (n, errs[n]) if errs.get(n) else n for n in b["missing"]]
        parts.append("\nNot answered: %s. Absence here is a lost look, not "
                     "'nothing to do there'." % ", ".join(named))
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n  ... (brief truncated)"
    return text


def brief_to_plan(brief: Dict[str, Any], *, max_steps: int = 8) -> Dict[str, Any]:
    """The merged brief as a plan, in the loop's own plan shape.

    Steps come from the `decompose` lens because that is the only lens that
    produces an ORDER. The other lenses are not thrown away — the artifacts and
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
                        timeout_s: float = 900.0) -> Dict[str, Any]:
    """Run every lens concurrently, merge host-locally, return a plan.

    `generate(prompt, system=...)` is injected, not imported: it keeps the style
    testable with no model, and it lets the CALLER choose the routing role. The
    GPU gate is capacity 1, so a fan-out aimed at the GPU queues rather than
    parallelising — accepted (2026-09-09): the CPU nodes, where five lenses
    could have run side by side, are a hard throughput limit (16 tokens in
    329 s), so the lenses go to the GPU one after another and stay short.

    A lens that fails or times out contributes nothing, is named in `missing`,
    and says why in `errors`. One dead node must not cost the whole plan.
    """
    cat = list(catalog or [])
    shown = select_catalog(cat, goal)
    cat_block = ("\n\nAVAILABLE CAPABILITIES (%d of %d, the ones nearest this goal):\n"
                 % (len(shown), len(cat))
                 + "\n".join("- %s" % c for c in shown)) if shown else ""

    async def one(name: str, question: str) -> Tuple[str, str, str]:
        sys_p = ("You are helping plan an automated agent's work. Answer ONLY "
                 "the question asked, as short lines. No preamble, no "
                 "explanation, no markdown headings.\n\n" + question)
        try:
            out = await asyncio.wait_for(
                generate("GOAL: %s%s" % (goal, cat_block if name == "caps" else ""),
                         system=sys_p),
                timeout=timeout_s)
            return name, str(out or ""), ""
        except asyncio.TimeoutError:
            # Named, so a brief with every lens missing reads as "the nodes
            # were too slow for this budget", not as "the goal had nothing to
            # say". Observed live: five CPU lenses, 180s, nothing answered.
            return name, "", "TimeoutError: no answer within %.0fs" % timeout_s
        except Exception as e:
            return name, "", "%s: %s" % (type(e).__name__, str(e)[:160])

    triples = await asyncio.gather(*(one(n, q) for n, q in lenses))
    results = {n: text for n, text, _err in triples}
    errors = {n: err for n, _text, err in triples if err}
    brief = merge_brief(results, goal=goal, known_caps=cat, errors=errors)
    return brief_to_plan(brief, max_steps=max_steps)


#: The registry. A style is {id, label, description, plan}. `single` is the
#: loop's existing behaviour, present so the set is honest about what already
#: exists — it has no `plan` here because the loop owns it and this module does
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
        "description": ("Five short lenses — decompose, artifacts, risks, "
                        "criteria, caps — each a no-think GPU call through the "
                        "gate, merged on the host with no second model call. "
                        "Modelled on the research brief, but gated, and a "
                        "criterion or artifact asserting a number the goal "
                        "never gave is rejected."),
        "owner": "vera.planning.planner_styles",
        "plan": plan_detailed,
    },
}


def style_ids() -> List[str]:
    return sorted(STYLES)


def get_style(style_id: Any) -> Optional[Dict[str, Any]]:
    return STYLES.get(str(style_id or "").strip().lower())
