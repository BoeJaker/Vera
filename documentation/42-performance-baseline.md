# Performance and capacity baseline

W0-03 starts with the authoring path because users can see code finish streaming
and then wait without knowing what Vera is doing. Disabling validation would make
that pause shorter at the cost of returning broken files; the first step is to
measure it precisely.

## Code author timing envelope

Successful `code.author` results now contain `timing` using schema
`vera.code-author-timing/v1`. Vera also emits a compact `code.author.timing`
event correlated by session, trace, and path.

The envelope reports total/model/post-generation time; delay from the last
visible streamed chunk until generation returns and until the result is ready;
preparation, parsing/syntax-repair, persistence, and smoke/runtime-repair phase
durations; repair/smoke counts; and timing-event emission overhead.

This distinguishes a model that remains busy after its last displayed token from
syntax repair, another repair-model call, file/version persistence, bounded
Python execution, and runtime-repair calls. Existing generation, syntax, save,
and smoke behavior is unchanged.

## Scope and next baseline

This slice adds deterministic instrumentation and tests; it deliberately does
not launch a model workload while other agents are active. The next authorized
benchmark should freeze representative authoring tasks, run them serially through
the shared model gate, and publish p50/p95 values by phase, language, file size,
repair count, route, and model. The same vocabulary can then cover prose authoring,
DAG/loop transitions, queue admission, and the gap from one capability result to
the next loop cycle.
