# Modern AI ecosystem roadmap

This roadmap identifies modern AI libraries and standards that Vera does not
currently integrate meaningfully, then separates valuable interoperability from
another round of capability and runtime sprawl. It is an adoption portfolio, not
a dependency shopping list.

Research was refreshed on 2026-08-22 from primary project documentation. The
repository comparison was static. No package was installed, no service was
started, no model was called, and no live benchmark was run. The validation plan
at the end is queued until explicitly authorized.

## Executive recommendation

Vera should integrate protocols and narrow provider contracts before adding more
agent frameworks. The highest-value missing pieces are:

1. OpenTelemetry GenAI conventions plus OpenInference export for portable traces.
2. Agent2Agent (A2A) support for communication with independent agents.
3. A standard evaluation adapter, initially exercised with DeepEval and Promptfoo.
4. Structured generation adapters for Instructor and Outlines.
5. A LiteLLM provider/gateway adapter, without outsourcing Vera's policy resolver.
6. Docling as the first richer document-conversion adapter.
7. DSPy as an experimental optimization backend for evaluated prompts/programs.
8. MLflow interoperability for experiments, evaluations, prompts, and model records.
9. Qdrant as a conformance test for modern hybrid/multivector retrieval.
10. Hugging Face PEFT and one scalable serving adapter when the model lifecycle is
    ready to carry their provenance and deployment records.

LlamaIndex, Haystack, Microsoft Agent Framework, GraphRAG, Ray Serve, BentoML,
Langfuse, Phoenix, Guardrails AI, Ragas, and similar systems are valuable, but Vera
should approach them through interchangeable adapters and experiments. It should
not add their orchestration, storage, policy, or registry semantics directly to
the kernel.

## Multi-round walk plan

### Iteration 1 — inventory and standards

- [x] Search Vera source and documentation for existing integrations and partial
  dependencies.
- [x] Review current official documentation for candidate libraries and standards.
- [x] Separate protocols, frameworks, providers, developer tools, and stores.
- [x] Identify conflicts with Vera's proposed Capability v2, Workflow IR, Run,
  Record, Artifact, Memory, Evidence, and ModelPackage contracts.

### Iteration 2 — portfolio and boundaries

- [x] Rank candidates by interoperability value, maturity signal, duplication,
  operational burden, data exposure, and reversibility.
- [x] Select narrow first adapters and explicitly defer competing products.
- [x] Define what Vera owns versus what each external library owns.
- [x] Define exit conditions so a pilot can be removed cleanly.

### Iteration 3 — adapter design

- [x] Define bounded work units for standards, evaluation, generation, ingestion,
  optimization, retrieval, models, serving, and agent frameworks.
- [x] Define package isolation, version pinning, feature negotiation, provenance,
  security, observability, and rollback requirements.
- [x] Add a queued live-test matrix without executing it.

### Iteration 4 — queued live validation

- [ ] Freeze representative tasks, datasets, model packages, and baselines.
- [ ] Install candidates only in isolated, pinned environments or containers.
- [ ] Run conformance, quality, latency, cost, failure, cancellation, recovery,
  upgrade, and teardown tests.
- [ ] Compare candidates and update the portfolio from measured evidence.
- [ ] Activate only adapters that meet their decision gate.

Iteration 4 must remain queued until the user explicitly says to begin live tests.

## What Vera already has

The static search found meaningful existing support for LangGraph, PydanticAI,
smolagents, OpenClaw, MCP, Ollama, vLLM, ONNX, Chroma, FAISS, Neo4j, Postgres,
object storage, and a generic model-provider surface. Hugging Face Transformers is
used optionally for research NLP, and the UI refers to Hugging Face models and
`.safetensors` assets.

The search did not find meaningful implementation references for DSPy, LlamaIndex,
Haystack, LiteLLM, Instructor, Outlines, MLflow, OpenTelemetry, OpenInference,
Langfuse, Phoenix, DeepEval, Ragas, Promptfoo, Qdrant, Ray Serve, BentoML,
Microsoft GraphRAG, Docling, Unstructured, A2A, or PEFT. A word match alone is not
an integration; generic uses of words such as `guidance`, `outlines`, and
`haystack` were excluded.

## Scoring model

Each candidate is scored qualitatively against gap fit, interoperability,
replacement value, boundary clarity, operational cost, and evidenceability.

- **P0 contract** — define now because many later integrations depend on it.
- **P1 pilot** — build one isolated adapter after its prerequisite contracts.
- **P2 conditional** — useful after scale or product evidence demonstrates need.
- **Watch** — support through generic protocols; no dedicated work yet.
- **Decline deep integration** — may run externally, but Vera should not absorb its
  internal abstractions.

## Priority portfolio

