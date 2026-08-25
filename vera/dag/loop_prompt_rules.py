"""Loop prompt rules â€” one definition per rule, with its consumers named.

Phase 1 of documentation/PLAN-agentic-loop-prompt-architecture.md.

WHY THIS EXISTS. The loop assembles 23 system prompts across 11 LLM stages from
~84k characters of text, and rules that several stages are supposed to share were
being edited in one place and silently missed in another. Both directions have
now happened for real:

  * `_V7_CRITERIA_RULE` reached only the FULL planner prompt, so it did nothing on
    every real run - all of them take the minimal path (VERA_LOOP_MINIMAL_PLAN has
    been the default primary since 2026-08-17);
  * `_V7_CAP_ROUTING` reached only the MINIMAL planner prompt, so the documented
    escape hatch back to the full primary silently dropped cap routing and the
    "already checked, do not re-verify" rule inside it.

Neither was visible by reading either prompt on its own. A rule with ONE
definition and an explicit list of consumers makes that class of drift a fact you
can query and test, rather than something you find by accident weeks later.

WHAT THIS IS NOT. Extraction only - the texts below are byte-for-byte what the
module already sent, and tests/test_planner_prompt_golden.py holds them to that.
Rewriting any of them is a separate, behavioural change that needs its own
before/after runs (see the plan, section 5).

Pure: no app imports, so it can be unit-tested directly.
"""

from typing import Dict, List, NamedTuple, Tuple


class Rule(NamedTuple):
    """One prompt rule: its text, which stages must carry it, and why it exists."""
    id: str
    text: str
    stages: Tuple[str, ...]
    evidence: str


CAP_ROUTING = Rule(
    id='cap_routing',
    stages=('planner:full', 'planner:minimal', 'controller', 'adjust'),
    evidence='Reached only planner:minimal until 2026-08-25, so VERA_LOOP_MINIMAL_PLAN=0 silently dropped cap routing and the already-checked rule inside it.',
    text="CAPABILITY ROUTING — match the deliverable to the RIGHT cap (one source of truth):\n  • SOURCE CODE (.py/.js/.ts/.html/.css/.sh/.go/…) → code.author (creates) / code.edit (surgical change). The coding specialist writes it, grounded on any context_files, syntax-checked and versioned. NEVER llm.generate, ide.fs.write, or a heredoc for code.\n    ALREADY CHECKED, EVERY LANGUAGE. Before returning, code.author/code.edit run a real parser on what they wrote and repair what it finds; ok=true means it parsed. The verdict is in the result (`syntax_ok`, `checked_with`, `runtime_ok`, `bytes`). Whether the file exists, is complete, or is valid is therefore ALREADY ANSWERED — never spend a call finding out. Do not read it, `cat` it, re-parse it, or run it to answer those questions, and do not plan a step that does. Read an authored file only to USE its content in another call (e.g. context_files for an edit); run one only to obtain a RESULT you need. A .py is no different from a .html here.\n  • A DOCUMENT (README, report, article, essay, spec, notes — .md/.txt/.rst) → prose.author, grounded on the real files it describes. NEVER hand-write it via llm.generate + ide.fs.write.\n  • REAL-WORLD DATA (a dataset, factual records, API results, a populated JSON/CSV) → FETCH it (web.* / http.get / a script hitting the source API) and PARSE it with a script (exec.python.run). NEVER llm.generate as the data source — it fabricates plausible-but-wrong values.\n  • EXTERNAL / CURRENT FACTS (a person, company, price, news, docs, anything online) → web.search then web.fetch / http.get (browser.navigate for JS-heavy sites). memory.seek / fabric.query search ONLY Vera's already-stored data, never the live web.\n  • RUN / OPERATE something (a command, a build, a deploy, a check) → exec.bash.run / exec.python.run / http.*. llm.* CANNOT run, fetch, or read anything — it only writes/transforms text you give it.\n  • llm.generate is ONLY for authoring/transforming text FROM what you already provide (summarise, rewrite, explain) — never to look something up, run something, produce code, or invent data.\n",
)

CRITERIA_SETTLEABLE = Rule(
    id='criteria_settleable',
    stages=('planner:full', 'planner:minimal'),
    evidence='Reached only planner:full when first added, so it was inert on every real run; done_when kept demanding browser proof nothing could settle.',
    text='FOR A FILE THIS RUN AUTHORS, THE PROOF IS THE AUTHOR\'S OWN VERDICT — NOT A TRIAL RUN OF THE FILE. code.author/code.edit put the file through a real parser and return `syntax_ok`/`checked_with`/`bytes`; that report IS the verification, and it is the only one this run needs or can get. Phrase every criterion so that verdict settles it: the file EXISTS, the author reported it verified, and it CONTAINS the required features — e.g. "index.html is created and verified by code.author, with start/pause/reset controls, a 25-minute work interval and a 5-minute break".\nWANT THE PAGE\'S BEHAVIOUR VERIFIED TOO? That is legitimate — but there is exactly ONE way to do it here: an `operator.run` step. It drives a REAL headless browser (observe→think→act, real clicks, real observed DOM changes, screenshots) against the file served out of this session\'s sandbox. So a criterion like "the timer counts down, pauses and resets" is allowed ONLY when the plan actually contains an operator.run step to settle it — otherwise leave the clause out rather than assert something nothing will check.\nNEVER try to verify a page any OTHER way. exec.bash.run/exec.python.run cannot see a rendered page: starting `python3 -m http.server`, driving Selenium, calling webbrowser.open()/xdg-open, or grepping the file for a function name proves nothing about behaviour. (Observed: a done_when reading "functions correctly in a browser" with no operator.run step sent a run to `python3 -m http.server`, where it hung until it was killed.) Use operator.run, or omit the claim.\n',
)

RULES: Dict[str, Rule] = {r.id: r for r in (CAP_ROUTING, CRITERIA_SETTLEABLE)}


def rules_for(stage: str) -> List[Rule]:
    """Every rule a given stage must carry, in declaration order."""
    return [r for r in RULES.values() if stage in r.stages]


def rule_ids_for(stage: str) -> List[str]:
    """Just the ids â€” what a stage-context audit record carries."""
    return [r.id for r in rules_for(stage)]


def missing_from(stage: str, composed: str) -> List[str]:
    """Rules this stage should carry that are NOT present in the composed prompt.

    The drift check: a rule added to one branch and not another shows up here
    instead of going quietly missing.
    """
    return [r.id for r in rules_for(stage) if r.text not in (composed or "")]
