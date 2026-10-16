// An app window on the desk (DA-05b, DESK-SPINE-DESIGN §4.2): an app from the catalogue as an item - opened (manifest,
// skeleton, read), drawn like a capability's result, refreshed only while seen, restored from its content.
//   node tests/test_app_window.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };

t('opening: a skeleton, and the element is asked to open it once', () => {
  const opened = []; const el = { _apps: {}, _appOpen: (k, c) => opened.push([k, c.app]) };
  const h = V.BLOCK.app({ app: 'markets.overview' }, 'm', 'app:x', el);
  assert(/vc-app-sk/.test(h) && /opening markets\.overview/.test(h)); assert.deepStrictEqual(opened, [['app:x', 'markets.overview']]);
  V.BLOCK.app({ app: 'markets.overview' }, 'm', 'app:x', Object.assign(el, { _apps: { 'app:x': { opening: true } } })); assert.strictEqual(opened.length, 1, 'not twice');
});
t('read: drawn as a capability\'s result; its head line says the panel and when it was read; Refresh and Open the panel', () => {
  const el = { _apps: { 'app:y': { manifest: { panel: 'markets', title: 'Overview', read: { cap: 'markets.overview' } }, cap: 'markets.overview', args: {}, result: { rows: [{ a: 1 }, { a: 2 }] }, at: Date.now() - 4000 } } };
  const h = V.BLOCK.app({ app: 'markets.overview' }, 'm', 'app:y', el);
  assert(/class="vc-app"/.test(h) && /data-app="markets.overview"/.test(h) && !/vc-app-sk/.test(h));
  const d = el._decl['app:y']; assert(/markets · read 4 s ago/.test(d.sub)); assert.deepStrictEqual(d.acts.map((a) => a.act), ['appread', 'appopen']);
});
t('the manifest\'s own view when the generic mapping has only the raw tree; an error says what failed; a missing argument says what it needs', () => {
  assert(S.includes("if ((!v || v.kind === 'json') && mk && BLOCK[mk] && mk !== 'app' && st.result && typeof st.result === 'object') v = { kind: mk, content: st.result };"));
  const el = { _apps: { 'app:z': { manifest: { panel: 'p' }, error: 'boom' } } }; assert(/vc-res-err">boom/.test(V.BLOCK.app({ app: 'a' }, 'm', 'app:z', el)));
  assert(S.includes("st.waiting = m.title + ' needs ' + miss.join(', ');"));
});
t('only a read-shaped source is read on its own; refresh only while seen, one read in flight', () => {
  assert(S.includes("if (readShaped(st.cap)) return this._appRead(key); st.waiting = st.cap + ' is not a read - press Refresh to run it';"));
  assert(S.includes("if (!forced && !readShaped(st.cap)) return;") && S.includes('if (!st || !st.cap || st.reading) return;'));
  assert(S.includes("if (!c || !c.offsetParent || c.classList.contains('desk-chip') || c.classList.contains('cmp-behind') || c.classList.contains('compact') || c.classList.contains('ufold')) return;"));
  assert(S.includes("if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;"));
});
t('it is a block type the server keeps, named by its manifest, with its own glyph', () => {
  const P = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
  assert(/"app":\s+\{"desc": "An APP from the catalogue/.test(P) && P.includes('"content": "{app:str(app id), args?:obj, title?:str}"'));
  assert(V.KIND_GLYPH.app === '▦' && S.includes('if (b && b.content && !b.content.title && m.title) b.content.title = m.title;'));
});
t('a pill wins over a hand placement (the pill control did nothing on a card placed or sized by hand)', () => {
  assert(S.includes('const pilledKey = !lk && !!this._pillGet()[key];') && S.includes('const us = !lk && !pilledKey && up[key]') && S.includes('const p = (!pilledKey && userAt(key)) ||'));
});
console.log(n + ' passed');
