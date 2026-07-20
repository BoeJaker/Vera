# 33 — Loop Lab (Evolve): CI/CD for the agentic loops

`vera/evolve/evolve_capabilities.py` is a **CI/CD suite for Vera's agent
engines** — and, through cap-type tasks, a smoke-test harness for every other
subsystem. It implements the "**Vera runs, Claude edits, until Vera can take
over**" pattern as a pipeline: test → evaluate → edit-on-a-branch → re-test →
gate → **promote or roll back**. The critic/editor can be a local Ollama model
or an external API (Claude / ChatGPT) via the providers registry.

Panel: **Loop Lab** (🧪) — `evolve_panel.html`, its own top-level tab, with a
live activity strip (event-bus + poll driven) and tabs for Overview, Tasks,
Runs, Improve, **CI/CD**, **Sandbox**, Variants, Settings.

> **If nothing seems to run**, press **✓ Self-test** (top bar / `evolve.selftest`).
> It pre-flights Redis, task seeding, the `loops.run` engine (a real 1-cycle
> run), and the critic + editor providers, and tells you exactly which link is
> broken. Local-only loop runs are slow (CPU inference) — the live strip and
> per-stage `current` field show they're progressing, and **Stop** now hard-
> cancels a run stuck mid-loop.

---

## 1. The cycle

```
task ─ run (loops.run / cap call) ─→ trace + final output
     ─ checks (ground truth) ──────→ pass/fail per assertion
     ─ critic LLM (rubric) ────────→ score 0-10 + critique + edit suggestions
     ─ editor LLM ─────────────────→ next tuning variant (knobs + prompt preamble)
     ─ rerun with variant ─────────→ … until target_score or max_rounds
     ─ promote best variant ───────→ overlay merged into every loops.run of that profile
```

`combined` score = programmatic checks (50%) + critic score (50%), so tuning
can't be gamed by a generous LLM — the ground-truth checks anchor it.

---

## 2. Tasks & checks

A **task** is a benchmark. Three types:

- **`loop`** — a `goal` run through a loop **profile** (via `loops.run`), with
  an `allowed_caps` floor. Exercises the full engine: triage, planning, tool
  calls, synthesis.
- **`cap`** — a single capability call (`cap` + `args`). A fast smoke test for
  any subsystem (`fabric.datasets`, `dream.scheduler.status`, `llm.generate`, …).
- **`sim`** — an agent loop run against the **business simulation**
  (`business.sim.start` → `evaluate` → the loop operates the sim → `score`).
  The combined score IS the mechanical sim-ledger outcome (0-100 → 0-10) — real
  ground truth, no LLM critic needed. This is how Loop Lab "uses simulations to
  loop-improve."

Every task carries **checks** (programmatic ground truth) and a **rubric** (LLM
judge guidance). Check types: `contains` / `not_contains` / `regex` /
`cap_called` / `min_steps` / `max_steps` / `max_seconds` / `final_nonempty` /
`json_valid` / `no_error`.

Tasks seed on first start (tags `core`, `loop`, `smoke`, `dream`, `sim`,
`markets`). Merge-seeded — your edits are never clobbered. The **suite runs fast
cap smoke-tests first** so the counter moves immediately (0→1→2 in seconds)
before the slow agent-loop tasks; the live strip shows the current task, its
elapsed time, and the loop's live tool calls.

| Cap | Purpose |
|---|---|
| `evolve.tasks` / `evolve.task.upsert` / `evolve.task.delete` | Task CRUD |
| `evolve.task.run` | Run one task now (optionally with critic assessment) |
| `evolve.goal.run` | Run an **ad-hoc goal** through a loop + critic analysis (optionally save it as a task) |
| `evolve.tasks.generate` | **LLM-generate** benchmark tasks from a goal/subsystem, saved (tag `generated`) for comparative runs |
| `evolve.selftest` | Pre-flight redis · tasks · `loops.run` · critic · editor — run this first if a suite "does nothing" |

---

## 3. Critic / editor providers

