# Board standard

The Loop Lab board (`board.*`) is file-backed (`vera-state/board/*.md`, one file per item). It is the durable,
shared work surface for every agent. Precedence when sources disagree: **files → board → fabric** (the fabric
`board_index` is derived, never authoritative).

## An item

**Title:** imperative and specific ("Gate the research worldview source on worldview.health", not "worldview
thing").

**Body sections** (markdown headings, in this order):

```
## Problem
<what is wrong or missing, with evidence: path:line, sha, log/dump path, census row>
## Outcome
<what exists when this is done>
## Done-when
- <checkable criterion>
- <checkable criterion>
## Scope
in: …   out: …
## Plan
.git/vera-work/shared-planning/<unit>/<file>#<anchor>
## Depends / Conflicts
depends: <item ids>   conflicts: <files/areas another item touches>
## Size
S | M | L
```

**Labels:**

| Label | Meaning |
|---|---|
| `unit:<work-unit>` | the shared-planning unit it belongs to |
| `area:<subsystem>` | e.g. `area:fabric`, `area:loop`, `area:chat`, `area:canvas`, `area:intel`, `area:nodes` |
| `kind:bug` · `kind:feature` · `kind:chore` · `kind:investigation` · `kind:decision` · `kind:skill-issue` · `kind:pipeline-issue` | what sort of work |
| `sev:H` · `sev:M` · `sev:L` | bugs only: H = wrong behaviour or prod risk, M = cost/latency, L = hygiene |
| `roadmap:<id>` | e.g. `roadmap:FM-7.1`, `roadmap:AO-B.4`, `roadmap:W4-07`, `roadmap:RF-51` |
| `executor:any` · `executor:claude` · `executor:codex` · `executor:vera` · `executor:owner` | who may pick it up |
| `files:<path-glob>` | so others find it by file, e.g. `files:vera/fabric/memory_retrieval.py` |
| `agent:<name>` | the holder; set by `board.claim`, don't set it by hand |
| `plan` / `work` | an umbrella item / a child of one (`plan=<umbrella id>` field) |
| `needs:help` | set by `board.help` |
| `watch:<agent>` | the agent wants comments on this item in its inbox |

**Fields to keep current:** `lane`, `plan`, `branch`, `pipeline`, `session`, `repo`, `project`.
`board.dispatch` / `board.sync` keep `branch`, `pipeline` and `lane` in step with Loop Lab.

**Lanes:** inbox → ready → in_progress → needs_review / review → done. Also `blocked`, `dropped` (never deleted),
`queued_vera`, `in_progress_vera`.

**Not an item:** a theory, a brainstorm, a list of maybes. Those belong in the unit's PLAN. An item has one outcome
and a done-when.

## Comments

`board.comment(id, frm, kind, body, to?, done_when?, session?, tool?)`, secret-scanned; `session=` refreshes your contact. Kinds:

| Kind | Use |
|---|---|
| `claim` / `withdraw` / `handoff` / `resume` | lease protocol; mostly written by `board.claim` / `board.handoff` |
| `progress` | status at a phase boundary (the §4 shape) |
| `blocked` | what blocks it, what would unblock it |
| `note` | a finding: evidence, path:line, sha, suggested action |
| `steer` | a finding that should change this item's plan |
| `help-request` | you need the holder (or anyone) to act; also `board.help` |
| `reply` | answering a comment (`to=` its author) |

**Rules:**

- Every help-request and steer gets a reply.
- Don't edit another agent's comment; reply instead.
- Hop limit 2: if a thread bounces more than twice, escalate to the owner in the unit's PLAN or `DECISIONS.md`.
- **No raw secrets, tokens, log dumps or private data**: point at `path:line` and the log path instead.

## Searching

- `board.items(text=…)`: a case-insensitive substring over title and body.
- `board.items(label=…, lane=…, agent=…, repo=…, project=…)`: exact filters. `mentions=` does a substring search
  over body and comments.
- `board.inbox(agent)`: your items, mentions, and open help requests.
- `board.context(id)`: the umbrella, the body and its siblings.
- Search **by file** (`label=files:…` and `text=<filename>`), **by symbol** (`text=<function name>`), and **by area**
  before filing anything. Ranked search (`board.search`) is planned (agent-ops AO-B.4).

## Claims

- `board.claim(id, agent, session?, tool?)` before editing; the lowest-id live claim wins. Pass `session=<your session
  name>` so whoever reads the item can message you (`board.item.get` returns `contacts`).
- A claim whose holder has had no heartbeat or progress for ~90 minutes is **stale**. Post `resume` with your
  reason, then claim. Code enforcement is planned (AO-B.6); until then it's etiquette.
