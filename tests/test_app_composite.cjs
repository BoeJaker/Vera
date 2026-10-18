// DA-07a: a composite app (DESK-SPINE-DESIGN §6.1) - a list of reads, each a part; the parts read side by side and each
// draws as it arrives (its own skeleton, its own error), in the manifest's layout: dashboard, tabs, split, stack.
//   node tests/test_app_composite.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const MAN = (layout, parts) => ({ panel: 'markets', title: 'Symbol', read: [{ as: 'quote', cap: 'markets.quote' }, { as: 'chart', cap: 'markets.chart' }, { as: 'news', cap: 'info.seek' }, { as: 'mood', cap: 'markets.sentiment.map' }],
  view: Object.assign({ kind: 'composite' }, layout ? { layout } : {}, parts ? { parts } : {}) });
const PARTS = () => [
  { as: 'quote', cap: 'markets.quote', args: {}, result: { symbol: 'ETH-USD', price: 2412.5, change: 1.2 }, ms: 120 },
  { as: 'chart', cap: 'markets.chart', args: {}, reading: true },
  { as: 'news', cap: 'info.seek', args: {}, waiting: 'info.seek is not a read - press Refresh to run it' },
  { as: 'mood', cap: 'markets.sentiment.map', args: {}, error: 'sentiment model offline', ms: 40 }];
const draw = (man, ui) => { const el = { _apps: { 'app:s': { manifest: man, parts: PARTS(), result: null, at: Date.now(), ui } } }; return V.BLOCK.app({ app: 'markets.symbol' }, 'l', 'app:s', el); };
const secs = (h) => [...h.matchAll(/<section class="vc-app-part([^"]*)" data-app-part="([^"]+)"><header><b>([^<]*)<\/b><span>([^<]*)<\/span>/g)].map((m) => ({ cls: m[1].trim(), as: m[2], title: m[3], state: m[4] }));

