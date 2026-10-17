// DU-E-calendar: an event carried to another day (its dot in the grid, its row in the list) and written there through the
// diary; a small form on a day to add one. The marks while the diary answers (moving, the error), the form, and the date
// arithmetic of a move: same time of day, the end shifted by the same days, all-day kept, across month / year ends and
// a clock change.
//   node tests/test_desk_calendar_edit.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'calendar:probe';
const EVENTS = [
  { id: 'a', title: 'Rent', start: '2026-09-01', end: '2026-09-02', all_day: true, color: '#9fe1e7' },
  { id: 'b', title: 'Standup', start: '2026-09-15T09:30:00', end: '2026-09-15T09:45:00', color: '#c9955a' },
  { id: 'c', title: 'Review', start: '2026-09-15T14:00:00', location: 'Room 2' },
  { id: 'd', title: 'Dream', start: '2026-09-20T03:00:00', read_only: true },
  { title: 'No id', start: '2026-09-22T10:00:00' },
];
const host = (ui) => { const h = { dataset: {} }; if (ui) h._calUi = { [KEY]: ui }; return h; };
const draw = (c, el) => mod.BLOCK.calendar(Object.assign({ month: '2026-09', events: JSON.parse(JSON.stringify(EVENTS)) }, c || {}), 'l', KEY, el === undefined ? host() : el);
const tag = (h, sel) => (h.match(new RegExp('<[^>]*' + sel + '[^>]*>', 'g')) || []);

