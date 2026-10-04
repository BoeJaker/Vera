# vera-coordinate — learnings

Append-only autopsy log (format: `skill-smith` §3). Newest at the bottom.

## 2026-10-04 — plans and handovers were scattered across five places
- task: four-minds / agent-ops recon (unit agent-ops)
- what happened: plans turned up in five places besides the shared planning area, so agents could not find each
  other's work:
  - `Notes/` (gitignored)
  - a tracked repo-root roadmap
  - `~/.claude/plans/`
  - agent memory notes
  - stale worktree copies
- cause: the rule lived only inside two long skills; no conventions file existed
- rule now: one home per kind of thing (SKILL §1); `shared-planning/README.md` is the conventions file
- evidence: shared-planning/agent-ops/PLAN.md §0, §10
- status: folded into SKILL.md v1

## 2026-10-04 — "Vera is down" was wrong: only the MCP bridge was
- task: four-minds planning
- what happened: the session treated Vera as unreachable for hours because the `vera` MCP server failed to
  connect. Prod answered `GET /health` and `POST /mcp/call` over HTTPS the whole time.
- cause: no rule to check prod directly before entering degraded mode
- rule now: check `GET https://<vera-host>:8999/health` before declaring Vera down (SKILL §5)
- evidence: session vera-62, 2026-10-04
- status: folded into SKILL.md v1

## 2026-10-04 — finding a peer session by broadcast cost ten sessions a turn each
- task: asking the loop-analysis owner to review a plan
- what happened: there was no way to map a work unit to a live session, so a review request went to every peer;
  most replied "not me"
- cause: unit READMEs did not name the owning session
- rule now: a unit README header names `Owner: <agent:session>`; comment on the unit's board item instead of
  broadcasting
- evidence: four-minds ROADMAP §14 v2; shared-planning/README.md §2
- status: folded into SKILL.md v1

## 2026-10-04 — standards sent by peer message are, rightly, not adopted without the owner
- task: broadcasting the planning/comms standards
- what happened: some sessions declined to adopt conventions on a peer's word and asked their owner
- cause: peer messages carry no owner authority
- rule now: new standing conventions need the owner's confirmation in each session, or a skill / AGENTS.md change
  landed through the pipeline. A broadcast is a notice, not an instruction.
- evidence: vera-13 reply, 2026-10-04
- status: open
