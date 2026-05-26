# 07 · Research System

Vera's research subsystem is a self-contained pipeline server (`researcher_api.py`, default port `:8765`) that runs deep, multi-stage research jobs combining web search, recursive crawling, NLP analysis, and LLM synthesis. It exposes 11 named pipelines through Vera's capability framework, integrates with the data fabric for long-term recall, and writes activity to the memory graph for cross-session continuity.

The 11 pipeline capabilities all live in `research_capabilities.py` and route requests to `researcher_api` over HTTP. `research_vera_bridge.py` and `research_fabric.py` keep the standalone researcher aligned with Vera's cluster routing and fabric persistence.

---

## 1. Architecture

```
            ┌─────────────────────────────────────────┐
            │  Vera Orchestrator  (port 8999)         │
            │                                         │
            │  research.* capabilities (11 pipelines) │
            │  ├─ proxy to researcher_api over HTTP   │
            │  ├─ poll for completion                 │
            │  ├─ record activity to memory graph     │
            │  └─ persist results to data fabric      │
            │                                         │
            │  research.recall.* capabilities         │
            │  └─ semantic search over the corpus     │
            └─────────────┬───────────────────────────┘
                          │ HTTP
                          ▼
            ┌─────────────────────────────────────────┐
            │  researcher_api  (port 8765)            │
            │                                         │
            │  ┌─────────┐   ┌────────┐   ┌────────┐ │
            │  │ Thinker │   │ Writer │   │Analyst │ │
            │  │   GPU   │   │  CPU A │   │  CPU B │ │
            │  └─────────┘   └────────┘   └────────┘ │
            │                                         │
            │  Sources: SearXNG · Brave · DDG · arXiv │
            │           NVD · GitHub · HackerNews     │
            │                                         │
            │  Pipeline stages:                       │
            │  Direct → Search → NLP → Synthesise →   │
            │  Write → Expand → Verify → Reference    │
            └─────────────────────────────────────────┘
                          │
                          ▼
            ┌─────────────────────────────────────────┐
            │  research_vera_bridge                   │
            │  - LLM routing through Vera cluster     │
            │  - Fabric persistence                   │
            │  - Activity tracking                    │
            └─────────────────────────────────────────┘
```

---

## 2. The three-tier model

Research uses three LLM tiers, mapped to the Ollama cluster via `research.routing.resolve`:

| Tier | Default node | Role |
|---|---|---|
| **THINKER** | GPU node | Slow, careful reasoning. Builds the `ResearchDirective`, plans expansions, synthesises deep reports. |
| **WRITER** | CPU node A | Fast generation. Writes drafts, extracts findings per sub-question, fills file/code templates. |
| **ANALYST** | CPU node B | NLP processing. Entity extraction, knowledge bullet extraction, citation scoring, fact verification. |

The three run **concurrently** during most pipeline stages — while Thinker is synthesising, Writer is drafting, and Analyst is processing citations. This is the core performance win over a sequential model.

`research.routing.resolve` is the cap researcher_api calls at startup to learn which Vera instances should fill each tier. The mapping respects current online status — if the GPU is down, THINKER falls back to a CPU.

---

## 3. The 11 pipelines

Each pipeline is a `research.<name>` capability that hits a `researcher_api` endpoint with pre-configured `mode` and `output_mode` flags.

| Capability | Mode | Output | Description |
|---|---|---|---|
| `research.report` | single | report | Default. Thinker directs → search → writer drafts → analyst verifies → references. |
| `research.parallel` | parallel | report | Writer decomposes query into 3–5 sub-questions, gathers and extracts findings concurrently, Thinker synthesises. |
| `research.deep` | deep | report | Recursive research: directive → recursive crawl → Thinker synthesises knowledge base → Writer drafts → expansion pass. |
| `research.guide` | single | guide | Section-by-section long-form guide. Thinker outlines, Writer fills each section with sources. |
| `research.code` | single | code | Architect → implement → review chain. Thinker designs file structure, Writer implements file-by-file, Analyst reviews. |
| `research.filestore` | single | filestore | Generate a full file tree with content. Used for project scaffolding. |
| `research.quick_search` | single | report | Fast gather + entity extraction, no deep synthesis. Single search round. |
| `research.analysis` | single | report | Analyse provided citations without external sources. Optional rerun of an existing job's citations. |
| `research.security` | parallel | report | NVD + GitHub + web. Optimised for CVEs and vulnerability research. |
| `research.academic` | deep | report | arXiv + web in deep mode. For research papers, scientific surveys. |
| `research.nlp_addon` | single | report | Triggers the standalone NLP server (`:8766`) for specialised analysis. |

All pipelines accept a `query` (and `session_id`, `project_id` for tracking). Pipelines with iteration support also accept `context` and `context_mode="fresh|continue"`.

Returns a job ID and initial status; the cap then polls until completion and emits progress events.

---

## 4. The pipeline stages

A typical research run goes through these stages (with variations per output mode):

### Stage 1 — Directive (THINKER)

