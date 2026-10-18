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
t('Desk follows (the LV-12 pin): auto / the chat / the running loop - a setting kept with the canvas, the loop moves the topic unless it is the chat, the chat does not while a loop runs and it is the loop', H.includes('<select id="cfgCvFollow"') && H.includes("cfgCvFollow:['desk-follow','auto'],") && H.includes("if(_cvCfg('cfgCvFollow')==='chat') return;") && H.includes("if(_cvCfg('cfgCvFollow')==='loop'&&typeof _LOOP_RUNS!=='undefined'"));
/* D14, the first live loop (2026-10-18): a real loop's previews are cut at 12000 characters or are text - nothing landed. The page's own
   reader, run here: a cut JSON is read to its last whole value, a writer's text after its "wrote ... act on it:" line. */
const src = (name) => { const i = H.indexOf('function ' + name + '('); return H.slice(i, H.indexOf('\n  }\n', i) + 4); };   /* to the function's closing line (its body holds quoted braces) */
const R = new Function(src('_jsonPrefix') + '\n' + src('_loopPreviewValue') + '\nreturn { _jsonPrefix, _loopPreviewValue };')();
const cut = JSON.stringify({ query: 'btc price', count: 3, sources: [{ url: 'https://a.example', title: 'A' }, { url: 'https://b.example', title: 'B' }, { url: 'https://c.example', title: 'C long text' }] }).slice(0, -30);
const v = R._loopPreviewValue(cut);
t('a cut JSON preview is read to its last whole value (the query and the sources that arrived whole)', v && v.query === 'btc price' && Array.isArray(v.sources) && v.sources.length === 2 && v.sources[1].title === 'B');
t('a whole JSON preview is read as it is', JSON.stringify(R._loopPreviewValue('{"a":1,"b":[1,2]}')) === '{"a":1,"b":[1,2]}');
t('a writer\'s preview is its text, after its own "wrote <file> ... act on it:" line', R._loopPreviewValue('prose.author wrote btc_summary.txt (515 chars, NOT parser-verified). Its content is shown here IN FULL - do NOT read it back; act on it:\n\nOn October 18 Bitcoin rose to 82k.') === 'On October 18 Bitcoin rose to 82k.');
t('a read of a file a step already wrote lands nothing; the run\'s own answer lands once beside it', H.includes("if(/(^|\\.)(fs\\.read|fs\\.list|files\\.read|read_file)$/.test(String(ev.tool))&&typeof res==='string') return;") && H.includes("if((t==='done'||/\\.done$/.test(t))&&ev.summary&&!R.answered){ R.answered=true; try{ _loopStepLand(R, {tool:'loop.answer', step_id:'answer', preview:String(ev.summary)}); }catch(_){} }"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);