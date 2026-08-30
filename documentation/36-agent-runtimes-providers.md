# 36 · Agent Runtimes, Providers, and Model Catalog

Vera can run its own DAG/loop engine and can also delegate work to external
agent frameworks. The Agent Bridges layer normalizes those runtimes; Providers
manage hosted-model connections and usage; Catalog helps choose models that fit
available hardware.

## Supported layers

| Layer | Examples | Contract |
|---|---|---|
| Native runtime | DAG and `loops.*` | Vera owns planning, tools, state, and events |
| Agent bridge | smolagents, LangGraph, PydanticAI | Vera owns launch/policy; framework owns its internal run |
| Remote agent protocol | A2A v1.0 | Remote agent owns its task; Vera owns local policy, projection, and verified artifacts |
| Coding bridge | Claude Code, Codex, remote IDE agents | Vera owns work item, branch, capacity, and handoff |
| Provider | hosted chat/model APIs | Sealed credentials, model discovery, usage, and cost |
| Catalog | Hugging Face/Ollama metadata | Search, fit estimation, and installation handoff |

## Offline runtime comparison matrix

`agentbridge.runtime_matrix` is the non-executing LIB18 comparison surface. It
covers native Vera, the shipped LangGraph/PydanticAI/Smolagents bridges, and
prospective OpenClaw, Google ADK, OpenAI Agents SDK, Strands, Agno, and
Hermes-compatible paths. Each is assessed across tools, providers, handoffs,
structured output, policy, sessions, recovery, traces, resources, teardown,
streaming, cancellation, artifacts, sandboxing, and MCP.

Every dimension has separate **upstream** and **Vera** states. Upstream means
current official documentation describes the feature; Vera means repository
adapter code has implemented or verified it. An advertised upstream session or
guardrail therefore remains `not_integrated` until Vera has evidence. The
matrix imports no optional runtime, installs nothing, performs no model/network
call, and selects no winner. All execution and failure drills are `queued_live`.

## Launch lifecycle

1. Inspect bridge/provider status and dependencies.
2. Resolve a model and verify it is available to the selected runtime.
3. Supply a bounded goal, allowed tools, and execution limits.
4. Start the run and retain its Vera run/session identity.
5. Stream or poll normalized status while preserving framework-native detail.
6. Record terminal output, usage, artifacts, and errors.

Image `ensure` capabilities prepare runtime environments but should not silently
upgrade an active workload. Pin versions for repeatability. A bridge being
installed does not mean its provider credentials, model, or tools are valid.

## Model selection and accounting

Catalog fit is an estimate derived from model metadata, quantization, and known
hardware. Confirm with a real load/warm-up before routing important traffic.
Provider usage should retain provider, model, token counts, price revision, and
request identity. Updating a pricing table changes estimates, not historical
provider invoices.

## Structured generation contract

LIB-04 starts with the provider-neutral, non-executing contract in
`vera/providers/structured_generation.py`. It normalizes a bounded portable JSON
Schema subset, assigns a stable schema and plan identity, records exactly one
retry owner, and describes optional semantic validation, latency, and streaming
requirements. Provider-native JSON Schema, Instructor correction, and Outlines
constrained decoding are static profiles behind the same record.

`providers.structured.validate` deterministically checks supplied JSON values
and returns only violation paths/codes—not the value. It does not run a semantic
validator. Schema and semantic failures can produce a bounded retry plan;
provider failures are not silently retried, and cancellation or timeout is
terminal. Streaming remains explicitly unverified.

The provider page and Agent Bridges show contract/profile state, while all three
execution paths remain `queued_live`. The offline layer imports neither
Instructor nor Outlines, accepts no prompt, calls no model, decodes no token, and
starts no stream. This prevents vLLM's existing `guided_json` option or a hosted
provider's schema feature from becoming a separate canonical task family.

| Capability | Purpose |
|---|---|
| `providers.structured.status` | Inspect profiles and execution readiness |
| `providers.structured.plan` | Normalize schema and plan provider/retry ownership |
| `providers.structured.validate` | Validate supplied JSON without returning it |
| `providers.structured.retry.plan` | Plan but never start one correction attempt |

