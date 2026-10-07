# skill-smith — learnings

Append-only autopsy log (format: §3). Newest at the bottom.

## 2026-10-04 — shell heredocs with mixed quotes failed while writing skill files over SMB
- task: creating the LEARNINGS/CHANGELOG files for every skill
- what happened: one Bash call writing fourteen files through heredocs and `printf '…'` died with "unexpected
  EOF while looking for matching quote". No file was written.
- cause: apostrophes and backticks in the text, inside single-quoted `printf` arguments
- rule now: write skill files with the editor's Write tool (or a Python script reading a file), never with
  multi-file shell heredocs
- evidence: session vera-62
- status: open

## 2026-10-04 — the first draft of vera-coordinate cited wrong board parameters
- task: writing vera-coordinate
- what happened: the draft said `board.claim(item, frm=…)` and `board.comment(item, …)`. The real signatures are
  `board.claim(id, agent)` and `board.comment(id, frm, kind, …)`, and the move capability is `board.item.move`.
  The adversarial self-review caught it before landing.
- cause: the parameter names were written from memory of a recon summary, not from the code
- rule now: verify every capability name and parameter a skill cites against its `@capability` definition (§2
  step 4)
- evidence: `vera/board/board_capabilities.py:185`, `:259`, `:279`, `:317`, `:346`
- status: folded into SKILL.md v1
