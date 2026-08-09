---
name: improve-vera-sandboxed
description: Build, fix, or improve Vera's own RUNTIME source by working inside a Loop Lab sandbox — cut a typed branch, edit in its worktree, commit via the HOST (git-over-SMB fails), test, then land through the CI/CD pipeline (adopt → review_request → promote) with the change tracked + attributed to your session. Diagnose against the live prod instance (its Redis traces, UI, behaviour are ground truth) but land the FIX here, never by editing prod's live checkout. Use this whenever the work is a source change to Vera itself.
---

# Improving Vera — sandboxed, adversarially-reviewed, gated, attributed

Diagnose against the real running instance (Redis event traces, live UI, actual
behaviour — see `improve-vera` §1–§3a, still valid), but land the FIX through
this sandboxed pipeline, not a direct edit to prod's working tree. Direct-to-prod
editing is the rare exception (small, urgent, explicitly sanctioned infra fix).

## 0. Still true, unchanged
- **Never run two Ollama-calling tests at once.** A sandbox loop is an Ollama
  consumer. Check `vera:loop:sessions` first. The shared GPU is now gated
  (`ollama.gate`) so calls queue rather than collide, but don't pile on.
- **Multi-agent estate is live.** Other agents may be working in their own
  containers (`evolve.sandbox.list` shows them), all sharing one GPU. Never touch
  another agent's branch/container. Before a **prod restart**, check
  `ollama.gate` (is someone mid-generation?) and warn — a restart interrupts
  every agent's prod-side cap calls.
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
  "caller_kind":"mcp", "session_id":"<your claude session uuid>" }
```
`caller_kind:"mcp"` stamps the pipeline/commit as `controller=claude_code`; the
`session_id` links it to your chat (drill-down in the CI/CD UI). A bare curl or
the browser tags `user` — use `/mcp/call` so your work is attributed.

## 2. Get a sandbox on a TYPED branch, edit ONLY in its worktree
```
evolve.sandbox.up(branch="feat/<short-name>")     # your own primary sandbox
# or, to run ALONGSIDE other agents on your own container+port:
evolve.sandbox.spawn(branch="feat/<short-name>")
```
Use a **typed** branch (`feat/…`, `fix/…`, `chore/…`) off `main` — the branch
may need creating first: `evolve.sandbox.exec(where="worktree", cmd="git branch
feat/<name> main")`. `up`/`spawn` create the worktree at
`<repo>/.loop-lab-worktrees/<safe-branch>/` + a `vera-dev*` container (now with
`VERA_DEV_MODE=1`, so you can restart your own sandbox). Poll
`evolve.sandbox.status` for `reachable`.

**Edit only inside that worktree** (`…\.loop-lab-worktrees\<branch>\…` over SMB,
or `evolve.sandbox.fs.write`). Never the main checkout. The bind mount makes a
saved edit visible to the container immediately.

## 3. Commit via the HOST — git-over-SMB does NOT work
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

## 4. Two roles on the SAME change — don't skip the reviewer
1. **Coder** — make the fix in the worktree.
2. **Adversarial reviewer of your OWN diff, before landing.** Re-read `git diff`
   as a skeptic: regressions in adjacent code, half-finished branches, edge cases
   the happy path misses, security/prompt-injection surface, error handling that
   swallows the failure. If a `code-reviewer` agent is available, invoke it. Fix
   what it finds BEFORE gating.

## 5. Tests — build them if missing
- **pytest / pure-module unit test** under `tests/` for the touched logic. Pure
  helpers (no I/O) are easiest — extract them if needed (e.g. `sandbox_reap.py`,
  `ollama_gate.py`). Run against the sandbox. If pytest isn't on the host, a
  stdlib driver works for pure `assert` tests.
- **Loop Lab task** (`evolve.task.upsert`) for behavioural/loop changes, with
  `checks` that would actually fail if the bug returned.
- Verify the module **boots** (import-time errors py_compile misses).

## 6. Land it through the CI/CD pipeline (adopt → review → promote)
This is the current flow — it tracks + attributes your hand-authored change and
uses the SAFE merge (never a blind `git checkout`):
```
evolve.pipeline.adopt(branch="feat/<name>", title, summary, session_id)  # via /mcp/call
   → gate_passed: true  (compile gate on changed .py) | null (docs/UI — promote force)
evolve.pipeline.review_request(id, reason)
evolve.pipeline.promote(id, to="main"[, force=true])   # force for docs/UI/infra
```
`adopt` records the pipeline (it shows in the CI/CD UI with your session +
drill-down). Promote does a guarded `--no-ff` merge into prod's `main` checkout
and returns `restart_required`. **Refuses a dirty prod tree** — that's the guard
protecting WIP, not a failure. *(Aim: an `evolve.pipeline.begin(title)` that
creates branch+worktree+record atomically is on the roadmap — until then do §2–§3
by hand.)*

## 7. Deploy = push + restart (only for `.py`)
- **Push:** creds live on the Windows host — push from there:
  `git -C \\llm.int\boejaker\Vera -c credential.interactive=false push origin main`.
- **Restart** (`.py`/import-time changes only): the restart tool re-execs prod.
  **UI-only changes (panel HTML, `/ui/elements/*.js`) are served fresh — NO
  restart.** Before restarting, check the multi-agent rule in §0.
- Verify LIVE on prod (127.0.0.1 / llm.int:8999), not just in the sandbox.

## 8. Close the unit — tell the user
Say it in chat: branch, pipeline id, what the gate/tests showed, what landed.
Promote is safe to run yourself for your own vetted change, but if the user
should decide, stop at `review_requested`/`pending` and surface it — don't merge
silently. It also shows in the Loop Lab CI/CD + Review tabs.
