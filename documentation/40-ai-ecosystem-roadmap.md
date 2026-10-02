# Modern AI ecosystem roadmap

This page describes where Vera stands relative to the wider AI tooling
ecosystem: which modern libraries and standards Vera already integrates, which
meaningful gaps remain, how those gaps are prioritised, and what "integrated"
has to mean before Vera depends on something. It is an adoption portfolio, not a
dependency shopping list. The guiding rule is that Vera integrates **protocols
and narrow provider contracts** before adding more agent frameworks, and that no
external library is allowed to bring its own orchestration, storage, policy, or
registry semantics into Vera's kernel.

The portfolio was built from primary project documentation (refreshed in August
2026) and a static search of Vera's source. Since then, many of the selected
boundaries have shipped as **offline contracts**: deterministic, non-executing
adapters, mappings, and evidence formats that live in
[`vera/execution/`](../vera/execution/), [`vera/providers/`](../vera/providers/),
[`vera/models/`](../vera/models/), [`vera/fabric/`](../vera/fabric/), and
[`vera/agentbridges/`](../vera/agentbridges/). None of the candidate third-party
packages is installed or imported by Vera core. Live validation of every
candidate (installation, real runs, measurement, teardown) remains queued until
it is explicitly authorised.

## Contents

- [Summary](#summary)
- [How to read this page](#how-to-read-this-page)
  - [Priority tiers](#priority-tiers)
  - [Status vocabulary](#status-vocabulary)
- [What Vera already integrates](#what-vera-already-integrates)
- [Priority portfolio](#priority-portfolio)
- [Standards](#standards)
  - [OpenTelemetry GenAI and OpenInference](#opentelemetry-genai-and-openinference)
  - [Agent2Agent protocol](#agent2agent-protocol)
- [Provider pilots](#provider-pilots)
  - [Evaluation: DeepEval and Promptfoo](#evaluation-deepeval-and-promptfoo)
  - [Structured generation: Instructor and Outlines](#structured-generation-instructor-and-outlines)
  - [LiteLLM](#litellm)
  - [Docling](#docling)
  - [DSPy](#dspy)
  - [MLflow interoperability](#mlflow-interoperability)
  - [Qdrant](#qdrant)
  - [Hugging Face PEFT](#hugging-face-peft)
- [Comparison pilots](#comparison-pilots)
  - [LlamaIndex and Haystack](#llamaindex-and-haystack)
  - [Microsoft Agent Framework](#microsoft-agent-framework)
  - [GraphRAG](#graphrag)
  - [Ray Serve and BentoML](#ray-serve-and-bentoml)
  - [Observability backends](#observability-backends)
  - [Guardrails and Ragas](#guardrails-and-ragas)
- [Durable execution](#durable-execution)
  - [DBOS first](#dbos-first)
  - [Temporal as reference](#temporal-as-reference)
  - [Prefect and Dagster boundaries](#prefect-and-dagster-boundaries)
- [Agent SDKs](#agent-sdks)
- [Data and training infrastructure](#data-and-training-infrastructure)
  - [Hugging Face Datasets](#hugging-face-datasets)
  - [Accelerate](#accelerate)
  - [DVC](#dvc)
  - [Lance and DuckDB](#lance-and-duckdb)
  - [Portable memory providers](#portable-memory-providers)
- [Inference engines](#inference-engines)
  - [llama.cpp](#llamacpp)
  - [SGLang](#sglang)
- [Libraries not selected for dedicated work](#libraries-not-selected-for-dedicated-work)
- [Implementation status at a glance](#implementation-status-at-a-glance)
- [Cross-cutting adapter requirements](#cross-cutting-adapter-requirements)
- [Acceptance gates by adapter](#acceptance-gates-by-adapter)
- [Recommended sequence](#recommended-sequence)
- [Queued live-test matrix](#queued-live-test-matrix)
- [Adoption decision template](#adoption-decision-template)
- [Related pages](#related-pages)

## Summary

The highest-value missing pieces, in priority order:

1. **OpenTelemetry GenAI conventions plus OpenInference** for portable traces.
2. **Agent2Agent (A2A)** for communication with independent agents.
3. **A standard evaluation adapter**, exercised first with DeepEval and Promptfoo.
4. **Structured generation adapters** for Instructor and Outlines.
5. **A LiteLLM provider/gateway adapter**, without outsourcing Vera's policy resolver.
6. **Docling** as the first richer document-conversion adapter.
7. **DSPy** as an experimental optimisation backend for evaluated prompts/programs.
8. **MLflow** interoperability for experiments, evaluations, prompts, and model records.
9. **Qdrant** as a conformance test for modern hybrid/multivector retrieval.
10. **Hugging Face PEFT** and one scalable serving adapter, once the model
    lifecycle can carry their provenance and deployment records.

LlamaIndex, Haystack, Microsoft Agent Framework, GraphRAG, Ray Serve, BentoML,
Langfuse, Phoenix, Guardrails AI, Ragas, and similar systems are valuable, but
Vera approaches them through interchangeable adapters and experiments rather
than adding their internal abstractions to the kernel.

Durability is considered more valuable than another agent framework: DBOS is the
first durable-execution candidate, with Temporal kept as a paper reference.

## How to read this page

### Priority tiers

| Tier | Meaning |
|---|---|
| **P0 contract** | Define now because many later integrations depend on it. |
| **P1 pilot** | Build one isolated adapter after its prerequisite contracts exist. |
| **P2 conditional** | Useful once scale or product evidence demonstrates need. |
| **Watch** | Support through generic protocols; no dedicated work yet. |
| **Decline deep integration** | May run externally, but Vera should not absorb its internal abstractions. |

Candidates are scored qualitatively on gap fit, interoperability, replacement
value, boundary clarity, operational cost, data exposure, reversibility, and
whether results can be evidenced.

### Status vocabulary

| Status | Meaning |
|---|---|
| **Offline contract shipped** | A deterministic, non-executing Vera boundary exists (schema, mapping, plan, or injected-driver adapter) with tests. The third-party package is not imported. |
| **Partially integrated** | Some real runtime path exists inside Vera. |
| **Planned** | Integration shape is defined; no Vera code yet. |
| **Queued live** | Real installation, execution, and measurement are defined but await explicit authorisation. |

An offline pass proves Vera's side of a contract. It never implies that the
external system has been run, measured, or activated.

## What Vera already integrates

Meaningful existing support: **LangGraph**, **PydanticAI**, and **smolagents**
(isolated container bridges), **OpenClaw**, **MCP** (client and server), **Ollama**,
**vLLM**, **ONNX** (export and runtime), **Chroma**, **FAISS**, **Neo4j**,
**Postgres**, S3-compatible object storage (Garage), and a generic model-provider
surface. Hugging Face Transformers is used optionally for research NLP, and the UI
refers to Hugging Face models and `.safetensors` assets.

The original static search found no meaningful implementation for DSPy,
LlamaIndex, Haystack, LiteLLM, Instructor, Outlines, MLflow, OpenTelemetry,
OpenInference, Langfuse, Phoenix, DeepEval, Ragas, Promptfoo, Qdrant, Ray Serve,
BentoML, Microsoft GraphRAG, Docling, Unstructured, A2A, PEFT, DBOS, Temporal,
Prefect, Dagster, Google ADK, OpenAI Agents SDK, Strands, Agno, Hugging Face
Datasets/Accelerate, DVC, SGLang, a direct llama.cpp provider, Lance, or a DuckDB
query provider. A word match alone is not an integration (generic uses of words
such as `guidance`, `outlines`, `haystack`, or `balance` were excluded). Several
of these now have offline Vera contracts; see
[Implementation status at a glance](#implementation-status-at-a-glance).

## Priority portfolio

| Candidate | Category | Priority | Vera integration shape | Current status |
|---|---|---|---|---|
| OpenTelemetry GenAI conventions | Standard | P0 | Run/Event exporter and importer mapping | Offline contract shipped; opt-in OTLP/HTTP JSON exporter |
| OpenInference | Instrumentation | P0 | Optional AI span projection over OpenTelemetry | Span kinds in the portable projection |
| A2A | Protocol | P0 | Agent card, task/message/artifact adapter | Offline mapping and plan contracts shipped; transports queued |
| DeepEval | Evaluation | P1 | EvalProvider over frozen Run traces | Frozen-projection import shipped; execution queued |
| Promptfoo | Evaluation/security | P1 | CLI/OCI EvalProvider and red-team adapter | Frozen-projection import shipped; execution queued |
| Instructor | Structured output | P1 | StructuredGenerationProvider | Static profile in the structured-generation contract |
| Outlines | Constrained decoding | P1 | StructuredGenerationProvider for compatible local runtimes | Static profile in the structured-generation contract |
| LiteLLM | Gateway/provider | P1 | InferenceProvider and usage/health adapter | Planned (generic OpenAI-compatible adapter exists) |
| Docling | Document intelligence | P1 | DocumentParser provider | Offline contract shipped; profile `not_imported` |
| DSPy | Optimisation | P1 experimental | OptimizerProvider consuming evaluations and prompt packages | Proposal-only optimiser contracts shipped |
| MLflow | Lifecycle/observability | P1 | Run/evaluation/prompt/model export and import | Planned |
| Qdrant | Retrieval | P1 | VectorIndexProvider | Injected-driver exact-snapshot adapter shipped; no package imported |
| Hugging Face PEFT | Training | P1/P2 | TrainingAdapter producing derived ModelPackages | Provider-neutral training contracts shipped |
| LlamaIndex | Data/RAG/agents | P2 | Data/Workflow adapters and selected readers | Planned |
| Haystack | RAG/pipelines | P2 | Component and pipeline import/export adapter | Planned |
| GraphRAG | Graph retrieval | P2 experimental | Offline Index/Evidence adapter over snapshots | Injected-runtime exact-snapshot boundary shipped |
| Ray Serve | Distributed serving | P2 | DeploymentProvider | Planned |
| BentoML | Model serving | P2 | DeploymentProvider | Planned |
| Langfuse | Observability/evals | P2 backend | OTLP/OpenInference export target | Comparison evidence contract shipped; deployment queued |
| Phoenix | Observability/evals | P2 backend | OTLP/OpenInference export target | Comparison evidence contract shipped; deployment queued |
| Guardrails AI | Validation/safety | P2 | ValidatorProvider | Planned |
| Ragas | RAG evaluation | P2 metric pack | EvalProvider metric extension | Planned |
| Microsoft Agent Framework | Agent runtime | Watch/P2 | RuntimeAdapter after API stabilisation | Watch |
| Unstructured | Document ingestion | Watch/alternative | DocumentParser provider | Later comparison against the Docling corpus |
| DBOS | Durable execution | P1 experimental | Workflow IR runtime adapter over Postgres | Static mapping shipped; executable spike queued |
| Temporal | Durable execution | P2/reference | Workflow IR runtime adapter via service and SDK | Static mapping and injected-runner plan shipped; live pilot deferred |
| Prefect | General/data workflows | Watch | Workflow IR import/export adapter | Watch |
| Dagster | Data orchestration | P2 conditional | Asset/lineage adapter for Fabric datasets | Conditional |
| Google ADK | Agent SDK | P2 comparison | RuntimeAdapter plus A2A | In the runtime matrix (prospective) |
| OpenAI Agents SDK | Agent SDK | P2 comparison | RuntimeAdapter plus trace/guardrail projection | In the runtime matrix (prospective) |
| Strands Agents | Agent SDK | Watch/P2 | RuntimeAdapter | In the runtime matrix (prospective) |
| Agno | Agent platform | Watch | Prefer A2A or generic RuntimeAdapter | In the runtime matrix (prospective) |
| Hugging Face Datasets | Dataset access | P1 | DatasetProvider and SourcePackage adapter | Offline adapter shipped (pinned to `datasets==4.8.4`) |
| Hugging Face Accelerate | Distributed training | P2 | TrainingRuntime adapter | Planned |
| DVC | Data versioning | P2 | External Dataset/Artifact registry adapter | Offline local-file adapter shipped |
| llama.cpp server | Inference | P1 | InferenceProvider for GGUF and CPU/edge placement | Planned |
| SGLang | Inference | P2 conditional | InferenceProvider | Admission benchmark defined, not run |
| Lance format | Multimodal data | P2 experimental | Dataset/Artifact format adapter | Planned |
| DuckDB | Analytical query | P1 | QueryProvider over artifacts/dataset snapshots | Offline adapter shipped (pinned to `duckdb==1.5.5`) |

## Standards

### OpenTelemetry GenAI and OpenInference

OpenTelemetry semantic conventions provide common meanings for telemetry
attributes. OpenInference complements OpenTelemetry with AI-focused conventions
and instrumentations spanning model, retrieval, and tool operations.

Sources: [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
and [OpenInference](https://github.com/Arize-ai/openinference).

**Shape.** Vera's Run protocol maps to OpenTelemetry spans, with optional
OpenInference attributes. The Run record remains Vera's durable control
authority; spans are portable observations. Export must support redaction,
sampling, content-off mode, stable correlation, and arbitrary OTLP collectors.
Langfuse, Phoenix, MLflow, or any other product SDK is not hard-wired into
capabilities.

**Current status.** Shipped in
[`vera/execution/portable_telemetry.py`](../vera/execution/portable_telemetry.py):
deterministic redacted span projection; an explicit, default-off OTLP/HTTP JSON
exporter; and a separately opt-in bounded terminal-Run queue with batching,
duplicate suppression, backpressure counters, capped transient retry, and
bounded shutdown flush. Configuration uses the standard
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT` (no endpoint
means offline), `..._PROTOCOL` (only `http/json` is accepted), `..._TIMEOUT`, and
`..._HEADERS`, plus `VERA_OTLP_MAX_REQUEST_BYTES` and `VERA_OTLP_AUTO_EXPORT`.
The frozen seven-case telemetry corpus (`eval.run.telemetry`) gates OTLP shape,
correlation, terminal semantics, redaction, failure isolation, structural
bounds, and default-off behaviour using injected transports.

**Still queued.** A real local-collector fixture, external collector
compatibility, and measured runtime overhead.

### Agent2Agent protocol

A2A is an open standard for communication between independent, potentially
opaque agent systems. It models messages, tasks, and artifacts rather than
requiring the parties to share an agent framework.

Source: [A2A specification](https://google-a2a.github.io/A2A/specification/).

**Shape.** Client and server adapters around Capability Contract v2, Workflow IR,
Run, and ArtifactRef. Remote discovery is untrusted input, authentication is
explicit, remote artifacts are verified, and side effects remain locally
authorised. A2A and MCP stay distinct: MCP exposes tools and resources; A2A
represents an independent agent and its task lifecycle.

**Current status.** The offline protocol mapping
(`vera.a2a-protocol-mapping/v1`, A2A `1.0`, pinned to `a2a-sdk==1.1.2`) maps
Agent Cards and skills to unauthorised Capability v2 candidates, preserves
server-owned task/context identity, maps every task state to Run or an explicit
semantic gap, and requires verification before any Part or Artifact becomes an
ArtifactRef. Inline card analysis is bounded and rejects plaintext credentials,
unsafe endpoints, duplicate skills, unsupported versions/bindings, and required
extensions Vera does not understand. Adapter-plan contracts deterministically
plan one reviewed `effects=["none"]` client task and an authenticated server
exposure plan without doing I/O. Inspect with `interop.a2a.conformance` or the
Agent Bridges panel. See
[Interoperability foundations](46-interoperability-foundations.md#remote-agents-and-a2a).

**Still queued.** Discovery, transports, authentication, sending, streaming,
cancellation, duplicates, disconnect/resume, artifact fetching and verification,
malicious metadata, and unsupported parts against a live peer (six live cases).

## Provider pilots

### Evaluation: DeepEval and Promptfoo

DeepEval documents end-to-end, trajectory, and component evaluation for agents,
tools, conversations, RAG, and MCP. Promptfoo is a local CLI/library for
evaluation and red teaming that can run in CI across providers.

Sources: [DeepEval introduction](https://deepeval.com/docs/introduction),
[agent evaluation](https://deepeval.com/docs/getting-started-agents),
[Promptfoo](https://www.promptfoo.dev/docs/intro/), and
[Promptfoo red teaming](https://www.promptfoo.dev/docs/guides/llm-redteaming/).

**Shape.** Neutral evaluation records come first (`EvaluationRequest`,
`EvaluationReport`, metric results, case identities, judge provenance, usage).
The tools consume frozen Run traces and fixtures; they do not define canonical
Vera task identity or silently upload content. Judge calls use the normal
resolver and policy gate, with cost and provenance recorded.

**Current status.** Provider-neutral evaluation contracts, an offline reference
evaluator, fail-closed execution controls, and a strict, payload-free importer
for frozen DeepEval and Promptfoo result projections are in
[`vera/models/`](../vera/models/) (`training_contracts.py`,
`deterministic_evaluation.py`, `evaluation_execution.py`,
`external_evaluation_import.py`). Running either tool, judge calls, and CI
behaviour remain queued.

### Structured generation: Instructor and Outlines

Instructor emphasises typed outputs with validation and retry using error
context. Outlines supports constrained JSON generation from Pydantic, JSON
Schema, or a function signature for compatible local models.

Sources: [Instructor validation](https://python.useinstructor.com/blog/2025/05/20/understanding-semantic-validation-with-structured-outputs/)
and [Outlines JSON generation](https://dottxt-ai.github.io/outlines/reference/generation/json/).

**Shape.** Both sit behind one `StructuredGenerationProvider`. Provider-native
JSON schema, Instructor correction, Outlines constrained decoding, and Vera's
current JSON retry behaviour compete through that contract. Retries are bounded
Run child steps with their own cost and error evidence.

**Current status.** `vera.structured-generation-plan/v1`
([`vera/providers/structured_generation.py`](../vera/providers/structured_generation.py),
capabilities `providers.structured.*`) normalises a bounded portable schema
subset, exposes static provider-native, Instructor, and Outlines profiles,
assigns one retry owner, validates supplied JSON without returning it, and plans
bounded schema/semantic correction without starting an attempt. Optional
provider imports, model calls, constrained decoding, streaming,
semantic-validator execution, and latency measurement remain queued.

### LiteLLM

LiteLLM documents normalised access to many providers, a proxy/gateway, retry
and fallback routing, cost tracking, budgets, authentication, and rate limiting.

Source: [LiteLLM documentation](https://docs.litellm.ai/).

**Shape.** One `InferenceProvider`; Vera retains policy, locality, ModelPackage
eligibility, task resolution, and evaluation. Avoid double retries by assigning
one retry owner per request (disable LiteLLM fallback first so Vera can observe
one retry owner at a time). Test SDK and proxy separately; prefer the isolated
proxy boundary rather than importing its provider dependency graph into the core
process.

**Current status.** Planned. Vera has a portable adapter for injected
OpenAI-compatible inference transports
([`vera/models/openai_inference_adapter.py`](../vera/models/openai_inference_adapter.py))
that a LiteLLM proxy could sit behind, but no LiteLLM-specific health, model, or
usage import.

### Docling

Docling converts broad document and media formats into a unified representation
and exports JSON, Markdown, text, chunks, and other formats.

Sources: [Docling formats](https://github.com/docling-project/docling/blob/main/docs/usage/supported_formats.md)
and [DocumentConverter](https://docling-project.github.io/docling/reference/document_converter/).

**Shape.** A `DocumentParser` adapter accepts an ArtifactRef and emits record
envelopes for structure, text, tables, media, page coordinates, and derived
assets, preserving the original, parser version/configuration, conversion
status, and deterministic element IDs. Unstructured is a later comparison
against the same frozen corpus rather than a parallel dependency. Source:
[Unstructured partitioning](https://docs.unstructured.io/open-source/core-functionality/partitioning).

**Current status.** A bounded plan/result boundary is shipped in
[`vera/providers/document_parser.py`](../vera/providers/document_parser.py)
(capabilities `providers.document.*`): portable media types, parser/config
provenance, deterministic source/page/locator element IDs, exact citations, OCR
declarations, resource ceilings, derived-artifact budgets, cancellation, and
non-destructive teardown plans. Encrypted, corrupt, oversized, malformed, or
policy-incompatible evidence fails closed before a file is opened. A
frozen-corpus evaluator compares content-free text/table/layout hashes across at
most 64 cases. The Docling profile is reported as `not_imported` / `queued_live`.
Installing and isolating Docling, converting the corpus, measuring fidelity, OCR,
latency and resources, crash/cancel behaviour, artifact persistence, and full
teardown remain queued.

### DSPy

DSPy treats model programs as modules and optimises prompts and/or weights
against a metric and examples.

Sources: [DSPy](https://dspy.ai/) and
[DSPy optimizers](https://github.com/stanfordnlp/dspy/blob/main/docs/docs/learn/optimization/optimizers.md).

**Shape.** DSPy is only an `OptimizerProvider`. Inputs are a versioned
PromptPackage or Workflow fragment, an evaluation suite, a budget, eligible
models, and policy. Outputs are immutable candidates, traces, costs, and an
evaluation report. Promotion remains a Vera registry decision; candidates are
never activated automatically. Start with bounded structured extraction, not
Dream or an open-ended agent.

**Current status.** Provider-neutral, proposal-only optimisation contracts
(`OptimizerProfile`, `OptimizationBudget`, `OptimizationRequest`, CI policy) are
in [`vera/models/optimizer_contracts.py`](../vera/models/optimizer_contracts.py).
No DSPy execution.

### MLflow interoperability

MLflow documents GenAI tracing/evaluation, prompt evaluation and registry,
datasets, model lifecycle, and agent-oriented evaluation workflows.

Sources: [MLflow GenAI evaluation](https://mlflow.github.io/mlflow-website/docs/latest/genai/eval-monitor/),
[prompt evaluation](https://mlflow.github.io/mlflow-website/docs/latest/genai/prompt-registry/evaluate-prompts/),
and [MLflow cookbook](https://mlflow.org/cookbook/).

**Shape.** Vera Run, Artifact, PromptPackage, and ModelPackage IDs are never
replaced by MLflow IDs. An exporter/importer with an identity map exports
training runs, evaluations, prompts, parameters, metrics, and artifacts, and
imports pinned candidates as untrusted registry proposals with verified hashes
and source revisions.

**Current status.** Planned.

### Qdrant

Qdrant supports dense+sparse hybrid queries, named vectors, multistage prefetch,
fusion, multivectors, filtering, and reranking patterns. It is a strong test of
whether a vector-index provider contract is genuinely portable beyond Chroma and
FAISS.

Sources: [Qdrant hybrid search](https://qdrant.tech/documentation/search/text-search/hybrid-search/)
and [hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/).

**Shape.** Index a read-only snapshot with stable Vera record/revision IDs.
Record the dense, sparse, and reranker ModelPackages, dimensions, metrics,
chunking, fusion, and build ID. Compare quality and latency with Fabric;
canonical records never move.

**Current status.** A fail-closed external-retrieval boundary
([`vera/fabric/external_retrieval.py`](../vera/fabric/external_retrieval.py)), an
optional Qdrant HTTP driver that owns one deterministic collection per exact
snapshot binding ([`vera/fabric/qdrant_retrieval.py`](../vera/fabric/qdrant_retrieval.py),
no Qdrant package imported), and an offline provider-neutral retrieval
comparison ([`vera/fabric/retrieval_comparison.py`](../vera/fabric/retrieval_comparison.py))
are shipped. Measured comparisons against a running Qdrant remain queued.

### Hugging Face PEFT

PEFT provides parameter-efficient adaptation and integrates with Transformers,
Diffusers, and Accelerate.

Source: [PEFT documentation](https://huggingface.co/docs/peft/index).

**Shape.** A training adapter that runs only once ModelPackage and TrainingRun
exist. Each adapter or LoRA is a derived package with base-model revision,
dataset snapshot, method/config, framework versions, precision, evaluation,
license, and hardware requirements. Current vLLM LoRA strings should become
deployments of registered derived packages rather than independent state.

**Current status.** Provider-neutral, non-executing `TrainingRequest` /
`TrainingRun` / `PromptPackage` contracts are in
[`vera/models/training_contracts.py`](../vera/models/training_contracts.py). No
PEFT execution.

## Comparison pilots

### LlamaIndex and Haystack

LlamaIndex focuses on agents over data, connectors, RAG, and event-driven
workflows. Haystack provides components, document stores, agents, tools, and
directed multigraph pipelines with loops and branches.

Sources: [LlamaIndex](https://llamaindex.openml.io/),
[Haystack](https://docs.haystack.deepset.ai/), and
[Haystack pipelines](https://docs.haystack.deepset.ai/docs/pipelines).

Both overlap substantially with Fabric, Context, agents, and Workflow IR. Only
two experiments are defined: import/export a supported workflow while emitting
Run, and expose selected reader/retriever/store components as providers. Useful
connectors are retained without adopting an entire second workflow and storage
model.

### Microsoft Agent Framework

Microsoft documentation presents agent abstractions, plugins/function calling,
threads, orchestration, and human collaboration across its ecosystem.

Sources: [Semantic Kernel agents](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/)
and [Microsoft Agent Framework](https://learn.microsoft.com/en-gb/agent-framework/).

A RuntimeAdapter candidate, especially for cross-language and Microsoft-system
interoperability. A deep adapter waits until current migration and declarative
semantics map honestly; A2A may deliver practical interoperability sooner with
less coupling.

### GraphRAG

Microsoft GraphRAG indexes unstructured text into graph data and queries
completed indexes, including local search combining knowledge-graph data with
text chunks.

Sources: [GraphRAG indexing](https://microsoft.github.io/graphrag/index/overview/)
and [GraphRAG query](https://microsoft.github.io/graphrag/query/overview/).

GraphRAG runs only as an offline index/evidence experiment over a frozen Fabric
snapshot, compared with entity graphs, hybrid retrieval, and Worldview. All
indexing model calls and cost are recorded, and extracted graph claims are never
treated as source truth. An exact-snapshot boundary for an externally configured
GraphRAG runtime ships in
[`vera/fabric/graphrag_retrieval.py`](../vera/fabric/graphrag_retrieval.py): it
accepts evidence only when the injected runtime proves it indexed, queried, and
removed the complete requested snapshot, and it imports no GraphRAG package.

### Ray Serve and BentoML

Ray Serve provides framework-agnostic composition, autoscaling, streaming, and
multi-model/multi-node deployment. BentoML provides Python model-service
packaging and deployment across models and clouds.

Sources: [Ray Serve](https://docs.ray.io/en/latest/serve/index.html),
[Ray Serve LLM](https://docs.ray.io/en/latest/serve/llm/index.html), and
[BentoML](https://docs.bentoml.com/en/latest/).

Both implement a `DeploymentProvider`; neither defines ModelPackage. Pilot
BentoML first for a simple reproducible service bundle, or Ray Serve first only
when a measured requirement needs multi-node composition or autoscaling. Do not
operate both before a real scenario distinguishes them.

### Observability backends

Langfuse covers tracing, prompt management, evaluations, datasets, experiments,
and analytics. Phoenix receives OpenTelemetry/OpenInference spans and provides an
open-source observability/evaluation backend.

Sources: [Langfuse](https://langfuse.com/docs) and
[Phoenix](https://arize.com/docs/phoenix/).

The same redacted portable telemetry is exported to each; no backend-specific
instrumentation is added throughout Vera. Export failure must never fail a user
task.

A frozen, provider-neutral comparison contract
([`vera/execution/observability_comparison.py`](../vera/execution/observability_comparison.py))
binds both backend observations to the exact same content-redacted portable trace
and derived OTLP/HTTP JSON digest. Reports keep span/parent/event/attribute
fidelity, query dimensions and UI views, evaluation linkage, ingest/query
latency, retention/access/deletion, storage/CPU/memory, standards portability,
outage isolation, and teardown separate. Missing measurements remain explicit;
the report has no composite score, winner, activation authority, backend import,
or network path. Real isolated Langfuse and Phoenix deployments — and their
ingestion, query, recovery, resource, retention, access, deletion, export, and
teardown measurements — remain queued.

### Guardrails and Ragas

Guardrails AI combines input/output guards with structured validation and a
validator ecosystem. Ragas focuses on RAG evaluation.

Sources: [Guardrails AI](https://guardrailsai.com/guardrails/docs) and
[Ragas](https://aclanthology.org/2024.eacl-demo.16.pdf).

Validators and metrics would be optional provider packs. Vera retains policy,
effect authorisation, evidence, failure mode, and audit. Third-party validators
must declare model calls, egress, latency, and measured errors.

## Durable execution

Durability is more valuable to Vera than another agent framework. Current native
systems independently implement persistence, queues, retry, schedule, resume, and
recovery. A durable runtime could replace those mechanics while preserving
Dream, Loop Lab, DAG, Calendar, and Agents as product policies.

A vendor-neutral durability fixture comes first (`workflow.durability.fixture`):
normalised Workflow IR with pinned definition and implementation revisions, and
clean, retry, cancel, timeout, compatible-version resume, incompatible-version
refusal, and crash-before/after-every-step scenarios with expected Run event
types, resume points, attempts, and effect-receipt counts. A static analyser
(`workflow.durability.gaps`) fails closed on absent semantics or events,
non-executable adapters, unsafe version policy, missing key-and-receipt
deduplication, and unsupported exactly-once claims. It imports and executes no
runtime.

### DBOS first

DBOS documents workflows that resume from the last completed step, durable queues
and sleeps, workflow IDs used as idempotency keys, cancellation/timeouts, and
Postgres-backed execution, and distinguishes workflow errors from retriable step
failures.

Sources: [DBOS overview](https://docs.dbos.dev/),
[workflow semantics](https://docs.dbos.dev/python/tutorials/workflow-tutorial),
and [architecture](https://docs.dbos.dev/architecture).

DBOS is the first spike because its application-library and Postgres shape
matches Vera's deployment better than a separate cluster service. It is still an
external runtime, not the Workflow IR definition or Run authority. The adapter
must map Workflow IR workflow/run/step IDs to DBOS IDs; Run states to DBOS status
without inventing exactly-once guarantees for external effects; idempotency key,
retry owner, timeout, cancel, durable sleep, queues, and signals; code/application
version to definition and implementation revisions; step results as artifact
references rather than large or sensitive workflow state; and DBOS recovery
observations back into Run events.

**Current status.** A static mapping manifest pinned to `dbos==2.30.0`
([`vera/execution/dbos_mapping.py`](../vera/execution/dbos_mapping.py),
`workflow.durability.dbos_mapping`) binds the fixture identity, Workflow IR hash,
revisions, DBOS constructs, retry/timeout options, status projection, and
Run-event evidence sources. It does not import DBOS, emit runnable source,
connect to Postgres, or execute a workflow. It stays not-ready while
timeout/cancel provenance, absolute wake conversion, compatible-version patches,
external-effect receipts, event ordering, large-result ArtifactRefs, and
cancellation boundaries lack live evidence. The first live task should be a
non-LLM, non-mutating workflow interrupted between deterministic steps.

### Temporal as reference

Temporal documents durable Workflows, Activities, Workers, service-based
recovery, messages, schedules, versioning, observability, and a Python test
framework.

Sources: [Temporal documentation](https://docs.temporal.io/),
[Python developer guide](https://github.com/temporalio/documentation/blob/main/docs/develop/python/index.mdx),
and [Python SDK reference](https://python.temporal.io/).

Temporal is the comparison target when Vera needs cross-service, long-lived,
multi-worker durability beyond the DBOS deployment shape. Both systems are not
operated until DBOS conformance exposes a concrete gap.

**Current status.** An offline comparison manifest pinned to `temporalio==1.32.0`
([`vera/execution/temporal_mapping.py`](../vera/execution/temporal_mapping.py),
`workflow.durability.temporal_paper`) records candidate mappings for Workflows,
Activities, timers, retries/timeouts, cancellation, history events,
Signals/Updates, patching, and Worker Versioning. A loss-aware Workflow IR
compiler for an *injected* Temporal runner also exists
([`vera/execution/temporal_workflow_adapter.py`](../vera/execution/temporal_workflow_adapter.py)).
Cross-service workers, long-lived history, versioned routing, messaging, child
workflows, schedules, visibility/retention, and in-flight migration all still
require live evidence; with no measured DBOS failure to justify it, the decision
remains **defer**.

### Prefect and Dagster boundaries

Prefect supplies flow/task retry and general workflow operations. Dagster is a
data orchestrator centred on assets, lineage, observability, and testability.

Sources: [Prefect retries](https://docs.prefect.io/v3/how-to-guides/workflows/retries)
and [Dagster overview](https://docs.dagster.io/).

Neither should become a third native Vera engine. Prefect remains a Workflow IR
adapter candidate for external definitions. Dagster becomes interesting only if
Fabric users need asset-centric materialisation, lineage, and data quality that
Vera cannot economically provide; in that case Dagster assets map to immutable
record/dataset/artifact revisions and Runs without duplicating state in Fabric.

## Agent SDKs

Agent frameworks are interchangeable runtimes, not Vera subsystems. They enter
via A2A when remote, or a RuntimeAdapter when embedded or containerised. All
receive the same task corpus, Capability v2 tools, model resolver, policy
boundary, Run events, cancellation, artifact handling, and teardown tests.

The runtime matrix (`agentbridge.runtime_matrix`) already compares ten
candidates across fifteen dimensions, keeping upstream-documented features
separate from Vera-verified behaviour, and declares ten live conformance cases.
It selects no universal winner. See
[Interoperability foundations](46-interoperability-foundations.md#runtime-matrix).

| SDK | Position |
|---|---|
| **Google ADK** ([docs](https://adk.dev/)) | Agent/tool development, multi-agent orchestration, graph workflows, evaluation, multi-language deployment. Prefer its A2A surface; a direct RuntimeAdapter only for features unavailable through A2A, declared as native extensions rather than silently added to Workflow IR. |
| **OpenAI Agents SDK** ([agents](https://openai.github.io/openai-agents-python/agents/), [running agents](https://openai.github.io/openai-agents-python/running_agents/), [tracing](https://openai.github.io/openai-agents-js/guides/tracing/)) | Agent/Runner loop with tools, handoffs, sessions, guardrails, structured outputs, hooks, and tracing. A provider-aware RuntimeAdapter: traces map to Run and portable telemetry (never sensitive by default), handoffs to child runs/messages, guardrails to policy/validator evidence; Vera remains final side-effect authority. Test a non-OpenAI model provider so vendor identity is not hard-coded into Workflow IR. |
| **Strands Agents** ([guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-frameworks/strands-agents.html)) | Conditional on a real Bedrock/AWS task. |
| **Agno** ([docs](https://docs.agno.com/)) | Overlaps almost every Vera layer; prefer its external protocol/runtime boundary and decline a deep native integration. |

None of these gets a bespoke panel or default capability family.

## Data and training infrastructure

### Hugging Face Datasets

Hugging Face Datasets supports local and remote formats, Arrow-backed data,
streaming, memory mapping, Parquet, and newer Lance access paths.

Sources: [loading datasets](https://github.com/huggingface/datasets/blob/main/docs/source/loading.mdx)
and [streaming](https://huggingface.co/docs/datasets/en/stream).

**Shape.** A `DatasetProvider`, not a second Fabric. An imported dataset becomes
a pinned source package plus a Fabric dataset snapshot recording upstream
repository/revision, config, split, file hashes, schema/features, license/card,
streaming cursor, cache, and transformations. Remote loading is data
egress/ingress subject to policy; unreviewed dataset code must not execute in
Vera's core process.

**Current status.** The adapter boundary
([`vera/fabric/huggingface_dataset_adapter.py`](../vera/fabric/huggingface_dataset_adapter.py),
[`vera/fabric/dataset_provider.py`](../vera/fabric/dataset_provider.py)) targets the
documented `datasets==4.8.4` loading and iterable-checkpoint APIs. It requires a
full Hub commit SHA, explicit config/split, an injected loader and version, no
ambient token, bounded complete materialisation, and source-bound `state_dict`
streaming resume. It does not install or import the package in Vera core and
exposes no public network capability. References:
[loading methods](https://huggingface.co/docs/datasets/v4.8.4/package_reference/loading_methods)
and [streaming/checkpoints](https://huggingface.co/docs/datasets/v4.8.4/stream).
The small/large Hub fixtures and cache/license/card checks remain queued.

### Accelerate

Hugging Face Accelerate offers a unified interface and launcher for distributed
PyTorch training and inference, including mixed precision, FSDP, DeepSpeed,
large-model loading, and multiple hardware platforms.

Source: [Accelerate documentation](https://huggingface.co/docs/accelerate/index).

A TrainingRuntime selected by a TrainingRun, not a new training schema. Its
resolved configuration, launcher command, environment, topology, precision,
checkpoints, profiler output, and failures belong in Run/Artifact records, and a
container or isolated environment owns the dependency stack. A distributed
TrainingRuntime contract (launcher/topology/precision/checkpoint/progress
semantics) comes first, without running a distributed job. **Status:** planned.

### DVC

DVC uses Git-adjacent metadata and external caches/remotes to version data and
models, and also provides pipeline and experiment features.

Sources: [DVC home](https://www.dvc.org/) and
[command/workflow reference](https://dvc.org/doc/command-reference/).

Vera imports/exports DVC-tracked artifacts and dataset revisions through
repository intake, preserving DVC hash, remote, path, Git commit, and stage
metadata. `dvc.yaml` execution is not adopted as another native pipeline.

**Current status.** An offline local-file adapter
([`vera/fabric/dvc_artifact_adapter.py`](../vera/fabric/dvc_artifact_adapter.py))
inspects one standalone `.dvc` descriptor at an explicitly pinned Git `HEAD`,
verifies its single file in the default DVC 3 MD5 cache, and copies the exact
bytes into Vera's SHA-256 artifact store with an ArtifactRef plus DVC/Git
provenance. It does not invoke Git or DVC, load remote configuration or
credentials, check out a workspace, or execute a pipeline. Its deterministic
tests cover identity, packed refs, containment, missing/corrupt objects, size
limits, source non-mutation, and rejection of directory, multiple, uncached, and
pipeline outputs. Proving the descriptor is a clean tracked blob at that commit,
DVC's own retrieval (which may consult a remote; see the
[`get` contract](https://dvc.org/doc/command-reference/get)), the Python API,
custom cache directories, and live remote/credential tests remain queued.

### Lance and DuckDB

Lance is an open lakehouse format for multimodal AI data on object storage, with
vector/full-text search, random access, transactions, time travel, and
integrations. DuckDB provides local analytical SQL; its VSS extension adds
experimental HNSW vector indexing.

Sources: [Lance format](https://lance.org/),
[DuckDB VSS](https://duckdb.org/docs/lts/core_extensions/vss), and
[DuckDB extension tiers](https://duckdb.org/docs/current/core_extensions/overview).

**Lance** (the format, separate from LanceDB) is a planned experiment: store a
frozen multimodal dataset artifact and prove schema, identity, time-travel
revision, portability, and cleanup, compared with Parquet plus ArtifactRefs. It
does not become canonical until transaction, evolution, repair, and ecosystem
behaviour are tested.

**DuckDB** is a strong `QueryProvider` for local snapshots and artifacts. An
offline adapter ([`vera/fabric/duckdb_artifact_query.py`](../vera/fabric/duckdb_artifact_query.py),
pinned to `duckdb==1.5.5` as an optional dependency) queries checksum-verified
local Parquet artifacts through a structured `QueryRequest` and **never accepts
caller SQL**. It binds paths and equality values as parameters, enforces bounded
pages and query-bound cursors, and uses a fresh locked-down connection with
extension installation/loading and ambient cloud credential discovery disabled.
Its fake-backed tests cover injection refusal, binding, checksum enforcement,
parameterisation, cursors, cancellation, bounded errors, and teardown. Real
DuckDB/Parquet execution, malicious files, measured memory/time, concurrency, and
Arrow decisions remain queued. VSS stays experimental and rebuildable, never the
only copy. References: DuckDB [Python API](https://duckdb.org/docs/stable/clients/python/overview),
[configuration](https://duckdb.org/docs/stable/configuration/overview), and
[Parquet reader](https://duckdb.org/docs/lts/data/parquet/overview).

### Portable memory providers

Memory adapters have a provider-neutral target
([`vera/fabric/memory_provider.py`](../vera/fabric/memory_provider.py)) that treats
Fabric record revisions as authority, requires exact source citations, carries
explicit tenant/principal policy context, and standardises bounded filters,
pagination, tombstones, and redacted result projection. The source revision
content hash stays distinct from the derived projection hash. The frozen
reference adapter is default-deny and deterministic: a conformance reference,
not a new production store or ranking benchmark.

A read-only native adapter converts plain `MemoryRecord` snapshots only when
given an explicit tenant/native-ID binding to an immutable Fabric revision; it
does not import the live Memory runtime, query a backend, or redirect a
capability, and conflicting identity, type, archive/tombstone, or lifecycle data
fails closed. It can emit a deterministic, payload-free conversion receipt for
offline replay and tamper comparison. Apply/read/search attempts emit
checksummed audit receipts
([`vera/fabric/memory_audit.py`](../vera/fabric/memory_audit.py)) keyed by an
explicit caller request ID, recording only identities, safe context hashes,
counts, and generation; the reference sink is bounded and idempotent, not
durable or signed. Bounded non-mutating reconciliation and a context-provider
bridge also exist (`memory_reconciliation.py`, `memory_context_provider.py`).

Current Vera Memory, Postgres, Chroma, Neo4j, and the public `memory.*`
capabilities are unchanged. Durable/signed audit storage, bulk projection,
export/reconciliation, and recovery come next; any second provider (for example
MemPalace) still requires live trials for identity, citation fidelity,
tenant/session isolation, ranking, outage/sync, update/tombstone/delete/export,
and reconciliation before it can be advertised.

## Inference engines

### llama.cpp

llama.cpp provides local GGUF inference and an OpenAI-compatible server with
chat, responses, embeddings, quantised CPU/GPU execution, grammar constraints,
and other serving features.

Sources: [llama.cpp project](https://github.com/ggml-org/llama.cpp/blob/master/README.md)
and [server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

A P1 InferenceProvider because it covers CPU-heavy, small-node, edge, and GGUF
deployments not cleanly represented by vLLM. Ollama uses llama.cpp internally
(Vera already tunes its runner threads), but provider conformance matters: Vera
should be able to operate llama.cpp directly when that reduces overhead or
exposes needed controls. GGUF files would be imported as ModelPackages with
checksum, quantisation, architecture, tokenizer/template, license, source
revision, context/resource estimates, and supported tasks, and the server started
through a DeploymentProvider with explicit argv, ports, resources, health, and
teardown — never interpolated shell commands or unverified model paths.
**Status:** planned; live work queued with all other model tests.

### SGLang

SGLang documents high-performance serving for language and vision-language
models, structured outputs, prefix caching, batching, speculative decoding,
quantisation, multi-LoRA, parallelism, metrics, tracing, and multi-node
deployment.

Source: [SGLang documentation](https://docs.sglang.io/).

P2 until a frozen workload identifies a vLLM gap in model support, structured
generation, multimodal behaviour, prefix reuse, LoRA batching, or
throughput/latency. The admission benchmark isolates those candidate advantages
(plus one multi-node scenario only if hardware exists) and is defined but not
run. A win on one benchmark does not justify replacing vLLM globally; routing may
retain both for different eligible deployments.

## Libraries not selected for dedicated work

- More agent frameworks such as CrewAI and legacy AutoGen should first use A2A,
  MCP, OpenAPI, or a RuntimeAdapter. Vera needs conformance evidence more than
  another named panel.
- More vector databases wait until Qdrant proves the provider contract.
- More observability suites use OTLP/OpenInference and need no Vera-specific code
  unless they demonstrate a unique feature.
- TensorRT-LLM and Kubernetes serving stacks enter through InferenceProvider or
  DeploymentProvider when a concrete gap exists.
- Broad all-in-one frameworks do not become core dependencies merely because Vera
  wants one of their features; integrate the narrow feature or protocol boundary.

## Implementation status at a glance

| Boundary | Source | Inspect with | Status |
|---|---|---|---|
| Portable Run telemetry (OTel/OpenInference) | `vera/execution/portable_telemetry.py` | `run.telemetry.preview`, `run.telemetry.status`, `eval.run.telemetry` | Offline contract + opt-in exporter; collector tests queued |
| A2A mapping and adapter plans | `vera/execution/a2a_mapping.py`, `a2a_adapter.py` | `interop.a2a.conformance` | Offline; transports queued |
| Evaluation records and external import | `vera/models/training_contracts.py`, `deterministic_evaluation.py`, `evaluation_execution.py`, `external_evaluation_import.py` | — | Offline; tool execution queued |
| Structured generation | `vera/providers/structured_generation.py` | `providers.structured.*` | Offline; provider execution queued |
| Document parsing | `vera/providers/document_parser.py` | `providers.document.*` | Offline; Docling queued |
| Prompt optimisation | `vera/models/optimizer_contracts.py` | — | Proposal-only contracts |
| External retrieval (Qdrant, GraphRAG) | `vera/fabric/external_retrieval.py`, `qdrant_retrieval.py`, `graphrag_retrieval.py`, `retrieval_comparison.py` | — | Injected-driver boundaries; measurements queued |
| Observability backend comparison | `vera/execution/observability_comparison.py` | — | Evidence contract; deployments queued |
| Durability fixture, DBOS and Temporal mappings | `vera/execution/durability_fixture.py`, `dbos_mapping.py`, `temporal_mapping.py`, `temporal_workflow_adapter.py` | `workflow.durability.*` | Offline; executable spikes queued/deferred |
| Agent runtime matrix | `vera/agentbridges/runtime_matrix.py` | `agentbridge.runtime_matrix`, `.evaluate` | Offline comparison; live cases queued |
| Hugging Face DatasetProvider | `vera/fabric/huggingface_dataset_adapter.py` | — | Offline adapter |
| DVC artifact adapter | `vera/fabric/dvc_artifact_adapter.py` | — | Offline local-file adapter |
| DuckDB QueryProvider | `vera/fabric/duckdb_artifact_query.py` | — | Offline adapter, optional dependency |
| Portable memory provider | `vera/fabric/memory_provider.py`, `memory_audit.py`, `memory_reconciliation.py` | — | Offline contract and native projection |
| OpenAI-compatible inference | `vera/models/openai_inference_adapter.py` | — | Injected-transport adapter |

The underlying foundations these adapters rely on are documented in the
subsystem guides:

| Concern | Guide |
|---|---|
| Reproducible performance evidence and resource envelopes | [Performance and sizing](00-performance-and-sizing.md), [Performance baseline](42-performance-baseline.md) |
| Contract v2 projection, lint, coverage, gate, observations | [Capability contracts](43-capability-contracts.md) |
| Frozen deterministic/queued-live corpora | [Evaluation corpus](44-evaluation-corpus.md) |
| Resolver shadow, policy verdicts, receipts, selective enforcement | [Capability policy](45-capability-policy.md) |
| Run shadow, Workflow IR, durability fixture, runtime mappings | [DAG engine](03-dag-engine.md), [Execution](12-execution.md) |
| Activity, graphs, harness status | [Harness UI](02-harness-ui.md), [Activity and boards](39-activity-boards.md) |

Together they form one path — describe, measure, resolve, authorise, execute,
observe — while native execution stays authoritative until a specific adapter
passes its conformance and live gates.

## Cross-cutting adapter requirements

Every pilot must provide:

- a pinned version or image digest and an upstream source/license record;
- an isolated optional dependency group or container, never an unconditional core
  import;
- feature negotiation with explicit unsupported semantics;
- Vera IDs plus native-ID mappings for runs, records, artifacts, prompts, models,
  deployments, and tasks;
- schemas, limits, pagination, timeout, cancellation, retry, idempotency, health,
  and teardown behaviour;
- declared filesystem, network, accelerator, secret, and egress requirements (as
  a [Capability Contract v2](43-capability-contracts.md) where it is exposed as a
  capability);
- Run events and portable telemetry with content-redaction controls;
- conformance fixtures and a baseline comparison;
- activation, rollback, upgrade, export, and removal procedures;
- no new default model-discovery surface until selection quality is measured.

## Acceptance gates by adapter

Each adapter is accepted only when its gate passes with live evidence.

| Adapter | Gate |
|---|---|
| Portable telemetry | Local collector fixture, external collector compatibility, measured overhead |
| A2A | Authentication/policy, cancellation, duplicates, disconnect/resume, artifact verification, malicious metadata, unsupported parts |
| Evaluation provider | Deterministic scorers, judge provenance/cost, CI behaviour, redaction, timeout, partial reports, stable case identity |
| Structured generation | Valid/invalid schemas, unsupported constructs, retry budget, cancellation, streaming, latency, semantic-validator failures |
| LiteLLM | Schema/stream parity, tools, structured outputs, errors, rate limits, usage/cost, secrets, attempt provenance, outage |
| Docling | Text/table/layout fidelity, stable IDs, citations, corrupt/encrypted files, resource ceilings, OCR declaration, cancellation, teardown |
| DSPy | Held-out improvement, cost/time, reproducibility, overfitting, provenance, cancellation, rollback |
| MLflow | Hashes, revisions, aliases, missing artifacts, incompatible schemas, access policy, round trip, no authority inversion |
| Qdrant | Stable record IDs, rebuild, filters, recall/precision, p50/p95 latency, resources, outage fallback, deletion projection, reconciliation |
| PEFT | Base revision, dataset provenance, reproducibility, quality/safety delta, storage/resources, license, load/unload, rollback |
| LlamaIndex / Haystack | Workflow IR gaps, record/citation fidelity, retrieval quality, latency, dependency weight, teardown, no duplicate authority |
| Ray Serve / BentoML | Packaging, cold start, streaming, batching, autoscaling if required, health, recovery, placement, provenance, rollback |
| Observability backends | Trace fidelity, query/UI use, evaluation linkage, retention, access, resource cost, export portability, outage isolation, teardown |
| GraphRAG | Answer/citation quality, indexing cost/time, incremental update, drift, provenance, deletion/rebuild; repeated in a second domain |
| DBOS | Restart/recovery at every boundary, idempotency, retry ownership, cancellation, version mismatch, state/artifact limits, database loss, full teardown |
| Temporal | Approved only if a named distributed/longevity/versioning requirement cannot be met safely by DBOS |
| Agent SDKs | Task/tool correctness, provider substitution, handoff/child identity, policy enforcement, structured output, sessions, cancel/recovery, portable traces, dependency isolation, teardown |
| Hugging Face Datasets | Revision/hash/license/card, split/schema, streaming resume, cache limits, offline replay, malicious builder prevention, export, removal |
| TrainingRuntime / Accelerate | Static config validation, hardware eligibility, secrets/environment allowlist, cancellation, checkpoint ArtifactRefs, unsupported topology disclosure |
| DVC | Git/DVC identity, remote and credential boundary, missing cache, hash mismatch, path traversal, offline behaviour, export round trip, no worktree mutation |
| DuckDB | Read-only enforcement, SQL/schema types, memory/time/output limits, cancellation, artifact provenance, malicious files, concurrency, teardown |
| Lance | Schema evolution, random access, media fidelity, checksums, transaction/time-travel behaviour, reader portability, storage/latency, corruption, removal |
| llama.cpp | Model metadata, CPU/GPU placement, chat/responses/embedding/structured output, stream/cancel, concurrency, cold/warm latency, memory, health, crash/restart, teardown |
| SGLang | No deployment unless an important workload improves enough to cover added operational cost while keeping quality, policy, and provenance |

## Recommended sequence

1. Portable Run telemetry and the neutral evaluation records.
2. The runtime-neutral durability fixture, then the DBOS spike before migrating
   any Vera product workflow; keep Temporal at its paper decision until a
   specific gap appears.
3. A2A, then structured generation; Google ADK and OpenAI Agents SDK comparisons
   only after A2A.
4. LiteLLM and Docling.
5. Data adapters in the order DatasetProvider, read-only DuckDB, DVC, then Lance.
6. A TrainingRuntime contract before Accelerate or PEFT execution; DSPy, MLflow,
   Qdrant, and PEFT once evaluation and model/record contracts are active.
7. llama.cpp only after ModelPackage and InferenceProvider contracts exist;
   SGLang stays an admission benchmark.
8. LlamaIndex/Haystack, serving, and GraphRAG comparisons only after narrower
   adapters have established baselines.

Multiple framework or serving pilots are not run concurrently merely to save
time: their value depends on stable fixtures and comparable telemetry, and Vera's
shared model gate must remain coordinated. Static contract and schema work can
proceed independently.

## Queued live-test matrix

This matrix is intentionally inactive until explicitly authorised.

| Test class | Applies to | Evidence captured |
| --- | --- | --- |
| Installation | all | pinned lock/digest, dependency delta, CVE/license scan, disk/time |
| Contract | all | supported features, schemas, unsupported-feature honesty |
| Functional | all | task success and exact output/artifact/citation identity |
| Quality | eval/RAG/optimisation/models | held-out metrics and human rubric where needed |
| Performance | runtimes | cold/warm p50/p95, throughput, tokens, CPU/RAM/GPU, storage |
| Cost | model/judge/index | calls, tokens, provider cost, indexing/training budget |
| Safety | agent/tool/parser/gateway | injection, egress, secrets, archive bombs, policy bypass |
| Reliability | services/runtimes | timeout, cancellation, rate limit, crash, restart, disconnect, retry |
| Consistency | data/models | idempotency, revision mapping, reconciliation, export/delete |
| Upgrade | all | old/new compatibility, migration, downgrade or rollback |
| Removal | all | deactivate, remove env/config/state, preserve canonical records |

No benchmark uses production data or mutates production. Model and judge tests
acquire the shared GPU gate and run sequentially. External network and hosted
service calls require explicit configuration and egress approval. Results become
evaluation reports linked to the exact source, package, fixture, and Run IDs.

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

"Adopt" means Vera maintains an adapter and conformance suite. It does not mean
the library becomes Vera's internal architecture or dozens of default
capabilities.

## Related pages

- [Interoperability foundations](46-interoperability-foundations.md) — the boundaries every adapter must cross.
- [Capability contracts](43-capability-contracts.md), [Capability policy](45-capability-policy.md), [Evaluation corpus](44-evaluation-corpus.md).
- [Agent runtimes and providers](36-agent-runtimes-providers.md) — shipped agent bridges and provider panels.
- [Execution](12-execution.md) and [DAG engine](03-dag-engine.md) — Run protocol, Workflow IR, durability.
- [Data fabric](06-data-fabric.md) — datasets, artifacts, retrieval.
- [Machine learning](16-machine-learning.md) and [ONNX](30-onnx.md) — ModelPackage, training and evaluation contracts.
- [vLLM](21-vllm.md) and [Ollama cluster](04-ollama-cluster.md) — current inference backends.
- [Performance and sizing](00-performance-and-sizing.md).
