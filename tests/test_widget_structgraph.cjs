// THE EXPLODE CONTRACT AS A WIDGET FORM (the canvas's final form §3.6a; vera/widgets/widget_record.py +
// vera/widgets/widget_element.js).
//
// Code and prose read as a structured graph already exist — the contract, the renderer, the layers, the caps — but
// the only way to put one anywhere was a bespoke drawer on the canvas. As a FORM it draws wherever a widget can,
// at five sizes, and carries a source it can read again, which an exploded item never had.
//   node tests/test_widget_structgraph.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..');
const EL = fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const REC = fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'widget_record.py'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the element in node, without a DOM (the harness test_dashboard_records_array.cjs uses)
function element(defined) {
  const made = [];
  const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, getAttribute() { return null; }, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, children: [], remove() {} });
  const window = { addEventListener() {}, location: { hash: '' }, customElements: { get(n) { return defined && defined.indexOf(n) >= 0 ? class {} : null; }, define() {} }, HTMLElement: class {}, CustomEvent: class {}, ResizeObserver: class { observe() {} disconnect() {} }, matchMedia: () => ({ matches: false, addEventListener() {} }), getComputedStyle: () => ({ getPropertyValue: () => '' }) };
  const document = Object.assign(stub(), { head: stub(), body: stub(), documentElement: stub(),
    createElement: (tag) => { const e = Object.assign(stub(), { tagName: String(tag).toUpperCase(), attrs: {}, doc: null,
      setAttribute(k, v) { this.attrs[k] = v; }, setDoc(d) { this.doc = d; } }); made.push(e); return e; } });
  const ctx = { window, document, console, setTimeout, clearTimeout, setInterval, clearInterval, fetch: () => Promise.reject(new Error('no net')), HTMLElement: window.HTMLElement, CustomEvent: window.CustomEvent, customElements: window.customElements, JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Error, isNaN, parseInt, parseFloat };
  vm.createContext(ctx); vm.runInContext(EL, ctx, { filename: 'widget_element.js' });
  return { VW: ctx.window.VeraWidget, made, document };
}

/* a small, real-shaped contract: two cards with spans, one typed relation, two layers, one assessment */
const CONTRACT = {
  kind: 'code', source: { path: 'vera/research/assess_core.py', label: 'assess_core.py' },
  layout: { direction: 'LR', mode: 'dependency' },
  layers: [{ id: 'symbols', label: 'Symbols', by: 'code_explode_core', kind: 'entity', on: true },
           { id: 'ner', label: 'GLiNER', by: 'gliner', kind: 'entity', on: false }],
  groups: [{ id: 'g1', label: 'assess_core.py' }],
  cards: [{ id: 'c1', kind: 'function', label: 'score', span: [10, 240], line: 2, line_end: 9, layer: 'symbols', by: 'code_explode_core' },
          { id: 'c2', kind: 'class', label: 'Scorer', span: [260, 700], line: 12, line_end: 40, layer: 'symbols', by: 'code_explode_core' }],
  edges: [{ from: 'c1', to: 'c2', label: 'CALLS', resolution: 'exact', layer: 'symbols' }],
  assessments: [{ id: 'a1', scorer: 'readability', score: 0.62, evidence: 'span 10-240' }],
};

/* ── the form is declared, beside the graphs, and says what it is ────────────────────────────────────────────── */
t('the registry carries a structgraph form, shaped as a graph, on the widgets board',
  /_F\("structgraph", "graph", glyph="structgraph", options=\("mode", "layers", "assess"\),\s*\n\s*name="Structured graph", boards=\("widgets",\)\)/.test(REC));
t('it sits with the other graphs, not off on its own', REC.indexOf('_F("structgraph"') > REC.indexOf('# ── graphs ──') && REC.indexOf('_F("structgraph"') < REC.indexOf('_F("orbit"'));
{
  const { VW } = element([]);
  const f = VW.forms().find((x) => x.id === 'structgraph');
  t('the element knows it too, as a graph at every size', !!f && f.shape === 'graph' && JSON.stringify(f.sizes) === JSON.stringify(['xs', 's', 'm', 'l', 'xl']), JSON.stringify(f));
}

