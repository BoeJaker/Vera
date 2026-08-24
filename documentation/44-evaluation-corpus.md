# Frozen evaluation corpus

W0-02 gives Vera a versioned, reviewable source of evaluation intent rather
than scattering implied acceptance criteria across benchmark code and reports.
The canonical fixture is `evaluations/frozen-corpus-v1.json`.

Cases have stable IDs, a domain, a lane, a fixture reference, and exact expected
fields. `deterministic` cases must require no model or network call.
`queued_live` cases are inert until explicitly scheduled and must declare call,
time, and shared-GPU-gate budgets. The corpus policy forbids secrets and defaults
all live work to queued.

`eval.corpus.inspect` validates the corpus and returns its structural
fingerprint plus domain/lane coverage. `detail=true` returns bounded case
metadata and supports lane/domain filters; inspection never executes cases.

The initial corpus covers Run recovery/control, Workflow IR round-trip and
fail-closed behavior, resolver effect safety, memory deletion/citations, Fabric
projection failure, structured generation, representative Operator readiness,
bounded research cancellation, and mixed interactive/background load. Fixture
references beginning with `planned:` are honest gaps, not passing tests.

Deterministic scoring compares declared dotted output paths using strict value
equality. Missing paths fail closed. Model-judge rubrics and real workload
adapters remain later slices and must preserve the frozen case identity and
budget rather than silently changing the benchmark.
