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


# ── The loop's planning-style selector ───────────────────────────────────────
#
# The agentic loop (dag.agent_loop_v6/v7) takes a `plan_style` and asks this
# table what that style means for ITS planning phase. The table is the whole
# contract: the loop reads these switches at its own call sites and does not
# branch on style names anywhere else, so a style's behaviour can be read here
# in one place and tested without booting the loop.
#
#   auto      the loop's behaviour before styles existed: tier + intent pick
#             the path (one-shot plan, master-plan escalation for strategic
#             goals, recon, the shape guards).
#   flat      ONE plan from the loop's planner (plus its empty-plan retry) and
#             nothing else - no master-plan escalation, no recon rounds.
#   stepwise  NO upfront plan. The run starts from one bootstrap step and the
#             adaptive controller plans each next step from the evidence so far.
#             The shape guards are off: they would "repair" the one-step start.
#   detailed  the multi-lens brief (plan_detailed's lenses, merged host-side)
#             is handed to the loop's planner as context, which then writes the
#             steps under its own sizing and cap-routing rules. The lenses alone
#             over-decompose (a one-line sum became six steps, 2026-09-10).
LOOP_STYLES: Dict[str, Dict[str, Any]] = {
    "auto": {"label": "Auto (tier & intent decide)",
             "run_planner": True, "master_plan": True, "recon": True,
             "shape_guards": True, "lens_brief": False, "stepwise_controller": False},
    "flat": {"label": "Flat (one plan, no escalation)",
             "run_planner": True, "master_plan": False, "recon": False,
             "shape_guards": True, "lens_brief": False, "stepwise_controller": False},
    "stepwise": {"label": "Stepwise (plan each step from evidence)",
                 "run_planner": False, "master_plan": False, "recon": False,
                 "shape_guards": False, "lens_brief": False, "stepwise_controller": True},
    "detailed": {"label": "Detailed (multi-lens brief, then plan)",
                 "run_planner": True, "master_plan": True, "recon": True,
                 "shape_guards": True, "lens_brief": True, "stepwise_controller": False},
    # BROAD: research-shaped. One call splits the goal into work-streams; every
    # stream is planned CONCURRENTLY as a piece of the whole (on the
    # planning_style stream / stream_cpu routes, so the GPU and both CPU nodes
    # can each take one); the sub-plans are merged host-side with cross-stream
    # `needs`. It replaces the single planner call (run_planner off) and falls
    # back to it when no stream plans.
    "broad": {"label": "Broad (work-streams planned in parallel)",
              "run_planner": False, "master_plan": False, "recon": False,
              "shape_guards": True, "lens_brief": False, "stepwise_controller": False,
              "broad": True},
    # BROAD-STEPWISE (user, 2026-09-27: "a type of plan that blends broad and
    # stepwise"): broad's split into work-streams and its CPU briefs, but each
    # stream is planned as ONE opening step and then grown one step at a time by
    # the controller from what the stream has found (stepwise). Broad's up-front
    # plans were its weak point in census run1 (coarse 2-step plans that missed
    # parts of the goal); stepwise's evidence-led steps were its strength (12/12).
    # `stream_steps` caps each stream's up-front plan; the shape guards are off
    # for the stepwise reason - they would "repair" the one-step openings.
    "broad-stepwise": {"label": "Broad-stepwise (work-streams, each grown step by step)",
                       "run_planner": False, "master_plan": False, "recon": False,
                       "shape_guards": False, "lens_brief": False, "stepwise_controller": True,
                       "broad": True, "stream_steps": 1},
}
DEFAULT_LOOP_STYLE = "auto"


def loop_style_ids() -> List[str]:
    return list(LOOP_STYLES)


def resolve_loop_style(requested: Any) -> Tuple[str, Dict[str, Any], str]:
    """(effective id, switches, reason). An unknown or empty request runs as
    `auto` and SAYS so - a typo must not silently become a different style."""
    req = str(requested or "").strip().lower()
    if not req or req == DEFAULT_LOOP_STYLE:
        return DEFAULT_LOOP_STYLE, dict(LOOP_STYLES[DEFAULT_LOOP_STYLE]), "default"
    if req in LOOP_STYLES:
        return req, dict(LOOP_STYLES[req]), "requested"
    return (DEFAULT_LOOP_STYLE, dict(LOOP_STYLES[DEFAULT_LOOP_STYLE]),
            "unknown style %r - ran as %s (known: %s)"
            % (req[:40], DEFAULT_LOOP_STYLE, ", ".join(LOOP_STYLES)))


