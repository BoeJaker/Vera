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
// fix 1: a narrow card keeps "Edit source" in its head - it is declared lead, and the head draws a lead action first
/* WSU-58 S6: lead and replaces are static - the diagram registers them (VeraItem.registerBody) and the head fills them in (actOf) */
const lead = (a) => !!(a && mod.actOf('diagram', a).lead === true);
{ const el = host(); draw(el); const e = el._decl[KEY].acts.find((a) => a.act === 'dgedit');
  t('narrow card: "Edit source" is declared lead (closed)', lead(e));
  const el2 = host({}); draw(el2); t('narrow card: "Editing" is declared lead (open)', el2._decl[KEY].acts[0].act === 'dgedit' && lead(el2._decl[KEY].acts[0]));
  const had = globalThis.VeraOut; globalThis.VeraOut = { diagram: () => '<div class="code-card"></div>' };   /* the kit's card path */
  const el3 = host(); const h3 = draw(el3); globalThis.VeraOut = had;
  t('...on the kit card path too', h3.includes('vc-diagwrap') && lead(el3._decl[KEY].acts.find((a) => a.act === 'dgedit')), h3.slice(0, 80)); }
t('the head draws lead actions before the kit\'s (data-pri 0 survives the 640 px rule)', /dacts\.filter\(\(a\) => a\.lead\)\.forEach\(dact\);\s*if \(kit\) kit\.acts/.test(C) && C.includes('dacts.filter((a) => !a.lead).forEach(dact);') && C.includes('const dacts = declActs(b.type, d);'));
// fix 2: the box keeps its focus and caret across the redraws after opening
t('opening focuses the box: now, next frame, ~120 ms', /const dgFocus = \(el, key, force\) => \{[\s\S]{0,1200}go\(\); requestAnimationFrame\(go\); setTimeout\(go, 120\); \};/.test(C));
t('...with the caret it had (selection restored)', /const dgFocus[\s\S]{0,1000}ta\.setSelectionRange\(s, e\)/.test(C));
t('...called right after the open render', C.includes("if (ui.open) dgFocus(this, key, act === 'dgedit');"));
t('...and again after each placement while it held the focus', C.includes("this.addEventListener('vera:canvas:placed', () => { const m = this._dgUi || {}; Object.keys(m).forEach((k) => { if (m[k] && m[k].open && m[k].had) dgFocus(this, k, false); }); });"));
t('the caret is tracked as it is typed / moved', C.includes("['input', 'keyup', 'pointerup', 'select'].forEach((t) => body.addEventListener(t, dgCaret));"));
// ONE edit button: Edit source stands for the code kit's own Edit (replaces), so the kit's is left out of the head and the menu
t('Edit source replaces the kit’s Edit', /id: 'dgedit'[^\n]*lead: true, replaces: 'edit' \}/.test(C));   /* registered: BODY_SPEC.diagram */
t('...the head leaves a replaced kit action out', C.includes("if (kit) kit.acts.filter((a) => a.vo !== 'explode' && a.vo !== 'canvas' && a.vo !== 'pane' && !repl.has(a.vo)).sort("));
t('...and so does the item menu', C.includes("kit.acts.filter((x) => !SKIP_VO.has(x.vo) && !repl.has(x.vo) && (kk !== key ||"));
{ const el = {}; mod.BLOCK.diagram({ mermaid: SRC }, 'l', 'diagram:rep', el); const d = (el._decl || {})['diagram:rep'] || {};
  t('...as the diagram declares it', (d.acts || []).some((a) => a.act === 'dgedit' && mod.actOf('diagram', a).replaces === 'edit')); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
