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

## 2026-10-04 — CORRECTION to "`evolve.pipeline.test` cannot gate a branch no worktree holds any more"
- task: landing intel's MoE routing (claude:d5e6f826)
- what happened: `evolve.pipeline.test(id)` returned "no worktree for this pipeline" again, this time with the
  branch still checked out in the hand-made worktree. Meanwhile the pipeline's own gate (run by `adopt`) was sitting
  at "critical tier queued behind another gate in this process", and it later passed and promoted.
- cause (corrected): pipelines recorded by `adopt` from a hand-made worktree carry no worktree, so `pipeline.test`
  can never re-run their gate, whatever is checked out. `adopt` runs the gate itself; its critical tier can queue
  behind other sessions' gates for a long time, and the client call times out while the server keeps going.
- rule now: after `adopt`, never call `pipeline.test`; read `evolve.pipeline.get(id)` (`current`, `steps`,
  `gate_passed`) until the gate finishes. If you add commits after adopting, adopt again (a new pipeline gates them
  all) and mark the old one superseded. And never leave a background "adopt then promote" script running while you
  supersede its pipeline: it will still promote.
- evidence: pipelines a1d0c70e (promoted by the still-running script after it was marked superseded) and eb6a77b3
  (re-adopted, gated both commits, 7dc90221)
- status: open (belongs in §7)

## 2026-10-04 — Patch scripts and background jobs from a Windows Git Bash session
- task: node test pool + node parity (vera-d2; shared-planning/test-runners; board 01733d71)
- what happened: Python patch scripts passed through a bash heredoc failed or were garbled again and again
  ("unexpected EOF while looking for matching `''", backslashes eaten, raw-string UNC paths turned into
  `\192...` and FileNotFoundError). Separately, a parity check launched with a plain `&` looked dead (no
  process visible) but was alive, so re-launching it ran two parity checks at once.
- cause: Git Bash on Windows re-processes heredoc bodies in ways that break quotes and backslashes even with a
  quoted delimiter; and `&` jobs from the Bash tool may or may not survive the call, invisibly either way.
- rule now: write any patch script longer than a line with the Write tool into the scratchpad and run it
  (`python p_x.py`); never put Python with quotes or backslashes in a heredoc. Use `run_in_background` for
  anything long, never `&`; to check on it, read its output file. Assert every replace anchor (`count == 1`)
  so a mangled patch fails loudly instead of silently.
- evidence: this session's scratchpad p_*.py files; duplicate parity runs (two sets of pytest.shard jobs at
  17:28:46, one "waiting" behind the other)
- status: open (belongs in §1)

## 2026-10-04 — A client-side release waiter on "nothing running" can wait forever
- task: releasing behind the gate queue at the owner's request (vera-d2)
- what happened: a waiter polling "no gate running" never fired: as running gates finished, queued ones
  started. Prod already had the right mechanism.
- cause: `evolve.release.prod` (b70bf83c, live since 4 Oct) itself holds a non-forced release while gates run
  and sets a drain key so NEW gates wait for the release - a client waiter has no such drain.
- rule now: to release behind running gates, just call `evolve.release.prod` (census=finish); it returns
  held=true with the gates it waits for, and goes out when they finish. Do not build a client-side waiter.
  Read gate state from pipeline `steps` (a `critical-tests` step = done), not `current`, which still says
  "running critical tier" after a gate ends.
- evidence: release 0340fcd1 (held for 2 gates, then released); pipeline f3ebd092 record
- status: open (belongs in §8)

## 2026-10-05 — health alone was not enough to justify census control
- task: admit the W5-05 Langfuse live trial (item `w5-05-live-observability-backends`)
- what happened: census control was changed after reading `/health`; the owner reported the census was already
  stopped, and the control change unnecessarily disturbed its state
- cause: the admission check did not reconcile `/health` with `census.control`, `census.live`, and the referenced
  loop session before mutation
- rule now: read all three census surfaces and the live session before any control write; an owner correction
  triggers revalidation, not another compensating control write
- evidence: census `intent-core-probe-run8`; board item `w5-05-live-observability-backends`
- status: folded into SKILL.md v1