// ── what it drew before is still drawn ─────────────────────────────────────────────────────────────────────────────
{ const h = draw();
  t('still a month grid: 30 days, Monday first', (h.match(/data-cal-day="/g) || []).length === 30 && (h.match(/vc-cal-d out/g) || []).length === 1);
  t('still the list, with times and the location', h.includes('Standup') && h.includes('09:30') && h.includes('Room 2') && h.includes('all day'));
  t('no element (null host) still draws', mod.BLOCK.calendar({ month: '2026-09', events: EVENTS }, 'm', KEY, null).includes('September 2026')); }

// ── draggable events ───────────────────────────────────────────────────────────────────────────────────────────────
{ const h = draw();
  const dots = tag(h, 'data-cal-ev=').filter((x) => x.startsWith('<i '));
  t('the grid\'s dots carry their event and item', dots.length === 3 && dots.every((d) => d.includes('data-cal-key="' + KEY + '"')), dots.join(' '));
  const rows = tag(h, 'class="vc-cal-e[^"]*" data-cal-ev=');
  t('the list\'s rows carry their event', rows.length === 3 && rows.some((r) => r.includes('data-cal-ev="b"')), rows.join(' '));
  t('a read-only event (a dream) is not carried', !h.includes('data-cal-ev="d"'));
  t('an event without an id is not carried', (h.match(/data-cal-ev=/g) || []).length === 6);
  t('the dot keeps its colour', /<i data-cal-ev="b"[^>]*background:#c9955a/.test(h)); }

// ── moving, and the error ──────────────────────────────────────────────────────────────────────────────────────────
{ const ui = { pend: { b: { start: '2026-09-17T09:30:00', end: '2026-09-17T09:45:00' } }, err: {} };
  const h = draw({ selected: '2026-09-17' }, host(ui));
  t('a move waiting on the diary is drawn on the new day at once', /data-cal-day="2026-09-17"[^>]*>[\s\S]*?<i data-cal-ev="b"/.test(h.split('data-cal-day="2026-09-18"')[0].split('data-cal-day="2026-09-16"')[1] || ''));
  t('and is gone from the old day', !/data-cal-day="2026-09-15"[^>]*>(?:(?!<\/button>)[\s\S])*data-cal-ev="b"/.test(h));
  t('marked moving', h.includes('vc-cal-e pend') && h.includes('moving…') && /<i data-cal-ev="b"[^>]*class="pend"/.test(h));
  t('the list of the new day has it', h.includes('Standup') && !h.includes('Review')); }
{ const ui = { pend: {}, err: { b: 'the diary is down' } };
  const h = draw({ selected: '2026-09-15' }, host(ui));
  t('a failed move: back on its own day', /data-cal-day="2026-09-15"[^>]*>(?:(?!<\/button>)[\s\S])*data-cal-ev="b"/.test(h));
  t('with the error said', h.includes('not moved: the diary is down') && h.includes('vc-cal-e err') && /<i data-cal-ev="b"[^>]*class="err"/.test(h)); }
{ const h = draw({ selected: '2026-09-15' }, host({ lit: { id: 'c' } }));
  t('the event just written is lit', h.includes('vc-cal-e lit') && /<i data-cal-ev="c"[^>]*class="lit"/.test(h)); }

// ── the add form ───────────────────────────────────────────────────────────────────────────────────────────────────
{ const h = draw({}, host({ add: { day: '2026-09-16', title: 'Lun<ch', time: '12:30', err: '' } }));
  t('the form opens on the day\'s week', h.includes('data-cal-add="' + KEY + '"') && h.includes('data-cal-for="2026-09-16"'));
  // Wednesday 16 Sep 2026: its week ends Sunday the 20th, so the form row follows the 20th and precedes the 21st
  const at = h.indexOf('data-cal-add='); t('under that week (after Sunday the 20th, before the 21st)', at > h.indexOf('data-cal-day="2026-09-20"') && at < h.indexOf('data-cal-day="2026-09-21"'));
  t('the day is marked', /vc-cal-d[^"]*adding[^"]*" data-cal-day="2026-09-16"/.test(h));
  t('title and time boxes keep the draft (escaped)', h.includes('data-cal-in="title"') && h.includes('value="Lun&lt;ch"') && h.includes('data-cal-in="time"') && h.includes('value="12:30"'));
  t('save and cancel', h.includes('data-act="calsave"') && h.includes('data-act="calcancel"'));
  t('the form does not count as a day', (h.match(/data-cal-day="/g) || []).length === 30);
  const e = draw({}, host({ add: { day: '2026-09-30', title: '', time: '', err: 'a title is needed' } }));
  t('on the last day (a short last week) it still shows, with its error', e.includes('data-cal-for="2026-09-30"') && e.includes('a title is needed') && e.indexOf('data-cal-add=') > e.indexOf('data-cal-day="2026-09-30"'));
  t('a form for another month is not drawn here', !draw({}, host({ add: { day: '2026-10-02' } })).includes('data-cal-add='));
  t('the selected day offers "+ add"', draw({ selected: '2026-09-15' }).includes('data-act="caladd" data-cal-key="' + KEY + '" data-cal-for="2026-09-15"') && !draw().includes('data-act="caladd"')); }

// ── the date arithmetic ────────────────────────────────────────────────────────────────────────────────────────────
{ const S = mod.BLOCK.calendar.shift;
  t('the helper is there', typeof S === 'function');
  const m = S({ start: '2026-09-15T09:30:00', end: '2026-09-15T09:45:00' }, '2026-09-17');
  t('same time of day, the end moved by the same days', m && m.start === '2026-09-17T09:30:00' && m.end === '2026-09-17T09:45:00', JSON.stringify(m));
  t('an end on another day keeps its gap', S({ start: '2026-09-15T22:00:00', end: '2026-09-16T01:00:00' }, '2026-09-20').end === '2026-09-21T01:00:00');
  t('no end: none made up', !('end' in S({ start: '2026-09-15T14:00:00' }, '2026-09-03')));
  const ad = S({ start: '2026-09-01', end: '2026-09-02', all_day: true }, '2026-09-10');
  t('all-day kept, date only', ad.start === '2026-09-10' && ad.end === '2026-09-11' && ad.all_day === true, JSON.stringify(ad));
  t('across a month end', S({ start: '2026-01-30T08:00:00', end: '2026-02-01T08:00:00' }, '2026-02-27').end === '2026-03-01T08:00:00');
  t('across a year end', S({ start: '2026-12-31T23:00:00', end: '2027-01-01T01:00:00' }, '2027-01-02').end === '2027-01-03T01:00:00');
  t('backwards across a year', S({ start: '2027-01-02T10:00:00', end: '2027-01-02T11:00:00' }, '2026-12-30').start === '2026-12-30T10:00:00');
  t('a leap day', S({ start: '2028-02-28', end: '2028-03-01', all_day: true }, '2028-02-29').end === '2028-03-02');
  // the UK clock changes on 29 Mar and 25 Oct 2026: a local-time day count would be 23 / 25 hours off
  const prev = process.env.TZ; process.env.TZ = 'Europe/London';
  t('across the spring clock change: same wall time', S({ start: '2026-03-28T09:00:00', end: '2026-03-30T09:00:00' }, '2026-03-29').end === '2026-03-31T09:00:00');
  t('across the autumn one', S({ start: '2026-10-24T00:30:00Z', end: '2026-10-24T01:30:00Z' }, '2026-10-26').start === '2026-10-26T00:30:00Z');
  if (prev == null) delete process.env.TZ; else process.env.TZ = prev;
  t('a zone suffix rides along', S({ start: '2026-09-15T09:30:00+01:00' }, '2026-09-16').start === '2026-09-16T09:30:00+01:00');
  t('the same day: nothing', S({ start: '2026-09-15T09:30:00' }, '2026-09-15') === null);
  t('an unreadable date: nothing', S({ start: 'soon' }, '2026-09-15') === null && S({ start: '2026-09-15' }, '15/09') === null); }
{ const T = mod.BLOCK.calendar.time, A = mod.BLOCK.calendar.addArgs;
  t('times read', T('') === '' && T('9') === '09:00' && T('9:30') === '09:30' && T('2pm') === '14:00' && T('12am') === '00:00' && T('21.15') === '21:15');
  t('and nonsense does not', T('25:00') === null && T('lunch') === null && T('13pm') === null);
  t('add with a time', JSON.stringify(A('2026-09-16', ' Lunch ', '12:30')) === JSON.stringify({ title: 'Lunch', start: '2026-09-16T12:30:00' }));
  t('add with no time: all day', JSON.stringify(A('2026-09-16', 'Lunch', '')) === JSON.stringify({ title: 'Lunch', start: '2026-09-16', all_day: true }));
  t('no title, a bad time: nothing to write', A('2026-09-16', '  ', '') === null && A('2026-09-16', 'x', 'later') === null); }

// ── wired: only cal.event.upsert writes; the drag is not a card drag; the re-read is the month's own ──────────────
t('a move writes through cal.event.upsert with id, title, start, end, all day, location', C.includes("this.callResult('cal.event.upsert', args)") && /const args = \{ id: e\.id, title: String\(e\.title \|\| ''\), start: nx\.start, all_day:/.test(C) && C.includes('if (nx.end) args.end = nx.end;') && C.includes('if (e.location) args.location = e.location;'));
t('a failed write puts it back with the error', C.includes("if (bad) { delete ui.pend[id]; ui.err[id] = "));
t('the month is read again through _calAct (cal.events.list)', C.includes("return this._calAct(key, '0', (events, month, r) => {") && C.includes("if (typeof then === 'function') return then(events, month, r);"));
t('the add form writes through the same cap', C.includes("const r = await this.callResult('cal.event.upsert', args);") && C.includes('const args = calAddArgs(a.day, a.title, a.time);'));
t('the press on an event stops there (never a card drag)', /const h = t\.closest\('\[data-cal-ev\]\[data-cal-key\]'\); if \(!h \|\| !body\.contains\(h\)\) return;\s+ev\.preventDefault\(\); ev\.stopPropagation\(\);/.test(C));
t('the release is not a day press', C.includes('this._dgEnd = Date.now();   /* the click this release makes is not a day press */'));
t('Enter saves, Esc cancels', C.includes("if (ev.key === 'Escape') this._calAddClose(k); else this._calAddSave(k);"));
t('the actions', C.includes("if (act === 'caladd' || act === 'calsave' || act === 'calcancel')"));

// ── a day press: drawn at once, its write held a beat; a double-click drops it (no select/unselect writes) ─────────
t('a day press goes through the held write', C.includes('this._calDayPress(cd.dataset.calKey, cd.dataset.calDay); return; }') && !C.includes('this._calAct(cd.dataset.calKey, cd.dataset.calDay)'));
t('a double-click drops the held write before the form opens', C.includes('this._calDayDrop(d.dataset.calKey); this._calAddOpen(d.dataset.calKey, d.dataset.calDay); });'));
t('held about a double-click interval', /const CAL_DBL_MS = (2[5-9]\d|300);/.test(C));
(async () => {
  const a = C.indexOf('    _calDayPress(key, day) {'), z = C.indexOf('    /* DU-E-calendar. The month read again');
  t('the press helpers are found', a > 0 && z > a);
  const Cls = new Function('CAL_DBL_MS', 'return class { ' + C.slice(a, z) + ' }')(40);
  const mk = (sel) => { const o = new Cls(); o.block = { key: KEY, content: { month: '2026-09', selected: sel, events: [] } }; o.writes = []; o.draws = 0;
    o._doc = {}; o._blockOf = () => o.block; o.render = () => { o.draws++; }; o.call = (cap, args) => { o.writes.push([cap, args]); }; return o; };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  { const o = mk(''); o._calDayPress(KEY, '2026-09-16');
    t('a single press: the day is drawn selected at once', o.block.content.selected === '2026-09-16' && o.draws === 1);
    t('and nothing is written yet', o.writes.length === 0);
    await wait(80);
    t('a beat later: exactly one canvas.update, with the day', o.writes.length === 1 && o.writes[0][0] === 'canvas.update' && o.writes[0][1].key === KEY && o.writes[0][1].content.selected === '2026-09-16', JSON.stringify(o.writes)); }
  { const o = mk('2026-09-03'); o._calDayPress(KEY, '2026-09-16'); o._calDayPress(KEY, '2026-09-16'); o._calDayDrop(KEY);
    t('a double-click: the selection is as it was before', o.block.content.selected === '2026-09-03');
    await wait(80);
    t('and nothing is written', o.writes.length === 0, JSON.stringify(o.writes)); }
  { const o = mk(''); o._calDayPress(KEY, '2026-09-16'); o._calDayPress(KEY, '2026-09-16');
    await wait(80);
    t('pressed and pressed back inside one hold: nothing written', o.writes.length === 0 && o.block.content.selected === ''); }
  { const o = mk(''); o._calDayPress(KEY, '2026-09-16'); o._calDayPress(KEY, '2026-09-17');
    await wait(80);
    t('two days pressed quickly: one write, the last day', o.writes.length === 1 && o.writes[0][1].content.selected === '2026-09-17'); }
  { const o = mk(''); o._calDayPress(KEY, '2026-09-16'); o.block = { key: KEY, content: { month: '2026-09', selected: '', events: [{ id: 'n' }] } };
    await wait(80);
    t('a refresh in the hold: the write is the new content with the day', o.writes.length === 1 && o.writes[0][1].content.selected === '2026-09-16' && o.writes[0][1].content.events.length === 1); }
  { const o = mk(''); o._calDayDrop(KEY); t('a drop with nothing held: no change, no write', o.writes.length === 0 && o.block.content.selected === ''); }
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
