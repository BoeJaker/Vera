/* VeraDive - the deep dive's eight improvements (looplab-control §9 Phase K5, 19 Oct 2026; the canvas's Deep dive board).
   The dive itself stays VeraWidget.dive (widget_element.js); this file is served with it (the widget route appends it, as
   it does present.js) and VeraWidget.dive hands it the open sheet. Every dashboard, the desk and Loop Lab get them.

     1 layer another record   ⊕ Layer: a template from the catalogue drawn under the main one, on the same sheet
     2 brush a range          the rows' own time field as a strip; drag across it to keep only that range
     3 related records        templates that read the same family of capabilities, one click to layer
     4 provenance + freshness what was read, with what, when, how often - and how old it is now
     5 row drill              a row opens the data drawer (the item whole, where it came from, the items related to it)
     6 diff over time         ↻ Read again says what changed since the last read: rows added, gone, changed
     7 annotations            notes on the record, per viewer, kept with its key
     8 keyboard               r read again · l layer · n note · / search the rows · b clear the brush · ? the keys

   Pure (Node-tested): timeField(rows), brushRows(rows, field, from, to), bins(rows, field, n), diffAnswers(a, b),
   rowKey(row), relatedTemplates(list, rec, max), noteKey(rec). */
(function (root) {
  'use strict';
  if (root.VeraDive && typeof root.VeraDive.diffAnswers === 'function') { if (typeof module !== 'undefined' && module.exports) module.exports = root.VeraDive; return; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }
  function toMs(v) { if (v == null || v === '' || typeof v === 'boolean') return null; if (typeof v === 'number') return v > 1e9 && v < 1e11 ? v * 1000 : (v >= 1e11 && v < 1e14 ? v : null); var t = Date.parse(String(v)); return isNaN(t) ? null : t; }

  var TIME_KEYS = ['ts', 't', 'time', 'at', 'when', 'date', 'started_at', 'start', 'created_at', 'updated_at', 'finished_at', 'timestamp', 'day'];
  /* the field the rows are dated by: a known name that dates most rows, else any field that does */
  function timeField(rows) {
    rows = (rows || []).filter(isObj); if (rows.length < 2) return '';
    var keys = {}; rows.slice(0, 50).forEach(function (r) { Object.keys(r).forEach(function (k) { keys[k] = 1; }); });
    var cand = TIME_KEYS.filter(function (k) { return keys[k]; }).concat(Object.keys(keys).filter(function (k) { return TIME_KEYS.indexOf(k) < 0; }));
    for (var i = 0; i < cand.length; i++) {
      var k = cand[i], n = 0, ok = 0;
      rows.slice(0, 200).forEach(function (r) { if (r[k] == null) return; n++; if (toMs(r[k]) != null && (typeof r[k] !== 'number' || TIME_KEYS.indexOf(k) >= 0)) ok++; });
      if (n >= 2 && ok / n >= 0.8) return k;
    }
    return '';
  }
  function brushRows(rows, field, from, to) {
    if (!field || from == null || to == null) return (rows || []).slice();
    var a = Math.min(from, to), b = Math.max(from, to);
    return (rows || []).filter(function (r) { var t = isObj(r) ? toMs(r[field]) : null; return t != null && t >= a && t <= b; });
  }
  function bins(rows, field, n) {
    n = Math.max(1, n || 40); var ts = (rows || []).map(function (r) { return isObj(r) ? toMs(r[field]) : null; }).filter(function (t) { return t != null; });
    if (!ts.length) return { from: null, to: null, counts: [] };
    var lo = Math.min.apply(null, ts), hi = Math.max.apply(null, ts), span = Math.max(1, hi - lo), c = new Array(n).fill(0);
    ts.forEach(function (t) { c[Math.min(n - 1, Math.floor((t - lo) / span * n))]++; });
    return { from: lo, to: hi, counts: c };
  }
  /* a row's identity across two reads: its id-like field, else its whole value */
  var ID_KEYS = ['id', 'key', 'name', 'run_id', 'pipeline_id', 'host_id', 'hash', 'sha', 'path', 'ts'];
  function rowKey(r) {
    if (!isObj(r)) return 'v:' + JSON.stringify(r);
    for (var i = 0; i < ID_KEYS.length; i++) { var v = r[ID_KEYS[i]]; if (v != null && v !== '' && typeof v !== 'object') return ID_KEYS[i] + ':' + v; }
    return 'j:' + JSON.stringify(r);
  }
  function diffAnswers(prev, next) {
    var a = {}, b = {}; (prev || []).forEach(function (r) { a[rowKey(r)] = r; }); (next || []).forEach(function (r) { b[rowKey(r)] = r; });
    var added = [], removed = [], changed = [], same = 0;
    Object.keys(b).forEach(function (k) { if (!(k in a)) added.push(b[k]); else if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) changed.push({ key: k, before: a[k], after: b[k] }); else same++; });
    Object.keys(a).forEach(function (k) { if (!(k in b)) removed.push(a[k]); });
    return { added: added, removed: removed, changed: changed, same: same };
  }
  function capOf(t) { if (!t) return ''; if (typeof t.source === 'string') return t.source; return (t.reads && t.reads.cap) || (t.read && t.read.cap) || ''; }
  /* templates that read the same family of capabilities (evolve.release.* beside evolve.release.lane), nearest first */
  function relatedTemplates(list, rec, max) {
    var cap = capOf(rec); if (!cap) return [];
    var parts = cap.split('.'), self = (rec && (rec.id || rec.template)) || '';
    return (list || []).map(function (t) {
      var c = capOf(t), p = c.split('.'), n = 0; while (n < parts.length && n < p.length && parts[n] === p[n]) n++;
      return { t: t, n: n, same: c === cap };
    }).filter(function (x) { return x.n >= 1 && x.t.id !== self && capOf(x.t); })
      .sort(function (a, b) { return b.n - a.n || (a.same === b.same ? 0 : a.same ? 1 : -1) || String(a.t.name).localeCompare(String(b.t.name)); })
      .slice(0, max || 8).map(function (x) { return x.t; });
  }
  function noteKey(rec) { return 'vera-dive-notes:' + String((rec && (rec.id || rec.template || ((rec.source || '') + '|' + (rec.title || rec.form)))) || 'widget').slice(0, 160); }

  var api = { timeField: timeField, brushRows: brushRows, bins: bins, rowKey: rowKey, diffAnswers: diffAnswers, relatedTemplates: relatedTemplates, noteKey: noteKey, toMs: toMs };
  root.VeraDive = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof document === 'undefined') return;

  var CSS = '.vw-dive-x{min-width:0;background:var(--s0,var(--bg0,#101216));border-radius:10px;padding:10px 12px}.vw-dive-x h4{margin:0 0 8px;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--dim,#8b93a1)}'
    + '.vw-dx-kv{display:grid;grid-template-columns:110px 1fr;gap:3px 10px;font-size:12px}.vw-dx-kv b{font-weight:500;color:var(--dim,#8b93a1)}.vw-dx-stale{color:#e0a458}'
    + '.vw-dx-brush{grid-column:1/-1;height:46px;position:relative;cursor:crosshair;background:var(--s0,var(--bg0,#101216));border-radius:8px}.vw-dx-brush svg{width:100%;height:100%;display:block}'
    + '.vw-dx-sel{position:absolute;top:0;bottom:0;background:rgba(122,162,247,.18);border:1px solid rgba(122,162,247,.6);pointer-events:none}.vw-dx-bl{position:absolute;right:8px;top:4px;font-size:10.5px;color:var(--dim,#8b93a1)}'
    + '.vw-dx-layers{grid-column:1/-1;display:grid;gap:10px}.vw-dx-layer{background:var(--s0,var(--bg0,#101216));border-radius:10px;padding:8px;position:relative;height:260px}.vw-dx-layer>button{position:absolute;right:6px;top:6px;z-index:2}'
    + '.vw-dx-pick{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}.vw-dx-pick input{font:inherit;font-size:12px;padding:3px 8px;border-radius:6px;border:1px solid var(--bd2,rgba(255,255,255,.12));background:var(--s2,#1f232b);color:inherit;min-width:240px}'
    + '.vw-dx-chip{font:inherit;font-size:11px;border:1px solid var(--bd2,rgba(255,255,255,.12));border-radius:10px;background:none;color:inherit;padding:1px 8px;cursor:pointer}'
    + '.vw-dx-diff .a{color:#6db87a}.vw-dx-diff .r{color:#c96b6b}.vw-dx-diff .c{color:#e0a458}.vw-dx-diff li{font-size:11.5px;font-family:var(--f-mono,monospace);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.vw-dx-notes textarea{width:100%;box-sizing:border-box;font:inherit;font-size:12px;min-height:54px;border-radius:6px;border:1px solid var(--bd2,rgba(255,255,255,.12));background:var(--s2,#1f232b);color:inherit;padding:6px}'
    + '.vw-dx-note{font-size:12px;border-left:3px solid #7aa2f7;padding:2px 8px;margin:4px 0}.vw-dx-note small{color:var(--dim,#8b93a1);margin-right:6px}.vw-dx-keys{font-size:11px;color:var(--dim,#8b93a1)}.vw-dx-keys kbd{border:1px solid var(--bd2,rgba(255,255,255,.2));border-radius:3px;padding:0 4px;margin:0 2px}';
  function css() { if (document.getElementById('vw-dive-x-css')) return; var st = document.createElement('style'); st.id = 'vw-dive-x-css'; st.textContent = CSS; document.head.appendChild(st); }
  function ago(ms) { var s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? s + ' s' : s < 3600 ? Math.floor(s / 60) + ' min' : Math.floor(s / 3600) + ' h ' + Math.floor((s % 3600) / 60) + ' min'; }
  function store(k, v) { try { if (v === undefined) return JSON.parse(localStorage.getItem(k) || '[]'); localStorage.setItem(k, JSON.stringify(v)); } catch (_) { return []; } }
  var _tpls = null;
  function templates(call) { if (!_tpls) _tpls = Promise.resolve(call('widget.template.list', {})).then(function (c) { return (c && c.templates) || []; }, function () { _tpls = null; return []; }); return _tpls; }
  function match(list, q) {
    if (root.VeraDashboardSummon && root.VeraDashboardSummon.matches) return root.VeraDashboardSummon.matches(list, q);
    q = String(q || '').toLowerCase(); return (list || []).filter(function (t) { return String(t.name || '').toLowerCase().indexOf(q) >= 0; });
  }

  /* the open sheet gets the eight: ctx = {sc, bd, hd, rec, rows(data) -> rows, call(cap, args), setRows(rows), refresh()} */
  function enhance(ctx) {
    css();
    var rec = ctx.rec, bd = ctx.bd, cur = { data: ctx.data, rows: ctx.rows(ctx.data), at: Date.now(), prevRows: null, brush: null };
    var every = (rec.read && rec.read.refresh) || (rec.reads && rec.reads.every) || '';
    // the strip and the sections, after the dive's own (they live in their own nodes: the dive's refill keeps them)
    var brush = document.createElement('div'); brush.className = 'vw-dx-brush'; brush.title = 'drag across to keep only that time range (b clears)';
    var layers = document.createElement('div'); layers.className = 'vw-dx-layers';
    var prov = document.createElement('div'); prov.className = 'vw-dive-x'; var diff = document.createElement('div'); diff.className = 'vw-dive-x vw-dx-diff';
    var rel = document.createElement('div'); rel.className = 'vw-dive-x'; var notes = document.createElement('div'); notes.className = 'vw-dive-x vw-dx-notes';
    var keys = document.createElement('div'); keys.className = 'vw-dx-keys'; keys.style.gridColumn = '1/-1';
    keys.innerHTML = 'keys <kbd>r</kbd> read again <kbd>l</kbd> layer <kbd>n</kbd> note <kbd>/</kbd> search rows <kbd>b</kbd> clear the brush <kbd>Esc</kbd> close';
    var main = bd.querySelector('.vw-dive-main');
    if (main && main.nextSibling) bd.insertBefore(layers, main.nextSibling); else bd.appendChild(layers);
    bd.insertBefore(brush, layers.nextSibling);
    [prov, diff, rel, notes, keys].forEach(function (x) { bd.appendChild(x); });

    // 4 provenance and freshness
    function drawProv() {
      var args = (rec.read && rec.read.args) || (rec.reads && rec.reads.args) || {}, age = Date.now() - cur.at;
      var stale = every && /^(\d+)\s*s$/.test(every) && age > 2 * 1000 * +RegExp.$1;
      prov.innerHTML = '<h4>Where it came from</h4><div class="vw-dx-kv"><b>reads</b><span>' + esc(capOf(rec) || (rec.data !== undefined ? 'data it carries' : '—')) + '</span><b>with</b><span>' + esc(JSON.stringify(args)) +
        '</span><b>read</b><span class="' + (stale ? 'vw-dx-stale' : '') + '">' + esc(new Date(cur.at).toLocaleTimeString()) + ' · ' + ago(age) + ' ago' + (stale ? ' · stale' : '') + '</span><b>refresh</b><span>' + esc(every || 'on demand') +
        '</span><b>record</b><span>' + esc([rec.id || rec.template, rec.form].filter(Boolean).join(' · ')) + '</span><b>rows</b><span>' + cur.rows.length + (cur.brush ? ' (' + ctxShown + ' in the brushed range)' : '') + '</span></div>';
    }
    var ctxShown = 0, tick = setInterval(function () { if (!ctx.sc.isConnected) { clearInterval(tick); return; } drawProv(); }, 15000);

    // 2 brush a range over the rows' own time field
    function drawBrush() {
      var f = timeField(cur.rows); brush.style.display = f ? '' : 'none'; if (!f) return;
      var b = bins(cur.rows, f, 60), mx = Math.max.apply(null, b.counts.concat([1]));
      brush.innerHTML = '<svg viewBox="0 0 600 46" preserveAspectRatio="none">' + b.counts.map(function (c, i) { var h = c / mx * 38; return '<rect x="' + (i * 10 + 1) + '" y="' + (44 - h) + '" width="8" height="' + h + '" fill="#7aa2f7" fill-opacity=".55"/>'; }).join('') + '</svg>' +
        '<span class="vw-dx-bl">' + esc(f) + ' · ' + esc(new Date(b.from).toLocaleString()) + ' → ' + esc(new Date(b.to).toLocaleString()) + '</span>';
      brush._range = b; brush._field = f;
      if (cur.brush) { var w = brush.clientWidth || 600, x0 = (cur.brush[0] - b.from) / Math.max(1, b.to - b.from) * w, x1 = (cur.brush[1] - b.from) / Math.max(1, b.to - b.from) * w;
        var sel = document.createElement('div'); sel.className = 'vw-dx-sel'; sel.style.left = Math.min(x0, x1) + 'px'; sel.style.width = Math.max(2, Math.abs(x1 - x0)) + 'px'; brush.appendChild(sel); }
    }
    function applyBrush() {
      var shown = cur.brush ? brushRows(cur.rows, brush._field, cur.brush[0], cur.brush[1]) : cur.rows; ctxShown = shown.length;
      ctx.setRows(shown); drawProv(); drawBrush();
    }
    var bx = null;
    brush.addEventListener('mousedown', function (e) { var r = brush.getBoundingClientRect(); bx = e.clientX - r.left; });
    brush.addEventListener('mouseup', function (e) {
      if (bx == null || !brush._range) return; var r = brush.getBoundingClientRect(), x = e.clientX - r.left, B = brush._range, span = Math.max(1, B.to - B.from);
      if (Math.abs(x - bx) > 4) { cur.brush = [B.from + Math.min(x, bx) / r.width * span, B.from + Math.max(x, bx) / r.width * span]; applyBrush(); }
      bx = null;
    });

    // 6 diff over time - drawn when a second read arrives
    function drawDiff() {
      if (!cur.prevRows) { diff.innerHTML = '<h4>Since the last read</h4><i style="color:var(--dim)">↻ Read again (r) to see what changed</i>'; return; }
      var d = diffAnswers(cur.prevRows, cur.rows), li = function (cls, s, rows) { return rows.slice(0, 6).map(function (r) { return '<li class="' + cls + '">' + s + ' ' + esc(rowKey(r.after || r).slice(0, 90)) + '</li>'; }).join(''); };
      diff.innerHTML = '<h4>Since the last read</h4><div style="font-size:12px;margin-bottom:4px">' + d.added.length + ' added · ' + d.removed.length + ' gone · ' + d.changed.length + ' changed · ' + d.same + ' the same</div><ul style="margin:0;padding-left:16px">' +
        li('a', '+', d.added) + li('r', '−', d.removed) + li('c', '~', d.changed) + '</ul>';
    }

    // 1 + 3 layers: a template drawn under the main one; related templates one click away
    function addLayer(t) {
      var box = document.createElement('div'); box.className = 'vw-dx-layer';
      var w = document.createElement('vera-widget'); w.setAttribute('size', 'l'); w.setAttribute('bare', ''); w.setAttribute('item-drawer', ''); w.setAttribute('no-sample', '');
      var r = {}; Object.keys(t).forEach(function (k) { if (!/^(placements|instances|created_at|updated_at|version)$/.test(k)) r[k] = t[k]; }); r.title = t.name || t.id;
      if (t.form === 'panel' && t.source && t.source.panel) r.panel = t.source.panel;
      w.setAttribute('record', JSON.stringify(r));
      var x = document.createElement('button'); x.className = 'vw-dx-chip'; x.textContent = '✕ ' + (t.name || t.id); x.title = 'take this layer off'; x.onclick = function () { box.remove(); };
      box.appendChild(x); box.appendChild(w); layers.appendChild(box);
    }
    function drawRel() {
      rel.innerHTML = '<h4>Related records</h4><div class="vw-dx-pick"><input class="vw-dx-q" placeholder="layer another record… (l)"></div><div class="vw-dx-pick vw-dx-hits"></div><div class="vw-dx-pick vw-dx-rel"><i style="color:var(--dim)">…</i></div>';
      templates(ctx.call).then(function (list) {
        var rs = relatedTemplates(list, rec, 8), box = rel.querySelector('.vw-dx-rel');
        box.innerHTML = rs.length ? rs.map(function (t, i) { return '<button class="vw-dx-chip" data-rel="' + i + '" title="' + esc(capOf(t)) + '">⊕ ' + esc(t.name) + '</button>'; }).join('') : '<i style="color:var(--dim)">no other record reads this family</i>';
        box.onclick = function (e) { var b = e.target.closest('[data-rel]'); if (b) addLayer(rs[+b.getAttribute('data-rel')]); };
        var q = rel.querySelector('.vw-dx-q'), hits = rel.querySelector('.vw-dx-hits'), m = [];
        q.oninput = function () { m = q.value.trim() ? match(list, q.value).slice(0, 6) : []; hits.innerHTML = m.map(function (t, i) { return '<button class="vw-dx-chip" data-hit="' + i + '">⊕ ' + esc(t.name) + '</button>'; }).join(''); };
        q.onkeydown = function (e) { if (e.key === 'Enter' && m[0]) { addLayer(m[0]); q.value = ''; hits.innerHTML = ''; } e.stopPropagation(); };
        hits.onclick = function (e) { var b = e.target.closest('[data-hit]'); if (b) { addLayer(m[+b.getAttribute('data-hit')]); q.value = ''; hits.innerHTML = ''; } };
      });
    }

    // 7 annotations, per viewer, kept with the record's key
    function drawNotes() {
      var k = noteKey(rec), list = store(k) || [];
      notes.innerHTML = '<h4>Notes · ' + list.length + '</h4>' + list.slice().reverse().map(function (n, i) { return '<div class="vw-dx-note"><small>' + esc(new Date(n.at).toLocaleString()) + '</small>' + esc(n.text) + ' <button class="vw-dx-chip" data-del="' + (list.length - 1 - i) + '" title="delete">✕</button></div>'; }).join('') +
        '<textarea class="vw-dx-nt" placeholder="a note on this record (n) - kept in this browser; Ctrl+Enter saves"></textarea>';
      var ta = notes.querySelector('.vw-dx-nt');
      ta.onkeydown = function (e) { e.stopPropagation(); if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && ta.value.trim()) { list.push({ at: Date.now(), text: ta.value.trim().slice(0, 2000) }); store(k, list); drawNotes(); } };
      notes.onclick = function (e) { var b = e.target.closest('[data-del]'); if (b) { list.splice(+b.getAttribute('data-del'), 1); store(k, list); drawNotes(); } };
    }

    // 8 keyboard
    function onKey(e) {
      if (!ctx.sc.isConnected) { document.removeEventListener('keydown', onKey, true); return; }
      var tg = e.target; if (tg && (/^(INPUT|TEXTAREA|SELECT)$/.test(tg.tagName) || tg.isContentEditable) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'r') ctx.refresh();
      else if (e.key === 'l') { var q = rel.querySelector('.vw-dx-q'); if (q) q.focus(); }
      else if (e.key === 'n') { var ta = notes.querySelector('.vw-dx-nt'); if (ta) ta.focus(); }
      else if (e.key === '/') { var s = bd.querySelector('.vw-dive-sec vera-widget'); var inp = s && (s.shadowRoot || s).querySelector && (s.shadowRoot || s).querySelector('input'); if (inp) inp.focus(); }
      else if (e.key === 'b') { cur.brush = null; applyBrush(); }
      else return;
      e.preventDefault(); e.stopPropagation();
    }
    document.addEventListener('keydown', onKey, true);

    drawProv(); drawBrush(); drawDiff(); drawRel(); drawNotes(); ctxShown = cur.rows.length;
    return {
      // the dive read again: freshness, the diff against the last read, the brush kept
      answered: function (data) { cur.prevRows = cur.rows; cur.data = data; cur.rows = ctx.rows(data); cur.at = Date.now(); drawDiff(); applyBrush(); },
      addLayer: addLayer, state: cur,
      // the sheet closed: its key listener and the freshness tick go with it (vera-d0's review)
      destroy: function () { document.removeEventListener('keydown', onKey, true); clearInterval(tick); }
    };
  }
  api.enhance = enhance;
})(typeof window !== 'undefined' ? window : globalThis);
