// LV-12 (owner 2026-10-17: "you need to add integrating the agentic loop into the desk/canvas ... its a key part of the chat ui and needs to be
// able to drive it just the same"): a loop step's answer lands as an item through the chat's own adapters, and the desk's topic follows the step.
//   node tests/test_loop_drives_desk.cjs
'use strict';
const fs = require('fs'); const path = require('path'); const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
t('a finished tool step of a loop is landed', H.includes("if(/\\.tool_done$/.test(t)&&ev.ok!==false&&ev.tool){ try{ _loopStepLand(R, ev); }catch(_){} }"));
t('through the chat\'s own adapters, as its own item beside the run', H.includes('async function _loopStepLand(R, ev){') && H.includes("made=_cvAdapt(String(ev.tool), res, null);") && H.includes("anchor:{mid:R.mid, turn:R.mid, beside:R.key}"));
t('the desk\'s topic follows the step (the run and its last steps\' items), only when the desk follows the conversation', H.includes("cv.setTopic([R.key].concat(R.stepKeys.slice(-4)), {}, 'loop:'+R.rid+':'+String(ev.step_id||0));") && H.includes("if(typeof _cvTopicOn==='function'&&!_cvTopicOn()) return;"));
t('one run, one item: the loop\'s session names it before its stream', H.includes("const rid=String(ev.run_id||ev.session_id||ev.stream_id||'run').slice(0,80);"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);