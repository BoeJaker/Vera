// DU-E-diagram: a diagram's source edited beside its drawing - a declared "Edit source" control, the edit mode (the source
// in a box on the left, the drawing on the right, Save and Cancel), the error under the box, and the old controls kept.
//   node tests/test_desk_diagram_edit.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'diagram:probe';
const SRC = 'graph TD\n  A[Ask] --> B[Search]\n  B --> C[Answer & "done"]';
const DIAG = { title: 'Flow', mermaid: SRC, caption: 'the lookup' };
const host = (ui) => { const h = { dataset: {}, _live: {} }; if (ui) h._dgUi = { [KEY]: Object.assign({ open: true, draft: SRC, good: SRC, err: '' }, ui) }; return h; };
const draw = (el, c) => mod.BLOCK.diagram(JSON.parse(JSON.stringify(c || DIAG)), 'l', KEY, el);
const acts = (el) => ((el._decl && el._decl[KEY] && el._decl[KEY].acts) || []).map((a) => a.act);

{ const el = host(); const h = draw(el);
  t('closed: the old controls stay (Open in the chat, Copy, Source)', ['dgopen', 'dgcopy', 'dgsrc'].every((a) => acts(el).includes(a)), acts(el).join(','));
  t('closed: "Edit source" is declared', acts(el).includes('dgedit') && el._decl[KEY].acts.find((a) => a.act === 'dgedit').label === 'Edit source');
  t('closed: the drawing and its source as before, no editor', h.includes('data-live="mermaid"') && h.includes('vc-dgsrc') && !h.includes('data-dg-src') && h.includes('the lookup')); }
{ const el = host({}); const h = draw(el);
  t('open: the source in a box, keyed to the item', h.includes('<textarea class="vc-dged-src" data-dg-src="diagram:probe"') && h.includes('A[Ask] --&gt; B[Search]'));
  t('open: the source is escaped inside the box', h.includes('&quot;done&quot;') && h.includes('&amp;'));
  t('open: the drawing beside the box (the same live slot), the box first', h.indexOf('vc-dged-l') < h.indexOf('vc-dged-r') && /class="vc-dged-r vc-diag"><div class="vc-live" data-live="mermaid" data-key="diagram:probe"/.test(h));
  t('open: Save and Cancel', h.includes('data-act="dgsave"') && h.includes('data-act="dgcancel"') && !/data-act="dgsave" disabled/.test(h));
  t('open: no error line shown while it parses', /<div class="vc-dged-err" role="status" hidden>/.test(h));
  t('open: the control says it is editing, and the old ones stay', acts(el)[0] === 'dgedit' && el._decl[KEY].acts[0].on === true && ['dgopen', 'dgcopy', 'dgsrc'].every((a) => acts(el).includes(a)));
  t('open: the caption stays', h.includes('the lookup')); }
{ const el = host({ draft: SRC + '\n  C --> D[oops', err: 'line 4: "[" is not closed' }); const h = draw(el);
  t('a draft that does not parse: the error under the box, Save held', h.includes('<div class="vc-dged-err" role="status">line 4: &quot;[&quot; is not closed</div>') && /data-act="dgsave" disabled/.test(h));
  t('...the draft is what the box holds', h.includes('D[oops</textarea>')); }
{ const el = host({}); const h = draw(el, { title: 'Old', code: SRC });
  t('content under `code`: edited the same way', h.includes('data-dg-src="diagram:probe"')); }
// the wiring in the element
t('the stacked layout is a container query', C.includes('.vc-dgedw{container:dged / inline-size') && C.includes('@container dged (max-width: 440px) { .vc-dged{grid-template-columns:minmax(0,1fr)} }'));
t('typing: debounced ~300 ms, drawn into the live element, no whole render', /dataset\.dgSrc == null[\s\S]{0,900}this\._mermaidInto\(inner, key\); \}, 300\);/.test(C));
t('a draft that does not parse keeps the last good drawing', C.includes("if (err) return; ui.good = ui.draft.trim();") && C.includes('const code = dg && dg.open && dg.good ? dg.good :'));
t('Save is the one write: canvas.update with the new source (and code/source when the item used them)', C.includes("this.call('canvas.update', Object.assign(this._ref(key), { content: nc }))") && C.includes("const nc = Object.assign({}, c, { mermaid: s }); if (c.code != null) nc.code = s; if (c.source != null) nc.source = s;"));
t('Esc in the box is Cancel', /dataset\.dgSrc == null \|\| ev\.key !== 'Escape'[\s\S]{0,200}data-act="dgcancel"/.test(C));
t('the parse check uses the element\'s own parser', C.includes('inner._build(src, t)'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
