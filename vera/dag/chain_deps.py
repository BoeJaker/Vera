"""Which chain hops actually DEPEND on a failed hop.

`_run_chain` runs an agent-authored pipeline of cap calls, piping each hop's
output into the next via `$N` references. When a hop failed it did:

    if not entry_ok:
        # A broken hop poisons everything downstream - stop the pipeline.
        break

That is true only of hops that USE the failed output. A pipeline like

    0: web.search   1: web.fetch($0)   2: prose.author($1)   3: notes.write("...")

loses hop 3 as well when hop 1 fails, even though hop 3 never referenced it -
so a whole step is abandoned over an unrelated failure, and the loop then burns
cycles rediscovering the parts that would have worked.

This module answers only the question worth testing: given the set of failed hop
indices, does THIS hop reference any of them? Pure - no app import, no I/O.

Poison is TRANSITIVE: if hop 2 is skipped because it needed failed hop 1, then
hop 2's own output is absent too, so anything referencing $2 must also be
skipped. The caller adds each skipped hop's index to the poisoned set.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Set

# `$N`, `$N.a.b`, `$N:code`, `$N:json` - the reference forms _chain_ref accepts.
_REF_RE = re.compile(r"\$(\d+)(?:[:.][^\s\"']*)?")


def referenced_indices(value: Any) -> Set[int]:
    """Every `$N` index reachable anywhere inside `value`.

    Walks dicts/lists as well as strings, because a reference can sit in a
    nested arg (`{"body": {"text": "$2"}}`), not just at the top level.
    """
    out: Set[int] = set()
    if isinstance(value, str):
        out.update(int(m) for m in _REF_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            out |= referenced_indices(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= referenced_indices(v)
    return out


def hop_references(hop: Any) -> Set[int]:
    """Indices a hop depends on: its `from` map, its `when` guard, and its args.

    All three are resolved through _chain_ref at run time, so all three can
    carry a `$N`. `name` is excluded - a tool name is not a data dependency.
    """
    if not isinstance(hop, dict):
        return set()
    refs: Set[int] = set()
    for key in ("from", "when", "args", "input"):
        if key in hop:
            refs |= referenced_indices(hop.get(key))
    for k, v in hop.items():
        if k not in ("name", "from", "when", "args", "input"):
            refs |= referenced_indices(v)
    return refs


def blocked_by(hop: Any, poisoned: Iterable[int]) -> Optional[int]:
    """The lowest failed index this hop needs, or None if it is independent.

    Returning the INDEX rather than a bool so the caller can say which hop
    caused the skip - "depends on failed hop $1" is actionable, "skipped" is not.
    """
    bad = set(poisoned or ())
    if not bad:
        return None
    hit = hop_references(hop) & bad
    return min(hit) if hit else None
