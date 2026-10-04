---
name: skill-smith
description: Create, autopsy and consolidate the skills (and their helper scripts) that AI sessions use on Vera, so each skill improves from real use instead of decaying. Scaffold a new skill in the standard shape (SKILL.md + LEARNINGS.md + CHANGELOG.md + references/scripts), register it in Vera's registry and expose it to Codex via AGENTS.md; run an autopsy when a skill-guided task fails, is slow, or ends; periodically fold repeated learnings back into the skill's rules. Use when asked to write or change a skill, when a skill's instructions turned out wrong or missing during a task, at the end of a task that used a skill, or when a skill's LEARNINGS has piled up.
---

# Skill smith — skills that learn from their own use

Skills rot in two ways: their rules go stale as the code moves, and the lessons from failures stay in one chat and
never reach the next agent. This skill stops both. **Each use leaves a trace (an autopsy), and the traces are
periodically folded back into the rules (consolidation).** It works the same for Claude skills and for Codex,
which reads them through `AGENTS.md`.

## 1. The shape of a skill

```
.claude/skills/<name>/
  SKILL.md        the procedure; front-matter (name, description); ≤ ~300 lines; rules, not stories
  LEARNINGS.md    append-only, dated autopsy entries (§3); the raw material for consolidation
  CHANGELOG.md    one line per version: date · version · what changed · pipeline/sha
  references/     long material SKILL.md links to (standards, tables, worked examples)
  scripts/        helpers; each file starts with a one-line purpose comment
```

- **The description** says *what* and *when*: the triggers an agent will actually have in front of it. It's the
  only part loaded before the skill is chosen.
- **Rules, not stories, in SKILL.md.** The story goes in LEARNINGS (dated, with evidence); the rule it produced goes
  in SKILL.md, citing the LEARNINGS entry if the reason isn't obvious.
- **Public repo:** no secrets, tokens, private hostnames beyond the documented share layout, or personal content.
  Private skills live in the user's skill dir and are registered with `source.origin=user`.
- **Skills that plan** must emit work packages others can claim (`vera-coordinate` §6).

## 2. Creating a skill

1. Check it doesn't already exist: `registry.list(kind="skill", q=…)`, `ls .claude/skills/`, and the skill list in
   `AGENTS.md`. Prefer extending a skill over adding a near-duplicate.
2. Scaffold from `templates/`: `SKILL.template.md`, `LEARNINGS.template.md`, `CHANGELOG.template.md`.
3. Write SKILL.md:
   - the procedure in numbered sections;
   - the decision points;
   - the verification steps;
   - the hazards that already cost someone time;
   - a closing **autopsy step** (copy §3's short form).
4. **Verify every capability name and parameter the skill cites** against its `@capability` definition (grep the
   `async def cap_…` signature). Agents copy these literally, and a wrong parameter name fails silently or returns
   "no such item".
5. Land it through the pipeline like code (`improve-vera-sandboxed`): branch, worktree, host commit, review,
   promote to `bleeding-edge`.
6. **Register it** with `registry.upsert`:

   ```
   {entry: {kind: "skill", name, summary: <description's first sentence>, body: <SKILL.md>,
            version: <sha of SKILL.md on bleeding-edge>,
            source: {origin: "claude-code", path: ".claude/skills/<name>/SKILL.md", repo: "vera"},
            tags, applies_to: ["claude", "codex"],
            helpers: [{name, purpose, path} for each script]}}
   ```

   Then add it to the skills table in `AGENTS.md`.
7. Add a `CHANGELOG.md` line: `YYYY-MM-DD · v1 · created · pipeline <id>`.

## 3. Autopsy: during and after use

Run an autopsy:

- **at once** when a step of a skill was wrong, missing, or misled you, and it cost time or caused a defect;
- **at the end** of every task that used the skill, if anything was learned.

Append to that skill's `LEARNINGS.md`:

```
## YYYY-MM-DD — <one-line title>
- task: <what you were doing> (unit/item/pipeline)
- what happened: <the failure or slowness, concretely>
- cause: <root cause — the skill's rule, a missing rule, a tool, the code>
- rule now: <the rule that would have prevented it, phrased as an instruction>
- evidence: <path:line, sha, log/dump path, board item>
- status: open | folded into SKILL.md vN | rejected (why)
```

- **Several skills implicated:** add an entry to each, cross-linked.
- **The fix needs code or a tool:** also file a board item `kind:skill-issue`, label `skill:<name>`
  (`kind:pipeline-issue` for Loop Lab pipeline trouble), and link it from the entry.
- **No lesson, no entry.** "It went fine" isn't an autopsy.

## 4. Consolidation: folding learnings back

Run it when a skill's LEARNINGS has ~10 or more `open` entries, weekly for the most-used skills, or when an entry's
rule contradicts SKILL.md.

1. Read the open entries and group them by cause.
2. For each group:
   - add or change one rule in SKILL.md;
   - delete rules the code has made obsolete (check against the current code, not memory);
   - move long explanations to `references/`.
3. Mark each entry `folded into SKILL.md vN`, or `rejected (why)`.
4. Keep SKILL.md around 300 lines or fewer; if it's growing past that, split it into a suite and keep the old name as
   a short router.
5. Land it through the pipeline; the review reads the diff of SKILL.md plus the folded entries. Bump the CHANGELOG
   and update the registry entry's `version`.

## 5. Keeping the registry and AGENTS.md true

- Every skill and helper under `.claude/skills/` has a registry entry. An entry whose files are gone is marked
  retired, not deleted.
- `AGENTS.md`'s skill table lists every repo skill with its one-line description; update it in the same change as
  the skill.
- Agent-ops AO-E.2/E.3 will generate both from the files. Until then, keep them in step by hand.

## 6. Evidence of use

- Census rows record the driving skill and session (`improve-vera-sandboxed` §11.7). Pipelines record controller and
  session.
- When consolidating, check whether the skill is actually used and where it fails. A skill that is never used, or
  keeps producing skill-issue items, is a candidate for merging, rewriting or retiring.

## 7. Close with your own autopsy

Did this skill's process fail you? Append to `skill-smith/LEARNINGS.md`.
