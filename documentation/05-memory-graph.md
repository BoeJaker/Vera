# 05 · Memory Graph

Vera's memory system is a Neo4j-backed knowledge graph augmented with vector search. Every meaningful interaction — a capability call, a chat turn, a research job, a file write, a workspace open — can land on the graph as a node, linked into a per-session activity chain. The graph is what gives the rest of the system long-term, cross-session continuity.

The system has three layers:

1. **`memory.py`** — `MemoryRecord` dataclass, backend abstraction (Neo4j + vector), the canonical `store()`/`query()` API.
2. **`memory_hooks.py`** — session graph helpers, `memory.*` capabilities, the session graph endpoints.
3. **`memory_second_order.py`** — inferred edges (co-occurrence, similarity) built on top of the first-order chain.

---

## 1. The MemoryRecord

```python
@dataclass
class MemoryRecord:
    id:            str
    session_id:    str
    record_type:   str      # "message" | "event" | "fact" | "summary" | "entity" | ...
    source_type:   str      # "user" | "ai" | "tool" | "system" | ...
    category:      str      # dotted: "cap.fabric" | "ide.workspace" | "research.job"
    tags:          List[str]
    text:          str      # short, indexable (≤500 chars)
    full_text:     str      # the complete content
    summary:       str
    human_text:    str
    ai_output:     str
    importance:    float    # 0–1, used for visual weighting + retention scoring
    archived:      bool
    language:      str
    capability:    str      # which cap created this record
    content_hash:  str
    parent_id:     str      # DERIVED_FROM edge to parent
    model:         str
    metadata:      dict     # arbitrary JSON
    created_at:    str      # ISO timestamp
    updated_at:    str
```

Records are stored in two places by default:

- **Neo4j**, as `(m:Memory {...})` nodes with all fields as properties.
- **Vector store** (Chroma), with `text` (or `full_text` truncated) embedded for semantic search.

Session nodes are stored as `(s:Session {session_id, agent_name, created_at, ...})`. The Neo4j backend creates a `(:Session)-[:CONTAINS]->(:Memory)` edge automatically for every record with a `session_id`, so the harness can scope queries to a single session without joining anything explicitly.

---

## 2. The session chain

Every record stored with a `session_id` participates in two automatic edge patterns:

### `:Session -[:CONTAINS]-> :Memory`

Created by the Neo4j backend at store time, for every record with a session ID. This is the *parent-of* relationship — anything in the session is contained by it.

### `:Memory -[:NEXT_IN_SESSION]-> :Memory`

Created at store time: matches the most recent existing record in the same session whose `created_at < this.created_at`, and links it to the new record. This gives every session a linear chronological chain.

### `:Memory -[:DERIVED_FROM]-> :Memory`

Created when `parent_id` is set — explicit lineage. Used for things like "this summary was derived from those raw messages" or "this analysis was derived from that research job."

### `:Memory -[:FOLLOWS_ACTIVITY]-> :Memory`

The richer activity chain, separate from `NEXT_IN_SESSION`. Where `NEXT_IN_SESSION` is purely temporal, `FOLLOWS_ACTIVITY` tracks *causal* flow — one capability triggering the next, even across sessions, with edge properties carrying the category and timestamp.

The capability decorator's activity worker is what writes `FOLLOWS_ACTIVITY`. The `_SESSION_CURSOR` dict tracks the last node ID per session, and every new cap call links from the cursor to itself, updating the cursor.

---

## 3. Activity recording

When a capability with `memory="on"` runs and has a `session_id`, the wrapper enqueues an activity record onto `_ACT_QUEUE`. The `_activity_worker` background task drains the queue every two seconds and writes:

- One `MemoryRecord` per cap call (the **call node**), tagged `cap.<group>`, with both input parameters and output stored in `full_text` and `metadata`.
- A `FOLLOWS_ACTIVITY` edge from the previous chain step.
- (Optionally) a fabric record in dataset `caps.<group>` for semantic recall.

The worker uses a single rich node per call, **not** a call+output pair — the cap node carries both, and the Neo4j backend's auto-created session edge plus the FOLLOWS_ACTIVITY edge are enough to express the relationship.

Activity recording skips:

- Capabilities with `memory="off"`.
- Capabilities in groups: `fabric`, `memory`, `obs`, `health`, `ui` (these would recurse or pollute).
- Capability calls without a session ID (nowhere to attach them).

The `VERA_ACTIVITY_RECORDING` env var defaults to `"0"`. Set it to `"1"` to enable the worker. With it disabled, the activity queue silently fills and is discarded — no graph writes occur.

---

## 4. The `memory.*` capabilities

