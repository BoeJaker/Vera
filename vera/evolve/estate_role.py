"""Only the estate's owner may sweep it.

On 2026-08-31 a foundry-provisioned Vera worker, running in a Proxmox container
on another machine (192.168.0.141) against the shared coordinator Redis, spent a
day deleting sandbox pool descriptors. It survived a prod restart, it survived
the user stopping every Loop Lab sandbox, and it never appeared in this host's
process table - because it was never on this host.

WHY IT DID SO MUCH DAMAGE, and this is the part worth generalising: prune
decides a descriptor is dead when `Path(worktree).exists()` is False. On that
machine `/home/boejaker/Vera/.loop-lab-worktrees/` does not exist AT ALL, so
EVERY descriptor probed ABSENT and every sandbox looked reapable. An instance
that cannot see the estate is not a neutral observer of it - it is the worst
possible judge of it, and it was running the judgement hourly.

A Vera worker is a full Vera process. `docker.worker.spawn` wires it to the
shared REDIS_URL so it can take tasks, and it therefore also starts the whole
ambient scheduler - including the sweeps that reap containers, worktrees,
branches and the pool registry. Nothing about being a worker stopped that.
`_SANDBOX_SKIP_JOBS` gates dev sandboxes through VERA_IS_DEV_SANDBOX; there was
no equivalent for workers, and a worker has even less business touching an
estate it does not host.

THE RULE HERE IS DELIBERATELY NOT "am I a worker".

A flag only stops instances that carry it, and the instance that caused this
predates any flag we add - which is the whole difficulty with stale code. So the
primary test is possession: an instance may only sweep the estate if the estate
is actually THERE. That is a property of the filesystem the offender fails
automatically, whatever it believes itself to be and however old it is.

`is_worker` and `is_dev_sandbox` are still honoured, because a worker running on
the SAME host can see the worktrees and would otherwise pass the possession
test. Belt and braces, with the braces load-bearing.
"""

from __future__ import annotations

import os
from typing import Dict, Tuple

#: Ambient jobs that mutate shared estate state. Reads are unrestricted.
ESTATE_JOBS = frozenset({
    "evolve.scaffolding.sweep",      # reaps worktrees + merged branches, calls prune
    "evolve.sandbox.idle_sweep",     # pauses/reaps containers estate-wide
    "evolve.mainline_mirror.refresh",  # rewrites the shared mirror worktree
})

#: The directory whose presence means "this machine hosts the sandbox estate".
ESTATE_DIR = ".loop-lab-worktrees"

DENY_NO_ESTATE = ("this instance cannot see the sandbox estate "
                  "({d}/ is not present) - every worktree would probe ABSENT, "
                  "so every descriptor would look reapable")
DENY_WORKER = "a worker is a task runner, not the estate's host"
DENY_SANDBOX = "a dev sandbox is a guest on the estate, not its owner"


def estate_is_present(repo_root: str, estate_dir: str = ESTATE_DIR) -> bool:
    """Whether the sandbox estate lives on THIS machine."""
    root = str(repo_root or "").strip()
    if not root:
        return False
    try:
        return os.path.isdir(os.path.join(root, estate_dir))
    except OSError:
        # Cannot look - do not conclude it is absent. Absence is what licenses
        # deletion, so an unreadable answer must never read as "gone".
        return True


def is_estate_job(name: str) -> bool:
    return str(name or "") in ESTATE_JOBS


def may_sweep_estate(*, estate_present: bool, is_worker: bool = False,
                     is_dev_sandbox: bool = False,
                     estate_dir: str = ESTATE_DIR) -> Tuple[bool, str]:
    """(allowed, reason). Reason is empty when allowed."""
    if not estate_present:
        return False, DENY_NO_ESTATE.format(d=estate_dir)
    if is_worker:
        return False, DENY_WORKER
    if is_dev_sandbox:
        return False, DENY_SANDBOX
    return True, ""


def decide(job_name: str, *, estate_present: bool, is_worker: bool = False,
           is_dev_sandbox: bool = False) -> Dict[str, object]:
    """Whether this instance may run `job_name` right now."""
    if not is_estate_job(job_name):
        return {"run": True, "reason": ""}
    ok, why = may_sweep_estate(estate_present=estate_present, is_worker=is_worker,
                               is_dev_sandbox=is_dev_sandbox)
    return {"run": ok, "reason": "" if ok else f"skipping '{job_name}': {why}"}