Both accept a provider **spec**: `ollama[:model]` runs on the local cluster
(`llm.generate`); `anthropic[:model]` / `openai[:model]` / any stored provider
id routes through `providers.chat` (sealed keys + usage/cost tracking). Add API
keys under **Workers & Ollama → API** (the providers registry).

Default config: critic `ollama`, editor `anthropic` — Claude tunes while the
local models run. `evolve.assess.compare` scores one run with two critics and
reports agreement (score delta + pass/fail agreement): that's how you know the
local critic is ready to take over from Claude.

| Cap | Purpose |
|---|---|
| `evolve.providers` | Providers usable as critic/editor |
| `evolve.assess` | Critic scores a stored run → score + critique + edit suggestions |
| `evolve.assess.compare` | Two critics on one run → agreement |
| `evolve.config.get` / `evolve.config.set` | Critic/editor/target/rounds/allow_code_edits |

---

## 3b. What you're improving — a categorised target

Loop Lab improves **any part of Vera**, picked as *category → target* (not a raw
"profile" dropdown). `evolve.targets` returns the tree:

| Category | Targets | Improve by |
|---|---|---|
| **Specialist loops** | the 15 loop profiles | variant overlay (tune) or code edit |
| **Agents** | `agent.list` (system prompt / model / caps) | prompt/model variant or code edit `agents.py` |
| **Agentic loops** | engines v5–v8 | engine-knob variant or code edit |
| **Chat** | the chat system | code edit |
| **System components** | cap groups (dream, markets, fabric, …) | code edit via CI pipeline |

Every run/session/pipeline carries a `target` (`specialist:coding`, `agent:coder`,
…) that resolves to the loop profile used to exercise it. The **Test** tab's Run
composer and the Improve form both use this selector.

## 3c. Running a test — background, never "failed"

A single test used to be a blocking POST that outlived the proxy timeout on the
CPU cluster (bare "failed" toast, nothing live). **`evolve.run.start`** launches
the run in the background and returns a `run_id` immediately; the panel streams it
live (`evolve:<run_id>` agent events → the `<vera-agent-loop-output>` element)
and polls `evolve.run.status`. The **Test** tab is the home: compose Loop / Cap /
Task / Suite, hit Run, and watch every stage + tool in the **dynamic workflow
diagram** (Input → Implementer → Reviewers → Fixer → Output).

## 3d. Adversarial evaluation — implementer → reviewers → fixer

Evaluation is **adversarial** (Bun-article Loop 1): after the loop runs
(implementer), **N reviewers** (`config.reviewers`, default 2) each see *only* the
goal + trace + output — not the rubric — and are told *"assume it's wrong;
enumerate concrete failures."* Their failures are unioned and the score is the
harsher aggregate; the **fixer** (the background edit queue on gpt-oss:20b/CPU)
proposes the next variant from those failures. `evolve.workflow` events animate
the diagram. Toggle in Settings (`adversarial`, `reviewers`).

## 4. Improve sessions — test → evaluate → synthesize

`evolve.improve.start` launches a background session that loops three visible
phases per round:

1. **Test** — run the agent loop for each task (watch it live in the agentic
   loop UI, sandbox-first).
2. **Evaluate** — the critic scores each run (score + critique + failures).
3. **Synthesize** — propose a better **variant**: engine-knob overrides
   (whitelisted + clamped) plus a **prompt preamble** prepended to the agent's
   system prompt. **This runs on the background edit queue** (§4b).

Repeat until `target_score` or `max_rounds`. The **Watch** tab shows the phase
stepper, the live `<vera-agent-loop-output>` element (the real agentic-loop UI),
the streaming critique, and the synthesised variants.

**Goal source** — a session can tune against:
- `tasks` (default) — the profile's existing benchmark loop tasks;
- `goals` — an explicit list, or the system's long-term goals (`goals.list`);
- `generate` — LLM-generated tasks from a named subsystem/objective.

