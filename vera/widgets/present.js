/* vera/widgets/present.js — VeraWidget.present(): ONE reading of a capability's answer for the desk (DA-02a,
   DESK-SPINE-DESIGN.md §2). Loaded after widget_element.js (its route appends this file), it adds to window.VeraWidget:

     VeraWidget.present(cap, result, opts?) → { element, kind, map, views, title, detail, records, why, empty?, declared }
       cap      the capability that answered
       result   what it answered (the envelopes of §2.3 are opened: {type:'tool_result', content}, {ok, result},
                {data} when the declaration's path is not at the top, a JSON string)
       opts     { args, presentation, title, size, max }   presentation: the declaration, else the one /mcp/tools gave
     The precedence (§2.1; the server mirror widget.from_result keeps the same):
       1 the capability's declared presentation (contract["presentation"], DA-01) → its kind and map, exactly
       2 its widget hint · 3 the shape rules - both fromCapResult's, unchanged
       4 nothing drawable → records [] and element 'none' (the tool card stays in the chat)
     element names the desk element that shows it (table, records, markdown, widget, code, session, image, kv, calendar,
     entities, composite; receipt and job: no item of their own; timeline, matrix and parts are widgets - the gantt, the heat
     map or the status matrix, the donut - DA-01e); records are widget records that draw it now - each
     carries the declaration (record.presentation), so a re-read is read through present() again.

     VeraWidget.presentations(list?)  → the declarations by capability; given /mcp/tools' list, it keeps theirs
     VeraWidget.presentationOf(cap)   → one capability's declaration, or null
     VeraWidget.presentKinds          → the closed vocabulary (kind → element, form, views) - capability_contract_core's

   Pure: no DOM, no fetch (the host hands /mcp/tools' list to presentations()). */
