# vera-design-adopt — learnings

Append-only autopsy log (format: `skill-smith` §3). Newest at the bottom.

## 2026-10-04 — 28 recorded design-landing failures, consolidated for v2
- task: agent-ops recon of the last design programme (Notes 41–43, the design-adopt handovers, the LHM audit, memory
  notes)
- what happened:
  - mechanisms landed without the board's look;
  - work was graded against old screenshots instead of the rendered board;
  - design files were not kept;
  - "done" was claimed without the owner;
  - only some panes were checked;
  - source scans overcounted panels;
  - spec boards were graded as UI;
  - there were no per-size variants;
  - the state file was stale for weeks;
  - the mirror misreported its refresh.
- cause:
  - the skill has no board-role inventory, no viewport matrix and no implementation contract;
  - its survey is source-only;
  - it has no owner-verdict field;
  - it is hard-wired to one canvas (Notes 37–41, M1–M8).
- rule now: follow "Landing a UI design" in `vera-coordinate/references/complex-projects.md`:
  - board roles;
  - viewports × states;
  - an implementation contract written during design;
  - keep the design files;
  - a live UI + backend survey;
  - compare against the rendered board;
  - one state record that separates my-check from owner-verdict.
- evidence: shared-planning/agent-ops/PLAN.md §10 (the full list, with sources), WS-F
- status: open (v2 = agent-ops AO-F.1–F.9)
