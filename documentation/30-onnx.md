# 30 · ONNX Export & Runtime

## Portable ModelPackage boundary

W2-06 begins with `vera.models.model_package`, an offline provider-neutral
identity contract. A package pins architecture and format; role-addressed
artifact URIs with SHA-256 and size; tokenizer and preprocessing; framework and
optional opset; source, licence, signature, training/evaluation lineage;
hardware requirements; and typed task/input/output compatibility. Canonical
ordering produces a stable `mpkg_…` identity.

The first registry is deliberately in-memory and non-executing. Registration is
immutable and idempotent, aliases use compare-and-set semantics, and registering
a URI never opens, moves, deletes, verifies, or activates its file. Durable
storage, read-only verification, safe ONNX import, admission policy, legacy
capability bindings, and audited activation and rollback now build on that
boundary. Inference parity remains a separate adapter concern. No model or
external runtime tests are enabled by these contracts.

`vera.models.inference_contracts` adds the portable execution seam without
redirecting an existing model call. Every request names an immutable
ModelPackage, task, input/output contracts, canonical bounded inputs and scalar
parameters, streaming intent, and an output-byte ceiling. Large or binary values
travel as URI, SHA-256, size and media-type artifact references rather than
unbounded inline objects. Request identity is derived from the complete canonical
request.

An `InferenceProvider` emits a contiguous asynchronous event stream tied to the
request, package and provider identities. The shared consumer validates provider
task claims, event order, exactly one terminal outcome, output budgets, bounded
usage counters, and cancellation. It does not retry, interpret model payloads,
load artifacts, or choose a provider. Strict reconstruction verifies serialized
request identity, giving ONNX Runtime, vLLM and cluster adapters one comparable
contract before any traffic is moved.

The second W2-06 slice adds `SQLiteModelPackageRegistry`. Canonical package JSON
and aliases survive restart in transactional tables; package content is
revalidated and its identity recomputed on every read, so malformed or forged
stored state fails visibly. Alias changes are compare-and-set operations.

`verify_local_artifact` separately produces a read-only receipt for an explicit
local path or local `file:` URI. It checks the size ceiling, expected size and
streamed SHA-256 with cancellation, and reports missing, unsupported, oversized,
size-mismatched, hash-mismatched or verified state. Verification never imports,
moves, deletes, activates, or executes the artifact. Signature trust policy and
durable receipt history remain later work.

`SQLiteModelPackageRegistry.activate` is the audited control-plane operation for
changing a serving alias. It requires the expected current package and a
caller-supplied idempotency ID, then moves the alias and appends a
`ModelActivationReceipt` in one immediate SQLite transaction. Competing stale
requests fail without a partial history entry. `rollback` names the exact
activation receipt to reverse and succeeds only while the alias still points to
that activation's target; retrying the same rollback is idempotent, while a
second or stale rollback is refused. Reopen preserves ordered history.

Activation here changes registry identity only. It does not load a model,
inspect artifact files, create an ONNX Runtime session, or claim inference
parity. The older low-level `alias` method remains for compatibility but does
not produce activation history and is not the audited deployment path.

Legacy ONNX invocation identities can also be attached to package identity
without changing how they execute. `legacy_onnx_bindings` describes both the
selector-based `ml.onnx.run#<slug>` identity and its dynamic
`ml.onnx.model.<slug>` capability. `bind_legacy_capabilities` verifies that the
target package is registered and writes the pair transactionally with
compare-and-set protection; conflicts cannot leave a half-migrated pair.
Bindings retain their manifest-source provenance, survive reopen, and are
queryable independently of execution.

This is a discovery and migration bridge, not runtime delegation: the legacy
capabilities do not yet consult the binding, and no inference call is redirected
by this slice. That traffic step remains gated on identical legacy/package
inference evidence.

Before an audited alias activation, `evaluate_model_admission` compares the
package against an explicit `ModelDeploymentTarget` and `ModelTrustPolicy`. It
classifies task and input/output contract mismatches, insufficient declared
memory, missing accelerators, unsupported framework/opset, and missing or
untrusted signature identifiers. The stable receipt contains package, policy,
target and reason identities, not model data.

`activate_admitted` recomputes this receipt from the immutable stored package;
it does not accept a caller's assertion that admission passed. An accepted
receipt and alias activation commit in one transaction and remain queryable by
operation or in activation order. Rejected or corrupt evidence leaves no alias
or activation entry. This is a declarative admission gate: target facts are not
hardware probes, and trusted signature identifiers are policy input rather than
cryptographic signature verification. Those live/trust integrations and
inference parity remain separately queued gates.

Vera can turn a **trained ML Workshop module** into a portable `.onnx` artifact
and serve it through ONNX Runtime (ORT) — as a first-class capability and on the
edge nodes — so inference no longer needs the NumPy/PyTorch training stack
present. The same machinery also offers an opt-in ONNX-Runtime embedding backend
and a cross-encoder reranker.

Everything here is **additive and optional**: `onnx`, `onnxruntime`, and
`fastembed` are guarded imports. If they are not installed, the relevant caps
report it and the rest of Vera is unaffected.

