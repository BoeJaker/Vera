# Vera Dev-Lifecycle × Agent-Swarm — Consolidated Route Forward

**Status:** active roadmap · **Date:** 2026-08-12 (M3.5 added, M3.6 guardrail added, M3 gate landed 2026-08-16) · **Owner:** admin (BoeJaker)
**Supersedes nothing; synthesises two plans:**
- `documentation/specs/dev-lifecycle-and-repo-hygiene.md` (in-tree, the standard)
- `~/vera_sandbox/agentic swarm.md` (out-of-tree, Agent Boards & Comms)

> This is the single "where next" view. The two source plans stay authoritative for
> their own detail; this doc plots the route across both, records what's **QC-verified
> done** (checked live 2026-08-12, not just marked), and lists the tech debt found while
> verifying. Update this doc as milestones land; keep the source plans' status glyphs in
> sync.

> **HARD RULE - branch/merge discipline (added 2026-08-16, after a direct-to-main incident).**
> **ALL new code lands on `bleeding-edge`. `main` advances ONLY on the user's explicit,
> unambiguous go-ahead**, and only via `evolve.bleeding_edge.promote_to_main` - never a
> per-feature `adopt`/`promote` with `to="main"`. This is NOT a convention to remember; it
> must be *enforced by the system* (see **M3.6**), because relying on `adopt`'s `to="main"`
> default already broke it once (four changes landed on `main`, rolled back, re-landed via
> `bleeding-edge`). Until M3.6 lands: treat every `to=` argument as main-until-checked, and
> never promote `bleeding-edge` -> `main` without an explicit, unambiguous instruction.

---

## 1. Snapshot — what is DONE and QC-verified (2026-08-12)

Verified live against prod this pass unless noted.

**Repo hygiene / dev-lifecycle**
- ✓ **Pre-commit + commit-msg hooks** (§3) — block direct-`main`, enforce typed branch,
  reject AI author + AI-attribution trailer, secret-scan. All four **fired in a fresh
  worktree** test; clean BoeJaker `fix/` commit passed. (Plan §3 was stale, now ✓.)
- ✓ **Safe pipeline promote** (`_merge_in_checkout`) — guarded in-checkout merge (on-branch,
  clean-tree, conflict-preflight, `restart_required` flag). Used all session.
  **Refined 2026-08-16 (`cdcc3a9`):** the clean-tree guard now refuses only on **tracked**
  uncommitted work (`tracked_dirty_lines` in `evolve_git_core.py`), not on unrelated
  **untracked** files — an open scratch/spec doc left in the standing bleeding-edge worktree
  no longer blocks every promote. `git merge` still refuses to overwrite a colliding untracked
  file on its own, so a genuine collision fails safely. 5 critical-tier tests.
