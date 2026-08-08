---
name: improve-vera-sandboxed
description: Build, fix, or improve Vera's own RUNTIME source by working entirely inside a Loop Lab sandbox/worktree — cut a branch, edit in the worktree, build/extend Loop Lab + pytest tests for the area touched, review your own diff adversarially, gate it through evolve.pipeline.run, and stop at "awaiting manual promote." Never edit prod's live checkout for runtime code and never call evolve.pipeline.promote yourself — merging to main is always the user's explicit call, surfaced to them, not assumed. Use this instead of editing prod directly whenever the work is a source change to Vera itself, not a live-prod diagnosis session.
---

# Improving Vera — sandboxed, adversarially-reviewed, gated

`improve-vera` documents how to diagnose and fix a live issue by editing
prod's own checkout directly over SMB and restarting the real process.
That was the right (and only) tool for a while, but prod is being phased
out as an EDIT TARGET — it's fine to keep diagnosing symptoms against
the real running instance (its Redis event traces, its live UI, its
actual behavior are still ground truth — see `improve-vera` §1–§3a,
still valid), but the FIX itself should now land through this sandboxed
pipeline instead of a direct edit to prod's working tree. Reach for
`improve-vera`'s direct-SMB-edit-to-prod path only for something small,
urgent, and explicitly sanctioned in the moment (infra-level fixes to
shared capabilities, the kind of thing that can't wait for a branch) —
treat that as the exception, not the default.

## 0. Still true, unchanged from improve-vera

- **Never run two Ollama-calling tests at once** (§0) — a sandbox test is
  still an Ollama consumer; the same `vera:loop:sessions`/`vera:loop:run:<sid>`
  check applies before launching one, sandboxed or not.
- **The standalone-verification trap** (§2), **root-cause discipline**
  (§3), **"no error" isn't proof a code path ran** (§3a), **background
  monitoring not tight polling** (§5), **the Perf/Observe UI** (§6), and
  **when to keep watching vs. stop** (§7) all still apply exactly as
  written — none of that changes just because the edit now lands in a
  worktree instead of prod's checkout.

## 1. Get a sandbox + branch, then edit ONLY inside the worktree

```
POST /evolve/sandbox/ensure   {"branch": "loop-lab/<short-descriptive-name>"}
```
Creates the branch (if new) + a git worktree at
`<repo>/.loop-lab-worktrees/<branch>/` + a running `vera-dev` container
bind-mounting that worktree read-write, and snapshots current Loop Lab
state into it. If a sandbox is already up on a DIFFERENT branch and you
need this one, `evolve.sandbox.up(branch=..., rebuild_image=...)`
explicitly moves/recreates it. Poll `evolve.sandbox.status` to confirm
`reachable`.

**Every edit for this unit of work happens inside that worktree path**
(`\\llm.int\boejaker\Vera\.loop-lab-worktrees\<branch>\...` over SMB), never
in the main checkout. The container's bind mount means a saved Python
edit is visible to `vera-dev` immediately — no rebuild needed for source
changes (only `rebuild_image=True` if dependencies themselves changed).

## 2. Two roles, in order, on the SAME change — don't skip the second

1. **Coder.** Make the fix/feature in the worktree. Small, focused
   commits (`git -C <worktree> commit`, or `evolve.sandbox.exec`) — one
   logical change per commit, real messages, not "wip".
2. **Adversarial reviewer — of your own diff, before anyone else sees it.**
   This is the step that's easy to skip because you just finished
   writing the thing and it feels done. Explicitly switch mindset and
   re-read `git -C <worktree> diff` as a skeptical reviewer would:
   regressions in adjacent code, incomplete/half-finished branches of
   the change, edge cases the happy-path test won't catch, anything that
   looks like a security or prompt-injection surface, error handling
   that silently swallows the exact failure mode §3a warns about. If a
   `code-reviewer`-style agent/skill is available, actually invoke it
   against the diff rather than only self-reviewing — a second, less
   invested pass catches things the first one won't. Fix what it finds
   BEFORE moving to gating, not after.

## 3. Tests: build them if they don't exist, don't just assume coverage

For whatever area the change touches:
- **Loop Lab task** — check `GET /evolve/tasks` for one covering it. If
  none exists, add one (`evolve.task.upsert`): a real `goal`, tight
  `allowed_caps`, and `checks` that would actually fail if the bug
  reappeared (not just `final_nonempty`) — see the `web-brief` fix in
  `documentation/36-agentic-loop-v7-evaluation.md` §17 for an example of
  a check that was too strict AND one that was too loose; aim for
  neither.
- **pytest unit test** — check `tests/` for coverage of the touched
  module/function. If none exists, add one (`make test-unit` /
  `python -m pytest tests -q` runs the suite; run it before and after
  your change so you know your test would actually have caught the bug).
- Run both **against the sandbox**, not prod — `evolve.task.run` and the
  suite naturally target the sandbox once one is up (`_resolve_sandbox`
  prefers it automatically).

## 4. Gate it — never promote it yourself

```
POST /evolve/pipeline/run   {"kind":"code","profile":"<profile>",
                             "edits":[{"area":"...","suggestion":"..."}],
                             "auto_promote": false}
```
`auto_promote: false` is not optional — this is what keeps the change
from ever touching main without a human decision. Poll
`evolve.pipeline.get` until `decision` is `pending` (gate passed,
awaiting promote) or `held` (gate failed — fix and re-run, don't force
it through). **Never call `evolve.pipeline.promote` in this skill's
flow** — that capability merges the branch into main
(`evolve_pipeline_promote`, `evolve_capabilities.py:4185`) and exists
specifically so that decision stays a manual, separate act.

## 5. End the work unit by telling the user, not by merging

When a pipeline reaches `pending` (or `held`, if it needs their input on
whether to keep iterating or drop it), say so directly in chat: branch
name, pipeline id, what the gate/tests showed, and that
`evolve.pipeline.promote` is theirs to call whenever they're ready — it
will also show up passively in Dispatch → Branches (built this session:
a branch ahead of main with no merge yet) and in Loop Lab's pipeline
list, but don't rely on either as the ONLY notification; say it. There
is currently no active push-notification when a pipeline reaches
`pending` (checked live — `_audit`/`emit_event` fire, but nothing pages
the user) — that's a known gap in the supporting systems, not something
this skill papers over. If closing that gap is in scope for the current
work, it's a reasonable next increment; if not, flag it rather than
silently assuming someone's watching.

## 6. "Restart" means the sandbox now, not prod

`sys.dev.restart` re-execs the REAL prod process — irrelevant to this
flow, since prod's checkout was never touched. To pick up a worktree
change in a running sandbox container, re-run `evolve.sandbox.up` on the
same branch (no rebuild needed for pure source edits, since the bind
mount is already live — a container bounce is only needed if the
running process itself needs to reload, e.g. a module-import-time
constant). Reserve an actual prod restart for the rare direct-to-prod
exception in the intro above, and even then check §0's concurrency rule
and warn the user first — it kills every in-flight loop/chat/session on
the box, exactly as `improve-vera` §4 already says.