---

## 1. Why ONNX

ONNX Runtime is the standard choice for **fast, portable, low-footprint
inference decoupled from the training framework**:

- One `.onnx` file runs anywhere via selectable **execution providers** —
  `CUDAExecutionProvider` (GPU node), `DmlExecutionProvider` (Windows host /
  DirectML), `CPUExecutionProvider` (the CPU nodes).
- int8 quantizable; far lighter than carrying a full PyTorch install.
- Already present in the stack (`kokoro-onnx` TTS in `edge/GPU_inference.py`).

---

## 2. Export: ML Workshop module → `.onnx`

The exporter lives in [`vera/machine learning/ml_onnx.py`](../vera/machine%20learning/ml_onnx.py)
(loaded after `ml_training` in `_module_files`).

It reproduces the **trained** reference forward — `ml_training._forward_with_weights(module, weights, X)`
— *not* the `ml.run` demo path (which uses throwaway seed-42 weights). Weights
are resolved exactly as `ml.train.predict` does: in-memory `_WEIGHTS` →
`_load_weights` (Redis/fabric) → fresh `_init_weights`.

### Supported node types

The exporter faithfully emits the feed-forward subset and **refuses** anything
else rather than shipping a wrong model:

| Supported | Refused (for now) |
|---|---|
| input, dense, linear_probe, mlp, activation, layer_norm, rms_norm, dropout, add, residual, concat, output | rnn, gru, lstm, multi_head_attention, transformer_block, conv1d/2d, embedding, pool, reshape, kan_layer, … |

Activations (relu, gelu, sigmoid, tanh, swish/silu, softmax, step) are emitted
as primitive ops so the result matches the NumPy reference within dtype error.
Refused modules return `{"unsupported": [...]}` and write nothing.

---

## 3. Capabilities

| Capability | Method / path | Purpose |
|---|---|---|
| `ml.export.onnx` | POST `/ml/export/onnx` | Export `module_id` → validated artifact. Params: `dtype` (float32\|float64), `register_cap` (bool). |
| `ml.onnx.run` | POST `/ml/onnx/run` | Run inference. Params: `artifact`, `X` (JSON list). |
| `ml.onnx.verify` | POST `/ml/onnx/verify` | Numeric parity vs the reference forward. Params: `module_id`, optional `X`, `n`, `tol`. |
| `ml.onnx.list` | GET `/ml/onnx/list` | List artifacts + provider availability. |
| `ml.onnx.delete` | POST `/ml/onnx/delete` | Delete an artifact and its cap. |
| `ml.onnx.model.<slug>` | POST `/ml/onnx/model/<slug>` | Auto-registered per-artifact runner. |

Each exported model is promoted to its own `ml.onnx.model.<slug>` cap (MCP / DAG
/ HTTP callable) with no torch loaded. These are re-registered from disk at
startup so they survive restarts.

### Verified parity

Offline against `dense → gelu → layer_norm → dense+softmax`:

| dtype | max abs diff vs reference |
|---|---|
| float32 | ~1.3e-7 |
| float64 | ~4.9e-10 |

---

## 4. Artifacts

Artifacts are written to `ML_ONNX_DIR` (default `<repo>/edge/models/`, on the
network share so the **server exports** and **any edge node serves**):

```
edge/models/<slug>.onnx        # the model        (slug = sanitised module_id)
edge/models/<slug>.json        # manifest: dtype, opset, node_types, cap_name, created
edge/models/<slug>.int8.onnx   # optional int8-quantized copy (§6)
```

`ML_ONNX_OPSET` (default 17) and `ML_ONNX_IR_VERSION` (default 10 — older ORT
builds reject the IR version newer `onnx` stamps) are env-overridable.

---

## 5. UI

The ML Workshop panel ([`ml_workshop_panel.html`](../vera/machine%20learning/ml_workshop_panel.html))
has a **⬇ ONNX** toolbar button and an **ONNX** right-tab: export the current
module, browse artifacts, and one-click **Verify** (shows `max|Δ|` + pass/fail +
provider) or **Delete**.

---

## 6. Edge runtime

[`edge/onnx_runtime.py`](../edge/onnx_runtime.py) is a small, orchestrator-free
ORT model server for the edge/CPU nodes. It shares `edge/models/` with the
exporter.

- **Provider selection** — CUDA → DirectML → CPU, whichever is available.
- **int8 dynamic quantization** — `quantize_model(slug)` → `<slug>.int8.onnx`
  (no calibration data needed; ~4× smaller, faster CPU inference).
- **HTTP** (optional) — `GET /health`, `GET /models`, `POST /run/{slug}`,
  `POST /quantize/{slug}`.
- **CLI** — `serve`, `list`, `run`, `quantize`, `bench`.

```bash
python edge/onnx_runtime.py serve --port 8770
python edge/onnx_runtime.py quantize <slug>
python edge/onnx_runtime.py bench    <slug> --n 1000
```

