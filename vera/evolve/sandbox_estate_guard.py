"""A dev sandbox is a guest. It does not get to reap the house.

A Loop Lab dev sandbox is a FULL Vera process. It registers every capability
prod does - measured inside a live sandbox on 2026-08-31, 2261 of them,
including:

    evolve.sandbox.prune                 reaps the shared sandbox registry
    evolve.sandbox.down / .reap          removes other agents' containers
    evolve.branch.delete                 deletes git branches
    evolve.bleeding_edge.promote_to_main MERGES INTO PROD'S LIVE CHECKOUT
    evolve.pipeline.promote              merges into the shared integration branch

and it shares prod's COORDINATOR Redis (VERA_COORD_REDIS_DB=0), where the
sandbox pool and the GPU gate leases live. `VERA_IS_DEV_SANDBOX=1` is set, but
until now that flag only gated scheduled ambient jobs - nothing stopped anything
inside a sandbox from simply CALLING these.

This is not hypothetical. During census 20 a prune ran from outside prod and
emptied the sandbox pool mid-run; the operator's fallback then had nothing to
choose from and the goal lost its browser. Leech boot now keeps the SCHEDULER
from firing those sweeps in a sandbox, but a scheduler guard does nothing about
a loop, an agent, or a user hitting the sandbox's own API.

WHAT IS DENIED is deliberately narrow and explicit rather than a prefix sweep: a
sandbox legitimately reads all of this (status, diff, list, graph, audit) and
those must keep working - the Loop Lab UI inside a sandbox is how you inspect a
branch. Only the operations that mutate state OUTSIDE the sandbox are refused,
each named, so the list is auditable and cannot quietly widen.

The refusal explains where the call belongs, because "denied" without a next
move is how a guard becomes something people work around.

Pure: no imports from the app. The caller supplies whether it is a sandbox.
"""

from __future__ import annotations

from typing import Any, Dict

#: Mutates the shared sandbox estate - other agents' containers, the pool
#: registry every instance reads, or shared worktrees.
_ESTATE = {
    "evolve.sandbox.prune",
    "evolve.sandbox.reap",
    "evolve.sandbox.down",
    "evolve.sandbox.up",
    "evolve.sandbox.restart",
    "evolve.sandbox.pause",
    "evolve.sandbox.resume",
    "evolve.sandbox.spawn",
    "evolve.sandbox.pin",
    "evolve.sandbox.approve",
    "evolve.sandbox.worktree.repair",
    "evolve.bleeding_edge.container.ensure",
}

#: Mutates git history or the branches everyone shares.
_GIT = {
    "evolve.branch.create",
    "evolve.branch.delete",
    "evolve.bleeding_edge.promote_to_main",
    "evolve.pipeline.promote",
    "evolve.pipeline.rollback",
    "evolve.repo.gitea_push",
    "content.edit",
}

#: Everything a sandbox may not do to the world outside itself.
DENIED_IN_SANDBOX = frozenset(_ESTATE | _GIT)

#: Kept explicit so a future reader can see these were considered and allowed:
#: they read shared state, or write only inside this sandbox.
EXPLICITLY_ALLOWED = frozenset({
    "evolve.sandbox.list", "evolve.sandbox.status", "evolve.sandbox.exec",
    "evolve.sandbox.fs.read", "evolve.sandbox.fs.write", "evolve.sandbox.diff",
    "evolve.sandbox.logs", "evolve.sandbox.preflight",
    "evolve.git.status", "evolve.git.graph", "evolve.audit.list",
    "evolve.pipeline.list", "evolve.pipeline.get", "evolve.pipeline.diff",
    "evolve.pipeline.adopt", "evolve.pipeline.review.request",
    "content.status",
})


def is_denied(cap_name: str, *, in_sandbox: bool) -> bool:
    """Whether this call must be refused because it is running in a sandbox."""
    if not in_sandbox:
        return False
    return str(cap_name or "").strip() in DENIED_IN_SANDBOX


def refusal(cap_name: str) -> Dict[str, Any]:
    """The refusal, with the next move in it."""
    name = str(cap_name or "").strip()
    return {
        "ok": False,
        "error": (
            f"{name} is refused inside a dev sandbox: it changes state OUTSIDE "
            "this sandbox (the shared sandbox estate, or git branches every "
            "agent uses). A sandbox is a guest on this box - it may read all of "
            "this, and write freely inside itself, but the estate and the "
            "branches belong to the orchestrator. Run this against prod's API "
            "instead of the sandbox's own."),
        "refused_by": "sandbox_estate_guard",
        "cap": name,
    }
