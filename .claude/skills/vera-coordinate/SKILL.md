---
name: vera-coordinate
description: How any AI session (Claude Code, Codex, a Vera delegate) works ALONGSIDE other sessions on Vera — where plans, handovers and evidence go (the shared planning area, never docs/Notes/user dirs), how tasks and findings travel through the Loop Lab board (claim, comment, hand off, search before filing), when and how to post status (on records the work already writes, not chat), what to do when Vera is unreachable, how to cut work so one or many agents can carry it out, and how to end a session cleanly. Use at the START of every Vera session, before writing any plan or handover, before filing or picking up board work, when another agent's area is touched, and at the end of a session.
---

# Working alongside other agents on Vera

Several sessions work on Vera at once: Claude Code sessions, Codex, and Vera's own delegates. They rarely know about
each other. This skill makes them cooperate through two shared surfaces: **the shared planning area** (narrative,
scratch, evidence) and **the Loop Lab board** (solid tasks, claims, findings). The rules are short, and breaking
them costs other agents hours.

The conventions file is `.git/vera-work/shared-planning/README.md`; this skill is the procedure. The programme it
comes from is `shared-planning/agent-ops/PLAN.md`. References:

- `references/board-standard.md`: item and label standard, comment kinds, search.
- `references/status-handover.md`: status cadence and shape, the handover template, the outbox.
- `references/complex-projects.md`: planning and executing large or multi-session work, including design landing.

## 0. Identity and reach

- **Name yourself** consistently everywhere: `claude:<session-id>`, `codex:<session-id>`, `vera:<job-id>`. Use the
  same string as `frm` on board comments, `owner` on worktree claims and `agent:` labels.
- **Reach Vera** through the MCP bridge registered with your caller kind (`--caller-kind claude|codex`), or directly
  with `POST https://<vera-host>:8999/mcp/call {"name","arguments","caller_kind","session_id"}` (see
  `improve-vera-sandboxed` §1). If the bridge's allow-list hides `board.*` / `registry.*`, use `/mcp/call`.

## 1. Where things go — one home per kind of thing

| Thing | Home |
|---|---|
| Plans, roadmaps, evidence, handovers, audits, reviews, decisions, design notes | `<git-common-dir>/vera-work/shared-planning/<unit>/` |
| Solid tasks (one outcome, a done-when) | the board: `board.item.upsert` |
| A finding about an existing task or area | a **comment** on that board item |
| Status during work | the record the work already writes (§4) |
| Skills, helpers, techniques | repo `.claude/skills/<skill>/` + a registry entry (`registry.upsert`) |
| Facts about working with the owner | your own memory, as pointers only |

**Never** put plans or handovers in `documentation/` (product docs only), `Notes/`, the repo root, `~/.claude/plans`,
your memory dir, or only in chat. If you find one there, list it in `shared-planning/agent-ops/` (migration) rather
than moving it silently.

**Paths:** `git rev-parse --git-common-dir` from any worktree, then `/vera-work/shared-planning/`. On the host it is
`/home/boejaker/Vera/.git/vera-work/shared-planning/`; over SMB, `//<host>/boejaker/Vera/.git/vera-work/shared-planning/`.
Git over SMB fails inside worktrees; read and write these files directly.

