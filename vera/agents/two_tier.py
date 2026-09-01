"""Answer immediately from the question, then continue with the context.

Time to first token in chat is dominated by things that happen BEFORE the model
sees anything: assembling session memory, ontology fragments, retrieved Q&A, and
- when the web source is on - a blocking `web.search` the main generation is
gated on. The prompt that finally arrives is also large, so prompt-eval is long
before the first token appears. For a question that did not need any of it
("what does this error mean", "rewrite this sentence"), the user waits for
context they were never going to use.

The existing quick opener already splits the wait, but it is an ACKNOWLEDGEMENT
channel: it is told "Do NOT answer any part of the request", fires only above a
1500-character threshold, and produces one throwaway sentence. It hides latency
rather than removing it.

This is the other half: a first pass that actually ANSWERS, from a deliberately
small prompt, and a second pass that continues it with the full context - as one
message, not two. The model decides whether the second pass is needed, because
only it knows whether the question turned on something it was not given.

THE PROTOCOL, and it is the whole design:

  Tier 1 is told what it is missing and given one marker to emit. If it can
  answer from the question alone it answers and stops. If the question depends
  on the conversation, stored memory, or fetched material, it says what it can
  and ends with the marker.

  Tier 2 only runs if the marker appeared. It is given tier 1's text verbatim
  and told to continue it mid-flow - no greeting, no restating, no "as I
  mentioned". The transcript is one reply that happens to have been generated in
  two passes.

WHY THE MODEL DECIDES rather than a classifier: the cost of being wrong is
asymmetric and invisible from outside. Running tier 2 unnecessarily costs one
extra generation. NOT running it when it was needed produces a confident answer
from a model that was deliberately starved of the material - which is exactly
the failure mode where a wrong answer looks most convincing.

LEVELS - what tier 1 is starved of:

  off      no split; today's behaviour, one pass with everything.
  fetched  drop the RETRIEVED material (session memory, ontology, live context,
           web results, related past Q&A). Conversation history stays, so
           follow-ups still work. This is the useful default when memory is on,
           because memory is on by default and is the bulkiest fragment.
  message  tier 1 sees the user's message and nothing else - no history either.
           Fastest, and correspondingly the most likely to need tier 2.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Ordered from least to most aggressive.
LEVELS = ("off", "fetched", "message")

#: Opt-in. A split that fires by default would change every existing chat.
DEFAULT_LEVEL = "off"

#: WHO decides whether the second pass is needed. Both are kept so they can be
#: compared on the same traffic - they fail in opposite directions.
#:
#:   tier1  the first pass self-reports, by emitting CONTINUE_MARK. One
#:          generation when the answer was complete. But it is judging whether
#:          material it CANNOT SEE would have changed its answer, which is
#:          exactly the judgement a starved model is worst at - and a model that
#:          does not know what it is missing tends to feel finished.
#:
#:   tier2  the second pass decides, with the context in front of it, and says
#:          NO_ADDITION_MARK when the context adds nothing. Always costs the
#:          second generation, but the decision is made by the only stage that
#:          can actually see what was withheld.
DECIDERS = ("tier1", "tier2")
DEFAULT_DECIDER = "tier2"

#: Tier 2 emits this INSTEAD of a continuation when the context changes nothing.
#: It is checked as a prefix, so nothing reaches the user before it is ruled out.
NO_ADDITION_MARK = "[[NO-ADDITION]]"

#: What tier 1 emits when the question needs what it was not given. Chosen to be
#: something a model will not produce by accident and a user will never type.
CONTINUE_MARK = "[[NEEDS-CONTEXT]]"

#: Headings that introduce RETRIEVED material in the assembled system prefix.
#: Matching is on the heading line, so a fragment is dropped whole.
FETCHED_HEADINGS = (
    "## Session memory",
    "## Live context",
    "## Ontology",
    "## Capability mesh",
    "## Related past Q&A",
    "## Web results",
    "## Context pulled in",
    "## Retrieved",
)

_HEADING_RE = re.compile(r"^##\s+\S", re.M)


def normalise_level(level: Any) -> str:
    """Anything unrecognised means OFF - an unknown tuning value must not
    silently start starving prompts."""
    s = str(level or "").strip().lower()
    return s if s in LEVELS else DEFAULT_LEVEL


def normalise_decider(decider: Any) -> str:
    s = str(decider or "").strip().lower()
    return s if s in DECIDERS else DEFAULT_DECIDER


def is_fetched_heading(line: str) -> bool:
    head = str(line or "").strip()
    return any(head.startswith(h) for h in FETCHED_HEADINGS)


def strip_fetched_context(system_prefix: str) -> str:
    """Drop retrieved fragments, keep everything else.

    Sections are delimited by `## ` headings. Text before the first heading is
    the agent's own instruction and is always kept - starving tier 1 of who it
    is would change its voice, and the two passes have to read as one.
    """
    text = str(system_prefix or "")
    if not text.strip():
        return ""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return text
    out = [text[:matches[0].start()]]
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.start():end]
        if not is_fetched_heading(block.splitlines()[0] if block.splitlines() else ""):
            out.append(block)
    return "".join(out).strip()


def tier1_system_prefix(system_prefix: str, level: str) -> str:
    lvl = normalise_level(level)
    if lvl == "off":
        return str(system_prefix or "")
    if lvl == "message":
        return ""
    return strip_fetched_context(system_prefix)


def tier1_history(history: Optional[Sequence[Any]], level: str) -> List[Any]:
    """`message` drops the conversation too; `fetched` keeps it."""
    lvl = normalise_level(level)
    if lvl == "message":
        return []
    return list(history or [])


def tier1_instruction(level: str, decider: str = DEFAULT_DECIDER) -> str:
    """Tier 1 must know what it is missing, or it cannot judge whether to stop.

    Naming the omission precisely matters: a model told only "be quick" will
    guess at remembered facts rather than admit it was not given them.
    """
    lvl = normalise_level(level)
    if lvl == "off":
        return ""
    missing = ("the conversation so far, stored memory, and any retrieved or "
               "fetched material" if lvl == "message"
               else "stored memory and any retrieved or fetched material "
                    "(the conversation so far IS included)")
    if normalise_decider(decider) == "tier2":
        # Nothing is asked of tier 1 but a good answer. It is not qualified to
        # judge whether material it cannot see would have changed it, and asking
        # invites it either to hedge everything or to feel finished wrongly.
        return (
            "You are answering FIRST, before the slower context has loaded. You "
            f"have the user's message but NOT {missing}.\n"
            "Answer as fully and usefully as you can from what you have. Do not "
            "mention that anything is missing, do not hedge about what you might "
            "not know, and do not ask the user to wait - a second pass will "
            "continue your reply automatically if the context turns out to add "
            "anything. Just answer."
        )
    return (
        "You are answering FIRST, before the slower context has loaded. You have "
        f"been given the user's message but NOT {missing}.\n"
        "If the question can be answered from what you have, answer it fully and "
        "stop - do not mention that anything was withheld.\n"
        f"If it turns on something you were not given, say what you usefully can, "
        f"then end your reply with {CONTINUE_MARK} on its own line. Do not guess "
        "at remembered facts, file contents, or earlier turns to avoid emitting "
        "it - a confident wrong answer is worse than a continued one.\n"
        f"Never mention {CONTINUE_MARK} itself, and never explain that you are "
        "the first pass."
    )


def wants_continuation(text: str) -> bool:
    return CONTINUE_MARK in str(text or "")


def strip_marker(text: str) -> str:
    """Remove the marker and the blank space it sat in."""
    out = str(text or "").replace(CONTINUE_MARK, "")
    return re.sub(r"\n{3,}", "\n\n", out).rstrip()


def tier2_system_prefix(full_prefix: str, tier1_text: str,
                        decider: str = DEFAULT_DECIDER) -> str:
    """The full context, plus what tier 1 already said and how to continue it.

    Under the `tier2` decider this prompt also carries the DECISION: tier 2 is
    the only stage that can compare the context against what was already said,
    so it is told to emit NO_ADDITION_MARK and stop when the context adds
    nothing. Under `tier1` it only ever continues - the decision was already
    taken before it ran.
    """
    said = strip_marker(tier1_text).strip()
    decide = ""
    if normalise_decider(decider) == "tier2":
        decide = (
            "FIRST, DECIDE. You can now see context the first pass could not. If "
            "that context does not materially change or extend what is already "
            f"shown, reply with exactly {NO_ADDITION_MARK} and nothing else - no "
            "explanation, no punctuation. The reply already on screen then stands "
            "as the whole answer, which is the right outcome and costs the user "
            "nothing.\n"
            "Only if the context genuinely adds something - a fact that corrects "
            "or extends it, a detail it could not have known - continue as below. "
            "Do not continue merely to look thorough: repeating the same content "
            "in new words is worse than stopping.\n\n"
        )
    cont = decide + (
        "CONTINUATION. A first pass has ALREADY been shown to the user and is "
        "part of the same reply - the text below is on their screen now. Continue "
        "it directly, as one message.\n"
        "Do NOT greet, do NOT restate the question, do NOT summarise or repeat "
        "what is already there, and do NOT refer to it as a previous answer or "
        "say things like 'as I mentioned'. Pick up mid-flow: if it ends "
        "mid-thought, complete the thought; otherwise add what the context now "
        "makes possible.\n"
        "Start with a space or a newline as punctuation requires - the two halves "
        "are concatenated verbatim.\n\n"
        "--- already shown to the user ---\n"
        + (said or "(nothing yet)")
        + "\n--- end ---"
    )
    base = str(full_prefix or "").strip()
    return (base + "\n\n" + cont).strip() if base else cont


def plan(level: Any, system_prefix: str,
         history: Optional[Sequence[Any]] = None,
         decider: Any = DEFAULT_DECIDER) -> Dict[str, Any]:
    """Everything the caller needs for tier 1, decided in one place."""
    lvl = normalise_level(level)
    dec = normalise_decider(decider)
    if lvl == "off":
        return {"split": False, "level": lvl, "decider": dec,
                "system_prefix": str(system_prefix or ""),
                "history": list(history or []), "instruction": "",
                "always_run_tier2": False}
    prefix = tier1_system_prefix(system_prefix, lvl)
    instr = tier1_instruction(lvl, dec)
    return {
        "split": True, "level": lvl, "decider": dec,
        "system_prefix": (prefix + "\n\n" + instr).strip() if prefix else instr,
        "history": tier1_history(history, lvl),
        "instruction": instr,
        # Under tier2 the second pass must RUN in order to decide; under tier1
        # it runs only when the first pass asked for it.
        "always_run_tier2": dec == "tier2",
    }
