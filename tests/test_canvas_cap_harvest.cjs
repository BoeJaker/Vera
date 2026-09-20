// What a capability RESULT becomes on the session canvas (vera/chat/chat_panel.html).
//
// Every cap card landed on the canvas as `markdown` holding a fenced dump of its own result text, cut at 2000
// characters. A command's output, a list of docker hosts and a set of numbers all arrived as the same wall of
// text: nothing sortable, nothing chartable, nothing you could explore. That is the "it's not always going to
// have things it can represent" problem from the other end - it always had something, and always threw it away.
//
// Two things were wrong and only one of them was the drawing:
//
//   1. THE RESULT WAS NOT KEPT. executeCapInline turns `content` into pixels and drops it; the only trace left
//      in the DOM is the serialised text inside .cap-result, already truncated for display. So the harvest had
//      nothing to work from but that text. It is now kept on the card as el.__cap.
//   2. THE CHAT ALREADY KNEW BETTER. _widgetFormByShape picks a form for a result's shape, and the transcript
//      draws research reports and image galleries properly. The canvas simply never asked. These recognisers
//      reuse that rule rather than growing a second one beside it.
//
// A result that matches nothing still falls through to markdown - that is the honest representation of a thing
// with no shape, and the point of the fallback is that it is REACHED, not that it is never reached.
//
// This executes the recognisers the page actually ships, cut out of chat_panel.html by their markers.
//   node tests/test_canvas_cap_harvest.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ── the result is kept on the card in the first place ──────────────────────────────────────────────────────────
t('executeCapInline keeps the result on the card',
  /try\{ el\.__cap=\{ name:capName, args:coercedArgs, content, err:isErr \}; \}catch\(_\)\{\}/.test(src));
t('and the harvest reads it rather than the rendered text',
  /const cap = el\.__cap \|\| null, cres = cap && !cap\.err \? cap\.content : null;/.test(src));
t('a terminal lands big enough to read', /if\(k==='session'\) return 'm';/.test(src));

// ── cut the three recognisers out and run them ─────────────────────────────────────────────────────────────────
const A = "const _cvCapTerm=(c)=>{", B = "  function _cvHarvest(body){";
const i0 = src.indexOf(A), i1 = src.indexOf(B);
t('the recognisers are present in the page', i0 >= 0 && i1 > i0);
if (i0 < 0 || i1 < i0) { console.log(fails + ' FAILED'); process.exit(1); }
const block = src.slice(i0, i1);

// the two helpers they lean on are the chat's own shape rule; the real ones are further up the same file, so the
// test gives them the same contract rather than a different one
const ctx = {
  _widgetData: (x, form) => {
    if (Array.isArray(x)) return x;
    if (x && typeof x === 'object') {
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'values', 'entries', 'results']) {
        if (Array.isArray(x[k])) return x[k];
      }
    }
    return x;
  },
  _widgetFormByShape: (x) => {
    if (Array.isArray(x) && x.length) {
      const o = x[0];
      if (o && typeof o === 'object') {
        if (('t' in o || 'ts' in o) && ('v' in o || 'value' in o)) return 'trace';
        return 'table';
      }
      if (x.every((v) => typeof v === 'number')) return 'trace';
    }
    if (x && typeof x === 'object') {
      if (typeof x.value === 'number' && ('min' in x || 'max' in x)) return 'radial';
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) {
        if (x[k] != null) return ctx._widgetFormByShape(x[k]);
      }
    }
    return null;
  },
  _widgetRecord: (o) => ({ name: o.name, form: o.form, draw: { form: o.form, size: 'm' }, reads: { cap: o.source } }),
};
vm.createContext(ctx);
vm.runInContext(block + '\nthis.T=_cvCapTerm; this.B=_cvCapTable; this.W=_cvCapWidget;', ctx);
const { T, B: TBL, W } = ctx;