- ✓ **Out-of-tree state boundary** (`state_paths`) — render/build/board/notebook/media write
  out of tree; tree stays clean. Machine-output leak (§8.2 #7) closed for current writers.
- ✓ **Periodic scaffolding sweep** (§8.1 #4) — `evolve.sandbox.prune` (merged+clean+no-live-
  container only; unmerged→`review`; dirty protected) + `delete_merged_branches` + scheduled
  hourly `_scaffolding_sweep`. C3 root-owned removal fallback works. Backlog cleared: 2
  worktrees + 23 merged branches. ⚠ see tech-debt T1.
- ✓ **Content-edit surface** (Phase E) — `content.edit`/`content.status`, path-locked to
  `documentation/` (not assets) + `.claude/skills/`, out-of-tree `docs/content-sync` worktree,
  hook-gated commit, safe-merge to main. Verified end-to-end (write→commit→merge→served;
  identical=no-op; allowlist rejects `vera/`). ⚠ see tech-debt T7 (broken as of 2026-08-16).

**Sandbox lifecycle (the Ollama-contention work)**
- ✓ **Idle-reaper** — spawned containers idle >30m auto-pause (exempt: primary, VS Code
  sidecar, pinned, live board dispatch); `evolve.sandbox.reap` (dry-run/execute) +
  `evolve.sandbox.pin`; `list` reports paused/pinned. Safe via auto-unpause-on-exec.
- ✓ **Bring-up on use** — `_sandbox_probe(auto_unpause=True)` resumes a paused primary on
  test-use; exec auto-unpauses spawned. Reaped→paused→auto-wake works.
- ✓ **Per-container files/diff** — `fs.*`/`diff` route by name/branch (deep run-targeting),
  verified: `fs.list?name=<spawned>`→that worktree.
- ✓ **GitHub push key on llm.int** — repo-scoped ed25519 **deploy key** generated + host-only
  `url.insteadOf`. ⚠ **pending the user adding it** (D1) before prod can push.

**Agent Boards & Comms (swarm plan, Stage 1)**
- ✓ **Stage 1 complete + deployed** — `board.*` (items/upsert/move/get/claim/comment/inbox/
  help/import_plan/index/provider) [LIVE], plan grouping (work vs context umbrella),
  `board.context`, `board.dispatch` orchestrator (claim→pipeline→work→gate, 3 executor
  tiers), capacity pool + `capacity.*` seats, session-watch + resume/release/policy, Sessions
  pane. QC: board 36 items, capacity/status, sessions.watch all respond.

**Loop Lab UI (this session's roadmap — all 13 items live)**
- ✓ Pipeline rich UI + colored/syntax-highlighted diffs + draft-idle callout; **Swarm** live
  tab; **global context bar** (repo/sandbox, per-element override) wired to boards+swarm;
  make-active run-targeting; **Sandbox tab rewrite** (sandbox-centric list + Connection
  panel); repo-aware unit test; board branch filter; LHM vertical scroll; self-test reports
  its runner; git-branch-discipline codified.

---

## 2. What remains — grouped

### A. Dev-lifecycle plan (in-tree)
- **Phase B — test suite + merge gate** (○). Consolidate `tests/` into contract/behavioural/
  critical-system tiers; wire the gate. Primitives exist (`evolve.unittest.run`, suites).
- **Phase D — heatmap + auto-postmortem + critical-system flagging** (○). One-click postmortem
  from a failure; tests/errors heatmap tab; flag critical systems.
- **Phase E remainder** — auto-postmortem writer to `documentation/postmortems/`; a
  **doc/test-presence check** (warn when a merged branch changed code with no docs/tests);
  `content.remove`/rename; **scheduled content auto-push** (after D1).
- **C4b** — VS Code `.devcontainer/` + tasks; then **retire prod-share editing**
  (guardrail warn→block) now that content-edit + git-in-containers exist.
- **§8.2 #715** — agents don't know the caps to drive the pipeline (discoverability / a
  "how to land a change" skill).
- **File split — `dag_workshop_capabilities.py`** (○, session finding, not in either source
  plan). 22,818 lines holding five loop-engine generations (v3–v7) in one file; the 3
  duplicated tool-redirect rules already centralized in-file this session. See **M3.5**.

### B. Agent-swarm plan (out-of-tree)
- **Stage 2 — GitHub provider + files** — `board.sync` (pull evolve pipeline/run/error onto
  linked items), `board.budget` (write budget), GitHub as the projected work plane.
- **Stage 3 — Gitea** (lower priority).
- **Stage 4 — Vera executor tier** (independent) — local-model executors doing real board
  work, idle-only (§5.3), batched (§5.4), honest weaker-reviewer (§5.5).
- **Stage 5 — autonomy relaxation** — widen from HITL-every-step toward run-until-blocked as
  trust grows; auto-resume loop (currently off by default).
- **§0.3 / §6.5 open** — the instigation+spawn orchestration matrix (system→container→activate
  via vscode-push/ssh/in-container-CC/hand-to-Vera); `ide.remote.probe` (needs a live remote).

### C. Tech debt found this session (QC) — see §4.

---

## 3. The route forward — sequenced

**M0 — Stabilise the sweep/lifecycle. ✓ DONE (2026-08-12, main `cf6dee1`).** Fixed T1 +
T2 below. **Verified by reproduction:** a merged-branch sandbox with a STOPPED container
(absent from `docker ps`, the exact restart-window trigger) survived the sweep — the old
code would have reaped it.

**M1 — Close the content loop (after D1).** User adds the deploy key → verify prod push →
scheduled content auto-push → `content.remove`/rename. Retire prod-share editing warning
(C4b's endpoint) once agents use `content.edit` instead of hand-edits. Also needs T7
(content-sync worktree repair) before `content.edit` is usable again at all.

**M2 — Traceability guardrails (Phase D/E lite).** Doc/test-presence check on merge +
auto-postmortem writer. Cheap, high-leverage, reuses the pipeline + `evolve.errors`.

**M3 — Test-tier gate (Phase B). ✓ CORE COMPLETE (M3.1–M3.4 + perf-gating first slice all
landed 2026-08-16; only perf-gating follow-ons remain).** Tier `tests/`; wire the merge gate to run the critical-system
tier — the safety net both plans lean on. Closes **T6**. Broken into:

- **M3.1 — Critical-tier gate for any branch worktree. ✓ DONE (bleeding-edge).** `gate_passed`
  now runs the critical tier (`pytest -m critical` via `evolve.unittest.run`, in an isolated
  ephemeral `vera:latest` container) for ANY branch with a live worktree, alongside the
  `ast.parse` compile check — both must pass. `_branch_worktree` resolves a pool sandbox OR a
  plain `git worktree`. **Proven:** every `adopt` this session returned `gate_passed: true`
  after the tier ran live.
- **M3.2 — Coverage matrix cap + panel view. ✓ DONE (bleeding-edge).** `evolve.tests.matrix`
  parses `pytest --collect-only` into {module, tests, critical} (38 modules / 578 tests / 7
  critical), surfaced in the evolve panel's "Unit tests" section with a race-to-green strip.
  **Proven:** cap returns the matrix; ⚠ the panel view only renders in prod after a `main`
  promotion (UI serves off the running checkout).
- **M3.3 — Backfill the critical tier. ✓ DONE (bleeding-edge).** Promoted reap-safety
  (`test_sandbox_reap`), pre-push guard (`test_pre_push_guard`), main-merge guard
  (`test_main_merge_guard`), and merge-tolerance (`tracked_dirty_lines`, in `test_evolve_git_core`)
  into the `critical` marker set (`tests/conftest.py`). **Proven:** critical tier is green and
  grew across the session to **78/78** (adds M3.6 guard, pre-merge guard, merge-tolerance, and
  M4 `board_sync`). More backfill remains as systems grow (e.g. content path-lock, reaper
  idle-logic).
- **M3.4 — Test generation. ✓ DONE (bleeding-edge `daa4db9`).** `evolve.tests.generate` takes a
  branch's changed pure `vera/**/*.py` modules (excludes `tests/`, `__init__`, existing `test_*`),
  and LLM-proposes pytest that follows the repo convention (`sys.path.insert` + lowercase
  `from vera.X import …`). **Proposes for review — never writes files** (copy from the panel,
  commit, then promote into `_CRITICAL_MODULES` once solid). Pure decision logic (module filter,
  import/test-path mapping, fence-strip) is in `vera/evolve/test_gen_core.py` with **11
  critical-tier tests**. **Integrated into the visuals:** a "Test generation (M3.4)" card in the
  Loop Lab Unit-tests panel (branch input → Generate → per-module proposals with copy), beside
  the M3.2 matrix. **Live-verified:** generated a correct 5.7 KB `test_sandbox_reap.py` proposal
  end-to-end. `evolve.tasks.generate` (benchmark tasks) is unrelated. Follow-on: a one-click
  "save to branch" from the panel (today it's copy-and-commit).
- **Perf-based gating. ◑ FIRST SLICE DONE (bleeding-edge `e08ba46`).** `perf.gate` reduces
  `perf.scan` (event-loop stalls ≈ socket flap, Ollama saturation/contention, host CPU/RAM,
  stale consumers, zombie jobs) to a promote verdict `pass|warn|fail`. Pure evaluator
  (`perf_gate_core.py`, thresholds + strict-vs-advisory blocking + top-findings) with **12
  critical-tier tests**; live-verified returning `pass`. Wired into `evolve.pipeline.promote`
  for **code pipelines**, best-effort: records the verdict + a perf step, **advisory by default**
  (never blocks a merge on transient system load), `VERA_PERF_GATE_STRICT=1` makes a `fail` hold
  it, `force=true` overrides. Env: `VERA_PERF_GATE_MAX_CRIT`/`MAX_WARN`. **Follow-ons:** a
  socket-flap-specific detector (vs. the general event-loop-stall proxy), a chat/Ollama p95
  response-time finding, a perf-verdict indicator in the Unit-tests/Perf panel, and a decision
  on turning strict mode on once thresholds are tuned against real load.

**M3 is now LIVE IN PROD.** Released to `main` (`f8497f4`/`ac35b34`, then `15644fe` on explicit
user go-ahead) and **prod was restarted 2026-08-16** — so the whole session's work (M3.1–M3.3
gate/matrix/backfill, the M3.6 guards, merge-tolerance, board.sync, and the T9 trunk-protection
fix) is running in prod's process, not just on disk.

**M3.5 — Agentic-loop runner file split (`dag_workshop_capabilities.py`).** (○, added
2026-08-16, session finding — not tracked in either source plan.) After M3 lands the
test-tier gate — a structural refactor of this size wants a regression net, and right now
the merge gate is just `ast.parse` (a compile check, not a behaviour check). Split the
22,818-line loop-runner file into focused modules. Groundwork already landed in-file this
session: `_v5_route_write_call` centralizes the 3 write-protection redirect rules (proven-
file re-author, raw-write-against-proven-file, code-write gate) that were hand-duplicated
across the chain-hop and single-tool dispatch paths. Remaining candidates, in the order
the audit recommends: step-executor (`_v5_run_step_inner`, 3,238 lines, the largest single
function in the file), the v6 control/verify/journal/branch machinery, the v7-only
tiering/clarification block, the HTTP/SSE route surface, and — lowest priority, keep
as-is — the legacy v3/v4 engines (still externally referenced, not worth the risk of
touching without cause).
**Hold until the file's concurrent-edit pressure eases.** As of 2026-08-15/16, five+ other
branches had live or paused sandbox WIP touching this exact file or its immediate
neighbors (`fix/chat-memory-inject-unify-v2`, `feat/dag-planner-memory-v2`,
`refactor/activity-single-embed`, `feat/sandbox-mainline-mirror`, plus this session's own
two). A structural move now would rebase-conflict against all of them — this wants a quiet
window, not a moment when the whole swarm has WIP against the current layout.
**Mechanical risk, confirmed by dependency analysis on the v7-tiering candidate (not yet
executed):** each extracted module has on the order of 20 real cross-references back into
the rest of the file (`CAPABILITY_REGISTRY`, `emit_event`, `_safe_ollama_generate_dw`,
etc.). A naive top-level `from dag_workshop_capabilities import (...)` creates a circular
import, since the original file needs to import the extracted functions back. This
codebase's existing convention for that (see `_sandbox_mod()`/`_orch`-style lazy
`sys.modules.get()` + `getattr()` dispatch) needs each reference converted individually —
real, careful work per module, not a mechanical bulk move.
Full findings + the detailed split-order rationale: session audit artifact ("Loop Stall
Postmortem", 2026-08-15).

**M3.6 — Main-merge guardrail (anti-recurrence). (◐ IN PROGRESS — parts 1-2 landed on
`bleeding-edge` 2026-08-16; only part 3 remains; HIGH priority.)**
The 2026-08-16 direct-to-main incident proved branch discipline as a *convention* is not
enough — it was broken by relying on `evolve.pipeline.adopt`'s default `to="main"`, landing
four changes on `main` before they were rolled back and re-landed via `bleeding-edge`. Build
a system that makes a local `main` merge *impossible without explicit user authorization* and
*reminds* any actor the moment they try:
1. **Cap-layer refusal. ✓ DONE (bleeding-edge `1520ba3`, pipeline `5e949d7b`).**
   `evolve.pipeline.adopt`/`promote` now refuse `to` in {`main`, `master`, resolved-mainline}
   unless `authorize_main` equals the explicit sentinel `I-HAVE-EXPLICIT-USER-GO-AHEAD`
   (a per-call token, never a default or a left-set env). `adopt`'s default was already
   `bleeding-edge`. Pure guard (`main_merge_refusal`/`protected_mainline_names`/
   `MAIN_MERGE_SENTINEL`) in `evolve_git_core.py`, wired into both caps, + 10 critical-tier
   tests (`tests/test_main_merge_guard.py`; critical tier now 57/57). The one sanctioned path
   to `main` stays `evolve.bleeding_edge.promote_to_main`, which merges directly and does NOT
   route through the guarded caps (so it's unaffected). **Not yet active in prod** — it lives
   on `bleeding-edge`; it only takes effect after a `bleeding-edge`→`main` promotion + restart
   (itself the gated step). Part 3's loud reminder is delivered here at the cap layer (the
   refusal message names the HARD RULE + the sanctioned path).
2. **Git-level hook (defense in depth). ✓ DONE (bleeding-edge `6ee0de6`, pipeline `a024f25c`).**
   The existing `pre-commit` hook already blocks direct NON-merge commits to `main`
   (`VERA_ALLOW_MAIN_COMMIT=1` override) but deliberately exempts merge commits — so a raw
   `git merge <branch>` onto the live `main` checkout was ungated. New
   `tools/hooks/pre-merge-commit` refuses a merge landing on `main`/`master` unless
   `VERA_ALLOW_MAIN_COMMIT=1` (the same sanctioned-deploy override). The pipeline's own
   safe-merge (`_merge_in_checkout`/`_merge_isolated`) now sets that override on its merge, so
   `promote_to_main` still works while a by-hand merge is refused. 3 critical-tier real-merge
   tests (`tests/test_pre_merge_commit_guard.py`; block / override-allows / feature-branch-OK)
   prove both the hook and the bypass mechanism. **Residual gap (documented in the hook):** a
   *fast-forward* merge onto `main` creates no commit, so no hook fires — rare/odd by hand, and
   the cap-layer guard (part 1) plus `--no-ff` pipeline merges cover the real paths.
3. **`promote_to_main` confirm-gate. (○ remaining.)** Gate the sanctioned path itself on an
   explicit-authorization argument (today calling it *is* the deliberate act, but a single
   call ships everything on `bleeding-edge` to `main` + restarts prod with no confirm).
Reuses the exact pattern already on `bleeding-edge` (the remote-push guard); this is its
local-merge twin. Finish parts 2–3 BEFORE M4/M5/M6 widen autonomy.

**M4 — Board ↔ pipeline sync (Stage 2 start). ◐ IN PROGRESS (2026-08-16).** `board.sync`
first (local, no GitHub needed) — reflect pipeline state onto board items so the board is
the single work view. Then GitHub provider + `board.budget`.
- **`board.sync` cap ✓ + hardened (bleeding-edge `457b908`).** Reflects a linked pipeline's
  decision/gate/review onto its item — lane + an idempotent progress comment (dedupe via
  `sync_sig`), never yanking an item out of a human-parked `done`/`dropped`. The lane mapping,
  fingerprint, and parked-lane guard were extracted from the cap into pure `board_core.py`
  helpers (`pipeline_lane`/`pipeline_sync_sig`/`should_apply_lane`) + a **critical-tier
  `test_board_sync` (13 cases)**; a review-found gap was fixed (`decision=rolled_back` now maps
  to `dropped`, was silently `in_progress`). Only reflects the `pipeline` link today (items
  carry no `run`/`error` link field yet).
- **✓ scheduled poll (bleeding-edge `ac0f6bd`).** `board.sync` now runs on a timer
  (`board.sync.poll`, every `VERA_BOARD_SYNC_INTERVAL_S`=300s, toggle `VERA_BOARD_SYNC_ENABLED`),
  mirroring the evolve scaffolding-sweep scheduling — so the board self-updates as pipelines
  move through adopt→review→promote, no manual call. Idempotent via `sync_sig`. This is the
  spec's "board.sync polls on a schedule"; it makes the board a live planning/inter-agent surface.
- **○ remaining — GitHub provider + `board.budget`.** Blocked on the deploy key (D1/T3); the
  public write path also needs the full `secret_scan` wired in (board's `_scan_secret` is
  deliberately conservative defence-in-depth until then).

**M5 — Vera executor tier (Stage 4).** Independent of 2/3; local models doing idle-only board
work with honest review. Gated by capacity pool (already built).

**M6 — Autonomy relaxation (Stage 5).** Only after M2/M3 give the safety net; widen HITL
gates as trust accrues.

Ordering rationale: stabilise what's live (M0) → finish the half-built content loop (M1) →
lay the safety net (M2/M3), which also de-risks the M3.5 structural refactor → M3.5 once the
file's concurrent-edit pressure eases → widen autonomy / add coordination visibility (M4–M6).

---

## 4. Tech debt & known issues (from QC 2026-08-12)

- **T1 — Scaffolding sweep restart-race over-reaping. ✓ FIXED (`cf6dee1`).** Protection now
  keys off `docker ps -a` (existence in any state) + pool membership (every registered
  sandbox worktree is protected regardless of container state), so a paused/stopped/
  transitioning sandbox is never treated as dead. Belt-and-suspenders: the scheduled sweep
  skips a startup-grace window (`VERA_SWEEP_STARTUP_GRACE_S`). Original description:
  The hourly sweep ran in the restart
  window when paused/starting containers were momentarily absent from `docker ps`, so it
  reaped worktrees that BACK live (paused) sandboxes — including the primary's (it was on a
  merged branch). Fix: protect any worktree with a **pool entry** (not just one with a live
  `docker ps` container), and/or skip the sweep for ~N seconds after startup, and/or consult
  `docker ps -a`. Until fixed, keep `VERA_SCAFFOLD_SWEEP_ENABLED` handy.
- **T2 — Stale pool descriptors / half-alive sandboxes. ✓ FIXED (`cf6dee1`).** `evolve.sandbox.prune`
  now self-heals: a pool entry whose worktree is missing is reconciled (its useless
  port-holding container removed + descriptor dropped) so the branch can re-spawn clean.
  Original description: After T1, several pool entries point
  at reaped worktrees while their paused containers remain; `evolve.sandbox.down(name=…)`
  fails on them (worktree-gone). Fix: self-heal — a sandbox whose worktree is missing is dead;
  reconcile (remove container + pool entry, or `sandbox.up` to recreate). The primary
  self-heals on next `up`; 2 spawned ones (author-caps, foundry) are currently orphaned.
- **T3 — Deploy key pending (D1).** Prod cannot push to GitHub until the ed25519 deploy key is
  added with write access. Content still lands on `main` locally; GitHub push piggybacks the
  Windows flow meanwhile.
- **T4 — `content.edit` is write-only.** No `content.remove`/rename yet (self-test doc was
  removed via raw git). Small follow-on.
- **T5 — Prod push-from-Windows + SMB ref staleness** remain the deploy mechanics until D1
  lands; verify `local==origin` after every push.
- **T6 — Merge gate has no behavioural check (from QC 2026-08-15/16, agentic-loop audit).**
  `evolve.pipeline.adopt`'s gate for the `vera` repo is currently `ast.parse` on changed
  `.py` files only — a compile check, not a test run. Every fix landed in the 2026-08-15/16
  agentic-loop session needed hand-written unit tests and manual live verification in a
  sandbox to get real confidence, because the gate itself couldn't provide it. This is the
  concrete case for **M3**.
- **T7 — `content.edit`'s worktree link is broken (found 2026-08-16, landing this very
  edit).** `content.status` reports the out-of-tree worktree at
  `/home/boejaker/vera-state/content-sync-wt`, but `content.edit` fails with `fatal: not a
  git repository: /home/boejaker/Vera/.git/worktrees/content-sync-wt` — the administrative
  link inside the main repo's `.git/worktrees/` registry that ties the out-of-tree checkout
  back to the repo is missing or corrupted, even though the checkout directory itself still
  exists and `content.status` (which doesn't touch git) still reports it as healthy. Not
  investigated further this session (out of scope, didn't want to touch a shared worktree's
  git internals blind) — this specific doc edit landed via the normal Loop Lab pipeline
  instead (`docs/route-forward-m3-5`). Needs a real fix before Phase E / M1 can rely on
  `content.edit` again — likely a `git worktree repair` or `git worktree add` re-link
  against the existing directory, but verify no in-flight edits would be lost first.
- **T8 — `bleeding-edge` mirror couldn't refresh: root-owned `.git/refs/heads/loop-lab/`.
  ✓ FIXED (2026-08-16).** Every `promote` into `bleeding-edge` had reported
  `standing_container_refresh: {ok:false, "cannot lock ref … Permission denied"}` because
  `.git/refs/heads/loop-lab/` (and its reflog `.git/logs/refs/heads/loop-lab/`, and a handful
  of loose objects under `.git/objects/`) were owned `root:root` from an earlier
  in-container/root git op, so the `boejaker` process couldn't take a ref lock. Fix applied:
  `sudo chown -R boejaker: /home/boejaker/Vera/.git` (swept the whole class, not just the one
  dir), verified with `fsck --connectivity-only` clean. Confirmed end-to-end: the next
  `promote` reported `standing_container_refresh: {ok:true, mirror refreshed}`. Merges were
  never affected (only the mirror fast-forward), and prod (`main`) was never involved.
- **T9 — the hourly sweep force-deleted the `bleeding-edge` trunk after a release. ✓ FIXED on
  `bleeding-edge` (`9be3ce9`); needs a prod restart to protect the LIVE sweep.** Found
  2026-08-16 when `refs/heads/bleeding-edge` vanished from the repo (only `origin/bleeding-edge`
  remained). **Root cause:** after a `bleeding-edge`→`main` release, `bleeding-edge` is an
  ancestor of `main` (0 unique commits), so it reads as "merged". The scheduled
  `_scaffolding_sweep` runs `evolve_sandbox_prune(delete_branches=True,
  delete_merged_branches=True)` hourly, which then deleted the trunk **two ways**: the
  `bleeding-edge` branch via its reaped worktree + `delete_branches` (`git branch -D`), and
  `loop-lab/bleeding-edge-mirror` via `delete_merged_branches`. With `refs/heads/bleeding-edge`
  gone, `_default_pipeline_base` silently falls back to `main` — so **every agent's
  `pipeline.begin`/`promote` starts defaulting to `main`** (mitigated only if the M3.6
  cap-guard is live in prod). **Immediate recovery:** recreated `bleeding-edge` +
  `loop-lab/bleeding-edge-mirror` from `origin/bleeding-edge` (`c88a721`, a strict ancestor of
  `main` — nothing lost); kept `bleeding-edge` **branch-only** (no worktree) so neither sweep
  path can hit it in the interim. **Durable fix (`9be3ce9`):** `TRUNK_PROTECTED_BRANCHES` in the
  pure `sandbox_reap` core (bleeding-edge / main / master / both mirrors) — `plan_reap` keeps
  them unconditionally and both cap-layer deletion sites skip them; 3 critical-tier regression
  tests reproduce the incident. **Reaches prod's running sweep only after a `bleeding-edge`→
  `main` release + restart** — until then the branch-only trunk + the live mirror container are
  the interim guards.
- **T10 — the standing bleeding-edge container served a STALE/BROKEN mirror worktree. ✓
  recovered 2026-08-16; root fix pending.** After several promotes reported
  `standing_container_refresh: {ok:true, "mirror refreshed + container restarted"}`, the mirror
  worktree (`.loop-lab-worktrees/bleeding-edge-mirror`) was actually frozen commits behind
  `bleeding-edge` with a **severed git link** (`fatal: not a git repository:
  .git/worktrees/bleeding-edge-mirror`), so the container served old code (a just-landed cap
  read as "Unknown capability"). The refresh's success report is not trustworthy — it updates
  the mirror ref but the worktree checkout can silently fail. **Recovery:** `evolve.sandbox.down`
  the container (remove_worktree) → `git worktree prune` → `git branch -f
  loop-lab/bleeding-edge-mirror bleeding-edge` → `evolve.bleeding_edge.container.ensure` rebuilt
  it clean (now current, caps registered, `https://localhost:8984`). **Root fix (pending):** the
  refresh must verify the worktree HEAD actually moved (and `git worktree repair` / rebuild if
  the link is severed), not just report ok — same class as T7's content-sync worktree link.

---

## 5. Cross-reference

| This roadmap | dev-lifecycle plan | swarm plan |
|---|---|---|
| §1 done | §3, §8.1 #4, Phase E, §8.2 #7 | Stage 1 (§9.1), §6.2 |
| M2/M3 | Phase B, Phase D, Phase E | — |
| M3.5 | — (session finding, 2026-08-16) | — |
| M3.6 | Phase B (guardrail, 2026-08-16 incident) | — |
| M4/M5 | — | Stage 2/§6.3, Stage 4 |
| M6 | — | Stage 5, §5.3 |
| T1/T2 | §8.1 #4 (sweep) | §6.5 lifecycle |
| T6/T7 | Phase B / Phase E | — |
| T8 | — (infra: root-owned `.git` refs) | §6.5 lifecycle |

Board tracking: an umbrella board item per plan (dev-lifecycle, agent-swarm, this route-forward)
plus work items for M0–M6 (incl. M3.5) and T1–T7, tied to their branches when opened.
