# Postmortem — Agentic-loop planner drift (crypto plans → generic plans)

**Date of incident:** surfaced Wed 2026-08-06 evening · diagnosed/fixed 2026-08-06 → 2026-08-07
**Severity:** high — the agentic loop planned the *wrong task* for almost every goal
**Status:** fixed, live-verified
**Components:** `vera/dag/dag_workshop_capabilities.py` (v5/v6/v7 orchestrator + master planner)

---

## 1. Symptom

Two failures that felt separate but share one root:

1. **Crypto/quant hijack** (fixed in an earlier session). Tier + triage read the goal
   correctly, then the **orchestrator** produced a plan about *cryptocurrency /
   Bitcoin / DeFi / trading strategies* for unrelated goals — e.g. "get the latest
   AI/ML developments and write a report" or "create a gen1 pokedex in html".
2. **Generic / un-tailored plans** (this session). After the crypto fix, the
   orchestrator produced boilerplate agent-scaffold plans ("Initialize Session &
   Context", "Discover Available Capabilities", "Execute Specific Task (Dynamic)")
   or plans whose step titles were bare capability names (`memory.seek`,
   `memory.read`, `caps.describe`, `llm.generate`) — not tailored to the goal.
   The **drift detector fired but could not correct it** (the retry re-produced the
   identical bad plan).

---

## 2. Root cause

Both are manifestations of **the planner being pinned to greedy, deterministic
decoding.**

Every loop LLM call funnels through `_safe_ollama_generate_dw`, which applies a
determinism floor when `VERA_LOOP_DETERMINISTIC` is on (the default):

```
temperature = VERA_LOOP_TEMP (default 0)   # greedy
top_p       = 1.0
seed        = VERA_LOOP_SEED (default 7)   # fixed
```

That is correct for **verdicts / tool-selection / journal records** (one
reproducible answer) but wrong for **plan generation**, which is creative
decomposition:

- **Greedy (temp 0)** makes the model latch onto the single most "salient" thing
  in the prompt and follow it rigidly. The orchestrator prompt includes an
  `AVAILABLE SKILLS` block built from **every enabled skill, unfiltered**
  (`_v5_list_skills`). One of the user's skills — **"Maxhodl"** (`ecf9c33c`), a
  domain skill described as *"knowledgeable about cryptocurrency … Bitcoin …
  DeFi … airdrops … markets"* — was in that list. Greedy decoding locked onto it
  and the plan became crypto, echoing the skill's description almost verbatim.
- Once the crypto skill was **filtered out** (fix #1), greedy decoding had nothing
  domain-specific to lock onto, so it fell back to the model's default
  **agent-scaffold boilerplate** → the "generic / un-tailored" plans.
- A **fixed seed** means a bad plan is produced **identically every time**. The
  drift-guard's retry called the planner with the same inputs → same seed → the
  **same drifted plan**. Detection worked; correction was impossible.

The planner **model was never the problem** — `jaahas/qwen3.5-uncensored` plans
correctly the moment it is allowed to sample (confirmed by direct tests: neutral
prompts returned correct, on-topic planner personas).

---

## 3. Why it only surfaced Wednesday (and why that was hard to answer)

This is the important part. **None of the ingredients were new:**

| Ingredient | First appeared | Source |
|---|---|---|
| temp-0 determinism floor | **2026-07-30** (commit `04c7266`) | committed code |
| unfiltered skill injection into planner | old (in every prior commit) | committed code |
| "Maxhodl" crypto skill in the pool | **2026-05-01** | runtime data (skills store) |

All three predate the Wednesday incident by weeks-to-months. So a *third* change on
Wednesday 2026-08-06 tipped the latent combination into a visible failure — most
likely a change to skill selection/ordering, the catalog, or the orchestrator
prompt that raised the crypto skill's prominence.

**That change cannot be pinned from git.** There are **zero commits dated
2026-08-06** — all of Wednesday's work sat *uncommitted* in the working tree and
was later bundled into a single large commit (`33b3ce9`, 2026-08-07,
*"Accumulate loop-lab initiative work…"*, ~800 changed lines across many
subsystems). With no granular commits, no per-change timestamp, and no record of
which branch/session produced which edit, the activating change is unrecoverable
by inspection.

**This is the core lesson: the diagnosis cost two multi-hour sessions not because
the bug was subtle, but because there was no way to see *what changed, when, on
which branch, and by whom*.** A change timeline (commit- and config-level) tied to
the running version would have made this a 5-minute diagnosis.

---

## 4. The fixes

All in `vera/dag/dag_workshop_capabilities.py`.

**Fix #1 — filter skills to the goal (earlier session).**
`_v5_filter_skills_for_goal(skills, goal, catalog)` — the planner now only sees
skills relevant to the goal: structural teaching skills (`sys-*`, `fmt-*`) are
always kept; a user *domain* skill is kept only if it shares a content word with
the goal or teaches a capability in the run's catalog. Applied at both the v5 and
v6 skill-selection sites. An unrelated "cryptocurrency" skill can no longer be
placed in front of the planner.

**Fix #2 — planner is no longer deterministic (this session).**
- `_planner_sampling(base)` + env `VERA_PLANNER_NONDET` (default on) /
  `VERA_PLANNER_TEMP` (default **0.4**): planner calls get real temperature and a
  **fresh seed per call**. Verdicts/tool-selection/journals stay deterministic —
  only *planning* samples.
- Applied in `_v5_orchestrate_plan` (`plan_opts`) and `_v5_master_plan` (persona +
  long-form calls).
- The **drift-guard retry** now uses the **minimal** planner prompt (simpler, no
  skills, explicitly forbids capability-name titles) *and* a fresh seed, so the
  re-plan genuinely differs and can recover.
- Temp tuning: 0.55 was too loose (the planner grabbed a capability's description
  boilerplate — "bug fix / renamed symbol / review feedback" from `code.edit`);
  **0.4** is tailored and clean.

**Related (this session):** `_V6_PLANNER_RECALL` flag was added around the
"recalled past conversations" injection while testing whether *that* was the
cause. It was not (drift persisted with a proven-pristine goal), so it was left
**enabled**.

---

## 5. Verification

- Skills nulled as a controlled test → 2/2 previously-drifting goals planned
  on-topic (confirmed skills were the crypto source).
- With the filter + recall on → 4/4 non-crypto on the drifting goals; a genuine
  crypto goal still keeps its crypto skill.
- With non-deterministic planning (temp 0.4) → tailored, clean plans that **vary
  run-to-run** (proving retries can now differ); simple goals correctly take the
  single-cap fast path.

---

## 6. Lessons / follow-ups

1. **Don't force determinism on generative planning.** Reproducibility helps
   verdicts, not decomposition. (Done.)
2. **Don't hand the planner unfiltered domain context.** Anything in the prompt is
   treated as relevant. (Done — skill filter.)
3. **Instrument the exact prompt early.** The decisive step was emitting the real
   assembled planner prompt (goal → full prompt → skill list); it showed the goal
   was pristine and the crypto came from the injected skills. Hours were lost
   theorising (recall, then the model) before instrumenting.
4. **Don't blame the model.** It was wrongly called "weak"; it plans fine with
   normal sampling.
5. **Change traceability is the real gap.** Commit granularly; tie the running
   **branch/version** into logs; record which change (and which Claude Code
   session) touched what. See the observability/postmortem proposal that this
   incident motivated.
