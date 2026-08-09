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

## 2. Start atomically — `evolve.pipeline.begin` (ONE call), then edit the worktree
```
evolve.pipeline.begin(title="<what you're doing>", spawn=true, session_id="<yours>")
```
This does everything at once: creates a **typed** branch (`feat/<slug>`) off `main`,
materialises its worktree, brings up your **own** dev container (`spawn=true`, so
you don't disturb other agents' primary sandbox — now with `VERA_DEV_MODE=1`), and
records the CI/CD pipeline **with its worktree** (so diff/test work). It returns
`{id, branch, worktree, url, next[]}` — the exact next caps. No more guessing the
setup steps.

Lower-level alternative (if you need control): `evolve.sandbox.up(branch=…)` (your
primary sandbox) or `evolve.sandbox.spawn(branch=…)` (own container); create the
branch first with `evolve.sandbox.exec(where="worktree", cmd="git branch feat/<name>
main")`. Poll `evolve.sandbox.status` for `reachable`.

**Edit only inside the returned worktree.** Never the main checkout.

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

## 5. Tests — build them, and run them where they RESOLVE to YOUR worktree
- **pytest / pure-module unit test** under `tests/` for the touched logic. Pure
  helpers (no I/O) are easiest — extract them if needed (e.g. `board_core.py`,
  `remote_exec_core.py`, `sandbox_reap.py`, `ollama_gate.py`).
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
    suite use a separate ephemeral `vera:latest` exec, not the serving container.
  - Host venv (has pytest): `/home/boejaker/langchain/bin/python3 -m pytest
    tests/… -q` — fast, but only correct via the lowercase `vera.X` + sys.path
    insert above; `Vera.vera.X` there still hits main.
- **Loop Lab task** (`evolve.task.upsert`) for behavioural/loop changes, with
  `checks` that would actually fail if the bug returned.
- Verify the module **boots** — `evolve.sandbox.up` on the branch; a reachable
  probe (tool_count went up for a new cap) means every `_module_files` import,
  including yours, loaded (import-time errors py_compile misses).

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
protecting WIP, not a failure. If you started with `evolve.pipeline.begin` (§2) you
already have the pipeline `id` — skip `adopt` and go straight to `review_request` +
`promote` (promote refreshes the branch's commits before merging).

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
