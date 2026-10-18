// The desk's z-order is the item contract's one table (DEEP-29 11A.4 Decision 6, owner decision D1 2026-10-16): the
// element's bands are VeraItem.Z, no desk item is given a z-index that is not a band, and the item-level CSS reads the
// tokens. Stacking inside one card (its head, its icons, its resize grip) is the card's own: it may keep a small literal,
// marked z-local (WSU-58 S5 extends the check to the held column, the dashboard and the chat's item rules).
//   node tests/test_canvas_z_bands.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const I = require(path.join(__dirname, '..', 'vera', 'ui', 'vera_item.js'));
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

t('the element\'s bands are VeraItem.Z', () => {
  const m = src.match(/const ZB = (\{[\s\S]*?\});/);
  assert(m, 'ZB table present');
  const ZB = Function('return ' + m[1])();
  assert.deepStrictEqual(ZB, I.Z);
});
t('no item is given a literal z-index from script', () => {
  const bad = src.split('\n').map((l, i) => [i + 1, l]).filter(([, l]) => /\.style\.zIndex\s*=\s*['"]\d+['"]/.test(l) || /stackOrder\(0,\s*\{\s*(dragging|full)/.test(l));
  assert.deepStrictEqual(bad.map(([i]) => i), [], 'literal z-index assignments at lines');
});
t('the desk\'s item-level rules read the tokens', () => {
  const rules = [];
  src.replace(/^\s*([^{}\n]*(?:\.stage > \.[a-z-]+|\.stage \.it(?:\.[a-z-]+)*))\{([^}]*z-index:[^;}]+)/gm, (all, sel, body) => { rules.push([sel.trim(), body]); return all; });
  assert(rules.length >= 5, 'found the item-level rules');
  const bad = rules.filter(([, body]) => !/z-index:var\(--z-[a-z-]+,\d+\)/.test(body));
  assert.deepStrictEqual(bad.map(([s]) => s), []);
});
t('windows sit under a full item and over every tile; the focused window is the top window', () => {
  assert(I.z('window', 0) > I.z('tile', 999) && I.z('window', 99) < I.Z.full);
  assert(/zOf\('window', ZB\.window\[1\] - ZB\.window\[0\]\)/.test(src), 'focus raises a window to the top of the band');
});

/* WSU-58 S5: every item host - the canvas element (desk and held column), the dashboard, the chat's item rules - writes a
   z-index only as a token of the one table, var(--z-<band>,<n>) with n the table's number. A literal is allowed only with
   a "z-local" comment on its line saying why (a card's insides, the held column's own chrome, or an overlay that must
   stay over a host page's own numbers); 0 and auto are not bands and pass. */
const css = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'vera-item.css'), 'utf8');
const TOK = {}; css.replace(/--z-([a-z-]+):\s*(\d+)/g, (a, k, v) => { TOK[k] = Number(v); return a; });
const dash = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
function zLint(text, lines) {
  const bad = [];
  (lines || text.split('\n')).forEach((l, i) => {
    l.replace(/var\(--z-([a-z-]+),\s*(\d+)\)/g, (a, k, v) => { if (TOK[k] !== Number(v)) bad.push((i + 1) + ': ' + a + ' (table: ' + TOK[k] + ')'); return a; });
    if (/z-local:/.test(l)) return;
    l.replace(/z-index:\s*(-?\d+)/g, (a, v) => { if (Number(v) !== 0) bad.push((i + 1) + ': ' + a); return a; });
    if (/\.style\.zIndex\s*=\s*String\(\s*\d/.test(l) || /\.style\.zIndex\s*=\s*['"]\d/.test(l)) bad.push((i + 1) + ': literal style.zIndex');
  });
  return bad;
}
t('the table: a toast is over the sheets, and vera-item.css carries every band VeraItem.Z has', () => {
  assert(I.Z.toast > I.Z.sheet && TOK.toast === I.Z.toast && TOK.sheet === I.Z.sheet);
  assert(TOK.tile === I.Z.tile[0] && TOK['tile-top'] === I.Z.tile[1] && TOK.window === I.Z.window[0] && TOK['window-top'] === I.Z.window[1]);
});
t('the canvas element (desk and held column): no raw z-index - a token of the table, or a z-local exception', () => {
  assert.deepStrictEqual(zLint(src), []);
  assert(/\.it\.maxed\{z-index:var\(--z-full,150\)!important/.test(src) && /\.it\.hovopen\{z-index:var\(--z-pop,230\)/.test(src) &&
    /\.vc-toast\{[^}]*z-index:var\(--z-toast,400\)/.test(src), 'the held column\'s max, hover-open and toast take their bands');
});
t('the dashboard: no raw z-index - a token of the table, or a z-local exception; its windows read the window band', () => {
  assert.deepStrictEqual(zLint(dash), []);
  const m = dash.match(/var ZW = \(window\.VeraItem && window\.VeraItem\.Z && window\.VeraItem\.Z\.window\) \|\| (\[\d+, \d+\]);/);
  assert(m, 'winRaise reads VeraItem.Z.window'); assert.deepStrictEqual(JSON.parse(m[1]), I.Z.window, 'its fallback is the table\'s');
  assert(/html\.vd-solo-mode body>\.widget\.vd-solo\{[\s\S]{0,200}z-index:var\(--z-full,150\)/.test(dash), 'a solo widget is a full item');
});
t('the chat\'s item rules (item menus, the artifact window, the context menu and runner): tokens or z-local only', () => {
  const sels = ['.wmenu', '.art-pop', '.cmw', '.cmenu', '.crun'];
  const lines = sels.map((s) => { const r = chat.match(new RegExp('^' + s.replace(/[.]/g, '\\.') + '\\{[^\\n]*', 'm')); assert(r && /z-index:/.test(r[0]), s + ' rule with a z-index'); return r[0]; });
  assert.deepStrictEqual(zLint('', lines), []);
  assert.deepStrictEqual(zLint(chat).filter((b) => /var\(--z-/.test(b)), [], 'every token the chat writes is the table\'s number');
});
t('the lint bites: a raw number fails, a wrong token fails, a documented exception passes', () => {
  assert.strictEqual(zLint('.x{z-index:7}').length, 1);
  assert.strictEqual(zLint('.x{z-index:var(--z-pop,231)}').length, 1);
  assert.strictEqual(zLint('it.style.zIndex = String(130 + n);').length, 1);
  assert.strictEqual(zLint('.x{z-index:7}  /* z-local: inside a card */').length, 0);
  assert.strictEqual(zLint('.x{z-index:var(--z-pop,230)} .y{z-index:0} .z{z-index:auto}').length, 0);
});
console.log(n + ' passed');