### Portable document parsing and Docling

`vera/providers/document_parser.py` adds the LIB-06 `DocumentParser` contract.
It compiles an inert plan from an original `ArtifactRef`, supplied inspection
metadata, OCR policy, and bounded page/element/time/memory/artifact ceilings.
Encrypted, corrupt, oversized, cancelled, and OCR-required-but-disabled inputs
fail before provider import or file access. Element IDs deterministically bind
source checksum, page, ordinal, kind, and locator.

Supplied adapter evidence is validated without returning extracted content or
writing records. Every element needs text/structure hashes and a citation back
to the exact source checksum, page, and locator. Parser version/configuration,
derived-artifact checksums and budgets, OCR engine/languages, duplicate
positions, and cancellation are checked explicitly. A frozen-corpus evaluator
compares bounded text/table/layout hashes so later Docling and alternative
adapters can use the same evidence format.

The Docling profile is currently `not_imported`; conversion, fidelity, OCR,
resource enforcement, cancellation propagation, and teardown execution remain
`queued_live`. The teardown capability returns a checklist only and never
deletes the original or verified data.

| Capability | Purpose |
|---|---|
| `providers.document.status` | Inspect portable contract and honest Docling readiness |
| `providers.document.plan` | Compile a bounded, non-executing parse plan |
| `providers.document.validate` | Validate supplied provenance, IDs, citations, bounds, and OCR evidence |
| `providers.document.corpus.evaluate` | Compare frozen-corpus hash evidence without content |
| `providers.document.teardown.plan` | Plan isolated cleanup without starting it |

## Trust boundary

External runtimes may propose tool calls or return structured events, but Vera's
capability policy remains authoritative. Do not grant a framework every tool
merely because it runs in a container. Scope filesystem/network access, pass
secrets by reference, and treat framework output as untrusted until validated.

The first A2A foundation makes this ownership concrete without opening a network
boundary. Agent Cards are discovery evidence, not trusted registration; skills
become unresolved remote candidates rather than capabilities; A2A task/context
IDs remain server-owned; and Task/Message/Artifact projections cannot authorize
effects. This protocol layer must land before Google ADK or OpenAI Agents SDK can
join a common RuntimeAdapter/A2A conformance matrix.

Agent Bridges also exposes an interoperability summary in its UI. The summary
shows the LIB02 A2A mapping and inert client/server plans, LIB18 candidate and
dimension counts, queued live gates, and whether shared Vera contract
capabilities are actually registered. It also reports W3-06 MCP/OpenAPI source
intake as an inspection-only lifecycle, separating its implemented discovery,
inspection, and proposal states from queued build, verification, approval, and
activation. “Implemented” there means the bounded,
deterministic planning contract exists; transport/listener execution remains
labelled `queued_live` until the explicit live gate is authorised and passes.
W3-07 adds an equally inert build/activation proposal for pinned Python, CLI,
OCI, and repository sources. Agent Bridges shows the plan-contract state and
source-kind count, but exposes no build or activation action; actual external
materialisation and conformance remain queued.

## Troubleshooting

Separate dependency/image failure, provider authentication, model lookup,
framework initialization, tool-schema incompatibility, runtime exception, and
result-normalization failure. Preserve the native trace alongside Vera's
normalized error; collapsing everything to “agent failed” removes the evidence
needed to fix it.

## Source map

- `vera/agentbridges/` — catalog, environment, and launch normalization.
- `vera/agentbridges/runtime_matrix.py` — deterministic upstream-versus-Vera
  feature matrix and queued live conformance cases.
- `vera/execution/a2a_mapping.py` — offline A2A v1.0 mapping and conformance lanes.
- `vera/integrations/source_intake.py` and `source_build_plan.py` — bounded
  discovery and inert build/activation admission contracts.
- `vera/smolagents/`, `vera/langgraph/`, `vera/pydanticai/` — adapters.
- `vera/providers/` — credentials, models, chat, pricing, usage, and the neutral
  structured-generation and document-parser contracts.
- `vera/catalog/` — discovery and hardware-fit estimates.
- `vera/ide/` and `vera/board/` — coding-agent execution and work ownership.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
