// The calendar on the canvas (vera/canvas/canvas_element.js, vera/chat/chat_panel.html).
//
// "Add something for the calendar system so that created events display in the canvas, a full mini calendar
// widget would be good too - linked to Vera's calendar system."
//
// Linked, not copied. A calendar whose contents were fixed when it landed is wrong by the next event written,
// and being current is the whole of what a calendar is for - so the month buttons and a day press go back to
// cal.events.list and the answer is written into the item. The diary is the source; the item is a view of it.
//
// The grid is the part with arithmetic in it, so the grid is what this executes: the week starts Monday, the
// lead-in blanks are right for the month, February in a leap year has 29 days, and a day with events is
// distinguishable from one without - a month where a busy day and a quiet day look alike is a decoration.
//
//   node tests/test_canvas_calendar.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const cvSrc = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const chat = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// load the canvas module and take its BLOCK table - the renderer the page actually ships
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
t('the canvas module loads and exposes its blocks', !!(mod && mod.BLOCK && typeof mod.BLOCK.calendar === 'function'));
if (!mod || !mod.BLOCK || !mod.BLOCK.calendar) { console.log(fails + ' FAILED'); process.exit(1); }
const draw = (c) => mod.BLOCK.calendar(c, 'm', 'calendar:x', null);

// the shape cal.events.list actually answers with, taken from a live call
const EVENTS = [
  { id: 'a', title: 'Rent Payment', start: '2026-09-01', end: '2026-09-02', all_day: true, location: '', color: '#9fe1e7' },
  { id: 'b', title: 'Standup', start: '2026-09-15T09:30:00', end: '2026-09-15T09:45:00', color: '#c9955a' },
  { id: 'c', title: 'Review', start: '2026-09-15T14:00:00', location: 'Room 2', color: '#8fb87a' },
  { id: 'd', title: 'Dentist', start: '2026-09-30T11:00:00', color: '#c96b6b' },
];

