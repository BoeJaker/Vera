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

The projection path creates an offline seam between Fabric projections and
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

`JepaResultProjector` is the pure compatibility seam for current operational
result shapes. It accepts already-produced concept lists, next-concept/rollout
predictions, anomalies, counterfactual paths, drift reports, or latent-query
ranking results. The caller must supply the authoritative record-to-revision
mapping and explicit support records because the legacy JEPA responses do not
carry sufficient revision evidence. Missing citations fail closed.

The projector copies only identifiers, bounded labels, ranks/positions, scores,
concept numbers, and aggregate drift/counterfactual fields. Source/query text,
member text, reconstruction details, embeddings, vectors, prompts, and other
payloads are discarded. It does not call an operational capability, load a
checkpoint, query Fabric, run inference, alter result ordering, or attach the
evidence to a consumer. That last activation step requires the exact-identity
availability check plus separately measured quality and latency evidence.

`vera.worldview.reranking_shadow` provides the intervening, non-authoritative
comparison seam. It accepts only a reranking envelope whose dataset snapshot and
ModelPackage identities match exactly. Every evidence score must cite one
existing context candidate at its exact source-record revision; unknown,
ambiguous, duplicated, or mismatched identities fail closed. The resulting
payload-free report shows baseline and hypothetical ranks, but it does not
mutate candidates, register a ranker, invoke JEPA, or change context selection.
Stale and unavailable evidence is explicitly ineligible. Activation remains
blocked on the separate cited quality and latency evaluation.

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

The lineages can nevertheless exchange data through the same controlled
boundary. Non-JEPA Worldview and Godseye datasets may be normalized into
canonical Fabric records, explicit record revisions, and an immutable
`DatasetSnapshot`. JEPA Worldview may then consume that snapshot as training or
retrieval evidence while retaining its own model-package provenance. This
allows their datasets to complement the JEPA implementation without coupling
JEPA to Godseye storage, UI state, or product-specific schemas.

`vera.godseye.portable_dataset` implements that offline boundary for the
normalized CCTV, imagery, and building records. It sorts records by stable ID,
binds every record to the caller-supplied source revision, and emits both an
immutable `DatasetSnapshot` and a content-addressed artifact identity. Duplicate
IDs, invalid coordinates, malformed geometry, ambiguous revisions, and
oversized collections fail closed. The adapter performs no fetch, database
read, UI inspection, or inference; its provenance explicitly states that it is
not JEPA authority.

Context's current optional `worldview.query` and `worldview.rollout` lookups
refer specifically to the JEPA Worldview capability surface. Their historical
names do not make non-JEPA Worldview or Godseye implementations of JEPA, and
those other lineages must not be substituted behind the names implicitly.

Offline retrieval comparisons name JEPA explicitly as
`jepa_worldview_evidence` and measure it against other providers on identical
snapshot and citation fixtures. They report quality, latency, failures, storage,
and lifecycle costs separately. Comparison evidence cannot activate JEPA as a
ranker or imply that the non-JEPA/Godseye product is a JEPA implementation.

### Provenance-qualified retrieval

The historical `worldview.query` remains the general interactive latent search.
Its result IDs alone are not sufficient for a provider comparison: an index can
outlive or drift from the checkpoint and it does not identify canonical record
revisions.

`worldview.retrieval.bind` establishes the stricter comparison boundary. It
accepts a complete record manifest that must reproduce one immutable
`DatasetSnapshot`; every record must carry a unique `record_id` and
`revision_id`, and the manifest membership must exactly equal the active JEPA
index. Vera serialises the active checkpoint, content-identifies it as a
`ModelPackage`, and persists the checkpoint and binding together. Partial
indexes, changed records, duplicate identities, and legacy checkpoints without
this binding fail closed.

`worldview.retrieval.status` rechecks the current checkpoint bytes and complete
index membership against the persisted binding. `worldview.retrieval.query`
runs only while that check succeeds and requires the requested snapshot ID. Its
response contains a query digest, revision-qualified citations, and the exact
snapshot/package/provider receipt; it omits query text and member text. A model
update, streaming index change, checkpoint swap, or snapshot mismatch makes the
path unavailable until an explicit new binding is created.

`JepaWorldviewRetrievalAdapter` verifies that live receipt again before handing
citations to the provider-neutral comparison executor. The adapter reports
unavailable or failed evidence on identity drift and cannot choose a winner or
activate JEPA. Existing local checkpoints remain usable through the historical
JEPA UI and capabilities, but they are not silently upgraded into comparison
evidence.

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
| `worldview.retrieval.bind` | Bind a checkpoint and complete JEPA index to an immutable snapshot and revision manifest |
| `worldview.retrieval.status` | Verify that the active runtime still matches the binding |
| `worldview.retrieval.query` | Return snapshot/package-pinned, revision-qualified retrieval evidence |
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
- `vera/worldview/reranking_shadow.py` — exact-identity context comparison only.
- `vera/worldview/retrieval_provenance.py` — immutable snapshot/checkpoint/index binding.
- `vera/worldview/retrieval_adapter.py` — live receipt validation for retrieval comparison.
- `vera/godseye/portable_dataset.py` — portable non-JEPA Worldview/Godseye datasets.

## Related guides

[Data Fabric](06-data-fabric.md) · [Memory graph](05-memory-graph.md) ·
[Machine learning](16-machine-learning.md) · [ONNX](30-onnx.md) ·
[Interoperability foundations](46-interoperability-foundations.md)

## Documentation capture

The documentation recipe targets the dedicated **JEPA Worldview** panel, not
the separate Worldview Intelligence Platform or Godseye interfaces. It waits
for the latent-map canvas, initialized view description, and a positive
rendered-item count. If the optional JEPA runtime, model, or capability family
is unavailable, capture fails explicitly instead of publishing its empty shell.

<!-- VERA:AUTO:screenshots START -->
_No populated JEPA Worldview screenshot is currently available._
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