# ── BROAD: work-streams, planned concurrently, merged on the host ────────────

MAX_STREAMS = 5
#: Routing roles (profile planning_style), placed by the compute-roles rule
#: (.git/vera-work/shared-planning/compute-roles/PLAN.md): the GPU carries the
#: core plan, the CPU nodes plan IN PARALLEL and enrich it - the research
#: pipeline's writer/analyst split.
#:   stream  - each stream's step plan. GPU: the run is waiting on it, and the
#:             GPU does it in seconds. (First design alternated streams onto the
#:             CPU nodes: measured 2026-09-27 a CPU stream took 199-251 s against
#:             6-17 s on the GPU - the CPU node WAS the planning time.)
#:   enrich  - a deeper per-stream brief (inputs, pitfalls, what a complete
#:             deliverable holds) on a CPU node, concurrently; added to that
#:             stream's steps when it arrives.
PLAN_ROLE = "stream"
ENRICH_ROLE = "enrich"
#: Nothing waits for a CPU brief (user, 2026-09-27: the CPU node is a
#: NON-BLOCKING supplicant). The GPU writes a quick first brief for the first
#: stream alongside the plan, so step 1 never starts bare; each CPU brief is
#: applied to its stream's steps that have not started when it lands.
MAX_BRIEF_CHARS = 1400

BROAD_BRIEF_SYSTEM = (
    "You split an agentic GOAL into its WORK-STREAMS: the distinct, substantial bodies of "
    "work a thorough plan would cover. Each stream will be planned in detail separately, "
    "in parallel, so each must stand on its own. Rules:\n"
    "  - use the FEWEST streams the goal genuinely has, at most {n}. A short document or a "
    "small app is at most TWO streams (gather what it needs, then produce it); ONE stream "
    "if there is nothing to gather. Never invent streams to fill a count.\n"
    "  - a stream is a kind of work (gather the facts, build the thing, write it up), NOT a "
    "single command, and never a separate stream for citations, formatting or review of "
    "another stream's output - that belongs to the stream that produces it\n"
    "  - never add a stream for a deliverable the goal did not ask for (a format "
    "conversion, an HTML version, a dashboard, a test suite)\n"
    "  - `dependencies` lists ids of EARLIER streams whose deliverable this one consumes\n"
    "  - `caps`: up to 6 capability names from the list, exact names only\n"
    "  - `deliverable`: the concrete file or result that marks the stream done - never a "
    "value you worked out yourself\n"
    'Respond ONLY with JSON: {{"streams":[{{"id":1,"title":"<short>","objective":"<what this '
    'stream must achieve, 1-3 sentences>","deliverable":"<file or result>","dependencies":[],'
    '"caps":["cap.name"]}}]}}')


def broad_brief_prompt(goal: str, catalog_lines: str) -> str:
    return ("GOAL: %s\n\nAVAILABLE CAPABILITIES (name - description):\n%s\n\n"
            "Split the goal into its work-streams." % (goal, catalog_lines or "  (none)"))


def parse_streams(obj: Any, catalog: Optional[Iterable[str]] = None,
                  max_streams: int = MAX_STREAMS) -> List[Dict[str, Any]]:
    """The model's streams, validated: ids renumbered 1..n in the order given,
    dependencies kept only when they point at an EARLIER stream (a cycle or a
    forward reference is dropped, not trusted), caps filtered to the catalog."""
    raw = (obj or {}).get("streams") if isinstance(obj, dict) else obj
    if not isinstance(raw, list):
        return []
    allow = set(catalog or [])
    keep: List[Dict[str, Any]] = []
    old_to_new: Dict[Any, int] = {}
    for s in raw:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()
        objective = str(s.get("objective") or "").strip()
        if not (title or objective):
            continue
        new_id = len(keep) + 1
        old_to_new[s.get("id", new_id)] = new_id
        caps = [str(c).strip() for c in (s.get("caps") or []) if str(c).strip()]
        if allow:
            caps = [c for c in caps if c in allow]
        keep.append({"id": new_id, "title": (title or objective)[:120],
                     "objective": (objective or title)[:600],
                     "deliverable": str(s.get("deliverable") or "")[:240],
                     "_deps_raw": list(s.get("dependencies") or []),
                     "caps": list(dict.fromkeys(caps))[:6]})
        if len(keep) >= max(1, int(max_streams)):
            break
    for s in keep:
        deps = []
        for d in s.pop("_deps_raw"):
            nd = old_to_new.get(d)
            if nd is None:
                try:
                    nd = old_to_new.get(int(d))
                except (TypeError, ValueError):
                    nd = None
            if nd is not None and nd < s["id"] and nd not in deps:
                deps.append(nd)
        s["dependencies"] = deps
    return keep


