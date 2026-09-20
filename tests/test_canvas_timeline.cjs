// A timeline out of what a research run found (vera/chat/chat_panel.html, vera/canvas).
//
// THE PLANNED FOUNDATION DOES NOT EXIST. The idea was to lean on the research system's NLP. Measured on prod and
// on the mirror, nlp.ner answers {"error":"optimum/transformers not installed"} - and the model it would load if
// it were installed is dslim/bert-base-NER, a CoNLL-2003 tagger whose labels are PER, ORG, LOC and MISC. There is
// no DATE among them, so it could not have supplied the dates a timeline is made of even had it been working;
// and it truncates its input at 1024 characters, which is one paragraph of a report that runs to thousands.
//
// Dates are one of the few things where a matcher genuinely beats a general tagger: they are written in a handful
// of shapes and they carry their own precision. So the dates come from the text and each event is the sentence
// its date sits in.
//
// The property that matters most here is RESTRAINT. A number is not a date. A timeline that puts "version 2019"
// or "8080" or "£1976" on an axis is worse than no timeline, because it looks authoritative. So a bare year is
// only read as one where the prose is using it as a date - "in 1984", "by 2020", "(1976)" - and everything else
// is left alone. Those are the assertions worth having.
//
//   node tests/test_canvas_timeline.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const A = "const _TL_MON=", B = "  const _cvCapTerm=(c)=>{";
const i0 = src.indexOf(A), i1 = src.indexOf(B);
t('the extractor is present in the page', i0 >= 0 && i1 > i0);
if (i0 < 0 || i1 < i0) { console.log(fails + ' FAILED'); process.exit(1); }
const ctx = {}; vm.createContext(ctx);
vm.runInContext(src.slice(i0, i1) + '\nthis.D=_tlDates; this.E=_tlEvents;', ctx);
const { D, E } = ctx;

// ── the shapes a date is written in ────────────────────────────────────────────────────────────────────────────
{
  const w = (s) => D(s).map((x) => x.when).join(',');
  t('ISO', w('Released on 2007-01-09 to the public.') === '2007-01-09');
  t('day month year', w('Founded on 1 April 1976 in a garage.') === '1976-04-01');
  t('abbreviated month', w('Shipped 9 Jan 2007 at last.') === '2007-01-09');
  t('month day, year', w('Announced January 9, 2007 in San Francisco.') === '2007-01-09');
  t('month and year only, and it stays that precise',
    w('In April 1976 the company began.') === '1976-04', 'got ' + w('In April 1976 the company began.'));
  t('a bare year in use as one', w('In 1984 it launched the Macintosh.') === '1984');
  t('a year in parentheses', w('The Macintosh (1984) changed things.') === '1984');
  t('by / since / until', w('By 2020 it had grown.') === '2020' && w('Since 1976 it has shipped.') === '1976');
}

// ── restraint: a number is not a date ──────────────────────────────────────────────────────────────────────────
{
  const none = (s) => D(s).length === 0;
  t('a port number is not a year', none('The service listens on 8080 for requests.'));
  t('a version is not a year', none('Upgrade to version 2019 of the toolchain.'));
  t('a bare count is not a year', none('There were 1984 open issues at the time.'));
  t('a price is not a year', none('It cost 1976 pounds to build.'));
  t('an id is not a year', none('See ticket 2007 for the details.'));
  // and the one that would look most convincing on an axis while being nonsense
  t('a measurement is not a year', none('The array holds 2048 entries.'));
}

// ── the most precise reading of a year wins ────────────────────────────────────────────────────────────────────
{
  const r = D('Apple was founded in April 1976, and by 1976 it had its first customer.');
  t('a year is not logged twice at two precisions', r.length === 1, JSON.stringify(r.map((x) => x.when)));
  t('and the precise reading is the one kept', r[0].when === '1976-04', r[0] && r[0].when);
}

// ── events out of real prose, in order ─────────────────────────────────────────────────────────────────────────
{
  const report = [
    '# Apple Inc.',
    '',
    'Apple Inc. was founded on April 1, 1976 by Steve Jobs, Steve Wozniak and Ronald Wayne in Cupertino.',
    'In 1984 the company launched the Macintosh, which introduced the graphical interface to a wide audience.',
    '* The iPhone was announced on January 9, 2007 and went on sale that June.',
    'Tim Cook became chief executive in August 2011 after Jobs stepped down.',
    'The company runs its services on port 8080 internally, which is not a date at all.',
    'By 2018 it had reached a market capitalisation of one trillion dollars.',
  ].join('\n');
  const ev = E(report, { url: 'https://example.com/apple' });
  t('a report yields its dated events', ev.length === 5, 'got ' + ev.length + ': ' + ev.map((e) => e.when).join(','));
  t('in date order', ev.map((e) => e.when).join(',') === '1976-04-01,1984,2007-01-09,2011-08,2018',
    ev.map((e) => e.when).join(','));
  t('the event is the sentence its date sits in', /founded on April 1, 1976/.test(ev[0].label));
  t('the port number produced nothing', !ev.some((e) => /8080/.test(e.label)));
  t('markdown markers are not part of the event', !/^[*#>-]/.test(ev[2].label), ev[2] && ev[2].label.slice(0, 20));
  t('each event remembers where it came from', ev.every((e) => e.url === 'https://example.com/apple'));
  t('nothing is invented for a year-only event', ev.find((e) => e.when === '1984').when === '1984');
}

// ── the guards around the whole thing ──────────────────────────────────────────────────────────────────────────
{
  t('empty text yields nothing', E('').length === 0 && E(null).length === 0);
  t('undated prose yields nothing', E('This document has no dates in it whatsoever, only words.').length === 0);
  t('code fences are ignored', E('```\nconst YEAR = 1984;\n```').length === 0);
  const many = Array.from({ length: 80 }, (_, i) => 'In ' + (1900 + i) + ' something happened here.').join('\n');
  t('a long history is capped', E(many).length === 40, String(E(many).length));
  const dupes = 'In 1999 the thing happened.\nIn 1999 the thing happened.';
  t('the same event is not listed twice', E(dupes).length === 1);
}

// ── and the canvas can draw one ────────────────────────────────────────────────────────────────────────────────
{
  const cv = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
  t('the canvas draws a timeline', /^    timeline: \(c\) => \{/m.test(cv));
  const py = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_capabilities.py'), 'utf8');
  t('and the block type is declared server-side', /"timeline": \{"desc"/.test(py));
  t('the type says why `when` is loose', /did\s*\n?\s*"?\s*not have|not have\./.test(py) || /precision the prose did/.test(py));
}

// ---- and it lands when a run finishes, but only when there is a history to draw ------------------------------
{
  t('a finished research report is offered to the extractor', /try\{ _cvLandTimeline\(result, _cvRelMid\|\|''\); \}catch/.test(src));
  /* a report about a library's API has no dates in it and should get no axis - an empty item saying "no events"
     is worse than no item, because it implies the run looked and found nothing worth showing */
  t('two dates are a pair, not a history', /if\(events\.length<3\) return 0;/.test(src));
  t('keyed by the turn, so asking again does not overwrite the answer you have', /const key='timeline:'\+\(mid\|\|Date\.now\(\)\)/.test(src));
  t('and it is titled with the question that produced it', /subject\?\('Timeline/.test(src));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
