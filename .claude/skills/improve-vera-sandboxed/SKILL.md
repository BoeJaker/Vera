---
name: improve-vera-sandboxed
description: Build, fix, or improve Vera's own RUNTIME source by working inside a Loop Lab sandbox — cut a typed branch off `bleeding-edge`, edit in its worktree, commit via the HOST (git-over-SMB fails), test, then land through the CI/CD pipeline (adopt → review_request → promote) into `bleeding-edge` — the shared integration branch every change (code AND docs) funnels through before `main`. Diagnose against the live prod instance (its Redis traces, UI, behaviour are ground truth) but land the FIX here, never by editing prod's live checkout. Use this whenever the work is a source or docs change to Vera itself.
---

# Improving Vera — sandboxed, adversarially-reviewed, gated, attributed

Diagnose against the real running instance (Redis event traces, live UI, actual
behaviour — see `improve-vera` §1–§3a, still valid), but land the FIX through
this sandboxed pipeline, not a direct edit to prod's working tree. Direct-to-prod
editing is the rare exception (small, urgent, explicitly sanctioned infra fix).

## 0. Still true, unchanged
- **⛔ NEVER assume how long an LLM/generation takes — it is UNBOUNDED.** A call
  can run seconds or tens of minutes (model size, cold-load, context, queue depth).
  Do NOT estimate a duration, set a mental deadline, or declare a call "hung/broken"
  from **elapsed time alone** — that is the mistake I make most. To tell PROGRESSING
  from STUCK, **monitor REAL ACTIVITY, never the clock:** `ollama.gate.status` (who
  holds the slot — VERIFIED useful), the ollama node's own server log (is it decoding),
  streaming tokens from the cap (`code.author`/`prose.author` take a `stream_cb`), and
  the loop's live state (`/workshop/agent_loop/sessions?status=running` works; the
  per-session event/journal endpoints exist in source but confirm one actually returns
  data before relying on it). Only when real activity has genuinely flatlined — no
  tokens, no log motion, no status change — is it stuck. Otherwise let it run and watch.
- **⛔ THE GPU GATE IS CAPACITY 1 — never fire GPU-routed model calls concurrently.**
  Only ONE GPU generation runs at a time (`ollama.gate.status` → `gpu_cap: 1`, VERIFIED);
  the rest **QUEUE**, so a call that seems slow is very often waiting behind your own
  previous one — check the gate owner before blaming the cap.
  **Which roles run on the GPU is LIVE-CONFIGURABLE (Model Routing page overrides the
  source `deny_gpu` defaults) — VERIFY, don't assert from the code.** Confirmed facts
  (user): **planner and controllers run on the GPU; embeds run on CPU** (cpu-246/247).
  Do NOT extrapolate to "all roles". Safe takeaway: a running loop IS a GPU consumer
  (its planner/controllers are GPU), so never add a concurrent GPU call while a loop runs.
  **Discipline:** (1) before any GPU call, if one is in flight (`held ≥ 1` with an owner),
  WAIT. (2) Fire ONE, watch its real activity until it COMPLETES, THEN the next. (3)
  Never two loops at once — `/workshop/agent_loop/sessions?status=running` must be empty
  first; stop a loop via **`/workshop/agent_loop/cancel` (`{session_id}`)** — a
  sandbox/container restart is NOT a reliable stop (observed: after a restart the session
  still showed `running` and `cancel` found no live task in the new process). (4) Any
  timing read from a window where >1 inference ran is contention-confounded — discard it.
  (See the standing memory note `no-concurrent-loop-tests`.)
- **Multi-agent estate is live.** Other agents may be working in their own
  containers (`evolve.sandbox.list` shows them), all sharing one GPU. Never touch
  another agent's branch/container. Before a **prod restart**, check
  `ollama.gate` (is someone mid-generation?) and warn — a restart interrupts
  every agent's prod-side cap calls.