def stream_directive(goal: str, streams: Sequence[Dict[str, Any]],
                     stream: Dict[str, Any], first_step_only: bool = False) -> str:
    """The `[PIECEWISE]` master_plan text for ONE stream's planner call: the whole
    stream map as context, then this stream to plan and nothing else. With
    `first_step_only` (broad-stepwise) only the step that STARTS the stream -
    the rest is planned from its results."""
    lines = []
    for s in streams:
        dep = (" (uses: %s)" % ", ".join("stream %d" % d for d in s["dependencies"])
               if s.get("dependencies") else "")
        lines.append("  %d. %s - %s -> %s%s" % (s["id"], s["title"], s["objective"],
                                               s.get("deliverable") or "?", dep))
    deps = [x for x in streams if x["id"] in (stream.get("dependencies") or [])]
    dep_txt = ("DEPENDS ON: " + "; ".join("stream %d %s -> %s" % (d["id"], d["title"],
                                                                 d.get("deliverable") or "?")
                                          for d in deps)
               + "\nThose deliverables WILL ALREADY EXIST when this stream starts. READ and use "
               "them; do NOT plan any step that gathers, researches, fetches or derives what "
               "they already provide.\n") if deps else ""
    caps_txt = ("SUGGESTED CAPS for this stream: %s\n" % ", ".join(stream["caps"])
                if stream.get("caps") else "")
    ask = ("Plan ONLY this stream's FIRST concrete step - the one action that starts it. "
           "Its later steps are planned one at a time from what this step finds: do NOT "
           "plan them now, and do NOT plan the other streams' work."
           if first_step_only else
           "Turn THIS stream into concrete, ordered steps that end in its deliverable. Do "
           "NOT plan the other streams' work - they are planned separately.")
    return ("[PIECEWISE]WORK-STREAMS OF THIS GOAL (planned separately, in parallel - "
            "context only):\n" + "\n".join(lines) + "\n\n"
            ">>> PLAN ONLY STREAM %d of %d: %s\nOBJECTIVE: %s\nDELIVERABLE: %s\n%s%s%s"
            % (stream["id"], len(streams), stream["title"], stream["objective"],
               stream.get("deliverable") or "(not stated)", dep_txt, caps_txt, ask))


ENRICH_SYSTEM = (
    "You are the ANALYST beside an agentic loop's planner. The planner is writing the steps; "
    "you give the depth it has no time for. For ONE work-stream of the goal, write a short "
    "brief with three parts, as plain bullet lines:\n"
    "NEEDS: the facts, inputs or files this stream must have before it can finish\n"
    "PITFALLS: the specific ways an automated agent gets this stream wrong\n"
    "COMPLETE MEANS: what its deliverable must contain to count as done\n"
    "Be specific to this goal. Never state a numeric result you worked out yourself. "
    "No preamble, no headings beyond the three labels, at most 12 lines.")


def enrich_prompt(goal: str, streams: Sequence[Dict[str, Any]],
                  stream: Dict[str, Any]) -> str:
    others = "\n".join("  %d. %s -> %s" % (s["id"], s["title"], s.get("deliverable") or "?")
                       for s in streams)
    return ("GOAL: %s\n\nWORK-STREAMS:\n%s\n\nBRIEF THIS STREAM: %d. %s\nOBJECTIVE: %s\n"
            "DELIVERABLE: %s" % (goal, others, stream["id"], stream["title"],
                                 stream["objective"], stream.get("deliverable") or "(not stated)"))


