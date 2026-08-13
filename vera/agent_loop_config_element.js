/* ============================================================================
 * <vera-agent-loop-config> — unified agentic-loop configuration control
 * ----------------------------------------------------------------------------
 * One reusable web component that exposes the FULL config surface of the
 * /workshop/agent_loop/stream endpoint (every body param the runner accepts),
 * so any panel (dream, netmap, chat, dag workshop, …) gets identical, complete
 * controls instead of each hand-rolling a partial subset.
 *
 * Served at /ui/elements/agent_loop_config.js (ui_capabilities.py). A host does:
 *     <script src="/ui/elements/agent_loop_config.js"></script>
 *     <vera-agent-loop-config id="cfg"></vera-agent-loop-config>
 *
 * Public API
 *   el.getConfig()            → { ...body-ready config } (all known keys)
 *   el.getConfig(true)        → only the keys relevant to the selected engine
 *   el.value                  → alias for getConfig()
 *   el.setConfig(obj)         → apply values from an object (unknown keys ignored;
 *                               `loop_version` is accepted as an alias for `version`)
 *   el.reset()                → restore every field to its default
 *   el.defaults()             → { ...default config }
 *   el.getVersion()           → current engine version ("v5" | … | "v1")
 *   el.setVersion(v)          → set engine + re-evaluate field visibility
 *
 * Attributes
 *   version="v5"      initial engine (default v5)
 *   versions="v5,v2"  restrict the engine selector to these versions
 *   hide-engine       hide the engine selector (host pins the version)
 *   exclude="model,instance_id"  hide these field keys (host controls them elsewhere)
 *   compact           denser layout
 *   collapsed         start with advanced groups collapsed
 *
 * Events
 *   alc:change  {detail:{key, value, config}}  — any field changed
 * ========================================================================== */
