# 21 · vLLM Backend

`vllm/vllm_capabilities.py` integrates **vLLM** as an LLM backend, mirroring the Ollama backend pattern but targeting vLLM's OpenAI-compatible server. It's how Vera's [backend-agnostic cluster](./04-ollama-cluster.md) gains a high-throughput inference option for the GPU node — without any cap having to know it's talking to vLLM rather than Ollama.

> **Opt-in.** This module ships commented out in the orchestrator's `_module_files` list. Enable it by uncommenting that line or adding `vllm/vllm_capabilities.py` to the `VERA_MODULES` env var (see [Capability Framework §11](./01-capability-framework.md#11-module-loading)).

---

## 1. Why vLLM

vLLM unlocks features that matter on a home lab GPU:

| Feature | Benefit |
|---|---|
| **PagedAttention** | KV-cache allocator that eliminates fragmentation; enables concurrent requests with far better GPU utilisation |
| **Continuous batching** | Requests batch at the *iteration* level; new prompts join in-flight batches as tokens free up |
| **Speculative decoding** | A small draft model runs ahead; the target verifies multiple tokens/step, cutting latency (`VLLM_SPEC_MODEL`) |
| **Prefix caching (APC)** | Repeated prompt prefixes (system prompts, few-shot) share KV blocks → much lower TTFT for templates |
| **Chunked prefill** | Long prompts are chunked so prefill doesn't starve decode; flat latency on large contexts |
| **Quantisation** | GPTQ / AWQ / FP8 / GGUF checkpoints via `--quantization` (`VLLM_QUANTIZATION`) |
| **Tensor parallelism** | Shard across multiple GPUs (`VLLM_TENSOR_PARALLEL_SIZE`) |
| **CPU offload** | Spill KV cache to host RAM — useful on 12 GB GPUs with big host RAM |
| **LoRA hot-swap** | Load multiple adapters at runtime, select per-request |
| **Logprobs / guided decoding** | Full logprobs + JSON-schema / regex / grammar-guided output |
| **Embedding mode** | Embedding endpoints alongside generation |

---

## 2. Architecture

Mirrors the Ollama cluster layer so the router treats both uniformly:

| Piece | Role |
|---|---|
| `VLLMInstance` | Dataclass tracking one vLLM server endpoint |
| `VLLMRegistry` | A collection of instances with health-check + routing |
| `vllm_generate()` | Drop-in companion to `ollama_generate()` |
| `vllm_chat()` | OpenAI-compatible `/v1/chat/completions` |
| `vllm_embed()` | `/v1/embeddings` |

---

## 3. Capabilities

| Cap | Path | Purpose |
|---|---|---|
| `vllm.status` | `GET /vllm/status` | Cluster health summary |
| `vllm.instances.list` | `GET /vllm/instances` | List configured instances |
| `vllm.instances.add` | `POST /vllm/instances` | Add an instance at runtime |
| `vllm.instances.remove` | `DELETE /vllm/instances/{id}` | Remove an instance |
| `vllm.models` | `GET /vllm/models` | Models served across all instances |
| `vllm.generate` | `POST /vllm/generate` | Raw `/v1/completions` |
| `vllm.chat` | `POST /vllm/chat` | `/v1/chat/completions` |
| `vllm.embed` | `POST /vllm/embed` | `/v1/embeddings` |
| `vllm.lora.load` | `POST /vllm/lora/load` | Load a LoRA adapter |
| `vllm.lora.list` | `GET /vllm/lora` | List loaded adapters |
| `vllm.metrics` | `GET /vllm/metrics` | Parsed Prometheus `/metrics` scrape |
| `vllm.server.start` | `POST /vllm/server/start` | Launch a managed vLLM subprocess (optional) |
| `vllm.server.stop` | `POST /vllm/server/stop` | Stop a managed subprocess |

---

## 4. Configuration

| Env var | Purpose |
|---|---|
| `VLLM_INSTANCES` | JSON list `[{"id","url","has_gpu"}]` **or** a single URL string |
| `VLLM_MODEL` | Default model name |
| `VLLM_API_KEY` | Optional API key (vLLM `--api-key`) |
| `VLLM_QUANTIZATION` | `gptq` \| `awq` \| `fp8` \| `gguf` \| `turbomind` |
| `VLLM_TENSOR_PARALLEL` | Tensor-parallel size (default 1) |
| `VLLM_GPU_MEM_UTIL` | Fraction of GPU VRAM to use (default 0.90) |
| `VLLM_SPEC_MODEL` | Draft model for speculative decoding |
| `VLLM_CPU_OFFLOAD_GB` | KV-cache spill to host RAM |

---

## 5. UI

**`vllm-panel`** (`vllm_panel.html`) shows per-instance health, served models, loaded LoRA adapters, parsed metrics (throughput, queue, KV-cache usage), and the optional managed-server controls.

---

## See also

- [LLM Cluster](./04-ollama-cluster.md) — the backend-agnostic router vLLM plugs into; Ollama is the sibling backend
- [Configuration](./10-configuration.md) — env vars in one place
- [Capability Framework](./01-capability-framework.md) — `vllm.*` registration
- [Machine Learning](./16-machine-learning.md) — LoRA adapters trained/loaded here

## Screenshots

## Serving lifecycle and capacity

Vera treats each vLLM endpoint as a model-serving instance with health,
available models, routing metadata, and optional managed-process state. Chat,
completion, and embedding capabilities use the OpenAI-compatible API; server
start/stop capabilities additionally manage a local subprocess and its launch
arguments.

Capacity is dominated by model weights, KV cache, maximum model length,
batching, tensor parallelism, and LoRA allocation. A server that binds but
cannot complete a warm-up request is not healthy. Verify `/v1/models`, run a
small generation, then inspect Prometheus metrics before enabling production
routing. Reduce context/batch pressure before raising GPU memory utilization.

Managed launch settings such as quantization, dtype, speculative model, prefix
caching, and chunked prefill are workload decisions, not universal optimizations.
Record them with benchmarks. See [Performance and sizing](00-performance-and-sizing.md)
for a repeatable measurement procedure.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