/* ── what it draws, per size ─────────────────────────────────────────────────────────────────────────────────── */
{
  const { VW } = element([]);
  const strip = (h) => String(h).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  t('a sticker says what was found — a diagram is not readable at that size',
    VW.figure('structgraph', CONTRACT) === '2 cards', VW.figure('structgraph', CONTRACT));
  /* xs and s are the element's OWN sticker and chip, built for every form from its glyph and figure() — a form that
     drew its own small face would be the one that looked unlike the other hundred-odd */
  const s = VW.draw('structgraph', CONTRACT, 's', { height: 40 });
  t('at s: the element\'s chip, carrying what was found', /class="vw-chip"/.test(s) && /2 cards/.test(strip(s)), strip(s));
  t('at xs: the element\'s sticker, same figure', /class="vw-xs"/.test(VW.draw('structgraph', CONTRACT, 'xs', {})) && /2 cards/.test(strip(VW.draw('structgraph', CONTRACT, 'xs', {}))));
  const m = VW.draw('structgraph', CONTRACT, 'm', { height: 260 });
  t('at m: a slot for the estate\'s own renderer, carrying the contract', /class="vw-sg"/.test(m) && /data-sg="/.test(m) && /renderer=&quot;struct&quot;|renderer="struct"/.test(m), strip(m).slice(0, 80));
  t('...and the contract in the slot is the contract, not a summary of it',
    (() => { const raw = (m.match(/data-sg="([^"]*)"/) || [])[1] || ''; const back = JSON.parse(raw.replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'"));
      return back.cards.length === 2 && back.edges[0].resolution === 'exact' && back.cards[0].span[1] === 240; })());
  t('the mode the contract was laid out in rides with it, so the drawing opens as it was read', /mode=(&quot;|")dependency/.test(m));
  t('nothing to draw says so, rather than drawing an empty frame',
    /needs an Explode contract/.test(strip(VW.draw('structgraph', { nope: 1 }, 'm', { height: 200 }))) &&
    /nothing was found/.test(strip(VW.draw('structgraph', { kind: 'prose', cards: [], edges: [] }, 'm', { height: 200 }))));
  const l = VW.draw('structgraph', CONTRACT, 'l', { height: 520 });
  t('at l the same slot, given the room it was drawn with', /class="vw-sg"/.test(l) && /height:520px/.test(l));
}

/* ── and it mounts the ONE renderer, handed the contract it already holds ────────────────────────────────────── */
{
  const { VW, made, document } = element(['vera-graph-embed']);
  const slot = Object.assign({ dataset: { sg: JSON.stringify(CONTRACT), sgAttrs: 'renderer="struct" mode="dependency"' }, style: {}, innerHTML: 'x', appendChild(el) { this.kid = el; } }, {});
  const root = { querySelectorAll: (sel) => (/vw-sg/.test(sel) ? [slot] : []) };
  const n = VW.hydrate(root);
  const el = slot.kid;
  t('the slot becomes the estate\'s embed, and is not hydrated twice', n === 1 && !!el && el.tagName === 'VERA-GRAPH-EMBED' && slot.dataset.live === '1');
  t('it is asked for the structured renderer, bare, in the mode the contract names',
    el.attrs.renderer === 'struct' && el.attrs.mode === 'dependency' && 'bare' in el.attrs);
  t('AND IT IS HANDED THE CONTRACT — setDoc, not a second reading of the source',
    !!el.doc && el.doc.cards.length === 2 && el.doc.source.path === 'vera/research/assess_core.py');
  // with the element absent the slot is left alone: the counts stand in, and a later hydrate still works
  const { VW: VW2 } = element([]);
  const slot2 = { dataset: { sg: JSON.stringify(CONTRACT) }, style: {}, innerHTML: '', appendChild() {} };
  t('without the renderer on the page, nothing is mounted and nothing is marked live',
    VW2.hydrate({ querySelectorAll: (sel) => (/vw-sg/.test(sel) ? [slot2] : []) }) === 0 && slot2.dataset.live === undefined);
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
