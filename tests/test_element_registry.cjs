// DO-0.2 (DESK-OUTPUT-REDESIGN-PLAN §2.1, §6; backbone review 2026-10-18 points 1-3): THE element registry is
// VeraItem.registerBody with more fields - what it draws (contract), its faces (pill / tile / window / full), its size
// classes (xs-xl, each with the dashboard span it maps to), its own controls, where it goes deeper (navigation), the cap it
// searches with, its live source and the surfaces that use it. Every canvas BLOCK kind and every rich shared element is
// registered; there is no other element table; and what the head and the ⋯ menu read (controls / menu / body / action)
// answers exactly as before (a snapshot taken from the base, bleeding-edge-design 14017f87).
//   node tests/test_element_registry.cjs            check
//   node tests/test_element_registry.cjs --write    (re)write the head snapshot - on the BASE only, never after a change
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');
const ROOT = path.join(__dirname, '..');
const V = require(path.join(ROOT, 'vera', 'canvas', 'canvas_element.js'));
const VI = require(path.join(ROOT, 'vera', 'ui', 'vera_item.js'));
const SNAP = path.join(__dirname, 'fixtures', 'element_registry_heads.json');
const KINDS = Object.keys(V.BLOCK).filter((k) => k[0] !== '_');
const SHARED = ['vera-mkt-chart', 'vera-graph', 'vera-graph-embed', 'vera-out', 'vera-mermaid', 'vera-terminal', 'vera-flow-builder',
  'vera-research-card', 'vera-exploded', 'vera-mkt-map', 'vera-mkt-folio'];

/* ── what the head and the menu read, kind by kind, in every state a host can ask for ─────────────────────────────── */
function headAnswers(kind) {
  const out = { controls: [VI.controls(kind)], menu: [], body: null, actions: [] };
  [undefined, [], ['list'], ['list', 'cards']].forEach((views) => { for (let m = 0; m < 128; m++) {
    const o = {}; if (views !== undefined) o.views = views;
    if (m & 1) o.window = true; if (m & 2) o.lock = false; if (m & 4) o.pill = false; if (m & 8) o.max = false;
    if (m & 16) o.more = false; if (m & 32) o.fold = true; if (m & 64) o.narrow = true;
    out.controls.push(VI.controls(kind, o));
  } });
  let seed = 7; const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed; };
  const masks = [0, (1 << VI.MENU_ALL.length) - 1]; for (let i = 0; i < 300; i++) masks.push(rnd() % (1 << VI.MENU_ALL.length));
  [{}, { source: { cap: 'a.b' } }, null].forEach((item) => {
    out.menu.push(VI.menu(kind, item));
    masks.forEach((mk) => { const o = {}; VI.MENU_ALL.forEach((id, i) => { if (mk & (1 << i)) o[id] = true; }); out.menu.push(VI.menu(kind, item, o)); });
  });
  const b = VI.body(kind); out.body = { views: b.views, fit: b.fit, minSize: b.minSize, actions: b.actions };
  out.actions = b.actions.map((a) => VI.action(kind, a.id)).concat([VI.action(kind, 'nope')]);
  return out;
}
const digest = (o) => crypto.createHash('sha256').update(JSON.stringify(o)).digest('hex');
const HEAD_KINDS = KINDS.concat(['mystery']);

if (process.argv.includes('--write')) {
  const snap = { base: 'bleeding-edge-design 14017f87 (before DO-0.2)', kinds: {} };
  HEAD_KINDS.forEach((k) => { const a = headAnswers(k); snap.kinds[k] = { digest: digest(a), body: a.body, controls0: a.controls[0], menu0: a.menu[0] }; });
  fs.writeFileSync(SNAP, JSON.stringify(snap, null, 1) + '\n');
  console.log('wrote', SNAP, Object.keys(snap.kinds).length, 'kinds'); process.exit(0);
}

