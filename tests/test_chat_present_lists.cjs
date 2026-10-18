// D10 measurement (2026-10-17) found: a declared capability whose declaration names a form hands present() the whole
// answer ({events:[...]}), and _cvPresented's calendar (and records) case only took an array - a 6-event calendar became
// no item. The list is now found in the answer under its usual name.
//   node tests/test_chat_present_lists.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const PRES = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'present.js'), 'utf8');   /* DO-0.1: the record -> item step is shared (VeraWidget.itemOf) */
const a = PRES.indexOf('  function itemOf(capName, result, args, opts){'), b = PRES.indexOf('  Object.assign(W, {', a);
const decl = {}; const W = { presentationOf: (c) => decl[c] || null, present: (cap, res) => ({ element: decl[cap].element, title: cap, records: [{ form: decl[cap].form, data: res }] }),
  dataFor: (d) => d, mapped: (r, f, d) => d };
const ctx = { W, calMonth: () => '2026-10', Object, String, Array, JSON }; vm.createContext(ctx);
vm.runInContext(PRES.slice(a, b) + ';this.f=itemOf;', ctx);
const EV = [1, 2, 3, 4, 5, 6].map((i) => ({ id: 'e' + i, title: 'Event ' + i, start: '2026-10-1' + i + 'T09:00' }));
decl['cal.events.list'] = { element: 'calendar', form: 'schedule' };
{ const it = ctx.f('cal.events.list', { events: EV }, {});
  t('a declared calendar answered as {events:[...]} lands as the calendar with its 6 events', !!it && it.kind === 'calendar' && it.content.events.length === 6, JSON.stringify(it)); }
{ const it = ctx.f('cal.events.list', EV, {}); t('...and as a bare array, as before', !!it && it.content.events.length === 6); }
{ t('...an empty one is still no item', ctx.f('cal.events.list', { events: [] }, {}) === null); }
decl['web.search'] = { element: 'records', form: 'records' };
{ const it = ctx.f('web.search', { ok: true, items: [{ url: 'https://a.example/x', title: 'A' }, { url: 'https://b.example/y', title: 'B' }] }, {});
  t('a declared records answer with its list under items lands as its records', !!it && it.kind === 'records' && it.content.items.length === 2 && it.content.items[1].domain === 'b.example', JSON.stringify(it)); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
