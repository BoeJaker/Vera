// LV-1 (DESK-ARRANGEMENT 7.1 principle 1, owner 2026-10-17: "each widget and element could have an 'aspect' preference - these
// could be portrait (tall), landscape 'wide', and squarish, plus maybe another one for smaller tiles like widgets and small charts"):
// every element's preferred aspect, in order - declared, form, data shape, kind, type.
//   node tests/test_desk_aspect.cjs
'use strict';
const fs = require('fs'); const path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'); const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const A = V.aspectFor;
t('what the item declares wins', A({ own: 'tile', form: 'candles' }) === 'tile' && A({ own: 'nonsense', form: 'candles' }) === 'landscape');
t('OHLCV and time series are landscape', ['candles', 'trace', 'area', 'lines', 'small-multiples', 'timeline', 'gantt'].every((f) => A({ form: f }) === 'landscape'));
t('lists of results are portrait', ['list', 'checklist', 'feed', 'links', 'news', 'reading', 'notices', 'log'].every((f) => A({ form: f }) === 'portrait') && A({ type: 'records' }) === 'portrait');
t('tables, graphs and maps are square', ['table', 'kv', 'graph', 'vgraph', 'scatter', 'globe', 'treemap', 'calendar'].every((f) => A({ form: f }) === 'square') && A({ type: 'table' }) === 'square');
t('small widgets and small charts (the widget system\'s, not OHLCV) are tiles', ['counter', 'gauge', 'ring', 'hero', 'numbers', 'sparkline', 'minigraph', 'status'].every((f) => A({ form: f }) === 'tile'));
t('an @iso form is its flat form', A({ form: 'gauge@iso' }) === 'tile' && A({ form: 'area@iso' }) === 'landscape');
t('an unknown form falls to its data shape, then the kind guess, then the type', A({ form: 'zzz', shape: 'series' }) === 'landscape' && A({ shape: 'level' }) === 'tile' && A({ kind: 'chart' }) === 'landscape' && A({ type: 'research' }) === 'landscape' && A({}) === '');
t('every form the widget system draws has an aspect by form or shape', (() => { const W = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
  const m = W.match(/const DRAWN = \{([\s\S]*?)\};/); const pairs = [...m[1].matchAll(/'?([\w-]+)'?:\s*'(\w+)'/g)].map((x) => [x[1], x[2]]);
  const miss = pairs.filter(([f, sh]) => !A({ form: f, shape: sh })); if (miss.length) console.log('  no aspect:', miss.map((x) => x.join(':')).join(' ')); return pairs.length > 100 && !miss.length; })());
t('placement asks for a size by aspect and marks the card (data-aspect); a tile stays small', S.includes("const asp = this._aspectOf(c, key); c.dataset.aspect = asp || '';")
  && S.includes("else if (kind === 'tile') ksz = { w: Math.round(Math.min(FW, 280)), h: 180 }; }") && S.includes("else if (!user && kind === 'tile') { o.maxW = Math.min(o.maxW || Infinity, 380); o.maxH = Math.min(o.maxH || Infinity, 260); o.ratio = 2; }"));
t('a card says how tall it wants to be for what it holds (measured content, a table rows, a widget drawn content) and the slots hear it', S.includes('_wantH(c, key) {') && S.includes('want: this._wantH(x.c, kx)'));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);