The Thinker analyses the query and produces a `ResearchDirective`:

- `output_style` — narrative report, structured guide, code spec, etc.
- `key_questions` — what facts the report needs to answer
- `sub_questions` — decomposition for parallel modes
- `scope_focus` / `scope_exclusions` — what counts as on-topic
- `writer_sys` — a tailored system prompt for the Writer based on this query
- `nlp_tools` — which Analyst NLP phases to enable
- `depth` — recursive depth for deep mode

This stage emits a `directive` broadcast event so the panel can show the planned approach.

### Stage 2 — Search (WRITER)

Sources are searched in parallel:

- **SearXNG** — the primary web search backend (configured via `VERA_SEARXNG_URL`)
- **Brave** — fallback web search (requires `BRAVE_API_KEY`)
- **DuckDuckGo** — secondary fallback
- **arXiv** — scientific papers
- **NVD** — CVE database
- **GitHub** — repos, issues, code
- **HackerNews** — discussion threads

The Writer goes through top results and extracts findings, populating the citations list. Citations stream into the panel as they're found.

### Stage 3 — Analyst Engine (ANALYST, concurrent with Search/Synthesis)

The Analyst runs 12 NLP phases on citations:

1. Tokenisation
2. Sentence splitting
3. Entity extraction
4. Relationship extraction
5. Date / number extraction
6. Knowledge bullet extraction
7. Citation scoring
8. (LLM phase, optional) Structural classification
9. Topic clustering
10. Contradiction detection
11. Fact deduplication
12. Compact context generation

Phases 1–7 and 9–12 are host-local (no LLM) and finish in 0.1–2 s. Phase 8 uses the Analyst instance. The compact context generated by phase 12 is what the Writer sees instead of raw source text — significantly improving report quality.

### Stage 4 — Synthesis (THINKER, deep modes only)

The Thinker integrates findings across the knowledge base. Resolves contradictions, identifies key insights, plans the writing structure. Only runs in deep mode.

### Stage 5 — Writing (WRITER)

The Writer drafts the report using the directive's `writer_sys`, the compact analyst context, and the citation list. Streams tokens to the panel as they're generated.

### Stage 6 — Expansion (optional, deep modes)

Thinker scans the draft for thin sections and plans expansions. Writer gathers expansion sources and writes addendum sections.

### Stage 7 — Verification

Analyst pass over the final draft: flags unsupported claims, lists referenced facts, generates an analyst report section.

### Stage 8 — References

Numbered reference list appended. Citations get sequential `[N]` markers; the panel renders these as clickable chips that open the source.

---

## 5. Modes vs. output modes

The matrix:

| Mode | Output: report | Output: guide | Output: code | Output: filestore |
|---|---|---|---|---|
| **single** | One search round → write | Section-by-section guide | Coding pipeline (architect → implement → review) | File tree gen |
| **parallel** | Sub-question decomposition → parallel gather → synthesise | (routes to single) | (routes to single) | (routes to single) |
| **deep** | Recursive research → thinker synthesises → writer drafts → expand | (routes to single) | (routes to single) | (routes to single) |

The `output_mode` controls what gets written; the `mode` controls how the research is gathered. Code/Guide/Filestore output modes don't benefit from parallel gathering, so they're routed to single mode internally.

---

## 6. Code pipeline

`research.code` runs a distinct three-agent flow:

1. **Architect (THINKER)** — designs the file structure and module boundaries, produces a spec.
2. **Implement (WRITER)** — writes each file one at a time, wrapped in `=== FILE: path ===` markers. Files are parsed and materialised to disk as they're produced.
3. **Review (ANALYST)** — reviews each file for issues, can request rewrites.

A `chain_id` parameter lets you continue an existing code project across runs — the architect sees the previous output and adds to it rather than starting from scratch. This is how multi-run projects scale beyond a single LLM context.

---

## 7. Filestore pipeline

`research.filestore` produces a directory of generated files. The pipeline:

1. Thinker builds a `research_directive` for the file structure.
2. Search/gather runs to provide context.
3. Analyst extracts knowledge bullets from citations.
4. For each planned file, Writer generates the content wrapped in file markers.
5. The complete file tree is materialised to the project directory and a README index is written.

Output is the README plus all generated files. The panel shows the file tree as it's populated.

---

## 8. researcher_api

The standalone server lives at `:8765` by default. It owns:

- **Job state** — `ResearchJob` dataclass with status, citations, file tree, result text.
- **Project state** — `Project` dataclass with context summary across jobs in the same project.
- **Instance pool** — `get_instance(ModelTier.X)` returns the best available Ollama instance for that tier. Uses `research.routing.resolve` to align with Vera's cluster.
- **WebSocket broadcast** — every job gets a `/ws/research/{job_id}` channel. Events: `status`, `step`, `citation`, `token`, `directive`, `file_created`, `done`, `error`.
- **SQLite DB** (`research_db.py`) — primary store for jobs, citations, pages, sessions, projects.
- **Coding pipeline** (`agents.py` integration) — the three-agent code flow.