| Candidate | Category | Priority | Vera integration shape | Main reason |
| --- | --- | --- | --- | --- |
| OpenTelemetry GenAI conventions | Standard | P0 | Run/Event exporter and importer mapping | Portable telemetry vocabulary |
| OpenInference | Instrumentation | P0 | Optional AI span projection over OpenTelemetry | Covers models, retrieval, and tools without a proprietary backend |
| A2A | Protocol | P0 | Agent card, task/message/artifact adapter | Communicate with independent opaque agents |
| DeepEval | Evaluation | P1 | EvalProvider over frozen Run traces | Agent, tool, RAG, conversation, and component evaluation |
| Promptfoo | Evaluation/security | P1 | CLI/OCI EvalProvider and red-team adapter | Local/CI evaluation and adversarial testing |
| Instructor | Structured output | P1 | StructuredGenerationProvider | Typed validation and corrective retry |
| Outlines | Constrained decoding | P1 | StructuredGenerationProvider for compatible local runtimes | Token-level schema-constrained generation |
| LiteLLM | Gateway/provider | P1 | InferenceProvider and usage/health adapter | Broad provider coverage and gateway operations |
| Docling | Document intelligence | P1 | DocumentParser provider | Rich multi-format conversion and portable representation |
| DSPy | Optimization | P1 experimental | OptimizerProvider consuming evaluations and prompt packages | Evaluation-driven optimization |
| MLflow | Lifecycle/observability | P1 | Run/evaluation/prompt/model export and import | Established ML and GenAI lifecycle interoperation |
| Qdrant | Retrieval | P1 | VectorIndexProvider | Tests dense+sparse hybrid and multivector contracts |
| Hugging Face PEFT | Training | P1/P2 | TrainingAdapter producing derived ModelPackages | Provenance-aware parameter-efficient adaptation |
| LlamaIndex | Data/RAG/agents | P2 | Data/Workflow adapters and selected readers | Broad ecosystem, but heavy Fabric/orchestration overlap |
| Haystack | RAG/pipelines | P2 | Component and pipeline import/export adapter | Modular retrieval with Workflow/Fabric overlap |
| GraphRAG | Graph retrieval | P2 experimental | Offline Index/Evidence adapter over snapshots | Compare graph retrieval with Fabric/Worldview |
| Ray Serve | Distributed serving | P2 | DeploymentProvider | Multi-model composition and autoscaling |
| BentoML | Model serving | P2 | DeploymentProvider | Portable Python model services |
| Langfuse | Observability/evals | P2 backend | OTLP/OpenInference export target | Self-hosted trace/evaluation UI |
| Phoenix | Observability/evals | P2 backend | OTLP/OpenInference export target | Portable open-source observability backend |
| Guardrails AI | Validation/safety | P2 | ValidatorProvider | Reusable input/output validators |
| Ragas | RAG evaluation | P2 metric pack | EvalProvider metric extension | Specialized retrieval/generation metrics |
| Microsoft Agent Framework | Agent runtime | Watch/P2 | RuntimeAdapter after API stabilization | Cross-language ecosystem access |
| Unstructured | Document ingestion | Watch/alternative | DocumentParser provider | Compare with Docling rather than install both first |

## P0 standards

### OpenTelemetry GenAI and OpenInference

OpenTelemetry semantic conventions provide common meanings for telemetry
attributes. OpenInference complements OpenTelemetry with AI-focused conventions
and instrumentations spanning model, retrieval, and tool operations.

