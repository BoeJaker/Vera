# Vera Dev Lifecycle, Repo Hygiene & Observability — Plan / Standard

> **⏸ ON HOLD (2026-08-09).** Active focus moved to the companion plan
> `~/vera_sandbox/agentic swarm.md` (**Agent Boards & Comms**), Stage 1. This plan
> is paused, not abandoned — Phases C/D/E remainders (C4b, heatmap/auto-postmortem,
> the content-edit surface) resume after the board foundation lands. The §8.2 #7
> boundary work (`state_paths`) shipped and directly unblocks that plan's Stage 0.

**Status:** proposed (plan) · **Author of plan:** drafted with Claude Code · **Date:** 2026-08-07
**Owner:** admin (user) · **Home of the standard once ratified:** this file (versioned in-repo)
**Revisions:** 2026-08-08 — added §2.2b (branch & worktree lifecycle) + §7 row + §9 rules
8–9, after a branch/worktree-sprawl + duplicate-history incident while building the tooling.

> This document is BOTH the plan for the work AND, once ratified, the standard itself.
> It is deliberately kept in the repo (not just in an assistant's memory) — per the
> traceability rule it defines in §4. Update it in place; don't fork it into memory.

---

## 0. Why this exists

Two recent failures (crypto planner hijack; planner-determinism drift — see
`documentation/postmortems/2026-08-06-agentic-loop-planner-drift.md`) each cost
multiple multi-hour debugging sessions. Neither was subtle. Both were slow to
diagnose for the same structural reasons:

1. **No isolation** — a team edits the single **prod checkout on the `\\llm.int` network
   share**, live. Concurrent edits to the same files, no per-feature separation, and the
   running prod app is the thing being changed.
2. **No change traceability** — the activating change was made Wednesday, left
   **uncommitted**, then bundled a day later into one ~800-line commit
   (`33b3ce9`). There are **zero commits dated the day the bug appeared**. Nothing tied
   the running code to a branch/version, a commit, or the session that produced it.
3. **No regression safety net** — the ad-hoc tests written during debugging were never
   formalized, so nothing would have caught the regression before it shipped.
4. **Recurring git-mechanics failures** on the SMB share (see §7) make committing itself
   unreliable, which discourages the granular commits that would have made #2 a non-issue.

This plan fixes all four, reusing the Loop Lab / `evolve.*` infrastructure that already
exists rather than inventing parallel mechanisms.

---

## 1. Principles

- **Isolate every unit of work.** Independent work happens on its own branch, in its own
  container — never by editing the shared prod checkout in place.
- **The running code must be identifiable.** Every log/metric/run/error carries the
  branch + commit (+ dirty flag) it came from, and — where a change was assistant-driven —
  the Claude Code session id.
- **A change is not done until it is committed, tested and documented.** Code, its tests,
  and its docs land together.
- **Prefer existing infra.** `evolve.*` (Loop Lab) already has repos, branches, worktrees,
  pipelines, gates, reviews, sandboxes, error ingestion, test suites, self-test, a git
  graph, and Claude-session ingestion. Extend it; don't duplicate it.
- **Guardrails over good intentions.** Rules that can be enforced programmatically (hooks,
  gates, branch-name checks) are enforced, not just documented.

---

## 2. The Repo Hygiene Standard (normative)

These are the rules. §3 makes them enforceable; §8 sequences the build.

### 2.1 Branch-per-work, tagged by purpose
- **No direct commits to `main`.** All work happens on a branch off `main`.
- Branch names are **typed**: `<type>/<slug>[-<issue>]` where `type ∈
  {feat, fix, refactor, perf, docs, test, chore, spike, hotfix}`.
  Examples: `fix/planner-determinism`, `feat/curated-dataset-layer`.
- One branch = one coherent unit of work. Don't accumulate unrelated changes on a branch
  (the `33b3ce9` bundling is the anti-pattern).
- Every branch is registered as a Loop Lab **pipeline** (`evolve.pipeline.run`) so it has a
  controller identity, a gate, and a place in the git graph / Review tab.

### 2.2 Isolation — no dev on the prod share
- **The `\\llm.int` prod checkout is a deploy target, not a dev workspace.** Direct editing
  of it is deprecated and (§3) blocked by a guardrail.
- Development happens in a **per-branch dev container** (generalized Loop Lab sandbox, §5):
  its own git worktree/clone on a **local (non-SMB) filesystem**, its own Vera process on
  its own port, isolated from prod and from other developers' containers.
- This directly removes: concurrent same-file edits, the "someone changed my file" class of
  confusion (`git-repo-concurrent-changes-normal`), and every SMB git-mechanics failure in
  §7 (dev containers commit against a local `.git`, not the share).
- **Deploy model (settled 2026-08-08, see §8.1 #1):** prod **tracks `main`** and only ever
  **fast-forwards** to it — a deploy is `git fetch && git merge --ff-only main` + restart, a
  deliberate atomic step; prod's tree stays clean. Integration (feature→`main`) happens
  off-prod via the isolated-worktree gate, **never** by merging into prod's live checkout.
  `--ff-only` is the safety: if it can't fast-forward, prod's tree was dirtied out-of-band —
  fix that rather than force it.

### 2.2b Branch & worktree lifecycle — no scaffolding left behind
The mess this standard prevents includes the **scaffolding of doing the work**, not just
the work. An incident on 2026-08-07 (building this very tooling) left redundant branches
and stray worktrees behind and littered the git graph with duplicate-content commits —
the exact opposite of the goal. Rules:
- **One worktree per active unit, on a local path — removed the moment the unit lands or
  is abandoned** (`git worktree remove` + `git worktree prune`). A finished unit leaves
  **no** worktree. Worktrees are scaffolding, not deliverables.
- **A branch's content lands EXACTLY ONCE, via ONE mechanism** (the promote/merge gate in
  §3). Never cherry-pick the same change onto another branch; never create throwaway
  "boot-test" merge commits; never mix fast-forward + merge + file-copy to land a single
  unit. Each duplicates content under a new SHA, producing **phantom "unmerged" branches**
  and an incoherent graph. Need to boot-test a combination? Do it **on the unit's own
  branch**, not a disposable side-branch.
- **Delete the branch when it lands** (or is abandoned). Its commits remain reachable from
  the target; the label is scaffolding. The git graph should show **only genuinely
  in-flight work**.
- **Prove-redundant-before-deleting.** A branch/worktree is deleted only after proving it
  carries no unique content: `git merge-tree --write-tree <target> <tip>` equals the
  target's tree, **or** `git cherry <target> <tip>` marks every commit already-applied
  (`-`). *Reasoning that it's redundant is not sufficient — run the check first.* (Deleting
  a branch keeps its commits by SHA, so a mistake is recoverable — but verify, don't lean
  on recovery.)
