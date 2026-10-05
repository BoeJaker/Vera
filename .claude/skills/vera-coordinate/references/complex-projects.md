# Planning and executing complex work

For anything multi-session, multi-agent, or that replaces something users rely on: redesigns, subsystem rewrites,
programmes like four-minds. Distilled from the recorded failures of earlier programmes (design adoption, dashboard
packs, LHM unification, census improvement). The full list of 28 design-landing failures is in
`shared-planning/agent-ops/PLAN.md` §10.

## The general method

1. **Survey before you plan, from the live system as well as the source.** Inventory the surfaces the work touches:
   UI places (`ui.places`, `/ui/panels`, `RETIRED_TABS`), capabilities and their real response shapes and
   latencies, stores, schedules, consumers. Source scans overcount (modules shadow registrations; a registered
   panel isn't necessarily a tab), so confirm against the live instance. Write it to
   `<unit>/EVIDENCE-<date>.md` with `path:line` and dates.
2. **Plan as work packages** (SKILL §6): done-when, conflicts, executor. Put an explicit **kill criterion** on
   anything speculative.
3. **Must-keep list before replacing anything.** Every handler, control, pane, widget, capability and behaviour the
   live survey found becomes a keep row. Dropping a row needs the owner's own words, and silence is not consent.
4. **Slice.** Each slice can be landed, tested and reverted on its own, with its own pipeline. Order slices by
   dependency, and put cheap no-model or deletion wins first.
5. **Verify in the owner's real context**:
   - the embedded view, real data, every pane;
   - the served page after confirming the mirror's head and port;
   - not a fresh empty session, not an old screenshot.

   Record **"my check"** (evidence: sha, screenshot, readout) separately from **"owner verdict"** (their words,
   dated). Never claim "done" on your check alone.
6. **Checkpoint with the owner** after the first slice of each region, before scaling the pattern.
7. **Record decisions** in `<unit>/DECISIONS.md`: the owner's decisions in their words, dated, and decisions you
   made with the reason. Don't reopen them without new evidence.
8. **Review adversarially before each landing** (`improve-vera-sandboxed` §5). For a whole plan, run a red-team pass
   and a code-verification pass on the plan itself before building. Plans built from unverified claims fail.
9. **Autopsy at the end** (`skill-smith`): what went wrong, which rule caught it or should have, and append the
   lesson to the responsible skill's LEARNINGS.

## Landing a UI design (with `vera-design` and `vera-design-adopt`)

Build UI to `vera-design` (the bootstrap, tokens, menu/settings/appearance contracts, Playwright verification).
Land a designed canvas with `vera-design-adopt`. Add these steps; agent-ops WS-F is building them into
`vera-design-adopt` v2:

1. **Board inventory before mapping.** Give every board on the canvas a role:
   - mood board
   - spec / implementation notes
   - variation of X (and whether it was chosen)
   - land-target
   - storyboard / motion
   - reference
   - QC / audit

   Only land-targets get parity rows. Spec boards become the **implementation contract**. Variations need the
   owner's pick before slicing.
2. **Screen sizes are data.** Each land-target lists its viewports (e.g. 1440×1000, 2560, 3440×1440, narrow ≤ 800),
   its UI scales (1.0, 1.4) and its states (tiers, packs, empty/loading/error). Verification cells are board ×
   viewport × state. Measure in on-screen pixels; geometry APIs lie under CSS zoom.
3. **Implementation details are written during design.** Tokens, component specs, geometry constants, state
   machines and routing rules go in portable form (JSON + markdown, no canvas-specific vocabulary) next to the
   boards, so the design can be landed in Vera or anywhere else.
4. **Keep the design files.** Commit the extracted boards, `canvas.json`, annotations, standalone renders and the
   board manifest under `docs/design-adoption/<canvas>/design/`, with the artifact URL and an extraction hash. Never
   keep them only in a scratchpad.
5. **Survey the backend per part.** For each read a design part shows: is it a catalogued capability, what does it
   really return, how slow is it (flag > 5 s), is it read-through-allowed in a sandbox? Parts with no backing read
   are separate "new backend" packages.
6. **Compare against the rendered board, never an old screenshot.** Run one pairdiff per viewport; do pre-land
   route substitution (serve the worktree's files into prod's page); write guard tests per landed region.
7. **One state record.** Per row, keep my-check evidence and the owner's verdict, with statuses on-edge / on-main /
   owner-verified. Push it to the registry on every adopt and promote.

## Programmes with censuses (loop work)

Follow `improve-vera-sandboxed` §11:

- The noise floor is ±1 capped goal / ±13% wall.
- Prove the code path ran.
- Analyse from artifacts.
- Compare same build, same day, against a declared control.
- Two censuses to confirm.

The census calendar is owned by compute-roles; ask before adding arms.
