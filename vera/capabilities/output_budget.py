"""When the caller asks for 200 words, do not let the model write 16,000.

`llm.generate` grants a generous bounded window and then sets
``num_predict = num_ctx`` - the whole window - on the reasoning that "the model
still stops early at a natural EOS for short answers". Census run 18 is the
counter-example, and it cost a goal:

    prose-only, "Write a 200-word explainer of what a race condition is"
      prose.author  prompt=2644 chars -> eval_count=6182   (333s)
      prose.author  prompt=2750 chars -> eval_count=13215  (738s)
      prose.author  prompt=2908 chars -> eval_count=16384  (949s)

16384 is 2**14 - the ceiling itself. The prompt barely moved; the OUTPUT ran
away, three times, until a single call consumed the goal's entire 25-minute
budget at ~17 tok/s. The step then wall-capped having re-authored four times.

So when the request states a length, size the budget to it. The stated length is
a fact about what was asked, not a guess: "200-word", "three paragraphs", "two
sentences". Everything else is unchanged - no stated length means the existing
full-window behaviour, because that is what protects long structured outputs
(big JSON, whole source files) from the truncation this ceiling was raised to
fix in the first place.

The headroom is deliberately generous. The point is to stop a 200-word request
becoming a 16,000-token essay, not to hold the model to an exact count - a
slightly-over answer is fine, a 60x overrun is not.

Pure: text in, number out. No I/O.
"""

from __future__ import annotations

import re
from typing import Optional

#: Tokens per English word, rounded up. Real ratio is ~1.3; 2.0 keeps the
#: estimate on the safe side of truncation.
TOKENS_PER_WORD = 2.0

#: Rough sizes for structural units, in words.
WORDS_PER_PARAGRAPH = 120
WORDS_PER_SENTENCE = 25

#: Multiplied over the estimate so a slightly longer answer is never cut off.
HEADROOM = 4.0

#: Never propose a budget below this - a tiny parse must not starve a response.
FLOOR_TOKENS = 512

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a": 1, "an": 1,
}

#: Two digits minimum for a bare numeral, deliberately. "the top 5 words in
#: the file" is a request ABOUT words, not a request FOR five words, and a false
#: positive here truncates real work - the exact failure the full window was
#: introduced to prevent. Spelled-out numbers ("two sentences") are kept because
#: they only appear when a length is genuinely being stated.
_WORDS_RE = re.compile(
    r"(?:^|[^\w])(\d{2,6}|" + "|".join(_NUMBER_WORDS) + r")[\s-]*word", re.I)
_PARA_RE = re.compile(
    r"(?:^|[^\w])(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")[\s-]*paragraph", re.I)
_SENT_RE = re.compile(
    r"(?:^|[^\w])(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")[\s-]*sentence", re.I)


def _as_int(tok: str) -> Optional[int]:
    tok = (tok or "").strip().lower()
    if tok.isdigit():
        return int(tok)
    return _NUMBER_WORDS.get(tok)


def requested_words(text: str) -> Optional[int]:
    """Words the request explicitly asks for, or None.

    Only an explicit statement counts. Ambiguity returns None, which leaves the
    caller on the existing unbounded-window path - the safe direction, since a
    wrong small number truncates real work.
    """
    if not text:
        return None
    for rx, per in ((_WORDS_RE, 1), (_PARA_RE, WORDS_PER_PARAGRAPH),
                    (_SENT_RE, WORDS_PER_SENTENCE)):
        hits = [n for n in (_as_int(m.group(1)) for m in rx.finditer(text)) if n]
        if hits:
            # The largest stated figure: "3 to 5 paragraphs" should budget for 5.
            return max(hits) * per
    return None


def budget_tokens(text: str, *, ceiling: int,
                  floor: int = FLOOR_TOKENS) -> Optional[int]:
    """num_predict for this request, or None to leave the caller's default.

    Never exceeds `ceiling` (the existing window) and never drops below `floor`.
    """
    words = requested_words(text)
    if not words or ceiling <= 0:
        return None
    est = int(words * TOKENS_PER_WORD * HEADROOM)
    return max(min(est, int(ceiling)), min(int(floor), int(ceiling)))


def describe(text: str, *, ceiling: int) -> str:
    """One log line explaining a bounded generation, so a short output is
    never a mystery."""
    words = requested_words(text)
    budget = budget_tokens(text, ceiling=ceiling)
    if not budget:
        return ""
    return (f"output budget: request states ~{words} words -> num_predict="
            f"{budget} (ceiling {ceiling})")
