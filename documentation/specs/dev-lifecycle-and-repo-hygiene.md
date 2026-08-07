# Vera Dev Lifecycle, Repo Hygiene & Observability — Plan / Standard

**Status:** proposed (plan) · **Author of plan:** drafted with Claude Code · **Date:** 2026-08-07
**Owner:** admin (user) · **Home of the standard once ratified:** this file (versioned in-repo)

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
| **Import-time breakage py_compile misses** (`loop-lab-multi-repo-foundation`) | forward-ref default-arg → `NameError` at import → took down the whole `evolve.*` namespace | Gate runs `evolve.selftest` (real boot), not just compile (§2.6, §6) |
| **Stale sandbox image** (`loop-lab-sandbox-and-impl-timeline`) | dev sandbox on stale `vera:latest` → missing caps → `loops.run` 404 | Per-branch containers built from a **fresh** image; probe requires a workhorse cap |
| **Reload to load backend changes** | Python changes need a process reload; panel HTML needs only a refresh | Dev-container loop includes reload; the test/verify step assumes it |
| **Concurrent collaborators** (`git-repo-concurrent-changes-normal`) | unfamiliar diffs on disk are normal, not anomalies | Isolation removes the confusion; don't investigate others' diffs by default |
| **Shared cluster backend** (`vera-cluster-shared-backend`) | instances share one Neo4j+Redis; per-node caches drift | Dev containers get isolated (or namespaced) backing state so testing doesn't corrupt prod data |
| **Windows/Bash quirks** (`bash-tool-broken-use-powershell`, `vera-runs-on-linux-not-windows`) | Bash tool dies (cygwin); Vera runtime is POSIX | Tooling assumes PowerShell on the Win host, POSIX in containers |

---

## 8. Phased roadmap

Each phase is independently shippable and leaves the system better than it found it.

- **Phase A — Provenance + first guardrails (highest leverage, lowest cost).**
  §5.1 stamping (git sha/branch/dirty/session on the event+log+run streams) + §3 pre-commit
  hooks (attribution, secret-scan, no-commit-to-main, branch-name) + the first
  **critical-system regression tests for the planner** (the ones that would have caught both
  recent incidents). Fold the branch-naming + "document plans/reports in-repo" rules into how
  I (Claude) work immediately.

- **Phase B — Test suite + merge gate.**
  §6: consolidate `tests/`, wire `evolve.suite`/`selftest` as regression-on-push and
  gate-on-promote. Results stamped (Phase A) and persisted.

- **Phase C — Per-branch dev containers + VSCode/Vera management + UI access.**
  §4: generalize the dev sandbox to per-branch containers with a port pool, `.devcontainer/`
  + VSCode tasks/commands, Vera-UI + operator launchers for each container, the **Ollama/GPU
  contention discipline** (§4.1) and the **cleanup/archive lifecycle** (§4.2). Retire
  prod-share editing (guardrail flips from warn → block).

- **Phase D — Unified observability plane + tests/errors heatmap + auto-postmortem.**
  §5.2–5.5: one store, the Loop Lab heatmap tab with playback, deep-dive → generated
  postmortem, critical-system flagging + comms alerts + gate integration.

- **Phase E — Documentation/traceability automation.**
  Make §2.5 partly automatic: a branch's plan doc is created on `evolve.pipeline.run`; the
  auto-postmortem writes to `documentation/postmortems/`; a check warns when a merged branch
  changed code with no matching docs/tests.

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
