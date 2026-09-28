"""The capabilities a goal's INTENT actually uses, moved to the front of the
agent loop's catalogue once the intent is known.

Measured 2026-09-28 over 407 loops (Redis vera:loop:events, census + chat):
the catalogue is not missing tools (calls outside the offered toolkit: build
4/249, action 2/82, research 0/54), but it is long (mostly 30-39 caps) and
several stages read only its head - the fast path the first 16, the master
planner and the tier brief the first 24, broad the first 40. The caps an
intent leans on sat late: prose.author (92% of research runs) was past
position 16 in all 54 research runs; code.author (77% of build runs) sat at
median position 14 and past 16 in 53/249 build runs.

So this ORDERS, it does not trim: the caps the goal names in full keep the
front, then the intent's core, then everything else in the order it had.
A core cap missing from the catalogue is added only when it is registered and
not blocked. Nothing is removed. `mixed` (or an unknown intent) has no core.

Pure: the caller hands in the registry names and the blocked set.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: Per intent, most-used first (share of runs using the cap, 2026-09-28):
#: build    code.author 77, fs.read 46, exec.bash 43, code.edit 33, operator.run 30,
#:          prose.author 27, exec.python 20
#: research web.research 100, prose.author 92, fs.read 75, web.fetch 70,
#:          exec.bash 36, web.search 26
#: action   exec.bash 100, code.author 57, exec.python 54, code.edit 50, fs.read 50,
#:          prose.author 26
INTENT_CORES: Dict[str, Tuple[str, ...]] = {
    "build": ("code.author", "sandbox.session.fs.read", "exec.bash.run", "code.edit",
              "operator.run", "prose.author", "exec.python.run"),
    "research": ("web.research", "prose.author", "sandbox.session.fs.read", "web.fetch",
                 "exec.bash.run", "web.search"),
    "action": ("exec.bash.run", "code.author", "exec.python.run", "code.edit",
               "sandbox.session.fs.read", "prose.author"),
}

MODES = ("off", "order")

#: The fast path reads the first 16 - the smallest head any stage reads.
FRONT = 16

_CAP_NAME = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\b")


def resolve_mode(value: Any) -> str:
    """'order' or 'off' (anything unrecognised is off)."""
    v = str(value or "").strip().lower()
    return v if v in MODES else "off"


def named_caps(goal: str, catalog: Iterable[str]) -> List[str]:
    """Catalogue caps the goal names in full, in catalogue order."""
    said = set(_CAP_NAME.findall(str(goal or "").lower()))
    return [c for c in catalog if c.lower() in said]


def apply(catalog: Sequence[str], intent: str, goal: str = "", *,
          known: Optional[Iterable[str]] = None,
          blocked: Optional[Iterable[str]] = None,
          front: int = FRONT) -> Tuple[List[str], Dict[str, Any]]:
    """The catalogue with the intent's core at the front.

    Order: caps the goal names in full, then the core (most-used first), then
    the rest as they were. A core cap not in the catalogue is added when it is
    in `known` (the registry; None = trust the core) and not in `blocked`.
    Returns (order, info): info carries what moved, what was added and the
    positions before/after, for the loop's event.
    """
    cat = [str(c) for c in (catalog or []) if str(c or "").strip()]
    cat = list(dict.fromkeys(cat))
    it = str(intent or "").strip().lower()
    core = INTENT_CORES.get(it, ())
    info: Dict[str, Any] = {"intent": it, "core": list(core), "moved": [], "added": [],
                            "before": {}, "after": {}}
    if not core:
        return cat, info
    known_set: Optional[Set[str]] = set(known) if known is not None else None
    blocked_set = set(blocked or ())
    have = set(cat)
    added = [c for c in core if c not in have and c not in blocked_set
             and (known_set is None or c in known_set)]
    usable = [c for c in core if c in have or c in added]
    head = [c for c in named_caps(goal, cat) if c not in usable]
    lead = head + usable
    lead_set = set(lead)
    order = lead + [c for c in cat if c not in lead_set]
    pos_before = {c: i for i, c in enumerate(cat)}
    pos_after = {c: i for i, c in enumerate(order)}
    info["added"] = added
    info["before"] = {c: pos_before[c] for c in usable if c in pos_before}
    info["after"] = {c: pos_after[c] for c in usable}
    info["moved"] = [c for c in usable if c in pos_before and pos_before[c] != pos_after[c]]
    info["named"] = head
    info["front"] = order[:max(0, int(front))]
    return order, info
