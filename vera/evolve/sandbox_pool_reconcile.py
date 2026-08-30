"""Which sandbox registrations may be deleted - and when we must not decide.

Deleting a pool descriptor is destructive and irreversible: it un-registers a
running container, frees its port and Redis DB for reuse, and (on the heal
path) `docker rm -f`s the container first. `evolve.sandbox.exec` is the only
host-exec route agents have, so losing a registration stops that agent's work
until someone notices and re-creates it.

The prune decided both reaps from two reads it never checked:

    ps = await _sh(["docker", "ps", "-a", "--format", "{{.Names}}"])
    exists = {n.strip() for n in (ps.get("out", "") or "").splitlines() ...}

`_sh` returns ``{"ok": False, "out": ""}`` when the binary is missing, when the
command times out (240s default), or on any exception - so a FAILED docker read
is indistinguishable from "no containers exist", and every registered sandbox
is then classified "container TRULY removed" and deleted. One blind read
un-registers the whole estate at once. The filesystem side has the same shape:
``Path.exists()`` returns False on an OSError, so a transient stat failure reads
as "worktree gone" rather than "could not look".

Observed 2026-08-30: the registry went from 13 registrations to 0, and the
pinned standing mirror was repeatedly de-registered while its worktree was
demonstrably present (`.git` pointer intact) - each loss killing the exec route
mid-session.

**Whether an unobservable read is what fired on any given day is not proven,
and this module does not claim it.** The point is narrower and sufficient: the
code cannot tell the difference, and the safe answer when you cannot observe is
to do nothing. Its neighbour already works this way - `classify_sandbox`
returns ``docker_unreachable`` and `lifecycle_preflight` refuses to act when
``not docker_observable``. This applies the same rule to the one place that
deletes state.

Pure: no I/O. The caller performs the reads and reports what they yielded,
including whether they could be trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Iterable, List

#: A worktree probe is three-valued on purpose. "absent" means we looked and it
#: was not there; "unknown" means we could not look. Only the first is grounds
#: for reconciling a descriptor away.
PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"

KEEP_PINNED = "pinned: standing infrastructure is never auto-reaped"
KEEP_DOCKER_BLIND = "docker state unobservable: refusing to infer removal"
KEEP_WT_UNKNOWN = "worktree could not be probed: refusing to infer absence"


def docker_is_observable(*, ok: bool, container_names: Iterable[str],
                         pool_size: int) -> bool:
    """Whether the container listing may be used to conclude something is GONE.

    Two ways it may not be. The command failed - `_sh` reports that in ``ok``,
    and the prune never looked. Or it succeeded and returned nothing while we
    hold descriptors for containers that should be in that list: a genuinely
    empty docker host is possible, but "zero containers" arriving alongside a
    non-empty pool is far more likely a truncated or broken read than the
    simultaneous disappearance of every sandbox. The cost of being wrong is
    asymmetric - waiting means a dead descriptor lingers until the next sweep,
    acting means the estate is un-registered - so this fails closed.
    """
    if not ok:
        return False
    if pool_size and not list(container_names):
        return False
    return True


def plan_pool_reconcile(pool: Mapping[str, Mapping[str, Any]], *,
                        container_names: Iterable[str],
                        docker_ok: bool,
                        worktree_probe: Mapping[str, str]) -> Dict[str, Any]:
    """Split the pool into what may be reaped, healed, or must be left alone.

    ``worktree_probe`` maps slug -> PRESENT/ABSENT/UNKNOWN. A slug missing from
    it is treated as UNKNOWN, so a caller that forgets to probe something
    cannot accidentally delete it.
    """
    names = {str(n).strip() for n in (container_names or []) if str(n).strip()}
    observable = docker_is_observable(ok=bool(docker_ok), container_names=names,
                                      pool_size=len(pool or {}))
    stale: List[str] = []
    heal: List[str] = []
    kept: Dict[str, str] = {}

    for slug, descriptor in (pool or {}).items():
        d = descriptor or {}
        if d.get("pinned"):
            kept[slug] = KEEP_PINNED
            continue

        probe = str(worktree_probe.get(slug, UNKNOWN) or UNKNOWN)
        if d.get("worktree") and probe == ABSENT:
            heal.append(slug)
            continue

        if not observable:
            kept[slug] = KEEP_DOCKER_BLIND
            continue
        if str(d.get("name") or "") not in names:
            stale.append(slug)
            continue

        if d.get("worktree") and probe == UNKNOWN:
            kept[slug] = KEEP_WT_UNKNOWN

    return {"stale": stale, "heal": heal, "kept": kept,
            "docker_observable": observable}


def audit_summary(plan: Mapping[str, Any], pool: Mapping[str, Any]) -> str:
    """A prune line that says WHAT it acted on, not just how many.

    The original reported counts only - "1 dead pool entr(ies), 1 reconciled
    (worktree-gone)" - which is why three separate occurrences in one session
    could not be told apart, or even attributed to a particular sandbox. Naming
    the slug and the reason is what makes the next occurrence diagnosable.
    """
    bits = []
    if plan.get("stale"):
        bits.append("dropped %s" % ", ".join(sorted(plan["stale"])))
    if plan.get("heal"):
        bits.append("reconciled (worktree absent) %s" % ", ".join(sorted(plan["heal"])))
    if not plan.get("docker_observable"):
        bits.append("docker unobservable: %d descriptor(s) left untouched"
                    % len(plan.get("kept") or {}))
    elif plan.get("kept"):
        bits.append("kept %d" % len(plan["kept"]))
    return "; ".join(bits) or "nothing to reconcile (%d descriptor(s))" % len(pool or {})
