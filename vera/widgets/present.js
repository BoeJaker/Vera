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
     entities, composite; receipt and job: no item of their own); records are widget records that draw it now - each
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
    if (typeof P.form === 'string' && P.form) { rec(P.form, c, { map: m, draw: isObj(P.draw) ? P.draw : undefined }); return { records: out.slice(0, 1) }; }
    const at = (k) => (m[k] ? pick(c, m[k]) : undefined);
    const content = (K.content || []).map((k) => at(k)).find((v) => v !== undefined);
    if (K.content.length && empty(content)) return { records: [], empty: P.empty ? fill(P.empty, o.args) : 'nothing to show' };
    const rowsOf = (k) => asRows(at(k)), renamed = (rows, keys) => rows.map((r) => { if (!isObj(r)) return r; const x = Object.assign({}, r); keys.forEach((k) => { if (m[k] && x[k] === undefined) { const v = pick(r, m[k]); if (v !== undefined) x[k] = v; } }); return x; });
    switch (P.kind) {
      case 'table': {
        rec('table', c, { map: { rows: m.rows } });
        if (m.name && m.value) rec('ranked', c, { map: { values: m.rows, name: m.name, value: m.value }, title: title + ' · by ' + String(m.value).split('.').pop().replace(/_/g, ' ') });
        break; }
      case 'records': case 'reading': {
        const rows = renamed(rowsOf('rows'), ['id', 'title', 'url', 'snippet', 'when', 'score', 'domain', 'text', 'cited']);
        rec('reading', rows); break; }
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
      case 'kv': rec('kv', at('fields')); break;
      case 'calendar': rec('schedule', renamed(rowsOf('events'), ['start', 'end', 'title'])); break;
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