- **Periodic sweep** (the branch/worktree analogue of §4.2's container reaping): no
  worktree without an active unit; no branch whose content is fully in the target.

### 2.3 Commit hygiene
- **Commit granularly and often.** A day's work is many small commits on the branch, not one
  bundle later. This is what makes `git bisect` / "what changed Wednesday" answerable.
- Commit messages: imperative subject, a body explaining **why**. (Claude is already good at
  this — keep it.)
- **Git identity is always the user** (`admin`). Never set `GIT_AUTHOR_*`/`GIT_COMMITTER_*`
  to Claude/agent identities, and **never add a `Co-Authored-By: Claude` trailer**
  (`no-claude-coauthor-trailer`, `git-attribution-internal-only`). "Claude Code did this" is
  recorded ONLY in Vera-internal fields (pipeline `controller`, `reviews[].reviewer`,
  author-map), never in git.
- No secrets in commits — the `tools/secret_scan.py` pre-commit gate stays
  (`secret-scan-precommit-gate`); in a local dev container it runs fast (the multi-minute
  hangs were an SMB artifact).

### 2.4 Tests land with the change
- Any new feature/capability/module ships with tests. Minimum bar:
  - a **contract test** (the cap registers, has the right `http_path`, imports cleanly), and
  - at least one **behavioural test** of the core promise.
- Bug fixes ship with a **regression test** that fails before the fix and passes after
  (e.g. "a pokedex goal never yields a crypto plan"; "planner plans share vocabulary with
  the goal"; "a re-plan differs from the plan that drifted").
- Tests live in `tests/` and are runnable head-lessly (`python -m pytest`, not a bare
  `pytest` — see the PATH lesson in §7).

### 2.5 Documentation & traceability
- **Every change is documented in git** (the commit) **and, when it carries design intent,
  in the repo** — not only in an assistant's memory.
- Specifically, these artifacts go **in the repo**, not just in chat/memory:
  - **Branch/feature plans** → `documentation/plans/<branch>.md` (the plan I would otherwise
    hold only in memory).
  - **Postmortems** → `documentation/postmortems/<date>-<slug>.md`.
  - **Specs/standards** → `documentation/specs/`.
  - **Reports / analyses / any pertinent text file the user asks for during a run** → saved
    to the repo (or the run's artifact dir, linked from the branch plan) with a durable path,
    not left inline in a transcript.
- Memory (`.claude/.../memory`) remains the assistant's index/hooks, but is a **pointer to**
  the in-repo document, never the sole home of it.

### 2.6a Naming & record association (human-friendly, machine-linkable)
- Keep the **human-friendly names** (e.g. this plan's readable title) — they aid recall — but
  make every artifact **cross-referable** to the records around it. Each unit of work has a
  small stable **key** and a set of links:
  - branch `↔` its **head commit sha(s)** `↔` its Loop Lab **pipeline id** `↔` its
    **plan doc** (`documentation/plans/<branch>.md`) `↔` the **Claude session id** that drove
    it `↔` any **postmortem / report** it produced `↔` the **dev container** it ran in.
- **Encode the associated commit into the artifact name/front-matter**: a plan/postmortem/
  report carries the `sha` (and branch) of the commit it describes in its filename or YAML
  front-matter, so "which commit does this doc belong to" is answerable without guessing.
  (e.g. front-matter `branch: fix/planner-determinism`, `commit: <sha>`, `pipeline: <id>`,
  `session: <id>`.)
- These links are the same fields §5.1 stamps onto events — one association graph, readable
  from the git graph, the Loop Lab Review row, a log line, or a doc.

### 2.6 Verify against the real running thing before "done"
- `py_compile`/`ast.parse` are necessary but **not sufficient** — they miss import-time
  failures (forward-referenced default args → `NameError` that takes down a whole namespace;
  see §7). "Done" requires the change actually **loaded and exercised** in a dev container
  (boot + `evolve.selftest` + the feature's tests), and for UI/runtime changes, physically
  observed (`reuse-proven-fix-verify-live-dont-guess`).

---

## 3. Programmatic guardrails (enforcement)

The point of §2 is that it's enforced, not hoped for.

- **Pre-commit hook (repo-managed, `tools/hooks/`):**
  - reject a `Co-Authored-By:` / AI-attribution trailer;
  - reject a commit whose `GIT_AUTHOR/COMMITTER` isn't the configured user identity;
  - run `tools/secret_scan.py --staged` (existing);
  - reject a direct commit to `main`.
- **Branch-name check** (pre-commit / pipeline-creation): reject names that don't match the
  typed pattern in §2.1.
- **"Not on the prod share" guardrail:** a hook that refuses to commit when the repo's
  worktree path is under `\\llm.int` / the prod checkout (configurable), steering the work
  into a dev container. (Escape hatch: an explicit env var for the deploy/promote flow.)
- **Merge-to-main gate (Loop Lab pipeline):** `main` only advances via
  `evolve.pipeline.promote`, which requires: green tests (`evolve.pipeline.test` /
  `evolve.suite.run`), a clean `evolve.selftest` (boots the app — catches import-time
  breakage), and a review decision. No gate pass → no promote.
- **Version/branch stamping (see §5.1):** enforced at the source — the stamp is added in the
  event/log emit path so it can't be forgotten.
- **SMB recovery is codified, not folklore:** the `git update-ref` / direct-`HEAD`-write
  recovery for the SMB ref-write failure (§7) is wrapped in a helper/cap so a stranded
  commit is a one-command fix, not a rediscovery each time. (Only relevant for the prod
  checkout; dev containers don't hit it.)

---

## 4. Container-based dev environment

Generalize the **Vera-only dev sandbox** (`evolve.sandbox.*`, currently one app-specific
docker-compose on port 8998) into **per-branch dev environments**:

- **One container per branch/pipeline**, each: a local git worktree/clone of the target repo
  at that branch, a Vera process on an allocated port (not prod's 8999, not a fixed 8998 —
  a port pool), and its own isolated state. Built from a **fresh image** so it isn't stale
  (`loop-lab-sandbox-and-impl-timeline`: the stale-`vera:latest` → missing-caps trap; the
  probe must require a workhorse cap, and `up` must be able to force-rebuild).
- **Lifecycle caps** (mostly exist): `evolve.sandbox.up/down/pause/resume/status/exec/
  fs.*/diff/snapshot`, `evolve.branch.create/delete`, `evolve.pipeline.*`. Gaps to add:
  per-branch container identity (not a single shared dev sandbox), port allocation, and a
  "open this branch in a fresh container" one-shot.
- **VSCode integration:**
  - A **`.devcontainer/`** definition so "Reopen in Container" / "Attach to Running
    Container" (VSCode's built-in Dev Containers + Docker features) connects an editor
    straight into a branch's dev container — VSCode can automate the connection setup.
  - **VSCode tasks** (`.vscode/tasks.json`) + a thin extension command surface (build on the
    existing self-packaged VSIX / `/vscode/connect`, `remote-claude-client`) to: create a
    branch container, open it, run its tests, tail its logs, promote.
  - Claude Code running inside the dev container edits a **local** checkout — no SMB git
    failures, fast secret-scan, real import/boot verification.
- **Easy access to each dev container's Vera Web UI** (physical testing):
  - a launcher (Vera UI **and** VSCode task) that opens the branch container's UI at its
    allocated port — one click from the Loop Lab pipeline/Review row and from VSCode.
  - **Operator access:** register each live dev container as an operator target (it's already
    "every machine/container is a Vera node" territory — `unified-nodes-estate`,
    `remote-access-workspaces-subsystem`) so the operator system can drive the dev UI for
    automated testing, not just a human.

### 4.1 Resource contention (Ollama / GPU) — best practice
Concurrent dev containers **must not contend for scarce shared resources — chiefly Ollama**.
- **One in-flight generation per Ollama worker.** Concurrency across containers is fine only
  if requests are spread across **distinct workers**; two containers hammering the same
  worker serialize and mislead timing. The **GPU worker is much faster than the CPU workers**
  — reserve it, don't let idle/background dev containers monopolize it.
- A dev container that exercises **chat / agentic loops** declares that it needs an LLM slot;
  the harness assigns it a specific worker (or queues it) rather than letting N containers
  free-for-all. Reuse the existing routing (`llm.route.resolve`, worker pinning) + the
  "no concurrent live loop tests" rule (`no-concurrent-loop-tests`).
- **For this workstream specifically** (repo hygiene, provenance, tests, observability, the
  guardrails) LLM load is minimal — we're not testing chat/loops soon — so container
  concurrency is safe here. The rule matters the moment a dev container starts running loops.

**✓ Implemented — cross-process GPU gate / "one big queue" (C5-U1, 2026-08-08, live on prod).**
The sketch above ("harness assigns a specific worker") was replaced by a simpler, robust
primitive: a **bounded, crash-safe semaphore per Ollama node on a SHARED coordination Redis
DB** that prod and every dev container both reach (`vera/ollama_gate.py`, wired at the single
`_ollama_slot` chokepoint). A generation must hold a slot before it runs and releases it
after; the GPU node is capped at 1 concurrent generation across ALL processes, so prod and
dev containers **queue** for the GPU instead of colliding. This is the coordination-plane /
data-plane split: coordination Redis is SHARED (`VERA_COORD_REDIS_DB=0`), data Redis stays
isolated per sandbox.
- Slots are TTL-fenced leases (a killed container's slot auto-frees); release is owner-fenced
  (Lua CAS); **fail-open** everywhere (gate off / node ungated / coord Redis down / queued past
  the wait budget → proceed unslotted) so the gate can only ever ADD waiting, never BREAK
  generation.
- Flags: `VERA_OLLAMA_GATE` (on/off — **now on in prod's `.env`**), `VERA_GPU_GATE_N` (default 1),
  `VERA_NODE_GATE_N` (default 0 = CPU nodes ungated), `VERA_GATE_TTL_S`, `VERA_GATE_WAIT_S`.
  Live occupancy: `GET /ollama/gate` (`ollama.gate.status`). Verified cross-process: a prod
  generation's held slot was visible from a dev container's `/ollama/gate`.

### 4.2 Container lifecycle — cleanup & archive
Dev containers are cattle, not pets. A clear teardown policy prevents a pile of stale, stale-
imaged, resource-holding sandboxes (we've already hit the stale-image trap):
- **States:** `live` → `paused` (idle, resumable) → `archived` (snapshot kept, container
  removed) → `pruned` (gone). `evolve.sandbox.pause/resume/snapshot/down` already exist —
  wrap them in a policy.
- **On done** (branch promoted or abandoned): snapshot the worktree diff + artifacts (they're
  already captured by the pipeline record / `documentation/`), then **`down` + prune** the
  container and free its port. The *record* (pipeline, diff, plan, postmortem) persists; the
  running container does not.
- **TTL / idle reap:** a scheduled sweep pauses containers idle > N hours and archives those
  idle > M days; never touches prod (8999) or a container with an open review/live pipeline.
- **Fresh-image discipline:** archived/relaunched containers rebuild from a current image
  (never resurrect a stale `vera:latest` — see §7).
- Surface all of this in the Loop Lab Sandbox tab (status, last-used, "archive", "prune",
  "rebuild") — most of the status plumbing already exists (`/remote/sandbox/list` state
  semantics, the rebuild button).

### 4.3 Data isolation — dev must not pollute prod's stores
A dev sandbox is a FULL Vera process **sharing prod's backing services**: the `_dev_compose_yaml`
env points it at prod's live Postgres, Chroma and Neo4j (only Redis is isolated by DB number,
and fabric.db is snapshotted). So a dev loop that stored memories or wrote graph nodes was
**mutating prod**.
- **✓ Implemented — write-isolate guard (C5-U2, 2026-08-08).** `vera/sandbox_guard.py`:
  `write_blocked()` is true only in a dev sandbox (`VERA_IS_DEV_SANDBOX=1`) and is a **strict
  no-op in prod**. Guarded at the write chokepoints — `MemoryFabric.store/update` (fans out to
  PG+Chroma+Neo4j), the `FabricNeo4j` write methods + write-cypher `query()`,
  `MemoryGraphAdapter`, `FabricChroma.upsert`, and `_pg_archive`. **Reads pass through** to prod
  (dev sees real context); only writes are suppressed. Escape hatch `VERA_SANDBOX_WRITE_GUARD=0`.
  Verified: a sandbox `memory.store` touched nothing in prod while `memory/edges/diag` still read
  prod's 3.2M-relationship graph; a prod store persisted normally (guard inert).
- **○ Gap to close (revisit U2):** the guard covers the fabric + memory write surface (where ~all
  loop/agent persistence flows), but any subsystem doing **direct** Neo4j/PG/Chroma writes through
  its own session (candidates: dream, goals, projects, worldview) would still leak. **Do a full
  audit of direct-write sites** and route them through the guard (each is a one-liner). Until then
  the leak is "mostly closed," not hermetic.

### 4.4 Leech boot — dev inherits prod's state, doesn't recompute it
A fresh full boot runs embeddings + fetches/rebuilds fabric sources; N dev containers each doing
that independently would hammer Ollama and duplicate prod's work.
- **✓ Implemented — leech boot (C5-U3, 2026-08-08).** In a dev sandbox, `scheduler_loop` skips
  heavy **ambient** scheduled jobs (agent-RAG re-embedding, node benchmarks, model sync/pull
  sweeps, the long-term loop scheduler, v8 program ticks, external calendar sync) via a denylist
  (`_SANDBOX_SKIP_JOBS`) + a `schedule(..., skip_in_sandbox=True)` opt-in. One-time `_startup`
  module-init hooks still run (panels/state init). Complements `EMBED_CAPS_ON_START=0` (already in
  the dev compose) and the dream/claude-session jobs that already self-gate. The sandbox
  **read-throughs** prod's already-computed state instead of rebuilding it. Strict no-op in prod.
- **Direction (better method, confirmed):** one-directional sync FROM the main session INTO dev
  containers — snapshot (Redis/fabric.db, already) + read-through (PG/Chroma/Neo4j, §4.3) + skip
  recompute (this) — rather than each container booting its own heavy load.

---

## 5. Observability & Postmortem system

The heart of "detect → track → root-cause → fix." Built on the existing perf/jobs/syslog
subsystems + `evolve.errors.*` + `evolve.suite/selftest` + `evolve.git.graph` + Claude
session ingestion.

### 5.1 Provenance stamping (do this first — it's the cheapest, highest-leverage piece)
- Every emitted event/log/metric/run/error/pipeline record gains: `git_sha`, `branch`,
  `dirty` (uncommitted?), `instance`/port, and `session_id` when the running code (or the
  triggering action) came from a Claude Code session (via the existing
  `ide.claude_sessions.*` ingestion — link the change to the conversation that made it).
- Stamped at the emit chokepoint (like `_now_context_line` grounds every loop LLM call) so
  it's structural, not per-call-site.
- Payoff: any log line / error / bad plan is one hop from "which commit, which branch, which
  Claude session" — the exact thing missing in the Wednesday incident.

### 5.2 Unified plane
- Land logs, perf samples, syslog, jobs, **test results**, and **errors** in one queryable
  store keyed by `(time, branch/sha, instance, session)`. Reuse the perf subsystem's capture
  + the task-stream/jobs infra; don't build a new logger.

### 5.3 Tests + errors heatmap (Loop Lab tab, with playback)
- A new Loop Lab tab: a heatmap of **system × time**, each cell coloured by test pass/fail
  **and** error/regression density (errors from `evolve.errors.*`, tests from
  `evolve.suite.*`/`evolve.unittest.run`). Same "playback over time" UX as the sprint UI.
- Critical systems (planner, LLM routing, fabric ingest, memory retrieval, event loop, …)
  are first-class rows; a regression there is visually loud.

### 5.4 One-click auto-postmortem (deep-dive)
- Click a red cell → an **auto-assembled postmortem** (the incident report I wrote by hand
  becomes a generated artifact): the failing test/error + its exact inputs (for the planner:
  the assembled prompt, catalog, skills), the active `branch/sha`, the **diff since the last
  green** on that system, the linked **Claude session**, and the surrounding
  perf/jobs/syslog. Draft written to `documentation/postmortems/` (per §2.5) for the human to
  finalize. `evolve.errors.suggest` already hints at a fix — feed it this context.

### 5.5 Regression flagging for critical systems
- Tag systems "critical"; a new failing test or a spike in errors on a critical system raises
  a flag on the heatmap + proactively notifies (comms), instead of waiting for a human to
  notice a bad plan. Gate blocks promotion to `main` while a critical flag is open.

---

## 6. Test system (formalize the ad-hoc into a suite + gate)

- Consolidate the tests I've been writing (`tests/test_fabric_curation.py`, the eval scripts,
  existing `tests/test_*`) into a real suite with three tiers:
  1. **contract** (caps register / import cleanly — `tests/test_capabilities_contract.py`
     exists),
  2. **behavioural** (core promises),
  3. **critical-system regression** (the planner guards, event-loop non-blocking, fabric
     idempotency, memory retrieval scoping, …).
- Run via the existing `evolve.suite.run` / `evolve.unittest.run` / `evolve.selftest`:
  - **on branch push** → regression run (results → heatmap, stamped with §5.1),
  - **as the merge-to-main gate** → must be green + selftest-clean to `promote`.
- Because `evolve.selftest` boots the app, the gate catches the import-time class of failure
  that `py_compile` misses (§7).

---

## 7. Known failures this standard must design around (from across all sessions)

These are the concrete, already-encountered failure modes the guardrails address:

| Failure | What happens | How the standard handles it |
|---|---|---|
| **SMB ref-write** (`git-smb-ref-write-failure`) | `git commit` writes the object but can't move `refs/heads/main` ("couldn't set 'refs/heads/main'"), or can't write the `HEAD` symref — commit silently doesn't land | Dev in **local-FS containers** (no SMB); prod-checkout commits are the exception, with the `git update-ref` / direct-HEAD-write recovery codified (§3) |
| **Secret-scan slow over SMB** (`secret-scan-precommit-gate`) | pre-commit hook takes minutes over the share; commit appears to hang; staged-vs-worktree ignore-file gotcha | Runs fast on a container's local FS; keep pragma / `.secretscanignore` conventions |
| **AI attribution in git** (`no-claude-coauthor-trailer`, `git-attribution-internal-only`) | `Co-Authored-By: Claude` surfaced Claude as a GitHub contributor; expensive force-push to undo | Hook rejects attribution trailers + non-user author; internal fields carry "Claude Code" |
| **Bundled uncommitted work** (this incident) | a day's changes uncommitted, then one 800-line commit → breaking change unpinnable, `bisect` useless | Branch-per-work + granular commits + gate (§2.1, §2.3) |
| **Branch/worktree sprawl + duplicate history** (2026-08-07) | building the hygiene tooling itself left redundant branches + stray worktrees behind, and cherry-pick/boot-merge created duplicate-content commits → phantom "unmerged" branches, an incoherent graph, deletes made on a hunch | §2.2b: one worktree per unit removed on land; land content once via one mechanism; delete branch on land; prove-redundant-before-delete |
| **Import-time breakage py_compile misses** (`loop-lab-multi-repo-foundation`) | forward-ref default-arg → `NameError` at import → took down the whole `evolve.*` namespace | Gate runs `evolve.selftest` (real boot), not just compile (§2.6, §6) |
| **Stale sandbox image** (`loop-lab-sandbox-and-impl-timeline`) | dev sandbox on stale `vera:latest` → missing caps → `loops.run` 404 | Per-branch containers built from a **fresh** image; probe requires a workhorse cap |
| **Reload to load backend changes** | Python changes need a process reload; panel HTML needs only a refresh | Dev-container loop includes reload; the test/verify step assumes it |
| **Concurrent collaborators** (`git-repo-concurrent-changes-normal`) | unfamiliar diffs on disk are normal, not anomalies | Isolation removes the confusion; don't investigate others' diffs by default |
| **Shared cluster backend** (`vera-cluster-shared-backend`) | instances share one Neo4j+Redis; per-node caches drift | Dev containers get isolated (or namespaced) backing state so testing doesn't corrupt prod data |
| **Windows/Bash quirks** (`bash-tool-broken-use-powershell`, `vera-runs-on-linux-not-windows`) | Bash tool dies (cygwin); Vera runtime is POSIX | Tooling assumes PowerShell on the Win host, POSIX in containers |

---

## 8. Phased roadmap

Each phase is independently shippable and leaves the system better than it found it.
**Status as of 2026-08-08** — ✓ done · ◐ partial · ○ not started. Kept current as phases
land (per §2.5 this doc is the living record, not a snapshot of original intent).

- **Phase A — Provenance + first guardrails (highest leverage, lowest cost).  ◐ mostly done**
  - ✓ **Provenance stamping (§5.1)** — `vera/provenance.py` resolves git sha/branch/dirty
    once at boot; `event_stamp` stamps `{ver,br,dirty}` at the `emit_event` chokepoint;
    `obs.provenance` cap exposes full detail. Deployed to prod and verified (live events
    carry `ver`/`br`; `GET /obs/provenance` returns real data). **Container caveat:** the app
    image has no git binary and a worktree's `.git` sits outside the bind mount, so
    provenance resolves via env → live git → a host-written `.vera-provenance.json` that
    bring-up generates (`python -m Vera.vera.provenance`).
  - ✓ **`session_id` link to the Claude session** — DONE (2026-08-08):
    `evolve.pipeline.adopt`/`run` record `session_id` (the driving session — the Claude Code
    UUID for `controller=claude_code`, injected by `/mcp/call`) + `via` (caller_kind). The CI/CD
    pipeline detail surfaces the session with an **"open chat ▸"** drill-down rendering
    `ide.claude_sessions.history`. **And the chat actually resolves** — `ide.claude_sessions`
    already ingests remote/Windows-host sessions via the **`vscode-client` push channel**
    (`ide.claude_sessions.sources` lists them), NOT via any deferred SSH scan. Verified end to
    end: the driving session `cd43896f…` (`via=mcp`) resolved to **223 ingested turns** of this
    very conversation. **git-graph commit nodes are stamped too** (§8.2 #6 done) — the same
    drill-down works from the commit DAG, not only the pipeline record.
  - ✓ **Planner regression tests** — pure helpers extracted to `vera/dag/planner_core.py`;
    `tests/test_planner_guards.py` (10) locks skill-filtering, drift detection, and
    non-deterministic planner sampling (the logic behind both incidents). `evolve.unittest
    .run` fixed to use `sys.executable` (was bare `python`, not on PATH). Live-verified
    2026-08-08: `dag.plan` on two diverse goals produced on-topic plans, no drift.
  - ◐ **Pre-commit hooks (§3)** — only the **secret-scan** gate is wired
    (`tools/hooks/pre-commit`). Still to add: reject AI-attribution trailer; reject non-user
    author; block direct commit to `main`; enforce the typed branch-name pattern. (Also: the
    hook isn't executable in fresh worktrees, so it silently skips there — fix it.)
  - ◐ **Fold hygiene into how I work** — branch-per-unit + document-in-repo in force; §2.2b
    (branch/worktree lifecycle) added 2026-08-08 after a sprawl incident (§8.1).

- **Phase A+ — Safe sandbox landing (unplanned; shipped 2026-08-07/08).  ✓ done**
  Not in the original plan but necessary and delivered — approval/landing can no longer
  clobber a live checkout:
  - ✓ **Clobber-proof accept** — `ide.workspace.changes.accept` is now compare-and-swap:
    propose records each file's `base_sha`; accept refuses any file whose live target drifted
    (returned in `conflicts`), never overwriting newer work. Pure `ide/ws_changes_core.py` +
    `tests/test_ws_changes_guard.py`.
  - ✓ **Git-merge approval** — `evolve.sandbox.approve` merges a reviewed branch into an
    integration branch **inside a throwaway worktree**, never touching a live checkout; hard-
    refuses if the target is checked out or on conflict. Pure `evolve/evolve_git_core.py` +
    `tests/test_evolve_git_core.py`. `ide.workspace.changes.mark_merged` clears a landed
    proposal with no file write-back. Loop Lab **Review** tab now surfaces sandbox proposals.
  - This is the safe primitive Phase B's gate and Phase C's deploy build on — and it surfaced
    a fix Phase B must make (§8.1: `evolve.pipeline.promote` is still unsafe).

- **Phase B — Test suite + merge gate.  ○ not started (primitives exist)**
  §6: consolidate `tests/` into contract/behavioural/critical-system tiers; wire
  `evolve.suite`/`selftest` as regression-on-push and **gate-on-promote**; results stamped
  (Phase A) + persisted. **Amendment (RESOLVED 2026-08-08):** the gate promotes via the safe
  isolated-worktree / guarded in-checkout merge — `evolve.pipeline.promote` was reworked (§8.1 #2),
  so the "blind `git checkout <to>`" hazard is gone. What's left for Phase B proper: the tiered
  test suite + wiring `evolve.suite`/`selftest` as the on-promote gate (today `adopt` only
  compile-gates; see §8.3 #2).

- **Phase C — Per-branch dev containers + VSCode/Vera management + UI access.  ◐ mostly done**
  §4: per-branch containers with a port pool, `.devcontainer/` + VSCode tasks/commands,
  Vera-UI + operator launchers, the Ollama/GPU discipline (§4.1) and cleanup/archive
  lifecycle (§4.2).
  - ✓ **C1 — per-branch container pool** (`evolve.sandbox.spawn`, `sandbox_pool.py`: port
    8998→8980 + Redis DB 3–15 allocator; `vera-dev-<slug>` containers; `evolve.sandbox.list`
    unifies primary+spawned). Verified 3 concurrent.
  - ✓ **C2 — Loop Lab sandbox selector** (spawn/list/down in the panel).
  - ✓ **C3 — cleanup/reap lifecycle** (§4.2): `_remove_worktree_robust` (root-owned-file
    fallback) + `evolve.sandbox.prune` (dry-run default, prove-merged-before-delete, dirty-guard,
    never a live-container worktree; `sandbox_reap.py` + tests).
  - ✓ **C5 — resource discipline**: U1 GPU gate (§4.1, live on prod), U2 data-isolation write
    guard (§4.3), U3 leech boot (§4.4). All landed + verified 2026-08-08.
  - ◐ **C4 — in-container git + VS Code, then `.devcontainer/` + tasks.**
    - ✓ **C4a — git + VS Code in dev containers (2026-08-09, branch `feat/git-graph-attribution`,
      commit `4cfde41`):** `git` is baked into `vera:latest`; both the `vera-dev` container
      (`_dev_compose_yaml`) and the code-server sidecar (`sandbox.code.attach`) now mount the
      main `.git` **and** the worktree at its **host-absolute path**, so every git-worktree
      pointer (`.git` file → `.git/worktrees/<name>` → `gitdir` → worktree) resolves inside the
      container and you can `git commit` from the VS Code terminal. `safe.directory='*'` is baked
      into the dev image and passed via `GIT_CONFIG_*` env to the foreign code-server image so the
      root user isn't blocked by git's dubious-ownership guard on host-owned files. Validated
      end-to-end against real containers (branch resolves, commit as BoeJaker, pre-commit
      secret-scan runs). **Security note:** a dev container now has **read-write access to the
      whole `.git`** (all branches/objects), not just its worktree — acceptable because these are
      our own trusted sandboxes and there are no push creds in-container (worst case is local ref
      corruption, git-recoverable), but don't run untrusted loop code that could rewrite history.
      **Rollout note:** takes effect on the next prod restart (prod re-emits the new compose/attach);
      **existing** dev containers keep the old mounts until recreated via `evolve.sandbox.up`.
    - ○ **C4b — VS Code `.devcontainer/` + tasks** (still to build). The docker-socket need for
      the in-container docker-tail collector remains (today it works only because prod is a host
      process). **Retire prod-share editing** (guardrail warn → block) once C4b lands — this is
      the endpoint of the §8.2 #7 boundary work: with git in dev containers (C4a) agents no longer
      need to edit prod's live checkout, and the out-of-tree state boundary (§8.2 #7, `state_paths`)
      stops machine output landing there. See §8.2 #7's "🔗 Linked work" note.
  - ○ **C6 — UI access to dev instances (new; requested 2026-08-08):** see §8.2 — connect to a
    branch's sandbox from within Vera (no new page), SSH/web launchers, sandbox-session indicator.

- **Phase D — Unified observability plane + tests/errors heatmap + auto-postmortem.  ◐ started**
  - ✓ **Unified sandbox log/error/perf collector (§5.2, first slice)** — a background
    collector tails each loop-lab sandbox container's docker logs into capped Redis streams,
    samples `docker stats`, and routes **distinct** errors into `evolve.errors` (Error Radar/
    postmortem), all provenance-stamped. Caps `evolve.sandbox.logs/.metrics/.log_status`; pure
    `evolve/evolve_logs_core.py` + `tests/test_evolve_logs_core.py`; a Loop Lab **Logs** pane.
    (Collects where the app has docker = prod host process; see container-limits amendment.)
  - ○ **Heatmap tab + one-click auto-postmortem + critical-system flagging (§5.3–5.5)** — still
    to build; the hand-written 2026-08-06 postmortem is the template.

- **Phase E — Documentation/traceability automation.  ○ not started**
  Make §2.5 partly automatic: a branch's plan doc created on `evolve.pipeline.run`; the auto-
  postmortem writes to `documentation/postmortems/`; a check warns when a merged branch
  changed code with no matching docs/tests.
  - **A sanctioned place for Vera to update non-code content — WITHOUT a dev container
    (reframed 2026-08-09 per the user).** The user should NOT have to spin up a dev container just
    for Vera to change **documentation, notes, skills, plans, or images**. Vera (running on prod)
    needs a first-class content-edit surface that lands these to `main` safely: a cap like
    `content.edit(path, body)` scoped to an allowlist (`documentation/`, `.claude/skills/`, notes,
    image assets) that commits (author = the human, **no AI trailer**, secret-scan gated) and a
    **scheduled ~24h auto-push** of any pending content changes to GitHub. This is the natural home
    for the doc/skill/plan edits currently done by hand this session.
  - **Cross-container reconciliation — NOT needed (user, 2026-08-09).** We do not need to merge doc
    edits made concurrently from multiple containers; the content-edit surface above lands from
    prod. (Still SEPARATE from §8.2 #7's *generated* files — `docs.build` screenshots stay
    gitignored / emitted outside the tree.)

### 8.1 Build learnings & amendments (2026-08-08)
Recorded from actually building Phases A / A+ / D, so the plan reflects reality, not intent:
1. **Prod branch-tracking — RESOLVED 2026-08-08: prod tracks `main`.** Was: prod ran the
   transitional integration branch `agentic-loop-improvements-3` while approve/promote target
   `main`, so "approve" didn't put code where prod ran, and every deploy was a hack —
   a merge into prod's *live checkout* + restart. **Decision (best practice, and what §2.2
   already says — prod is a deploy target, not a dev workspace):** `main` is the single
   integration branch; feature branches merge into it **off-prod** via the isolated-worktree
   gate; **prod tracks `main` and deploys fast-forward-only** (`git fetch && merge --ff-only
   main` + restart), keeping a clean tree. approve/promote simply target `main` — the
   "dynamic prod-branch / in-checkout merge" idea is **dropped** as the hack it was. **Done:**
   `main` fast-forwarded to `04ce1af` (contains all work); prod's checkout switched to `main`
   (content-neutral — same commit, WIP preserved, running pid unaffected). **Remaining:** one
   restart to load `04ce1af` (activates the log collector + re-stamps provenance `branch=main`);
   the uncommitted mesh WIP still lives in prod's tree and should be committed to a `feat/…`
   branch by its author (its two `.bin` artifacts carry an embedded key — gitignore, don't
   commit). **Ideal end-state (Phase C):** prod runs from an immutable image built from a
   `main` commit — no working checkout on prod at all.
2. **`evolve.pipeline.promote` — RESOLVED 2026-08-08: reworked, now safe.** Was: it ran
   `git checkout <to>` in the **prod** repo root — switching prod's live checkout out from
   under the running process. **Now:** it routes through `_merge_isolated` (throwaway worktree
   when `to` isn't checked out) or `_merge_in_checkout` (guarded in-place merge when `to` is a
   live checkout like prod on `main`: refuses to switch branches, refuses a dirty tree, does a
   `merge-tree` conflict preflight, `merge --no-ff`, and flags `restart_required` rather than
   restarting). Gate enforced (`gate_passed is not True and not force → held`). The blind
   `git checkout <to>` is gone. Push-to-GitHub (creds live on the Windows host) and the restart
   remain deliberate out-of-band steps.
3. **Sandbox containers lack docker + git (Phase C requirement).** Provenance,
   `evolve.sandbox.approve`'s git ops, and the collector's docker-tail work today only because
   **prod runs as a host process** with docker+git. Per-branch containers (§4) must mount a
   docker socket and include git, or these run host-side.
4. **Land content once, via one mechanism; leave no scaffolding (§2.2b).** Cherry-picks,
   throwaway "boot-test" merges, and mixing fast-forward/merge/file-copy created duplicate-
   content commits (phantom "unmerged" branches) and stray branches/worktrees. The 2026-08-07
   cleanup + §2.2b address it; the periodic-sweep guardrail is still to build.
5. **"No error" ≠ "it ran" — probe the real thing, the right way (reinforces §2.6).** A
   dev-container probe (`host.docker.internal:8999`) misreported prod as missing caps; the
   direct host probe was the truth. Verify against prod itself, not through a proxy.
6. **Destructive git ops need proof-first (now §2.2b / §9.9).** Delete a branch/worktree only
   after `git merge-tree`/`git cherry` proves zero unique content — and force-removing a
   worktree can hit **root-owned files** a container left behind (remove them via a root
   container, e.g. `docker run --rm -v …:/wt alpine rm -rf /wt/<name>`).

### 8.2 Requested enhancements (2026-08-08) — backlog

Captured from the user during the C5 build; not yet scheduled.

1. **[SHELVED 2026-08-09 — see §8.4] Revisit U2 (§4.3) and close the direct-write gaps.** The
   write guard covers the fabric + memory chokepoints; auditing every OTHER subsystem's direct
   Neo4j/PG/Chroma writes (dream, goals, projects, worldview, …) for a fully hermetic boundary is
   deferred — the dominant surfaces are already guarded.

2. **✓ Review UI — accepted/rejected history (2026-08-08).** The Loop Lab **Review** tab now
   lists **decided** pipelines below the queue (`promoted`=accepted / `rolled_back`=rejected) —
   verdict, attribution (controller + adopted badge), driving-session **chat drill-down**, and
   decided-at. An auditable review log, not just the queue. (`decidedCard` in evolve_panel.html.)

3. **✓ Connect to a sandbox from inside Vera + indicator (C6, 2026-08-08).**
   - ✓ **SANDBOX-SESSION indicator** — a dev container's UI shows a prominent amber
     `⚠ SANDBOX · <branch> · :<port>` badge in the header + tints it + prefixes the tab title.
     Client-side by port (prod=:8999), branch from `/obs/provenance` — restart-free.
   - ✓ **HTTPS dev containers** — dev containers now serve HTTPS with prod's SAME cert (mounted
     `/certs` ro), and the cert was regenerated with a full SAN (`llm.int, localhost, vera.vera.int,
     127.0.0.1, …` via `TLS_EXTRA_SANS`) so `https://llm.int:<port>` validates. Probes are
     scheme-agnostic (`_dev_resolve_base`: https-verify-off then http, cached) so the http→https
     transition is non-breaking (a pre-existing http container still routes fine).
   - ✓ **In-page embed** — Sandbox tab `▣ embed here` iframes the sandbox's `https://<your-host>:<port>`
     same-securely (no mixed-content), with a ⚠ SANDBOX header. **One-time step:** the self-signed
     cert must be TRUSTED once (import `~/.vera/tls/cert.pem` into the OS/browser Trusted-Root store)
     — a cert warning can't be clicked through inside an iframe, so an untrusted cert renders blank.
   - **Transition note:** NEW sandboxes are HTTPS automatically; a container created before this
     (the primary, another agent's) stays http until re-created (`evolve.sandbox.up`/`spawn`).

4. **✓ Connect to per-branch dev instances via SSH *or* web (C6, 2026-08-08).**
   - ✓ **SSH/exec launcher** — Sandbox tab `⌨ shell` copies `ssh boejaker@llm.int -t "docker exec
     -it <name> sh"` (the git-over-SMB-safe way to a terminal in the container).
   - ✓ **Operator-target registration** — `operator.ensure_target({"kind":"sandbox","branch":…})`
     now resolves the SPECIFIC per-branch container from `evolve.sandbox.list` (not just the
     primary), at its scheme-aware url — so `operator.run`/`operator.mission.run` can drive ANY
     live dev sandbox (the operator browser already ignores the self-signed HTTPS cert).
   - ○ **Full remote-node registration** (`unified-nodes-estate` / `remote-access-workspaces`) so
     dev containers also appear in the nodes/remote estate — a fuller follow-up, not required for
     operator-driving. C6 core is DONE.

5. **✓ Internal UI attribution of git-graph nodes & changes (2026-08-08).** The repo keeps a
   human author with **no AI-attribution trailer** (`git-attribution-internal-only`,
   `no-claude-coauthor-trailer`) — that stays. Internally, the CI/CD pipeline UI + the commit
   DAG now attribute each run/commit to its agent + session: `controller` (claude_code / vera /
   autonomous / user), `session_id`, `via`. **DONE:** pipeline records + `evolve.git.graph`
   (`_commit_attribution_map`) stamp commits; UI shows the badges. Remaining spread: apply the
   same to OTHER infographics (author-map, activity timelines).

6. **✓ Git-graph chat drill-down (2026-08-08); layered session graph still TODO.** `<vera-git-graph>`
   commit nodes now carry an attribution chip that is **clickable → the chat that produced the
   commit** (`window.openSessionChat` → `ide.claude_sessions.history`), and the CI/CD pipeline
   detail has the same "open chat ▸". Verified live (4 commits attributed to claude_code, the
   recent ones session-linked to `cd43896f…`). **Layered session→branches view DONE** too — the
   CI/CD tab groups pipeline runs by driving session (`renderSessionGroups`: session → the
   branches it drove ✓/✗/·, with chat drill-down). **Remaining polish:** render that session
   overlay directly ON the commit-DAG lanes (visual overlay), not just as a grouped list.

7. **⚠ Vera writes generated files into its OWN tracked repo — must stop (found 2026-08-08).**
   Dogfooding the pipeline surfaced this: `docs.build` (operator Playwright screenshot capture)
   regenerates `documentation/assets/**/*.png` **and** the `<!-- VERA:AUTO:screenshots -->`
   sections of the top-level `documentation/NN-*.md` docs **in place**, continuously dirtying
   prod's live working tree. The safe promote (`_merge_in_checkout`) correctly refuses a dirty
   tree, so a background regen **blocks every promote-into-live-checkout** — and it's a moving
   target (can't win the discard→merge race). Other subsystems likely write into the tree too
   (audit needed: dream outputs, operator run artifacts, media, catalog, etc.). **TEMPORARY fix
   applied (commit `2738e9a`):** `documentation/assets/` untracked + gitignored; the regenerated
   top-level `documentation/NN-*.md` are `git update-index --skip-worktree` on prod so their
   churn is invisible (specs/postmortems deliberately NOT skip-worktree'd — they must stay
   committable). **PROPER FIX (TODO, revisit):** generated artifacts must be emitted OUTSIDE the
   tracked tree (a gitignored `build/`/`out/` dir, or object storage), and/or prod runs from an
   **immutable image with no working checkout** (§8.1) — which dissolves this class of problem
   entirely. This is the strongest concrete evidence yet for that end-state.
   - **◐ FIRST STEP LANDED 2026-08-09 (`feat/state-dir-boundary`): a single out-of-tree state
     boundary.** `vera/state_paths.py` centralises where machine-cadence output goes — a single
     `VERA_STATE_DIR` root **outside the repo** (default `~/vera-state`) with `build_output_dir()` /
     `render_output_dir()` / `board_dir()` / `notebook_dir()` / `media_dir()` helpers — plus
     `guard_out_of_tree(path)`, which makes a mis-pointed output **fail loudly at the write**
     instead of silently dirtying prod and blocking every promote. First writer migrated:
     `build_capabilities._OUT_DIR` (was `vera/build/output`, **un-gitignored** — a live hole)
     now resolves under the state root, guarded at import. `tests/test_state_paths.py` pins the
     invariant (incl. the sibling-prefix edge case). **Remaining writers to migrate (the audit
     above):** `render/_out` (already gitignored), `docs.build` assets, dream/media/catalog
     outputs. **This is the linked prerequisite for two other threads** — see cross-refs below.
   - **🔗 Linked work.** This boundary, **C4b** (retire prod-share editing — once agents never
     edit prod's live checkout, the hazard's *source* dries up; git-in-dev-containers C4a landed
     2026-08-09) and **Agent Boards & Comms §9.0 Stage 0** (`~/vera_sandbox/agentic swarm.md` —
     live board/notebook state must live outside the tracked tree; that plan calls a machine-cadence
     agent board "the same hazard, worse") are **one problem**: nothing Vera writes at machine
     cadence may land in the tracked tree. `state_paths` is the shared mechanism; the *versioned*
     write-back (docs/skills/notes Vera legitimately authors) is the separate content-edit surface
     (Phase E), which stages via a branch+commit rather than a raw write into prod's checkout.

### 8.3 Pipeline dogfood findings (2026-08-08) — make it easier to operate

From landing a real UI change end-to-end THROUGH the pipeline (`adopt → review_request →
promote`, all via the Claude Code MCP channel — pipeline `237208d4`, merged `eb5909c`). What
worked: the safe promote, the gate, `controller` attribution, and the guard correctly refusing
a dirty tree (audit trail even records the two blocked attempts then the success). Friction to
fix so an agent (me, or another) can drive it cleanly:

1. **MCP manifest is per-session-static.** A cap deployed mid-session (`evolve.pipeline.adopt`)
   never appeared as an `mcp__vera__*` tool, and `promote`'s cached schema lacked the newer
   `force` param. Workaround used: hand-rolled `POST /mcp/call` with `{caller_kind:"mcp",
   session_id, arguments:{…, force:true}}` — which is exactly what the real bridge does, so it's
   honest, but undocumented. **Do:** (a) document the `/mcp/call` + `caller_kind=mcp` escape
   hatch in the skill so agents can always reach a fresh cap; (b) keep pipeline cap **schemas
   stable/complete** (don't drop params) so a stale manifest still works.
2. **`adopt`'s gate is compile-only** (ast.parse of changed `.py`). Fine for docs/infra, but a
   behavioural change still needs the dev-sandbox suite. **Do:** let `adopt` optionally run the
   critical unit-test tier (or the sandbox suite) and set a real `gate_passed`, so `force` isn't
   the only path for non-trivial changes.
3. **Promote → deploy is a manual two-step** (push from the Windows host + restart via the tool).
   Inherent for the push (creds live on Windows), but **Do:** a small "deploy" helper that, on a
   `restart_required` promote, signals the push + triggers the restart in one gated action.
4. **`session_id` is not on the pipeline record** — `controller` is `claude_code` but not WHICH
   session. This is the §8-Phase-A open item; being built next (ties to §8.2 #5/#6 attribution +
   chat drill-down).
5. **The dirty-tree blocker (§8.2 #7)** was the dominant friction — `docs.build` writing into the
   tracked tree. Already noted + temp-fixed; the real unlock is the immutable-prod-image end-state.

6. **UI-only changes deploy WITHOUT a prod restart.** `/evolve/panel` and `/ui/elements/*.js`
   are read fresh off disk per request, so a promote that merges only panel HTML / element JS is
   **live the instant the merge lands** — no restart. Reserve the restart for `.py` (import-time)
   changes. This matters under concurrency: **2026-08-08 the multi-agent estate went live** (three
   dev containers — `vera-dev`, `vera-dev-loop-lab-foundry-pxe-netboot` @ :8994 for another agent,
   `vera-dev-agentic-loop-improvements-2` @ :8997 — all gated on the one shared GPU slot, the C1/
   U1/U2/U3 payoff), and a prod restart interrupts every agent's prod-side cap calls (adopt/
   promote/git.graph). So: batch `.py` deploys, keep UI changes restart-free, and check
   `ollama.gate` / `evolve.sandbox.list` before a restart.

7. **Second-agent workflow blockers (found running `improve-vera-sandboxed` on another agent;
   mostly FIXED 2026-08-08).**
   - ✓ `evolve.sandbox.exec` was hardcoded to the primary `vera-dev` container/worktree — a second
     agent on its own spawned container could never reach it. Now takes `name`/`branch` to route to
     the right container + worktree (`_resolve_exec_target`).
   - ✓ **Git-over-SMB:** a worktree's `.git` points at a Linux host path Windows/SMB can't resolve,
     so an agent can't run git in the worktree directly. Fix: `evolve.sandbox.exec where=worktree
     branch=<theirs>` runs git natively on the HOST — that is the commit path.
   - ✓ Dev containers now set `VERA_DEV_MODE=1` (was off) so an agent can restart its own sandbox.
   - ✓ `/evolve/panel` now sends `Cache-Control: no-cache` (was cacheable → stale iframe could
     render inconsistently against the always-fresh element JS).
   - ○ **Agents don't know the caps** to create branch+worktree+sandbox and drive the pipeline —
     they backtrack and reinvent the flow. → #8 (atomic start) + the skill update.

8. **✓ Atomic branch+worktree creation in the pipeline (DONE 2026-08-08).** `evolve.pipeline.begin
   (title, spawn=true)` — one call creates the typed branch off main + worktree + own dev container
   + records the pipeline WITH its worktree, and returns `{id, branch, worktree, url, next[]}`
   (the exact next caps). No more reinventing the setup. **Also fixed:** `evolve.pipeline.diff`
   falls back to `git diff base...branch` when a pipeline has no worktree, so the "no worktree for
   this pipeline" error (every Review-tab diff) is gone; and `promote` refreshes the branch's
   commits before merging so a `begin`-stub gets accurate commits/attribution. The skill leads with
   `begin`. Verified end-to-end (own container on the correctly-allocated port, no collision).

9. **✅ CORRECTED 2026-08-09 — the "OOM" was a misdiagnosis. Two independent sessions re-diagnosed
   it and found TWO distinct, real, non-memory causes (both credited below).** `docker inspect`
   shows **`Running=true, OOMKilled=false, ExitCode=0, RestartCount=0`**, `mem_limit=0` (unlimited),
   ~0.6–1 GiB idle usage — there is **no memory problem**. A 16 GiB host swap file was added as
   general headroom but did **not** fix anything (it was never memory). Two things were conflated:
   - **(a) Host-side import-path trap.** `Vera` is a **namespace package** (no `__init__.py`), so
     `from Vera.vera.X import …` resolves to whatever is first on `sys.path` — on the HOST that is
     the **main checkout, not your worktree**. A host `pytest` therefore silently tests the OLD code
     (your symbols "missing", changes "did nothing"), which looks like a failure/crash. Fix: import
     worktree code as lowercase **`from vera.X`** with the worktree root on `sys.path` (the pure-core
     pattern — `board_core.py`, `remote_exec_core.py`, `session_watch_core.py`), or run pytest
     **inside the container** where the worktree *is* `/app/Vera` so `Vera.vera.X` resolves.
   - **(b) In-container full-suite contention with the LIVE app.** Running an app-importing `pytest`
     *inside the container that is already serving the app* can make that container's HTTP go
     **unresponsive** (`exec`/`/health` calls drop) and not recover cleanly — a second full-app
     import re-runs module-init and touches shared state (coordination Redis, sockets) and/or starves
     the box while the live server is trying to serve. NOTE the scale caveat: a **single quick
     isolated** `docker exec python -m pytest one_test.py` completes fine in ~1 s (verified) — it is
     the **longer/full-suite** run against the *serving* container that contends. So don't test in the
     serving container.
   **Both point the same way (real fix, not memory):** run a branch's suite in a **separate ephemeral
   test container** (a fresh `vera:latest` that runs pytest and exits — never the serving container),
   or a dedicated `evolve.sandbox.test`/`evolve.unittest.run` in an isolated process; **bake `git` +
   `pytest` into the dev image** (§8.1 #3 / C4a) so tests don't need a fragile `pip install`; and
   prefer the **pure-core pattern** so most unit tests import `vera.X` and never touch the app at all.
   The `improve-vera-sandboxed` skill §5 documents the correct test-running approaches. Net: the
   in-sandbox test tier is **not memory-blocked** — it needs the right import path (a) and a
   test-isolated exec (b), both now understood.

---

## 8.4 Shelved — deliberately deferred, do at the end

Real but low-urgency; parked here so they don't clog the active phases.
- **U2 hermetic-boundary audit (§8.2 #1, shelved 2026-08-09).** Audit every OTHER subsystem's
  direct Neo4j/PG/Chroma writes and route them through `sandbox_guard.write_blocked()`. The
  dominant surfaces (fabric + memory) are already guarded; the rest is deferred.
- **Git history author remediation (`admin` → `BoeJaker`).** Future commits are fixed (git config
  set 2026-08-08). Rewriting PAST history (`git filter-repo` + force-push) is disruptive — it
  changes every SHA and breaks live clones/worktrees — so do it **at a quiet moment with no other
  agent's branches in flight**, then everyone re-syncs. (User: remediate later.)
- **Retire prod-share editing (guardrail warn → block)** — only after C4 gives dev containers a git
  binary + docker socket, so the sandboxed flow fully replaces prod-in-place edits.

_(Moved OUT of shelved 2026-08-08 per the user — these are active, not deferred: **session overlay
on the DAG lanes** → §8.2 #6 remaining; **spread attribution to author-map / activity timelines** →
§8.2 #5 remaining; **docs cross-container reconciliation** → Phase E — the problem is real and
confirmed to bite (§8.2 #7); only the earlier "over-thinking" wording was wrong, not the problem.)_

---

## 9. Operating rules for Claude (the part I follow now, before the tooling exists)

Until the guardrails are built, I hold to the standard manually:
1. **Branch first, typed** (`fix/…`, `feat/…`) — never work directly on `main`; register a
   pipeline where the flow supports it.
2. **Prefer a dev container** for anything non-trivial (via `improve-vera-sandboxed`);
   edit prod-in-place only when you explicitly ask, and say so.
3. **Commit granularly**, user as author, **no AI attribution trailer**, secret-scan clean.
4. **Ship tests with the change**; a bug fix gets a failing→passing regression test.
5. **Write the plan/report/postmortem to the repo** (`documentation/…`), not only to memory;
   memory just points at it.
6. **Verify on the real running thing** (boot + selftest + feature test; observe UI/runtime)
   before calling it done — never trust `py_compile` alone.
7. **Document the change** in the commit and the relevant doc; leave a breadcrumb from memory.
8. **Tear down scaffolding when a unit lands** (§2.2b): remove its worktree, delete its
   branch, and land its content **once via one mechanism** — never cherry-pick or
   boot-test-merge it onto a side-branch (that duplicates history and litters the graph).
   Leave no worktree or dead branch behind; the git graph shows only in-flight work.
9. **Prove-safe-before any destructive git op.** Before deleting a branch/worktree, prove
   it's redundant (`git merge-tree`/`git cherry`, §2.2b); before any reset/force/overwrite,
   check the target first. Verify, don't act on a hunch and rely on recovery.

---

### Appendix — infra this builds on (already exists)
`evolve.repo.*` (registry) · `evolve.pipeline.*` (branch→gate→review→promote) ·
`evolve.branch.*` · `evolve.sandbox.*` (up/down/exec/fs/diff/snapshot) ·
`evolve.unittest.run` / `evolve.suite.*` / `evolve.selftest` · `evolve.errors.*`
(ingest/list/suggest) · `evolve.git.graph` · `evolve.board` / `evolve.tasks*` /
`evolve.report` · Loop Lab panel (Pipelines, Review tab, git-graph, `branch_pipeline` /
`author_map` / `git_graph` elements) · `ide.claude_sessions.*` (session ingestion) ·
perf/jobs/syslog subsystems · the self-packaged VSIX + `/vscode/connect` +
`remote-claude-client` · session sandboxes + `remote/workspaces`.
