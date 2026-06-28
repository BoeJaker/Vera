# ONNX / ONNX Runtime — Adoption TODO

Working roadmap for bringing ONNX Runtime (ORT) deeper into Vera. ONNX is
already in use today via `kokoro-onnx` TTS in
[edge/GPU_inference.py](edge/GPU_inference.py#L266-L289) — these items extend
that pattern (export `.onnx` → serve via ORT) to other subsystems.

Why ORT: framework-neutral model format + a fast, low-footprint inference engine
with selectable **execution providers** (CPU, CUDA, TensorRT, **DirectML** on the
Windows host, OpenVINO). Quantizable to int8, no heavyweight training stack
needed at inference time.

Priority order below reflects interest, not difficulty. **Item 3 is the lead spike.**

---

## ⭐ 3. ML Workshop → ONNX export → callable caps  (LEAD) — ✅ DONE

**Status:** implemented in [vera/machine learning/ml_onnx.py](vera/machine%20learning/ml_onnx.py),
registered after `ml_training` in the loader. Parity verified offline on the
edge node: dense + gelu + layer_norm + softmax reproduced with **max abs diff
1.3e-7 (float32) / 4.9e-10 (float64)** vs `_forward_with_weights`; unsupported
node types (rnn/attention/conv/embedding) are refused, not mis-exported.

**Goal:** turn a trained Vera ML module into a portable `.onnx` artifact served
by ONNX Runtime, exposed as a first-class capability — so inference no longer
needs NumPy/PyTorch/JAX training infrastructure present.

**Today:**
- [ml_workshop.py](vera/machine%20learning/ml_workshop.py) — modules are
  JSON compute graphs (Dense/Conv2D/RNN/LSTM/Attention/...), executed on
  NumPy with optional PyTorch/JAX. Called via `ml.run(<module_id>, inputs)`.
- [ml_training.py](vera/machine%20learning/ml_training.py) — trains them;
  weights live in Redis (`ml.train.weights_get` / `ml.train.weights_load`),
  inference via `ml.train.predict`.

**Can ONNX export create caps from Vera's ML systems? — Yes.** Design:

1. **`ml.export.onnx`** — new cap. Input: `module_id` (+ optional weights/run
   id). Produces an `.onnx` file from the module graph:
   - PyTorch-backed modules → `torch.onnx.export` (dynamo or TorchScript path).
   - Pure-NumPy graphs → emit ONNX nodes directly via the `onnx` helper API
     (map each layer/op type in the workshop graph to its ONNX operator), or
     route through a thin torch shim built from the same graph. Start with the
     PyTorch path; NumPy-native emit is the harder follow-up.
   - Store the artifact (fabric/Postgres blob or `edge/models/`), keyed by
     `module_id` + weights hash. Validate with `onnx.checker`.
2. **`ml.onnx.run`** — generic cap. Input: artifact id + input tensor(s);
   loads an `onnxruntime.InferenceSession` (cached per artifact), runs, returns
   outputs. Pick EP by node: `CUDAExecutionProvider` on the GPU node,
   `CPUExecutionProvider`/`DMLExecutionProvider` on CPU/Windows.
3. **Auto-registered model caps** — on export, register a dynamic cap
   `ml.onnx.<module_slug>` that wraps `ml.onnx.run` with the artifact bound, so
   a trained model becomes a named capability the DAG/agents can call directly.
4. **Parity check** — `ml.onnx.verify`: run the same inputs through `ml.run`
   (native) and `ml.onnx.run`, assert outputs match within tolerance.

**Files:** `vera/machine learning/ml_workshop.py`,
`vera/machine learning/ml_training.py`, panel hooks in
`ml_workshop_panel.html` / `ml_lab_panel.html` (an "Export ONNX" button +
artifact list). Follows the @capability decorator pattern in
[01-capability-framework.md](documentation/01-capability-framework.md).

**Deps:** `onnx`, `onnxruntime` (+ `onnxruntime-gpu` on the GPU node),
`torch` already optional-present.

**Acceptance:**
- [x] Train a workshop module, `ml.export.onnx`, get a validated `.onnx`.
- [x] `ml.onnx.run` reproduces the reference forward within tolerance (`ml.onnx.verify` passes).
- [x] Exported model callable as an auto-registered `ml.onnx.model.<slug>` cap with no torch loaded.
- [x] Panel: export button + artifact browser — ⬇ ONNX toolbar button + an **ONNX**
      right-tab in [ml_workshop_panel.html](vera/machine%20learning/ml_workshop_panel.html)
      (export / list / verify / delete).

---

## 4. Edge inference — ORT as the edge runtime — ✅ DONE

**Status:** implemented in [edge/onnx_runtime.py](edge/onnx_runtime.py) and
verified on the edge node — EP auto-selection (CUDA→DirectML→CPU), cached
sessions, `edge/models/` registry (shares `ML_ONNX_DIR` with the exporter),
int8 dynamic quantization, benchmark, and an optional FastAPI server
(`/health`, `/models`, `/run/{slug}`, `/quantize/{slug}`) + CLI. fp32 parity vs
the reference forward = 8e-8; int8 round-trips correctly. Cluster advertising
now lands in `obs.cluster` (`/cluster`): shared `edge/models/` artifacts always,
plus per-node provider/model counts when `ONNX_RUNTIME_URLS` is set.

**Goal:** make `edge/` a home for small, quantized ONNX models that run on the
CPU nodes (and the Windows host via DirectML), not just the GPU box.

**Notes:**
- ORT = small binary, CPU-capable, int8 quantization → fits the two CPU nodes
  in the reference cluster ([04-ollama-cluster.md](documentation/04-ollama-cluster.md)).
- **DirectML EP** is relevant because the host is Windows 10 — lets non-CUDA
  hardware accelerate ORT.
- Natural consumer of the §3 artifacts and the §1/§5 models.

**Tasks:**
- [x] Add an ORT serving entrypoint under `edge/` (sibling to
      [GPU_inference.py](edge/GPU_inference.py)) with EP auto-selection
      (CUDA → DML → CPU) and a model registry/cache.
- [x] int8 dynamic-quantization helper (`onnxruntime.quantization`) + a
      size/latency benchmark vs the fp32 model.
- [x] `edge/models/` layout + manifest (shared `ML_ONNX_DIR`). *(cluster
      advertise of hosted ORT models still TODO)*
- [x] Health/capabilities endpoint reporting active EP + loaded models.

**Deps:** `onnxruntime` (CPU), `onnxruntime-directml` (Windows host).

---

## 5. NLP rerankers / classifiers on ORT CPU — ✅ mostly DONE

**Status:** `nlp.rerank` (+ `nlp.models`) implemented in
[vera/research/nlp_capabilities.py](vera/research/nlp_capabilities.py), backed by
fastembed's ONNX cross-encoder on ORT CPU, registered in the loader. Additive &
opt-in. **Wired into research retrieval** ([researcher_api.py](vera/research/researcher_api.py))
behind `VERA_RERANK_ENABLED` (default off): the merged citation pool is
re-ranked by query relevance before context assembly, falling back to source
order on any failure. **Classifier caps** `nlp.classify` (sentiment) + `nlp.ner`
added (optimum + transformers ORT pipelines, guarded optional).

**Goal:** serve small NLP models (cross-encoder rerankers, sentiment, NER,
classification) on ONNX Runtime CPU — no heavyweight serving process.

**Today:** research/NLP path (`vera/research/nlp_panel.html`,
`researcher_api.py`) — confirm current reranking/classification approach before
building.

**Tasks:**
- [ ] Add a cross-encoder reranker cap (e.g. `nlp.rerank`) backed by an ONNX
      cross-encoder via ORT CPU; wire into research/vector retrieval.
- [ ] Optional sentiment/NER classifier caps from ONNX models.
- [ ] Batch + cache sessions; run on CPU nodes to keep the GPU free.

**Deps:** `onnxruntime`, `transformers`/`optimum` for export, or pre-exported
ONNX models from the hub.

---

## 1. Embeddings via fastembed (ONNX Runtime CPU) — ✅ DONE (opt-in)

**Status:** implemented as a back-compat, opt-in provider.
[vera/fabric/fastembed_provider.py](vera/fabric/fastembed_provider.py) wraps
fastembed; `ollama_embed()` in
[capability_orchestration.py](vera/capability_orchestration.py#L772) takes the
fastembed fast-path **only** when `cfg.EMBED_PROVIDER == "fastembed"`
(`VERA_EMBED_PROVIDER` env), and falls through to Ollama on any failure. Default
is unchanged (`ollama`). Because every embed call funnels through `ollama_embed`,
one switch flips the whole fabric/memory/DAG hot path. `fastembed` is an optional
dep (commented in requirements). Couldn't be runtime-exercised from the edge
shell (fastembed not installed, embed path runs on the server).

**Goal:** serve `nomic-embed-text` embeddings on ORT CPU instead of Ollama,
removing Ollama from the embedding hot path the data fabric + memory system hit.

**Today:** `llm.embed` → `nomic-embed-text` via `OLLAMA_EMBED_URL` on a CPU node
([04-ollama-cluster.md:187-189](documentation/04-ollama-cluster.md#L187-L189)).

**Tasks:**
- [x] Add a `fastembed`-backed embedding provider, selectable via
      `VERA_EMBED_PROVIDER` at the `ollama_embed()` choke-point (Ollama fallback).
- [x] Benchmark throughput vs the Ollama path — `embed.provider.benchmark`.
- [x] Confirm vector dimensionality matches existing stored embeddings — migration
      guard `embed.provider.check` ([embed_provider_capabilities.py](vera/fabric/embed_provider_capabilities.py))
      reports dim match + cosine + re-index guidance.
- [x] Re-index path — `memory.reindex_embeddings` ([memory.py](vera/fabric/memory.py))
      re-embeds the Chroma store with the current provider (dry-run by default,
      `confirm=true` to write). Data-fabric stores can follow the same pattern.

**Deps:** `fastembed` (bundles `onnxruntime`).

**Risk:** embedding-space drift vs already-indexed vectors — verify the ONNX
model produces compatible embeddings before switching the default.

---

## Cross-cutting

- [x] Pin `onnx` / `onnxruntime` versions in `requirements.txt`; add
      `onnxruntime-gpu` (GPU node) and `onnxruntime-directml` (Windows host) as
      environment-specific extras (commented).
- [x] Standardize an artifact store + naming — `ML_ONNX_DIR` (default
      `edge/models/`), `<slug>.onnx` + `<slug>.json` manifest, shared by §3/§4.
- [x] Document an "ONNX" section — [documentation/30-onnx.md](documentation/30-onnx.md).