def enrich_note(text: Any, deep: bool = True) -> str:
    """A brief as the block appended to a stream's step goal, bounded; '' if the
    brief is empty. `deep` = the long-horizon CPU node's review; otherwise the
    GPU's quick first look that lets the first stream start without waiting.
    (The invented-value filter is for SUCCESS CRITERIA; a brief is evidence, and
    its 4-digit numbers are mostly years.)

    NOT clean_lines: that drops any line over MAX_BULLET_CHARS as prose, and the
    brief models write each section as ONE long line ("NEEDS: a; b; c") - live
    2026-09-27 both the GPU quick brief and cpu-247's first 35B brief came back
    as 0 characters that way. A long line is split at its ';' boundaries into
    bullets, and any piece still too long is trimmed, never discarded."""
    lines: List[str] = []
    seen = set()
    for raw in _LINE_SPLIT.split(str(text or "")):
        line = _BULLET_STRIP.sub("", raw).strip()
        if not line:
            continue
        pieces = [line]
        if len(line) > MAX_BULLET_CHARS:
            head, sep, rest = line.partition(":")
            label = head.strip() + ":" if sep and len(head) <= 24 else ""
            body = rest if label else line
            parts = [p.strip() for p in body.split(";") if p.strip()]
            pieces = ([label] if label else []) + parts if len(parts) > 1 else [line]
        for p in pieces:
            p = p[:MAX_BULLET_CHARS]
            if p.lower() in seen or p.lower() in ("none", "n/a", "nothing"):
                continue
            seen.add(p.lower())
            lines.append(p)
        if len(lines) >= 20:
            break
    if not lines:
        return ""
    body = "\n".join("  - %s" % ln for ln in lines)[:MAX_BRIEF_CHARS]
    what = ("a deeper review of this work-stream, prepared in parallel" if deep else
            "a quick first look at this work-stream; a deeper review may follow")
    return ("\n\nSTREAM BRIEF (%s - use it as evidence, the step goal above still decides "
            "the work):\n" % what + body)


def merge_streams(streams: Sequence[Dict[str, Any]],
                  sub_steps: Sequence[Sequence[Dict[str, Any]]],
                  hard_cap: int = 16) -> List[Dict[str, Any]]:
    """Every stream's steps as ONE plan: stream order, ids renumbered, `needs`
    inside a stream remapped, and each stream's first step needing the LAST step
    of every stream it depends on (the piecewise rule). A stream that planned
    nothing contributes nothing and breaks no link - its dependents fall back to
    the streams before it."""
    out: List[Dict[str, Any]] = []
    last_of: Dict[int, int] = {}
    for s, subs in zip(streams, sub_steps):
        subs = [x for x in (subs or []) if isinstance(x, dict)]
        if not subs or len(out) >= hard_cap:
            continue
        offset = len(out)
        local = {}
        for i, st in enumerate(subs):
            local[st.get("id", i + 1)] = offset + i + 1
        first_needs = [last_of[d] for d in (s.get("dependencies") or []) if d in last_of]
        for i, st in enumerate(subs):
            if len(out) >= hard_cap:
                break
            ns = dict(st)
            ns["id"] = offset + i + 1
            ns["needs"] = [local[n] for n in (st.get("needs") or [])
                           if n in local and local[n] < ns["id"]]
            if i == 0:
                ns["needs"] = sorted(set(ns["needs"]) | set(first_needs))
            ns["piece"] = s["id"]
            ns["piece_title"] = s["title"]
            out.append(ns)
        if out and out[-1].get("piece") == s["id"]:
            last_of[s["id"]] = out[-1]["id"]
    return out


_DEDUPE_STOP = {"and", "the", "from", "with", "for", "into", "its", "their", "this", "that",
                "all", "any", "using", "via", "about", "details", "information", "data"}


def _step_words(st: Dict[str, Any]) -> set:
    return {w for w in _words("%s %s" % (st.get("title") or "", st.get("goal") or ""))
            if w not in _DEDUPE_STOP and "." not in w}


