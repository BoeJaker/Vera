# 46 · Interoperability Foundations

Vera's recent foundation work turns a broad collection of capabilities, loops,
workflows, model tools, and external integrations into layers that can cooperate
without becoming one another. This guide describes shipped architecture; it is
not an internal roadmap or a claim that queued live integrations are already
production-ready.

## The common control path

Every integration should cross the same five boundaries:

1. **Contract** — canonical task, schemas, effects, lifecycle, resources, and
   provider identity.
2. **Resolution** — deterministic eligibility and ranking, initially observable
   in shadow mode and never equivalent to authorization.
3. **Policy** — local authority for effects, approvals, secrets, filesystem,
   network, tenancy, and budgets.
4. **Run protocol** — runtime-neutral identity, events, cancellation, retries,
   artifacts, deadlines, and teardown while the native runtime retains its task.
5. **Evidence** — bounded activity, traces, timings, evaluation receipts,
   provenance, and artifacts that can be inspected without leaking payloads.

This decomposition addresses capability and loop sprawl. A model provider,
workflow engine, or agent protocol normally supplies an adapter and explicit
semantic-gap report; it should not introduce another incompatible
planner/executor/event stack.

## Capability selection and policy

Capability Contract v2 projects migrated and legacy registry entries into one
manifest. Missing declarations stay `unknown`. Coverage and lint make migration
measurable, while the strict gate applies only to explicitly selected families.
Resolver shadow mode compares candidates without invoking them. Policy observes
and selectively enforces declared effects independently of resolver rank, so
better health or latency never grants more authority.

The frozen evaluation corpus makes these decisions reproducible.
Provider-neutral training/evaluation contracts and deterministic providers
produce portable evidence without importing or invoking optional external
libraries during the offline lane.

## Workflows and durable execution

Workflow IR provides typed, bounded control flow and loss-aware adapter analysis.
Runtime durability semantics describe leases, heartbeats, retries, idempotency,
recovery, cancellation, versioning, and artifacts before a third-party engine is
selected. Static DBOS and Temporal decision manifests record what maps cleanly,
what is lossy, and which live failure drills are still required. Neither imports
its optional runtime or claims production readiness.

This lets Vera compare native DAG execution with libraries such as LangGraph,
DBOS, or Temporal around one Run protocol instead of wrapping each library in a
new agent loop.

The LIB18 runtime matrix applies that boundary to agent frameworks. It keeps
upstream-documented features separate from Vera-verified bridge behavior across
native Vera, LangGraph, PydanticAI, Smolagents, OpenClaw, Google ADK, OpenAI
Agents SDK, Strands, Agno, and Hermes-compatible paths. Static declarations can
identify gaps and required tests; they cannot select a universal winner.

## Models, ONNX, and Worldview

`ModelPackage` is the portable boundary between training, evaluation, import,
activation, and serving. Packages carry immutable identity and provenance; safe
ONNX import verifies content before admission; activation emits receipts; and
legacy model capabilities can bind to packages without silently changing their
public names. Admission and evaluation stay separate from hardware execution.

Worldview follows the same migration pattern. Frozen graph/vector projection
manifests, legacy snapshots, parity reports, and bounded evidence windows expose
revision drift and missing provenance without copying source content or changing
the current JEPA trainer. The projection is evidence—not authority—until live
shadow parity and migration gates are deliberately completed.

## Remote agents and A2A

The offline A2A v1.0 foundation maps Agent Cards, skills, task states, messages,
parts, artifacts, and protocol operations onto Vera's contracts and Run model.
Remote task/context identifiers remain opaque; they are never replaced by local
session IDs. Cards are bounded, credential-free HTTPS discovery documents, not
registration or authorization grants. Required unsupported extensions and
unsafe metadata fail closed.

`interop.a2a.conformance` exposes the static mapping and its deterministic/live
lanes. It intentionally reports that no A2A client or server is implemented.
Sending, streaming, authentication, cancellation acknowledgement, artifact
fetching, webhook security, recovery, and teardown require separately authorized
live evidence.

## Activity, UI, and coordination

Runs and capability activity provide a shared evidence vocabulary for Harness,
chat/context graphs, memory views, activity timelines, and narrator consumers.
UI projections should link to underlying run/action metadata instead of
inventing a parallel state model. Explicit unknowns are preferable to a polished
timeline that guesses what occurred.

The Agent Bridges panel is the interoperability-specific projection. It shows
LIB02's A2A foundation and inert client/server plan status, LIB18 runtime
candidate/dimension coverage, queued live cases, and registration state for the
shared Run, Workflow IR, telemetry, durability, runtime-matrix, A2A, Capability
v2, resolver/policy, and source-intake capabilities. It also distinguishes
W3-07's implemented inert build-plan contract from queued build and activation
execution. LIB-04 adds the same distinction for structured generation: the
portable schema/validation/retry contract is implemented, while provider-native,
Instructor, Outlines, streaming, and model execution remain queued. LIB-06 adds
the same honest boundary for documents: Agent Bridge displays the portable
parse/frozen-corpus contract, supported media types, stable citation identity,
and the `not_imported` Docling profile. Parsing, OCR, fidelity measurement, and
teardown execution remain queued, and no document content is projected into the
panel. This complements—rather than duplicates—the existing Run evidence
in Activity, the harness overlay, Chat/LHM, and Memory Graph. Readiness labels
come from the backing inspection capability; the panel does not infer readiness
from package presence or a successful page load.

Loop Lab follows the same principle. The board is the authoritative operational
plan; sandbox work plans are concise handoffs; private Markdown shared across
worktrees lives under `<git-common-dir>/vera-work/shared-planning/` and is never
published.

## Boundaries that remain deliberate

- Contracts describe; policy authorizes.
- Source inspection proposes; the build-plan contract specifies immutable
  provenance, evidence, least privilege, approval, rollback, export, upgrade,
  and teardown without performing them. Catalogs, builders, and activation
  remain separate reviewed state transitions.
- Resolver observations inform; they do not mutate declarations.
- Workflow adapters translate; native runtimes keep native authority.
- Model packages identify artifacts; activation remains reviewed.
- Structured schemas validate supplied values; provider decoding and semantic
  validators remain separately authorized runtime work.
- Document-parser plans validate supplied provenance, IDs, citations, hashes,
  bounds, and OCR declarations; Docling conversion and persistence remain
  separately authorized isolated runtime work. Render remains output policy.
- Worldview projections provide parity evidence; they do not feed training by
  default.
- A2A discovery projects candidates; it does not register or execute them.
- Live/model/external conformance tests stay queued until their environment and
  authority are explicitly provided.

## Related guides

- [Capability framework](01-capability-framework.md)
- [Worldview](11-worldview.md)
- [Execution](12-execution.md)
- [Machine learning](16-machine-learning.md)
- [ONNX](30-onnx.md)
- [Loop Lab](33-evolve.md)
- [Agent runtimes and providers](36-agent-runtimes-providers.md)
- [Activity and boards](39-activity-boards.md)
- [Capability contracts](43-capability-contracts.md)
- [Evaluation corpus](44-evaluation-corpus.md)
- [Capability policy](45-capability-policy.md)
