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

## ⭐ 3. ML Workshop → ONNX export → callable caps  (LEAD)

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
- [ ] Train a workshop module, `ml.export.onnx`, get a validated `.onnx`.
- [ ] `ml.onnx.run` reproduces `ml.run` outputs within tolerance (parity check passes).
- [ ] Exported model callable as an auto-registered `ml.onnx.<name>` cap with no torch loaded.
- [ ] Panel: export button + artifact browser.

---

## 4. Edge inference — ORT as the edge runtime

**Goal:** make `edge/` a home for small, quantized ONNX models that run on the
CPU nodes (and the Windows host via DirectML), not just the GPU box.

**Notes:**
- ORT = small binary, CPU-capable, int8 quantization → fits the two CPU nodes
  in the reference cluster ([04-ollama-cluster.md](documentation/04-ollama-cluster.md)).
- **DirectML EP** is relevant because the host is Windows 10 — lets non-CUDA
  hardware accelerate ORT.
- Natural consumer of the §3 artifacts and the §1/§5 models.

**Tasks:**
- [ ] Add an ORT serving entrypoint under `edge/` (sibling to
      [GPU_inference.py](edge/GPU_inference.py)) with EP auto-selection
      (CUDA → DML → CPU) and a model registry/cache.
- [ ] int8 dynamic-quantization helper (`onnxruntime.quantization`) + a
      size/latency benchmark vs the fp32 model.
- [ ] `edge/models/` layout + manifest; wire to the cluster so CPU nodes can
      advertise which ORT models they host.
- [ ] Health/capabilities endpoint reporting active EP + loaded models.

**Deps:** `onnxruntime` (CPU), `onnxruntime-directml` (Windows host).

---

## 5. NLP rerankers / classifiers on ORT CPU

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

## 1. Embeddings via fastembed (ONNX Runtime CPU)

**Goal:** serve `nomic-embed-text` embeddings on ORT CPU instead of Ollama,
removing Ollama from the embedding hot path the data fabric + memory system hit.

**Today:** `llm.embed` → `nomic-embed-text` via `OLLAMA_EMBED_URL` on a CPU node
([04-ollama-cluster.md:187-189](documentation/04-ollama-cluster.md#L187-L189)).

**Tasks:**
- [ ] Add a `fastembed`-backed embedding provider (ONNX `nomic-embed-text`),
      selectable behind the existing `llm.embed` cap (config flag /
      provider switch — keep Ollama as fallback).
- [ ] Benchmark batched throughput + memory vs the Ollama CPU path.
- [ ] Confirm vector dimensionality matches existing stored embeddings (avoid a
      re-index); plan a migration if dims differ.
- [ ] Roll out to data fabric + memory once parity is confirmed.

**Deps:** `fastembed` (bundles `onnxruntime`).

**Risk:** embedding-space drift vs already-indexed vectors — verify the ONNX
model produces compatible embeddings before switching the default.

---

## Cross-cutting

- [ ] Pin `onnx` / `onnxruntime` versions in `requirements.txt`; add
      `onnxruntime-gpu` (GPU node) and `onnxruntime-directml` (Windows host) as
      environment-specific extras.
- [ ] Standardize an artifact store + naming (`module_id` + weights hash) shared
      by §3/§4.
- [ ] Document an "ONNX" section (new `documentation/30-onnx.md` or fold into
      [16-machine-learning.md](documentation/16-machine-learning.md)).
