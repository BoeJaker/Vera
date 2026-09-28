# Frozen evaluation corpus

Vera has a versioned, reviewable source of evaluation intent rather than
scattering implied acceptance criteria across benchmark code and reports. The
canonical fixture is `evaluations/frozen-corpus-v1.json`.

Cases have stable IDs, a domain, a lane, a fixture reference, and exact expected
fields. `deterministic` cases must require no model or network call.
`queued_live` cases are inert until explicitly scheduled and must declare call,
time, and shared-GPU-gate budgets. The corpus policy forbids secrets and defaults
all live work to queued. A deterministic fixture must resolve to a repository
test file, optionally naming one exact pytest node; unresolved `planned:`
references fail validation. Queued live cases may retain a `planned:` reference
while their external prerequisite or controlled test window is unavailable.

`eval.corpus.inspect` validates the corpus and returns its structural
fingerprint plus domain/lane coverage. `detail=true` returns bounded case
metadata and supports lane/domain filters; inspection never executes cases.

The initial corpus covers Run recovery/control, Workflow IR round-trip and
fail-closed behavior, resolver effect safety, memory deletion/citations, Fabric
projection failure, structured generation, representative Operator readiness,
bounded research cancellation, and mixed interactive/background load. Fixture
references identify the executable deterministic evidence or the explicitly
queued live work needed to produce it; a declaration alone is not a passing
result.

Deterministic scoring compares declared dotted output paths using strict value
equality. Missing paths fail closed. Model-judge rubrics and real workload
adapters remain later slices and must preserve the frozen case identity and
budget rather than silently changing the benchmark.

## Resolver shadow lane

`evaluations/resolver-shadow-v1.json` is a separate synthetic-only corpus. Its
frozen registries, requests, redacted observations, expected selection, and
explicit unsafe implementation sets exercise effect rejection, output
compatibility, policy unknowns, observed health, and evidence ranking.

`evaluate_resolver_corpus` projects each synthetic registry through Capability
Contract v2 and calls only the pure shadow resolver. It reports exact selection
accuracy and unsafe-choice rate, and verifies every preview remains
`authorized: false` and `executed: false`. An invalid corpus scores zero and is
never partially executed. The lane makes no model, network, or capability call;
live/model comparison remains queued. `eval.resolver.shadow` exposes the
aggregate report and returns bounded per-case details only when requested.

## Policy and portable telemetry lanes

`evaluations/policy-boundary-v1.json` freezes adversarial cases for
prompt injection, alias and callback bypass, replayed approval, secret leakage,
and confused-deputy behavior. `eval.policy.boundary` evaluates synthetic facts
and receipts only; it does not invoke the selected capability or touch a network.

`evaluations/run-telemetry-v1.json` freezes portable telemetry cases.
`eval.run.telemetry` checks OTLP-compatible shape, lineage, retry and terminal
semantics, redaction, failure isolation, structural bounds, and default-off
behavior using injected transports. Export to a real collector remains a
separately queued live integration, so a deterministic pass proves the adapter
contract rather than external service operation.

Frozen Langfuse/Phoenix comparison evidence adds a decision layer above that
adapter gate. Both observations must bind the same portable trace and OTLP
digests; the report exposes fidelity, usability, evaluation linkage,
governance, resource, portability, outage, and teardown evidence independently.
It cannot contact, choose, configure, or activate either backend.

Together the lanes distinguish three kinds of evidence: declared contracts,
synthetic deterministic conformance, and explicitly budgeted live measurements.
Vera does not promote one kind into another implicitly.
