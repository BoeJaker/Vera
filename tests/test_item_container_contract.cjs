// The item container (DEEP-29 11A.4 Decision 4, owner decision D1 2026-10-16): ONE head for every kind on every surface of
// the canvas. A body draws no head - it declares its line and its actions, and the container draws them (the line beside
// the title, the actions in the item's menu); the head's one row is the standard one, in the ADR's order.
//   node tests/test_item_container_contract.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
const KIT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera_output_element.js'), 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

/* a sample of every kind, enough for its body to draw (a kind left out is drawn from {}) */
const SAMPLE = {
  markdown: { md: '# A\n\ntext' }, code: { code: 'def f():\n    return 1\n', lang: 'python', filename: 'f.py' },
  diagram: { mermaid: 'graph TD\n A-->B', title: 'Boot' }, explode: { binds: 'code:x', title: 'x - exploded' },
  image: { url: '/x.png' }, note: { text: 'hi' }, table: { columns: ['a', 'b'], rows: [[1, 2]] },
  widget: { widget: 'sysmon.status' }, session: { host_id: 'ct126' }, notebook: { notebook_id: 'nb1', cell_id: 'c1', cell_type: 'code', lang: 'python', content: 'print(1)' },
  panel: { panel: 'workers', src: '/ui/panels/file/worker_panel.html' }, schedule: { when: 'tomorrow', what: 'x' },
  html: { html: '<p>hi</p>' }, calendar: { month: '2026-10', events: [] }, timeline: { events: [{ when: '2026', label: 'x' }] },
  source: { url: 'https://example.org/a', title: 'A' }, result: { cap: 'fabric.query', args: {}, result: { ok: true }, ok: true },
  json: { data: { a: 1 } }, kv: { fields: { a: 1 } }, chat: { messages: [{ role: 'user', text: 'q' }] },
  records: { items: [{ id: '1', title: 'one', url: 'https://example.org' }] }, entities: { groups: [] },
  loop: { goal: 'g', status: 'running', steps: [] }, activity: { status: 'running', calls: [] },
  research: { status: 'running', items: [] },
};
const HEADS = /class="(?:vc-th|vc-codehead)"|<div class="code-bar">/;
const FORMER = ['tdetach', 'tattach', 'tshare', 'nbrun', 'nbopen', 'dgopen', 'dgcopy', 'dgsrc', 'xpsync', 'pquery', 'pdispatch', 'prefresh', 'pstand', 'cprev'];

