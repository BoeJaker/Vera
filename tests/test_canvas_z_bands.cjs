// The desk's z-order is the item contract's one table (DEEP-29 11A.4 Decision 6, owner decision D1 2026-10-16): the
// element's bands are VeraItem.Z, no desk item is given a z-index that is not a band, and the item-level CSS reads the
// tokens. Stacking inside one card (its head, its icons, its resize grip) is the card's own and is not checked.
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
console.log(n + ' passed');
