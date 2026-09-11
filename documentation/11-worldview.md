# 11 · Worldview: Graph-Native JEPA World Model

Worldview is Vera's experimental world-model subsystem. It learns graph-aware
representations of Fabric records, groups them into interpretable concepts, and
models how concepts evolve across temporal, graph, and entity walks. It supports
prediction, counterfactual exploration, anomaly detection, and visualization.

Worldview is optional. Missing Torch, FAISS, weights, or backend services
degrade its capabilities rather than preventing Vera from starting.

## Naming and product boundary

This guide describes **JEPA Worldview**, the predictive representation system in
`vera/worldview/worldview_jepa.py`. Vera also contains an older non-JEPA
Worldview product lineage. Godseye is the integrated successor intended to
converge with that non-JEPA experience; it does not supersede JEPA Worldview.
Keeping those lineages explicit prevents model evidence, geospatial/visual
product state, and UI ownership from being treated as interchangeable.

## Architecture

| Layer | Responsibility | Boundary |
|---|---|---|
| Graph builder | Reads records, Loom relations, entities, and embeddings | Fabric revisions remain authoritative |
| Graph encoder | Multi-relational GraphSAGE-style message passing | Pure Torch; no `torch_geometric` requirement |
| Concept book | EMA-updated vector-quantised codebook | Concepts are inspectable model state |
| Dynamics model | Causal transformer over concept walks | Predictions are not authoritative facts |
| Latent index | Nearest-neighbour search | FAISS is optional and rebuildable |
| Projection adapter | Freezes graph/vector lineage | Non-executing and payload-free |
| Shadow parity | Compares legacy snapshots with projections | Evidence only; cannot change training |

The encoder combines a record embedding with neighbours reached through Loom
relations and shared entities. The codebook quantises that latent vector. The
dynamics model learns next-concept distributions from records ordered by time,
biased graph walks, and sequences that share an entity.

## Training and inference

The three model stages can train together or separately. Inference can encode a
record, predict or roll out concepts, replace a concept for a counterfactual,
query latent neighbours, score anomalies, and expose concept populations and
transitions. These are model observations: they do not overwrite Fabric,
authorize tools, or prove causality.

## Projection-backed migration boundary

The W2 projection path creates an offline seam between Fabric projections and
JEPA training. A frozen manifest pins graph/vector specification IDs and
generations, the embedding package/dimension/preprocessing/metric, exact active
record/revision pairs, tombstone count, and hashes of both snapshots.

Graph and vector inputs must agree on coverage, revision, content hash, and
tombstone state. Duplicates, forged identities, drift, mixed dimensions,
cancellation, malformed edges, and oversized snapshots fail closed. The
manifest contains no source content, embeddings, graph payload, or result.

This path is not wired into `worldview.train`: it performs no backend read or
model work. The existing loader/trainer remains authoritative until live shadow
evidence supports a deliberate migration.

## Portable JEPA evidence

`vera.worldview.evidence_provider` defines an offline contract for the six JEPA
signal families: concepts, predictions, anomalies, counterfactuals, drift, and
reranking. Every evidence envelope binds an exact immutable `DatasetSnapshot`, a
compatible JEPA Worldview `ModelPackage` checkpoint, a provider revision, a
zoned observation time, and cited record revisions. Its identity changes when
any authority input or observation changes.

The contract carries bounded scores and non-payload attributes. It rejects raw
text, prompts, vectors, embeddings, payloads, credential-like fields, non-finite
scores, duplicate observation identities, uncited observations, incompatible
checkpoints, and evidence predating its input snapshot. Counterfactuals and
predictions remain derived evidence—not causal facts or execution authority.

`FrozenEvidenceProvider` is a deterministic conformance/reference store. It can
filter exact evidence identities but cannot load Torch, inspect Fabric, generate
a signal, rank context, fall back to a stale revision, or activate a checkpoint.
Exact snapshot and ModelPackage matching is required before evidence is marked
usable; missing evidence is unavailable and mismatched evidence is stale.

## Snapshot, parity, and evidence

The legacy loader retains backend-supplied revision/hash evidence. Missing or
malformed provenance stays visibly incomplete rather than being inferred.
`worldview_shadow_snapshot` copies only record identity, vectors,
revision/hash evidence, and edge tuples; source text and unrelated metadata are
discarded. Shape, duplicate, endpoint, size, and cancellation checks are
fail-closed.

The parity comparator reports coverage, dimension/finiteness, provenance, and
edge integrity using bounded IDs, relation types, counts, failure classes, and
checksums—never source or vector payloads. `worldview_shadow_evidence` retains a
bounded in-memory window of those reports and resets consecutive readiness when
a new failure appears. Persistence and automatic live collection are not yet
implemented.

## Integration rules

Worldview can inform resolver, workflow, memory, and remote-agent features only
through provenance-pinned projections:

- capability descriptions and Agent Cards are untrusted metadata, not facts;
- resolver/policy outcomes may become observations, never declarations;
- Run events need stable identity, redaction, and retention before projection;
- Fabric revisions outrank derived graph/vector indexes; and
- parity readiness is evidence for review, not automatic migration approval.

This prevents prompts and tool telemetry becoming an uncontrolled training
feedback loop.

## Operational checks

Before training, verify optional dependencies/device, bounded Fabric inputs,
embedding package and dimension, revision/tombstone agreement, and checkpoint /
index generation. Never coerce malformed vectors, invent provenance, or drop
dangling edges merely to obtain a green report.

## Primary capabilities

| Capability | Purpose |
|---|---|
| `worldview.train` / `worldview.train_stage` | Train all or one model stage |
| `worldview.encode` | Encode text/record into latent and concept |
| `worldview.predict` / `worldview.rollout` | Predict concept transitions |
| `worldview.counterfactual` | Roll out after a concept swap |
| `worldview.query` | Query latent neighbours |
| `worldview.anomalies` | Produce anomaly evidence |
| `worldview.snapshot` | Produce a visualization projection |
| `worldview.concepts` | Inspect concept labels and populations |
| `worldview.concept_neighbors` | Inspect transition evidence |
| `worldview.concept_members` | Inspect assigned records |
| `worldview.explain_record` | Combine concept, neighbours, and trajectory |

## Source map

- `vera/worldview/worldview_jepa.py` — operational model and capabilities.
- `vera/worldview/worldview_projection_adapter.py` — frozen manifest.
- `vera/worldview/worldview_shadow_snapshot.py` — bounded legacy snapshot.
- `vera/worldview/worldview_shadow_parity.py` — parity report.
- `vera/worldview/worldview_shadow_evidence.py` — evidence window.

## Related guides

[Data Fabric](06-data-fabric.md) · [Memory graph](05-memory-graph.md) ·
[Machine learning](16-machine-learning.md) · [ONNX](30-onnx.md) ·
[Interoperability foundations](46-interoperability-foundations.md)

## Documentation capture

WorldView is registered as an injected Data Fabric section, while its full UI is
served by a dedicated same-origin panel route. Documentation capture uses that
route directly and waits for the latent-map canvas and initialized view
description. This avoids photographing the otherwise empty injection wrapper.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
