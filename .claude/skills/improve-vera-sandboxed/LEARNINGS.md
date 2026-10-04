# improve-vera-sandboxed — learnings

Append-only autopsy log (format: `skill-smith` §3). Lessons from before 2026-10-04 live inline in SKILL.md as dated
rules; new ones start here. Newest at the bottom.

## 2026-10-04 — `evolve.pipeline.begin` fails after cutting the branch when the primary sandbox is taken
- task: landing the agent-ops skills
- what happened: `begin(spawn=false)` returned "sandbox up failed: primary sandbox is occupied by another branch".
  The branch existed, but there was no worktree and no pipeline.
- cause: without `spawn`, `begin` tries to bring up the primary, which other agents routinely hold
- rule now: on that error, do these steps in order:
  1. Make a hand-made worktree via `evolve.sandbox.exec(where="worktree", branch=<a running mirror>, cmd="cd
     /home/boejaker/Vera && git worktree add .loop-lab-worktrees/<name> <branch>")`.
  2. Claim it at once (`evolve.worktree.claim`).
  3. Commit through the same exec.
  4. Record the pipeline with `evolve.pipeline.adopt(to="bleeding-edge")`.
- evidence: session vera-62, branch feat/agent-ops-vera-coordinate-and-skill-smit
- status: open (belongs in §3)

## 2026-10-04 — the MCP bridge's allow-list hides the board and the registry
- task: filing board items
- what happened: `.mcp.json` allows only `dag.,obs.,sandbox.session.,code.,evolve.`, so `board.*`, `registry.*`,
  `census.*` and `activity.*` are unreachable as tools
- cause: the allow-list predates the board/registry workflow; `.mcp.json` is gitignored, so each machine drifts
- rule now: use `/mcp/call` (§1) for those caps; agent-ops AO-B.1 widens the list
- evidence: shared-planning/agent-ops/PLAN.md §0
- status: open

## 2026-10-04 — Loop Lab `cap` schedules run inline in the scheduler tick
- task: four-minds census design (verification pass)
- what happened: a long `cap` schedule would block every other schedule. It cannot yield at window end, it
  ignores running loops, and a census can start beside it.
- cause: `cap` is not a background kind (`schedule_core.BACKGROUND_KINDS`)
- rule now: never schedule a long-running cap; use a background kind or the idle queue
- evidence: recon-fixes RF-09; `vera/evolve/schedule_capabilities.py:428-431`, `:580-586`
- status: open

## 2026-10-04 — `evolve.pipeline.test` cannot gate a branch no worktree holds any more
- task: landing intel fixes (claude:d5e6f826)
- what happened: `adopt` recorded pipeline 9be2da0d, but its gate run was cut by a prod restart. A re-run of
  `evolve.pipeline.test(id)` then returned "no worktree for this pipeline (branch step may have failed)", because
  the hand-made worktree had meanwhile been switched to a new branch stacked on it.
- cause: the pipeline resolves the worktree by branch at test time; a hand-made worktree holds one branch at a time
- rule now: keep a branch checked out until its pipeline is promoted. To stack work, either finish the first
  pipeline first, or land the stacked branch (it contains both) and mark the first pipeline `blocked` with
  "superseded by <id>" in the review.
- evidence: pipelines 9be2da0d (superseded) and 741cd57d (landed, f13bcefb)
- status: open (belongs in §3/§7)

## 2026-10-04 — `/health` can say no census while `census.live` blocks every idle-queue job
- task: verifying intel's first live jobs (claude:d5e6f826)
- what happened: `/health` showed `census.busy: false` (the scheduled control loop: intent-core-probe done 7/7),
  while `census.live` showed a 12-goal default census in progress. The idle queue's busy probe reads `census.live`,
  so every background job (intel, embed backfill, agent re-index) stayed blocked with "a census is running". Twice
  it was a run outside the scheduler: once wedged by a stuck model, then a fresh run driven through a long-lived
  older Vera process (its loop held the GPU gate as `LLM:<that pid>:...`).
- cause: `/health`'s census block reflects only the scheduled harness control; `census.live` reflects any loop
  registered as a census goal
- rule now: before concluding "no census", read BOTH `/health` and `census.live`, and
  `/workshop/agent_loop/sessions?status=running` (its `updated_at` tells live from stale). If the queue says "a
  census is running" and the schedule list has nothing in window, report it to the owner rather than yielding or
  cancelling a run you don't own.
- evidence: shared-planning intelligence/HANDOVER-2026-10-04-claude-d5e6f826.md; census sessions 2f20173f,
  3377d775, b32f075e
- status: open (belongs in §0)