| Cap | Path | Purpose |
|---|---|---|
| `memory.store` | `POST /memory/store` | Store an arbitrary record |
| `memory.query` | `POST /memory/query` | Hybrid vector + text + filter search |
| `memory.search` | `POST /memory/search` | Semantic search (vector-only) |
| `memory.record_turn` | `POST /memory/record_turn` | Record a chat turn (human + AI) |
| `memory.agent_context` | `POST /memory/agent_context` | Build context for an agent (recent + relevant) |
| `memory.session_nodes` | `GET /memory/session/nodes` | All nodes in a session |
| `memory.session_edges` | `GET /memory/session/edges` | All edges in a session |
| `memory.session_graph` | `POST /memory/session/graph` | Combined nodes + edges (graph panel) |
| `memory.graph_stats` | `GET /memory/graph/stats` | Counts by label, category, edge type |
| `memory.graph_clear` | `POST /memory/graph/clear` | Destructive: wipe everything (requires `confirm=true`) |
| `memory.session_summary` | various | Summary helpers (categories, top nodes, etc.) |

### `memory.query`

Hybrid retrieval. Combines:

- **Vector similarity** — embed the query, find nearest neighbours in Chroma
- **Text match** — Neo4j full-text index on the `text` and `full_text` fields
- **Filters** — by `session_id`, `record_type`, `tags`, time range, importance threshold

Results are fused (score from each backend, weighted, deduplicated by ID).

### `memory.agent_context`

Builds a context blob for an LLM agent: pulls the N most recent records in the session, plus the M most semantically relevant records across history, formats them as a chronological narrative. Used by chat agents to give the LLM continuity.

---

## 5. Cross-module integration

Modules like `ide_capabilities.py`, `research_capabilities.py`, and `agents.py` use shared helpers to write to the graph in a uniform way:

- `_record(...)` (each module has its own) — stores a `MemoryRecord` with category like `ide.workspace`, `research.job_started`, `agent.chat_turn`.
- `_link(from_id, to_id, rel, ...)` — adds an explicit edge (e.g. `TRIGGERED_BY`, `PRODUCES`, `CITES`).
- The session's FOLLOWS_ACTIVITY cursor is shared across modules so all modules see the same chain.

`session_integration.py` provides cap wrappers (`integration.ide.*`, `integration.research.*`) that other systems call when they generate events outside the normal cap flow. For example, when researcher_api (running standalone) completes a job, it calls `integration.research.job_completed` to put the result on the graph.

---

## 6. Second-order edges

`memory_second_order.py` adds inferred edges that aren't from any explicit action:

- **Co-occurrence** — entities mentioned in the same record get `CO_OCCURS` edges with a count.
- **Similarity** — vector-nearest record pairs get `SIMILAR_TO` edges with the similarity score.
- **Topical clustering** — records sharing tags or categories above a threshold get cluster membership.

These edges are visible in the memory graph panel but don't participate in physics-driven layout (they'd distort the topology). Their purpose is recall: when querying "what's related to this?" the second-order edges expand the result set.

---

## 7. Querying the graph

A few canonical Cypher queries the system uses:

```cypher
// All records in a session, newest first
MATCH (s:Session {session_id:$sid})-[:CONTAINS]->(m:Memory)
RETURN m ORDER BY m.created_at DESC LIMIT 200

// Activity chain through a session
MATCH (a:Memory {session_id:$sid})-[r:FOLLOWS_ACTIVITY]->(b:Memory)
RETURN a, r, b ORDER BY r.ts

// Capability frequency per session
MATCH (s:Session {session_id:$sid})-[:CONTAINS]->(m:Memory)
WHERE m.capability <> ''
RETURN m.capability AS cap, count(*) AS n ORDER BY n DESC

// Find similar records across sessions
MATCH (m:Memory {id:$mid})-[s:SIMILAR_TO]->(n:Memory)
RETURN n ORDER BY s.score DESC LIMIT 10
```

The Neo4j driver requires modern `RETURN x AS alias` syntax for property access (use `record["alias"]` not `record["x.property"]`).

---

## 8. The memory graph panel

`memory_graph_panel.html` renders the session graph using the `vera-graph.js` component in `memory` mode. Features:

- **Source picker**: Fabric / Memory / Net
- **Memory mode selector**: Current session / Recent (24h) / All
- **Node-type chips**: toggle visibility per record type
- **Edge-type chips**: toggle per relationship type (NEXT_IN_SESSION, FOLLOWS_ACTIVITY, DERIVED_FROM, CONTAINS, ...)
- **Layout modes**: Default / Force+Axis / Timeline / Hierarchy / Radial
- **Pagination**: "Load older" pulls records older than the current oldest visible

Clicking a node opens a detail drawer with the record's full text, metadata, and action buttons (open in source panel, query similar, etc.).

The panel reads theme variables from the parent harness via the postMessage bridge so it stays in step with the active theme.

---

## 9. Maintenance

- **`memory.graph_clear`** — wipes all `:Memory` and `:Session` nodes. Destructive, requires `confirm=true`.
- **`memory.graph_stats`** — diagnostic. Returns memory count, session count, top categories, edge type distribution, and crucially a *duplicate* report (sessions sharing the same `session_id`, which should never happen — if it does, there's a bug somewhere).
- **Archived records** — set `archived=true` to hide a record from default queries without deleting it.

---

## See also

- [Data Fabric](./06-data-fabric.md) — the semantic store records also land in (caps datasets)
- [Galaxy Graph](./09-galaxy-graph.md) — the rendering component
- [Capability Framework](./01-capability-framework.md) — activity recording mechanics