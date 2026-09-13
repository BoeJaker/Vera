"""
sandbox_idle_core.py — pure decisions for the session-sandbox idle tick
========================================================================

No Redis, no docker, no app import: the idle-sleep tick hands this module the
raw records and the alias map and gets back a plan. Pinned by
tests/test_sandbox_idle_plan.py.

Why this exists (2026-09-12, prod): a chat session was LINKED to a goal's
container (sandbox.session.link → KEY_ALIAS), but its own record kept its own
container, active=true and a last_used that nothing would ever bump again
(routing for an alias touches the TARGET's record). The idle tick judged that
stale record — "own container running, idle 30 min" — and called
sandbox.session.sleep on it, which resolves the alias and slept the GOAL's
container instead. The alias's own container was never touched, so the same
record was due again 120 s later, and each sleep first packaged context into
the goal container — waking it (docker start) to write a file, snapshot it,
and stop it. A full start/snapshot/stop of an unused container every two
minutes, logged under the alias's id.

Rules:
  * A record whose session id is an ALIAS is never a sleep target — the
    container it would sleep belongs to someone else. If it still holds a
    container of its own that is not the target's, that container is an
    ORPHAN left behind by the link; report it so the tick can retire it.
  * A container-owning record is due when it is active, has a container and
    a last_used stamp, and that stamp is at least idle_s old. A record with
    no stamp gets one now (grace period) instead of being slept immediately.
  * Two records naming the same container on the same host (the truncated
    container-name collision, see container_name) yield ONE sleep target.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

_SAFE = "_.-"
NAME_LIMIT = 48


def _safe(session_id: str) -> str:
    return "".join(c if (c.isalnum() or c in _SAFE) else "-" for c in session_id)


def container_name(session_id: str, *, prefix: str = "vera-sbx-", suffix: str = "",
                   limit: int = NAME_LIMIT) -> str:
    """Docker container/volume name for a session id: [a-zA-Z0-9_.-] only and
    bounded in length. Ids at or under the limit keep the exact historical
    spelling (so every existing session still finds its container). Longer
    ids used to be cut at the limit, which made two goals that differ only
    after character 48 share ONE container; they now end in a short hash of
    the full id so distinct sessions get distinct names."""
    safe = _safe(session_id)
    if len(safe) > limit:
        digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:limit - 9].rstrip('-._')}-{digest}"
    return f"{prefix}{safe}{suffix}"


@dataclass
class IdlePlan:
    due: List[dict] = field(default_factory=list)        # container owners to sleep
    orphans: List[dict] = field(default_factory=list)    # alias records still holding their own container
    unstamped: List[dict] = field(default_factory=list)  # owners with no last_used → stamp now


def idle_plan(records: Iterable[dict], aliases: Mapping[str, str], *,
              now: float, idle_s: float) -> IdlePlan:
    """Decide what the idle-sleep tick should touch. `records` are the raw
    KEY_SBX values; `aliases` maps session_id → container-owning session_id."""
    plan = IdlePlan()
    recs: Dict[str, dict] = {}
    for rec in records:
        sid = rec.get("session_id") if isinstance(rec, dict) else None
        if sid:
            recs[sid] = rec
    seen: set = set()
    for sid, rec in recs.items():
        if not rec.get("active") or not rec.get("container"):
            continue
        target = aliases.get(sid)
        if target and target != sid:
            target_container = (recs.get(target) or {}).get("container")
            if rec["container"] != target_container:
                plan.orphans.append(rec)
            continue
        last = float(rec.get("last_used") or 0)
        if not last:
            plan.unstamped.append(rec)
            continue
        if now - last < idle_s:
            continue
        key: Tuple[str, str] = (str(rec.get("docker_host_id") or "local"), rec["container"])
        if key in seen:
            continue
        seen.add(key)
        plan.due.append(rec)
    return plan


def own_container_to_retire(own: Optional[dict], target: Optional[dict]) -> Optional[str]:
    """When `own` (the session being linked) already holds a docker container
    that is not the target's, return its name — the link makes it unreachable
    (all routing now lands in the target) so it must not be left running.
    None for no record, no container, a local-backend record, or a shared
    container."""
    if not own or not own.get("container"):
        return None
    cname = str(own["container"])
    if cname.startswith("local:"):
        return None
    if target and target.get("container") == cname:
        return None
    return cname
