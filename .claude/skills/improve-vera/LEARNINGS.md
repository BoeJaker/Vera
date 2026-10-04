# improve-vera — learnings

Append-only autopsy log (format: `skill-smith` §3). Newest at the bottom.

## 2026-10-04 — a seam that exists in code may not be on the runtime path
- task: planning retrieval changes (four-minds)
- what happened: a plan assumed rankers registered on `ContextRegistry` would affect `memory.seek`. A verification
  pass found the registry is used only by tests and one offline module.
- cause: reading a contract's code without checking its runtime callers
- rule now: before relying on a seam, grep for its non-test callers and confirm on the live instance. "Built" is
  not "wired".
- evidence: shared-planning/four-minds/EVIDENCE-2026-10-04.md §8b
- status: open

## 2026-10-04 — check prod directly before declaring it down
- task: four-minds planning
- what happened: the MCP bridge failed to connect while prod served `/health` and `/mcp/call` over HTTPS
- rule now: `GET https://<vera-host>:8999/health` first; the bridge is not prod
- evidence: session vera-62
- status: open
