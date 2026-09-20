"""Pick a chat context window that does not force an ollama runner reload.

ollama keys a loaded runner by (model, num_ctx). Asking for a *different*
num_ctx than the runner already holding the model evicts it and loads another
one. On a node whose VRAM fits exactly one runner that is not a tuning
inefficiency, it is a thrash:

    chat asks for the node maximum  -> evicts the loop/research runner
    loop/research asks for its fit  -> evicts the chat runner
    ...

which is what happened on gpu-250 (Tesla V100-PCIE-**12GB**, sharing the card
with gpu-inference.service) on 2026-09-20. `ollama_generate` already sizes its
window to the prompt (`_AUTO_CTX_FIT` in capability_orchestration.py), but the
chat path in agents.py asked for `effective_num_ctx(...)` — the node maximum —
on every turn regardless of prompt size, so a six-token question requested a
16384-token window. Every chat turn forced a reload; repeated reloads that could
not be satisfied eventually deadlocked ollama's scheduler outright, and because
`/api/ps`, `/api/tags` and `/api/version` do not go through that scheduler,
every health probe Vera had kept reporting the node online and idle.

The fix is in two parts, both here:

  * `stable_chat_num_ctx` — reuse the window of a runner that is ALREADY loaded
    whenever it is big enough for this turn and no larger than we are allowed to
    use. That is the part that actually stops the thrash, and it can never
    truncate: we only ever snap *up* to an existing window.
  * when nothing suitable is resident, round the requirement UP to a coarse
    step so successive turns converge on one value instead of drifting through
    a new window (and therefore a new runner) every time.

`stable_chat_num_ctx` is deliberately pure — no I/O, no clock, no globals — so
it is unit-testable as `vera.agents.chat_ctx_core` without importing the app
(see the namespace-package import trap in the improve-vera-sandboxed skill).
"""
from typing import Any, Dict, List, Optional

# Granularity for a fresh window. Matches _CTX_STEP in capability_orchestration:
# coarse enough that ordinary turns land on the same value repeatedly.
CTX_STEP = 4096

# Smallest window worth asking for; mirrors _CTX_FLOOR in capability_orchestration.
CTX_FLOOR = 4096


def _round_up(n: int, step: int) -> int:
    """Smallest multiple of `step` that is >= n (step <= 0 disables rounding)."""
    if step <= 0:
        return int(n)
    return ((int(n) + step - 1) // step) * step


def stable_chat_num_ctx(needed: int,
                        cap: int,
                        resident: int = 0,
                        floor: int = CTX_FLOOR,
                        step: int = CTX_STEP) -> int:
    """The num_ctx to request for a chat turn.

    Parameters
    ----------
    needed   : tokens this turn actually needs (prompt + room for the reply).
    cap      : the node-safe ceiling — `effective_num_ctx(...)`, which already
               folds in the agent's own num_ctx and OLLAMA_MAX_AUTO_CTX. The
               result never exceeds it (when cap > 0).
    resident : context_length of a runner already loaded for this model on this
               node, or 0 when nothing suitable is loaded.
    floor    : never ask for less than this.
    step     : rounding granularity for a fresh window.

    Returns
    -------
    A positive num_ctx. Reuses `resident` when it fits the turn and the cap —
    which is what avoids the reload — otherwise the rounded-up requirement,
    clamped to [floor, cap].
    """
    needed = max(int(needed or 0), 0)
    cap = int(cap or 0)
    resident = max(int(resident or 0), 0)
    floor = max(int(floor or 0), 1)

    # The floor applies to the requirement before anything else, so a tiny turn
    # still asks for a sensible window rather than a few hundred tokens.
    want = max(needed, floor)

    # Reuse ANY already-loaded window that covers this turn — including one
    # LARGER than `cap`.
    #
    # The first cut refused those, and that left the thrash in place: gpu-250
    # kept a 24576 runner loaded by other callers, chat's cap was the agent's
    # 16384, so every turn still forced a reload (measured 2026-09-20: 102s,
    # 150s timeout, 88s for one-word replies). `cap` bounds the window we would
    # ask to CREATE; a bigger one that already exists costs nothing to use, and
    # the reload it avoids is the whole problem.
    #
    # It does NOT license sending more context than the agent allows: the caller
    # compacts to min(cap, window), so the operator's num_ctx still bounds the
    # prompt. Only the allocation is borrowed.
    if resident and resident >= want:
        return resident

    want = _round_up(want, step)
    if cap > 0:
        want = min(want, cap)
    # A cap below the floor is the operator's explicit choice (a deliberately
    # tiny agent window); honour it rather than overriding it upward.
    return max(want, min(floor, cap) if cap > 0 else floor)


def resident_ctx_from_ps(ps: Optional[Dict[str, Any]], model: str) -> int:
    """context_length of `model`'s loaded runner, from an /api/ps payload.

    Matches the exact tag first, then the tag-insensitive base name, because
    ollama serves several tags of one blob (`jaahas/qwen3.5-uncensored`,
    `:latest`, `:9b` are all digest 155911794292... on gpu-250) and any of them
    may be the one currently resident. Adopting that runner's *window* is safe
    whichever tag loaded it — the model we request is unchanged.

    Returns 0 when nothing matches or the payload is unusable.
    """
    if not isinstance(ps, dict) or not model:
        return 0
    rows: List[Dict[str, Any]] = ps.get("models") or []
    if not isinstance(rows, list):
        return 0

    def _ctx(row: Any) -> int:
        if not isinstance(row, dict):
            return 0
        try:
            return max(int(row.get("context_length") or 0), 0)
        except (TypeError, ValueError):
            return 0

    base = str(model).split(":")[0]
    fallback = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("model") or "")
        if name == model:
            got = _ctx(row)
            if got:
                return got
        if not fallback and name.split(":")[0] == base:
            fallback = _ctx(row)
    return fallback