def dedupe_across_streams(steps: Sequence[Dict[str, Any]],
                          threshold: float = 0.5) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(kept, dropped). Streams are planned in isolation, so two of them plan the
    same work - observed 2026-09-27: a report goal's plan researched the topic in
    three streams. A step is dropped when an EARLIER step from ANOTHER stream
    shares most of its words (Jaccard >= threshold) AND at least one capability
    (or neither names one) - word overlap alone would merge 'fetch the data' with
    'write about the data'. Steps are renumbered; a `needs` that pointed at a
    dropped step points at the step it duplicated. Within a stream nothing is
    touched: the stream's own planner decided that order."""
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    alias: Dict[Any, Any] = {}
    for st in steps or []:
        w = _step_words(st)
        caps = set(st.get("caps") or [])
        twin = None
        for k in kept:
            if k.get("piece") == st.get("piece"):
                continue
            kw = _step_words(k)
            if not w or not kw:
                continue
            j = len(w & kw) / float(len(w | kw))
            kcaps = set(k.get("caps") or [])
            if j >= threshold and ((caps & kcaps) or not (caps or kcaps)):
                twin = k
                break
        if twin is not None:
            alias[st.get("id")] = twin.get("id")
            dropped.append({"id": st.get("id"), "title": st.get("title"),
                            "piece": st.get("piece"), "duplicate_of": twin.get("id")})
        else:
            kept.append(dict(st))
    renum = {k.get("id"): i + 1 for i, k in enumerate(kept)}
    for k in kept:
        needs = []
        for n in k.get("needs") or []:
            target = alias.get(n, n)
            if target in renum and renum[target] not in needs:
                needs.append(renum[target])
        k["id"] = renum[k.get("id")]
        k["needs"] = sorted(n for n in needs if n < k["id"])
    for d in dropped:
        d["duplicate_of"] = renum.get(d["duplicate_of"])
    return kept, dropped


#: The controller's extra instruction in a stepwise run. Without it the
#: controller's only move on an empty queue is "continue" - and the run ends
#: after the bootstrap step.
STEPWISE_CONTROLLER_NOTE = (
    "STEPWISE MODE: this run has NO upfront plan - you are its planner, one step at a "
    "time. When PENDING STEPS is empty and the GOAL (see DONE WHEN) is not yet "
    "demonstrably met, choose \"insert\" with exactly ONE next step: the single most "
    "useful concrete action given what the ledger shows. Choose \"stop\" only when the "
    "goal is met. Never plan several steps ahead.\n")

#: broad-stepwise: the controller grows each work-stream from its evidence.
#: Inserted steps run next (ahead of the other streams' pending openings), so a
#: stream keeps the floor until the controller judges its deliverable done.
BROAD_STEPWISE_CONTROLLER_NOTE = (
    "BROAD-STEPWISE MODE: this goal is split into WORK-STREAMS. Each was started with "
    "ONE opening step; you grow each stream one step at a time from what it finds:\n"
    "%s\n"
    "After each step, look at the work-stream that step belongs to. If that stream's "
    "deliverable does not exist yet or is incomplete, choose \"insert\" with exactly ONE "
    "next step for THAT stream - it runs before the other streams' pending steps. When "
    "its deliverable exists and is complete, choose \"continue\" so the next stream's "
    "opening step runs. Choose \"stop\" only when every stream is done and the GOAL is "
    "met. Never plan several steps ahead, and never do another stream's work in a "
    "stream's step.\n")


def controller_note(switches: Dict[str, Any], streams: Sequence[Dict[str, Any]] = (),
                    steps: Sequence[Dict[str, Any]] = ()) -> str:
    """The controller's style instruction for a run, or "" when the style has
    none. broad-stepwise gets the run's own stream map - each stream's
    deliverable and the step that opens it - so it can tell which stream a
    step belongs to and when that stream is done."""
    sw = switches or {}
    if not sw.get("stepwise_controller"):
        return ""
    if not (sw.get("broad") and streams):
        return STEPWISE_CONTROLLER_NOTE
    opens: Dict[Any, Dict[str, Any]] = {}
    for st in steps or []:
        if isinstance(st, dict) and st.get("piece") is not None:
            opens.setdefault(st.get("piece"), st)
    lines = []
    for s in streams:
        op = opens.get(s.get("id"))
        lines.append("  stream %s: %s -> deliverable: %s%s" % (
            s.get("id"), s.get("title") or "?", s.get("deliverable") or "(not stated)",
            (" (opens with step %s \"%s\")" % (op.get("id"), str(op.get("title") or "")[:80])
             if op else " (no opening step)")))
    return BROAD_STEPWISE_CONTROLLER_NOTE % "\n".join(lines)