(function () {
  if (customElements.get('vera-agent-loop-config')) return;

  const VERSIONS = [
    ['v7', 'v7 — tiered + branching'],
    ['v6', 'v6 — adaptive specialists'],
    ['v5', 'v5 — orchestrated specialists'],
    ['v4', 'v4 — staged plan/explore/think/act/verify'],
    ['v3', 'v3 — ReAct + HITL approval'],
    ['v2', 'v2 — ReAct + dynamic toolkit'],
    ['v1', 'v1 — legacy observe/think/act'],
  ];

  // Field schema — drives rendering, defaults, coercion AND version visibility.
  // type: 'select-version' | 'bool' | 'int' | 'text' | 'textarea'
  // vers: 'all' or array of engine versions the field applies to.
  const G = {
    engine: 'Engine', planning: 'Planning (v5/v6)',
    adaptive: 'Adaptive control (v6)', branching: 'Branching (v7)', recon: 'Recon (v5/v6)',
    skills: 'Skills (v5/v6)', code: 'Code artifacts (v5/v6)',
    react: 'Triage & ReAct (v2–v4)', phases: 'Phases & continuation (v2–v4)',
    pipeline: 'Step pipeline (v4)', hitl: 'Human-in-the-loop (v3/v4)',
    longrun: 'Long-running', handover: 'Handover synthesis',
    context: 'Context & prompt',
  };

  const FIELDS = [
    // ── Engine ──────────────────────────────────────────────────────────────
    { k: 'version', g: 'engine', t: 'select-version', def: 'v5', label: 'Engine' },
    { k: 'model', g: 'engine', t: 'text', def: '', vers: 'all', label: 'Model', ph: '(cluster default)' },
    { k: 'instance_id', g: 'engine', t: 'text', def: '', vers: 'all', label: 'Instance id', ph: '(auto)' },
    { k: 'prefer_gpu', g: 'engine', t: 'bool', def: true, vers: 'all', label: 'Prefer GPU' },
    { k: 'max_cycles', g: 'engine', t: 'int', def: 8, min: 1, max: 40, vers: ['v1', 'v2', 'v3', 'v4'], label: 'Max cycles' },

    // ── Planning (v5/v6) ────────────────────────────────────────────────────
    { k: 'max_steps', g: 'planning', t: 'int', def: 6, min: 1, max: 20, vers: ['v5', 'v6'], label: 'Max steps' },
    { k: 'step_cycle_budget', g: 'planning', t: 'int', def: 6, min: 1, max: 20, vers: ['v5', 'v6'], label: 'Step cycle budget' },
    { k: 'catalog_size', g: 'planning', t: 'int', def: 40, min: 8, max: 80, vers: ['v5', 'v6'], label: 'Catalog size' },
    { k: 'enable_replan', g: 'planning', t: 'bool', def: true, vers: ['v5'], label: 'Failure replan (v5)' },
    { k: 'enable_subplans', g: 'planning', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Sub-plans (complex steps)' },
    { k: 'enable_phases', g: 'planning', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Per-step phases', hint: 'Let a step run as an explore→think→act→verify cadence, each phase its own scoped sub-agent (explore = read-only recon, think = tool-free reasoning, act = do the work, verify = read-only self-check). On a COMPLEX / long-term (v7 strategic) plan the planner is actively steered to PHASE any step that creates or changes something so each such step self-verifies and self-corrects; on ordinary runs phases stay sparing so simple goals stay fast. Off = every step runs as one specialist.' },
    { k: 'phase_policy', g: 'planning', t: 'enum', def: 'auto', vers: ['v5', 'v6'], label: 'Phase usage',
      opts: [['auto', 'Auto — heavy on complex/long-term'], ['encourage', 'Encourage — phase most substantial steps'], ['sparing', 'Sparing — only when clearly needed']],
      hint: 'How strongly the planner phases steps (needs Per-step phases ON). AUTO = phase heavily on a complex/strategic (v7) plan, sparingly otherwise. ENCOURAGE = always push create/change steps into explore→act→verify regardless of tier (use this to get phases on a v6 run). SPARING = only when the phases are genuinely different kinds of work.' },
    { k: 'phase_set', g: 'planning', t: 'text', def: 'explore,think,act,verify', vers: ['v5', 'v6'], label: 'Allowed phases (csv)', ph: 'explore,think,act,verify', hint: 'Which of the explore / think / act / verify phases the planner may assign to a step (subset, in this canonical order). Drop one to forbid it — e.g. "explore,act,verify" to skip the tool-free think phase, or "act,verify" to only ever add a self-check. plan is the orchestration itself and is always on.' },
    { k: 'enable_master_planner', g: 'planning', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Master planner (extreme goals)' },
    { k: 'enable_tiering', g: 'planning', t: 'bool', def: true, vers: ['v7'], label: 'Tier classifier', hint: 'Before planning, classify the goal (single / simple / complex / strategic) with a cheap heuristic AND a less-prescriptive LLM pass. single = one-cap fast path. simple = a few steps. complex = a thorough MULTI-STEP normal plan (NO master planner) — the middle layer for concrete multi-domain jobs. strategic = open-ended / multi-day → the cap-agnostic master planner drafts a long-form strategy, split into pieces run one-per-session. "single" also skips recon.' },
    { k: 'plan_tier', g: 'planning', t: 'text', def: 'auto', vers: ['v7'], label: 'Force tier', ph: 'auto', hint: 'auto = classify. Or pin a tier: single | simple | complex | strategic.' },
    { k: 'auto_escalate', g: 'planning', t: 'bool', def: true, vers: ['v7'], label: 'Auto-escalate (silent)', hint: 'Permit the loop to SILENTLY escalate to the heaviest (strategic / multi-day) tier. Off = a strategic goal is capped at "complex". Consultation is governed independently by Consult level/mode — this toggle no longer suppresses clarifying questions.' },
    { k: 'enable_fast_path', g: 'planning', t: 'bool', def: true, vers: ['v7'], label: 'Single-cap fast path', hint: 'When the tier classifier says a goal is achievable with ONE capability, pick and run that cap in a single cheap call (like the chat command bar) and return — bypassing the orchestrator + specialist machinery entirely. Falls through to full planning on a miss.' },
    { k: 'clarify_level', g: 'planning', t: 'int', def: 1, min: 0, max: 3, vers: ['v7'], label: 'Consult level (0–3)', hint: 'Sliding scale of clarifying questions asked BEFORE committing to a documented plan — 0 off · 1 strategic only · 2 complex+strategic · 3 always. Independent of Auto-escalate; folds your answers into the plan.' },
    { k: 'clarify_mode', g: 'planning', t: 'enum', def: '', vers: ['v7'], label: 'Consult mode',
      opts: [['', 'Auto (derive from level)'], ['off', 'Off — never ask'], ['ask', 'Ask & wait for a reply'], ['auto_raise', 'Auto-raise tier instead of asking'], ['auto_accept', 'Auto-accept best assumptions']],
      hint: 'How the pre-plan consultation behaves. ASK sends the questions to the loop UI (and the Consult channel) and BLOCKS until you answer or the timeout elapses. AUTO-RAISE plans one tier harder instead of interrupting. AUTO-ACCEPT drafts the questions AND their most reasonable assumed answers and proceeds. Auto = derive from Consult level.' },
    { k: 'clarify_channel', g: 'planning', t: 'enum', def: 'ui', vers: ['v7'], label: 'Consult channel',
      opts: [['ui', 'Loop UI'], ['telegram', 'Telegram (comms)']], hint: 'Where clarifying questions (whole-goal AND a step\'s ask_user) are sent. UI shows an answer box in the loop; Telegram messages the admin chat and accepts the reply back.' },
    { k: 'clarify_timeout_secs', g: 'planning', t: 'int', def: 180, min: 15, max: 3600, vers: ['v7'], label: 'Consult timeout (s)', hint: 'How long a whole-goal consultation waits for answers before proceeding without them.' },
    { k: 'max_caps_per_piece', g: 'planning', t: 'int', def: 6, min: 1, max: 12, vers: ['v7'], label: 'Caps per piece (max)', hint: 'PIECEWISE planning suggests the caps (the strategist stays cap-agnostic): how many capabilities each stage/piece may suggest. These seed the piece\'s steps.' },
    { k: 'max_plan_pieces', g: 'planning', t: 'int', def: 6, min: 2, max: 30, vers: ['v7'], label: 'Plan pieces (max)', hint: 'How many ORDERED phases/pieces the strategic master plan splits into (2–30). Raise it for broad multi-phase goals whose later phases only become clear once earlier ones finish; a strategic goal still runs ONE piece per session and defers the rest to the dream system.' },
    { k: 'cap_override_mode', g: 'planning', t: 'enum', def: 'piece', vers: ['v7'], label: 'Step caps from piece',
      opts: [['piece', 'Piece — steps use their piece\'s caps'], ['off', 'Off — orchestrator picks per step']], hint: 'PIECE scopes each stage\'s steps directly to that piece\'s suggested caps (an easy way to give steps all the relevant caps), instead of the orchestrator re-selecting a few per step. The full run catalog stays reachable mid-step via need_caps if a step is under-tooled.' },
    { k: 'enable_dream_persistence', g: 'planning', t: 'bool', def: true, vers: ['v7'], label: 'Persist strategic → dream', hint: 'A "strategic" (multi-day) goal persists its documented master plan as a DREAM PROJECT and folds each session\'s progress back into it, so the dream system continues the work a portion at a time over days. This session still executes the first portion inline.' },
    { k: 'enable_journal', g: 'planning', t: 'bool', def: true, vers: ['v6'], label: 'Structured run journal', hint: 'Distil each finished step into a STRUCTURED record (goal-relevant outputs, file/artifact paths, tools used, key entities), persist it to the DATA FABRIC (dataset agent_loop.journal, searchable) plus a journal.json mirror in the artifact dir, and fold a compact digest back into later steps so the run reuses its own outputs and paths instead of re-deriving them. By default only complex/long-term runs journal (see "Journal on every run").' },
    { k: 'journal_all_tiers', g: 'planning', t: 'bool', def: false, vers: ['v6'], label: 'Journal on every run', hint: 'Keep the structured journal even for simple/quick goals (default OFF — journaling is normally reserved for complex/long-term runs to avoid the per-step extraction cost). Turn ON to force the journal for any run, e.g. on a v6 engine that has no tier classifier.' },

    // ── Adaptive control (v6) ─────────────────────────────────────────────────
    { k: 'enable_adaptive', g: 'adaptive', t: 'bool', def: true, vers: ['v6'], label: 'Adaptive controller', hint: 'After every step, a controller reads the ledger and decides: continue, insert a remediation/gap step, replan the rest, or stop early once the goal is met.' },
    { k: 'enable_step_verify', g: 'adaptive', t: 'bool', def: true, vers: ['v6'], label: 'Per-step success check', hint: 'One cheap judge call per step verifies its success criterion was OBJECTIVELY met — an "ok" step that missed its bar is flagged unmet in the ledger so the controller remediates it.' },
    { k: 'enable_step_finalize', g: 'adaptive', t: 'bool', def: true, vers: ['v7'], label: 'Per-step finalise (distil)', hint: 'After each step, one cheap pass distils its cycles into a finalised, relevant-only output — keeping only what achieves the step goal or feeds a later step. Downstream steps and the verifier read this clean version; the raw transcript is kept for the UI. Turn off for the fastest path.' },
    { k: 'enable_chaining', g: 'adaptive', t: 'bool', def: false, vers: ['v6'], label: 'Stage-agent cap chaining', hint: 'Let a step\'s specialist chain several caps in ONE turn, piping each output into the next (e.g. llm.generate → ide.fs.write) with NO inference between hops — cheaper and more reliable than doing it call-by-call. Off by default (2026-08-13): live-observed to interfere more than help — a chained podcast.script → code.author hop spiralled into ~900 broken-syntax retries over roughly an hour instead of completing. Turn on to experiment; it stays fully implemented.' },
    { k: 'enable_step_questions', g: 'adaptive', t: 'bool', def: true, vers: ['v7'], label: 'Steps may ask the user', hint: 'A running step that is genuinely blocked on a decision only you can make may ask a question via the loop UI and the Consult channel (ask_user). It blocks briefly for a reply, then proceeds on its best assumption.' },
    { k: 'question_timeout_secs', g: 'adaptive', t: 'int', def: 180, min: 15, max: 3600, vers: ['v7'], label: 'Step question timeout (s)', hint: 'How long a step\'s ask_user waits for your reply before proceeding on an assumption.' },
    { k: 'enable_final_gate', g: 'adaptive', t: 'bool', def: true, vers: ['v6'], label: 'Final completion gate', hint: 'Verify the whole-goal done_when before finishing; append follow-up steps if the goal is not truly met.' },

    // ── Branching (v7) — the "git-tree" ───────────────────────────────────────
    { k: 'failure_strategy', g: 'branching', t: 'enum', def: 'extra_step', vers: ['v7'], label: 'On step failure',
      opts: [['extra_step', 'Extra step — continue from last output'], ['branch', 'Branch — fork alternate approaches'], ['default', 'Default — controller only (no recovery)']],
      hint: 'What happens when a step misses its success bar. EXTRA STEP ADJUSTS the step to navigate the specific problem the verifier found — a reframed tactic with new/added caps and (for a create/change step) an explore→act→verify cadence — seeded with the failed step\'s own output so it builds forward instead of restarting (falls back to a plain continuation step). BRANCH forks the last good point into alternate approaches and merges the first that meets the bar (powerful but can wander). DEFAULT leaves recovery to the adaptive controller. Only engages ON failure — clean runs are cost-neutral.' },
    { k: 'branch_fanout', g: 'branching', t: 'int', def: 2, min: 1, max: 4, vers: ['v7'], label: 'Fanout (alternates/fork)', hint: 'BRANCH mode only: how many DISTINCT alternate approaches a branch strategist proposes at each fork point.' },
    { k: 'max_branches', g: 'branching', t: 'int', def: 6, min: 0, max: 20, vers: ['v7'], label: 'Recovery budget (per run)', hint: 'Hard ceiling on total recovery inserts (extra steps) or alternate branches explored across the whole run — bounds the extra cost of recovery. 0 disables recovery.' },
    { k: 'branch_parallel', g: 'branching', t: 'bool', def: false, vers: ['v7'], label: 'Run branches in parallel', hint: 'BRANCH mode only: explore a fork\'s alternate approaches concurrently as parallel ephemeral agents (first to meet the bar wins) instead of one at a time. Faster recovery, higher peak load.' },
    { k: 'enable_delivery', g: 'adaptive', t: 'bool', def: true, vers: ['v6'], label: 'Delivery agent', hint: 'A dedicated final agent turns the run evidence into a markdown deliverable: the answer, a faithful account of the actions taken (including failures), artifacts produced, and a usage guide when code was built.' },
    { k: 'max_total_steps', g: 'adaptive', t: 'int', def: 0, min: 0, max: 40, vers: ['v6'], label: 'Max total steps (0=auto)', hint: 'Hard ceiling on inserted/replanned/follow-up steps. 0 = auto (2× max steps, capped at 40).' },

    // ── Recon (v5/v6) ─────────────────────────────────────────────────────────
    { k: 'enable_recon', g: 'recon', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Pre-plan recon' },
    { k: 'recon_max_rounds', g: 'recon', t: 'int', def: 3, min: 0, max: 8, vers: ['v5', 'v6'], label: 'Recon rounds (0=off, iterative)', hint: 'Iterative read-only explore loop: the planner keeps probing (files, web, ls/grep/cat/find) until it knows enough or rounds run out.' },

    // ── Skills (v5/v6) ──────────────────────────────────────────────────────
    { k: 'enable_dynamic_skills', g: 'skills', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Dynamic skill injection' },
    { k: 'auto_suggest_skills', g: 'skills', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Auto-suggest skills' },
    { k: 'skill_allow', g: 'skills', t: 'text', def: '', vers: ['v5', 'v6'], label: 'Skill allow (csv)', ph: '(any)' },
    { k: 'skill_deny', g: 'skills', t: 'text', def: '', vers: ['v5', 'v6'], label: 'Skill deny (csv)', ph: '(none)' },

    // ── Code artifacts (v5/v6) ──────────────────────────────────────────────
    { k: 'enable_code_autosave', g: 'code', t: 'bool', def: true, vers: ['v5', 'v6'], label: 'Auto-save fenced code' },
    { k: 'code_push_gitea', g: 'code', t: 'bool', def: false, vers: ['v5', 'v6'], label: 'Push code to Gitea' },

    // ── Triage & ReAct (v2–v4) ──────────────────────────────────────────────
    { k: 'satisfaction_check', g: 'react', t: 'bool', def: true, vers: ['v2', 'v3', 'v4'], label: 'Satisfaction check' },
    { k: 'enable_expand', g: 'react', t: 'bool', def: true, vers: ['v2', 'v3', 'v4'], label: 'Mid-run toolkit expand' },
    { k: 'triage_top_k', g: 'react', t: 'int', def: 16, min: 1, max: 60, vers: ['v2', 'v3', 'v4'], label: 'Triage top-k' },
    { k: 'triage_category', g: 'react', t: 'text', def: '', vers: ['v2', 'v3', 'v4'], label: 'Triage category (force)', ph: '(auto)' },
    { k: 'triage_keywords', g: 'react', t: 'text', def: '', vers: ['v2', 'v3', 'v4'], label: 'Triage keywords (csv)', ph: '(auto)' },
    { k: 'max_search_calls', g: 'react', t: 'int', def: 2, min: 0, max: 10, vers: ['v2', 'v3', 'v4'], label: 'Max search calls' },
    { k: 'max_expands', g: 'react', t: 'int', def: 1, min: 0, max: 6, vers: ['v2', 'v3', 'v4'], label: 'Max toolkit expands' },
    { k: 'count_failed_cycles', g: 'react', t: 'bool', def: false, vers: ['v2', 'v3', 'v4'], label: 'Count failed cycles' },
    { k: 'max_recovery_attempts', g: 'react', t: 'int', def: 2, min: 0, max: 6, vers: ['v1', 'v2', 'v3', 'v4'], label: 'Max recovery attempts' },

    // ── Phases & continuation (v2–v4) ───────────────────────────────────────
    { k: 'phased', g: 'phases', t: 'bool', def: true, vers: ['v2', 'v3', 'v4'], label: 'Phased cadence' },
    { k: 'min_explore_cycles', g: 'phases', t: 'int', def: 2, min: 0, max: 10, vers: ['v2', 'v3', 'v4'], label: 'Min explore cycles' },
    { k: 'require_validate', g: 'phases', t: 'bool', def: true, vers: ['v2', 'v3', 'v4'], label: 'Require validate' },
    { k: 'long_running_force_hitl', g: 'phases', t: 'bool', def: true, vers: ['v2', 'v3', 'v4'], label: 'Long-running forces HITL' },
    { k: 'allow_continue', g: 'phases', t: 'bool', def: true, vers: ['v2', 'v3', 'v4'], label: 'Allow continue' },
    { k: 'continue_increment', g: 'phases', t: 'int', def: 8, min: 1, max: 40, vers: ['v2', 'v3', 'v4'], label: 'Continue increment' },
    { k: 'auto_continue_max', g: 'phases', t: 'int', def: 0, min: 0, max: 20, vers: ['v2', 'v3', 'v4'], label: 'Auto-continue max (0=off)' },

    // ── Step pipeline (v4) ──────────────────────────────────────────────────
    { k: 'enabled_steps', g: 'pipeline', t: 'text', def: 'plan,explore,think,act,verify', vers: ['v4'], label: 'Enabled steps (csv)' },
    { k: 'select_steps', g: 'pipeline', t: 'bool', def: true, vers: ['v4'], label: 'Auto-select steps' },
    { k: 'require_verify', g: 'pipeline', t: 'bool', def: true, vers: ['v4'], label: 'Require verify' },
    { k: 'strict_complete', g: 'pipeline', t: 'bool', def: true, vers: ['v4'], label: 'Strict completion' },
    { k: 'long_running_caps', g: 'pipeline', t: 'text', def: '', vers: ['v4'], label: 'Long-running caps (csv)', ph: '(auto)' },

    // ── HITL (v3/v4) ────────────────────────────────────────────────────────
    { k: 'require_approval', g: 'hitl', t: 'bool', def: false, vers: ['v3', 'v4'], label: 'Require approval (HITL)' },
    { k: 'hitl_timeout_secs', g: 'hitl', t: 'int', def: 300, min: 10, max: 3600, vers: ['v3', 'v4'], label: 'HITL timeout (s)' },

    // ── Long-running ────────────────────────────────────────────────────────
    { k: 'await_long_running', g: 'longrun', t: 'bool', def: true, vers: 'all', label: 'Await long-running jobs' },
    { k: 'long_running_timeout_secs', g: 'longrun', t: 'int', def: 1800, min: 30, max: 21600, vers: 'all', label: 'Long-running timeout (s)' },

    // ── Handover synthesis ──────────────────────────────────────────────────
    { k: 'handover', g: 'handover', t: 'bool', def: false, vers: 'all', label: 'Synthesise final answer' },
    { k: 'handover_max_chars', g: 'handover', t: 'int', def: 20000, min: 500, max: 80000, vers: 'all', label: 'Handover max chars' },

    // ── Context & prompt ────────────────────────────────────────────────────
    { k: 'prefer_terminal_tools', g: 'context', t: 'bool', def: true, vers: ['v4', 'v5', 'v6'], label: 'Prefer terminal tools (grep/sed/awk)', hint: 'Always seed exec.bash.run into the toolkit and steer specialists to reach for terminal tools — grep / sed / awk / head / find — to inspect files and process text, instead of reading whole files or spinning up heavier caps. Applies to v4 and every loop above it (v5/v6/v7).' },
    { k: 'base_toolkit', g: 'context', t: 'text', def: '', vers: 'all', label: 'Base toolkit (csv)', ph: '(none)' },
    { k: 'attach_skills', g: 'context', t: 'text', def: '', vers: 'all', label: 'Attach skills (csv)', ph: '(none)' },
    { k: 'attach_ontologies', g: 'context', t: 'text', def: '', vers: 'all', label: 'Attach ontologies (csv)', ph: '(none)' },
    { k: 'system_prompt_template', g: 'context', t: 'textarea', def: '', vers: ['v1', 'v2', 'v3', 'v4'], label: 'System prompt template', ph: '(default)' },
  ];

  // Groups that start collapsed when the `collapsed` attribute is present.
  const ADVANCED_GROUPS = new Set(['react', 'phases', 'pipeline', 'hitl', 'longrun', 'handover', 'context', 'code']);

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  class VeraAgentLoopConfig extends HTMLElement {
    constructor() {
      super();
      this._sr = this.attachShadow({ mode: 'open' });
      this._fieldByKey = Object.fromEntries(FIELDS.map((f) => [f.k, f]));
      this._exclude = new Set();
      this._version = 'v5';
    }

    connectedCallback() {
      this._version = (this.getAttribute('version') || 'v5').toLowerCase();
      this._allowedVersions = (this.getAttribute('versions') || '')
        .split(',').map((s) => s.trim()).filter(Boolean);
      this._exclude = new Set((this.getAttribute('exclude') || '')
        .split(',').map((s) => s.trim()).filter(Boolean));
      this._hideEngine = this.hasAttribute('hide-engine');
      this._compact = this.hasAttribute('compact');
      this._startCollapsed = this.hasAttribute('collapsed');
      this._render();
    }

    // ── Public API ──────────────────────────────────────────────────────────
    defaults() {
      const o = {};
      FIELDS.forEach((f) => { o[f.k] = f.def; });
      return o;
    }

    getVersion() { return this._version; }

    setVersion(v) {
      v = String(v || 'v5').toLowerCase();
      const sel = this._sr.querySelector('[data-k="version"]');
      if (sel) sel.value = v;
      this._version = v;
      this._applyVisibility();
    }

    getConfig(onlyRelevant) {
      const o = {};
      FIELDS.forEach((f) => {
        if (this._exclude.has(f.k)) return;
        if (onlyRelevant && !this._appliesToVersion(f)) return;
        o[f.k] = this._readField(f);
      });
      return o;
    }

    get value() { return this.getConfig(); }

    setConfig(obj) {
      if (!obj || typeof obj !== 'object') return;
      const src = { ...obj };
      if (src.loop_version != null && src.version == null) src.version = src.loop_version;
      Object.keys(src).forEach((k) => {
        const f = this._fieldByKey[k];
        if (!f) return;
        this._writeField(f, src[k]);
        if (k === 'version') { this._version = String(src[k] || 'v5').toLowerCase(); }
      });
      this._applyVisibility();
    }

    reset() {
      FIELDS.forEach((f) => this._writeField(f, f.def));
      this._version = (this.getAttribute('version') || 'v5').toLowerCase();
      const sel = this._sr.querySelector('[data-k="version"]');
      if (sel) sel.value = this._version;
      this._applyVisibility();
      this._emit('', null);
    }

    // ── Field <-> value coercion ────────────────────────────────────────────
    _readField(f) {
      const el = this._sr.querySelector(`[data-k="${f.k}"]`);
      if (!el) return f.def;
      if (f.t === 'bool') return !!el.checked;
      if (f.t === 'int') {
        let n = parseInt(el.value, 10);
        if (isNaN(n)) n = f.def;
        if (f.min != null) n = Math.max(f.min, n);
        if (f.max != null) n = Math.min(f.max, n);
        return n;
      }
      if (f.t === 'select-version') return el.value;
      return el.value;
    }

    _writeField(f, v) {
      const el = this._sr.querySelector(`[data-k="${f.k}"]`);
      if (!el) return;
      if (f.t === 'bool') { el.checked = (v === true || v === 'true'); return; }
      el.value = (v == null ? '' : v);
    }

    _appliesToVersion(f) {
      if (f.k === 'version') return true; // engine selector always applies
      if (f.vers === 'all') return true;
      if (!Array.isArray(f.vers)) return false;
      if (f.vers.indexOf(this._version) !== -1) return true;
      // v7 is a strict SUPERSET of v6's surface — every v6 field applies to v7 too
      // (the V7-only fields below are tagged 'v7' directly). This avoids retagging
      // the whole v6 field set whenever v7 gains/keeps a shared control.
      if (this._version === 'v7' && f.vers.indexOf('v6') !== -1) return true;
      return false;
    }

    // ── Rendering ───────────────────────────────────────────────────────────
    _render() {
      const groups = [];
      const seen = new Set();
      FIELDS.forEach((f) => { if (!seen.has(f.g)) { seen.add(f.g); groups.push(f.g); } });

      const groupHtml = groups.map((g) => {
        const fields = FIELDS.filter((f) => f.g === g && !this._exclude.has(f.k)
          && !(f.k === 'version' && this._hideEngine));
        if (!fields.length) return '';
        const rows = fields.map((f) => this._fieldRow(f)).join('');
        const collapsed = this._startCollapsed && ADVANCED_GROUPS.has(g);
        return `<details class="grp" data-g="${g}" ${collapsed ? '' : 'open'}>
            <summary>${esc(G[g] || g)}</summary>
            <div class="rows">${rows}</div>
          </details>`;
      }).join('');

      this._sr.innerHTML = `<style>${this._css()}</style>
        <div class="wrap ${this._compact ? 'compact' : ''}">
          <div class="cfg-head">
            <input class="cfg-filter" type="text" placeholder="Filter settings…" data-filter aria-label="Filter settings">
          </div>
          ${groupHtml}
        </div>`;

      // Wire inputs
      this._sr.querySelectorAll('[data-k]').forEach((el) => {
        const k = el.getAttribute('data-k');
        const evt = (el.tagName === 'SELECT' || el.type === 'checkbox') ? 'change' : 'input';
        el.addEventListener(evt, () => {
          if (k === 'version') { this._version = el.value; this._applyVisibility(); }
          this._emit(k, this._readField(this._fieldByKey[k]));
        });
      });
      // Wire the settings filter (menu-style quick find).
      const _filt = this._sr.querySelector('[data-filter]');
      if (_filt) {
        _filt.value = this._filterQ || '';
        _filt.addEventListener('input', () => this._applyFilter(_filt.value));
      }
      this._applyVisibility();
    }

    _fieldRow(f) {
      const id = `f_${f.k}`;
      let input;
      if (f.t === 'select-version') {
        const opts = VERSIONS
          .filter(([v]) => !this._allowedVersions.length || this._allowedVersions.indexOf(v) !== -1)
          .map(([v, lbl]) => `<option value="${v}" ${v === this._version ? 'selected' : ''}>${esc(lbl)}</option>`)
          .join('');
        input = `<select id="${id}" data-k="${f.k}">${opts}</select>`;
      } else if (f.t === 'bool') {
        input = `<label class="tgl"><input id="${id}" data-k="${f.k}" type="checkbox"${f.def ? ' checked' : ''}>`
          + `<span class="tgl-track"><span class="tgl-thumb"></span></span></label>`;
      } else if (f.t === 'int') {
        input = `<input id="${id}" data-k="${f.k}" type="number"
            ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''}
            value="${f.def}">`;
      } else if (f.t === 'enum') {
        const opts = (f.opts || [])
          .map(([v, lbl]) => `<option value="${esc(v)}"${v === f.def ? ' selected' : ''}>${esc(lbl)}</option>`)
          .join('');
        input = `<select id="${id}" data-k="${f.k}">${opts}</select>`;
      } else if (f.t === 'textarea') {
        input = `<textarea id="${id}" data-k="${f.k}" rows="3" placeholder="${esc(f.ph || '')}"></textarea>`;
      } else {
        input = `<input id="${id}" data-k="${f.k}" type="text" placeholder="${esc(f.ph || '')}">`;
      }
      const hint = f.hint ? `<div class="hint">${esc(f.hint)}</div>` : '';
      return `<div class="row" data-row="${f.k}">
          <label class="lbl" for="${id}" title="${esc(f.hint || f.label)}">${esc(f.label)}</label>
          <div class="ctl">${input}${hint}</div>
        </div>`;
    }

    // Show/hide each field row + whole groups based on the selected engine AND
    // the active filter query (both combine here so there's one source of truth).
    _applyVisibility() {
      const groups = {};
      const q = (this._filterQ || '').toLowerCase();
      this._sr.querySelectorAll('[data-row]').forEach((row) => {
        const k = row.getAttribute('data-row');
        const f = this._fieldByKey[k];
        const okVer = this._appliesToVersion(f);
        const hay = ((f.label || '') + ' ' + (f.hint || '') + ' ' + (f.k || '')).toLowerCase();
        const okFilt = !q || hay.indexOf(q) !== -1;
        const show = okVer && okFilt;
        row.style.display = show ? '' : 'none';
        const g = f.g; groups[g] = groups[g] || false; if (show) groups[g] = true;
      });
      // version row always counts as visible for the engine group (unless filtering)
      if (!q) groups.engine = true;
      this._sr.querySelectorAll('.grp').forEach((d) => {
        const g = d.getAttribute('data-g');
        d.style.display = groups[g] ? '' : 'none';
        if (q && groups[g]) d.open = true;   // expand groups with matches while filtering
      });
    }

    _applyFilter(q) { this._filterQ = (q || '').trim(); this._applyVisibility(); }

    _emit(key, value) {
      this.dispatchEvent(new CustomEvent('alc:change', {
        detail: { key, value, config: this.getConfig() }, bubbles: true,
      }));
    }

    _css() {
      return `
        :host{display:block;font-family:var(--font,system-ui,-apple-system,Segoe UI,sans-serif);
          color:var(--text,#ddd5c8);font-size:11.5px}
        .wrap{display:flex;flex-direction:column;gap:5px}
        /* Filter head — sticky quick-find, like the chat/dag menu search. */
        .cfg-head{position:sticky;top:0;z-index:3;padding-bottom:3px;background:var(--panel1,var(--bg1,#171511))}
        .cfg-filter{width:100%;box-sizing:border-box;background:var(--inp,#15140f);color:var(--text,#ddd5c8);
          border:1px solid var(--border,#3a372f);border-radius:6px;padding:5px 9px;font-size:11px;font-family:inherit}
        .cfg-filter:focus{outline:none;border-color:var(--acc,#5a9e8f)}
        .cfg-filter::placeholder{color:var(--dim,#8c857a)}
        /* Collapsible sections — read as a menu: uppercase header, hover, open accent. */
        .grp{border:1px solid var(--border,#33312c);border-radius:7px;background:var(--panel2,#1c1a17);overflow:hidden}
        .grp>summary{cursor:pointer;padding:6px 10px;font-size:9px;letter-spacing:.9px;text-transform:uppercase;
          color:var(--dim,#8c857a);user-select:none;background:var(--panel3,#211f1b);list-style:none;
          display:flex;align-items:center;gap:6px;transition:color .13s,background .13s;border-left:2px solid transparent}
        .grp>summary:hover{color:var(--text2,#bfb6a8);background:var(--panel3h,#262320)}
        .grp[open]>summary{color:var(--acc,#5a9e8f);border-left-color:var(--acc,#5a9e8f)}
        .grp>summary::-webkit-details-marker{display:none}
        .grp>summary::before{content:"▸";display:inline-block;transition:transform .15s;opacity:.6;font-size:8px}
        .grp[open]>summary::before{transform:rotate(90deg);opacity:1}
        .rows{padding:7px 10px;display:flex;flex-direction:column;gap:6px}
        .row{display:grid;grid-template-columns:minmax(120px,44%) 1fr;gap:8px;align-items:center;
          padding:1px 0;border-radius:4px}
        .row:hover{background:var(--hov,rgba(255,255,255,.02))}
        .compact .row{grid-template-columns:minmax(100px,40%) 1fr;gap:6px}
        .lbl{color:var(--text2,#bfb6a8);font-size:10.5px;line-height:1.25}
        .ctl{display:flex;flex-direction:column;gap:2px;min-width:0}
        .ctl>input[type=text],.ctl>input[type=number],.ctl select,.ctl textarea{width:100%;box-sizing:border-box;
          background:var(--inp,#15140f);color:var(--text,#ddd5c8);border:1px solid var(--border,#3a372f);
          border-radius:4px;padding:3px 6px;font-size:11px;font-family:inherit}
        .ctl>input:focus,.ctl select:focus,.ctl textarea:focus{outline:none;border-color:var(--acc,#5a9e8f)}
        .ctl textarea{resize:vertical;font-family:var(--mono,ui-monospace,Menlo,Consolas,monospace);font-size:10px}
        .hint{font-size:9px;color:var(--dim,#8c857a);line-height:1.3}
        /* Toggle switch for booleans — the biggest "menu" visual upgrade. */
        .tgl{display:inline-flex;align-items:center;cursor:pointer;align-self:flex-start}
        .tgl input{position:absolute;opacity:0;width:0;height:0}
        .tgl-track{width:30px;height:16px;border-radius:9px;background:var(--inp,#15140f);
          border:1px solid var(--border,#3a372f);position:relative;transition:background .15s,border-color .15s}
        .tgl-thumb{position:absolute;top:1px;left:1px;width:12px;height:12px;border-radius:50%;
          background:var(--dim,#8c857a);transition:transform .15s,background .15s}
        .tgl input:checked + .tgl-track{background:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}
        .tgl input:checked + .tgl-track .tgl-thumb{transform:translateX(14px);background:var(--panel2,#1c1a17)}
        .tgl input:focus-visible + .tgl-track{box-shadow:0 0 0 2px var(--acc,#5a9e8f)}
      `;
    }
  }

  customElements.define('vera-agent-loop-config', VeraAgentLoopConfig);
})();