// ── the grid ───────────────────────────────────────────────────────────────────────────────────────────────────
{
  const h = draw({ month: '2026-09', events: EVENTS });
  const cells = (h.match(/class="vc-cal-d[^"]*"/g) || []);
  const real = (h.match(/data-cal-day="/g) || []).length;
  t('September 2026 has 30 days', real === 30, String(real));
  /* 1 September 2026 is a Tuesday, so one blank leads the week in. A Sunday-first grid would put two there,
     and every date in the month would sit under the wrong weekday. */
  t('the week starts Monday, so one blank leads it in', (h.match(/vc-cal-d out/g) || []).length === 1,
    String((h.match(/vc-cal-d out/g) || []).length));
  t('the first cell of the month is the 1st', /data-cal-day="2026-09-01"/.test(h));
  t('and the last is the 30th', /data-cal-day="2026-09-30"/.test(h) && !/data-cal-day="2026-09-31"/.test(h));
  t('the month is named in full', /September 2026/.test(h));
  t('every day can be pressed and carries the item it belongs to', cells.length >= 30 && /data-cal-key="calendar:x"/.test(h));
}
{
  // a leap February, because that is the month a hand-rolled grid gets wrong
  const h = draw({ month: '2028-02', events: [] });
  t('February 2028 has 29 days', (h.match(/data-cal-day="/g) || []).length === 29,
    String((h.match(/data-cal-day="/g) || []).length));
  t('and 2027 has 28', (draw({ month: '2027-02', events: [] }).match(/data-cal-day="/g) || []).length === 28);
}

// ── a day with something on looks different from one without ───────────────────────────────────────────────────
{
  const h = draw({ month: '2026-09', events: EVENTS });
  t('a day with events is marked', /data-cal-day="2026-09-15"[^>]*/.test(h) && /vc-cal-d[^"]*has[^"]*" data-cal-day="2026-09-15"/.test(h));
  t('an empty day is not', !/vc-cal-d[^"]*has[^"]*" data-cal-day="2026-09-02"/.test(h));
  t('each event puts its own colour on the day', /background:#c9955a/.test(h) && /background:#8fb87a/.test(h));
  t('today is marked as today', /vc-cal-d[^"]*today/.test(draw({ month: new Date().toISOString().slice(0, 7), events: [] })));
}

// ── the day list, which is the question a calendar is asked ────────────────────────────────────────────────────
{
  const h = draw({ month: '2026-09', events: EVENTS, selected: '2026-09-15' });
  t('a selected day lists its events', /Standup/.test(h) && /Review/.test(h));
  t('and only its events', !/Dentist/.test(h) && !/Rent Payment/.test(h));
  t('with the time, not the raw stamp', /09:30/.test(h) && !/09:30:00/.test(h));
  t('an all-day event says so', /all day/.test(draw({ month: '2026-09', events: EVENTS, selected: '2026-09-01' })));
  t('a location rides along', /Room 2/.test(h));
  t('a day with nothing on says so', /Nothing on this day/.test(draw({ month: '2026-09', events: EVENTS, selected: '2026-09-02' })));
  t('and with no day picked the month leads', /Rent Payment/.test(draw({ month: '2026-09', events: EVENTS })));
  t('an empty month says that instead', /Nothing in this month/.test(draw({ month: '2026-09', events: [] })));
}

// ── it is linked to the diary, not a copy of it ────────────────────────────────────────────────────────────────
{
  t('the month buttons go back to the diary', /callResult\('cal\.events\.list', \{ start: from, end: to \}\)/.test(cvSrc));
  /* `end` is EXCLUSIVE at midnight. Asking to the last day of the month drops that whole day - measured against
     the real cap: four events seeded into September, a 01..30 range answered with three, and the one on the 30th
     was the one missing. An event written on the last of the month landing a calendar that does not show it is
     the single case this has to get right. */
  t('the range runs to the first of the NEXT month, not the last of this one',
    /const to = nxt\.getFullYear\(\) \+ '-' \+ pad\(nxt\.getMonth\(\) \+ 1\) \+ '-01';/.test(cvSrc)
    && !/const to = month \+ '-' \+ pad\(new Date/.test(cvSrc));
  t('and what comes back is filtered to the month asked for',
    /\.filter\(\(e\) => String\(e && e\.start \|\| ''\)\.slice\(0, 7\) === month\)/.test(cvSrc));
  t('the chat side asks the same way', /const nxt=new Date\(yy,mm,1\); const to=nxt\.getFullYear\(\)/.test(chat)
    && /\.filter\(e=>String\(\(e&&e\.start\)\|\|''\)\.slice\(0,7\)===month\)/.test(chat));
  t('and the answer is written into the item, so it survives a reload',
    /canvas\.update', \{ key, content: Object\.assign\(\{\}, c, \{ month, selected: '', events \}\) \}/.test(cvSrc));
  t('a day press is local - the month is already here', /selected: c\.selected === day \? '' : day/.test(cvSrc));
  t('the buttons reach the handler', /\[data-cal-mv\]/.test(cvSrc) && /\[data-cal-day\]/.test(cvSrc));
  t('and the block type says the diary backs it', /"calendar": \{"desc"/.test(
    fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8')));
}

// ── and a created event shows up ───────────────────────────────────────────────────────────────────────────────
{
  t('a list of dated titled things is a month, not a table',
    /cvAdapter\('calendar',/.test(chat) && /e\[0\]\.start!=null&&e\[0\]\.title!=null/.test(chat));
  t('tried before the table, or the diary is drawn as a grid of cells',
    chat.indexOf("cvAdapter('calendar'") < chat.indexOf("cvAdapter('table'"));
  /* cal.event.upsert answers with the one event it wrote, which on its own is a receipt - the useful thing is
     the month it landed in, re-read from the diary so it shows everything else that month holds too */
  t('a written event lands the month it went into', /_cvLandCalendar\(cres\.event, _cvRelMid\|\|''\)/.test(chat));
  t('keyed by the month, so three events build one calendar', /key:'calendar:'\+month/.test(chat));
  t('and the day it was written on is the day shown', /selected:String\(start\)\.slice\(0,10\)/.test(chat));
  t('a month grid is not put in a small item', /if\(k==='calendar'\) return 'm';/.test(chat));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