let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };
const PRES = (() => { const s = fs.readFileSync(path.join(ROOT, 'vera', 'capability_contract_core.py'), 'utf8');
  const i = s.indexOf('PRESENTATION_KINDS'), j = s.indexOf('\n}', i); return [...s.slice(i, j).matchAll(/^\s{4}"([a-z]+)": \{/gm)].map((m) => m[1]); })();

t('the head and the ⋯ menu read exactly what they read before: controls, menu, body, action - every kind, every state', () => {
  const snap = JSON.parse(fs.readFileSync(SNAP, 'utf8'));
  HEAD_KINDS.forEach((k) => { const s = snap.kinds[k]; assert(s, k + ' is in the snapshot'); const a = headAnswers(k);
    assert.deepStrictEqual(a.body, s.body, k + ': body'); assert.deepStrictEqual(a.controls[0], s.controls0, k + ': controls');
    assert.deepStrictEqual(a.menu[0], s.menu0, k + ': menu'); assert.strictEqual(digest(a), s.digest, k + ': the answers in every state'); });
  assert.strictEqual(Object.keys(snap.kinds).length, HEAD_KINDS.length, 'no kind left out of the snapshot');
});

t('every BLOCK kind and every rich shared element is registered, with the registry\'s fields', () => {
  assert(KINDS.length >= 25, 'every kind is exported');
  const reg = VI.bodies();
  KINDS.concat(SHARED).forEach((k) => { assert(reg.includes(k), k + ' is not registered'); const e = VI.element(k);
    ['contract', 'faces', 'sizes', 'spans', 'controls', 'navigation', 'search', 'live', 'surfaces', 'role', 'element', 'src'].forEach((f) => assert(f in e, k + ' has no ' + f));
    assert(e.contract.length, k + ' says what it draws'); assert(e.surfaces.length, k + ' says where it is used'); });
  KINDS.forEach((k) => { const e = VI.element(k); assert.strictEqual(e.role, 'body', k); assert.deepStrictEqual(e.faces, VI.FACES, k + ': the container gives every body every face');
    assert(e.surfaces.includes('desk') && e.surfaces.includes('panel:canvas'), k + ' surfaces'); });
  SHARED.forEach((k) => { const e = VI.element(k); assert.strictEqual(e.role, 'element', k); assert(e.element && e.src, k + ' names its element and file');
    assert(fs.existsSync(path.join(ROOT, e.src)), k + ': ' + e.src + ' exists'); });
});

t('the fields hold the ADR\'s vocabulary only: faces, size classes, surfaces, navigation, contract', () => {
  const surf = (s) => ['desk', 'dashboard', 'chat', 'lhm'].includes(s) || /^panel:[a-z0-9_-]+$/.test(s);
  const capOk = (c) => /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/.test(c) || /^<[^>]+>$/.test(c);
  VI.bodies().forEach((k) => { const e = VI.element(k);
    e.faces.forEach((f) => assert(['pill', 'tile', 'window', 'full'].includes(f), k + ' face ' + f));
    assert(!e.faces.includes('card') && !e.faces.includes('panel'), k + ': card is not a face, a panel is a surface');
    e.sizes.forEach((s) => assert(['xs', 's', 'm', 'l', 'xl'].includes(s), k + ' size ' + s));
    assert.deepStrictEqual(e.sizes, VI.SIZES.filter((s) => e.sizes.includes(s)), k + ': sizes in order');
    assert.deepStrictEqual(Object.keys(e.spans), e.sizes, k + ': a span for each size');
    e.surfaces.forEach((s) => assert(surf(s), k + ' surface ' + s));
    e.contract.forEach((c) => { assert(c.kind && VI.CONTRACT_BY.includes(c.by), k + ' contract ' + JSON.stringify(c)); assert(Array.isArray(c.reads), k + ' reads');
      if (c.by === 'present') assert(PRES.includes(c.kind), k + ': ' + c.kind + ' is not a presentation kind');
      if (c.by === 'item') assert(KINDS.includes(c.kind), k + ': ' + c.kind + ' is not an item kind'); });
    e.navigation.forEach((v) => { assert(VI.NAV_TO.includes(v.to), k + ' navigation to ' + v.to); assert(v.args && typeof v.args === 'object', k + ' navigation args'); });
    e.controls.forEach((c) => assert(c.id && c.label, k + ' control ' + JSON.stringify(c)));
    if (e.search) assert(capOk(e.search.cap), k + ' search ' + e.search.cap);
    if (e.live) { assert(e.live.cap || e.live.sub || e.live.route, k + ' live says what it reads'); if (e.live.cap) assert(capOk(e.live.cap), k + ' live ' + e.live.cap); }
  });
  /* a spec outside the vocabulary is cut to it */
  const b = VI.registerBody('zz-probe', { faces: ['card', 'tile', 'panel'], sizes: ['xxl', 'm', 'xs'], surfaces: ['desk', 'nowhere', 'panel:x'], navigation: [{ to: 'mars' }, { to: 'url', args: { url: '<url>' } }],
    contract: [{ kind: 'table', by: 'present', reads: ['rows'] }, { kind: 'x' }], search: { cap: 'markets.lookup' } });
  assert.deepStrictEqual([b.faces, b.sizes, b.surfaces, b.navigation.length, b.contract.length, b.search.cap], [['tile'], ['xs', 'm'], ['desk', 'panel:x'], 1, 1, 'markets.lookup']);
});

t('the dashboard span of a size class is the dashboard\'s own (vera-dashboard.js spanFor)', () => {
  const D = fs.readFileSync(path.join(ROOT, 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
  const m = D.match(/return (\{ xs: \[[^}]*\})\[size\]/); assert(m, 'spanFor\'s table');
  assert.deepStrictEqual(VI.SPANS, Function('return ' + m[1])());
  assert.deepStrictEqual(VI.element('table').spans, { s: [2, 1], m: [4, 2], l: [6, 2], xl: [8, 4] });
});

t('element(kind) is the full spec; elements(filter) finds by contract kind, surface, role, face', () => {
  const mc = VI.element('vera-mkt-chart');
  assert(mc.contract.some((c) => c.kind === 'ohlcv') && mc.contract.some((c) => c.kind === 'series'), 'ohlcv / series');
  assert.strictEqual(mc.search.cap, 'markets.lookup');
  assert(mc.navigation.some((v) => v.to === 'element' && v.args.element === 'vera-mkt-chart' && 'symbol' in v.args), 'to another symbol');
  assert(['tf', 'ind', 'vol', 'fit'].every((id) => mc.controls.some((c) => c.id === id)), 'its bar');
  assert(mc.surfaces.includes('desk') && mc.surfaces.includes('panel:markets'));
  const by = (f) => VI.elements(f).map((e) => e.kind);
  assert(by({ contract: 'ohlcv' }).includes('vera-mkt-chart'));
  assert(by({ contract: 'table' }).includes('table'));
  assert(by({ surface: 'panel:markets' }).includes('vera-flow-builder'));
  assert(by({ surface: 'panel' }).includes('vera-terminal'), 'panel matches any panel:<id>');
  assert(!by({ surface: 'dashboard' }).includes('vera-mkt-chart'), 'not a dashboard tile today');
  assert.deepStrictEqual(by({ role: 'element' }).sort(), SHARED.slice().sort());
  assert(by({ face: 'pill' }).length >= KINDS.length);
  assert.strictEqual(VI.elements().length, VI.bodies().length);
  /* a copy: changing it changes nothing */
  mc.faces.push('pill'); assert(!VI.element('vera-mkt-chart').faces.includes('pill'));
  assert.strictEqual(VI.element('no-such-kind'), null);
});

t('a body says the presentation kinds present() routes to it (widgets/present.js KINDS), and how it reads them', () => {
  const P = fs.readFileSync(path.join(ROOT, 'vera', 'widgets', 'present.js'), 'utf8');
  const rows = [...P.matchAll(/^\s{4}([a-z]+): \{ element: '([a-z]+)'/gm)].map((m) => [m[1], m[2]]); assert(rows.length >= 20);
  rows.filter(([, el]) => KINDS.includes(el)).forEach(([kind, el]) => assert(VI.element(el).contract.some((c) => c.by === 'present' && c.kind === kind), el + ' draws ' + kind));
});

/* ── ONE registry: registerBody is the only registration call; no other element table exists ────────────────────── */
function grepRepo(re) {
  try { return execFileSync('git', ['grep', '-n', '-I', '-E', re, '--', 'vera', 'tools', 'tests'], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 << 20 }).split('\n').filter(Boolean); }
  catch (e) { if (e.status === 1) return []; throw e; }
}
t('no second element table: registerBody is the one registration call, and only the registry declares faces / surfaces', () => {
  const HOME = ['vera/ui/vera_item.js', 'vera/canvas/canvas_element.js'];
  const outside = (lines) => lines.filter((l) => !HOME.includes(l.split(':')[0]) && !/^tests\//.test(l));
  assert.deepStrictEqual(outside(grepRepo('registerBody\\(')), [], 'registerBody called outside the registry');
  assert.deepStrictEqual(outside(grepRepo("faces\\s*:\\s*\\[\\s*['\"](pill|tile|window|full)")), [], 'faces declared outside the registry');
  assert.deepStrictEqual(outside(grepRepo("surfaces\\s*:\\s*\\[\\s*['\"](desk|dashboard|chat|lhm|panel:)")), [], 'surfaces declared outside the registry');
  assert.deepStrictEqual(outside(grepRepo('\\b(registerElement|ELEMENT_REGISTRY|ELEMENT_TABLE|ELEMENT_SPECS?|elementRegistry)\\b|\\bELEMENTS\\s*=\\s*\\{')), [], 'an element table by name');
  /* the canvas registers its kinds in the one call, from BODY_SPEC (the S6 block) - nothing beside it */
  const S = fs.readFileSync(path.join(ROOT, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
  assert.strictEqual((S.match(/VI\.registerBody\(/g) || []).length, 1, 'one registration, in the S6 block');
  const D = fs.readFileSync(path.join(ROOT, 'tools', 'element_registry_dump.cjs'), 'utf8');
  assert(!/faces\s*:\s*\[|surfaces\s*:\s*\[|registerBody\(/.test(D) && D.includes('VeraItem') && D.includes('.elements('), 'the dump only reads the registry');
});

t('the dump prints the registry as JSON', () => {
  const out = JSON.parse(execFileSync(process.execPath, [path.join(ROOT, 'tools', 'element_registry_dump.cjs')], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 << 20 }));
  assert.strictEqual(out.count, VI.bodies().length - 1, 'every entry (less the probe this test registered in its own process)');
  assert.deepStrictEqual(out.entries.map((e) => e.kind).sort(), KINDS.concat(SHARED).sort());
  assert.deepStrictEqual(out.vocabulary.faces, VI.FACES); assert.deepStrictEqual(out.vocabulary.spans, VI.SPANS);
  assert(out.entries.every((e) => !('draw' in e) && !('natural' in e)), 'functions are not data');
});

console.log(n + ' passed');
