# 36 · Agent Runtimes, Providers, and Model Catalog

Vera can run its own DAG/loop engine and can also delegate work to external
agent frameworks. The Agent Bridges layer normalizes those runtimes; Providers
manage hosted-model connections and usage; Catalog helps choose models that fit
available hardware.

The Agent Bridges panel treats its runtime catalog and interoperability summary
as separate read models. Each reports loading, empty, ready, or failed state
independently and offers a retry after transport or response failure. This keeps
an available catalog usable when its summary is unavailable, and prevents a
failed request from looking like an indefinitely loading service.

The API Providers panel applies the same rule independently to provider
inventory, structured-generation status, document-parser status, model lists,
and usage. An HTTP, application, or malformed-response failure is visible and
retryable without suppressing healthy sibling reads. Failed model discovery
disables the affected selector and playground action instead of presenting a
fallback as verified inventory; failed usage reads replace plausible zeroes with
unknown values. A confirmed empty response remains distinct from an unavailable
service. Configuration, credential storage, provider execution, and usage
accounting retain their existing authority.

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

`agentbridge.runtime_matrix` is the non-executing runtime comparison surface. It
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
For a shipped RuntimeAdapter, package pins and directly equivalent lifecycle
dimensions come from that adapter's declaration. This keeps execution evidence
in one place without inflating unrelated matrix claims such as policy or
recovery.

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

Portable model-serving providers are discoverable through
`InferenceProviderRegistry`. Each immutable descriptor identifies the provider,
supported ModelPackage IDs and tasks, placement labels, an externally supplied
health record, and a compare-and-set revision. A health record identifies its
probe source, observation and expiry times, available packages, concurrency,
in-flight and queued work, and optional observed latency. Its content-derived
identity makes altered or stale evidence detectable. Candidate queries require
the caller's explicit evaluation time and accept `ready` only while that record
is current and names the requested package. Resolution always requires the
caller to name a provider; the registry never silently chooses another
candidate.

This boundary intentionally does not run probes, rank providers, balance
traffic, retry a request, or fail over. Cluster and deployment policy can use
the descriptors as evidence while retaining one visible owner for routing and
retry decisions. A saturated provider can remain healthy—available slots,
queue depth, and readiness are separate facts. Updating health or replacing a provider requires the
previous descriptor revision, preventing a stale controller from overwriting a
newer cluster view.

Offline parity and outage checks use `InferenceConformanceExpectation`. The
fixture hashes output values and compares request/package identity, terminal
status, stable error code, and—when requested—usage counters across supplied
transcripts. Reports contain only mismatch field names and hashes, never model
payloads. These checks do not dispatch inference; representative runtime,
placement, load, and recovery tests remain a separate live gate.

`InferenceDeployment` is the durable join between those pieces. Its stable
identity binds one admitted ModelPackage to a provider and target, exact runtime
kind/version, verified artifact digests, placement labels, and a single retry
owner. The SQLite registry stores definitions idempotently and appends
compare-and-set observations for desired versus observed lifecycle state.
Observations may embed the exact health evidence that produced a provider state;
pending, stopped, and failed states remain explicit even when no probe exists.
Repeated writes of the same observation are idempotent, while stale competing
writes fail closed.

Deployment records are evidence and coordination—not an executor. Creating or
updating one cannot load an artifact, start or stop a runtime, probe health,
route traffic, retry inference, or change an active compatibility alias. Those
effects remain owned by a future deployment controller and separately gated
runtime adapters.

The native tensor compatibility layer provides deployment-bound PyTorch and
TensorFlow batch adapters without taking over lifecycle ownership. Both reuse
the same canonical JSON input, prediction/shape output, cancellation, output
limit, and stable-error boundary as existing batch providers. Construction
fails when the admitted deployment's package, runtime, provider, or artifact
evidence does not match the selected framework. Framework imports, weight
deserialization, device selection, worker discovery, retries, and traffic
cutover remain outside this layer.

Before an execution owner dispatches, `plan_inference_dispatch` can join one
explicit provider and deployment choice to a portable request. It requires the
provider to be eligible at a caller-supplied time, the deployment to be desired
active and observed ready, and both registries to cite the same current health
record. An explicit policy may refuse saturated providers or excessive queues
and bounds retry attempts; the immutable deployment supplies the sole retry
owner. The content-addressed result records the exact provider, deployment,
health, policy, and registry revisions used for the decision.

Planning does not enumerate or rank alternatives and does not return a provider
object. It cannot invoke inference, reserve capacity, route traffic, retry, or
fail over. If evidence changes between planning and execution, the recorded
revisions make revalidation the execution owner's responsibility rather than a
silent fallback.

## Structured generation contract