**Cluster advertising:** the `/cluster` view (`obs.cluster`) reports the shared
`edge/models/` artifacts, and — when `ONNX_RUNTIME_URLS` lists running edge ORT
servers — each node's selected provider + hosted-model count (via their
`/health`).

---

## 7. ONNX-Runtime embeddings (opt-in)

`ollama_embed()` — the single choke-point every embed funnels through — can use
a local fastembed (ORT CPU) backend instead of Ollama. It is **off by default**;
set `VERA_EMBED_PROVIDER=fastembed` (`cfg.EMBED_PROVIDER`). On any failure it
falls through to Ollama, so it is fully back-compatible.

- Provider wrapper: [`vera/fabric/fastembed_provider.py`](../vera/fabric/fastembed_provider.py)
  (`VERA_FASTEMBED_MODEL`, default `nomic-ai/nomic-embed-text-v1.5`).
- ⚠️ **Vector-space caveat:** the fastembed model is 768-dim like Ollama's
  `nomic-embed-text` but the values differ — **re-index** before enabling on a
  populated vector store.

---

## 8. NLP reranker (opt-in)

[`vera/research/nlp_capabilities.py`](../vera/research/nlp_capabilities.py)
exposes `nlp.rerank` — a cross-encoder reranker on ORT CPU via fastembed
(`VERA_RERANK_MODEL`, default `Xenova/ms-marco-MiniLM-L-6-v2`):

```
POST /nlp/rerank    {"query": "...", "documents": ["...", "..."], "top_k": 5}
→ {"ranked": [{"index", "score", "text"}, ...]}
GET  /nlp/models    → rerank / classify / ner availability + providers
```

It also exposes ONNX **classifiers** via optimum + transformers (ORT CPU,
guarded optional — `VERA_SENTIMENT_MODEL`, `VERA_NER_MODEL`):

```
POST /nlp/classify  {"text": "..."}  → {top, labels:[{label, score}]}
POST /nlp/ner       {"text": "..."}  → {entities:[{entity, word, score, start, end}]}
```

It is wired into research retrieval as an **opt-in** step: set
`VERA_RERANK_ENABLED=1` and the merged citation pool is re-ordered by query
relevance before the context is built (off by default; any failure leaves source
order unchanged).

### Embedding backend migration guard

Before flipping `VERA_EMBED_PROVIDER` to `fastembed` on a populated store, use
[`vera/fabric/embed_provider_capabilities.py`](../vera/fabric/embed_provider_capabilities.py):

```
GET  /embed/provider/info       → active backend + fastembed availability
POST /embed/provider/check      {"text": "..."}  → dim match, cosine, re-index guidance
POST /embed/provider/benchmark  {"n": 20}         → Ollama vs fastembed throughput
```

**Rolling out the switch:** set `VERA_EMBED_PROVIDER=fastembed`, then re-embed the
existing store with `memory.reindex_embeddings` (`POST /memory/reindex_embeddings`)
— dry-run by default; pass `confirm=true` to re-embed the Chroma vectors in place
with the new provider (documents/metadata preserved).

`embed.provider.check` embeds the same text through both backends (forcing each
via the `provider=` override on `ollama_embed`) and reports whether they share a
dimensionality and how different the vectors are — making the re-index decision
explicit.

---

## 9. Portable package import boundary

Vera's provider-neutral model layer can register an existing ONNX artifact by
reference through `inspect_and_register_onnx`. The import boundary requires an
immutable `ModelPackage` with exactly one `model` artifact whose path ends in
`.onnx`. It hashes every declared artifact under an explicit size limit before
writing the package to a registry.

This operation is deliberately inspect-only: it does not import ONNX libraries,
parse a graph, create an inference session, activate an alias, or move, delete,
rewrite, or execute the source file. A failed, cancelled, missing, changed, or
oversized artifact returns a verification-failed receipt and performs no
registry write. Loading, inference parity, activation, and rollback remain
separate gated lifecycle operations.

Implementation: [`vera/models/onnx_import.py`](../vera/models/onnx_import.py).

---

## 10. Dependencies

```
onnx>=1.16            # graph builder for ml.export.onnx
onnxruntime>=1.18     # inference engine (CPU)
# onnxruntime-gpu          — on the CUDA GPU node (instead of onnxruntime)
# onnxruntime-directml     — on the Windows host (DirectML EP)
# fastembed>=0.3           — §7 embeddings + §8 reranker (downloads weights on first use)
```

The `ml.*onnx*` caps need `onnx`/`onnxruntime` installed **on the orchestrator
host**; the edge runtime needs them on the **edge node**.

---

## See also

- [Machine Learning](./16-machine-learning.md) — the ML Workshop / training engine these export from
- [LLM Cluster](./04-ollama-cluster.md) — the embedding hot path (`ollama_embed`) §7 hooks
- [Research System](./07-research.md) — where `nlp.rerank` will plug into retrieval
- [Capability Framework](./01-capability-framework.md) — `@capability` pattern the caps follow
- `ONNX_TODO.md` (repo root) — live status and remaining work

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
