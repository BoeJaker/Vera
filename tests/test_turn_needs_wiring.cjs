// The chat stream decides from the turn's words whether it needs the web before the model, behind a switch.
//   node tests/test_turn_needs_wiring.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const A = R('vera/agents/agents.py'), S = R('vera/minds/switchboard_core.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the switch is registered, off by default, owned by context-and-ui', S.includes('_f("context.needs_web", ("off", "rules"), "chat", "CU-T.3",') && S.includes('owner="context-and-ui"'));
t('a switchboard that cannot be read leaves the switch off', A.includes('async def _switch_mode(feature_id: str, minds: Any = None, default: str = "off") -> str:') && A.includes('return default'));
t('the rules read the bare question, never the message with its injected blocks', A.includes('_bare_q = str(body.get("canvas_text") or message or "")[:2000]'));
t('only when the user has not already asked for a search, and only in rules mode', A.includes('_web_rules = (not _web_enabled\n                      and await _switch_mode("context.needs_web", body.get("minds")) == "rules")') && A.includes('if _needs and _web_rules and _needs["web"]["on"]:'));
t('every decision is an event', A.includes('await emit_event({"type": "context.needs", "session_id": session_id, "decider": "rules",'));
t('a search the rules started holds the answer at most 3 s', A.includes('_wres = await asyncio.wait_for(asyncio.shield(_web_task), 3.0)'));
t('past the budget: the phase says late, and the sources follow later - never an empty "no results"', A.includes('"late",') && A.includes('asyncio.create_task(_late_results())') && A.includes('fr["late"] = True'));
t('the model is told the search was already run', A.includes('This search was run for you before you answered; do not call web.search again for the'));
t('the decision is a phase frame (needs stage)', A.includes('await _q.put(_ph.sse("needs", "web", "deciding whether to search the web",'));
{
  // a single-pass answer says "writing the answer" only after the search has answered (or the cap let it go)
  const pump = A.slice(A.indexOf('async def _pump_main():'), A.indexOf('async def _pump_two_tier():'));
  const wait = pump.indexOf('_wres = await asyncio.wait_for(asyncio.shield(_web_task), 3.0)');
  const mem = pump.indexOf('_mfrag = await _await_mem_deep()');
  const start = pump.indexOf('await _model_start_after_context(_late)');
  const run = pump.indexOf('AGENT_RUNNER.run_stream(');
  t('single pass: the model start frame follows the web and memory waits and precedes the model call',
    wait > 0 && mem > wait && start > mem && run > start);
  t('the early start frame is kept only when the model does not wait (no search, or tier 1 of two-tier)',
    A.includes('if not _model_waits_for_context:') &&
    A.includes('_model_waits_for_context = ((_web_task is not None or _mem_task is not None)\n                                    and not (_two_tier is not None and _tt_plan.get("split")))'));
  t('the model duration is measured from when the model really started', A.includes('nonlocal _model_t0'));
}
{
  // deep memory before the model when the turn refers back (CU-T.5), behind its own switch
  t('the memory switch is registered, off by default', S.includes('_f("context.needs_memory", ("off", "rules"), "chat", "CU-T.5",'));
  t('the memory rules run only in rules mode, and only switch memory on', A.includes('_mem_rules = await _switch_mode("context.needs_memory", body.get("minds")) == "rules"') && A.includes('if _needs and _mem_rules and _needs["memory_deep"]["on"]:'));
  t('the deep search starts at message arrival, held to 1.5 s', A.includes('_mem_task = asyncio.create_task(_run_mem_deep())') && A.includes('get_memory_context_deep(_mem_query, limit=8, budget_s=1.5)'));
  t('a hang cannot hold the answer past 2.5 s', A.includes('_mr = await asyncio.wait_for(asyncio.shield(_mem_task), 2.5)'));
  const tier2 = A.slice(A.indexOf('async def _pump_two_tier():'), A.indexOf('async def _pump_opener():'));
  t('tier 2 of a two-tier reply gets the deep memory too', tier2.includes('_mfrag2 = await _await_mem_deep()'));
  t('the decision and the search are phase frames', A.includes('"deciding whether to search earlier conversations"') && A.includes('"acquire", "memory", _MEM_LABEL, "start"'));
  t('the web decision frame is only sent when the web switch is on', A.includes('if _needs is not None and _web_rules:'));
  t('the stream teardown cancels the memory search and its watcher', A.includes('_web_task, _web_phase, _mem_task, _mem_phase):'));
  const H = R('vera/fabric/memory_hooks.py');
  t('the deep search reads every session and holds one budget', H.includes('async def get_memory_context_deep(query: str, limit: int = 8, budget_s: float = 1.5)') && H.includes('_recall_core.fast_then_vector('));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
