"""Whether two model names name the same model, in one place.

Ollama resolves a bare name to its `:latest` tag, and nothing else: `qwen2.5`
and `qwen2.5:latest` are one model, `qwen2.5:7b` and `qwen2.5:0.5b` are two.

The routers did not agree. Both copies of `_has_model` - the orchestrator's and
the load-aware patch that replaces it - counted a tag as present when the BASE
name matched:

    if m == mdl or m.startswith(mdl + ":") or m.split(":")[0] == mdl_base:

so a request for `qwen2.5:7b` was "satisfied" by a node holding only
`qwen2.5:0.5b` - a 7-billion-parameter coder answered by a half-billion one, or
more likely a 404 from a node that was chosen because it looked ready. Every
node happens to carry both tags today, which is the only reason it has not
bitten; it is the same class of fault as routing on a name the node does not
serve (2026-09-22, where three such 404s took the GPU out of rotation and spilled
a 35k-char call onto a CPU node for 45 minutes).

`vera/dag/operator_model_arg_core.py` already had the right rule, written for
the operator's made-up-model repair. This is that rule, shared, so there is one
answer to "does this node have this model" instead of three.

Pure: strings in, booleans out.
"""
from __future__ import annotations

from typing import Iterable, Set


def variants(name: str) -> Set[str]:
    """The spellings that name this exact model.

    A bare name and its `:latest` tag are the same model; every other tag is
    its own model. A repository path is kept intact - the tag is whatever
    follows the LAST colon after the final `/`.
    """
    n = str(name or "").strip()
    if not n:
        return set()
    out = {n}
    tail = n.rsplit("/", 1)[-1]
    if n.endswith(":latest"):
        out.add(n[: -len(":latest")])
    elif ":" not in tail:
        out.add(n + ":latest")
    return out


def same_model(a: str, b: str) -> bool:
    """True when two names resolve to the same model."""
    va, vb = variants(a), variants(b)
    return bool(va and vb and (va & vb))


def is_served(model: str, served: Iterable[str]) -> bool:
    """True when `model` is one of the models a node actually lists.

    An empty `model` means "no preference" and is served by anyone - that is
    how the callers use it.
    """
    if not str(model or "").strip():
        return True
    wanted = variants(model)
    for s in served or ():
        if variants(s) & wanted:
            return True
    return False
