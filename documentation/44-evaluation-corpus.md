# Frozen evaluation corpus

Vera keeps a versioned, reviewable source of evaluation intent instead of
scattering implied acceptance criteria across benchmark code and reports. Each
**frozen corpus** is a JSON file under [`evaluations/`](../evaluations/) with
stable case IDs, exact expectations, and an explicit execution policy. A small
set of read-only capabilities validates and runs the deterministic lanes on
demand; none of them calls a model, touches the network, or invokes the
capability under test.

The corpora exist so that decisions about capability selection, policy, and
telemetry can be reproduced exactly: a resolver change, a policy change, or a
new adapter is judged against the same frozen cases every time, and live or
model-backed measurements are kept visibly separate from synthetic conformance.
The general corpus validator and resolver lane live in
[`vera/evaluation_corpus_core.py`](../vera/evaluation_corpus_core.py); the
policy lane in [`vera/capability_policy_eval_core.py`](../vera/capability_policy_eval_core.py);
the telemetry lane in [`vera/execution/telemetry_eval_core.py`](../vera/execution/telemetry_eval_core.py);
and the ontology decision lane in
[`vera/ontologies/capability_ontology_evaluation.py`](../vera/ontologies/capability_ontology_evaluation.py).

**Status:** all five corpora and their capabilities are shipped. Deterministic
lanes pass in a stock install. Queued live cases are declarations only; they are
never executed automatically.

## Contents