researcher_api is intentionally a separate process. It has its own DB, its own port, its own scheduler. Vera's capability layer is a thin wrapper that submits jobs, polls them, and persists results — researcher_api can run standalone if needed.

---

## 9. The Vera bridge

`research_vera_bridge.py` is the integration layer:

### LLM routing

When researcher_api makes an Ollama call, the bridge intercepts it and routes through Vera's `ollama.generate_raw` cap. This means:

- The GPU + 2 CPU cluster is shared with chat/dream/IDE rather than the researcher maintaining its own pool.
- Failover works across the whole cluster transparently.
- A single point of metrics — all LLM usage shows up on Vera's cluster panel.

If Vera is unreachable (`VERA_BASE_URL` unset or down), the bridge gracefully degrades to direct Ollama calls.

### Fabric persistence

Every job / citation / crawled page / notebook / cell is written to the data fabric in addition to research_db:

- `research.jobs` — job records
- `research.results` — final markdown
- `research.citations` — deduplicated citation corpus
- `research.crawl_pages` — all fetched pages
- `research.notebooks` / `research.notebook_cells` — notebook content
- `research.files` — generated code/files

This means subsequent recall queries (`research.recall.*`, `fabric.query`) find research artifacts semantically, not just by ID.

### Activity tracking

Every search / crawl / LLM call hits the `research.activity.*` capabilities, which write to the memory graph's FOLLOWS_ACTIVITY chain. This lets the agentic loop and dream cycles see research as a sequence of causally-linked events rather than opaque job state.

---

## 10. Recall

`research_recall_capabilities.py` exposes semantic recall over the corpus:

| Cap | Purpose |
|---|---|
| `research.recall.search` | Search `research.*` datasets by query |
| `research.recall.jobs` | List jobs matching a filter (session, project, status) |
| `research.recall.job` | Pull a complete job + its citations + result |
| `research.recall.crawled_pages` | Search crawl datasets by query and/or domain |
| `research.recall.notebook` | Pull a notebook + its cells |
| `research.recall.notebooks_list` | List all notebooks |
| `research.recall.session` | All jobs for a session, plus the FOLLOWS_ACTIVITY chain |
| `research.recall.session.list` | List research sessions known to the fabric |
| `research.recall.datasets` | List all research-related datasets with record counts |
| `research.recall.history` | Recent research activity (job starts, completions, file writes) |

These caps are what the dream system, the chat agent, and the IDE agent use to find prior research — they call recall instead of submitting a fresh research job.

---

## 11. Notebooks

The notebook system (`notebook_panel.html`) is a Jupyter-style document where each cell is one of:

- **research** — a research query that runs a pipeline and embeds the result
- **note** — free text
- **code** — generated code (or raw text editing)
- **chat** — LLM conversation against the notebook context

Cells are saved to the fabric (`research.notebook_cells`) on every change. The notebook is one logical document; cells share context — later cells can reference earlier ones via `{cell:N}` placeholders.

Capabilities: `research.activity.cell_save`, `research.recall.notebook`, `research.recall.notebooks_list`.

---

## 12. NLP addon

A separate server (`:8766`, configured via `VERA_NLP_URL`) runs specialised NLP modules:

- Named entity recognition
- Sentiment analysis
- Topic modelling
- Summarisation
- Question generation

`research.nlp_addon` is a research pipeline that triggers these modules. The NLP panel (`nlp_panel.html`) is a standalone tab for ad-hoc NLP runs against arbitrary text or fabric datasets.

---

## 13. Iteration mode

The research panel has an "Iterate" toggle. With it on, submitting a new query while a previous job is complete continues the same research thread:

- The previous job's result becomes `prior_context` for the new job.
- The new job is tagged `[Iter N]` in the thread display.
- A "Dive" sub-query can drill into a specific section of a previous report.

`context_mode="continue"` is what enables iteration server-side. `context_mode="fresh"` starts independent.

---

## 14. The research panel

`research_panel.html` is the primary UI:

- **Thread view** — every research run in this session becomes an entry in the thread, with mode/output badges, status, activity strip (live tool calls + crawl tray), and the rendered result.
- **Mode/output selectors** — segmented buttons for `single|parallel|deep` and `report|guide|filestore|code`.
- **Iterate toggle** — switches between fresh and continuation submissions.
- **Bookmarks** — flag citations for keeping.
- **Citations bar** — chips for every cited source, clickable to open.
- **Agent cards** — live per-tier status (Thinker/Writer/Analyst) with model, tokens, elapsed time.
- **Project selector** — group jobs into a Project so they share context.

The thread is persistent — closing and reopening the panel preserves the last N entries.

---

## See also

- [Data Fabric](./06-data-fabric.md) — where research artifacts live
- [Memory Graph](./05-memory-graph.md) — the activity chain for research runs
- [Ollama Cluster](./04-ollama-cluster.md) — the tier-to-instance routing
- [IDE Module](./08-ide.md) — code generation, which shares the same pipeline