t('no body draws a head of its own, and its former head buttons are declared, not drawn', () => {
  const kinds = Object.keys(V.BLOCK); assert(kinds.length >= 20, 'every kind is exported');
  global.document = global.document || {};   /* a page: the estate's elements draw */
  kinds.forEach((k) => {
    const el = { _live: {}, _pq: {}, _pdOpen: {}, deskAlign: () => false, previewOn: () => false };
    let html = ''; try { html = V.BLOCK[k](SAMPLE[k] || {}, 'm', k + ':t', el); } catch (e) { html = ''; }
    assert(!HEADS.test(String(html)), k + ' draws a head');
    FORMER.forEach((a) => assert(!String(html).includes('data-act="' + a + '"'), k + ' draws its former head button ' + a));
  });
});
t('what a head held is declared: a line and actions per kind', () => {
  const el = { _live: {}, _pq: {}, _pdOpen: {}, deskAlign: () => false, previewOn: () => false };
  const want = { session: ['tdetach', 'tshare'], notebook: ['nbrun', 'nbopen'], diagram: ['dgopen', 'dgcopy', 'dgsrc', 'dgedit'], explode: ['xpsync'], html: ['cprev'] };
  Object.keys(want).forEach((k) => { V.BLOCK[k](SAMPLE[k], 'm', k + ':d', el); const d = el._decl[k + ':d'];
    assert(d, k + ' declares'); assert.deepStrictEqual((d.acts || []).map((a) => a.act), want[k], k); });
  assert(/over the bridge/.test((V.BLOCK.panel(SAMPLE.panel, 'l', 'panel:d', el), el._decl['panel:d'].sub)));
});
t('the shared kit is asked for a headless card at every canvas call, and lists and runs its own actions', () => {
  assert.strictEqual((SRC.match(/bar: false/g) || []).length, 3, 'card, file card, diagram');
  assert(KIT.includes("const barOpen = (o) => '<div class=\"code-bar\"' + (o && o.bar === false ? ' hidden' : '') + '>';"));
  assert(KIT.includes('.code-card > .code-bar[hidden]{display:none}'));
  assert(/function barActions\(c\)/.test(KIT) && /function actOn\(c, name, b\)/.test(KIT) && /css: CSS, barActions, actOn \};/.test(KIT));
});
t('one head: glyph, title, the body\'s line, badges - then the standard row in the ADR\'s order', () => {
  /* WSU-58 S6: the row and its order are VeraItem.controls; the head draws each control (HB) in that order */
  const i = SRC.indexOf("const HB = {"), j = SRC.indexOf("const nn = ctl.length", i); assert(i > 0 && j > i);
  const order = ["hb('hdview'", "hb('lock'", "hb('pill'", "hb('max'", "hb('hdmore'", "hb('remove'"].map((s) => SRC.indexOf(s, i));
  assert(order.every((x) => x > i && x < j), 'every control in the row'); assert.deepStrictEqual(order, order.slice().sort((a, b) => a - b), 'in order');
  assert(SRC.includes('const ctl = row.map((id) => HB[id]());'));
  const VI = require('../vera/ui/vera_item.js'); assert.deepStrictEqual(VI.controls('table', { views: ['table', 'cards'], lock: true, pill: true }), ['view', 'lock', 'pill', 'max', 'more', 'drop']);
  assert(SRC.includes("(d.sub ? `<span class=\"it-sub\">${d.sub}</span>` : '')"));
  assert(!/class="it-ft"|OWN_HEAD|\.ownhead|own-fold|_ownBtns|data-act="ownbtn"/.test(SRC), 'no rail, no own-head kinds, no re-pressed buttons');
});
t('the menu holds the rest in order, then the body\'s declared actions, the kit\'s, its dropdowns and each pane\'s', () => {
  const a = SRC.indexOf('    _hdMenu(key, btn) {'), b = SRC.indexOf('    _viewMenu(key, btn) {'); const M = SRC.slice(a, b);
  const seq = ["row('park'", "row('size'", "row('desk-auto'", "row('open'", "row('fold'", "row('cmpsep'", "row('explode'", "row('edit'", "row('ctx'", "row('rerun'", "data-act=\"dive\"", 'const own = (kk, host, lead)'].map((s) => M.indexOf(s));
  /* WSU-58 S6: the entries and their order are VeraItem.menu's (MENU_ALL); each is drawn by R in that order */
  { const VI = require('../vera/ui/vera_item.js'); const ids = ['park', 'size', 'arrange', 'inplace', 'fold', 'group', 'explode', 'edit', 'context', 'refresh', 'dive'];
    const at = ids.map((x) => VI.MENU_ALL.indexOf(x)); assert(at.every((x) => x >= 0)); assert.deepStrictEqual(at, at.slice().sort((a, b) => a - b)); assert(M.includes('let h = VI.menu(b.type, { source: src }, o)')); }
  assert(seq.every((x) => x > 0), JSON.stringify(seq)); assert.deepStrictEqual(seq, seq.slice().sort((x, y) => x - y));
  assert(M.includes('VO.barActions(card)') && M.includes("data-act=\"vo\" data-vo-act=") && M.includes("c.querySelectorAll('.fu-p[data-key]')"));
  assert(SRC.includes("if (act === 'vo') {") && SRC.includes('VO.actOn(card, btn.dataset.voAct)'));
});
t('the column has the menu too: drawn over the page where there is no stage', () => {
  assert(SRC.includes("_popHost() { return this.shadowRoot.getElementById('stage') || this.shadowRoot; }") && SRC.includes("pop.style.position = 'fixed'"));
  assert(SRC.includes('  .hd-pop{position:absolute;z-index:var(--z-pop,230);'));
});
t('the item by keyboard: focusable, Enter opens, Esc restores, F fills, P pills, arrows move; a row control answers Enter', () => {
  assert(SRC.includes('return `<div class="${cls}" tabindex="0" data-key='));
  ["if (ev.key === 'Enter') { ev.preventDefault(); const h = t.querySelector(':scope > .it-hd'); if (h) h.click(); return; }", "if (ev.key === 'Escape') { if (t.classList.contains('maxed')) go('max'); return; }",
   "if (ev.key === 'f' || ev.key === 'F') return go('max');", "return go(this.deskAlign() ? 'pill' : 'fold');", "if (t.classList.contains('hb') && (ev.key === 'Enter' || ev.key === ' '))", '.it:focus-visible,.hda > .hb:focus-visible{outline:2px solid'].forEach((s) => assert(SRC.includes(s), s));
});
console.log(n + ' passed');