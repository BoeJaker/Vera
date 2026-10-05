# write-vera-capability — learnings

Append-only autopsy log (format: `skill-smith` §3). Newest at the bottom.

## 2026-10-04 — internal callers of a `memory="on"` cap write activity nodes
- task: four-minds recon
- what happened: `turn.understand` calls `nlp.ner` through `cap["func"]`, so each chat reply wrote up to 12
  activity nodes. Scheduled caps do the same.
- cause: `func` is the memory-recording wrapper; `raw` is the bare function
- rule now: when one capability calls another for internal work, call `cap["raw"]`, or register the callee
  `memory="off"`/silent. Keep `func` for calls that should be recorded.
- evidence: recon-fixes RF-01, RF-10; `vera/canvas/enrich_capabilities.py:36-41`
- status: open

## 2026-10-04 — `memory_hooks._should_record_cap` is dead code
- what happened: docs cite it as the record gate, but it has no callers
- rule now: the gate is the wrapper in `capability_orchestration.py` (≈7000-7010); don't cite `_should_record_cap`
- evidence: recon-fixes RF-14
- status: open
