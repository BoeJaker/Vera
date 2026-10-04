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
