# 06 · Data Fabric

The polyglot data fabric is Vera's unified data layer. It combines multiple database paradigms — vector (FAISS + Chroma), graph (Neo4j), relational (SQLite + PostgreSQL), and object storage (Garage / Ceph S3) — into a single ingestion pipeline and query DSL. Anything Vera produces or consumes that's worth keeping ends up in the fabric, where it can be recalled semantically, by relation, by exact filter, or by any combination of the three.

The fabric is what makes Vera's components additive rather than siloed. A research result is fabric-recallable, so the IDE agent can find it. A crawled page is fabric-recallable, so dream cycles can use it. A chat message is fabric-recallable, so future sessions can build on it.

![Data Fabric sources dashboard](https://github.com/BoeJaker/Vera/blob/main/images/DF%20-%20Sources%20-%20Dashbord.jpg)

---

## 1. Storage layers

| Layer | Role | Notes |
|---|---|---|
| **SQLite** | Always-available local fallback | Eager init at import — tables exist before first HTTP request. Used as primary store when other backends are offline. |
| **PostgreSQL** | Authoritative relational store | Tables for datasets, records, sources, relationships. Uses `cfg.POSTGRES_URL`. |
| **FAISS** | Persistent sharded vector index | Sharded by dataset for scalable similarity. Snapshots saved to object storage. |
| **ChromaDB** | Metadata-filtered vector search | Used alongside FAISS for metadata-rich queries. |
| **Neo4j** | Auxiliary graph layer | Dataset relationships, lineage, categories, entity graph. |
| **Redis** | Hot cache + streaming | Query result cache, ingestion stream, shared pool with orchestrator. |
| **Garage / Ceph** | Object storage | Large blobs, FAISS index snapshots. S3-compatible API, optional. |

Each layer can fail independently. The pipeline degrades gracefully — if Neo4j is down, ingestion still writes to SQLite/Postgres/Chroma; if FAISS is down, queries fall through to Chroma; if Chroma is down, queries fall through to text search.

---

## 2. The data model

```python
@dataclass
class DataRecord:
    id:         str
    dataset_id: str          # logical grouping (e.g. "research.results", "web.crawl.example_com")
    source:     str          # "api" | "web" | "research" | "chat" | ...
    source_id:  str          # the entity that produced this record (e.g. session_id, job_id)
    text:       str          # ≤2000 chars, indexable
    data:       dict         # full structured payload
    tags:       List[str]
    created_at: str
```

Datasets are first-class: every record belongs to one dataset, and datasets carry their own metadata, sources, and (optionally) explicit relationships to other datasets.

---

## 3. The ingestion pipeline

`DEFAULT_PIPELINE` is a sequence of stages, each responsible for one concern:

```
Hash  →  Schema  →  TextExtract  →  Embed  →  PG  →  Vector  →  Neo4j
```

| Stage | Action |
|---|---|
| **Hash** | Compute a content hash for deduplication |
| **Schema** | Infer or refine the dataset's schema from the record |
| **TextExtract** | Pull indexable text from structured fields |
| **Embed** | Generate embedding via Ollama (`llm.embed` cap → `OLLAMA_EMBED_URL`) |
| **PG** | Write to PostgreSQL (or SQLite fallback) |
| **Vector** | Insert into FAISS shard + Chroma collection |
| **Neo4j** | Register dataset/relationship nodes |

Each stage is async, and the pipeline awaits them in sequence per record. A stage failure logs the error and continues — partial ingestion is preferred over none.

### Post-ingestion pipeline

After every batch is ingested, a non-blocking `_post_ingest_pipeline` runs:

- **Source registration** — if the record came from a known source, update the source's record count.
- **Entity extraction** — extract named entities, dates, places from the text (writes to the second-order entity graph).
- **Loom linking** — when configured per-dataset, run cross-dataset relationship inference.

Errors here are logged but don't surface to the caller — ingestion is considered successful as soon as the primary stages have written the record.


![Data Graph dashboard](https://github.com/BoeJaker/Vera/blob/main/images/DF%20-%20Graph%20-%20Fabric%20Structure.jpg)

---

## 4. Ingestion API

### `fabric.ingest`

```python
await ingest_dataset(
    dataset_id = "my_dataset",
    data       = [{"text": "...", "title": "...", "extra_field": ...}, ...],
    source     = "api",
    tags       = ["t1", "t2"],
    source_id  = "session_abc",
)
```

Items can be:

- A list of dicts → each dict becomes one record (text auto-extracted from `text` field or concatenation of string values)
- A list of strings → each becomes a record with that string as text and as `data.value`
- A single dict or string → wrapped to a list

Returns `{ingested, errors, dataset_id}`.

Emits `fabric.ingested` event.

### `fabric.update`

Update an existing record by ID. Re-runs embedding if text changed.

### `fabric.delete_dataset`

Drop a dataset's records across all backends.

---

## 5. Query DSL

`fabric.query` accepts a hybrid query combining text, vector, filter, and graph expansion:

```python
await cap_fabric_query(
    text       = "machine learning frameworks",   # keyword/FTS search
    vector     = "ML libraries for Python",       # semantic search
    dataset_id = "research.results",              # scope to one dataset
    top_k      = 20,
    include_data = False,
)
```

Either or both of `text` and `vector` may be supplied. With both, results are fusion-scored (weighted vector + text + graph proximity, deduplicated by ID).

The cap also accepts:

- A `query` dict for the legacy API: `{text, vector, dataset_id, top_k, filter: {...}}`
- A JSON-encoded string (for MCP callers that serialise everything)
- A plain string (auto-converted to `text=...` + `vector=...`)

### Filter syntax

```python
{
    "filter": {
        "tags":     {"contains": "important"},
        "source":   "research",
        "created_at": {"gte": "2025-01-01"},
        "data.author": "Joe"
    }
}
```

Filters apply against PostgreSQL columns when the field is structured, and against Chroma metadata when going through the vector path.

### Graph expansion

If a dataset has explicit Neo4j relationships to others (set up via `fabric.link_datasets`), a query against one dataset can be expanded to include semantically-related results from linked datasets. The fusion score includes a graph-proximity component (decay by distance).

---

## 6. Sources

Sources are external feeds that get pulled into the fabric on demand or on schedule:

| Source type | Examples |
|---|---|
| RSS | News feeds, blogs |
| API | REST endpoints with JSON responses |
| Database | SQL queries (config-defined) |
| Web | Single URL or recursive crawl |
| File | Local file or upload |

### Source caps

| Cap | Path | Purpose |
|---|---|---|
| `fabric.source.add` | `POST /fabric/sources/add` | Register a new source |
| `fabric.source.list` | `GET /fabric/sources` | List all sources |
| `fabric.source.pull` | `POST /fabric/sources/pull` | Manually pull one source now |
| `fabric.source.delete` | `POST /fabric/sources/delete` | Remove a source |

When a source is pulled, items are deduplicated by content hash and bulk-inserted in chunks of 5, with `fabric.record.ingested` progress events emitted per chunk (so the UI can show records streaming in live). The async embed/vector/graph pipeline runs after SQLite writes so the UI sees data immediately.

---

## 7. Web acquisition

`fabric_web_acquisition.py` extends the fabric with a richer web pipeline beyond basic source pulls:

| Cap | Purpose |
|---|---|
| `fabric.web.acquire` | Multi-stage web crawl with full content fetch, page structure extraction (headings, sections, code blocks), and negative-filter exclusions |
| `fabric.web.continue` | Resume a previous acquisition that paused or was cancelled |
| `fabric.web.acquire_status` | Get status of running/completed acquisitions |
| `fabric.entity_graph.extract` | Extract a second-order entity graph from a dataset's records |
| `fabric.entity_graph.query` | Query the entity graph by entity, type, or dataset |
| `fabric.entity_graph.merge` | Merge duplicate entities across datasets |
| `fabric.entity_graph.bulk_load` | Bulk-load entities and relationships (used by analyser flows) |

![DF - Discover - Web Acquisition_zoomed](https://github.com/BoeJaker/Vera/blob/main/images/DF%20-%20Discover%20-%20Web%20Acquisition_zoomed.jpg)

### Entity graph

Entities (people, orgs, dates, places, technologies, code symbols) extracted from records get stored in Neo4j under the `:Entity` label, with `:MENTIONED_IN` edges to the `:FabricRecord` nodes they came from, and `:CO_OCCURS` / `:RELATES_TO` edges between entities that appear together. Entities are normalised (case-folded, deduplicated) so a single graph node aggregates all mentions across datasets.

### Loom (cross-dataset stitching)

The "Loom" pipeline finds relationships between datasets — pairs of datasets whose records mention the same entities or share topics. The harness's Fabric panel has a Loom tab with four numbered stages (gather, plan, stitch, link) and a graph view showing the resulting cross-dataset edges in distinct colours.

![Data Loom sources dashboard](https://github.com/BoeJaker/Vera/blob/main/images/DF%20-%20Graph%20-%20Loom.jpg)

---

## 8. Fabric capabilities

| Cap | Purpose |
|---|---|
| `fabric.ingest` | Insert records into a dataset |
| `fabric.update` | Update an existing record |
| `fabric.query` | Hybrid vector + text + filter + graph search |
| `fabric.schema` | Get/refine a dataset's schema |
| `fabric.datasets` | List all datasets with record counts |
| `fabric.stats` | Aggregate stats (total records, dataset count, vector count, ...) |
| `fabric.link_datasets` | Add an explicit Neo4j relationship between two datasets |
| `fabric.stream_publish` | Publish a record to the ingestion stream |
| `fabric.delete_dataset` | Drop a dataset |
| `fabric.source.*` | Source management (see §6) |
| `fabric.web.*` | Web acquisition (see §7) |
| `fabric.entity_graph.*` | Entity graph (see §7) |
| `fabric.bus.*` | Configure the ingestion bus |
| `fabric.aux_graph.*` | Query the auxiliary graph (dataset relationships, lineage) |
| `fabric.ai_analyse_links` | LLM-driven Loom suggestion |
| `fabric.ai_stitch` | LLM-driven stitch execution |

---

## 9. The Fabric panel

`fabric_panel.html` has three primary tabs:

### Discover

Topic search → suggest sources → kick off acquisition. The "Deep Crawl →" button hands off a discovered URL to the Web Acquisition tab with the URL and topic pre-filled.

### Web Acquisition

Multi-stage crawl UI. Fields: seed URL, topic, max depth, max pages, breadth, exclude words/URLs, content type filters. Live progress log + a shared crawl graph that's mirrored in the Discover tab.

### Loom (pipeline workbench)

Single-page workbench combining:

- **Graph canvas** (full-height) showing entities, relations, and stitched cross-dataset edges
- **Right-side drawer** (collapsible) with:
  - View controls (source picker, filter, layout)
  - Items list (Entities / Relations / Loom Edges sub-tabs)
  - Dataset Config
  - Automatic Triggers
  - Four numbered pipeline stages
  - Pipeline Log

The entity graph and the stitched cross-dataset graph are separate views — switch via the View controls. Stitched edges have raised alpha for visibility and use the 7 distinct Loom edge type colours.

---

## 10. Recall

Once data is in the fabric, recall is just `fabric.query`. Higher-level recall caps wrap it for common patterns:

- `research.recall.search` — search across `research.*` datasets only
- `research.recall.crawled_pages` — semantic search + domain filter on crawl datasets
- `research.recall.session` — pull all jobs for a research session, cross-reference the memory graph chain
- `research.recall.notebook` — fetch a notebook and its cells

See [Research System](./07-research.md) for the full recall surface.

---

## See also

- [Memory Graph](./05-memory-graph.md) — sister system; cap activity is mirrored to fabric `caps.*` datasets
- [Research System](./07-research.md) — research artifacts are fabric records
- [Capability Framework](./01-capability-framework.md) — the `fabric.*` caps surface