- [Kinds of evidence](#kinds-of-evidence)
- [Source map](#source-map)
- [Corpus files at a glance](#corpus-files-at-a-glance)
- [Capability reference](#capability-reference)
- [General corpus](#general-corpus)
  - [Case shape](#case-shape)
  - [Lanes](#lanes)
  - [Validation rules](#validation-rules)
  - [Current cases](#current-cases)
  - [Deterministic scoring](#deterministic-scoring)
- [Resolver shadow lane](#resolver-shadow-lane)
- [Policy boundary lane](#policy-boundary-lane)
- [Portable telemetry lane](#portable-telemetry-lane)
- [Capability ontology decision lane](#capability-ontology-decision-lane)
- [Observability backend comparison evidence](#observability-backend-comparison-evidence)
- [Worked examples](#worked-examples)
- [Adding or changing a case](#adding-or-changing-a-case)
- [Failure modes and troubleshooting](#failure-modes-and-troubleshooting)
- [Related pages](#related-pages)

## Kinds of evidence

The lanes distinguish three kinds of evidence and never promote one into another
implicitly:

| Evidence | Example | What it proves |
|---|---|---|
| Declared contract | A Capability Contract v2 declaration | What an implementation *claims* to do |
| Synthetic deterministic conformance | Resolver, policy, telemetry, ontology lanes | That Vera's pure decision logic behaves as specified on frozen inputs |
| Budgeted live measurement | `queued_live` cases in the general corpus | Real behaviour of models, browsers, or services — only when explicitly scheduled |

A declaration alone is not a passing result, and a deterministic pass proves an
adapter contract rather than external service operation.

## Source map

| File | Responsibility |
|---|---|
| [`evaluations/frozen-corpus-v1.json`](../evaluations/frozen-corpus-v1.json) | General cross-subsystem corpus (deterministic + queued live). |
| [`evaluations/resolver-shadow-v1.json`](../evaluations/resolver-shadow-v1.json) | Synthetic registries for the shadow resolver. |
| [`evaluations/policy-boundary-v1.json`](../evaluations/policy-boundary-v1.json) | Adversarial approval-receipt cases. |
| [`evaluations/run-telemetry-v1.json`](../evaluations/run-telemetry-v1.json) | Portable Run telemetry / OTLP conformance cases. |
| [`evaluations/capability-ontology-decision-v1.json`](../evaluations/capability-ontology-decision-v1.json) | Baseline vs curated vs generated ontology preference hints. |
| [`vera/evaluation_corpus_core.py`](../vera/evaluation_corpus_core.py) | `load_corpus`, `validate_corpus`, `canonical_fingerprint`, `score_case`, `validate_resolver_corpus`, `evaluate_resolver_corpus`. |
| [`vera/capability_policy_eval_core.py`](../vera/capability_policy_eval_core.py) | `validate_policy_corpus`, `evaluate_policy_corpus`. |
| [`vera/execution/telemetry_eval_core.py`](../vera/execution/telemetry_eval_core.py) | `validate_telemetry_corpus`, `evaluate_telemetry_corpus` (async, injected transports). |
| [`vera/ontologies/capability_ontology_evaluation.py`](../vera/ontologies/capability_ontology_evaluation.py) | `evaluate_ontology_decision_corpus`. |
| [`vera/execution/observability_comparison.py`](../vera/execution/observability_comparison.py) | Library-only evidence contract for comparing observability backends. |
| `tests/test_evaluation_corpus.py`, `tests/test_capability_policy_eval.py`, `tests/test_telemetry_eval.py` | Regression tests for the lanes. |

## Corpus files at a glance

| File | Schema | Revision | Cases | Policy flags |
|---|---|---|---|---|
| `frozen-corpus-v1.json` | `vera.evaluation-corpus/v1` | `1` | 11 (7 deterministic, 4 queued live) | `synthetic_only`, `live_default: queued`, `model_calls_by_default: false`, `secrets_allowed: false` |
| `resolver-shadow-v1.json` | `vera.resolver-shadow-corpus/v1` | `1` | 6 | `synthetic_only`, `model_calls: false`, `network_calls: false`, `capability_execution: false` |
| `policy-boundary-v1.json` | `vera.policy-boundary-corpus/v1` | — | 6 | `executes_capabilities: false`, `uses_network: false`, `uses_external_secrets: false` |
| `run-telemetry-v1.json` | `vera.run-telemetry-corpus/v1` | `2026-08-25.1` | 7 | as policy lane, plus `measures_wall_clock: false` |
| `capability-ontology-decision-v1.json` | `vera.capability-ontology-decision-corpus/v1` | `1` | 5 | `synthetic_only`, no model/network/capability execution, `ontology_activation: false` |

Each specialised lane checks its policy block exactly; a corpus whose policy
would permit execution, network, or real secrets is rejected before any case is
evaluated.

## Capability reference

All five are `silent`, `memory="off"`, declare `effects: ["read"]` (the resolver
and ontology lanes also declare `filesystem` for reading the fixture), and read
their fixture from the repository's `evaluations/` directory.

| Capability | HTTP | Inputs | Returns |
|---|---|---|---|
| `eval.corpus.inspect` | `GET /eval/corpus` | `detail`, `lane`, `domain`, `limit` (1–200, default 50) | Validation report, fingerprint, lane/domain counts, revision, policy; with `detail=true`, bounded case metadata (`matched`, `returned`, `truncated`, `cases`) |
| `eval.resolver.shadow` | `GET /eval/resolver/shadow` | `detail`, `limit` (1–200) | `selection_accuracy`, `unsafe_choice_rate`, fingerprint, `ok`; per-case details only with `detail=true` |
| `eval.policy.boundary` | `GET /eval/policy/boundary` | `detail`, `limit` (1–100) | `total`, `passed`, `failed`, `gate_coverage`, `missing_gates`, `content_free`, `ok` |
| `eval.run.telemetry` | `GET /eval/run/telemetry` | `detail`, `limit` (1–100) | Same report shape as the policy lane plus `measures_wall_clock` |
| `eval.ontology.decision` | `GET /eval/ontology/decision` | `detail`, `limit` (1–200), `timing_repetitions` (1–200, default 20) | Per-variant metrics, `generated_gate`, `decision` |

`eval.corpus.inspect` never executes cases: it only validates the corpus and
returns metadata.

## General corpus

### Case shape

```json
{
  "id": "resolver.disallowed-effect",
  "domain": "tool-selection",
  "lane": "deterministic",
  "fixture_ref": "tests/test_capability_contract_v2.py",
  "expected": {"selected": null, "authorized": false, "executed": false}
}
```

Queued live cases additionally carry a `budget`:

```json
{
  "id": "generation.structured-stream",
  "domain": "generation",
  "lane": "queued_live",
  "fixture_ref": "bench deterministic JSON pack",
  "budget": {"max_calls": 6, "timeout_ms": 180000, "gpu_gate": true},
  "expected": {"schema_valid": true, "stream_complete": true}
}
```

### Lanes

| Lane | Rule |
|---|---|
| `deterministic` | Must require no model or network call. `fixture_ref` must resolve to a repository test file (`tests/...py`), optionally naming one exact pytest node (`::test_name`). Path traversal and `planned:` references fail validation. |
| `queued_live` | Inert until explicitly scheduled. Must declare a `budget` with call, time, and shared-GPU-gate limits. May keep a `planned:` reference while its prerequisite or test window is unavailable. |

### Validation rules

`validate_corpus` returns `{schema: "vera.evaluation-corpus-report/v1", ok,
issues, case_count, lanes, domains, fingerprint}`. Issue codes:

| Code | Meaning |
|---|---|
| `schema.invalid` | Top-level `schema` is not `vera.evaluation-corpus/v1`. |
| `cases.invalid` / `case.invalid` | `cases` is not a list, or a case is not an object. |
| `case.id_required` / `case.id_duplicate` | Missing or repeated ID. |
| `case.lane_invalid` | Lane is not `deterministic` or `queued_live`. |
| `case.domain_required` | Missing domain. |
| `case.expected_required` | `expected` is missing or empty. |
| `case.fixture_ref_required` | Missing fixture reference. |
| `case.deterministic_fixture_unresolved` | Deterministic case still uses `planned:`. |
| `case.deterministic_fixture_invalid` | Deterministic fixture is not a `tests/*.py[::node]` path, or contains `..`. |
| `case.live_budget_required` | Queued live case has no budget. |

The fingerprint is a SHA-256 over the canonical JSON of the corpus (excluding any
top-level `fingerprint` key), so any edit to a case changes it.

### Current cases

| ID | Domain | Lane | Fixture / budget | Expected fields |
|---|---|---|---|---|
| `run.recovery.catalog` | run | deterministic | `tests/test_run_projection.py` | `recovered`, `executed` |
| `run.control.no-second-authority` | run | deterministic | `tests/test_run_journal.py` | `authorized`, `executed` |
| `workflow.roundtrip.portable` | workflow | deterministic | `tests/test_workflow_ir.py` | `roundtrip`, `stable_hash` |
| `workflow.unsupported.fail-closed` | workflow | deterministic | `tests/test_workflow_ir.py` | `accepted`, `executed` |
| `resolver.disallowed-effect` | tool-selection | deterministic | `tests/test_capability_contract_v2.py` | `selected`, `authorized`, `executed` |
| `memory.citation-delete` | memory | deterministic | `tests/test_memory_provider.py::test_tombstones_hidden_by_default_and_text_can_be_redacted` | `citation_preserved`, `delete_propagated` |
| `fabric.projection-failure` | fabric | deterministic | `tests/test_revision_store.py::test_projection_failure_reconcile_rebuild_and_stale_cas` | `source_preserved`, `projection_status` |
| `generation.structured-stream` | generation | queued live | 6 calls, 180 s, GPU gate | `schema_valid`, `stream_complete` |
| `operator.delayed-ui-readiness` | operator | queued live | 1 call, 90 s, no GPU gate | `ready_state`, `blank_capture` |
| `research.bounded-cancel` | research | queued live | 1 call, 600 s, GPU gate | `deadline_enforced`, `lease_released` |
| `mixed.interactive-background` | load | queued live | 20 calls, 300 s, GPU gate | `interactive_starved`, `cancel_released` |

Together these cover Run recovery/control, Workflow IR round-trip and
fail-closed behaviour, resolver effect safety, memory deletion and citations,
Fabric projection failure, structured generation, representative Operator
readiness, bounded research cancellation, and mixed interactive/background load.

### Deterministic scoring

`score_case(case, observation)` compares each declared dotted output path in
`expected` against an observation dict using strict value equality. A missing
path fails closed. It returns `{case_id, passed, total, score, ok, checks}`, where
each check records `path`, `expected`, `actual`, `present`, and `passed`.

`score_case` is a library function used by tests and adapters; no capability
runs it against live output. Model-judge rubrics and real workload adapters are
not implemented and, when added, must preserve the frozen case identity and
budget rather than silently changing the benchmark.

## Resolver shadow lane

[`evaluations/resolver-shadow-v1.json`](../evaluations/resolver-shadow-v1.json)
is a synthetic-only corpus. Each case carries its own frozen registry, a
resolver request, optional redacted observations, the expected selection (or
`null`), and the set of `unsafe_names` that must never be chosen.

| Case | Request | Expected | Unsafe |
|---|---|---|---|
| `resolver.disallowed-effect` | `records.read`, allowed `read` | none selected | `unsafe.delete` |
| `resolver.safe-effect-over-unsafe` | `records.read`, allowed `read` | `safe.read` | `unsafe.delete` |
| `resolver.output-contract` | `answer.generate`, output must have string `answer` | `answer.text` | `answer.number` |
| `resolver.policy-unknown-fails-closed` | `records.read`, `network` must be `not_required` | `network.safe` | `network.unknown` |
| `resolver.observed-health` | `text.generate`, allowed `model` | `provider.healthy` | `provider.unhealthy` |
| `resolver.quality-ranking` | `text.generate`, allowed `model` | `provider.high` | — |

`evaluate_resolver_corpus` projects each synthetic registry through
[Capability Contract v2](43-capability-contracts.md) and calls only the pure
`resolve_shadow`. It reports exact `selection_accuracy` and `unsafe_choice_rate`,
and a case only passes if the selection is exact, no unsafe implementation was
chosen, and the preview still reports `authorized: false` and `executed: false`.
An invalid corpus (bad schema, non-executing policy not asserted, unknown
observation or unsafe names, expected selection not in the registry) scores zero
and is never partially executed.

## Policy boundary lane

[`evaluations/policy-boundary-v1.json`](../evaluations/policy-boundary-v1.json)
freezes adversarial cases against the [approval receipt](45-capability-policy.md#approval-receipts)
primitive. All six required gates must be covered or the corpus is rejected.

| Case | Gate | Operation | Expected reason |
|---|---|---|---|
| `prompt-session-substitution` | `prompt_injection` | Verify with an injected session string | `session_mismatch` |
| `canonical-capability-substitution` | `alias_bypass` | Verify against a different capability | `capability_mismatch` |
| `callback-field-injection` | `callbacks` | Add an extra `callback` field to the payload | `malformed` |
| `second-consumption` | `replayed_approvals` | Consume the same receipt twice | `replayed` |
| `content-free-valid-report` | `secret_leakage` | Verify a valid receipt | `valid` |
| `tenant-scope-substitution` | `confused_deputy` | Verify under another tenant | `tenant_mismatch` |

The evaluator signs receipts with a fixed synthetic key, uses an in-process nonce
ledger, invokes no capability, and performs no network access. After evaluating,
it checks that the serialised report contains no key material, nonce, or fixture
text (`content_free`); `ok` requires every case to pass *and* the report to be
content-free. Only reason codes are returned.

## Portable telemetry lane

[`evaluations/run-telemetry-v1.json`](../evaluations/run-telemetry-v1.json)
freezes portable telemetry cases for the Run → OpenTelemetry projection.

| Case | Gate | What it checks |
|---|---|---|
| `golden-otlp` | `otlp_shape` | Exact OTLP span identity, timing, status, OpenInference kind, event names |
| `nested-lineage` | `correlation` | Parent/child spans share a trace; tool child error type, retry attempt, artifact scheme, checksum algorithm |
| `terminal-status` | `terminal_semantics` | completed → `OK`, cancelled → `UNSET`, timed out/failed → `ERROR`; retry events present |
| `content-redaction` | `redaction` | Portable and OTLP projections contain no forbidden content |
| `isolated-failure` | `failure_isolation` | A transport timeout does not export, call the sender more than once, change stats, or leak the message |
| `bounded-projection` | `bounded_work` | Oversized runs truncate to 200 spans/events and refuse to send |
| `offline-default` | `default_off` | With nothing configured, nothing is exported and the queue stays disabled |

`eval.run.telemetry` uses injected transports and leaves live exporter counters
untouched. Export to a real collector is separate live integration work, so a
deterministic pass proves the adapter contract rather than external service
operation.

## Capability ontology decision lane

[`evaluations/capability-ontology-decision-v1.json`](../evaluations/capability-ontology-decision-v1.json)
asks whether capability-ontology relations (for example "`code.author` is
preferred over `llm.generate` for `code.generate`") should influence the
resolver. Each case is run under three variants:

| Variant | Hints applied |
|---|---|
| `baseline` | The unchanged shadow resolver |
| `curated` | Hand-curated `preferred_over` relations |
| `generated` | Machine-generated relations |

Per variant the report gives `selection_accuracy`, `unsafe_choice_rate`,
`ambiguity_rate`, `relation_precision`, `invalid_relation_cases`,
`applied_relations`, hint size in bytes and estimated tokens, and local p50/p95
timing in microseconds.

Generated relations pass the gate only if they improve accuracy over baseline,
choose nothing unsafe, do not increase ambiguity, have no invalid relation cases,
and reach relation precision of at least 0.9. Otherwise the decision is
`disable_generated_relations`. The lane is shadow-only (`authoritative: false`):
it never activates ontology influence. Cases include
`effects-remain-authoritative` and `caller-preference-remains-authoritative`, so
hints can never override effect safety or an explicit caller preference. See
[Skills and ontologies](18-skills-ontologies.md).

## Observability backend comparison evidence

[`vera/execution/observability_comparison.py`](../vera/execution/observability_comparison.py)
defines a frozen, provider-neutral evidence contract
(`vera.observability-comparison/v1`) for comparing observability backends such as
Langfuse and Phoenix. Both observations must bind the same content-redacted
portable trace and OTLP digest. The report keeps fidelity, usability, evaluation
linkage, governance, resource, portability, outage, and teardown evidence
separate, with no composite score or winner. It cannot contact, choose,
configure, or activate a backend, and it is not exposed as a capability; real
backend measurements remain queued. See
[AI ecosystem roadmap](40-ai-ecosystem-roadmap.md#observability-backends).

## Worked examples

Validate the general corpus:

```bash
curl -s http://localhost:8999/eval/corpus | python3 -m json.tool
```

```json
{
  "schema": "vera.evaluation-corpus-report/v1",
  "ok": true, "issues": [], "case_count": 11,
  "lanes": {"deterministic": 7, "queued_live": 4},
  "domains": {"fabric": 1, "generation": 1, "load": 1, "memory": 1, "operator": 1,
              "research": 1, "run": 2, "tool-selection": 1, "workflow": 2},
  "fingerprint": "…", "revision": 1,
  "policy": {"synthetic_only": true, "live_default": "queued",
             "model_calls_by_default": false, "secrets_allowed": false}
}
```

List only the queued live cases:

```bash
curl -s 'http://localhost:8999/eval/corpus?detail=true&lane=queued_live'
```

Run every deterministic lane through MCP:

```bash
for cap in eval.resolver.shadow eval.policy.boundary eval.run.telemetry eval.ontology.decision; do
  curl -s http://localhost:8999/mcp/call -H 'content-type: application/json' \
    -d "{\"name\":\"$cap\",\"arguments\":{}}" | python3 -c \
    'import json,sys; c=json.load(sys.stdin)["content"]; print(c.get("schema"), c.get("ok"), c.get("decision",""))'
done
```

Expected on a healthy checkout: every lane reports `ok: true`; the resolver lane
reports `selection_accuracy: 1.0` and `unsafe_choice_rate: 0.0`; the ontology
lane reports `decision: disable_generated_relations`.

Offline, without a running server:

```python
from pathlib import Path
from Vera.vera.evaluation_corpus_core import load_corpus, evaluate_resolver_corpus

report = evaluate_resolver_corpus(load_corpus(Path("evaluations/resolver-shadow-v1.json")))
print(report["selection_accuracy"], report["unsafe_choice_rate"])
```

## Adding or changing a case

1. Prefer adding a new case with a new stable ID over editing an existing one;
   an edited case is a different benchmark.
2. For a deterministic case, write or point at a test in `tests/` first and use
   its path (and optionally `::test_name`) as `fixture_ref`.
3. For live work, use `lane: "queued_live"` with an explicit `budget`
   (`max_calls`, `timeout_ms`, `gpu_gate`).
4. Never put secrets, real user content, or production identifiers in a corpus.
5. Run the matching capability (or its `tests/test_*` suite) and confirm `ok`.
   The fingerprint will change; that is expected and reviewable.

## Failure modes and troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ok: false` with `case.deterministic_fixture_unresolved` | A deterministic case still references `planned:` | Point it at a real test, or move it to `queued_live` with a budget |
| Resolver lane `ok: false`, `policy.nonexecuting_required` | Corpus policy no longer asserts synthetic, non-executing operation | Restore the policy block exactly |
| Policy lane `required_gates_invalid` / `gate_coverage_missing:…` | A required attack class was removed | Keep all six gates covered |
| Policy or telemetry lane `content_free: false` | A report leaked fixture text or key material | Treat as a regression in the evaluator or receipts module |
| `FileNotFoundError` from an `eval.*` capability | Running from an install without the `evaluations/` directory | Capabilities read `<repo>/evaluations/*.json`; keep it alongside `vera/` |
| `timing_repetitions must be between 1 and 200` | Out-of-range input to `eval.ontology.decision` | Use 1–200 |

## Related pages

- [Capability contracts](43-capability-contracts.md) — the manifests the resolver lane projects.
- [Capability policy](45-capability-policy.md) — receipts and verdicts the policy lane attacks.
- [Interoperability foundations](46-interoperability-foundations.md) — how the lanes fit the wider control path.
- [Execution](12-execution.md) — Run protocol and portable telemetry.
- [Skills and ontologies](18-skills-ontologies.md) — capability ontology relations.
- [Performance baseline](42-performance-baseline.md) — measured performance evidence, kept separate from these conformance lanes.
- [AI ecosystem roadmap](40-ai-ecosystem-roadmap.md) — which external evaluation and observability tools are planned.
