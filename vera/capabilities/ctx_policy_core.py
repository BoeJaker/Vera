"""
ctx_policy_core.py — one context budget, decided in one place.

Pure: numbers in, numbers out. No HTTP, no ollama, no Vera imports. Pinned by
tests/test_ctx_policy.py.

`num_ctx`, `num_predict` and `num_keep` are three faces of ONE budget — the
window, how much of it output may consume, and how much of the prompt survives
when the window fills. Vera set them in four different places and never set the
third at all, so they could not agree.

The headline defect (capabilities.py:1986): `num_predict = num_ctx`. The prompt
already occupies part of the window, so that asks for more output than can
physically fit. For loop_executor — measured at 43,254 prompt chars, roughly
11-13k tokens — it permitted 28,672 output tokens into ~16,000 tokens of room.

What happens on overflow is the part that makes it expensive. Every ollama
runner on this estate launches with `--context-shift --keep 4`, and llama.cpp
documents `--keep` as "number of tokens to keep from the initial prompt". At 4,
a generation that outruns its window does not stop — it keeps producing, having
discarded most of the prompt it was given. On a CPU node at ~0.05 tok/s that is
days of work for an answer nobody is waiting for, which is exactly what was
found burning twelve cores on cpu-247 on 2026-09-16.

MEASURED CAVEAT, so this is not overstated: a shift does not reliably destroy
instruction-following. A formatting instruction ("start every sentence with
ZEPHYR") survived a forced shift at keep=4 with compliance close to the
unshifted control (9-10 markers per half against 12-13). The likely reason is
self-reinforcement — the model's own retained output still demonstrates the
pattern, so losing the system prompt does not change what it writes next.
Whether a fact that appears ONLY in the prompt survives is UNPROVEN: three
further designs each failed for a different instrumentation reason, not for
lack of trying. So the cost of a shift is established (wasted compute on an
unbounded generation) while the quality cost is not. That is why `fit` bounds
output rather than relying on `num_keep` to make shifting safe.

So `fit` is the default mode here: bound output to what actually fits.

    num_predict = num_ctx - prompt_tokens - margin

That cannot truncate valid output, because anything past that point could only
have been produced by evicting the prompt. It is not a restriction on the
model; it is the line where the answer stops being grounded in the question.

`longform` exists for the case shifting was designed for — output deliberately
longer than the window. It is NOT the default, and deliberately so: whether a
larger `num_keep` measurably preserves instruction-following across a shift is
UNPROVEN (the behavioural test was confounded by a model that ignored the
instruction before any shift occurred). See the PLAN. Until that is settled,
`longform` still bounds output — it simply bounds it to the caller's stated
intent rather than to the window.

One hard constraint, measured: `num_ctx` and `num_keep` are baked into the
runner at LAUNCH (a request with num_ctx=777, num_keep=137 spawned
`llama-server -c 777 --context-shift --keep 137`). So those two are a stable
per-role profile — varying them per call forces a model reload and a second
resident copy. `num_predict` is free to vary per request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Dict, Optional

#: Characters per token when no tokenizer is available. Deliberately LOW (a
#: conservative estimate OVER-counts tokens, which shrinks num_predict and stays
#: on the safe side of the window). Real English is ~4; 3.5 buys margin.
CHARS_PER_TOKEN = 2.3

#: Headroom left between prompt+output and the window edge. Absorbs tokenizer
#: disagreement and the chat template's own wrapper tokens. Undercounting the
#: prompt silently returns us to shifting, so this is not shaved thin.
DEFAULT_MARGIN = 256

#: Never hand back a bound below this: a tiny window should fail loudly as a
#: too-small ctx, not as mysteriously clipped answers.
MIN_PREDICT = 256

#: Modes.
FIT = "fit"
LONGFORM = "longform"


@dataclass
class CtxPlan:
    num_ctx: int
    num_predict: int
    num_keep: Optional[int] = None
    mode: str = FIT
    reason: str = ""

    def options(self) -> Dict[str, int]:
        """Just the ollama option keys, for merging into a request."""
        out: Dict[str, int] = {"num_ctx": self.num_ctx,
                               "num_predict": self.num_predict}
        if self.num_keep is not None:
            out["num_keep"] = int(self.num_keep)
        return out


def window_ceiling(*, has_gpu: bool, vram_gb: float, detected_max: int,
                   gpu_ceiling: int, floor: int = 2048) -> int:
    """Largest context window to offer a model on this node.

    CPU node -> the model's full window. These nodes have 50 GB of system RAM
    and no spill cliff: the KV cache simply lives in RAM, so the GPU-shaped
    ceiling does not apply. This is what lets a big prompt run UNSHIFTED on a
    CPU node that would have to shift on the card.

    GPU node -> the configured ceiling, because VRAM is the binding constraint.
    A 12 GB card holds ~28-65k tokens of KV for a 9B model; the model's own
    window may be 262,144.

    GPU with UNKNOWN VRAM -> still the ceiling. This is the case that was wrong
    (2026-09-16): unknown VRAM fell through to the CPU branch and returned the
    model's full window, so gpu-250 — which reports no vram_gb from the catalog
    — was being offered 262,144 on a 12.3 GB card, where the KV cache alone
    would be 32 GB. "We don't know" must mean the ceiling, never no ceiling.
    """
    if not has_gpu:
        return max(int(floor), int(detected_max) or int(floor))
    ceiling = int(gpu_ceiling) or int(detected_max) or int(floor)
    return max(int(floor), min(int(detected_max) or ceiling, ceiling))


def did_shift(prompt_tokens: int, eval_count: int, num_ctx: int) -> bool:
    """Did this generation overrun its window and shift?

    The only detector available: llama.cpp does not log the shift anywhere
    reachable (a deliberately forced shift logged nothing on any node), but
    ollama returns both counts on every response. Validated 2026-09-16 —
    forced 23+700=723 against num_ctx=320 -> True; 23+200=223 against 4096 ->
    False.
    """
    if not num_ctx or not prompt_tokens:
        return False
    return (int(prompt_tokens) + int(eval_count or 0)) > int(num_ctx)


def measured_chars_per_token(prompt_chars: int, prompt_tokens: int) -> Optional[float]:
    """The real ratio for one observed call, or None if it cannot be computed."""
    if not prompt_chars or not prompt_tokens:
        return None
    return float(prompt_chars) / float(prompt_tokens)


#: Shave this off a measured ratio before using it. The measurement is an EMA
#: over a route whose content varies call to call, so the average alone would be
#: wrong half the time — in the direction that overruns. 0.9 buys ~11% of token
#: over-count on top of the explicit window margin.
MEASURE_DISCOUNT = 0.9


def safe_chars_per_token(measured: Optional[float], *,
                         default: float = CHARS_PER_TOKEN,
                         floor: float = 1.2,
                         discount: float = MEASURE_DISCOUNT) -> float:
    """Pick the ratio to size a window with.

    With no measurement, the conservative default — under-counting the prompt
    overruns the window and shifts, so an unmeasured route must assume dense
    content.

    With a measurement, TRUST IT, discounted. Clamping a measurement down to the
    default would make the measurement pointless: it could only ever shrink the
    window, so a route that genuinely sends prose (measured 4.43 chars/token)
    would be charged the dense-code rate of 2.3 and lose most of its window for
    nothing. The point of measuring is to be accurate, not merely timid.

    The floor stops a pathological sample (a prompt of mostly rare unicode)
    shrinking the window to nothing.
    """
    if not measured or measured <= 0:
        return float(default)
    return max(float(floor), float(measured) * float(discount))


def output_room(*, global_max: int, node_ceiling: int, want_predict: int = 0,
                reserve: int = 1024, margin: int = DEFAULT_MARGIN) -> int:
    """Tokens of OUTPUT the window must be sized to hold for THIS call.

    The auto-fit used to reserve the flat global maximum (16,384) on every
    call, so any prompt at all rounded up to a 24,576-token window - and a
    five-word chat title loaded a 0.5b model on a CPU box with a 24k KV cache
    and an 8 GB prompt cache (54 s, 2026-09-23). The node's own ceiling and the
    caller's pinned `num_predict` were only applied to num_predict AFTERWARDS,
    once the window had already been sized for a report.

    So: the smallest of the global max, the node's ceiling, and - when the
    caller pinned a positive num_predict - that pin plus a margin. Never below
    `reserve`, so a call that states no intent still gets real room. A pinned
    num_ctx is handled by the caller as a FLOOR on the whole window, so a role
    that deliberately wants a big window keeps it.
    """
    cands = [int(c) for c in (global_max, node_ceiling) if c and int(c) > 0]
    room = min(cands) if cands else int(reserve)
    want = int(want_predict or 0)
    if want > 0:
        room = min(room, want + int(margin))
    return max(int(reserve), room)


#: Job types whose whole output is a few words. A routing rule for one of these
#: that names NO model lets the instance default through - the 9b - and a
#: sandbox's saved profile did exactly that onto a CPU node: one chat title ran
#: for nine hours (2026-09-23, judgement 18). Every node carries the 0.5b.
UTILITY_JOB_TYPES = frozenset({"naming"})
UTILITY_DEFAULT_MODEL = "qwen2.5:0.5b"


def utility_model(job_type: str, rule_model: str, served: "Iterable[str]" = ()) -> str:
    """The model a utility job should use when its rule names none.

    Only for UTILITY_JOB_TYPES, only when the rule left `model` empty, and only
    when the small model is actually served (an empty `served` means unknown
    and is trusted). Anything else returns "" and the caller's own default
    applies - a deliberate model choice is never overridden.
    """
    if str(job_type or "") not in UTILITY_JOB_TYPES:
        return ""
    if str(rule_model or "").strip():
        return ""
    names = [str(x) for x in (served or ())]
    if names and not any(n == UTILITY_DEFAULT_MODEL or n.startswith(UTILITY_DEFAULT_MODEL + ":")
                         for n in names):
        return ""
    return UTILITY_DEFAULT_MODEL


def apply_ceiling(want: int, ceiling: int) -> int:
    """A caller's `num_ctx_max` bounds the window from ABOVE.

    `llm.generate` used to pass its generous default (16,384) as `num_ctx`,
    and the auto-fit treats a caller's num_ctx as a FLOOR - the right reading
    for a role that deliberately wants a big window, the wrong one for a
    default meant as "at most". So a five-word chat title got a 16k window on a
    CPU box. A ceiling can only lower; 0 means none.
    """
    w = int(want or 0)
    c = int(ceiling or 0)
    if c <= 0:
        return w
    return min(w, c)


def output_bound(*, num_ctx: int, prompt_tokens: int, ceiling: int,
                 margin: int = DEFAULT_MARGIN, floor: int = 512) -> int:
    """num_predict that fits: window - prompt - margin, under the ceiling.

    The margin is the part the old inline code lacked (`num_ctx - prompt_tok`,
    nothing reserved), so any tokenizer disagreement landed on the window edge.
    """
    room = int(num_ctx) - int(prompt_tokens) - int(margin)
    return max(int(floor), min(int(ceiling), room))


#: num_keep as a fraction of the window: ctx // KEEP_DIVISOR, capped.
#: At 24576 that keeps 3072 tokens — comfortably more than any system prompt
#: Vera builds, without eating the window.
KEEP_DIVISOR = 8
KEEP_CAP = 4096


def keep_tokens(num_ctx: int, *, divisor: int = KEEP_DIVISOR,
                cap: int = KEEP_CAP) -> Optional[int]:
    """num_keep: how much of the prompt survives a context shift.

    Runners default to ollama's `--keep 4`, which discards the system prompt on
    the first shift — the model carries on having forgotten its instructions.

    **A pure function of num_ctx, deliberately.** Measured 2026-09-16: num_keep
    is baked into the runner when it is FIRST SPAWNED for a given
    (model, num_ctx), and silently ignored thereafter. Sending 8, then 512, then
    8 at the same model and ctx produced ONE new runner, at keep=8 — the 512 was
    dropped on the floor.

    So sizing this from the current call's system prompt (the obvious idea, and
    the first thing tried here) makes the value non-deterministic: whichever
    request happened to spawn the runner wins, and every later call's value is
    discarded. Deriving it from num_ctx alone means every call for a given
    (model, ctx) agrees on it, so the runner gets a predictable value and no
    extra runners are spawned — (model, ctx, keep) stays one identity.
    """
    if not num_ctx or num_ctx <= 0:
        return None
    keep = min(int(num_ctx) // max(1, int(divisor)), int(cap))
    keep = min(keep, max(0, int(num_ctx) // 2))
    return keep if keep > 0 else None


def estimate_tokens(text: str, *, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """Token estimate from character count. Only for when the node cannot be
    asked — /api/tokenize is exact and should be preferred."""
    if not text:
        return 0
    return int(len(text) / max(0.5, float(chars_per_token))) + 1


def resolve(*, num_ctx: int, prompt_tokens: int, mode: str = FIT,
            system_tokens: int = 0, want_predict: int = 0,
            margin: int = DEFAULT_MARGIN,
            min_predict: int = MIN_PREDICT) -> CtxPlan:
    """The whole policy.

    `want_predict` is the caller's own intent (0 = none stated). In `fit` it is
    honoured only where it is SMALLER than what fits — a caller may ask for
    less, never for more than the window holds. In `longform` it is the point of
    the mode and is honoured as given.

    `system_tokens` sizes `num_keep` so the instructions are the part of the
    prompt that survives a shift. Emitted whenever it is known: strictly better
    than the runner default of 4, and harmless when no shift occurs.
    """
    ctx = max(0, int(num_ctx or 0))
    ptoks = max(0, int(prompt_tokens or 0))
    want = max(0, int(want_predict or 0))
    if ctx <= 0:
        # Nothing to divide up: hand back the caller's intent untouched rather
        # than inventing a bound from a window we do not know.
        return CtxPlan(num_ctx=0, num_predict=want, mode=mode,
                       reason="no num_ctx known — caller's num_predict left alone")

    keep = None
    if system_tokens > 0:
        # Never let keep swallow the window: it must leave room to generate.
        keep = int(min(int(system_tokens), max(0, ctx // 2)))
        if keep <= 0:
            keep = None

    if mode == LONGFORM:
        # Output is MEANT to exceed the window; shifting is the mechanism. Bound
        # it to what the caller actually intends, never to the window, and keep
        # the instructions across the shift.
        predict = want if want > 0 else ctx
        return CtxPlan(num_ctx=ctx, num_predict=int(predict), num_keep=keep,
                       mode=LONGFORM,
                       reason=(f"longform: bounded by caller intent "
                               f"({predict} tokens), keep={keep}"))

    # ── fit ──────────────────────────────────────────────────────────────────
    room = ctx - ptoks - int(margin)
    if room < min_predict:
        # The prompt has already eaten the window. Clamping to `room` here would
        # produce a stub answer and look like a model fault, so surface the real
        # condition: this call needs a bigger ctx or a smaller prompt.
        predict = int(min_predict)
        return CtxPlan(num_ctx=ctx, num_predict=predict, num_keep=keep, mode=FIT,
                       reason=(f"prompt {ptoks} + margin {margin} leaves only "
                               f"{max(0, room)} of {ctx} — at the {min_predict} "
                               f"floor; raise num_ctx or shorten the prompt"))
    predict = int(room)
    why = f"fit: {ctx} window - {ptoks} prompt - {margin} margin"
    if 0 < want < predict:
        predict = want
        why = f"caller asked for {want}, under the {room} that fits"
    return CtxPlan(num_ctx=ctx, num_predict=predict, num_keep=keep, mode=FIT,
                   reason=why)
