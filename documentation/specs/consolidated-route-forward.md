# Vera Dev-Lifecycle × Agent-Swarm — Consolidated Route Forward

**Status:** active roadmap · **Date:** 2026-08-12 · **Owner:** admin (BoeJaker)
**Supersedes nothing; synthesises two plans:**
- `documentation/specs/dev-lifecycle-and-repo-hygiene.md` (in-tree, the standard)
- `~/vera_sandbox/agentic swarm.md` (out-of-tree, Agent Boards & Comms)

> This is the single "where next" view. The two source plans stay authoritative for
> their own detail; this doc plots the route across both, records what's **QC-verified
> done** (checked live 2026-08-12, not just marked), and lists the tech debt found while
> verifying. Update this doc as milestones land; keep the source plans' status glyphs in
> sync.

---

## 1. Snapshot — what is DONE and QC-verified (2026-08-12)

Verified live against prod this pass unless noted.

**Repo hygiene / dev-lifecycle**
- ✓ **Pre-commit + commit-msg hooks** (§3) — block direct-`main`, enforce typed branch,
  reject AI author + AI-attribution trailer, secret-scan. All four **fired in a fresh
  worktree** test; clean BoeJaker `fix/` commit passed. (Plan §3 was stale, now ✓.)
- ✓ **Safe pipeline promote** (`_merge_in_checkout`) — guarded in-checkout merge (on-branch,
  clean-tree, conflict-preflight, `restart_required` flag). Used all session.
- ✓ **Out-of-tree state boundary** (`state_paths`) — render/build/board/notebook/media write
  out of tree; tree stays clean. Machine-output leak (§8.2 #7) closed for current writers.
- ✓ **Periodic scaffolding sweep** (§8.1 #4) — `evolve.sandbox.prune` (merged+clean+no-live-
  container only; unmerged→`review`; dirty protected) + `delete_merged_branches` + scheduled
  hourly `_scaffolding_sweep`. C3 root-owned removal fallback works. Backlog cleared: 2
  worktrees + 23 merged branches. ⚠ see tech-debt T1.
- ✓ **Content-edit surface** (Phase E) — `content.edit`/`content.status`, path-locked to
  `documentation/` (not assets) + `.claude/skills/`, out-of-tree `docs/content-sync` worktree,
  hook-gated commit, safe-merge to main. Verified end-to-end (write→commit→merge→served;
  identical=no-op; allowlist rejects `vera/`).

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
(C4b's endpoint) once agents use `content.edit` instead of hand-edits.

**M2 — Traceability guardrails (Phase D/E lite).** Doc/test-presence check on merge +
auto-postmortem writer. Cheap, high-leverage, reuses the pipeline + `evolve.errors`.

**M3 — Test-tier gate (Phase B).** Tier `tests/`; wire the merge gate to run the
critical-system tier. This is the safety net both plans lean on.

**M4 — Board ↔ pipeline sync (Stage 2 start).** `board.sync` first (local, no GitHub needed) —
reflect pipeline/run/error state onto board items so the board is the single work view.
Then GitHub provider + `board.budget`.

**M5 — Vera executor tier (Stage 4).** Independent of 2/3; local models doing idle-only board
work with honest review. Gated by capacity pool (already built).

**M6 — Autonomy relaxation (Stage 5).** Only after M2/M3 give the safety net; widen HITL
gates as trust accrues.

Ordering rationale: stabilise what's live (M0) → finish the half-built content loop (M1) →
lay the safety net (M2/M3) before widening autonomy (M4–M6).

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

---

## 5. Cross-reference

| This roadmap | dev-lifecycle plan | swarm plan |
|---|---|---|
| §1 done | §3, §8.1 #4, Phase E, §8.2 #7 | Stage 1 (§9.1), §6.2 |
| M2/M3 | Phase B, Phase D, Phase E | — |
| M4/M5 | — | Stage 2/§6.3, Stage 4 |
| M6 | — | Stage 5, §5.3 |
| T1/T2 | §8.1 #4 (sweep) | §6.5 lifecycle |

Board tracking: an umbrella board item per plan (dev-lifecycle, agent-swarm, this route-forward)
plus work items for M0–M6 and T1–T5, tied to their branches when opened.