- **Sandbox pool is finite (Redis DBs 3–15, 13 slots) and fills up fast** — the
  swarm routinely runs 10+ concurrent branches. `evolve.sandbox.up(branch=…)`
  can fail with `no free Redis DB in the pool`. Check `evolve.sandbox.list`
  first; `evolve.sandbox.prune(dry_run=true)` shows what's reapable (usually
  nothing — other agents' unmerged WIP is protected on purpose). If the
  **primary** is idle (its branch's own pipeline already promoted/merged), it's
  fine to reuse it for your own unrelated work — `evolve.sandbox.up` on a new
  branch just switches it; this is the normal way most work in this skill
  actually gets a sandbox, not `spawn=true`, which needs its own free slot.
- **Prod is flaky mid-restart** — another agent restarting it (or you, later in
  the same session) causes transient `EOF`/`SSL` errors on any call, including
  plain `GET /health`, for up to ~30s. Retry with a short poll loop before
  concluding it's actually down.
- **A standing bleeding-edge container may exist** — `evolve.bleeding_edge.
  container.ensure` brings up (or confirms) a PERSISTENT, pinned sandbox
  permanently tracking bleeding-edge's tip (auto-refreshed after every promote
  into it), distinct from the ephemeral per-branch one `pipeline.begin` gives
  you. Useful if you want a stable place to poke at "what's actually on
  bleeding-edge right now" without spinning up your own. It's still a sandbox
  container though — no docker socket (§6), so it doesn't help with
  `evolve.unittest.run`.
- **Never edit the main checkout directly, not even a "trivial" docs/skill
  file.** It's tempting — no gate applies, no container to spin up — but it
  leaves `main` dirty (blocks the next promote) and skips `bleeding-edge`
  entirely (§2). If you catch yourself having done it, `git status` on main
  will show it dirty; revert with `git checkout -- <path>` (verify no stale
  `.git/index.lock` from an earlier interrupted command first — check for a
  live git process before removing one) and redo the edit inside a proper
  worktree instead.
- Root-cause discipline, the standalone-verification trap, "no error ≠ it ran",
  background-monitoring-not-polling — all still apply.

