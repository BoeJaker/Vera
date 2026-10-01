# 16 · Machine Learning

The `machine learning/` module is a live neural-network construction, training, and execution sandbox. It is two cooperating files:

- **`ml_workshop.py`** — build and run networks. A "module" is a JSON-serialised compute graph; the workshop assembles, introspects, and forward-passes it.
- **`ml_training.py`** — train them. A full training loop, dataset management, and data collectors wired into the [Data Fabric](./06-data-fabric.md).

Everything is pure-Python + NumPy by default, so it runs anywhere; PyTorch/JAX are used automatically when present.

## Portable training, evaluation, and prompt boundary

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
offline `DeterministicScalarEvalProvider` is the reference implementation for
that evaluation boundary. It consumes a pinned `ScalarEvaluationFixture` of
already-observed, finite metric values, verifies exact subject and dataset
revision matches, applies declared maximize/minimize thresholds, and emits a
canonical `EvaluationReport`. Its fixed arithmetic-mean aggregation and
case-level failure count are reproducible and perform no model, judge, trainer,
network, or external-provider call.

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

Evidence-producing providers run through `execute_evaluation` under an explicit
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
frozen-projection importer. It accepts a bounded Vera-owned envelope containing
the native evaluation request, content-bound expected cases, threshold metrics,
judge provenance, and integer usage. Full third-party exports are deliberately
not accepted: prompt/configuration, input, expected and actual output, response,
reason, environment, variable, message, stack, and traceback fields fail closed
at any nesting depth. The resulting receipt pins the source/version and export
digest and states that no provider was invoked and no payload was retained.
Incomplete result sets become partial evidence rather than an apparent pass.

Prompt optimizers use a proposal-only boundary. An `OptimizationRequest` pins
the source PromptPackage, distinct training and held-out dataset revisions,
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

`MLWorkshopTrainingRuntime` provides an offline-testable boundary around the
existing Workshop job API. A caller must explicitly bind a base
`ModelPackage` to a Workshop module and the objectives it is allowed to train.
Submissions carry the exact dataset revision, portable request/run identities,
and scalar hyperparameters; the native runner must echo those identities on
submit, observation, and cancellation. A response that drifts to a newer
dataset, another request, or another native job is rejected.

Portable training lifecycle is also projected into the common `Run` journal.
A native completion is successful only when it includes a schema-valid,
content-addressed `ModelPackage` whose `training_run_id` names that exact run.
A checkpoint or Workshop job marked complete without that package is recorded
as a failed portable run, rather than overstating interoperability. The adapter
does not import an ML framework, load data, invoke training on import, redirect
the existing `ml.train` capabilities, or select a runtime.

`MLWorkshopCapabilityBridge` supplies the explicit live seam without changing
those responsibilities. It resolves the request's exact dataset revision to
bounded finite numeric arrays, translates an allowlisted configuration to the
existing `ml.train` job API, verifies job and module identity on every status
read, and waits for a terminal acknowledgement when cancelling. Successful
jobs are exported through `ml.export.onnx`; the mutable Workshop export is
copied to a content-addressed immutable artifact before a run-bound
`ModelPackage` is verified and registered. A changing dataset revision,
identity mismatch, unsupported hyperparameter, mutable artifact race, or
conflicting content-addressed file fails closed.

Constructing the bridge does not train, export, register, activate, deploy, or
route traffic. Callers inject the dataset resolver and existing capability
calls, provide explicit module/package bindings, and opt into the runtime.
Production selection and any replacement of the legacy Workshop path remain a
separate, reversible operational decision.

DeepEval/Promptfoo adapters, Accelerate, PEFT, MLflow, DSPy, live judges, and
native training cutover remain subsequent gated work.

### Portable batch inference

The legacy `ml.train.predict` and `ml.onnx.run` result shapes now share the
validation and normalization boundary in
`models/legacy_prediction_adapter.py`. Both adapters bind a ModelPackage and a
legacy module/model selector, accept only the package's declared task and
input/output contracts, and emit the same bounded prediction/shape result and
terminal events. Runtime-specific metadata is included only through an explicit
field projection; arbitrary legacy response fields do not leak into the
portable result.

`LegacyMLWorkshopInferenceProvider` injects the existing Workshop runner rather
than importing NumPy, PyTorch, or a saved model. The ONNX adapter uses the same
core while retaining its execution-provider evidence. Neither adapter redirects
the existing capabilities, retries failures, loads artifacts, or performs work
when imported, so live parity and cutover remain separate decisions.

Portable provider readiness is evidence-backed rather than a free-form flag.
External cluster or runtime probes publish a bounded, expiring health record
with available ModelPackages and separate load counters. The inference registry
can replay candidate decisions at an explicit time and rejects stale,
foreign-provider, or undeclared-package evidence. It still does not probe a
runtime or select, retry, balance, or fail over providers.

