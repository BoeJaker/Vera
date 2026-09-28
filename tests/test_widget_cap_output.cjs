// Capability output widgets (the widget review, round 2): the result forms, the one mapping from an answer to its
// widget records (VeraWidget.fromCapResult), and the stream sink (VeraWidget.fromCapStream). The fixtures are shared
// with tests/test_widget_cap_output.py, which holds the python mirror to the same answers.
//   node tests/test_widget_cap_output.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const bad = (h) => /wempty|vw-nodata|data-sample/.test(h);

// ── the result forms ──
const NEW = ['json', 'diff', 'code', 'progress', 'status', 'media', 'error', 'markdown'];
const drawn = new Set(W.forms().map((f) => f.id));
t('the result forms are drawn forms: ' + NEW.join(' · '), NEW.every((f) => drawn.has(f)) && JSON.stringify(W.capForms()) === JSON.stringify(['kv', 'table', 'list', 'json', 'log', 'terminal', 'diff', 'code', 'progress', 'hero', 'trace', 'area', 'column', 'status', 'files', 'media', 'error', 'markdown']));
t('every result form has a sample face that draws (the pickers show it before anything is read)', NEW.every((f) => { const h = W.draw(f, undefined, 'm'); return /data-sample="1"/.test(h) && !/wempty/.test(h); }), NEW.filter((f) => /wempty/.test(W.draw(f, undefined, 'm'))).join(' '));
const J = W.draw('json', { a: { b: [1, 2] }, s: 'x', n: null }, 'l', { sample: false });
t('json: a tree that folds (the first level open), leaves coloured by kind', /<details open><summary>/.test(J) && /class="jnum">1</.test(J) && /class="js">"x"</.test(J) && /class="jn">null</.test(J));
const D = W.draw('diff', { diff: 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n+c' }, 'l', { sample: false });
t('diff: additions and removals counted and coloured, the hunk marked', /class="da">\+2</.test(D) && /class="dd">−1</.test(D) && /<span class="dh">@@/.test(D) && /<span class="da">\+b/.test(D));
const C = W.draw('code', { path: 'vera/x.py', code: 'a = 1\nb = 2' }, 'l', { sample: false });
t('code: numbered lines, the file and its language', /<u>2<\/u>b = 2/.test(C) && /x\.py/.test(C) && /py · 2 lines/.test(C));
const P = W.draw('progress', { steps: [{ name: 'plan', status: 'done', elapsed_s: 2 }, { name: 'act', status: 'running' }, { name: 'check', status: 'failed' }, { name: 'land' }] }, 'l', { sample: false, height: 160 });
t('progress: each step with its state (done ✓ · running ● · failed ✗ · waiting ○) and its time; the bar is done of all; a failure is named', /vb-ps done[\s\S]*✓[\s\S]*plan[\s\S]*2s/.test(P) && /vb-ps running/.test(P) && /vb-ps failed/.test(P) && /vb-ps waiting/.test(P) && /1 of 4 · failed at check/.test(text(P)));
const S = W.draw('status', { status: 'degraded', message: 'one needs a look', checks: [{ name: 'redis', status: 'ok' }, { name: 'gate', status: 'warn' }] }, 'l', { sample: false, height: 160 });
t('status: the verdict large with its message, every check with its state', /<b style="color:[^"]*">degraded<\/b>/.test(S) && /one needs a look/.test(S) && /redis[\s\S]*ok/.test(text(S)) && /gate[\s\S]*warn/.test(text(S)));
t('media: an image by address or base64; an unsafe address is not drawn', /<img src="https:\/\/x\/a\.png"/.test(W.draw('media', { url: 'https://x/a.png' }, 'l', { sample: false })) && /data:image\/png;base64,iVBOR/.test(W.draw('media', { image_b64: 'iVBORw0KGgo=' }, 'l', { sample: false })) && !/<img/.test(W.draw('media', { url: 'javascript:alert(1)' }, 'l', { sample: false })));
const E = W.draw('error', { ok: false, error: 'no answer in 25 s', traceback: 'Traceback ...' }, 'l', { sample: false, record: { source: 'ct.run' } });
t('error: what failed, said once, the detail folded', /ct\.run failed/.test(E) && /no answer in 25 s/.test(E) && /<details class="vb-errd"><summary>detail<\/summary><pre>Traceback/.test(E));
const MD = W.draw('markdown', '## Head\n**bold** and `code`\n- one\n- two\n[link](https://x.y) <script>', 'l', { sample: false });
t('markdown: headings, emphasis, code, lists, links - and nothing it was given is markup (escaped first)', /<h4>Head<\/h4>/.test(MD) && /<b>bold<\/b>/.test(MD) && /<code>code<\/code>/.test(MD) && /<ul><li>one<\/li><li>two<\/li><\/ul>/.test(MD) && /<a href="https:\/\/x\.y"/.test(MD) && !/<script>/.test(MD));
t('a result form takes the whole body at L (no detail list squeezed beside it)', !/vw-l/.test(W.draw('code', { code: 'x' }, 'l', { sample: false })) && !/vw-l/.test(W.draw('json', { a: 1 }, 'l', { sample: false })));

// ── the mapping: every fixture's answer → the forms it should draw as, and each draws ──
const F = JSON.parse(fs.readFileSync(path.join(__dirname, 'widget_cap_fixtures.json'), 'utf8'));
F.forEach((f) => { const rs = W.fromCapResult(f.cap, f.result, { args: { a: 1 } }); const got = rs.map((r) => r.form);
  const okForms = f.form === null ? rs.length === 0 : (got[0] === f.form && (!f.second || got[1] === f.second));
  const h = rs[0] ? W.draw(rs[0].form, rs[0].data, 'l', { record: rs[0], height: 160, width: 500, sample: false }) : '';
  t('fromCapResult ' + f.cap + ' → ' + (f.form || 'nothing') + (f.second ? ' + ' + f.second : '') + (rs[0] ? ' (' + rs[0].why + ')' : ''), okForms && (!rs[0] || !bad(h)) && rs.every((r) => r.source === f.cap && r.read.args.a === 1 && r.why && r.data !== undefined), got.join(',') + ' ' + text(h).slice(0, 80)); });
t('an MCP envelope is opened; max bounds the records', W.fromCapResult('x', { type: 'tool_result', content: { ok: false, error: 'e' } })[0].form === 'error' && W.fromCapResult('unknown.metrics', { samples: [{ t: 1, a: 1, b: 2 }, { t: 2, a: 2, b: 3 }, { t: 3, a: 1, b: 1 }] }, { max: 1 }).length === 1);
t('a record fromCapResult makes is valid for the element (normalise keeps its form, title, source, data)', (() => { const r = W.normalise(W.fromCapResult('identity.status', { configured: true, reachable: true })[0]); return r.form === 'kv' && r.source === 'identity.status' && r.data && r.data.configured === true; })());
t('the contract is written at the top of the function', WE.includes('VeraWidget.fromCapResult(capName, result, opts?) → [record, ...]') && WE.includes('const sink = VeraWidget.fromCapStream(capName, opts?)'));

// ── the stream sink ──
const s1 = W.fromCapStream('loop.run'); let seen = 0; s1.subscribe(() => { seen++; });
s1.push({ step: 'plan', status: 'running' }).push({ step: 'plan', status: 'done', elapsed_s: 1 }).push({ step: 'act', status: 'running' });
const r1 = s1.record();
t('a stream of steps is a progress that updates each step in place', s1.kind === 'steps' && r1.form === 'progress' && r1.data.steps.length === 2 && r1.data.steps[0].status === 'done' && seen === 3 && r1.stream === true);
s1.end({ ok: true, report: '# Done\n\n' + 'x '.repeat(120) });
t('its end draws the final answer beside it through fromCapResult', s1.ended && s1.records().length >= 2 && s1.records()[1].form === 'markdown');
const s2 = W.fromCapStream('llm.generate'); ['Hel', 'lo ', 'world'].forEach((x) => s2.push({ delta: x }));
t('a stream of chunks is text that grows', s2.record().form === 'markdown' && s2.record().data === 'Hello world');
const s3 = W.fromCapStream('sysmon.sample'); [1, 2, 3].forEach((i) => s3.push({ t: i, v: i * 10 }));
t('a stream of samples is a trace', s3.record().form === 'trace' && s3.record().data.length === 3 && s3.record().data[2].v === 30);
const s4 = W.fromCapStream('exec.tail', { max: 20 }); for (let i = 0; i < 30; i++) s4.push('line ' + i);
const r4 = s4.record(); const h4 = W.draw(r4.form, r4.data, 'm', { record: r4, height: 90, sample: false });
t('anything else is a log that keeps its newest (follow) and drops past max', r4.form === 'log' && r4.data.length === 20 && /line 29/.test(h4) && !/line 0\b/.test(h4));
t('a sink can drive an element: attach() sets its record, at most once a frame', WE.includes('attach(el) { if (el && S.els.indexOf(el) < 0)') && WE.includes("el.setAttribute('record', JSON.stringify(rr))"));
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
