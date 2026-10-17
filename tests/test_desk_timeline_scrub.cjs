// DU-E-timeline: a brush (drag across the axis to pick a date range - the events outside it dim, a line counts them,
// "clear" and a double-click on the axis reset it) and a scrubber (a handle dragged, or stepped by the arrow keys, to a
// date - the nearest event lit and brought into view inside the card). State is the element's (el._tlUi), per item.
//   node tests/test_desk_timeline_scrub.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'timeline:probe';
const EVENTS = [
  { when: '2001-01-15', label: 'Founded in a garage' }, { when: '2003-06-01', label: 'First customer signed' },
  { when: '2005', label: 'Moved to a real office' }, { when: '2008-09-15', label: 'The crash hit sales' },
  { when: '2010-03', label: 'Second product line' }, { when: '2012-11-20', label: 'Opened a European branch' },
  { when: '2015-05-05', label: 'Bought a competitor', url: 'https://example.org/a' }, { when: '2017-02-14', label: 'A new chief executive' },
  { when: '2019-08', label: 'Listed on the exchange' }, { when: '2021-12-01', label: 'Remote work for good' },
];
const pos = (w) => { const s = String(w); const y = +s.slice(0, 4) || 0, m = +s.slice(5, 7) || 0, d = +s.slice(8, 10) || 0; return y + (m ? (m - 1) / 12 : 0) + (d ? (d - 1) / 366 : 0); };
const host = (ui) => { const h = { dataset: {} }; if (ui) h._tlUi = { [KEY]: Object.assign({ b0: null, b1: null, s: null }, ui) }; return h; };
const draw = (el, key) => mod.BLOCK.timeline(JSON.parse(JSON.stringify({ events: EVENTS })), 'l', key === undefined ? KEY : key, el);
const rows = (h) => [...h.matchAll(/<div class="vc-tl-e([^"]*)" data-i="(\d+)">/g)].map((m) => ({ cls: m[1], i: +m[2] }));
const dots = (h) => [...h.matchAll(/<button class="vc-tl-dot([^"]*)"[^>]*data-tl-go="(\d+)"/g)].map((m) => ({ cls: m[1], i: +m[2] }));

{ const h = draw(host());
  t('what it drew before: header count and span, the axis, a dot per event, year ticks, a row per event, the years, the source link',
    h.includes('10 events · 2001–2021') && h.includes('class="vc-tl-ax"') && dots(h).length === 10 && (h.match(/class="vc-tl-tick/g) || []).length >= 2
    && rows(h).length === 10 && h.includes('<div class="vc-tl-y">2001</div>') && h.includes('Founded in a garage') && h.includes('href="https://example.org/a"'));
  t('a gap of years is still said', h.includes('<div class="vc-tl-gap">… 2 years</div>'));
  t('the axis carries its key and scale for the drag', h.includes('data-tl-key="timeline:probe"') && /data-tl-lo="2001\.0[0-9]*"/.test(h) && /data-tl-span="20\.8/.test(h));
  t('the axis says how to brush', h.includes('Drag across the axis to pick a range'));
  t('a scrubber handle, keyboard-reachable, unset', /<button class="vc-tl-sc" data-tl-scrub="timeline:probe" style="left:4\.00%" role="slider"/.test(h) && h.includes('aria-valuetext="not set"'));
  t('no range yet: no band, no count line, nothing dimmed, nothing lit', !h.includes('vc-tl-br') && !h.includes('vc-tl-bn') && !/ out/.test(h) && !/ cur/.test(h));
  t('each dot carries its position', (h.match(/data-x="/g) || []).length === 10); }

{ const a = pos('2005'), b = pos('2013-01-01');
  const h = draw(host({ b0: b, b1: a }));   // dragged right to left: the same range
  const r = rows(h), d = dots(h); const inside = [2, 3, 4, 5];
  t('the band is drawn on the axis', /<div class="vc-tl-br" style="left:[\d.]+%;width:[\d.]+%"><\/div>/.test(h));
  t('the events outside the range are dimmed in the list', r.every((x) => (x.cls.includes('out')) === !inside.includes(x.i)), JSON.stringify(r));
  t('and on the axis', d.every((x) => (x.cls.includes('out')) === !inside.includes(x.i)));
  t('the line counts them and names the range', h.includes('4 of 10 in Jan 2005 – Jan 2013'), (h.match(/<div class="vc-tl-bn"><span>([^<]*)/) || [])[1]);
  t('the clear control, keyed to the item', h.includes('<button class="ib" data-act="tlclear" data-tl-key="timeline:probe"'));
  t('nothing is lost: every row is still drawn', r.length === 10 && h.includes('Remote work for good')); }

{ const h = draw(host({ b0: pos('2030'), b1: pos('2031') }));
  t('a range with no event says 0', h.includes('0 of 10 in')); }

{ const h = draw(host({ s: pos('2012-06-01') }));
  t('scrubbed: the nearest event is lit, in the list and on the axis', rows(h).filter((x) => x.cls.includes('cur')).map((x) => x.i).join() === '5' && dots(h).filter((x) => x.cls.includes('cur')).map((x) => x.i).join() === '5');
  t('the handle stands at the date and says it', /class="vc-tl-sc on"[^>]*aria-valuetext="Jun 2012"/.test(h) && h.includes('nearest: 2012-11-20'));
  const e = draw(host({ s: 1990 }));
  t('a scrub off the end is held at the axis\'s end', /class="vc-tl-sc on"[^>]*style="left:4\.00%"/.test(e) && rows(e).find((x) => x.cls.includes('cur')).i === 0); }

{ const h = draw(host({ b0: pos('2008'), b1: pos('2016'), s: pos('2015-06') }));
  t('brush and scrubber together', h.includes('4 of 10 in') && rows(h).find((x) => x.cls.includes('cur')).i === 6); }

{ const el = host(); draw(el); t('state is the element\'s, per item key (made on first draw)', el._tlUi && el._tlUi[KEY] && el._tlUi[KEY].b0 === null);
  const h = draw(null, ''); t('no key: no brush, no scrubber, the old drawing', !h.includes('data-tl-key') && !h.includes('vc-tl-sc') && dots(h).length === 10);
  const one = mod.BLOCK.timeline({ events: [{ when: '1999', label: 'Only one' }] }, 'l', KEY, host());
  t('one event: no axis, no controls', !one.includes('vc-tl-ax') && one.includes('Only one'));
  t('no events: says so', mod.BLOCK.timeline({ events: [] }, 'l', KEY, host()).includes('No dated events were found.')); }

t('the brush, the scrubber and the double-click are wired', C.includes("t.closest('.vc-tl-ax[data-tl-key]')") && C.includes("body.addEventListener('dblclick', (ev) => { const t = ev.target; const ax = t && t.closest ? t.closest('.vc-tl-ax[data-tl-key]')")
  && C.includes('h.dataset.tlScrub == null') && C.includes("if (act === 'tlclear')"));
t('drawn again only on release, not per move', /const mv = \(e\) => \{[^}]*tlLive(Scrub|Brush)/.test(C) && !/const mv = \(e\) => \{[^\n]*render\(/.test(C));
t('the nearest row is brought into view inside the card (never the desk)', C.includes("box.querySelector('.vc-tl-e.cur');\n          if (row) mdShow(row)"));
t('never written to the server', !/tlUi[^\n]*canvas\.update/.test(C));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