{ const h = draw(MAN(), null); const s = secs(h);
  t('a composite is drawn as its parts (dashboard by default)', /data-as="composite"/.test(h) && /<div class="vc-app-parts" data-layout="dashboard">/.test(h) && s.map((x) => x.as).join() === 'quote,chart,news,mood', JSON.stringify(s));
  const part = (as) => (h.split('data-app-part="' + as + '"')[1] || '').split('</section>')[0];
  t('a part with its answer draws it (the quote as fields) with its time', /ETH-USD/.test(part('quote')) && /2412\.5/.test(part('quote')) && s[0].state === '120 ms');
  t('a part still reading has its own skeleton', s[1].cls === 'reading' && s[1].state === 'reading…' && /vc-app-psk/.test(part('chart')));
  t('a part that only acts says so, waiting for Refresh', /press Refresh to run it/.test(part('news')) && !/vc-app-psk/.test(part('news')));
  t('a failed part shows its error inside it only', s[3].cls === 'bad' && s[3].state === 'failed' && /sentiment model offline/.test(part('mood')) && !/sentiment model offline/.test(part('quote'))); }
{ const h = draw(MAN('stack', [{ from: 'mood', title: 'Mood' }, { from: 'quote', title: 'Price' }, { from: 'nope' }]), null); const s = secs(h);
  t('view.parts chooses, orders and names them (an unknown one is left out)', s.map((x) => x.as + ':' + x.title).join() === 'mood:Mood,quote:Price' && /data-layout="stack"/.test(h), JSON.stringify(s)); }
{ const h = draw(MAN('split', [{ from: 'quote' }, { from: 'chart' }]), null);
  t('split lays two parts side by side', /data-layout="split"/.test(h) && secs(h).length === 2); }
{ const h = draw(MAN('tabs'), {}); const s = secs(h);
  t('tabs: a tab per part, the first open, one part drawn', (h.match(/data-app-tab="/g) || []).length === 4 && /class="hx on" data-app-tab="quote" aria-selected="true"/.test(h) && s.length === 1 && s[0].as === 'quote');
  t('...a failed part\'s tab is marked, a reading one shows …', /class="hx bad" data-app-tab="mood"/.test(h) && /data-app-tab="chart"[^>]*>chart …</.test(h));
  const h2 = draw(MAN('tabs'), { tab: 'mood' });
  t('...the tab chosen is the part drawn', secs(h2).length === 1 && secs(h2)[0].as === 'mood'); }
{ const h = draw(MAN('weird'), null); t('an unknown layout falls back to the dashboard', /data-layout="dashboard"/.test(h)); }
t('the tab press is handled (and only on a composite)', S.includes("if (st && st.parts && t.closest('[data-app-tab]')) { ev.stopPropagation(); (st.ui || (st.ui = {})).tab = t.closest('[data-app-tab]').dataset.appTab; return this._appDraw(key); }"));
t('a single-read app is unchanged (no parts, its own path)', /vc-app-sk/.test(V.BLOCK.app({ app: 'x.y' }, 'm', 'app:x', { _apps: { 'app:x': { manifest: { title: 'X', read: { cap: 'a.list' } } } } })));

// the read: all parts at once, each drawn as it lands; a failure stays in its part; what only acts waits unless forced
(async () => {
  const src = S.slice(S.indexOf('    async _appReadParts(key, forced) {'), S.indexOf('    async _appRead(key, forced) {'));
  const readShaped = (cap) => /\.(list|quote|chart|map|status)$/.test(cap);
  const ctx = vm.createContext({ readShaped, Promise, Date, String, setTimeout });
  const M = vm.runInContext('({' + src + '})', ctx);
  const order = [], draws = []; let release = {};
  const parts = [{ as: 'quote', cap: 'markets.quote', args: { s: 1 } }, { as: 'chart', cap: 'markets.chart', args: {} }, { as: 'mood', cap: 'markets.sentiment.map', args: {} }, { as: 'news', cap: 'info.seek', args: {} }];
  const fake = { _apps: { k: { parts } }, _appDraw() { draws.push(parts.map((p) => p.as + ':' + (p.reading ? 'r' : p.error ? 'e' : p.result !== undefined ? 'd' : p.waiting ? 'w' : '-')).join(' ')); },
    callResult(cap, args) { order.push(cap); return new Promise((res) => { release[cap] = res; }); } };
  const run = M._appReadParts.call(fake, 'k', false);
  await new Promise((r) => setTimeout(r, 5));
  t('every readable part is asked at once (not one after another)', order.join() === 'markets.quote,markets.chart,markets.sentiment.map', order.join());
  t('...and the one that only acts is not run, it waits', draws[0] === 'quote:r chart:r mood:r news:w', draws[0]);
  release['markets.chart']({ points: [1, 2] }); await new Promise((r) => setTimeout(r, 5));
  t('the part that answers first is drawn first', draws[draws.length - 1] === 'quote:r chart:d mood:r news:w', draws[draws.length - 1]);
  release['markets.sentiment.map']({ ok: false, error: 'offline' }); release['markets.quote']({ price: 1 }); await run;
  t('a failure stays in its part; the others draw', draws[draws.length - 1] === 'quote:d chart:d mood:e news:w' && parts[2].error === 'offline' && fake._apps.k.error === null, draws[draws.length - 1]);
  t('each part keeps its own time', parts.every((p) => p.as === 'news' || typeof p.ms === 'number'));
  order.length = 0; const run2 = M._appReadParts.call(fake, 'k', true); await new Promise((r) => setTimeout(r, 5));
  t('Refresh (forced) runs every part, the one that acts too', order.join() === 'markets.quote,markets.chart,markets.sentiment.map,info.seek', order.join());
  Object.values(release).forEach((f) => f({})); await run2;
  t('the open: a list of reads becomes parts, args filled, the fastest refresh kept', S.includes("if (Array.isArray(m.read)) {   /* a composite (DA-07a)") && S.includes('st.every = evs.length ? Math.max(10, Math.min(...evs)) : 0; return this._appRead(key); }'));
  t('a read of a composite goes to the parts read', S.includes('{ const s0 = this._apps && this._apps[key]; if (s0 && s0.parts) return this._appReadParts(key, forced); }'));
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