| Cap | Purpose |
|---|---|
| `evolve.improve.start` / `status` / `list` / `cancel` | Session lifecycle (with `goal_source`, `goals`, `generate_from`) |
| `evolve.code.queue` | Dispatch one code suggestion to the Claude Code work queue |

## 4b. Background edit queue — synthesis on gpt-oss:20b (CPU node)

The synthesis phase is enqueued (not run inline) and drained by a single
background worker on a cheap **local model pinned to a CPU node** — by default
`gpt-oss:20b` — so editing stays off the critical path, off the GPU/API, and
every action is **visible and editable before it runs**. Configure model/node in
Settings → *Background synthesis*; set `sandbox_mode`'s sibling `editq_enabled`
off to run the `editor_provider` (e.g. Claude) inline instead.

| Cap | Purpose |
|---|---|
| `evolve.editq.list` / `get` | The queue + one action (prompt, result, status) |
| `evolve.editq.update` | Edit a **queued** action's prompt before it runs |
| `evolve.editq.cancel` / `worker` | Cancel an action / ensure the worker is up |
| `evolve.instances` | Ollama instances, for pinning to a CPU node |

**Code suggestions** from the critic/editor are never auto-applied. They
accumulate on the session; with `allow_code_edits` on (or one click / one
`evolve.code.queue` call) they go to the remote **Claude Code work queue**
(`ide.remote.queue.add`) — the "Claude edits" half.

---

## 5. Variants & the overlay

The best variant of a session can be **promoted** to the active **overlay** for
its profile. `loops.run` merges the overlay **between** the profile defaults and
the caller's explicit overrides (`_apply_evolve_overlay`), so every production
run of that profile picks up the learned knobs + preamble, while a caller can
still override any of them. Clearable at any time to revert to stock.

| Cap | Purpose |
|---|---|
| `evolve.variants` | Variants for a profile + the active overlay |
| `evolve.variant.promote` / `evolve.variant.clear` | Promote / revert overlay |
| `evolve.overlay.get` | The active overlay (read by `loops.run`) |

---

## 6. CI/CD pipelines — branches, gating, rollback

A **pipeline** carries one change through baseline → apply → test → gate →
promote/rollback. Two kinds:

- **`kind=variant`** — a tuning variant. Applied at runtime by `loops.run` via
  the overlay, so it needs no code reload: the pipeline measures a baseline
  suite, runs the suite with the candidate variant, gates on the score delta
  (default: must not regress), and — if it passes and `auto_promote` — promotes
  the overlay. Fully in-process.
- **`kind=code`** — a source change (from the editor/critic). The pipeline cuts
  a git branch `loop-lab/<id>`, hands the edit to the **Claude Code work queue**
  scoped to that branch, and (when the dev sandbox is running that branch) tests
  it there and gates. **Merging to main is always manual** (`evolve.pipeline.promote`
  → `git merge`); rollback deletes the branch (`evolve.pipeline.rollback`). A bad
  change never touches main — git is the rollback mechanism.

