# 16 · Machine Learning

The `vera/machine learning/` module is Vera's neural-network sandbox: build a
network as a JSON compute graph, run it, train it on fabric datasets, let an
LLM design or refine it, and export it to ONNX. It is pure Python + NumPy, so it
runs anywhere without a deep-learning framework.

It is complemented by the **portable model lifecycle** in `vera/models/`:
immutable contracts for prompts, training requests and runs, evaluation
evidence, optimisation proposals, inference deployments and the model
inventory. Those contracts describe and verify ML work without executing it;
the Workshop remains the place where training actually happens today.

**Maturity:** the Workshop, training engine, data collectors and ML Lab UI are
working tools aimed at small models and experiments (forward passes and
training run in-process on NumPy). The portable contracts are offline,
test-backed boundaries; activation of a native runtime is a separate, reviewed
decision.

## Contents

- [1. Source map](#1-source-map)
- [2. Modules as compute graphs (Workshop)](#2-modules-as-compute-graphs-workshop)
  - [Node catalogue](#node-catalogue)
  - [Templates](#templates)
  - [Storage](#storage)
  - [Workshop capabilities](#workshop-capabilities)
- [3. Training engine](#3-training-engine)
  - [Training capabilities](#training-capabilities)
- [4. Datasets and data collectors](#4-datasets-and-data-collectors)
- [5. Agentic build-and-test](#5-agentic-build-and-test)
- [6. ONNX export and inference](#6-onnx-export-and-inference)
- [7. UI: the ML Lab](#7-ui-the-ml-lab)
- [8. Events and storage keys](#8-events-and-storage-keys)
- [9. Worked examples](#9-worked-examples)
- [10. Runtime and operating model](#10-runtime-and-operating-model)
- [11. Portable training, evaluation and prompt boundary](#11-portable-training-evaluation-and-prompt-boundary)
  - [Portable training boundary](#portable-training-boundary)
  - [Portable batch inference](#portable-batch-inference)
  - [Portable model inventory and deployments](#portable-model-inventory-and-deployments)
- [See also](#see-also)

---

## 1. Source map

| File | Responsibility |
|---|---|
| `vera/machine learning/ml_workshop.py` | Node catalogue, templates, graph execution (forward pass), inspection, LLM generate/explain/suggest, module CRUD, `/ml/panel` route |
| `vera/machine learning/ml_training.py` | Losses, optimisers, LR schedules, backprop, training jobs, dataset generators and collectors, feature engineering, agentic build-and-test, `ml-lab` tab and `/ml/lab`, `/ml/training_panel` routes |
| `vera/machine learning/ml_onnx.py` | ONNX export, verification, inference and artifact management |
| `ml_lab_panel.html` | The combined ML Lab tab (embeds the two panels below) |
| `ml_workshop_panel.html` | Visual graph builder ("Build") |
| `ml_training_panel.html` | Training UI |
| `vera/models/*.py` | Portable contracts ([§11](#11-portable-training-evaluation-and-prompt-boundary)) |

## 2. Modules as compute graphs (Workshop)

A **module** is a JSON graph: `nodes` (each with `id`, `type` and parameters)
and `edges` (`from` → `to`, optionally `skip`). `ml.run` executes it in
topological order on NumPy arrays and returns per-node outputs and summary
statistics; `ml.inspect` reports shapes, parameter counts, connectivity and
cycles. PyTorch is detected and reported in `ml.catalogue` / `ml.inspect`
(`backends: {numpy, torch}`), but execution itself is NumPy.

### Node catalogue

`LAYER_CATALOGUE` in `ml_workshop.py`:

| Category | Node types |
|---|---|
| fundamental | `perceptron`, `dense`, `mlp` |
| activation | `activation` (`relu`, `gelu`, `sigmoid`, `tanh`, `swish`, `silu`, `softmax`, `step`, `identity`, `linear`, `error`) |
| conv | `conv1d`, `conv2d`, `depthwise_conv` |
| pooling | `pool` |
| recurrent | `rnn`, `lstm`, `gru` |
| attention | `attention`, `multi_head_attention`, `transformer_block` |
| norm | `layer_norm`, `batch_norm`, `rms_norm` |
| regularisation | `dropout` |
| embedding | `embedding`, `positional_encoding`, `rotary_embedding` |
| structural | `residual`, `concat`, `add`, `reshape`, `linear_probe` |
| exotic | `hopfield`, `reservoir`, `capsule`, `moe_router`, `kan_layer`, `fourier_layer`, `state_space`, `neural_ode` |

### Templates

`TEMPLATES` (used by `ml.from_template`): `perceptron`, `mlp_classifier`,
`transformer_block`, `lstm_seq2seq`, `resnet_block`, `moe`, `esn`,
`hopfield_memory`, `kan`, `ssm`.

### Storage

Module definitions are written to the fabric dataset `ml.modules` and cached in
Redis under `vera:ml:<module_id>` with a 24-hour expiry. At startup modules are
loaded from both. `ml.delete` removes the module from the in-process cache, the
fabric and Redis.

### Workshop capabilities

| Capability | Route | Purpose |
|---|---|---|
| `ml.catalogue` | `GET /ml/catalogue` | Layer catalogue and templates |
| `ml.create` | `POST /ml/create` | Create or update a module (`name`, `nodes`, `edges`, `description`) |
| `ml.from_template` | `POST /ml/from_template` | Instantiate a template |
| `ml.list` | `GET /ml/list` | Saved modules (from the fabric) |
| `ml.get` | `GET /ml/get` | One module by `id` |
| `ml.delete` | `POST /ml/delete` | Delete by `id` |
| `ml.run` | `POST /ml/run` | Forward pass: `id`, `inputs` (JSON `{node_id: array}`), `summarise` |
| `ml.inspect` | `GET /ml/inspect` | Shapes, parameter counts, connectivity, cycle detection |
| `ml.generate` | `POST /ml/generate` | **LLM**: design a module from a description (`constraints`, `save`) |
| `ml.explain` | `POST /ml/explain` | **LLM**: explain a module |
| `ml.suggest` | `POST /ml/suggest` | **LLM**: suggest improvements or extensions |
| `ml.compare` | `POST /ml/compare` | Compare two modules (parameters, depth, layer families, shapes) |

## 3. Training engine

`ml_training.py` adds a training loop that needs no deep-learning framework:

- **Gradients.** Exact reverse-mode gradients from one forward and one backward
  pass (`_forward_backward`) for graphs built only from `input`, `dense`,
  `linear_probe`, `perceptron`, `mlp`, `activation`, `layer_norm`, `rms_norm`,
  `dropout`, `add`, `residual`, `concat`, `embedding`, `reshape` and `output`
  nodes. Graphs containing other node types (for example recurrent, attention,
  `conv1d`, `kan_layer` or pooling nodes) fall back to one-sided finite
  differences (`eps = 1e-4`), run in a worker thread so the event loop is not
  blocked.
- **Optimisers:** `sgd`, `adam` (β₁ 0.9, β₂ 0.999), `adamw`.
- **Losses:** `mse`, `mae`, `bce`, `cross_entropy`, `huber`.
- **LR schedules:** `constant`, `step` (halve every `epochs/5`), `cosine`
  (default), `linear`.
- **Loop:** mini-batches, per-epoch loss and metrics, early stopping
  (`early_stopping_patience`, default 10), optional `weight_decay` and
  `grad_clip`; progress is emitted as `ml.train_epoch` events.
- **Checkpoint:** best weights saved to Redis under
  `vera:ml:weights:<module_id>` with a 7-day expiry.

### Training capabilities

| Capability | Route | Purpose |
|---|---|---|
| `ml.train` | `POST /ml/train` | Start a job: `module_id`, `X_train`, `y_train`, optional `X_val`, `y_val`, `config` JSON. Returns `{job_id}` immediately |
| `ml.train.from_dataset` | `POST /ml/train/dataset` | `ml.data.prepare` + `ml.train` in one call |
| `ml.train.status` | `GET /ml/train/status` | One job (`job_id`) or all jobs |
| `ml.train.stop` | `POST /ml/train/stop` | Stop a job |
| `ml.train.history` | `GET /ml/train/history` | Loss and metric history |
| `ml.train.evaluate` | `POST /ml/train/evaluate` | `{loss, accuracy, mse, mae, predictions_sample}` on test data |
| `ml.train.predict` | `POST /ml/train/predict` | Batch inference with trained weights |
| `ml.train.weights_get` | `GET /ml/train/weights` | Export weights as JSON |
| `ml.train.weights_load` | `POST /ml/train/weights/load` | Load weights from JSON |
| `ml.examples.load_all` | `POST /ml/examples/load_all` | Load 12 annotated example modules and synthetic datasets; `fetch_real_data=true` also pulls OHLCV from Stooq |

## 4. Datasets and data collectors

Training data lives in the fabric. Generators are pure NumPy; feature
engineering covers normalisation, standardisation, lag features, returns,
rolling statistics, RSI, MACD and Bollinger bands.

| Capability | Route | Source / behaviour |
|---|---|---|
| `ml.data.fetch_synthetic` | `POST /ml/data/synthetic` | `kind`: `classification`, `regression`, `xor`, `spiral`, `moons`, `timeseries`, `autoencoder`, `ohlcv_synthetic` |
| `ml.data.fetch_ohlcv` | `POST /ml/data/ohlcv` | Stooq CSV (no key); `symbol` such as `AAPL.US`, `interval` `d`/`w`/`m`; stored as `ml.ohlcv.<symbol>` |
| `ml.data.fetch_crypto` | `POST /ml/data/crypto` | CoinGecko public API; `coin_id`, `vs_currency`, `days` 1–365 |
| `ml.data.fetch_macro` | `POST /ml/data/macro` | FRED public series; default `GDP,CPIAUCSL,FEDFUNDS,UNRATE` into `ml.macro.fred` |
| `ml.data.list` | `GET /ml/data/list` | Every fabric dataset whose ID starts with `ml.` |
| `ml.data.prepare` | `POST /ml/data/prepare` | Features, normalisation and train/val/test split; `task`: `classification`, `regression`, `forecasting`, `ohlcv_direction` |

The `mkt.*` datasets ingested by [Markets](./15-markets.md) can be prepared and
trained the same way.

## 5. Agentic build-and-test

`ml.agent.build_and_test` (`POST /ml/agent/build_and_test`) designs, trains and
refines a network from a plain-language `goal`. Each round the LLM proposes an
architecture, the module is trained (`epochs`, default 30) on the given
`dataset_id` or a fresh synthetic dataset (`dataset_kind`, `n_samples` 600,
`features` 8, `classes` 3), evaluated, and revised to reduce validation loss;
the best candidate is kept. Arguments: `task` (`classification`,
`binary_classification`, `regression`, `forecasting`, `ohlcv_direction`,
`reconstruction`), `rounds` (3), `target_metric`, `constraints`. It returns
`{run_id}` immediately and emits `ml.agent_started` and `ml.agent_step`
events; `ml.agent.status` and `ml.agent.stop` (after the current round) manage
runs.

## 6. ONNX export and inference

`ml_onnx.py` exports trained Workshop modules to ONNX (opset `ML_ONNX_OPSET`,
default `17`; IR version `ML_ONNX_IR_VERSION`, default `10`) into
`ML_ONNX_DIR` (default `<repo>/edge/models`), as a `.onnx` file plus a `.json`
sidecar.

| Capability | Route | Purpose |
|---|---|---|
| `ml.export.onnx` | `POST /ml/export/onnx` | `module_id`, `dtype` (`float32`/`float64`), `register_cap` → `{ok, artifact, path, cap_name, unsupported}` |
| `ml.onnx.run` | `POST /ml/onnx/run` | ONNX Runtime inference → `{ok, predictions, provider, shape}` |
| `ml.onnx.verify` | `POST /ml/onnx/verify` | Compare the ONNX output with the NumPy reference forward pass |
| `ml.onnx.list` | `GET /ml/onnx/list` | Exported artifacts |
| `ml.onnx.delete` | `POST /ml/onnx/delete` | Delete an artifact and its registered capability |

Progress is emitted as `ml.onnx.progress`. Details are in [ONNX](./30-onnx.md).

## 7. UI: the ML Lab

`ml_training.py` registers one tab, **`ml-lab`** ("ML Lab", `tab_order=65`),
served at `/ml/lab`. It embeds the Workshop builder (`/ml/panel`,
`ml_workshop_panel.html`) and the training panel (`/ml/training_panel`,
`ml_training_panel.html`); the standalone Workshop tab has been merged into it.

- **Build:** drag nodes from the catalogue, wire them, run a forward pass,
  inspect shapes, or ask the LLM to generate a network from a prompt.
- **Train:** pick a module and dataset, set optimiser, loss and schedule,
  launch a run, and watch live loss and metric curves.

## 8. Events and storage keys

| Event | Emitted when |
|---|---|
| `ml.module_created`, `ml.module_generated`, `ml.module_deleted`, `ml.run_complete` | Workshop operations |
| `ml.train_started`, `ml.train_epoch`, `ml.train_complete` | Training lifecycle |
| `ml.data.ready`, `ml.examples.loaded` | Datasets and examples |
| `ml.agent_started`, `ml.agent_step` | Agentic build-and-test |
| `ml.onnx.progress` | ONNX export |

| Store | Key / dataset |
|---|---|
| Module cache | Redis `vera:ml:<module_id>` (24 h) |
| Weights | Redis `vera:ml:weights:<module_id>` (7 days) |
| Job state | Redis prefix `vera:ml:job:` |
| Module definitions | Fabric `ml.modules` |
| Dataset metadata | Fabric `ml.datasets` |
| Data | Fabric `ml.*` datasets (e.g. `ml.ohlcv.<symbol>`, `ml.macro.fred`) |
| ONNX artifacts | `ML_ONNX_DIR` |

## 9. Worked examples

Create a small classifier from a template, generate data and train:

```bash
curl -s -X POST http://localhost:8999/ml/from_template \
  -H 'Content-Type: application/json' -d '{"template":"mlp_classifier"}'
curl -s -X POST http://localhost:8999/ml/data/synthetic \
  -H 'Content-Type: application/json' -d '{"kind":"moons"}'
curl -s -X POST http://localhost:8999/ml/train/dataset \
  -H 'Content-Type: application/json' \
  -d '{"module_id":"<id>","dataset_id":"<dataset_id>","task":"classification",
       "config":"{\"epochs\":50,\"optimiser\":\"adam\",\"loss\":\"cross_entropy\"}"}'
```

Let the agent design one:

```bash
curl -s -X POST http://localhost:8999/ml/agent/build_and_test \
  -H 'Content-Type: application/json' \
  -d '{"goal":"classify 3 noisy clusters","task":"classification","rounds":3}'
```

## 10. Runtime and operating model

The ML subsystem separates **experiment definition** from **execution**. The
Workshop capabilities validate and persist a module; the training engine runs
jobs. Treat long-running work as a job: capture its identifier, poll status,
and read artifacts only after a terminal state. Training data remains a fabric
concern; the ML layer consumes a resolved dataset rather than keeping its own
store.

Operationally, check four boundaries in order: dataset availability, Python
import health (NumPy is required; `httpx` for collectors), accelerator
visibility (only relevant to the portable native runtimes), and artifact
writeability. A run that never leaves `queued` normally indicates dispatch or
capacity; a run that starts and fails immediately is usually configuration,
shapes or a missing dependency; a run that completes without an artifact points
to the output path or persistence stage. Graphs that fall back to finite
differences train much more slowly — keep large experiments to the
backprop-supported node types.

## 11. Portable training, evaluation and prompt boundary

The portable lifecycle begins with immutable contracts in
[`vera/models/training_contracts.py`](../vera/models/training_contracts.py). A
`PromptPackage` gives ordered role/template messages, declared variables,
output contract, metadata, and a canonical `ppkg_…` identity. A
`TrainingRequest` pins the dataset revision, objective, base ModelPackage,
optional PromptPackage, and scalar hyperparameters; `TrainingRun` records the
runtime-neutral lifecycle and requires successful runs to produce a
ModelPackage identity.

`EvaluationRequest` pins the subject, dataset revision, prompt, and complete
metric set. `EvaluationReport` records finite thresholded metrics, case counts,
provider identity, terminal outcome, and a canonical `eval_…` identity. It
cannot claim a pass unless every requested metric is present and passes and no
case failed. Strict reconstruction recomputes identities and derived pass flags,
so changed or forged serialized evidence fails visibly.

`EvalProvider` and `TrainingRuntime` are structural protocols with explicit
profiles; defining or validating these contracts invokes neither protocol. The
offline `DeterministicScalarEvalProvider` (`vera/models/deterministic_evaluation.py`)
is the reference implementation for that evaluation boundary. It consumes a
pinned `ScalarEvaluationFixture` of already-observed, finite metric values,
verifies exact subject and dataset revision matches, applies declared
maximize/minimize thresholds, and emits a canonical `EvaluationReport`. Its
fixed arithmetic-mean aggregation and case-level failure count are reproducible
and perform no model, judge, trainer, network, or external-provider call.

Case-level and incremental evidence uses the compatible contracts in
`vera/models/evaluation_evidence.py`. An `EvaluationCaseIdentity` binds a case
key to its exact dataset revision plus input and expected-output SHA-256
digests; raw inputs and answers never enter the evidence envelope.
`CaseEvaluationEvidence` records thresholded metrics, integer token/cost/latency
accounting, and explicit deterministic, model, or human judge provenance. Model
judges must identify the provider, ModelPackage, and PromptPackage together.

`PartialEvaluationReport` can preserve a non-empty subset after interruption
without claiming terminal success. Completion requires every expected
content-bound case, and strict reconstruction recomputes case/report IDs,
coverage, usage totals, and pass flags. `evaluate_ci_policy` is a pure,
effect-free decision over terminal state, coverage, failed cases, judge kind,
cost, and latency; it never calls a provider or activates the evaluated model,
prompt, capability, or run. This makes incomplete and forged evidence visible
while keeping optimization and activation as later, separately reviewed
decisions.

`DeterministicEvidenceEvalProvider` is the reference replay adapter for this
richer envelope. It accepts only a frozen request, its exact case identities,
and already-observed evidence, rejects dataset, membership, and metric drift,
and emits either an honest partial report or a complete one. Importing or
calling it performs no judge, model, network, filesystem, or activation effect.

Evidence-producing providers run through `execute_evaluation`
(`vera/models/evaluation_execution.py`) under an explicit
`EvaluationExecutionPolicy`. The default admits only providers declaring
offline operation and deterministic judges, passes no case payloads, limits the
case set and total integer cost, and keeps timeout/cancellation ownership in
Vera. The wrapper cancels a timed-out provider, preserves a bounded failed
report, rejects provider/request/case/provenance drift, and converts a cost
overrun into non-passing evidence. Enabling a model judge or an external
provider requires a deliberate policy change. External providers must also
declare enforceable cost-budget and cancellation support; a local task cancel
or post-hoc cost observation is not treated as proof of either. This contract
does not expose such an activation through a capability or UI.

Already-produced DeepEval and Promptfoo results can enter through a strict
frozen-projection importer (`vera/models/external_evaluation_import.py`). It
accepts a bounded Vera-owned envelope containing the native evaluation request,
content-bound expected cases, threshold metrics, judge provenance, and integer
usage. Full third-party exports are deliberately not accepted:
prompt/configuration, input, expected and actual output, response, reason,
environment, variable, message, stack, and traceback fields fail closed at any
nesting depth. The resulting receipt pins the source/version and export digest
and states that no provider was invoked and no payload was retained.
Incomplete result sets become partial evidence rather than an apparent pass.

Prompt optimizers use a proposal-only boundary
(`vera/models/optimizer_contracts.py`). An `OptimizationRequest` pins the
source PromptPackage, distinct training and held-out dataset revisions,
objective metrics, candidate count, integer cost, and duration budgets.
Candidates contain immutable PromptPackages plus exact optimizer name/version,
trace digest, parent prompt, and usage provenance. Proposal reconstruction
recomputes request, candidate, PromptPackage, and proposal identities and rejects
forged activation/effect claims.

Selection is a separate pure decision over complete held-out evidence. It first
requires the baseline and every candidate report to match the requested prompt,
dataset, metrics, and CI policy, then recommends the best reproducible
improvement or declines all candidates. Ties resolve by stable candidate ID.
Neither the proposal nor selection contract has an operation for prompt
registration, alias mutation, deployment, or activation. DSPy and other
optimizer execution remains an optional provider concern and is not enabled by
these records.

### Portable training boundary

`MLWorkshopTrainingRuntime` (`vera/models/ml_workshop_training_adapter.py`)
provides an offline-testable boundary around the existing Workshop job API. A
caller must explicitly bind a base `ModelPackage` to a Workshop module and the
objectives it is allowed to train. Submissions carry the exact dataset
revision, portable request/run identities, and scalar hyperparameters; the
native runner must echo those identities on submit, observation, and
cancellation. A response that drifts to a newer dataset, another request, or
another native job is rejected.

Portable training lifecycle is also projected into the common `Run` journal.
A native completion is successful only when it includes a schema-valid,
content-addressed `ModelPackage` whose `training_run_id` names that exact run.
A checkpoint or Workshop job marked complete without that package is recorded
as a failed portable run, rather than overstating interoperability. The adapter
does not import an ML framework, load data, invoke training on import, redirect
the existing `ml.train` capabilities, or select a runtime.

`MLWorkshopCapabilityBridge` (`vera/models/ml_workshop_runtime_bridge.py`)
supplies the explicit live seam without changing those responsibilities. It
resolves the request's exact dataset revision to bounded finite numeric arrays,
translates an allowlisted configuration to the existing `ml.train` job API,
verifies job and module identity on every status read, and waits for a
terminal acknowledgement when cancelling. Successful jobs are exported through
`ml.export.onnx`; the mutable Workshop export is copied to a content-addressed
immutable artifact before a run-bound `ModelPackage` is verified and
registered. A changing dataset revision, identity mismatch, unsupported
hyperparameter, mutable artifact race, or conflicting content-addressed file
fails closed.

Constructing the bridge does not train, export, register, activate, deploy, or
route traffic. Callers inject the dataset resolver and existing capability
calls, provide explicit module/package bindings, and opt into the runtime.
Production selection and any replacement of the legacy Workshop path remain a
separate, reversible operational decision. Accelerate, PEFT, MLflow, DSPy, live
judges and native training cut-over are not implemented.

### Portable batch inference

The legacy `ml.train.predict` and `ml.onnx.run` result shapes share the
validation and normalization boundary in `vera/models/legacy_prediction_adapter.py`.
Both adapters bind a ModelPackage and a legacy module/model selector, accept
only the package's declared task and input/output contracts, and emit the same
bounded prediction/shape result and terminal events. Runtime-specific metadata
is included only through an explicit field projection; arbitrary legacy
response fields do not leak into the portable result.

`LegacyMLWorkshopInferenceProvider` (`vera/models/ml_workshop_inference_adapter.py`)
injects the existing Workshop runner rather than importing NumPy, PyTorch, or a
saved model. The ONNX adapter uses the same core while retaining its
execution-provider evidence. Neither adapter redirects the existing
capabilities, retries failures, loads artifacts, or performs work when
imported, so live parity and cutover remain separate decisions.

Portable provider readiness is evidence-backed rather than a free-form flag.
External cluster or runtime probes publish a bounded, expiring health record
with available ModelPackages and separate load counters. The inference registry
can replay candidate decisions at an explicit time and rejects stale,
foreign-provider, or undeclared-package evidence. It still does not probe a
runtime or select, retry, balance, or fail over providers.

PyTorch and TensorFlow batch prediction can enter the same portable seam
through `PyTorchInferenceProvider` and `TensorFlowInferenceProvider`
(`vera/models/native_tensor_inference_adapter.py`). Each adapter requires a
matching deployment, package format/framework, artifact digest set, provider
identity, and runtime kind before it will call an injected runner. The runner
receives only the content-derived deployment ID and canonical JSON tensor input;
arbitrary runtime fields are removed from the portable result, and backend
failures become stable codes.

The adapters themselves remain runtime-free. An opt-in controller boundary in
`vera/models/native_tensor_runtime.py` can construct their injected runner from
one caller-resolved local artifact. Before deserialization it requires the
exact declared role, byte length, SHA-256, package/framework, deployment and
installed runtime version. PyTorch accepts TorchScript only and maps it to CPU;
it never uses pickle-based `torch.load`. TensorFlow accepts a single `.keras` v3
artifact through Keras safe mode. Both execute off the event loop and return
only the portable prediction and shape fields.

This loader does not resolve artifact URIs, download packages, select workers,
route traffic, retry, activate a deployment or modify existing ML traffic. The
execution owner must provision the optional framework and pass an explicit
local path. Representative CPU checks cover verified loading and parity for
both frameworks. Cancellation during a real kernel, accelerator placement,
memory pressure, long-running load, worker loss and teardown remain separate
operational gates. Those checks establish the native adapter and
artifact-verification boundary; they do not redirect Workshop prediction or
make either framework the default.

### Portable model inventory and deployments

`model.inventory` (`GET /models/inventory`, `vera/models/model_inventory_capabilities.py`)
is the read-only join across the portable model boundary. It projects
registered `ModelPackage` records, aliases, admission and activation receipts,
inference deployments and observations, and provider descriptors without
executing any of them. Source-owned inventories, including NLP/NER deployments,
may contribute only schema-valid packages; legacy name-only deployments remain
visible as unresolved candidates with their blockers and placement evidence.
The deployed NLP estate is an example of the inventory working with real ONNX
Runtime services: task-specific embedding, NER, classification, zero-shot, QA,
language-identification and reranking packages appear only when their artifact
manifests are content-verified. Inventory presence does not grant activation
or routing authority.

The NLP panel's **Models** view consumes this projection. Empty or unavailable
stores remain explicit source states, and dangling aliases, deployments or
providers appear as conflicts. This makes migration progress observable without
turning the inventory into a router, model loader, or activation authority.

Adapter parity can be checked offline from already-collected inference
transcripts (`vera/models/inference_conformance.py`). Conformance expectations
compare content hashes, terminal state, stable outage codes, and optional usage
counters while keeping output values out of reports. This supplies
deterministic regression fixtures without treating an offline check as proof
that a native runtime, placement, or model is healthy.

An admitted package can be described as an `InferenceDeployment`
(`vera/models/inference_deployment.py`) without executing it. The record pins
the admission receipt, target and provider, runtime version, artifact content
digests, placements, and retry owner. Its SQLite lifecycle journal records
revision-guarded desired/observed state and the exact health evidence behind
provider observations. This separates deployment identity and audit history
from model loading and traffic routing; neither registration nor observation
performs either effect.

An evidence-bound dispatch plan (`vera/models/inference_dispatch.py`) closes
the gap between provider discovery and deployment state without becoming a
router. For one caller-selected provider and deployment, it verifies request
compatibility, placement, current readiness, matching health evidence,
desired/observed lifecycle state, available capacity, queue policy, and the
deployment's single retry owner. The plan is content-addressed and includes
every source revision used in the decision. It does not select an alternative,
reserve a slot, invoke inference, retry, or fail over.

## See also

- [Data Fabric](./06-data-fabric.md) — datasets and module storage
- [Markets](./15-markets.md) — `mkt.*` OHLCV datasets for `ml.data.prepare`
- [DAG Engine](./03-dag-engine.md) — compose `ml.run` and training steps
- [Capability Framework](./01-capability-framework.md) — `ml.*` registration and events
- [ONNX](./30-onnx.md) — export, runtime and the NLP model estate
- [Worldview](./11-worldview.md) — the separate PyTorch world model over the fabric

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