## 1. Reaching capabilities — the `/mcp/call` escape hatch (READ THIS FIRST)
Your MCP tool list is fixed at session start. A cap deployed mid-session (or a
changed schema, e.g. `promote`'s `force`) will NOT appear as an `mcp__vera__*`
tool. Reach ANY cap — and attribute it to you — via the same HTTP entrypoint the
MCP bridge uses:
```
POST https://llm.int:8999/mcp/call
{ "name":"<cap.name>", "arguments":{...},
  "caller_kind":"claude", "session_id":"<your claude session uuid>" }
```
`caller_kind:"claude"` stamps the pipeline/commit as `controller=claude_code`; the
`session_id` links it to your chat (drill-down in the CI/CD UI). A bare curl or
the browser tags `user` — use `/mcp/call` so your work is attributed.

When registering the stdio bridge, pass `--caller-kind claude`. Codex uses
`--caller-kind codex`; never reuse another agent's identity merely to obtain an
attribution badge.

**If a POST from Windows (PowerShell `Invoke-RestMethod`) fails with `EOF`/`SSL`
errors while `GET` works fine**, don't assume the cap is broken — it's sometimes
a client-side TLS quirk specific to POST from that host at that moment. Retry
once; if it persists, route the same call through the Loop Lab HOST instead
(which never has this problem): `evolve.sandbox.exec(where="worktree",
cmd="curl -sk -X POST https://localhost:8998/mcp/call -H 'Content-Type:
application/json' -d '{...}'")` against your own sandbox's port, or
`https://localhost:8999/mcp/call` from the host reaches prod directly.

## 2. Land to `bleeding-edge`, not `main` — every change, code AND docs
`bleeding-edge` is the **one shared integration branch** every change funnels
through before `main`/prod. The invariant to protect: **`bleeding-edge` is
always a superset of `main`** — `main` only ever advances by promoting
`bleeding-edge` as a whole, never by merging an individual feature/docs branch
into `main` directly. Breaking this (landing straight to `main`) lets `main`
drift ahead of `bleeding-edge` with content the integration branch never saw,
which defeats the point of having one.

**This applies to docs too**, not just code — a `documentation/*.md` change
looks zero-risk (no import-time impact, no gate to fail), which is exactly why
it's tempting to shortcut straight to `main`. Don't. Route it through
`bleeding-edge` like everything else, even though `evolve.pipeline.adopt`'s
gate will report `gate_passed: null` for it (nothing to compile-check) and
`promote` needs `force=true` (no code gate to satisfy).

**This is now automatic, not a manual convention.** As of 2026-08-16
(`_default_pipeline_base` in evolve_capabilities.py — landed the same day as
this skill update, by a different concurrent session; genuinely the swarm
converging on the same conclusion independently), both `evolve.pipeline.begin`
(§3) and `evolve.pipeline.promote` (§7) default to `bleeding-edge` — branching
off it and merging into it — whenever a `bleeding-edge` branch exists for the
repo, falling back to the real mainline only if it doesn't. You don't need to
branch off it by hand any more; the plumbing above (`git branch feat/<name>
origin/bleeding-edge`) is the fallback for when you want explicit control, not
the normal path. Still pass `to="bleeding-edge"` explicitly on `adopt`/`promote`
anyway (§7) — `adopt`'s own default wasn't updated to match, so relying on the
default there specifically still lands you on `main`.

**Before landing, check the branches haven't diverged** (another agent may
have promoted something to `bleeding-edge` since you branched):
```
git -C \\llm.int\boejaker\Vera fetch origin bleeding-edge main
git -C \\llm.int\boejaker\Vera log --oneline origin/bleeding-edge..origin/main   # should be EMPTY
```
If that's non-empty, `main` has drifted ahead — reconcile (merge the extra
`main`-only commits' ancestry back into `bleeding-edge`, e.g. by branching your
next piece of work off `main` instead of `bleeding-edge` once, which pulls it
back in) before continuing.

**Promoting `bleeding-edge` → `main` is a separate, deliberate step**, not
something that happens as a side effect of landing a feature. Only do it when:
the user asks explicitly, or you've been given standing authorization for this
session. Ask first otherwise — it triggers a prod restart (§8) and ships
*everything* currently staged on `bleeding-edge`, including other agents'
already-merged work, not just yours.

