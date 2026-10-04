# Status updates, handovers and the outbox

## Why status rides on records

A status written only in chat is lost to every other agent and costs the owner a second read. A status written
into the pipeline, the board or the audit log is captured once, joined to the commit and the item, searchable,
and shown in Loop Lab. So status goes onto the record the work already writes, and chat gets a one-line pointer.

## Cadence

1. At every **phase boundary**: recon done · plan written · branch cut · tests run · review in · promoted · handed
   over.
2. In long stretches, at most **~25 minutes or ~40 tool calls** apart.
3. **At once** on a blocker, a surprise that changes the plan, or before a destructive or outward-facing step.

## Shape (≤ 600 characters)

```
phase: <recon|plan|build|test|review|land|handover>
did: <what changed since the last status>
evidence: <paths, shas, pipeline id, board item, test counts>
next: <the next concrete step>
blockers: <none | what, and what would unblock it>
confidence: <high|medium|low>
```

## Where (cheapest first)

| You are about to call | Put the status in |
|---|---|
| `evolve.pipeline.adopt` / `begin` | `summary` / `title` |
| `evolve.pipeline.review.request` | `reason` |
| `evolve.pipeline.review` | `findings` (the adversarial review is a status) |
| `board.item.upsert` / `board.item.move` | the item body's state, plus a `progress` comment |
| nothing else | `board.comment(kind="progress")` on your item; later `agent.status.post` (agent-ops AO-D.1) |

**Chat** (one line): `[build] tests green on feat/x (pipeline 1a2b) — see board item <id>`.

**Embedding:** pipeline adopt/review and board comments are already embedded by Vera's memory hooks. Don't also
paste the same text into memory or fabric. The planned `agent.status.post` is deliberately not embedded.

## The handover file

`shared-planning/<unit>/HANDOVER-<YYYY-MM-DD>-<agent>.md`:

```
# Handover — <unit> — <date> — <agent>

## NOT done (read first)
- …
## Done
- <change> — branch <b>, pipeline <id>, landed <sha on bleeding-edge|main>, board <item>
## State
- running: <loops, censuses, containers, worktrees + claims>; open claims: <items>
## Next
1. …
## Evidence
- <paths>
```

Update the unit README's **Current state** line to match. Release or explicitly keep claims.

## The outbox (Vera unreachable)

`shared-planning/_outbox/<agent>-<YYYY-MM-DD>.jsonl`, one JSON object per line:

```
{"cap": "board.comment", "args": {"id": "<item id>", "frm": "<agent>", "kind": "note", "body": "..."}, "why": "...", "ts": "<iso>"}
{"cap": "board.item.upsert", "args": {...}, "why": "...", "ts": "<iso>"}
```

**Replay** (the next session with Vera):

- For each line, check that the item still exists and the claim or lane still makes sense, then call it.
- Delete replayed lines. Keep failed lines with an `"error"` field.
- Never replay a `board.claim` older than ~90 minutes without re-checking the item.
