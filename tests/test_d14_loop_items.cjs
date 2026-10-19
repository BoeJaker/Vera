// D14-F1..F3, the faults the 18 Oct live loop showed on the desk (DESK-BACKLOG D14): a step item held the loop's digest instruction
// instead of its data (F1), an exec card read "no host yet · login shell" over a base64 blob (F2), and the run item's step rows
// wrapped over its head (F3).
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };

/* F1 - the chat's own reader, cut from the page */
const CH = R('vera/chat/chat_panel.html');
/* each function runs to the next one at the same indent (they sit side by side in the page's loop section) */
const cut = (start) => { const a = CH.indexOf(start); if (a < 0) throw new Error('missing ' + start); const b = CH.indexOf('\n  function ', a + 1); const c = CH.indexOf('\n  async function ', a + 1); return CH.slice(a, Math.min(b < 0 ? Infinity : b, c < 0 ? Infinity : c)); };
const ctx = { JSON, String }; vm.createContext(ctx); vm.runInContext(cut('function _jsonPrefix(s)') + '\n' + cut('function _loopPreviewValue(p)') + '\nthis.read = _loopPreviewValue;', ctx);
const data = { query: 'latest BTC USD price and top 3 crypto market news headlines', engine_used: 'ddg', count: 5,
  sources: [{ url: 'https://coinstats.app/ai/a/latest-news-for-bitcoin', title: 'Bitcoin (BTC) News Today', snippet: 'BTC is down' }, { url: 'https://example.net/b', title: 'BTC slips', snippet: 'x' }] };
/* as dag_workshop_capabilities builds it: the instruction, then the start of the answer's JSON (cut) */
const DW = R('vera/dag/dag_workshop_capabilities.py');
const digest = '[STRUCTURED DATA — NOT summarised. JSON dict: 6 items. To USE it, parse the SAVED FILE with exec.python.run (json.load) — do NOT eyeball, re-count, or re-summarise it; the count above is authoritative.]\nSample (first items): ' + JSON.stringify(data).slice(0, 230);
t('the digest this test builds is the one the loop writes', DW.includes('return (f"[STRUCTURED DATA — NOT summarised. JSON {kind}: {cnt}.{_access} To USE it, parse the "') && DW.includes('f"Sample (first items): {sample_txt}")'));
const v = ctx.read(digest);
t('F1: a digest reads as its data - the query and every source that arrived whole - not as the instruction',
  v && typeof v === 'object' && v.query === data.query && v.count === 5 && Array.isArray(v.sources) && v.sources.length >= 1 && v.sources[0].title === 'Bitcoin (BTC) News Today' && !JSON.stringify(v).includes('NOT summarised'));
t('F1: plain JSON, a cut JSON and a writer\'s text still read as before', ctx.read(JSON.stringify(data)).count === 5 && ctx.read(JSON.stringify(data).slice(0, 150)).query === data.query
  && ctx.read('wrote notes.md (1.2 KB) - act on it: The price is 104k and falling on ETF outflows.') === 'The price is 104k and falling on ETF outflows.');

/* F2 + F3 - the desk's bodies */
const B = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js')).BLOCK;
const script = 'import json\nd = json.load(open("r.json"))\nprint(d["count"])\n';
const b64 = Buffer.from(script, 'utf8').toString('base64');
const el = {}; const ran = B.session({ command: 'echo ' + b64 + ' | base64 -d > r.py && python3 r.py', output: '5', host: '', session_id: '' }, 'm', 'k1', el);
const sub = (el._decl && el._decl.k1 && el._decl.k1.sub) || '';
t('F2: a command that ran with no host says Vera ran it, not "no host yet · login shell"', sub.includes('ran by Vera') && !sub.includes('no host yet') && !sub.includes('login shell'));
t('F2: the base64 blob folds to a mark, in the command and the header, and the script it carried shows decoded',
  !ran.includes(b64) && !sub.includes(b64) && ran.includes('echo ‹3 lines, below› | base64 -d &gt; r.py &amp;&amp; python3 r.py') && ran.includes('class="vc-pre vc-b64"') && ran.includes('print(d[&quot;count&quot;])') && ran.includes('>5<'));
const el2 = {}; B.session({ command: 'echo ' + b64 + ' | base64 -d > r.py && python3 r.py', output: '5', host: 'gpu-250' }, 'm', 'k2', el2);
t('F2: with a host the header names it, as before', el2._decl.k2.sub.includes('gpu-250') && !el2._decl.k2.sub.includes('ran by Vera'));
const bin = Buffer.from([0, 1, 2, 3, 250, 251, 252, 253, 0, 1, 2, 3, 250, 251, 252, 253, 0, 1, 2, 3, 250, 251, 252, 253, 0, 1, 2, 3, 250, 251, 252, 253, 0, 1, 2, 3, 250, 251, 252, 253]).toString('base64');
const raw = B.session({ command: 'echo ' + bin + ' | base64 -d > blob.bin', output: '' }, 'm', 'k3', {});
t('F2: base64 that is not text stays as it was', raw.includes(bin) && !raw.includes('vc-b64'));
const el4 = {}; B.session({ title: 'Terminal', host_id: '' }, 'm', 'k4', el4);
t('F2: a shell not yet connected still says "no host yet"', el4._decl.k4.sub.includes('no host yet'));
const SRC = R('vera/canvas/canvas_element.js');
t('F3: a step is one line - the label takes the room, the capabilities give way with an ellipsis, the time never wraps',
  SRC.includes('.vc-steps li span{flex:1 1 auto;min-width:3em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}')
  && SRC.includes('.vc-steps li code{flex:0 1 auto;min-width:0;max-width:45%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;')
  && SRC.includes('.vc-steps li em{flex:none;white-space:nowrap;'));
const lp = B.loop({ goal: 'g', status: 'running', steps: [{ n: 'Finish what the goal asks', cap: 'a.b · c.d · c.d', status: 'ok', ms: '4.1 s' }] });
t('F3: what an ellipsis hides is in the tooltip', lp.includes('<span title="Finish what the goal asks">') && lp.includes('<code title="a.b · c.d · c.d">'));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