// ── a command and its output is a terminal ─────────────────────────────────────────────────────────────────────
{
  const r = T({ ok: true, rc: 0, command: 'docker ps --format "{{.Names}}"', stdout: 'vera-dev\nvera-redis\n', stderr: '', elapsed_ms: 42 });
  t('exec output becomes a terminal card, not prose', !!r && r.kind === 'session', JSON.stringify(r && r.kind));
  t('it carries the command and the output', !!r && r.content.command.startsWith('docker ps') && /vera-redis/.test(r.content.output));
  t('and says how it went', !!r && / rc 0/.test(r.d) && /42ms/.test(r.d), r && r.d);
  const err = T({ ok: false, rc: 1, command: 'ls /nope', stdout: '', stderr: 'No such file or directory' });
  t('a failing command keeps its stderr', !!err && /No such file/.test(err.content.output) && /rc 1/.test(err.d));
  t('something with no command is not a terminal', T({ ok: true, hosts: [] }) === null && T(null) === null);
  t('a command with no output at all is not a terminal card', T({ command: 'true', rc: 0 }) === null);
}

// ── rows of the same shape are a table ─────────────────────────────────────────────────────────────────────────
{
  const hosts = [{ name: 'gpu-250', cpu: 12, mem: '64G' }, { name: 'cpu-246', cpu: 40, mem: '32G' }];
  const r = TBL({ ok: true, hosts }, 'docker.hosts');
  t('a list of records becomes a table', !!r && r.kind === 'table', JSON.stringify(r && r.kind));
  t('with real columns, not a blob', !!r && r.content.columns.join(',') === 'name,cpu,mem', r && r.content.columns.join(','));
  t('and its rows in order', !!r && r.content.rows[0][0] === 'gpu-250' && r.content.rows[1][1] === '40');
  t('the row count is in the label', !!r && /2 rows/.test(r.d));
  // the guards: one column is a list, not a table; and nested objects are not cells
  t('a single-column list is not forced into a table', TBL({ items: [{ name: 'a' }, { name: 'b' }] }, 'x') === null);
  /* the envelope key is not in _widgetData's fixed list, and results are named for what they hold far more
     often than they are named `data` - this is the case that was falling through to the JSON dump */
  t('rows are found whatever the envelope calls them', !!TBL({ ok: true, containers: [{ id: 'a', state: 'up' }] }, 'x'));
  t('and the authored key still wins when there is one',
    TBL({ data: [{ a: 1, b: 2 }], other: [{ z: 9, y: 8 }] }, 'x').content.columns.join(',') === 'a,b');
  t('an empty result is not a table', TBL({ ok: true, hosts: [] }, 'x') === null && TBL(null, 'x') === null);
  // 80 rows is the cap, so a huge result cannot blow the canvas item up
  const many = Array.from({ length: 300 }, (_, i) => ({ id: i, v: i * 2 }));
  t('a huge result is capped', TBL({ rows: many }, 'x').content.rows.length === 80);
}

// ── a shape the widget rule recognises is a chart ──────────────────────────────────────────────────────────────
{
  const r = W({ value: 62, min: 0, max: 100 }, 'gpu.load', {});
  t('a gauge-shaped result becomes a widget', !!r && r.kind === 'widget' && r.content.form === 'radial', JSON.stringify(r && r.content && r.content.form));
  t('it carries its data', !!r && r.content.data && r.content.data.value === 62);
  t('and names the cap it came from', !!r && r.n === 'gpu.load');
  const series = W({ points: [{ t: 1, v: 3 }, { t: 2, v: 5 }] }, 'perf.trace', {});
  t('a time series becomes a trace', !!series && series.content.form === 'trace');
  /* a table-shaped result must NOT come back as a widget: the table recogniser above it builds real columns,
     and a widget would have swallowed it into a form with none */
  t('a table shape is left to the table recogniser', W({ hosts: [{ name: 'a', cpu: 1 }] }, 'x', {}) === null);
  t('a shapeless result is not a widget', W({ ok: true, note: 'done' }, 'x', {}) === null && W(null, 'x', {}) === null);
}

// ── and the fallback is still reachable, which is the point of it ──────────────────────────────────────────────
{
  const shapeless = { ok: true, note: 'nothing to draw here' };
  t('a result with no shape matches no recogniser', T(shapeless) === null && TBL(shapeless, 'x') === null && W(shapeless, 'x', {}) === null);
  t('so the harvest still falls through to markdown', /out\.push\(\{kind:'markdown', n:n\.split\(' '\)\[0\]/.test(src));
  t('and a card from a restored session, with no kept result, falls through too',
    /const cap = el\.__cap \|\| null/.test(src) && /if\(made\)\{ out\.push\(made\); return; \}/.test(src));
  t('a FAILED cap is never drawn as data', /cap && !cap\.err \? cap\.content : null/.test(src));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
