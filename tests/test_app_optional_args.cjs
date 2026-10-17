// Optional app arguments: a read arg template {arg.x?} may be left empty. Then a single read leaves that argument out of
// the call (not sent as ''), and a composite leaves out the part that names it - the other parts read as usual.
// {arg.x} stays required ("<title> needs x", no read).
//   node tests/test_app_optional_args.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const S = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const cut = (a, b) => { const i = S.indexOf(a), j = S.indexOf(b); if (i < 0 || j < i) throw new Error('slice not found: ' + a); return S.slice(i, j); };
const src = cut('    _appOpen(key, c) {', '    /* a row picked in a composite') + ',\n' + cut('    async _appReadParts(key, forced) {', '    async _appRead(key, forced) {');
const readShaped = (cap) => /\.(list|history)$/.test(cap);
const ctx = vm.createContext({ readShaped, Promise, Date, String, Object, Array, Set, Math, setTimeout });
const M = vm.runInContext('({' + src + '})', ctx);

const BRIEFING = { title: 'Briefing', view: { kind: 'composite', layout: 'dashboard' }, read: [
  { as: 'today', cap: 'cal.events.list', args: { start: 'today' }, refresh: { every_s: 300 } },
  { as: 'inbox', cap: 'mail.inbox.list', args: { limit: 15 } },
  { as: 'telegram', cap: 'tg.history', args: { chat_id: '{arg.chat_id?}', limit: 20 } }] };
const SINGLE = { title: 'Chat', read: { cap: 'tg.history', args: { chat_id: '{arg.chat_id?}', limit: 20 } } };
const NEEDS = { title: 'Chat', read: { cap: 'tg.history', args: { chat_id: '{arg.chat_id}', limit: 20 } } };

async function open(man, args) {
  const calls = [];
  const el = { _appManifest: async () => man, _blockOf: () => null, _appDraw() {}, _appTick() {},
    _appRead(key) { const st = this._apps[key]; return st.parts ? M._appReadParts.call(this, key) : this.callResult(st.cap, st.args); },
    callResult: async (cap, a) => { calls.push({ cap, args: a }); return { rows: [] }; } };
  el._appReadParts = M._appReadParts;
  M._appOpen.call(el, 'k', { app: 'x.y', args });
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 2));
  return { st: el._apps.k, calls };
}

(async () => {
  { const { st, calls } = await open(SINGLE, {});
    t('single read, optional arg not given: it reads', calls.length === 1 && !st.waiting && !st.error, JSON.stringify({ calls, st }));
    t('...and the argument is left out of the call (not sent as \'\')', calls[0] && !('chat_id' in calls[0].args) && calls[0].args.limit === 20, JSON.stringify(calls)); }
  { const { calls } = await open(SINGLE, { chat_id: '' });
    t('single read, optional arg given empty: also left out', calls.length === 1 && !('chat_id' in calls[0].args), JSON.stringify(calls)); }
  { const { calls } = await open(SINGLE, { chat_id: '42' });
    t('single read, optional arg given: sent', calls.length === 1 && calls[0].args.chat_id === '42', JSON.stringify(calls)); }
  { const { st, calls } = await open(NEEDS, {});
    t('a required arg not given still waits ("needs") and does not read', calls.length === 0 && st.waiting === 'Chat needs chat_id', JSON.stringify(st)); }
  { const { st, calls } = await open(BRIEFING, {});
    t('composite, optional arg not given: that part is left out (not read)', st.parts.map((p) => p.as).join() === 'today,inbox' && !calls.some((c) => c.cap === 'tg.history'), JSON.stringify(calls));
    t('...the other parts read as usual', calls.map((c) => c.cap).sort().join() === 'cal.events.list,mail.inbox.list' && !st.waiting && !st.error, JSON.stringify(st));
    t('...and it is not an error', st.parts.every((p) => !p.error)); }
  { const { st, calls } = await open(BRIEFING, { chat_id: '42' });
    const tg = calls.find((c) => c.cap === 'tg.history');
    t('composite, optional arg given: the part reads with it', st.parts.length === 3 && tg && tg.args.chat_id === '42' && tg.args.limit === 20 && calls.length === 3, JSON.stringify(calls)); }
  { const only = { title: 'Only', view: { kind: 'composite' }, read: [{ as: 'tg', cap: 'tg.history', args: { chat_id: '{arg.chat_id?}' } }] };
    const { st, calls } = await open(only, {});
    t('a composite whose every part is left out waits for the argument, not an error', calls.length === 0 && st.waiting === 'Only needs chat_id' && !st.error, JSON.stringify(st)); }
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