| Cap | Purpose |
|---|---|
| `evolve.git.status` | Repo branch, dirty flag, loop-lab/* branches |
| `evolve.branch.create` / `evolve.branch.delete` | Work-branch primitives |
| `evolve.pipeline.run` | Run a pipeline (background); `kind`, `profile`, `variant_id`/`edits`, `gate_threshold`, `auto_promote`, `auto_test` |
| `evolve.pipeline.list` / `evolve.pipeline.get` | Pipeline history / full stage trace |
| `evolve.pipeline.promote` | Promote: variant→set overlay, code→merge branch to main |
| `evolve.pipeline.rollback` | Roll back: variant→clear overlay, code→delete branch |

### Change sources — review & observability feed the pipeline

The critic isn't the only source of improvements. Two rich streams distil into
the same gated code pipeline:

- **Dream source review** (`evolve.pipeline.from_review`) — the autonomous
  source review's technical recommendations for a subsystem area
  (`dream.review.area_report`) are distilled into concrete edits and launched as
  a code pipeline.
- **Perf & observability** (`evolve.observe.scan`) — perf findings (`perf.scan`),
  event-loop stalls (`perf.stalls`) and recent errors are distilled into
  suggested code fixes; `launch=true` turns them into gated code pipelines. The
  `observe_selfheal` dream trigger runs this nightly (report only). This is the
  self-improvement mechanism for the perf/observe panel: errors and performance
  issues become reviewable, gated fixes.

### Errors work-queue — errors → suggested edit → approve → commit

The **Errors** tab is the Bun-style *errors-as-a-work-queue*: errors flow in, a
fix is suggested, you approve, and it becomes a commit. The observability and
ollama/workers monitors don't move here — they **push** their errors in:

- `evolve.errors.ingest` — the entry point any monitor calls to send an error
  (`source`, `title`, `detail`, `meta`); repeats dedup into a counter.
- `evolve.errors.sync` — pulls the current signals into the queue and suggests a
  fix for each. It ingests **only real problems**: `perf.scan` findings with
  severity **crit/warn** (healthy `ok`/`info` statuses like "no loop stall for
  15 min" are *ignored*), actual event-loop **stall/hang** events from
  `perf.stalls`, and `dream.sensor.syslog_errors` (which already aggregates
  ollama/workers `ollama.request_error` events). A config-gated background tick
  (`errors_autosync`, default off; the tab's *auto-sync* toggle) runs this on a
  cadence so errors flow without watching. `evolve.errors.clear` purges the queue.
- When a perf finding carries a **built-in safe remediation** (`remediable`/
  `remediation_id`, e.g. prune stale consumers / sweep stuck jobs), the suggested
  fix is that one-click action and **approve applies it directly** via
  `perf.remediate` — no LLM code edit, no pipeline.
- `evolve.errors.suggest` distils a concrete fix for a queued error (new →
  suggested); `evolve.errors.approve` launches a **gated code pipeline** from the
  suggestion (branch → edit → sandbox test → manual merge), **gated against the
  profile that actually failed** (from the item's stored `meta.profile`, not a
  generic default — so the re-test validates the real component) so the fix
  flows to a commit (suggested → approved); `evolve.errors.dismiss` drops one.
- Every failed **test** (run error, or ≥3 failed tool calls) auto-ingests here
  too, tagged with its `run_id` — dedup keys on the STABLE profile/cap+goal, not
  the run's own ephemeral id, so repeated failures of the same test bump one
  item's counter (and refresh which run it links to) instead of flooding the
  queue with "new" entries every retry. A run blocked by the sandbox posture
  (below) is an environment issue, never filed as a test defect.

The tab renders a kanban (new · suggested · approved · committed) plus an
animated flow diagram — nothing is applied without a human approve.

### Activity-aware timeouts — tests can run indefinitely

Single tests launched from the composer are **not killed on a fixed clock**: a
loop under test may legitimately run for a very long time. Instead a watchdog
tracks **activity** — a liveness fingerprint (`updated_at`, refreshed on every
loop event and immune to Redis's event-list trim cap, unlike a raw count) from
the run's own session *plus* any goal-matched strategic sub-sessions, filtered
to those that actually started after this run began (their persisted
`started_at`, not the session index's last-touched score) — and cancels only
after `run_idle_timeout_s` (default 300s) with **no new activity**, or at the
`run_max_s` hard ceiling (default 2h; `0` = unlimited). When the run executes
in the dev sandbox, the HTTP hop to it (`evolve.sandbox` `/mcp/call`) has its
client-side timeout raised to match — an indefinite test would otherwise be
silently killed by the sandbox call's own fixed timeout regardless of the
watchdog. Suite/benchmark and improve-session runs keep their fixed per-task
`timeout_s` — benchmarks must stay bounded.

### Know what you're testing · test failures feed remediation

- The composer's **ℹ** button shows the target's description + config: loop
  profile (engine, agent, caps, defaults, skills) with any promoted tuning
  **overlay**, agent model/domain-caps/system-prompt, engine knobs, or the IDE
  instance record (`evolve.target.info`).
- Every failed test (run error, or ≥3 failed tool calls) is **auto-ingested
  into the errors work-queue** tagged with its `run_id` — so a broken component
  flows test → error item → suggest → approve → gated remediation pipeline.
  The result column links to it; error cards link back to the run.

### Live implementer timeline — reattach, survives navigation

The Test home's implementer panel (`<vera-agent-loop-output>`) is fed the same
robust way the DAG Workshop feeds its loop view: on run start it **reattaches**
over SSE to the run's persisted agent-loop session
(`/workshop/agent_loop/reattach?session_id=evolve:<run_id>`), which replays every
stored step then tails the live ones. Because the events live in Redis
(`vera:loop:events:evolve:<run_id>`, written for any `agent_loop*` event carrying
that `session_id`), the timeline **populates reliably** (not via the best-effort
global event bus) and **restores when you navigate away and back** — switching to
Improve/Watch and returning re-attaches and shows the run still in progress
instead of looking idle. (Runs executed inside the dev sandbox emit into the
sandbox's own Redis, so their timeline shows there, not on prod.)

### Test & loop over remote IDE code

Every registered remote IDE (from `ide.remote.instances`) appears as an **IDE
code** target in the categorised selector. `evolve.ide.improve` runs a goal over
that workspace via `ide.remote.run` (Claude Code CLI or a Vera agent editing over
SSH), streaming into the workflow diagram and Recent runs. It is **diagnose-only
by default** (`apply=false` → it reviews and proposes the exact edits without
changing files); set `apply=true` to let it make the changes. In the Test
composer, pick the IDE-code category, type a goal, and toggle *apply changes*.

## 6b. Sandbox-first by default — tests can't touch real Vera

The default posture is **`require`** (`config.sandbox_mode`) — Loop Lab must
only ever operate on a containerised copy of the source, never the real one:

- **`require`** (default) — tests MUST run in the sandbox; a run is refused
  (recorded as `blocked`, not a test failure — never filed in the errors
  work-queue) with a clear reason if none is up. Bring one up from the Sandbox
  tab (`evolve.sandbox.ensure`) before testing.
- **`prefer`** — agent-loop and cap tests run in the dev sandbox whenever one
  is up (isolated Redis DB, separate process), **silently falling back to
  in-process** (touching real Vera) otherwise — only use this if you
  understand and accept that gap.
- **`off`** — always run in-process (fast, but a loop *can* touch real Vera).

A config value already stored in Redis always overrides this default (see
`_get_config`), so changing the code default doesn't disturb an existing
install's setting.

Execution routes through the sandbox's universal `/mcp/call` endpoint, so any
cap (including `loops.run`) runs against the sandbox's isolated state. As defence
in depth, a **test denylist** (`config.test_denylist`) strips external-effect cap
families (mail, tg, exec, docker, git, provision, ssh, mesh, deploy, comms,
business, commerce, accounts, `ide.remote.`, …) from every test loop's toolkit,
so even an in-process test can't act on the real world. `sim` tasks use the
business-sim's own `is_sim=1` isolation and never route to the dev sandbox.

`evolve.sandbox.ensure` brings up an isolation sandbox on a `loop-lab/sandbox`
branch if none is running — one click to get the safe posture. The header pill
(🛡) shows the current mode; each run in the Runs table shows **where** it ran.

**The real source is never touched.** Loop-lab branches are created with a bare
`git branch` (no checkout — prod's working tree stays on `main`, always), and
the branch's code only ever exists in its **worktree**
(`.loop-lab-worktrees/<branch>`), materialised by the self-healing
`_ensure_worktree` (prunes stale registrations; restores prod to `main` if a
legacy state left it on a branch). Code-pipeline edits are **worktree-pinned**:
`ide.remote.queue.add`/`ide.remote.run` accept a `workdir` override and the
pipeline passes the worktree path, so the editor's shell is hard-`cd`'d into the
branch copy — it cannot land an edit on the primary checkout. `evolve.code.queue`
routes through the same gated pipeline instead of queueing a raw edit.

**Approve-to-push.** Code changes live on branches and only reach `main` when you
explicitly promote (`evolve.pipeline.promote` → `git merge`) — the "push to real
source" step, which is itself audited. Nothing is auto-merged.

### Test any cap · unit-test any part of Vera

- `evolve.cap.test` — call **any** capability with args and grade it against
  checks, respecting sandbox-first mode (a write cap runs against isolated
  state). Panel: Tasks → Quick tests.
- `evolve.unittest.run` — run **pytest** or a **compile/import check** over any
  path and gate on the exit code (default `compile` = safe, executes nothing).

### Audit — verbose change & rollback log

Every mutating action — promote, rollback, merge-to-source, branch create/delete,
code-queue, pipeline decision, sandbox up/down, config change, unit-test — is
appended to a durable log (`evolve.audit.list`, panel **Activity** tab), so there
is a full trail of what changed, when, and why. Every rollback is itself logged.

## 7. Dev sandbox — test a branch in isolation

`evolve.sandbox.*` runs an isolated Vera on another port (`VERA_DEV_PORT`,
default **8998**) that executes a **branch's** code, so code changes are tested
running-and-reloaded before they reach main:

- a **git worktree** of the branch at `<repo>/.loop-lab-worktrees/<branch>` —
  prod's working tree stays on main;
- a generated `docker-compose.dev.yml` runs `vera-dev` from the `vera:latest`
  image with the worktree **bind-mounted** over `/app/Vera`, on the dev port and
  an **isolated Redis DB** (`VERA_DEV_REDIS_DB`, default 3) so it can't corrupt
  prod state. `vera:latest` is **local-only** (not on any registry), so
  `sandbox.up` first calls `docker.image.ensure` to **build it from the repo
  Dockerfile** if it's missing (and the compose sets `pull_policy: never`) —
  docker never attempts the doomed `pull access denied for vera` that used to
  break the sandbox;
- `evolve.sandbox.snapshot` copies Loop Lab config + tasks into the dev DB so it
  tests with the same suite;
- a `kind=code` pipeline, when the sandbox is up on its branch, runs the suite
  **through the sandbox's own `/evolve` HTTP API** (the branch code) and gates on
  that.

Requires docker on the orchestrator host. Everything degrades gracefully: no
sandbox → the code pipeline holds for manual review instead of failing.

| Cap | Purpose |
|---|---|
| `evolve.sandbox.status` | Descriptor + live health probe of the dev port |
| `evolve.sandbox.up` | Worktree + `vera-dev` container + snapshot for a branch |
| `evolve.sandbox.down` | Stop container, remove worktree (+ the VS Code sidecar) |
| `evolve.sandbox.snapshot` | Copy Loop Lab state into the dev Redis DB |
| `evolve.sandbox.diff` | Unified `git diff <base>` of the worktree (committed + uncommitted edits; untracked files listed) — powers the panel's **Changes** pane |
| `evolve.sandbox.code.attach` | code-server sidecar (`vera-dev-code`, port `VERA_DEV_CODE_PORT`/8996) with the worktree bind-mounted, registered behind the `/vscode/loop-lab-dev/` same-origin proxy |
| `evolve.sandbox.code.detach` | Remove the sidecar (worktree untouched) |
| `evolve.sandbox.exec` | **Terminal** — run a command inside the `vera-dev` container (`docker exec`) or the branch worktree; never the real tree |
| `evolve.sandbox.fs.list/read/write` | **File explorer** — browse/read/edit the worktree (path-jailed); writes land on the branch and reach main only via promote |

The Sandbox tab surfaces all of it: a **Changes on the branch** card (file list
+ coloured unified diff), a **VS Code on the branch** card embedding the
sidecar, a **Terminal** card (container or worktree shell), and a **Files**
card (worktree browser + editor with save-to-branch) — full access to the
sandboxed copy, zero access to the real source.

**Reviewer trace fidelity.** The engines' `loops.run` result often carries no
usable steps list, which once left the adversarial reviewer judging
"(no tool calls)" after a 25-step run. `_run_task` now rebuilds the tool trace
from the run's persisted agent-loop event log
(`vera:loop:events:evolve:<run_id>`, falling back to the dev Redis DB for
sandboxed runs) — every `tool_call`/`tool_done` with thought, ok and preview —
so reviewers judge the whole run, and timed-out runs keep their partial trace.

## 8. Suite & automation

`evolve.suite.run` executes every enabled task sequentially (loop goals + cap
smoke tests), optionally with critic assessment, and stores a **scoreboard**.
`evolve.report` renders the latest scoreboard + trend + regressions vs the
previous suite as markdown.

The **`loop_eval_nightly`** dream trigger (see [Dream](./17-dream.md)) runs the
suite during idle hours (2–6am) as a collector and delivers the QA report — the
automated regression harness for the loops and, via cap tasks, other systems.

| Cap | Purpose |
|---|---|
| `evolve.suite.run` | Run the whole suite → scoreboard |
| `evolve.suites` / `evolve.report` | Scoreboards / markdown QA report |
| `evolve.runs` / `evolve.run.get` | Run history / full trace |

---

## 9. Markets self-improving loop

`vera/markets/markets_evolve_capabilities.py` applies the same idea to the
markets system — a perpetual loop that improves on two fronts by orchestrating
existing markets + Loop Lab caps:

1. **Strategies & backtests** — for each target (a saved strategy + a dataset)
   it derives a parameter grid from the strategy's own spec, runs it through the
   native backtest **sweep** engine (`markets.backtest.sweep`), takes the best by
   the chosen metric, and — when the best beats both the incumbent and the
   acceptance floor — writes the improved params back (`markets.strategy.save`)
   and puts the strategy live (`markets.strategy.accept`). Underperformers are
   archived. Each iteration re-centres the grid on the current best and widens
   the search when a target stalls — a self-correcting hill-climb.
2. **Its own agent loop** — every N ticks it kicks a Loop Lab improve session
   (`evolve.improve.start`, tag `markets`) so the agentic loop Vera uses to
   reason about markets keeps improving. Markets benchmark tasks are seeded into
   Loop Lab on startup.

| Cap | Purpose |
|---|---|
| `markets.evolve.tick` | One improvement iteration (sweep → accept/archive → maybe improve loop) |
| `markets.evolve.start` / `stop` | The perpetual background loop (every `interval_minutes`) |
| `markets.evolve.status` | Config, live flag, leaderboard, recent ticks |
| `markets.evolve.history` | Past iterations |
| `markets.evolve.config.set` | Metric, floors, grid, targets, cadence |

Turn it on with `markets.evolve.start` after saving + accepting at least one
strategy to a dataset (so it has a monitor target), or set explicit `targets`.

## 10. Where it lives

Since it's more coding than DAGs, Loop Lab opens as a **top-level view in the
IDE** (the 🧪 Loop Lab button in the IDE titlebar swaps the whole editor area for
the full Loop Lab panel), and is also its own top-level **🧪 tab** and a toggle
in the **DAG Workshop → Loop Eval / Sim** section. The markets self-improve loop has
a control card in the **Markets** panel (Monitors column). The panel includes
inline-SVG visuals (suite score trend + per-task bars), an **Activity** tab for
the audit log, and a **Sandbox** tab with the `sandbox_mode` control.

## See also

- [Agentic Loops / DAG Workshop](./03-dag-engine.md) — `loops.run`, the profiles the harness tunes; Loop Lab is embedded in its Loop Eval tab
- [Dream](./17-dream.md) — `loop_eval_nightly`, `markets_evolve_nightly`, `observe_selfheal` triggers; dream source review feeds `evolve.pipeline.from_review`
- [Markets](./15-markets.md) — the backtest/sweep engine the markets loop drives
- [Business Simulation](./business-sim) — `business.sim.*`, the ground-truth scorer for `sim`-type tasks
- [Remote IDE / Claude Code driver](./remote-ide) — where queued code edits execute
- Providers registry — Workers & Ollama → API (critic/editor API keys)
