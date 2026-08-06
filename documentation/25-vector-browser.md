# 25 · Vector Browser

> **Doc status:** concise reference for `vector browser/`. Expand as the surface grows.

`vector browser/vector_browser_capabilites.py` is an inspection, audit, and browsing tool for the two vector stores that back the [Data Fabric](./06-data-fabric.md) — **Chroma** and **FAISS**. It's registered under the `fabric.vectors.*` group and served at its own panel (`/fabric/vectors/panel`).

This is the diagnostic counterpart to the fabric: where the fabric *uses* vectors for recall, this module lets you *see inside* them — confirm dimensions line up, browse raw embeddings, and compare how a record is represented across both stores.

| Cap | Purpose |
|---|---|
| `fabric.vectors.overview` | Combined stats for both stores + dimension alignment |
| `fabric.vectors.chroma.browse` | Paginated Chroma document listing with embeddings |
| `fabric.vectors.chroma.get` | Single-record detail from Chroma |
| `fabric.vectors.chroma.datasets` | Per-dataset vector counts from Chroma |
| `fabric.vectors.faiss.shards` | FAISS shard/dataset index stats |
| `fabric.vectors.faiss.sample` | Sample vectors from a FAISS shard or dataset |
| `fabric.vectors.audit` | Full dimension-alignment audit across both stores |
| `fabric.vectors.compare` | Compare a record's vectors across Chroma vs FAISS |

The **dimension-alignment audit** is the practical payoff: embedding-model changes or mixed-dim ingests are the classic cause of silent recall failures, and `fabric.vectors.audit` surfaces them. The module reads the fabric's live store handles (`FABRIC_CHROMA`, `FAISS_STORE`, `FABRIC_VECTOR_DIM`, `OLLAMA_EMBED_MODEL`) directly.

---

## See also

- [Data Fabric](./06-data-fabric.md) — the stores this module inspects; ingestion + recall
- [LLM Cluster §7](./04-ollama-cluster.md) — the embedding endpoint whose dimension must stay consistent
- [Memory Graph](./05-memory-graph.md) — the other half of recall

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
