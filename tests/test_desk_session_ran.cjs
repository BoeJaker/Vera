// DU-E-session: a command that ran shows its colours (ANSI), its exit code, and "run again" - asked first, in place; then
// the same capability with the same arguments, the answer written into the item. The chat keeps on the item what ran it.
//   node tests/test_desk_session_ran.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'term:probe';
const ESC = String.fromCharCode(27);
const OUT = ESC + '[1;32mPASS' + ESC + '[0m tests/a.py\n' + ESC + '[31mFAIL' + ESC + '[0m tests/b.py\n' + ESC + '[2K' + ESC + '[38;5;11mwarn' + ESC + '[39m plain\n' + ESC + ']0;title' + String.fromCharCode(7) + 'done';
const draw = (c, el) => mod.BLOCK.session(Object.assign({ command: 'pytest -q', host: 'ct126', output: OUT }, c || {}), 'l', KEY, el || { dataset: {}, _live: {} });

{ const h = draw({ rc: 1, cap: 'exec.bash.run', args: { command: 'pytest -q' } });
  t('colours: green bold PASS, red FAIL, a 256-colour warn', /<span style="color:#98c379;font-weight:700">PASS<\/span>/.test(h) && /<span style="color:#e06c75">FAIL<\/span>/.test(h) && /<span style="color:#ffd68a">warn<\/span>/.test(h), (h.match(/<code>[\s\S]*<\/code>/) || [''])[0].slice(0, 300));
  t('no escape codes left as text (cursor, title, ...)', !h.includes(ESC) && !h.includes('[2K') && !h.includes(']0;title') && h.includes('done'));
  t('the exit code, red when it failed', h.includes('<span class="vc-rc bad" title="exit code">exit 1</span>'));
  t('run again, when it knows what ran it', h.includes('data-act="tagain"'));
  t('no question until it is pressed', !h.includes('vc-ran-ask')); }
t('exit 0 is green', draw({ rc: 0, cap: 'x' }).includes('<span class="vc-rc ok" title="exit code">exit 0</span>'));
t('without its capability there is nothing to run again', !draw({ rc: 0 }).includes('data-act="tagain"'));
{ const el = { dataset: {}, _live: {}, _ranAsk: { [KEY]: true } }; const h = draw({ rc: 1, cap: 'exec.bash.run' }, el);
  t('pressed: it asks, in place, naming the command and the host', h.includes('class="vc-ran-ask"') && h.includes('<code>pytest -q</code> again on <b>ct126</b>?') && h.includes('data-act="tagainok"') && h.includes('data-act="tagainno"')); }
{ const el = { dataset: {}, _live: {}, _ranBusy: { [KEY]: true } }; t('running: it says so', draw({ cap: 'x' }, el).includes('running…')); }
t('when it ran again', draw({ cap: 'x', ran_at: '2026-10-16T22:10:05.000Z' }).includes('ran again 22:10:05'));
t('run again calls the same capability with the same arguments and writes the answer into the item', C.includes("body: JSON.stringify({ name: c.cap, arguments: c.args || {} })") && C.includes("await this.call('canvas.update', Object.assign(this._ref(key), { content: Object.assign({}, c, { output: out.slice(0, 16000), ran_at: new Date().toISOString() }, rc != null ? { rc } : {}) }))"));
t('the chat keeps what ran it on the item (adapter and declared path)', H.includes('function _cvRanOf(c, ctx, it){') && H.includes("(c,ctx)=>_cvRanOf(c, ctx, _cvCapTerm(c) ||") && fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'present.js'), 'utf8').includes('output:out.slice(0,16000), cap, args:Object.assign({}, args||{}) }'));   /* the declared path is shared (DO-0.1) */
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
