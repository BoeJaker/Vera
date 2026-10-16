// DA-06b: apps reach the desk from the chat LHM's Canvas menu - the catalogue (ui.apps.list) by panel, recently opened
// first, a search, an app that needs an argument asks for it in its row; the canvas opens one item per app + arguments.
//   node tests/test_apps_add_menu.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const R = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const C = fs.readFileSync(path.join(R, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the menu's helpers, cut out of the page and run on a catalogue like ui.apps.list's
const a0 = H.indexOf('  let _CV_APPS=null, _cvAppsQ=\'\';'), a1 = H.indexOf('  function _cvMenuRender(onlyIfOpen){');
t('the helpers are in the page, before the menu', a0 > 0 && a1 > a0);
const store = {};
const ctx = { esc: (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])), localStorage: { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; } },
  document: { addEventListener() {} }, window: { addEventListener() {} }, location: { origin: 'https://vera.local' }, _capCall: async () => ({}), _cvMenuRender() {}, Promise, JSON, console };
vm.createContext(ctx); vm.runInContext(H.slice(a0, a1) + '\n;this.__set=(l)=>{_CV_APPS=l;};this.__q=(q)=>{_cvAppsQ=q;};this.list=_cvAppsList;this.sec=_cvAppsSec;this.recent=_cvAppRecent;this.args=_cvAppArgs;', ctx);
const CAT = [{ id: 'calendar.agenda', panel: 'calendar-panel', title: 'Agenda', summary: 'What is on', needs_args: [] }, { id: 'markets.watchlist', panel: 'markets', title: 'Watchlist', summary: 'The assets kept up to date', needs_args: [] },
  { id: 'markets.symbol', panel: 'markets', title: 'Symbol', summary: "One symbol's candles", needs_args: ['symbol'] }, { id: 'estate.nodes', panel: 'estate', title: 'Compute nodes', summary: 'Every compute node', needs_args: [] }];
ctx.__set(CAT);
{ const h = ctx.sec();
  t('the section: a search and every app', h.includes('class="cvm-sec cvm-apps"') && h.includes('class="cvm-app-q"') && (h.match(/data-cvm="app"/g) || []).length === 4);
  t('grouped by panel', /cvm-app-g">calendar<[\s\S]*cvm-app-g">estate<[\s\S]*cvm-app-g">markets</.test(h));
  t('an app that needs an argument has a box for it', h.includes('<input class="cvm-app-arg" data-arg="symbol" placeholder="symbol"')); }
{ ctx.__q('watch'); const h = ctx.list(); t('a search narrows it', (h.match(/data-cvm="app"/g) || []).length === 1 && h.includes('data-app="markets.watchlist"'));
  ctx.__q('zzz'); t('nothing found says so', ctx.list().includes('No app has')); ctx.__q(''); }
{ ctx.recent('estate.nodes'); ctx.recent('markets.watchlist'); const h = ctx.list();
  t('recently opened first, newest first, not listed twice', /cvm-app-g">recent<\/div><div class="cvm-app" data-app-row="markets.watchlist">[\s\S]*data-app-row="estate.nodes"[\s\S]*cvm-app-g">calendar/.test(h) && (h.match(/data-app-row="markets.watchlist"/g) || []).length === 1); }
{ ctx.__set(null); t('before the catalogue is read: it says so', ctx.list().includes('Reading the catalogue')); ctx.__set([]); t('an empty catalogue says so', ctx.list().includes('No apps in the catalogue')); }
t('the act opens the app through the canvas, from the menu page too (cvact)', H.includes("if(a.a==='app'){ if(!a.app||typeof c.openApp!=='function') return Promise.resolve(); _cvAppRecent(a.app); return Promise.resolve(c.openApp(a.app, a.args||{})); }")
  && H.includes("if(a==='app'){ const args=_cvAppArgs(b); if(args===null) return; _cvMenuAct({ a, app:b.dataset.app||'', args })"));
t('a missing argument is asked for, not sent', H.includes("if(miss){ miss.classList.add('need'); miss.focus(); return null; } return args; }"));
t('the canvas: one item per app and arguments', C.includes("return this.call('canvas.add', { kind: 'app', key: 'app:' + id + (sig ? ':' + sig : ''), content: { app: id, args: a } });"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