## 2026-10-05 — generated env values and unbounded probes delayed a disposable backend trial
- task: run the isolated Langfuse half of W5-05 (pipeline `f56b4369`)
- what happened: shell-sourcing the generated env file failed on an unquoted display value containing spaces; a
  readiness `curl` without a total timeout then held the sandbox exec channel while the web service initialized
- cause: the harness mixed Docker env-file syntax with shell-source syntax, and the readiness probe had no bound
- rule now: quote shell-sourceable generated values or parse only exact credential keys, and give every readiness
  probe explicit connect and total timeouts
- evidence: shared-planning/observability-backend-comparison/EVIDENCE-2026-10-04.md; pipeline `f56b4369`
- status: folded into SKILL.md v1

## 2026-10-07 — sandbox down uses the stop preflight action
- task: release the merged Dream execution sandbox (pipeline `7e6e19f6`)
- what happened: `evolve.sandbox.preflight(action="down")` refused a healthy,
  merged sandbox with `unknown_action`, although the mutation is named
  `evolve.sandbox.down`.
- cause: preflight names the intended effect (`stop`), while the mutation uses
  the command name (`down`); the skill did not document that mapping.
- rule now: before `sandbox.down`, preflight the exact target with
  `action="stop"`, require `allowed=true`, then dry-run and execute `down`.
- evidence: pipeline `7e6e19f6`; traces
  `3bef9521-daa1-4466-bfc3-250f04dda83b` and
  `867a2002-d939-49b9-806d-131b10d36ab3`
- status: folded into SKILL.md v2

## 2026-10-07 — workplan status is closed and has no completed value
- task: close the Dream and schedule execution workplans (pipelines `7e6e19f6`,
  `740a7b59`)
- what happened: revision-guarded updates were rejected for both
  `status="completed"` and `status="done"`.
- cause: the capability has a closed status vocabulary that the skill did not
  document and exposes no terminal value through the current interface.
- rule now: do not invent workplan status values. Mark every step completed and
  record the receipt in the final step; use the board and pipeline as the
  authoritative terminal state.
- evidence: traces `c6142d3a-e40b-4fc1-a409-b240d923fdeb` and
  `ccb2aae7-a13c-439c-8517-2739fa3dfc5d`
- status: folded into SKILL.md v2

## 2026-10-13 — standalone and pipeline critical callers did not share execution
- task: validate the JEPA retrieval-binding durability repair (SW-09, pipeline
  `a9e77ffd`)
- what happened: `evolve.unittest.run` began the exact 1,019-file critical
  tier, then `evolve.pipeline.test` with identical paths, marker, timeout and no
  extra arguments waited instead of joining. After the first 12-shard run
  completed 7,400/0, the pipeline launched a second 12-shard run.
- cause: the documented cross-caller single-flight assumption is not true in
  the deployed Loop Lab implementation, even though each caller is internally
  single-flight.
- rule now: use one authoritative gate caller only: `pipeline.test` for a
  pipeline-managed change, or `unittest.run` for an independent branch; never
  sequence both for the same gate.
- evidence: pipeline `a9e77ffd`; unittest history at
  `2026-10-12T22:55:00Z`; board `loop-lab-critical-single-flight-join`
- status: folded into SKILL.md v3

## 2026-10-16 — loader entry points and stateful helpers need different import rules
- task: add Home Assistant owning-boundary effect evidence (pipeline `f200db75`)
- what happened: the first critical gate failed
  `tests.test_loader_modules_have_no_parent_package` because
  `homeassistant/ha_capabilities.py` used `from .ha_effects import …`. An earlier
  effect-context test had also shown that mixing `Vera.vera...` and `vera...`
  creates two `ContextVar` instances and loses attribution.
- cause: capability entry modules can be loaded by file path with no parent
  package, while Python also treats the uppercase and lowercase package paths
  as different module identities. “Always relative” and “lowercase for
  worktrees” are therefore not valid production-import rules.
- rule now: use canonical `Vera.vera...` imports in path-loaded entry modules;
  keep stateful package internals on one consistent identity; run the loader
  guard and an identity-sensitive focused test before the full gate.
- evidence: pipeline `f200db75`; first gate 7,445 passed/1 failed, repaired gate
  7,446 passed/0 failed, 36 skipped; promoted `658bb3f5`
- status: folded into SKILL.md v4