### Portable model inventory

`model.inventory` (`GET /models/inventory`) is the read-only join across the
portable model boundary. It projects registered `ModelPackage` records, aliases,
admission and activation receipts, inference deployments and observations, and
provider descriptors without executing any of them. Source-owned inventories,
including NLP/NER deployments, may contribute only schema-valid packages;
legacy name-only deployments remain visible as unresolved candidates with their
blockers and placement evidence.

The NLP panel's **Models** view consumes this projection. Empty or unavailable
stores remain explicit source states, and dangling aliases, deployments or
providers appear as conflicts. This makes migration progress observable without
turning the inventory into a router, model loader, or activation authority.

Adapter parity can be checked offline from already-collected inference
transcripts. Conformance expectations compare content hashes, terminal state,
stable outage codes, and optional usage counters while keeping output values out
of reports. This supplies deterministic regression fixtures without treating an
offline check as proof that a native runtime, placement, or model is healthy.

An admitted package can also be described as an `InferenceDeployment` without
executing it. The record pins the admission receipt, target and provider,
runtime version, artifact content digests, placements, and retry owner. Its
SQLite lifecycle journal records revision-guarded desired/observed state and
the exact health evidence behind provider observations. This separates
deployment identity and audit history from model loading and traffic routing;
neither registration nor observation performs either effect.

PyTorch and TensorFlow batch prediction can now enter the same portable seam
through `PyTorchInferenceProvider` and `TensorFlowInferenceProvider`. Each
adapter requires a matching deployment, package format/framework, artifact
digest set, provider identity, and runtime kind before it will call an injected
runner. The runner receives only the content-derived deployment ID and canonical
JSON tensor input; arbitrary runtime fields are removed from the portable
result, and backend failures become stable codes.

The adapters themselves remain runtime-free. An opt-in controller boundary in
`native_tensor_runtime.py` can now construct their injected runner from one
caller-resolved local artifact. Before deserialization it requires the exact
declared role, byte length, SHA-256, package/framework, deployment and installed
runtime version. PyTorch accepts TorchScript only and maps it to CPU; it never
uses pickle-based `torch.load`. TensorFlow accepts a single `.keras` v3 artifact
through Keras safe mode. Both execute off the event loop and return only the
portable prediction and shape fields.

This loader does not resolve artifact URIs, download packages, select workers,
route traffic, retry, activate a deployment or modify existing ML traffic. The
execution owner must provision the optional framework and pass an explicit
local path. Representative CPU checks cover verified loading and parity for
both frameworks. Cancellation during a real kernel, accelerator placement,
memory pressure, long-running load, worker loss and teardown remain separate
operational gates.

Those checks establish the native adapter and artifact-verification boundary;
they do not redirect Workshop prediction or make either framework the default.
The deployed NLP estate is a separate example of the portable inventory working
with real ONNX Runtime services: task-specific embedding, NER, classification,
zero-shot, QA, language-identification, and reranking packages appear in
`model.inventory` only when their artifact manifests are content-verified.
Inventory presence still does not grant activation or routing authority.

An evidence-bound dispatch plan closes the gap between provider discovery and
deployment state without becoming a router. For one caller-selected provider
and deployment, it verifies request compatibility, placement, current readiness,
matching health evidence, desired/observed lifecycle state, available capacity,
queue policy, and the deployment's single retry owner. The plan is
content-addressed and includes every source revision used in the decision. It
does not select an alternative, reserve a slot, invoke inference, retry, or fail
over.

---

## 1. Modules as compute graphs (Workshop)

Every ML module is a JSON graph of compute nodes:

| Node kind | Examples |
|---|---|
| Layers | Dense, Conv2D, RNN, LSTM, Attention, Embedding, Norm, Dropout |
| Ops | Add, Mul, Concat, Split, Reshape, Transpose, Softmax, … |
| Activations | ReLU, GELU, Swish, Sigmoid, Tanh, SiLU, custom |
| Perceptrons | single / multi-layer (the fundamental building block) |
| Ensembles | MoE, Bagging, Stacking, Boosting metacompositions |
| Exotic | Hopfield, Reservoir/ESN, CapsNet, Kolmogorov-Arnold |

Modules are stored in Redis (hot) and optionally Postgres (cold). They can be built interactively in the panel, **generated from a natural-language description via LLM**, assembled programmatically via caps, called as caps (`ml.run(<module_id>, inputs)`), or composed into a [DAG](./03-dag-engine.md).

### Execution backends

Pure Python + NumPy is always available. The executor auto-detects PyTorch / JAX and chooses the best backend (`HAS_NP`, `HAS_TORCH`). Forward-pass + introspection is the core; training is layered on by `ml_training.py`.

### Workshop capabilities

