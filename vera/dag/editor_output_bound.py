"""How much the editor may spend on the file it is editing.

Nothing bounded it. `llm.generate` grants `num_predict` the WHOLE context
window, so an edit of a 2KB file was free to spend 16384 tokens - and in census
run 20 it did: the third `code.edit` of `index.html` ran 1073s at 15.3 tok/s to
eval_count=16384, exactly 2**14, the ceiling. With the two before it that is
1728 seconds of editing against a 1500s wall cap, on a goal that produced
nothing.

WHY IT RUNS ON, measured 2026-08-31 against the real file from that run (74
lines, 2154 bytes) and the real coder (qwen2.5-coder:14b at temp 0.7, confirmed
via route_stats `loop_coder`, not the general 9B - `profile`/`role` do not
survive an /mcp/call boundary, which cost me two invalid arms before I checked):

    task shape                              runs  median s   median output
    the run-20 task (a whole-file spec)        9      86.6        5986 chars
    a narrow task ("add a Sound toggle")       4      12.8         864 chars

The editor is handed a `code.author` task - `_v5_route_write_call` RULE 1
redirects author to edit when the file has already run successfully, and passes
the task through verbatim - so it is asked for ten features at once and answers
with twice the file inside `replace` strings, anchored on finds of 6 to 8
characters. That is the run-on. It is not a sampling tail: seven of nine runs
land within a factor of two of each other.

A NEGATIVE RESULT worth not repeating: I rewrote that task at the redirect to
say plainly that the file already exists and that the spec was a list of
desired properties to diff against it, not a file to write. Eight runs of the
reframed task: median 100.5s and 5408 chars, against 86.6s and 5986 for the
task as it ships - marginally SLOWER, and the ranges are the same interval
(36.9-125.5s reframed, 52.0-124.2s not). Prompt framing does not shrink the work,
because the work really is ten features. That change was measured and dropped
rather than shipped on plausibility.

What DOES help is refusing to pay an unbounded price for it. The allowance below
is derived from the file: generous over any legitimate answer (the observed
usable 8-edit batch was ~1500 tokens against an allowance of 3692) and far under
the ceiling. On a large file it lands on exactly the ceiling llm.generate already
applies, so no legitimate large batch is truncated that is not truncated today.
"""

from __future__ import annotations

# Roughly 3.5 characters per token for source text - deliberately an
# UNDER-estimate of the token count, so the derived allowance errs generous.
CHARS_PER_TOKEN = 3.5
# A surgical edit batch six times the size of the file is not surgical.
HEADROOM = 6.0
# Never bound below this: a small file can still need one substantial edit.
FLOOR_TOKENS = 2048
# The ceiling llm.generate already applies (VERA_LLM_GEN_CTX, default 16384).
CEILING_TOKENS = 16384


def edit_num_predict(file_chars: int,
                     floor_tokens: int = FLOOR_TOKENS,
                     ceiling_tokens: int = CEILING_TOKENS) -> int:
    """Tokens the editor may spend on a file of `file_chars` bytes."""
    try:
        n = max(0, int(file_chars))
    except (TypeError, ValueError):
        n = 0
    allowance = int((n / CHARS_PER_TOKEN) * HEADROOM)
    return max(int(floor_tokens), min(int(ceiling_tokens), allowance))


def describe(file_chars: int) -> str:
    """One line for a log - what the bound is and what it is derived from."""
    return (f"editor output bounded to {edit_num_predict(file_chars)} tokens "
            f"for a {int(file_chars or 0)}-byte file")
