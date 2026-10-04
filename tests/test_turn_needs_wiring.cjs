// The chat stream decides from the turn's words whether it needs the web before the model, behind a switch.
//   node tests/test_turn_needs_wiring.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const A = R('vera/agents/agents.py'), S = R('vera/minds/switchboard_core.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the switch is registered, off by default, owned by context-and-ui', S.includes('_f("context.needs_web", ("off", "rules"), "chat", "CU-T.3",') && S.includes('owner="context-and-ui"'));
t('a switchboard that cannot be read leaves the switch off', A.includes('async def _switch_mode(feature_id: str, minds: Any = None, default: str = "off") -> str:') && A.includes('return default'));
t('the rules read the bare question, never the message with its injected blocks', A.includes('_bare_q = str(body.get("canvas_text") or message or "")[:2000]'));
t('only when the user has not already asked for a search, and only in rules mode', A.includes('if not _web_enabled and _bare_q.strip():') && A.includes('if await _switch_mode("context.needs_web", body.get("minds")) == "rules":'));
t('every decision is an event', A.includes('await emit_event({"type": "context.needs", "session_id": session_id, "decider": "rules",'));
t('a search the rules started holds the answer at most 3 s', A.includes('_wres = await asyncio.wait_for(asyncio.shield(_web_task), 3.0)'));
t('past the budget: the phase says late, and the sources follow later - never an empty "no results"', A.includes('"late",') && A.includes('asyncio.create_task(_late_results())') && A.includes('fr["late"] = True'));
t('the model is told the search was already run', A.includes('This search was run for you before you answered; do not call web.search again for the'));
t('the decision is a phase frame (needs stage)', A.includes('await _q.put(_ph.sse("needs", "web", "deciding whether to search the web",'));
{
  // a single-pass answer says "writing the answer" only after the search has answered (or the cap let it go)
  const pump = A.slice(A.indexOf('async def _pump_main():'), A.indexOf('async def _pump_two_tier():'));
  const wait = pump.indexOf('_wres = await asyncio.wait_for(asyncio.shield(_web_task), 3.0)');
  const start = pump.indexOf('await _model_start_after_web(_late)');
  const run = pump.indexOf('AGENT_RUNNER.run_stream(');
  t('single pass: the model start frame follows the web wait and precedes the model call', wait > 0 && start > wait && run > start);
  t('the early start frame is kept only when the model does not wait (no search, or tier 1 of two-tier)',
    A.includes('if not _model_waits_for_web:') &&
    A.includes('_model_waits_for_web = (_web_task is not None\n                                and not (_two_tier is not None and _tt_plan.get("split")))'));
  t('the model duration is measured from when the model really started', A.includes('nonlocal _model_t0'));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