## 3. Start atomically — `evolve.pipeline.begin` (ONE call), then edit the worktree
```
evolve.pipeline.begin(title="<what you're doing>", spawn=true, session_id="<yours>")
```
This does everything at once: creates a **typed** branch (`feat/<slug>`) off
`bleeding-edge` (§2 — automatic since 2026-08-16, falls back to the real
mainline only if `bleeding-edge` doesn't exist for this repo), materialises
its worktree, brings up your **own** dev container (`spawn=true`, so you don't
disturb other agents' primary sandbox — now with `VERA_DEV_MODE=1`), and
records the CI/CD pipeline **with its worktree** (so diff/test work). It
returns `{id, branch, worktree, url, next[]}` — the exact next caps. No more
guessing the setup steps.

Lower-level alternative (if you need control, or the pool is full — see §0):
`evolve.sandbox.up(branch=…)` (your primary sandbox, reused/switched) or
`evolve.sandbox.spawn(branch=…)` (own container, needs a free pool slot); create
the branch first with `git branch feat/<name> origin/bleeding-edge` (§2).
Poll `evolve.sandbox.status` for `reachable`.

**Edit only inside the returned worktree.** Never the main checkout.

**Edit only inside that worktree** (`…\.loop-lab-worktrees\<branch>\…` over SMB,
or `evolve.sandbox.fs.write`). Never the main checkout. The bind mount makes a
saved edit visible to the container immediately.

## 4. Commit via the HOST — git-over-SMB does NOT work
A worktree's `.git` points at a Linux host path (`…/.git/worktrees/…`) that
Windows/SMB can't resolve, so running `git` in the worktree from your machine
fails. Commit through the host instead:
```
evolve.sandbox.exec(where="worktree", branch="feat/<name>",
                    cmd="git -c user.name=BoeJaker add -A && git commit -m '…'")
```
`where="worktree"` runs on the HOST (git works natively); `branch=`/`name=`
routes to YOUR worktree/container (not the primary — that trap is fixed). Small,
focused commits, real messages, author = the human (**never** an AI-attribution
trailer; that's git-attribution policy). Container syntax checks:
`evolve.sandbox.exec(where="container", branch="feat/<name>", cmd="python3 -c
'import ast; ast.parse(open(\"vera/…\").read())'")`.

**Docs changes:** `content.edit`/`content.status` exist as a purpose-built,
lighter-weight path for `documentation/`/`.claude/skills/` (out-of-tree
worktree, hook-gated, no dev container needed) — try it first. **Known-broken
as of 2026-08-16 (route-forward.md T7):** its worktree's git link can be
severed (`content.status` reports it healthy; `content.edit` fails with `fatal:
not a git repository`). If it fails, don't try to repair a shared worktree's
git internals blind — fall back to a normal small branch + worktree + host
commit, same as code, then land via §7 like any other change.

## 5. Two roles on the SAME change — don't skip the reviewer
1. **Coder** — make the fix in the worktree.
2. **Adversarial reviewer of your OWN diff, before landing.** Re-read `git diff`
   as a skeptic: regressions in adjacent code, half-finished branches, edge cases
   the happy path misses, security/prompt-injection surface, error handling that
   swallows the failure. If a `code-reviewer` agent is available, invoke it. Fix
   what it finds BEFORE gating.

## 6. Tests — build them, and run them where they RESOLVE to YOUR worktree
- **pytest / pure-module unit test** under `tests/` for the touched logic. Pure
  helpers (no I/O) are easiest — extract them if needed (e.g. `board_core.py`,
  `remote_exec_core.py`, `sandbox_reap.py`, `ollama_gate.py`, `evolve_git_core.py`).
  `tests/conftest.py` already defines a **`critical` pytest marker** over a
  handful of modules — "pure, deterministic tests guarding systems where a
  regression is expensive and was actually hit." If your fix touches one of
  those systems (or is exactly this kind of regression-prone logic), add your
  test there and mark it `critical` — it becomes part of the merge gate itself
  (see §7), not just a test that exists.
- **`evolve.unittest.run(branch=, paths="tests", markers="critical"|"", timeout=)`**
  — runs pytest for a branch in a FRESH, ISOLATED ephemeral `vera:latest`
  container (`docker run --rm`), **never** the container serving the request, so
  it can't hang the app it's testing. This is the primitive `evolve.pipeline.adopt`
  itself now uses for the gate (§7) — call it directly too, any time, to check a
  branch before adopting. **Needs a docker socket — only prod's native process
  has one.** No sandbox container does (confirmed: not even the branch's own
  dev container). If you need to verify pytest results and you're working from
  a sandbox, either call this via `/mcp/call` against prod (§1) targeting your
  branch (it resolves the worktree from the shared sandbox pool, works from
  anywhere), or accept that full verification waits until the change is live.
- **⚠ Import-path trap — this bites HARD and hides (found 2026-08-09).** `Vera`
  is a **namespace package** (no `__init__.py`), so `from Vera.vera.X import …`
  resolves to whatever is first on `sys.path` — on the HOST that is the **main
  checkout, NOT your worktree**. A host `pytest` that imports `Vera.vera.X`
  therefore silently exercises the OLD code, so your change "does nothing" /
  `AttributeError`s on a symbol you just added. This is the real cause that was
  once **misread as an OOM** (dev-lifecycle §8.3 #9 — retracted: mem-limit 0,
  `oom_killed=false`, in-container pytest passes). Two correct ways to test
  worktree code:
  - **lowercase import, worktree on the path** — `sys.path.insert(0, <worktree
    root>)` then `from vera.X import …` (what `tests/test_board_core.py` /
    `test_remote_exec_seam.py` do). This is *why* the pure-core extraction pattern
    earns its keep: the logic is reachable as `vera.X` without the app.
  - **in the container**, where the worktree IS `/app/Vera` so `Vera.vera.X`
    resolves right: `evolve.sandbox.exec(where="container", branch="feat/<name>",
    cmd="pip install -q pytest && cd /app/Vera && python -m pytest tests/<one_file> -q")`.
    In-container pytest does **not** OOM — pytest just isn't baked into the image
    yet, so install it first. **Caveat (§8.3 #9b):** a *targeted* test is fine, but
    running the **full app-importing suite in the container that is SERVING the app**
    contends with its event loop and can make its HTTP go unresponsive — for a full
    suite use a separate ephemeral `vera:latest` exec, not the serving container
    (this is exactly what `evolve.unittest.run` above already does for you).
  - Host venv (has pytest): `/home/boejaker/langchain/bin/python3 -m pytest
    tests/… -q` — fast, but only correct via the lowercase `vera.X` + sys.path
    insert above; `Vera.vera.X` there still hits main.
- **Loop Lab task** (`evolve.task.upsert`) for behavioural/loop changes, with
  `checks` that would actually fail if the bug returned.
- Verify the module **boots** — `evolve.sandbox.up` on the branch; a reachable
  probe (tool_count went up for a new cap) means every `_module_files` import,
  including yours, loaded (import-time errors py_compile misses).

## 7. Land it through the CI/CD pipeline (adopt → review → promote **into `bleeding-edge`**)
This is the current flow — it tracks + attributes your hand-authored change and
uses the SAFE merge (never a blind `git checkout`):
```
evolve.pipeline.adopt(branch="feat/<name>", to="bleeding-edge", title, summary, session_id)  # via /mcp/call
   → gate_passed: true | false | null
evolve.pipeline.review_request(id, reason)
evolve.pipeline.promote(id, to="bleeding-edge"[, force=true])   # force for docs/UI/infra
```
`to="bleeding-edge"` per §2 — `evolve.pipeline.promote`'s own default is
`bleeding-edge` too now (as of 2026-08-16), but `adopt`'s default is still
`main`, so pass it explicitly on `adopt` regardless. If you started with
`evolve.pipeline.begin` (§3) you already have the pipeline `id` — skip `adopt`
and go straight to `review_request` + `promote(to="bleeding-edge")` (promote
refreshes the branch's commits before merging; if you omit `to`, it defaults
there anyway, but pass it — explicit beats relying on a default that could
change again).

**`gate_passed` is TWO checks, not one** (as of the 2026-08-16 M3 work — the
description on the cap itself is the source of truth if this drifts):
1. **Compile gate** — `ast.parse` on the branch's changed `.py` files. Always
   runs when `.py` changed; fast, dependency-free.
2. **Critical-tier gate** — `pytest -m critical` (§6) via `evolve.unittest.run`,
   in an isolated ephemeral container. Runs when `.py` changed AND the branch
   has a live worktree (bring one up first — `evolve.sandbox.up`/`begin`
   already gives you one). Skipped (falls back to the compile gate alone,
   same as before) if there's no live worktree yet.

`gate_passed` is `true` only when both applicable checks pass; `null` when
nothing was checked (no `.py` changed — docs/UI/infra, use `force=true` on
promote). `adopt` records the pipeline (shows in the CI/CD UI with your session
+ drill-down). Promote does a guarded `--no-ff` merge and returns
`restart_required` — **for `bleeding-edge` this is generally not actionable**
(nothing serves off it) unless prod happens to be the "live checkout" being
merged into, which it isn't for `bleeding-edge`. **Refuses a dirty target
tree** — that's the guard protecting WIP, not a failure.

**Promoting `bleeding-edge` itself into `main`** is the separate step from §2 —
use the dedicated `evolve.bleeding_edge.promote_to_main(repo="vera")` (not a
manual `adopt`/`promote` with `branch="bleeding-edge"`, though that shape
still works too). Same safe-merge machinery underneath, one call, no `id` to
track — this is a deliberate release action, never called automatically by
any gate/scheduler/other capability. Only do this with explicit authorization
(§2); it's the one that actually reaches prod (§8).

## 8. Deploy = push + restart (only `main` reaching prod, and only for `.py`)
- **Push:** creds live on the Windows host — push from there. Push
  `bleeding-edge` after every merge into it (`git -C \\llm.int\boejaker\Vera
  -c credential.interactive=false push origin bleeding-edge`) so it's durably
  available even before a `main` promotion. Push `main` the same way after a
  `bleeding-edge`→`main` promotion.
- **Restart** (`.py`/import-time changes reaching **prod's `main` checkout**
  only): the restart tool re-execs prod. A `bleeding-edge` merge never needs
  this — prod doesn't run it. **UI-only changes (panel HTML, `/ui/elements/*.js`)
  are served fresh — NO restart even on `main`.** Before restarting, check the
  multi-agent rule in §0, and re-check no loop is running on prod right before
  (`GET /workshop/agent_loop/sessions?status=running` should be empty).
- Verify LIVE on prod (127.0.0.1 / llm.int:8999) after a `main` promotion+restart
  — not just in the sandbox, and not just by trusting the merge succeeded.

## 9. Close the unit — tell the user
Say it in chat: branch, pipeline id, which branch it landed on
(`bleeding-edge`, almost always — not `main`), what the gate/tests showed, what
landed. Promoting your own vetted change into `bleeding-edge` is normal and
doesn't need to wait for permission. Promoting `bleeding-edge` → `main` (§2, §8)
is a separate, higher-stakes step — always confirm first unless already
authorized for the session. It all shows in the Loop Lab CI/CD + Review tabs.

## 10. Keep the plan and the board current — the board is the primary planning surface
Work isn't done when the code lands; it's done when the **shared planning state
reflects reality**. Two standing obligations, every unit:

- **Update the plan.** When a milestone/tech-debt item lands, changes status, or
  a new issue is found, reflect it in the route-forward plan
  (`documentation/specs/consolidated-route-forward.md`) and keep the source plans'
  glyphs in sync (§2 — docs route through `bleeding-edge` like everything else).
  A plan that lags the code is worse than no plan: other agents act on it. Mark
  items ✓/◐/○ with the landing commit; record the *reason* for tech debt, not just
  that it exists.
- **Use the board as the primary planning + inter-agent communication mechanism.**
  The board (`board.*`, out-of-tree file tier) — not chat, not a local note — is
  the durable, shared work surface every agent reads and writes: capture work as
  items, `board.claim` before starting (lease, lowest-comment-id wins), post
  `progress`/`blocked`/`help-request` envelopes as you go, link the item to its
  `pipeline`/`branch`, and move its lane. Precedence when sources disagree:
  **files → board → fabric** (the fabric index is derived, never authoritative).
  Re-read the board before resuming — the world moved while you were away; never
  re-run work an item already shows finished (reconcile, don't repeat).

**This is being automated, not left to discipline.** `board.sync` reflects a
linked pipeline's state (decision/gate/review → lane + a comment) onto its item,
and now runs on a **scheduled poll** (`board.sync.poll`, every
`VERA_BOARD_SYNC_INTERVAL_S`, toggle `VERA_BOARD_SYNC_ENABLED`), so the board
tracks pipeline movement without a human call — idempotent via each item's
`sync_sig`. Plan-doc freshness is not yet automated: that stays a manual step
here (a doc-staleness check on merge is the tracked follow-on — route-forward
Phase E / M2).
