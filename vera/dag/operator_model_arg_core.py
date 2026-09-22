"""Pure check for the `model` an agent-loop step hands to the web operator.

Why this exists (2026-09-21, census run57, goal `author-then-edit`): the
executor model called `operator.run` with `provider: "local", model: "fast"`
- a model name it made up. The operator's thinker forwarded "fast" to Ollama,
which answered 404 `model 'fast' not found` on EVERY think, so the operator
could not decide anything and clicked the same control until its guards
fired: 978 s + 279 s of a 1800 s goal spent clicking blind. The url and the
missing goal of that same call had already been repaired by the loop's
argument self-heal; the model was not.

`heal_model_arg` is that repair. It knows nothing about Ollama - the caller
hands in the names the cluster actually serves - and it never touches a
model that IS served, so a deliberate choice always survives. With no
catalogue at all it does nothing: guessing that a model is missing is worse
than the 404 it would prevent.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# Capabilities whose `model` argument reaches the operator's thinker.
OPERATOR_MODEL_CAPS = frozenset({
    "operator.run", "operator.act", "operator.think", "browser.navigate",
})


# The same rule the routers use for "does this node have this model" - one
# definition, so a name that routes cannot be a name the operator rejects.
try:
    from Vera.vera.model_tag_core import variants as _variants
except Exception:  # pragma: no cover - worktree / test layout
    from ..model_tag_core import variants as _variants


def model_is_served(model: str, served: Iterable[str]) -> bool:
    """True when `model` names a model in `served`, treating `x` and
    `x:latest` as the same model (that is how Ollama resolves a bare tag)."""
    wanted = _variants(model)
    if not wanted:
        return False
    for s in served:
        if _variants(s) & wanted:
            return True
    return False


def split_provider(provider: Any) -> Tuple[str, str]:
    """`"ollama:fast-preview"` -> ("ollama", "fast-preview"); `"local"` ->
    ("local", ""). The same split the operator's thinker applies, so a model
    smuggled in through `provider` is seen here before it reaches Ollama."""
    p = str(provider or "").strip()
    if ":" in p:
        name, model = p.split(":", 1)
        return name.strip(), model.strip()
    return p, ""


def heal_model_arg(tool: str, args: Optional[Dict[str, Any]],
                   served: Iterable[str]) -> List[Tuple[str, Any, str]]:
    """Return [(field, new_value, note)] edits for `args` of `tool`.

    Only for the operator capabilities, only when the step SET a model - in
    `model`, or inside `provider` as "<name>:<model>" (census run59, 2026-09-22:
    `provider: "...:fast-preview"` walked straight past a check that read only
    `model`, and its three instant 404s took the GPU node offline) - and only
    when the catalogue is non-empty and does not contain it. The edit clears
    the model so the routed default applies; the note says what was dropped,
    for the run's event stream.
    """
    if tool not in OPERATOR_MODEL_CAPS or not isinstance(args, dict):
        return []
    catalogue = [str(s) for s in (served or []) if str(s or "").strip()]
    if not catalogue:
        return []
    edits: List[Tuple[str, Any, str]] = []
    model = str(args.get("model") or "").strip()
    if model and not model_is_served(model, catalogue):
        edits.append(("model", "", f"model {model!r} is not served by any Ollama node -> dropped "
                                   f"(the routed default applies; a made-up name 404s every think)"))
    pname, pmodel = split_provider(args.get("provider"))
    if pmodel and not model_is_served(pmodel, catalogue):
        edits.append(("provider", pname,
                      f"provider {str(args.get('provider')).strip()!r} names model {pmodel!r}, "
                      f"which no Ollama node serves -> provider {pname!r} (the routed default "
                      f"applies; a made-up name 404s every think)"))
    return edits


def is_model_not_found_error(err: Any) -> bool:
    """Ollama's 404 for an unknown model, in either of the shapes Vera
    surfaces it: the raw `model 'x' not found` or the wrapped exception text."""
    s = str(err or "").lower()
    return "not found" in s and "model" in s