| Cap | Purpose |
|---|---|
| `ml.catalogue` | The palette of available node types |
| `ml.create` | Create a module from a graph spec |
| `ml.from_template` | Instantiate from a built-in template |
| `ml.list` / `ml.get` / `ml.delete` | Module CRUD |
| `ml.run` | Forward-pass a module on inputs |
| `ml.inspect` | Shapes, parameter counts, graph structure |
| `ml.generate` | **LLM**: build a module from a natural-language description |
| `ml.explain` | **LLM**: explain what a module does |
| `ml.suggest` | **LLM**: suggest next nodes / fixes for the editor |
| `ml.compare` | Compare two modules |

---

## 2. Training engine

`ml_training.py` adds a backprop training loop that needs no PyTorch:

- **Optimisers** — SGD, Adam, AdamW (pure NumPy).
- **Losses** — MSE, MAE, BCE, CrossEntropy, Huber.
- **Backprop** — through the module graph via numerical gradients (finite difference), with exact gradients for standard layers.
- **Loop** — mini-batch training with progress streamed via Redis events; per-epoch loss/accuracy/MAE/RMSE; early stopping; LR scheduling (step / cosine / plateau).
- **Checkpoint** — best weights saved to Redis keyed by `module_id`.

| Cap | Purpose |
|---|---|
| `ml.train` | Start a training run (streams events) |
| `ml.train.from_dataset` | Train directly from a prepared fabric dataset |
| `ml.train.status` | Live status of a run |
| `ml.train.stop` | Cancel a running job |
| `ml.train.history` | Loss/metric curves for a run |
| `ml.train.evaluate` | Evaluate on a held-out test set |
| `ml.train.predict` | Batch inference with trained weights |
| `ml.train.weights_get` | Export trained weights as JSON |
| `ml.train.weights_load` | Load weights into a module |
| `ml.examples.load_all` | Seed the workshop with all worked examples |

---

## 3. Datasets & data collectors

Training data lives in the fabric. The dataset engine provides a typed `DatasetSpec` (train/val/test splits), built-in generators (synthetic classification, regression, time-series, XOR, spiral, moons, circles — all pure NumPy), fabric loaders (OHLCV / any dataset → NumPy arrays), and feature engineering (normalise, standardise, lag features, returns, rolling stats, RSI, MACD, Bollinger bands).

| Cap | Pulls from |
|---|---|
| `ml.data.fetch_synthetic` | Generated synthetic datasets for any example |
| `ml.data.fetch_ohlcv` | Yahoo Finance / Alpha Vantage / Stooq |
| `ml.data.fetch_crypto` | CoinGecko OHLCV for crypto pairs |
| `ml.data.fetch_macro` | FRED macroeconomic series (GDP, CPI, rates) |
| `ml.data.list` | List all ML-ready datasets in the fabric |
| `ml.data.prepare` | Normalise + split a fabric dataset for training |

This is where ML meets [Markets](./15-markets.md): the `mkt.*` datasets that Markets ingests are directly trainable via `ml.data.prepare` and `ml.train.from_dataset`.

---

## 4. UI

- **`ml-workshop-panel`** (`ml_workshop_panel.html`) — the visual graph builder: drag nodes from the catalogue, wire them, run a forward pass, inspect shapes, or ask the LLM to generate a network from a prompt.
- **`ml-training-panel`** (`ml_training_panel.html`) — pick a module + dataset, configure the optimiser/schedule, launch a run, and watch live loss/metric curves stream in.

---

## See also

- [Data Fabric](./06-data-fabric.md) — where datasets and weights checkpoints live
- [Markets](./15-markets.md) — `mkt.*` OHLCV datasets feed `ml.data.prepare`
- [DAG Engine](./03-dag-engine.md) — compose `ml.run` / training steps into workflows
- [Capability Framework](./01-capability-framework.md) — `ml.*` registration & event streaming

## Screenshots

## Runtime and operating model

The ML subsystem separates **experiment definition** from **execution**. The
workshop capabilities validate a model/training configuration, persist it, and
hand execution to the training engine. Long-running work should be treated as a
job: capture its identifier, poll status, and read artifacts only after a
terminal state. Training data remains a fabric concern; the ML layer consumes a
resolved dataset rather than creating a second data store.

Operationally, check four boundaries in order: dataset availability, Python
framework/import health, accelerator visibility, and artifact writeability. A
run that never leaves `queued` normally indicates dispatch or worker capacity;
a run that starts and fails immediately is usually configuration, shapes, or a
missing framework; a run that completes without an artifact points to the
output path or persistence stage.

Key implementation surfaces are `vera/machine learning/ml_workshop.py`,
`ml_training.py`, and their panel HTML files. ONNX export is documented
separately in [ONNX](30-onnx.md).

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