Sources: [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
and [OpenInference](https://github.com/Arize-ai/openinference).

Map Vera's Run protocol to OpenTelemetry spans and metrics, then optionally add
OpenInference attributes. The Run record remains Vera's durable control authority;
spans are portable observations. Export must support redaction, sampling,
content-off mode, stable correlation, and arbitrary OTLP collectors. Do not
hard-wire Langfuse, Phoenix, MLflow, or another product SDK into every capability.

### Agent2Agent protocol

A2A is an open standard for communication between independent, potentially opaque
agent systems. It models messages, tasks, and artifacts rather than requiring the
parties to share an agent framework.

Source: [A2A specification](https://google-a2a.github.io/A2A/specification/).

Implement client and server adapters around Capability v2, Workflow IR, Run, and
ArtifactRef. Remote discovery is untrusted input, authentication is explicit,
remote artifacts are verified, and side effects remain locally authorized. A2A
and MCP stay distinct: MCP exposes tools/resources; A2A represents an independent
agent and task lifecycle.

## P1 pilots

### Evaluation: DeepEval and Promptfoo

DeepEval documents end-to-end, trajectory, and component evaluation for agents,
tools, conversations, RAG, and MCP. Promptfoo is a local CLI/library for evaluation
and red teaming that can run in CI across providers.

Sources: [DeepEval introduction](https://deepeval.com/docs/introduction),
[agent evaluation](https://deepeval.com/docs/getting-started-agents),
[Promptfoo](https://www.promptfoo.dev/docs/intro/), and
[Promptfoo red teaming](https://www.promptfoo.dev/docs/guides/llm-redteaming/).

Define neutral `EvalSuite`, `EvalCase`, `Scorer`, `EvaluationRun`, and
`EvaluationReport` records first. The tools consume frozen Run traces and fixtures;
they do not define canonical Vera task identity or silently upload content. Judge
calls use the normal resolver and gate, with cost and provenance recorded.

### Structured generation: Instructor and Outlines

Instructor emphasizes typed outputs with validation and retry using error context.
Outlines supports constrained JSON generation from Pydantic, JSON Schema, or a
function signature for compatible local models.

Sources: [Instructor validation](https://python.useinstructor.com/blog/2025/05/20/understanding-semantic-validation-with-structured-outputs/)
and [Outlines JSON generation](https://dottxt-ai.github.io/outlines/reference/generation/json/).

Both belong behind `StructuredGenerationProvider`. Provider-native JSON schema,
Instructor correction, Outlines constrained decoding, and Vera's current JSON
retry behavior compete through that contract. Retries are bounded Run child steps
with their own cost and error evidence.

### LiteLLM

LiteLLM documents normalized access to many providers, a proxy/gateway, retry and
fallback routing, cost tracking, budgets, authentication, and rate limiting.

Source: [LiteLLM documentation](https://docs.litellm.ai/).

Use it as one `InferenceProvider`; Vera retains policy, locality, ModelPackage
eligibility, task resolution, and evaluation. Avoid double retries by assigning
one retry owner per request. Test SDK and proxy separately; prefer the isolated
proxy boundary first rather than importing its provider dependency graph into the
core process.

### Docling

Docling converts broad document and media formats into a unified representation
and exports JSON, Markdown, text, chunks, and other formats.

Sources: [Docling formats](https://github.com/docling-project/docling/blob/main/docs/usage/supported_formats.md)
and [DocumentConverter](https://docling-project.github.io/docling/reference/document_converter/).

A `DocumentParser` adapter accepts an ArtifactRef and emits record envelopes for
structure, text, tables, media, page coordinates, and derived assets. Preserve the
original, parser version/configuration, conversion status, and deterministic
element IDs. Docling is the first pilot; compare Unstructured later against the
same frozen corpus. Source: [Unstructured partitioning](https://docs.unstructured.io/open-source/core-functionality/partitioning).

### DSPy

DSPy treats model programs as modules and optimizes prompts and/or weights against
a metric and examples.

Sources: [DSPy](https://dspy.ai/) and
[DSPy optimizers](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/optimization/optimizers.md).

Use DSPy only as `OptimizerProvider`. Inputs are a versioned PromptPackage or
Workflow fragment, EvalSuite, budget, eligible models, and policy. Outputs are
immutable candidates, traces, costs, and an EvaluationReport. Promotion remains a
Vera registry decision. Start with bounded structured extraction, not Dream or an
open-ended agent.

### MLflow interoperability

MLflow documents GenAI tracing/evaluation, prompt evaluation and registry,
datasets, model lifecycle, and agent-oriented evaluation workflows.

Sources: [MLflow GenAI evaluation](https://mlflow.github.io/mlflow-website/docs/latest/genai/eval-monitor/),
[prompt evaluation](https://mlflow.github.io/mlflow-website/docs/latest/genai/prompt-registry/evaluate-prompts/),
and [MLflow cookbook](https://mlflow.org/cookbook/).

Do not replace Vera Run, Artifact, PromptPackage, or ModelPackage IDs with MLflow
IDs. Build an exporter/importer with an identity map. Export training runs,
evaluations, prompts, parameters, metrics, and artifacts; import pinned candidates
as untrusted registry proposals with verified hashes and source revisions.

### Qdrant

Qdrant supports dense+sparse hybrid queries, named vectors, multistage prefetch,
fusion, multivectors, filtering, and reranking patterns. It is a strong test of
whether `VectorIndexProvider` is genuinely portable beyond Chroma and FAISS.

Sources: [Qdrant hybrid search](https://qdrant.tech/documentation/search/text-search/hybrid-search/)
and [hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/).

Index a read-only snapshot with stable Vera record/revision IDs. Record the dense,
sparse, and reranker ModelPackages, dimensions, metrics, chunking, fusion, and
build ID. Compare quality and latency with Fabric; canonical records never move.

### Hugging Face PEFT

PEFT provides parameter-efficient adaptation and integrates with Transformers,
Diffusers, and Accelerate.

Source: [PEFT documentation](https://huggingface.co/docs/peft/index).

Integrate it as a training adapter after ModelPackage and TrainingRun exist. Each
adapter or LoRA is a derived package with base-model revision, dataset snapshot,
method/config, framework versions, precision, evaluation, license, and hardware
requirements. Current vLLM LoRA strings should become deployments of registered
derived packages rather than independent state.

## P2 and comparison pilots

### LlamaIndex and Haystack

LlamaIndex focuses on agents over data, connectors, RAG, and event-driven
workflows. Haystack provides components, document stores, agents, tools, and
directed multigraph pipelines with loops and branches.

Sources: [LlamaIndex](https://llamaindex.openml.io/),
[Haystack](https://docs.haystack.deepset.ai/), and
[Haystack pipelines](https://docs.haystack.deepset.ai/docs/pipelines).

Both overlap substantially with Fabric, Context, agents, and Workflow IR. Define
only two experiments: import/export a supported workflow while emitting Run, and
expose selected reader/retriever/store components as providers. Retain useful
connectors without adopting an entire second workflow and storage model.

### Microsoft Agent Framework

Microsoft documentation presents agent abstractions, plugins/function calling,
threads, orchestration, and human collaboration across its ecosystem.

Sources: [Semantic Kernel agents](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/)
and [Microsoft Agent Framework](https://learn.microsoft.com/en-gb/agent-framework/).

Track it as a RuntimeAdapter candidate, especially for cross-language and
Microsoft-system interoperability. Avoid a deep adapter until current migration
and declarative semantics map honestly. A2A may deliver practical interoperability
sooner with less coupling.

### GraphRAG

Microsoft GraphRAG indexes unstructured text into graph data and queries completed
indexes, including local search combining knowledge-graph data with text chunks.

Sources: [GraphRAG indexing](https://microsoft.github.io/graphrag/index/overview/)
and [GraphRAG query](https://microsoft.github.io/graphrag/query/overview/).

Run it only as an offline Index/Evidence experiment over a frozen Fabric snapshot.
Compare it with entity graphs, hybrid retrieval, and Worldview. Record all indexing
model calls and cost; never treat extracted graph claims as source truth.

### Ray Serve and BentoML

Ray Serve provides framework-agnostic composition, autoscaling, streaming, and
multi-model/multi-node deployment. BentoML provides Python model-service packaging
and deployment across models and clouds.

Sources: [Ray Serve](https://docs.ray.io/en/latest/serve/index.html),
[Ray Serve LLM](https://docs.ray.io/en/latest/serve/llm/index.html), and
[BentoML](https://docs.bentoml.com/en/latest/).

Both implement `DeploymentProvider`; neither defines ModelPackage. Pilot BentoML
first for a simple reproducible service bundle, or Ray Serve first only when a
measured requirement needs multi-node composition or autoscaling. Do not operate
both before a real scenario distinguishes them.

### Observability backends

Langfuse covers tracing, prompt management, evaluations, datasets, experiments,
and analytics. Phoenix receives OpenTelemetry/OpenInference spans and provides an
open-source observability/evaluation backend.

Sources: [Langfuse](https://langfuse.com/docs) and
[Phoenix](https://arize.com/docs/phoenix/).

Export the same redacted portable telemetry to each; do not add backend-specific
instrumentation throughout Vera. Compare self-hosting cost, trace usefulness,
evaluation workflow, retention, access control, portability, and failure impact.
Export failure must never fail a user task.

### Guardrails and Ragas

Guardrails AI combines input/output guards with structured validation and a
validator ecosystem. Ragas focuses on RAG evaluation.

Sources: [Guardrails AI](https://guardrailsai.com/guardrails/docs) and
[Ragas](https://aclanthology.org/2024.eacl-demo.16.pdf).

Expose validators and metrics as optional provider packs. Vera retains policy,
effect authorization, evidence, failure mode, and audit. Third-party validators
must declare model calls, egress, latency, and measured errors.

## Libraries not selected for dedicated work yet

- More agent frameworks such as CrewAI and legacy AutoGen should first use A2A,
  MCP, OpenAPI, or RuntimeAdapter. Vera needs conformance evidence more than
  another named panel.
- More vector databases wait until Qdrant proves the provider contract.
- More observability suites use OTLP/OpenInference and require no Vera-specific
  code unless they demonstrate a unique feature.
- SGLang, TensorRT-LLM, llama.cpp servers, and Kubernetes serving stacks enter
  through InferenceProvider or DeploymentProvider when a concrete gap exists.
- Broad all-in-one frameworks do not become core dependencies merely because Vera
  wants one of their features; integrate the narrow feature or protocol boundary.

## Cross-cutting adapter requirements

Every pilot must provide:

- pinned version or image digest and upstream source/license record;
- isolated optional dependency group or container, not an unconditional core
  import;
- feature negotiation with explicit unsupported semantics;
- Vera IDs plus native-ID mappings for runs, records, artifacts, prompts, models,
  deployments, and tasks;
- schemas, limits, pagination, timeout, cancellation, retry, idempotency, health,
  and teardown behavior;
- declared filesystem, network, accelerator, secret, and egress requirements;
- Run events and portable telemetry with content redaction controls;
- conformance fixtures and a baseline comparison;
- activation, rollback, upgrade, export, and removal procedures;
- no new default model-discovery surface until selection quality is measured.

## Bounded Loop Lab backlog

### LIB-01 — portable GenAI telemetry

Map Run events to OpenTelemetry/OpenInference spans and a local collector fixture.

Gate: golden spans, correlation, error/cancel/retry, redaction, exporter failure
isolation, and bounded overhead.

### LIB-02 — A2A client/server adapter

Map agent cards, messages, tasks, status, and artifacts to Capability v2, Run, and
ArtifactRef. Start with discovery and one non-mutating task.

Gate: authentication/policy, cancellation, duplicates, disconnect/resume, artifact
verification, malicious metadata, and unsupported parts.

### LIB-03 — EvalProvider contract

Implement neutral evaluation records and frozen fixtures, then adapt DeepEval and
Promptfoo without activating judge calls by default.

Gate: deterministic scorers, judge provenance/cost, CI behavior, redaction,
timeout, partial reports, and stable case identity.

### LIB-04 — structured generation providers

Adapt provider-native schema generation, Instructor, and Outlines behind one
contract.

Gate: valid/invalid schemas, unsupported constructs, retry budget, cancellation,
streaming, latency, and semantic-validator failures.

### LIB-05 — LiteLLM provider

Run a pinned proxy and import health/model/usage. Disable provider fallback first
so Vera can observe one retry owner at a time.

Gate: schema/stream parity, tools, structured outputs, errors, rate limits,
usage/cost, secrets, attempt provenance, and outage.

### LIB-06 — Docling parser

Convert a frozen document corpus in isolation and emit records plus artifacts.

Gate: text/table/layout fidelity, stable IDs, citations, corrupt/encrypted files,
resource ceilings, OCR declaration, cancellation, and teardown.

### LIB-07 — DSPy optimizer

Optimize one structured-extraction PromptPackage against held-out cases. Produce
candidates only; never activate automatically.

Gate: held-out improvement, cost/time, reproducibility, overfitting, provenance,
cancellation, and rollback.

### LIB-08 — MLflow bridge

Export one TrainingRun, EvaluationReport, PromptPackage, and ModelPackage; import
them as proposals with native-ID mappings.

Gate: hashes, revisions, aliases, missing artifacts, incompatible schemas, access
policy, round trip, and no authority inversion.

### LIB-09 — Qdrant vector adapter

Build dense, sparse, hybrid, and multivector projections for a frozen snapshot and
compare them with current retrieval.

Gate: stable record IDs, rebuild, filters, recall/precision, p50/p95 latency,
resources, outage fallback, deletion projection, and reconciliation.

### LIB-10 — PEFT training adapter

Produce one derived ModelPackage and deploy it through an existing LoRA-capable
provider without using an unregistered path string.

Gate: base revision, dataset provenance, reproducibility, quality/safety delta,
storage/resources, license, load/unload, and rollback.

### LIB-11 — data/RAG framework comparison

Use one frozen ingestion/query workflow with LlamaIndex and Haystack. Import/export
only the supported semantic subset.

Gate: Workflow IR gaps, record/citation fidelity, retrieval quality, latency,
dependency weight, teardown, and no duplicate authority.

### LIB-12 — scalable serving decision

Run a ModelPackage through BentoML or Ray Serve based on a documented scale
scenario, then decide whether the second candidate is necessary.

Gate: packaging, cold start, streaming, batching, autoscaling if required, health,
recovery, resource placement, provenance, and rollback.

### LIB-13 — observability backend comparison

Send the same redacted OTLP/OpenInference fixture to Langfuse and Phoenix in
isolated deployments.

Gate: trace fidelity, query/UI use, evaluation linkage, retention, access,
resource cost, export portability, outage isolation, and teardown.

### LIB-14 — GraphRAG/Worldview comparison

Build GraphRAG over a small frozen Fabric snapshot and compare graph retrieval with
current graph and Worldview evidence paths.

Gate: answer/citation quality, indexing cost/time, incremental update, drift,
provenance, and deletion/rebuild. Keep experimental until a second domain repeats
the result.

## Sequencing

Recommended order:

1. LIB-01 and the neutral portion of LIB-03.
2. LIB-02 and LIB-04.
3. LIB-05 and LIB-06.
4. LIB-07 through LIB-10 after evaluation and model/record contracts are active.
5. LIB-11 through LIB-14 only after narrower adapters establish baselines.

Do not run multiple framework or serving pilots concurrently merely to save time.
Their value depends on stable fixtures and comparable telemetry, and Vera's shared
model gate must remain coordinated.

## Queued live-test matrix

This entire section is intentionally inactive until explicit authorization.

| Test class | Applies to | Evidence captured |
| --- | --- | --- |
| Installation | all | pinned lock/digest, dependency delta, CVE/license scan, disk/time |
| Contract | all | supported features, schemas, unsupported-feature honesty |
| Functional | all | task success and exact output/artifact/citation identity |
| Quality | eval/RAG/optimization/models | held-out metrics and human rubric where needed |
| Performance | runtimes | cold/warm p50/p95, throughput, tokens, CPU/RAM/GPU, storage |
| Cost | model/judge/index | calls, tokens, provider cost, indexing/training budget |
| Safety | agent/tool/parser/gateway | injection, egress, secrets, archive bombs, policy bypass |
| Reliability | services/runtimes | timeout, cancellation, rate limit, crash, restart, disconnect, retry |
| Consistency | data/models | idempotency, revision mapping, reconciliation, export/delete |
| Upgrade | all | old/new compatibility, migration, downgrade or rollback |
| Removal | all | deactivate, remove env/config/state, preserve canonical records |

No benchmark uses production data or mutates production. Model/judge tests acquire
the shared gate and run sequentially. External network and hosted-service calls
require explicit configuration and egress approval. Results become
EvaluationReports linked to exact source, package, fixture, and Run IDs.

## Adoption decision template

```text
Candidate:
Pinned version/digest:
Vera contract:
Unique value demonstrated:
Baseline and fixture versions:
Correctness/quality result:
Latency/resource/cost result:
Security and data boundary:
Unsupported semantics:
Operational owner:
Decision: adopt | retain experimental | defer | reject
Activation scope:
Rollback/removal:
Next review date:
```

“Adopt” means Vera maintains an adapter and conformance suite. It does not mean the
library becomes Vera's internal architecture or dozens of default capabilities.

## Round 2 — durable execution, newer agent SDKs, and AI data infrastructure

This follow-on scan broadens the portfolio without changing the queued-test rule.
It reviewed official documentation for DBOS, Temporal, Prefect, Dagster, Google
ADK, OpenAI Agents SDK, Strands, Agno, Hugging Face Datasets and Accelerate, DVC,
SGLang, llama.cpp, Lance, and DuckDB vector search. Repository search found no
meaningful Vera integration for these candidates. DuckDB appears in a sandbox
component catalog, which means it can be installed; that is not a Data/Query
provider integration.

No package, image, workflow service, dataset, or model was executed.

### Round 2 portfolio additions

| Candidate | Category | Priority | Vera integration shape | Decision |
| --- | --- | --- | --- | --- |
| DBOS | Durable execution | P1 experimental | Workflow IR runtime adapter over Postgres | First durability spike |
| Temporal | Durable execution | P2/reference | Workflow IR runtime adapter via service and Python SDK | Cross-service reference; do not deploy with DBOS initially |
| Prefect | General/data workflows | Watch | Workflow IR import/export adapter | Overlaps DAG/scheduling; no dedicated pilot yet |
| Dagster | Data orchestration | P2 conditional | Asset/lineage adapter for Fabric datasets and artifacts | Consider only for data-asset product requirements |
| Google ADK | Agent SDK | P2 comparison | RuntimeAdapter plus A2A | Add to common agent conformance matrix |
| OpenAI Agents SDK | Agent SDK | P2 comparison | RuntimeAdapter plus trace/guardrail projection | Useful Responses/handoff ecosystem; not a kernel |
| Strands Agents | Agent SDK | Watch/P2 | RuntimeAdapter | Pilot only with an AWS/Bedrock requirement |
| Agno | Agent platform | Watch | Prefer A2A or generic RuntimeAdapter | Decline deep integration due broad overlap |
| Hugging Face Datasets | Dataset access | P1 | DatasetProvider and SourcePackage adapter | Arrow/streaming ecosystem access with pinned revisions |
| Hugging Face Accelerate | Distributed training | P2 | TrainingRuntime adapter | Use when a TrainingRun needs distributed execution |
| DVC | Data versioning | P2 | External Dataset/Artifact registry adapter | Import/export pointers; do not adopt DVC pipelines as another engine |
| llama.cpp server | Inference | P1 | InferenceProvider for GGUF and CPU/edge placement | Clear local/edge gap alongside Ollama/vLLM |
| SGLang | Inference | P2 conditional | InferenceProvider | Pilot only against a measured vLLM feature/performance gap |
| Lance format | Multimodal data | P2 experimental | Dataset/Artifact format adapter | Open format experiment for multimodal snapshots |
| DuckDB | Analytical query | P1 | QueryProvider over artifacts/dataset snapshots | Strong local analytical seam; vector extension remains experimental |

## Durable execution decision

Durability is more valuable to Vera than another agent framework. Current native
systems independently implement persistence, queues, retry, schedule, resume, and
recovery. A durable runtime could replace those mechanics while preserving Dream,
Evolve, DAG, Calendar, and Agents as product policies.

### DBOS first

DBOS documents workflows that resume from the last completed step, durable queues
and sleeps, workflow IDs used as idempotency keys, cancellation/timeouts, and
Postgres-backed execution. It distinguishes workflow errors from retriable step
failures and documents version-aware recovery.

Sources: [DBOS overview](https://docs.dbos.dev/),
[workflow semantics](https://docs.dbos.dev/python/tutorials/workflow-tutorial),
and [architecture](https://docs.dbos.dev/architecture).

DBOS is the first spike because its application-library and Postgres shape matches
Vera's present deployment better than introducing a separate cluster service. It
is still an external runtime, not the Workflow IR definition or Run authority.

The adapter must map:

- Workflow IR workflow/run/step IDs to DBOS workflow/function IDs;
- Run states to DBOS status without inventing exactly-once guarantees for external
  side effects;
- idempotency key, retry owner, timeout, cancel, durable sleep, queues, and signals;
- code/application version to Workflow IR definition and implementation revisions;
- step result/artifact references without persisting large or sensitive values in
  workflow state;
- DBOS recovery observations back into Run events.

The first task is a non-LLM, non-mutating workflow interrupted between deterministic
steps. A later fixture performs one idempotent external effect with a receipt. Do
not start with agent model calls, Dream, or Evolve promotion.

### Temporal as reference, not a simultaneous deployment

Temporal documents durable Workflows, Activities, Workers, service-based recovery,
messages, schedules, versioning, observability, and a Python test framework.

Sources: [Temporal documentation](https://docs.temporal.io/),
[Python developer guide](https://github.com/temporalio/documentation/blob/main/docs/develop/python/index.mdx),
and [Python SDK reference](https://python.temporal.io/).

Temporal is the comparison target when Vera needs cross-service, long-lived,
multi-worker durability beyond the DBOS deployment shape. Build a semantic mapping
on paper now; do not operate both systems until DBOS conformance exposes a concrete
gap. A replacement decision must compare determinism constraints, versioning,
signals/updates, child workflows, scheduling, retention, operations, recovery,
and migration of in-flight runs.

### Prefect and Dagster boundaries

Prefect supplies flow/task retry and general workflow operations. Dagster is a
data orchestrator centered on assets, lineage, observability, and testability.

Sources: [Prefect retries](https://docs.prefect.io/v3/how-to-guides/workflows/retries)
and [Dagster overview](https://docs.dagster.io/).

Neither should become a third native Vera engine. Prefect remains a Workflow IR
adapter candidate for external definitions. Dagster becomes interesting only if
Fabric users need asset-centric materialization, lineage, and data quality that
Vera cannot economically provide. In that case, map Dagster assets to immutable
record/dataset/artifact revisions and Runs; do not duplicate their state in Fabric.

## Newer agent SDK decision

### Common rule

Agent frameworks are interchangeable runtimes, not Vera subsystems. They enter via
A2A when remote, or RuntimeAdapter when embedded/containerized. All receive the
same task corpus, Capability v2 tools, model resolver, policy boundary, Run events,
cancellation, artifact handling, and teardown tests.

### Google ADK

Google ADK documents agent/tool development, multi-agent orchestration, graph
workflows, evaluation, and deployment across multiple languages and cloud targets.

Source: [Google ADK](https://adk.dev/).

Add it to the agent conformance matrix after A2A. Prefer its A2A surface for remote
interoperation. A direct RuntimeAdapter is justified only for features unavailable
through A2A, and those features must be declared as native extensions rather than
silently added to Workflow IR.

### OpenAI Agents SDK

The OpenAI Agents SDK documents an Agent/Runner loop with tools, handoffs, sessions,
guardrails, structured outputs, lifecycle hooks, and tracing. Its run configuration
can override models/providers and session behavior.

Sources: [Agents](https://openai.github.io/openai-agents-python/agents/),
[running agents](https://openai.github.io/openai-agents-python/running_agents/),
and [tracing](https://openai.github.io/openai-agents-js/guides/tracing/).

Implement it as a provider-aware RuntimeAdapter. Map its trace hierarchy to Run and
portable telemetry; never emit sensitive traces by default. Map handoffs to child
runs/messages and guardrails to policy/validator evidence, but retain Vera as final
side-effect authority. Test a non-OpenAI model provider where supported so the
adapter does not accidentally hard-code vendor identity into Workflow IR.

### Strands and Agno

AWS documentation describes Strands as an open-source agent SDK with model APIs,
tools, and multi-agent patterns. Agno presents agents, teams, workflows, memory,
knowledge, evaluation, and an AgentOS runtime that can wrap other frameworks.

Sources: [Strands guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-frameworks/strands-agents.html)
and [Agno documentation](https://docs.agno.com/).

Strands is conditional on a real Bedrock/AWS task. Agno overlaps almost every Vera
layer, so prefer its external protocol/runtime boundary and decline a deep native
integration. Neither gets a bespoke panel or default capability family.

## AI data and training infrastructure

### Hugging Face Datasets

Hugging Face Datasets supports local and remote formats, Arrow-backed data,
streaming, memory mapping, Parquet, and newer Lance access paths.

Sources: [loading datasets](https://github.com/huggingface/datasets/blob/main/docs/source/loading.mdx)
and [streaming](https://huggingface.co/docs/datasets/en/stream).

Add `DatasetProvider`, not a second Fabric. An imported dataset becomes a pinned
SourcePackage plus a Fabric dataset snapshot. Record upstream repository/revision,
config, split, file hashes, schema/features, license/card, streaming cursor, cache,
and transformations. Remote loading is data egress/ingress subject to policy;
unreviewed dataset code must not execute in Vera's core process.

### Accelerate

Hugging Face Accelerate offers a unified interface and launcher for distributed
PyTorch training and inference, including mixed precision, FSDP, DeepSpeed, large
model loading, and multiple hardware platforms.

Source: [Accelerate documentation](https://huggingface.co/docs/accelerate/index).

Use it as a TrainingRuntime selected by a TrainingRun, not as a new training
schema. Its resolved configuration, launcher command, environment, topology,
precision, checkpoints, profiler output, and failures belong in Run/Artifact
records. A container or isolated environment owns the dependency stack.

### DVC

DVC uses Git-adjacent metadata and external caches/remotes to version data and
models, and also provides pipeline and experiment features.

Sources: [DVC home](https://www.dvc.org/) and
[command/workflow reference](https://dvc.org/doc/command-reference/).

Vera should import/export DVC-tracked artifacts and dataset revisions through its
repository intake. Preserve DVC hash, remote, path, Git commit, and stage metadata.
Do not adopt `dvc.yaml` execution as another native pipeline: optionally compile a
supported subset to Workflow IR, or invoke DVC as an isolated CLI adapter with
declared filesystem/network effects.

### Lance and DuckDB

Lance is an open lakehouse format and catalog direction for multimodal AI data on
object storage, with vector/full-text search, random access, transactions, time
travel, and integrations. DuckDB provides local analytical SQL; its VSS extension
adds experimental HNSW vector indexing.

Sources: [Lance format](https://lance.org/),
[DuckDB VSS](https://duckdb.org/docs/lts/core_extensions/vss), and
[DuckDB extension tiers](https://duckdb.org/docs/current/core_extensions/overview).

Treat Lance format separately from LanceDB as a service/library product. A format
spike should store a frozen multimodal dataset artifact and prove schema, identity,
time-travel revision, portability, and cleanup. Do not make it canonical until
transaction, evolution, repair, and ecosystem behavior are tested.

DuckDB is a strong `QueryProvider` for local snapshots and artifacts. It may query
Parquet/Arrow or other approved formats without importing all data into a long-lived
service. The VSS extension is explicitly experimental/secondary in official docs;
keep vector indexing experimental and rebuildable, never the only copy.

## Inference engine additions

### llama.cpp

llama.cpp provides local GGUF inference and an OpenAI-compatible server with chat,
responses, embeddings, quantized CPU/GPU execution, grammar constraints, and other
serving features.

Sources: [llama.cpp project](https://github.com/ggml-org/llama.cpp/blob/master/README.md)
and [server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

This is a P1 InferenceProvider because it covers CPU-heavy, small-node, edge, and
GGUF deployment scenarios not cleanly represented by vLLM. Ollama may already use
related formats internally, but provider conformance matters: Vera should be able
to operate llama.cpp directly when it reduces overhead or exposes needed controls.

Import GGUF files as ModelPackages with checksum, quantization, architecture,
tokenizer/template, license, source revision, context/resource estimates, and
supported tasks. Start the server through DeploymentProvider with explicit argv,
ports, resources, health, and teardown; never interpolate shell commands or accept
an unverified arbitrary model path.

### SGLang

SGLang documents high-performance serving for language and vision-language models,
structured outputs, prefix caching, batching, speculative decoding, quantization,
multi-LoRA, parallelism, metrics, tracing, and multi-node deployment.

Source: [SGLang documentation](https://docs.sglang.io/).

Keep it P2 until a frozen workload identifies a vLLM gap in model support,
structured generation, multimodal behavior, prefix reuse, LoRA batching, or
throughput/latency. Use the identical InferenceProvider conformance suite. A win on
one benchmark does not justify replacing vLLM globally; routing may retain both for
different eligible deployments.

## Round 2 bounded work units

### LIB-15 — durable runtime semantic fixture

Implement a runtime-neutral durability fixture before adding DBOS: deterministic
steps, durable wait, retry, timeout, cancel, idempotent external effect, crash at
every boundary, version change, resume, and complete Run events.

Gate: the fixture describes expected behavior without referencing a vendor and
fails current adapters that claim unsupported guarantees.

### LIB-16 — DBOS Workflow IR adapter

Compile the LIB-15 workflow to DBOS and map native state/events to Run. Use an
isolated database schema and no model calls.

Gate: restart/recovery at every boundary; idempotency; retry ownership; cancellation;
version mismatch; state/artifact limits; database loss behavior; full teardown.

### LIB-17 — Temporal paper adapter and decision gate

Map LIB-15 semantics to Temporal Workflows/Activities/messages/versioning without
deploying it. List every mismatch with DBOS and Vera.

Gate: approve a live Temporal pilot only if a named distributed/longevity/versioning
requirement cannot be satisfied safely by DBOS.

### LIB-18 — agent SDK conformance expansion

Add Google ADK and OpenAI Agents SDK to the existing RuntimeAdapter/A2A test matrix.
Keep Strands optional and Agno protocol-only initially.

Gate: task/tool correctness, model-provider substitution, handoff/child identity,
policy enforcement, structured output, sessions, cancel/recovery, portable traces,
dependency isolation, and teardown.

### LIB-19 — Hugging Face DatasetProvider

Import one small pinned dataset and stream one larger public fixture through an
isolated adapter into record/snapshot manifests. No training occurs.

Gate: revision/hash/license/card, split/schema, streaming resume, cache limits,
offline replay, malicious builder prevention, export, and removal.

### LIB-20 — distributed TrainingRuntime contract

Define launcher/topology/precision/checkpoint/progress semantics and map Accelerate
configuration without running a distributed job.

Gate: static config validation, hardware eligibility, secrets/environment allowlist,
cancellation, checkpoint ArtifactRefs, and unsupported topology disclosure.

### LIB-21 — DVC repository adapter

Inspect and import one DVC-tracked artifact from a local fixture repository, then
export a Vera ArtifactRef mapping. Do not execute its pipeline.

Gate: Git/DVC identity, remote and credential boundary, missing cache, hash mismatch,
path traversal, offline behavior, export round trip, and no worktree mutation.

### LIB-22 — DuckDB QueryProvider

Run read-only SQL over frozen Parquet/Arrow artifact fixtures through an isolated
DuckDB adapter. Keep extension installation disabled initially.

Gate: read-only enforcement, SQL/schema types, memory/time/output limits,
cancellation, artifact provenance, malicious files, concurrency, and teardown.

### LIB-23 — Lance format experiment

Write and read one frozen multimodal dataset revision through object storage and
compare with Parquet plus ArtifactRefs.

Gate: schema evolution, random access, media fidelity, checksums, transaction/time
travel behavior, reader portability, storage/latency, corruption, and removal.

### LIB-24 — llama.cpp InferenceProvider

Register one small pinned GGUF ModelPackage and server deployment in an isolated
container. This work unit remains queued with all other live model tests.

Gate: model metadata, CPU/GPU placement, chat/responses/embedding/structured output,
stream/cancel, concurrency, cold/warm latency, memory, health, crash/restart, and
complete server/model teardown.

### LIB-25 — SGLang admission benchmark

Define, but do not run, a benchmark that isolates candidate advantages over vLLM:
structured output, shared-prefix workload, multimodal input, LoRA mix, and one
multi-node scenario only if hardware exists.

Gate: no SGLang deployment unless at least one important workload improves enough
to cover added operational cost while maintaining quality, policy, and provenance.

## Revised sequencing after Round 2

1. Complete portable Run telemetry and EvalProvider foundations (LIB-01, LIB-03).
2. Define the runtime-neutral durability fixture (LIB-15).
3. Run the DBOS adapter spike (LIB-16) before migrating a Vera product workflow.
4. Keep Temporal at the paper-decision gate (LIB-17) until a specific gap appears.
5. Build A2A before adding Google ADK/OpenAI SDK comparisons (LIB-02, LIB-18).
6. Run data adapters in the order DatasetProvider, read-only DuckDB, DVC, then
   Lance format (LIB-19, LIB-22, LIB-21, LIB-23).
7. Define TrainingRuntime before Accelerate/PEFT execution (LIB-20, then LIB-10).
8. Test llama.cpp only after ModelPackage and InferenceProvider contracts exist;
   SGLang remains an admission benchmark (LIB-24, LIB-25).

Every executable item in this sequence remains queued until live testing is
explicitly authorized. Static contract/schema work can proceed independently in
future Loop Lab work units.