(function () {
  'use strict';
  const root = typeof window !== 'undefined' ? window : globalThis;
  const W = root.VeraWidget; if (!W || W.present) return;
  const isObj = (x) => x != null && typeof x === 'object' && !Array.isArray(x);
  const pick = (x, p) => W.pick(x, p);

  /* the vocabulary (capability_contract_core.PRESENTATION_KINDS): the desk element each kind is, the widget form that
     draws it, its views - and the content key the record cannot do without */
  const KINDS = {
    table: { element: 'table', form: 'table', content: ['rows'], views: ['table', 'cards', 'list', 'chart', 'graph'] },
    records: { element: 'records', form: 'reading', content: ['rows'], views: ['list', 'cards', 'table', 'graph'] },
    reading: { element: 'records', form: 'reading', content: ['rows'], views: ['list', 'cards', 'table', 'graph'] },
    document: { element: 'markdown', form: 'markdown', content: ['body'], views: ['document', 'outline'] },
    status: { element: 'widget', form: 'status', content: ['verdict'], views: ['status', 'table'] },
    metric: { element: 'widget', form: 'hero', content: ['value'], views: ['hero', 'ring', 'trace'] },
    series: { element: 'widget', form: 'trace', content: ['points'], views: ['trace', 'area', 'column', 'table'] },
    graph: { element: 'widget', form: 'vgraph', content: ['nodes'], views: ['graph', 'table'] },
    log: { element: 'widget', form: 'log', content: ['events'], views: ['log', 'table', 'pareto'] },
    progress: { element: 'widget', form: 'progress', content: ['steps'], views: ['progress', 'log'] },
    terminal: { element: 'session', form: 'terminal', content: ['stdout'], views: ['terminal'] },
    media: { element: 'image', form: 'media', content: ['url', 'b64'], views: ['media', 'gallery'] },
    code: { element: 'code', form: 'code', content: ['code'], views: ['code', 'explode'] },
    diff: { element: 'code', form: 'diff', content: ['diff'], views: ['diff', 'split'] },
    kv: { element: 'kv', form: 'kv', content: ['fields'], views: ['tiles', 'rows', 'graph'] },
    calendar: { element: 'calendar', form: 'schedule', content: ['events'], views: ['month', 'agenda', 'schedule'] },
    timeline: { element: 'widget', form: 'gantt', content: ['spans'], views: ['gantt', 'stepper', 'table'] },
    matrix: { element: 'widget', form: 'heat', content: ['cells'], views: ['heat', 'matrix', 'table'] },
    parts: { element: 'widget', form: 'donut', content: ['parts'], views: ['donut', 'treemap', 'waffle', 'table'] },
    entities: { element: 'entities', form: 'json', content: ['groups', 'entities'], views: ['chips', 'list', 'graph'] },
    receipt: { element: 'receipt', form: '', content: [], views: [] },
    job: { element: 'job', form: '', content: ['job_id'], views: [] },
    composite: { element: 'composite', form: '', content: [], views: [] },
  };
  /* an undeclared answer's kind, from the form the shape rules chose (so the caller reads one answer one way) */
  const FORM_KIND = { table: 'table', ranked: 'table', containers: 'table', sandboxes: 'table', reading: 'reading', files: 'table',
    markdown: 'document', status: 'status', hero: 'metric', ring: 'metric', numbers: 'kv', kv: 'kv', trace: 'series', area: 'series',
    candles: 'series', column: 'series', log: 'log', pareto: 'log', progress: 'progress', terminal: 'terminal', media: 'media',
    code: 'code', diff: 'diff', vgraph: 'graph', schedule: 'calendar', agenda: 'calendar', error: 'status', list: 'table', json: 'kv', string: 'document' };

  /* §2.3: the envelopes every answer is opened from */
  function open(c, contentPath) {
    for (let i = 0; i < 4; i++) {
      if (typeof c === 'string') { const s = c.trim(); if ((s[0] === '{' && s.endsWith('}')) || (s[0] === '[' && s.endsWith(']'))) { try { c = JSON.parse(s); continue; } catch (_) { /* text */ } } return c; }
      if (!isObj(c)) return c;
      if (c.type === 'tool_result' && 'content' in c) { c = c.content; continue; }
      if (c.ok === true && 'result' in c && Object.keys(c).length <= 4) { c = c.result; continue; }
      /* {data}: opened when the declaration's path is not at the top (or there is no declaration and data is all there is) */
      if ('data' in c && c.data != null && typeof c.data === 'object' && (contentPath ? pick(c, contentPath) === undefined && pick(c.data, contentPath) !== undefined : Object.keys(c).length <= 2)) { c = c.data; continue; }
      return c;
    }
    return c;
  }
  const empty = (v) => v == null || v === '' || (Array.isArray(v) && !v.length) || (isObj(v) && !Object.keys(v).length);
  /* a template over the call's arguments: "Quotes · {symbols}" */
  const fill = (t, args) => String(t).replace(/\{([a-zA-Z0-9_.]+)\}/g, (m, k) => { const v = pick(args || {}, k); return v == null ? '' : Array.isArray(v) ? v.join(', ') : String(v); }).replace(/\s*·\s*$/, '').trim();
  /* rows from a container that is a dict of things (workers keyed by id) - the key as the name */
  /* series keyed by name (DA-01e, map.keyed): {name: [{t, v}, ...]} or {name: {t: [...], v: [...]}} as one line per key,
     {name: [{t, v}]} - the shape split makes, which trace (as lines), area and column draw. t: map.t, else t / ts / time,
     else the place; v: map.v, else v / value - a number, else null (a point the chart leaves out) */
  const keyedNum = (x) => (typeof x === 'number' && isFinite(x)) ? x : (typeof x === 'string' && x.trim() !== '' && isFinite(+x)) ? +x : null;
  const keyedPoint = (p, i, tk, vk) => { if (!isObj(p)) return { t: i, v: keyedNum(p) };
    const t = tk ? pick(p, tk) : (p.t != null ? p.t : p.ts != null ? p.ts : p.time);
    return { t: t == null ? i : t, v: keyedNum(vk ? pick(p, vk) : (p.v != null ? p.v : p.value)) }; };
  function keyedSeries(v, tk, vk) {
    const out = {}; if (!isObj(v)) return out;
    Object.keys(v).forEach((k) => { const s = v[k]; let pts = null;
      if (Array.isArray(s)) pts = s.map((p, i) => keyedPoint(p, i, tk, vk));
      else if (isObj(s)) { const ts = pick(s, tk || 't'), vs = pick(s, vk || 'v'); if (Array.isArray(vs)) pts = vs.map((x, i) => ({ t: Array.isArray(ts) && ts[i] != null ? ts[i] : i, v: keyedNum(x) })); }
      if (pts && pts.length) out[k] = pts; });
    return out; }
  const asRows = (v) => Array.isArray(v) ? v : isObj(v) ? Object.keys(v).map((k) => isObj(v[k]) ? Object.assign({ name: k }, v[k]) : { name: k, value: v[k] }) : [];

  /* DA-01e (1): three kinds read onto forms the widget already draws. The server mirror (widget_cap_output.py) reads them
     the same way, field for field - the shared fixtures hold both to it. */
  const nil = (x) => x === undefined || x === null;
  /* a field: the declared path (map[k]) if there is one, else the first of the usual names the row has */
  const field = (r, m, k, names) => { if (m[k]) return pick(r, m[k]); for (const n of names) if (!nil(r[n])) return r[n]; return undefined; };
  const label = (x, i) => (nil(x) || typeof x === 'object') ? '#' + (i + 1) : String(x);
  /* timeline: a time in seconds - a number as it is, a plain number string, an ISO date or date-time (no zone = UTC);
     else null. Parsed by hand, so the page and the server agree to the millisecond (Date.parse reads a zone-less time as
     local) */
  const ISO_T = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$/i;
  function spanTime(x) {
    if (typeof x === 'number') return isFinite(x) ? x : null;
    if (typeof x !== 'string') return null;
    const s = x.trim(); if (/^-?\d+(\.\d+)?$/.test(s)) return +s;
    const t = s.match(ISO_T); if (!t || +t[1] < 100 || +t[2] < 1 || +t[2] > 12) return null;
    let off = 0; if (t[8] && t[8].toUpperCase() !== 'Z') { const z = t[8].replace(':', ''); off = (z[0] === '-' ? -1 : 1) * (+z.slice(1, 3) * 60 + +z.slice(3, 5)); }
    return (Date.UTC(+t[1], +t[2] - 1, +t[3], +(t[4] || 0), +(t[5] || 0), +(t[6] || 0)) + +(String(t[7] || '') + '000').slice(0, 3) - off * 60000) / 1000;
  }
  /* a span's status as the gantt colours it (state: done green, running the accent, failed red); the status kept as said */
  const SPAN_STATE = [[/^(ok|done|pass|passed|success|succeeded|merged|complete|completed|finished|ended|green)$/i, 'done'],
    [/^(running|active|started|live|current|in[ _-]?progress)$/i, 'running'],
    [/^(fail|failed|failure|error|errored|red|timeout|timed[ _-]?out|crashed|aborted|interrupted|cancelled|canceled)$/i, 'failed']];
  const spanState = (s) => { const x = SPAN_STATE.find((p) => p[0].test(s)); return x ? x[1] : ''; };
  /* dated spans -> gantt rows {name, start, end, at, status?, state?}: start and end in seconds from the first start (the
     gantt lays them on one axis), at the start as the answer gave it. No end: the duration's (seconds, in the start's
     unit) - else still open, so it runs to the latest time in the answer and reads as running. No start: not a span. */
  function timelineSpans(rows, m) {
    const got = [];
    rows.forEach((r, i) => { if (!isObj(r)) return;
      const a = field(r, m, 'start', ['start', 'started_at', 'start_at', 'begin', 'from']), t0 = spanTime(a); if (t0 === null) return;
      let t1 = spanTime(field(r, m, 'end', ['end', 'finished_at', 'ended_at', 'end_at', 'until', 'to']));
      if (t1 === null) { const du = spanTime(field(r, m, 'duration', ['duration', 'duration_s', 'seconds'])); if (du !== null) t1 = t0 + du; }
      const st = field(r, m, 'status', ['status', 'state', 'result']);
      got.push({ name: label(field(r, m, 'name', ['name', 'title', 'label', 'id']), i), t0, t1, at: a, status: (nil(st) || typeof st === 'object') ? '' : String(st) }); });
    if (!got.length) return [];
    const lo = Math.min(...got.map((g) => g.t0)), hi = Math.max(...got.map((g) => (g.t1 === null ? g.t0 : Math.max(g.t0, g.t1))));
    const rel = (x) => Math.round((x - lo) * 1000) / 1000;
    return got.map((g) => { const state = spanState(g.status) || (g.t1 === null ? 'running' : '');
      const o = { name: g.name, start: rel(g.t0), end: rel(g.t1 === null ? hi : g.t1), at: g.at }; if (g.status) o.status = g.status; if (state) o.state = state; return o; });
  }
  /* rows x columns of a measure -> {row: {column: value}} (every row has every column, '' where it has none - the heat map
     and the matrix take their columns from the first row; a grid, not rows, so a column called value / count / id is a
     column and not the row's number or name) and the form: the heat map when every value is a number, else the status matrix. Read from
       {row: {column: cell}}                       a grid
       [{<row>, <column>: cell, ...}]              wide rows (map.row names the row, the rest are its columns)
       [{<row>, <column>, <value>}]                long rows, one cell each (map.column set)
       [{<row>, <entries>: [{<column>, <value>}]}] each row's cells in a list of its own (map.entries)
     a cell that is an object is read at map.value; a list (or an object with no map.value) is not a cell */
  function matrixOf(v, m) {
    const order = [], cols = [], at = new Map();
    const put = (rn, cn, x) => { rn = String(rn); cn = String(cn); if (cn === 'name') return; if (!at.has(rn)) { at.set(rn, new Map()); order.push(rn); } if (!cols.includes(cn)) cols.push(cn); at.get(rn).set(cn, x); };
    const scalar = (x) => (nil(x) || typeof x === 'object') ? '' : x;
    const cellOf = (x) => (isObj(x) && m.value ? scalar(pick(x, m.value)) : scalar(x));
    const isCell = (x) => !Array.isArray(x) && !(isObj(x) && !m.value);
    const valueOf = (e) => scalar(field(e, m, 'value', ['value', 'v', 'status', 'state']));
    const rowName = (r, i) => label(field(r, m, 'row', ['name', 'id', 'key', 'label', 'title']), i);
    if (isObj(v)) Object.keys(v).forEach((k) => { if (isObj(v[k])) Object.keys(v[k]).forEach((c) => { if (isCell(v[k][c])) put(k, c, cellOf(v[k][c])); }); });
    else if (Array.isArray(v)) v.forEach((r, i) => { if (!isObj(r)) return; const rn = rowName(r, i);
      if (m.entries) { const es = pick(r, m.entries); if (Array.isArray(es)) es.forEach((e, j) => { if (isObj(e)) put(rn, label(field(e, m, 'column', ['column', 'name', 'key', 'label', 'dimension']), j), valueOf(e)); }); }
      else if (m.column) { const cn = pick(r, m.column); if (!nil(cn) && typeof cn !== 'object') put(rn, cn, valueOf(r)); }
      else { const own = m.row ? (/^[A-Za-z0-9_-]+$/.test(String(m.row)) ? String(m.row) : null) : ['name', 'id', 'key', 'label', 'title'].find((n) => !nil(r[n]));
        Object.keys(r).forEach((c) => { if (c !== own && isCell(r[c])) put(rn, c, cellOf(r[c])); }); } });
    if (!order.length || !cols.length) return null;
    const rows = {}; order.forEach((rn) => { const o = {}; const cs = at.get(rn); cols.forEach((c) => { o[c] = cs.has(c) ? cs.get(c) : ''; }); rows[rn] = o; });
    const vals = order.flatMap((rn) => cols.map((c) => rows[rn][c])).filter((x) => x !== '');
    return { rows, form: vals.length && vals.every((x) => typeof x === 'number') ? 'heat' : 'matrix' };
  }
  /* shares of a whole -> [{name, value}], the parts that are more than nothing (a number, or a number string); past eight
     the seven largest and 'other' (the donut draws eight - the ninth on would drop out of the whole). Read from
     {name: number} . {name: {<value>}} . [{<name>, <value>}] */
  const PART_VALUE = ['value', 'v', 'count', 'n', 'size', 'total'];
  function partsOf(v, m) {
    let out = [];
    if (Array.isArray(v)) v.forEach((r, i) => { if (isObj(r)) out.push({ name: label(field(r, m, 'name', ['name', 'label', 'key', 'id', 'type']), i), value: keyedNum(field(r, m, 'value', PART_VALUE)) }); });
    else if (isObj(v)) Object.keys(v).forEach((k) => { const x = v[k]; out.push({ name: k, value: keyedNum(isObj(x) ? field(x, m, 'value', PART_VALUE) : x) }); });
    out = out.filter((p) => p.value !== null && p.value > 0);
    if (out.length > 8) { const by = out.slice().sort((a, b) => b.value - a.value); out = by.slice(0, 7).concat([{ name: 'other', value: by.slice(7).reduce((s, p) => s + p.value, 0) }]); }
    return out;
  }
  /* DA-01e (3): OPTIONS on a declaration's rows, read here (and by the server mirror) before the form draws them - so the
     form's own row limit cuts the arranged rows. draw: {sort, group_by, units}; without them nothing below runs and the
     answer reads exactly as it did.
       sort: 'field' low first, '-field' high first - numbers by value then words without case, a missing value (none, '',
             an object) last either way; ties keep the answer's order                          table, records, reading
       group_by: 'field' - the rows by group, the groups in the order they first appear (no value: last), the group's
             field the row's FIRST key (the table's first column - its label; the table form has no section rows)  table
       units: {field: 'ms'} - a number in that field becomes its text with the unit ("12.5 ms", "40%"): the table and kv
             forms have no unit per column, so it is said in the data             table, records, reading, kv (fields)
     A declaration that names a row form (table, rows, cards, list, reading) has them read too, and the form is handed its
     draw without them (the table form's own draw.sort would sort again, high first). */
  const OPT_KEYS = ['sort', 'group_by', 'units'];
  const OPT_ROW_FORMS = ['table', 'rows', 'cards', 'list', 'reading'];
  const optNum = (x) => (typeof x === 'number' && isFinite(x)) ? x : (typeof x === 'boolean' ? +x : null);
  const optMissing = (x) => nil(x) || typeof x === 'object' || (typeof x === 'number' && !isFinite(x)) || (typeof x === 'string' && !x.trim());
  const optCmp = (a, b) => { const na = optNum(a), nb = optNum(b);
    if (na !== null && nb !== null) return na < nb ? -1 : na > nb ? 1 : 0;
    if (na !== null) return -1; if (nb !== null) return 1;
    const sa = String(a).toLowerCase(), sb = String(b).toLowerCase(); return sa < sb ? -1 : sa > sb ? 1 : 0; };
  const optPath = (p) => String(p).replace(/^\$\.?/, '').replace(/\[(-?\d+)\]/g, '.$1').split('.').filter(Boolean);
  /* a copy of x with the value at path set (each object or list on the way copied, nothing else); x itself when the path
     does not reach */
  function optSet(x, parts, v) {
    if (!parts.length) return v; const k = parts[0], rest = parts.slice(1);
    if (Array.isArray(x) && /^-?\d+$/.test(k)) { let i = +k; if (i < 0) i += x.length; if (i < 0 || i >= x.length) return x; const y = x.slice(); y[i] = optSet(x[i], rest, v); return y; }
    if (isObj(x) && (!rest.length || k in x)) { const y = Object.assign({}, x); y[k] = optSet(x[k], rest, v); return y; }
    return x;
  }
  function optSort(rows, spec) {
    const desc = spec[0] === '-', f = (desc ? spec.slice(1) : spec).trim();
    return rows.map((r, i) => [r, isObj(r) ? pick(r, f) : undefined, i]).sort((x, y) => { const mx = optMissing(x[1]) ? 1 : 0, my = optMissing(y[1]) ? 1 : 0;
      if (mx || my) return (mx - my) || (x[2] - y[2]);
      const tx = optNum(x[1]) === null ? 1 : 0, ty = optNum(y[1]) === null ? 1 : 0;   // numbers before words either way
      if (tx !== ty) return tx - ty; return (desc ? -optCmp(x[1], y[1]) : optCmp(x[1], y[1])) || (x[2] - y[2]); }).map((x) => x[0]);
  }
  function optGroup(rows, f) {
    const order = [], by = {}, none = [];
    rows.forEach((r) => { const v = isObj(r) ? pick(r, f) : undefined; if (optMissing(v)) { none.push(r); return; }
      const k = optNum(v) !== null ? 'n:' + optNum(v) : 's:' + v; if (!by[k]) { by[k] = []; order.push(k); }
      by[k].push(Object.assign({ [f]: v }, r)); });   // the group's field first (a nested one under its path's name)
    return order.flatMap((k) => by[k]).concat(none);
  }
  const optFmt = (x, u) => { const a = Math.abs(x), d = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3, k = Math.pow(10, d);
    return String(Math.round(x * k) / k) + (/^[%°]/.test(u) ? '' : ' ') + u; };
  function optUnits(r, units) {
    if (!isObj(r)) return r;
    Object.keys(units).forEach((f) => { const u = units[f], v = pick(r, f); if (typeof u === 'string' && u && typeof v === 'number' && isFinite(v)) r = optSet(r, optPath(f), optFmt(v, u)); });
    return r;
  }
  /* the options a declaration's draw holds, or null - none of them: nothing changes */
  const optsOf = (P) => (isObj(P.draw) && OPT_KEYS.some((k) => !nil(P.draw[k])) ? P.draw : null);
  function arrange(rows, d, grouped) {
    let rw = rows;
    if (typeof d.sort === 'string' && d.sort.replace(/^-/, '').trim()) rw = optSort(rw, d.sort.trim());
    if (grouped && typeof d.group_by === 'string' && d.group_by.trim()) rw = optGroup(rw, d.group_by.trim());
    if (isObj(d.units)) rw = rw.map((r) => optUnits(r, d.units));
    return rw;
  }
  /* the rows at path in c, arranged, put back in a copy of c - c as it is when the path holds no rows */
  const arrangedAt = (c, path, d, grouped) => { const rs = pick(c, path || ''); return (Array.isArray(rs) || isObj(rs)) ? optSet(c, optPath(path || ''), arrange(asRows(rs), d, grouped)) : c; };

  let REG = {};
  /* THE LAST DECLARATIONS SEEN ARE KEPT (review 2026-10-15 U12): with the hint table gone (DA-01d), a capability list that
     arrives without declarations - a server that does not carry them yet, a failed or stale /mcp/tools - drew every declared
     capability as raw data. The last list that had them is kept in this browser and used until a list with them comes. */
  const KEEP = 'vera:presentations';
  const keep = (r) => { try { if (typeof localStorage !== 'undefined') localStorage.setItem(KEEP, JSON.stringify(r)); } catch (e) { /* this page only */ } };
  const kept = () => { try { const v = typeof localStorage !== 'undefined' ? JSON.parse(localStorage.getItem(KEEP) || 'null') : null; return isObj(v) ? v : {}; } catch (e) { return {}; } };
  function presentations(list) {
    if (Array.isArray(list)) { const r = {}; list.forEach((t) => { if (t && t.name && isObj(t.presentation) && t.presentation.status === 'declared') r[t.name] = t.presentation; });
      if (Object.keys(r).length) { REG = r; keep(r); } else if (list.length) REG = kept(); else REG = r; }
    else if (isObj(list)) REG = Object.assign({}, list);
    return Object.assign({}, REG);
  }
  const presentationOf = (cap) => REG[String(cap || '')] || null;

  /* one declared kind → its widget record(s): the declared map in the widget map's words where the form reads through a
     map (the dashboard's reads and their refresh keep working), else the data picked out here */
  function declared(cap, c, P, o) {
    const K = KINDS[P.kind]; const m = isObj(P.map) ? P.map : {};
    const title = o.title || (P.title ? fill(P.title, o.args) : '') || cap || 'result';
    const out = []; const why = 'declared: ' + P.kind;
    const rec = (form, data, extra) => { const r = Object.assign({ id: (cap || 'result').replace(/[^a-z0-9]+/gi, '-') + '-' + form + (out.length ? '-' + out.length : ''),
      form, title: (extra && extra.title) || title, source: cap, read: { args: o.args || {}, map: (extra && extra.map) || {} }, frame: { size: o.size || 'l' }, data, why,
      presentation: P }, extra && extra.draw ? { draw: extra.draw } : {}); out.push(r); return r; };
    /* a declaration that names its widget form (DA-01d): the form draws the answer through the map in its own words,
       with its drawing options - what a widget hint held */
    const opts = optsOf(P), rowKind = P.kind === 'table' || P.kind === 'records' || P.kind === 'reading';
    if (typeof P.form === 'string' && P.form) {
      /* a row form with no rows drew its SAMPLE rows ("sample · no source"), which read as real data: the declaration's
         empty line instead (a composite's part drops out, as a plain table part with no rows does) */
      if (rowKind && OPT_ROW_FORMS.includes(P.form) && empty(pick(c, m.rows || ''))) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' };
      if (opts && rowKind && OPT_ROW_FORMS.includes(P.form)) {   /* DA-01e (3): the options read here, the rest of draw is the form's */
        const g = P.kind === 'table' && typeof opts.group_by === 'string' && opts.group_by.trim() ? opts.group_by.trim() : '';
        const dr = {}; Object.keys(opts).forEach((k) => { if (!OPT_KEYS.includes(k)) dr[k] = opts[k]; });
        if (g && Array.isArray(dr.columns)) dr.columns = [g].concat(dr.columns.filter((x) => x !== g));
        rec(P.form, arrangedAt(c, m.rows, opts, P.kind === 'table'), { map: m, draw: Object.keys(dr).length ? dr : undefined });
        return { records: out.slice(0, 1) };
      }
      rec(P.form, c, { map: m, draw: isObj(P.draw) ? P.draw : undefined }); return { records: out.slice(0, 1) };
    }
    const at = (k) => (m[k] ? pick(c, m[k]) : undefined);
    const content = (K.content || []).map((k) => at(k)).find((v) => v !== undefined);
    if (K.content.length && empty(content)) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' };
    const rowsOf = (k) => asRows(at(k)), renamed = (rows, keys) => rows.map((r) => { if (!isObj(r)) return r; const x = Object.assign({}, r); keys.forEach((k) => { if (m[k] && x[k] === undefined) { const v = pick(r, m[k]); if (v !== undefined) x[k] = v; } }); return x; });
    switch (P.kind) {
      case 'table': {
        rec('table', opts ? arrangedAt(c, m.rows, opts, true) : c, { map: { rows: m.rows } });
        if (m.name && m.value) rec('ranked', c, { map: { values: m.rows, name: m.name, value: m.value }, title: title + ' · by ' + String(m.value).split('.').pop().replace(/_/g, ' ') });
        break; }
      case 'records': case 'reading': {
        const rows = renamed(rowsOf('rows'), ['id', 'title', 'url', 'snippet', 'when', 'score', 'domain', 'text', 'cited']);
        rec('reading', opts ? arrange(rows, opts, false) : rows); break; }
      case 'document': rec('markdown', String(at('body'))); break;
      case 'status': rec('status', Object.assign({ status: at('verdict') }, m.checks ? { checks: asRows(at('checks')) } : {})); break;
      case 'metric': rec(m.max ? 'ring' : 'hero', c, { map: { value: m.value, max: m.max, unit: m.unit, trend: m.trend } }); break;
      case 'series':
        if (m.keyed === true) { const ks = keyedSeries(at('points'), m.t, m.v);
          if (!Object.keys(ks).length) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' };
          rec('trace', ks); }
        else if (Array.isArray(m.split) && m.split.length) rec('area', c, { map: { series: m.points, t: m.t, split: m.split } });
        else rec('trace', c, { map: { series: m.points, t: m.t, v: m.v } });
        break;
      case 'log': rec('log', c, { map: { events: m.events, t: m.t, text: m.text, kind: m.kind || m.level } }); break;
      case 'progress': rec('progress', { steps: renamed(rowsOf('steps'), ['name', 'status', 'detail']) }); break;
      case 'graph': { const nid = m.node_id, ef = m.edge_from, et = m.edge_to;
        const nodes = rowsOf('nodes').map((n) => (isObj(n) && nid && n.id === undefined ? Object.assign({ id: pick(n, nid) }, n) : n));
        const edges = rowsOf('edges').map((e) => (isObj(e) ? Object.assign({}, e, ef && e.source === undefined ? { source: pick(e, ef) } : {}, et && e.target === undefined ? { target: pick(e, et) } : {}) : e));
        rec('vgraph', { nodes, edges, links: edges }); break; }
      case 'terminal': { const cmd = at('command'), lines = [cmd ? '$ ' + cmd : ''].concat(String(at('stdout') || '').split('\n')).concat(String(at('stderr') || '').split('\n')).filter((l, i) => i > 0 || l);
        const rc = at('rc'); rec('terminal', { lines, state: rc != null ? 'rc ' + rc : '' }); break; }
      case 'media': { const u = at('url'), b = at('b64'); rec('media', Object.assign({}, u ? { url: u } : {}, b ? { image_b64: b } : {}, m.kind ? { kind: at('kind') } : {}, m.caption ? { caption: at('caption') } : {})); break; }
      case 'code': rec('code', { code: String(at('code')), lang: at('lang') || '', path: at('path') || '' }); break;
      case 'diff': rec('diff', { diff: String(at('diff')) }); break;
      case 'kv': { const f = at('fields'); rec('kv', opts && isObj(opts.units) && isObj(f) ? optUnits(f, opts.units) : f); break; }
      case 'calendar': rec('schedule', renamed(rowsOf('events'), ['start', 'end', 'title'])); break;
      /* DA-01e (1): the new kinds read to their form's own data - nothing left in it to draw: the empty line */
      case 'timeline': { const sp = timelineSpans(asRows(at('spans')), m); if (!sp.length) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' }; rec('gantt', sp); break; }
      case 'matrix': { const mx = matrixOf(at('cells'), m); if (!mx) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' }; rec(mx.form, mx.rows); break; }
      case 'parts': { const ps = partsOf(at('parts'), m); if (!ps.length) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' }; rec('donut', ps); break; }
      case 'entities': rec('json', m.groups ? { groups: at('groups') } : { entities: at('entities') }); break;
      default: break;
    }
    return { records: out.slice(0, Math.max(1, +o.max || 3)) };
  }

  function present(cap, result, opts) {
    const o = opts || {}; cap = String(cap || '');
    const P0 = isObj(o.presentation) ? o.presentation : presentationOf(cap);
    const P = P0 && KINDS[P0.kind] && P0.status !== 'invalid' ? P0 : null;
    const K = P ? KINDS[P.kind] : null;
    const c = open(result, P && !P.form && isObj(P.map) ? (K.content || []).map((k) => P.map[k]).find(Boolean) : '');
    const base = (kind, why) => ({ element: KINDS[kind] ? KINDS[kind].element : 'widget', kind, map: P ? Object.assign({}, P.map || {}) : {},
      views: (P && Array.isArray(P.views) && P.views.length ? P.views : (KINDS[kind] ? KINDS[kind].views : [])).slice(),
      title: o.title || (P && P.title ? fill(P.title, o.args) : '') || cap || 'result', detail: P && P.detail ? P.detail : null, why, declared: !!P });
    if (P) {
      if (P.kind === 'receipt') return Object.assign(base('receipt', 'declared: receipt - it updates what it acted on'), { records: [], receipt: { ok: P.map && P.map.ok ? pick(c, P.map.ok) : (isObj(c) ? c.ok : undefined), id: P.map && P.map.id ? pick(c, P.map.id) : undefined, message: P.map && P.map.message ? pick(c, P.map.message) : undefined } });
      if (P.kind === 'job') return Object.assign(base('job', 'declared: job - its live item follows'), { records: [], job: { job_id: pick(c, (P.map || {}).job_id || 'job_id'), stream: P.map && P.map.stream ? pick(c, P.map.stream) : undefined } });
      if (P.kind === 'composite') {
        const parts = (Array.isArray(P.parts) ? P.parts : []).map((pt) => (isObj(pt) && KINDS[pt.kind] && pt.kind !== 'composite' ? declared(cap, c, Object.assign({}, pt, { title: pt.title || '' }), Object.assign({}, o, { title: pt.title || o.title, max: 1 })).records[0] : null)).filter(Boolean);
        return Object.assign(base('composite', 'declared: composite of ' + parts.length), { records: parts }, parts.length ? {} : { element: 'none', empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' });
      }
      const d = declared(cap, c, P, o);
      return Object.assign(base(P.kind, 'declared: ' + P.kind), { records: d.records }, d.empty != null ? { empty: d.empty } : {});
    }
    /* 2 · 3: the hint and the shape rules, unchanged (fromCapResult) - read as one kind for the caller */
    const recs = W.fromCapResult(cap, c, { args: o.args, title: o.title, size: o.size, max: o.max });
    if (!recs.length) return Object.assign(base('none', 'nothing drawable'), { element: 'none', records: [] });
    /* a form the vocabulary has no kind for (the Loop Lab's pictures, a list, named numbers) is a widget */
    const kind = FORM_KIND[recs[0].form] || 'widget';
    return Object.assign(base(kind, recs[0].why || ''), { records: recs });
  }

  Object.assign(W, { present, presentations, presentationOf, presentKinds: JSON.parse(JSON.stringify(KINDS)) });
})();