Structured generation starts with the provider-neutral, non-executing contract in
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

`vera/providers/document_parser.py` defines the portable `DocumentParser` contract.
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

Container-based agent bridges share a runtime-neutral lifecycle boundary before
their library-specific code runs. The boundary declares image acquisition,
health, dependency isolation, streaming events, cancellation, resource gates,
teardown, and version reporting; unsupported or partial semantics remain
visible rather than being inferred from a successful container launch.
LangGraph is the first migrated bridge. Its existing `langgraph.*` capability
names and `langgraph.run.*` events are unchanged, while image health/build and
validated run requests now pass through the shared adapter. Static inspection
does not import LangGraph, build an image, contact a model, or launch a run.
Active runs can be cancelled by validated run ID through the runner's owned
process registry. Cancellation, timeout, malformed output, and normal completion
converge on one terminal event and release the shared resource gate. Protocol
payloads are bounded and cannot override trusted run, session, or event fields.
Success is emitted only after the owned container process exits. If it prints a
result and then hangs, Vera kills and reaps it; inability to reap becomes an
explicit teardown failure rather than a false successful run.
The image records its runtime identity and complete pinned package set as OCI
labels. Agent Bridge can compare those labels with the declared adapter without
starting the image; missing labels and drift remain visibly distinct from a
matching self-declaration. This is version evidence, not a signature or
independent supply-chain attestation.

The other shipped container bridges already share the hardened low-level
container runner, but they do not yet use the complete adapter facade.
PydanticAI and Smolagents still repeat Docker health checks, image build
wrappers, run preconditions, and request assembly; the aggregate catalog also
checks image presence directly. A safe consolidation seam is therefore the
existing RuntimeAdapter and ContainerRunRequest, not a new agent loop.
Static descriptors can centralize common health, image, cancellation, teardown,
and run lifecycle behavior while preserving each bridge's capability names,
event prefix, opt-in setting, image and Dockerfile, command arguments, progress
kinds, and runtime-specific errors. Catalog health can use registered adapters
with an explicit legacy fallback while migration is incomplete. No wrapper may
be removed until stored and external callers are inventoried and deterministic
success, error, timeout, cancellation, teardown, and resource-gate parity is
proven; live image builds and bridge runs remain separate validation gates.

Agent Bridges also exposes an interoperability summary in its UI. The summary
shows the A2A mapping and inert client/server plans, runtime candidate and
dimension counts, migrated adapter contracts with their explicit gaps, queued
live gates, and whether shared Vera contract
capabilities are actually registered. It also reports MCP/OpenAPI source
intake as an inspection-only lifecycle, separating its implemented discovery,
inspection, and proposal states from queued build, verification, approval, and
activation. “Implemented” there means the bounded,
deterministic planning contract exists; transport/listener execution remains
labelled `queued_live` until the explicit live gate is authorised and passes.
The source build contract adds an equally inert build/activation proposal for pinned Python, CLI,
OCI, and repository sources. Agent Bridges shows the plan-contract state and
source-kind count, but exposes no build or activation action; actual external
materialisation and conformance remain queued.

## Troubleshooting

For `llm.generate`, boolean thinking settings retain their boolean meaning even
when a tool transport supplies strings such as `"False"`. An Ollama response
without usable text returns `error_code: empty_generation` and does not save an
empty output artifact. Inspect the provider request log for the underlying
failure; an empty result alone does not identify its cause.

The opt-in `vera.models.live_inference_validation` module exercises the portable
Ollama adapter with sequential streaming and non-streaming requests. It requires
shared coordination, caps each case at 60 seconds, and records output hashes and
metrics. Transport success (completed, non-empty output) is separate from exact
expected-output conformance; a non-empty but incorrect answer cannot pass the
case. Prompt, output, and expected text remain omitted while their digests make
the verdict reproducible. The registry manifest digest identifies the selected
model declaration;
it is not an independent checksum of downloaded weights. These smoke checks do
not establish broader model quality, load tolerance, recovery, or cross-runtime parity.

Loop Lab sandboxes join the same GPU capacity queue through a narrow controller
broker. Each sandbox receives a rotated credential and can request, renew, or
release only opaque leases; it never receives the production Redis address,
lease key, or owner token. Missing authentication, an unavailable controller,
queue timeout, or renewal loss stops sandbox inference instead of silently
bypassing the shared limit. Production's existing gate behavior is unchanged.

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
- `vera/models/` — ModelPackage lifecycle, portable inference contracts,
  runtime adapters, and the inference provider registry.
- `vera/catalog/` — discovery and hardware-fit estimates.
- `vera/ide/` and `vera/board/` — coding-agent execution and work ownership.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