**A work unit** is a kebab-case folder with a `README.md` whose first lines are the standard header:
Status · Updated · Owner · Board label · Base sha, then Read first and Current state (what is NOT done first). Other
files: `PLAN.md`/`ROADMAP.md`, `EVIDENCE-<date>.md`, `HANDOVER-<date>-<agent>.md`, `ISSUES.md`,
`REVIEW-<date>.md`, `DECISIONS.md` (the owner's words, dated), `tools/`. **Append under dated headings; don't
rewrite another agent's section; don't make `.bak` copies.**

## 2. Starting a session

1. Read `shared-planning/README.md`, then the README and latest HANDOVER of the unit you're working in. If there is
   no unit for your work, create one (the header, one paragraph, Read first).
2. `board.inbox(agent=<you>)`: items you hold, mentions of you, open help requests.
3. **Search before you start**, by the files you'll touch and the area: `board.items(text=…)`,
   `board.items(label="files:<glob>")`, `label="unit:<unit>"`, `label="area:<subsystem>"`. Read the comments of
   anything relevant (`board.item.get(id)`). Another agent may hold it, have found the cause, or have decided against it.
4. **Claim before editing**: `board.claim(id=<item>, agent=<you>)`. The lowest claim wins; if you lost, pick other work or
   comment. Claim hand-made worktrees too (`evolve.worktree.claim`).
5. Check the shared constraints the unit README names (census calendar, GPU gate, frozen contracts) before anything
   that uses the GPU or prod.

## 3. Findings for other agents — the board is the message bus

You will often find something outside your task: a bug, a stale doc, a risk in someone else's area. You don't need
to know who owns it.

1. **Search** the board by file, symbol and area.
2. **If an item exists:** `board.comment(id=<item>, frm=<you>, kind="note", body=…)` with the finding, the evidence
   (`path:line`, sha, log or dump path), and a suggested action. Use `kind="steer"` if it should change that item's
   plan, and `kind="help-request"` if you need its holder to act.
3. **If none exists:** create an item to the standard (`references/board-standard.md`), with `kind:bug|…`, `sev:`,
   `files:` and `unit:` labels, so the next search finds it.
4. **If it's a defect list from a recon**, stage it in a unit's `ISSUES.md` in board-item form and file it when the
   board is reachable.

`board.item.upsert` replaces omitted item fields with their defaults; it is not a
partial patch. Before changing only a lane or one metadata field, read the item
with `board.item.get` and resubmit its complete title, labels, routing metadata,
and body. Comments are preserved independently.

Live peer messages (Claude's cross-session messaging) are a convenience. **Anything agreed that way is written to
the board item or the plan**, or it didn't happen.

## 4. Status: a by-product of the work, not a separate act

- **When:** at every phase boundary (recon done, plan written, branch cut, tests run, review in, promoted); at most
  ~25 minutes or ~40 tool calls apart in continuous work; at once on a blocker, a plan-changing surprise, or before
  a destructive or outward-facing step.
- **Shape:** `{phase, did, evidence, next, blockers, confidence}`, ≤ 600 characters.
- **Where, cheapest first:** the field of the call you're already making (`evolve.pipeline.adopt summary`,
  `review findings`, `review.request reason`); a board `progress` / `blocked` comment on your item; or, when nothing
  else is being written, `agent.status.post` (once it exists; until then a `progress` comment).
- **Chat** gets one line: the phase and where the record is. The owner reads the records; don't make them read a
  narrative twice.
- Details and examples: `references/status-handover.md`.

## 5. When Vera is unreachable (degraded mode)

- Board files are plain markdown on the share (`vera-state/board/*.md`). **Read them freely; never write them by
  hand**: claims, comment ids and atomic writes are enforced by `board.*`.
- Queue intended board writes as JSON lines, one per call, in `shared-planning/_outbox/<agent>-<date>.jsonl`:
  `{"cap": "board.comment", "args": {...}, "why": "...", "ts": "..."}`. The next session with Vera replays them
  (checking the item still exists and the claim still holds) and deletes the replayed lines.
- Status goes in your unit's HANDOVER while you're cut off.
- Check reachability with `GET https://<vera-host>:8999/health` before deciding Vera is down: a failed MCP bridge
  does not mean prod is down.

## 6. Cutting work so one or many can do it

Every plan you write is a set of **work packages**, each claimable on its own:

```
<ID>  <imperative title>
unit · ws · size S|M|L · executor any|claude|codex|vera|owner-decision
depends: [...]   conflicts: [files/areas another package touches]
problem (with evidence path) · outcome · done-when · outputs · plan link
```

- Mark `executor: vera` only for bounded, deterministic work with checkable acceptance and no prod effects (Vera
  delegates via `board.dispatch` / `evolve.delegate.start`).
- Mark `owner-decision` for anything the owner must choose.
- `conflicts` is what stops two agents colliding. A claim is the lock.

Large or multi-session work, and UI or design landing: follow `references/complex-projects.md`.

## 7. Switching, stopping, handing over

- **Before switching tasks:** park the current one with a progress comment and a plan note. Have at most two items
  in flight.
- **Handing to a named agent:** `board.handoff(id, frm, to, body)` (only the holder can).
- **Asking for help:** `board.help(id, frm, body)` adds `needs:help`.
- **Ending a session:** write `HANDOVER-<date>-<agent>.md` in the unit, in this order:
  1. **what is NOT done**;
  2. what was done (branches, pipelines, shas, board items);
  3. the state and anything left running;
  4. what's next;
  5. evidence paths.

  Then update the unit README's Current state, and release your claims or say why you keep them. If you were cut
  off by a quota, mark items `blocked:quota` with a resume note.

## 8. Rules shared by every Vera skill

- Land everything (code **and** docs **and** skills) on `bleeding-edge` through the Loop Lab pipeline
  (`improve-vera-sandboxed`). `main` and prod only on the owner's explicit go-ahead.
- Commit through the host; author is the human; **no AI attribution** in commits, trailers or PR text; never push to
  origin unless the owner asks.
- Background inference follows the owner's rules (idle queue, `prefer_gpu: false`, census yield). Never put GPU
  work into a running census window.
- Public repo: no secrets, credentials, private paths beyond the documented share layout, or personal content in
  skills, commits or board text.

## 9. Close every use of this skill with an autopsy

Before finishing: did any rule here fail you, slow you, or prove wrong? If so, append an entry to this skill's
`LEARNINGS.md` (`skill-smith` §3 has the format), and file a `kind:skill-issue` board item if it needs a code or
tool change.
