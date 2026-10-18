/* VeraMatrix - THE matrix (looplab-control §9 Phase K2, 18 Oct 2026; backbone B5): tiles where things repeat - tests x
   runs, branches x stages, goals x runs, models x measures, nodes x hours. Browser (window.VeraMatrix + <vera-matrix>)
   and Node (module.exports); the pure half is the same in both.

   A matrix is a RECORD:
     { id: "census-goals", source: { cap: "census.compare", args: {} },
       map: { cells: "rows", row: "goal", col: "run", value: "status", tip: "{goal} · {run} · {status}",
              cols_order: "run" },                                 // a field to order columns by (default: first seen)
       colour: { "done": "#3fae6c", "partial": "#e0a458", "failed": "#f87171", "*": "#5a6270" }  // value -> colour,
                                                                    // or { scale: [lo, hi], good: "high"|"low" } for numbers
       ok: ["done"],                                               // values that count as green for the row % / col totals
       group: "",  sort: "failures" | "name" | "",  window: 14 }    // group rows by a field; keep the last N columns

     problems(rec) / cellsOf(rec, answer) / layout(rec, cells) -> {rows, cols, grid, rowStats, colStats, groups}
     colourOf(rec, value) / render(rec, layout, {selected}) -> html

   <vera-matrix record='{...}'> reads the source, draws, and emits matrix:select {row, col, cell} on a click. No controls
   on the face (controls-not-in-display-widgets); grouping / sorting / the window are the record's, set by the host. */
(function (root) {
  'use strict';
  if (root.VeraMatrix && typeof root.VeraMatrix.layout === 'function') {
    if (typeof module !== 'undefined' && module.exports) module.exports = root.VeraMatrix;
    return;
  }

  function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function pick(obj, path) {
    if (!path) return obj;
    var cur = obj, parts = String(path).split('.');
    for (var i = 0; i < parts.length; i++) { if (cur == null) return undefined; cur = cur[parts[i]]; }
    return cur;
  }
  function fill(tpl, raw) { return String(tpl || '').replace(/\{(\w+(?:\.\w+)*)\}/g, function (_, p) { var v = pick(raw, p); return v == null ? '' : String(v); }); }

  function problems(rec) {
    var out = [];
    if (!isObj(rec)) return ['a matrix is an object'];
    var m = rec.map || {};
    if (!m.row) out.push('map.row: the field naming a row');
    if (!m.col) out.push('map.col: the field naming a column');
    if (rec.source != null && !(isObj(rec.source) && rec.source.cap)) out.push('source: {cap, args}');
    return out;
  }

  function cellsOf(rec, answer) {
    var m = (rec && rec.map) || {};
    var list = m.cells ? pick(answer, m.cells) : answer;
    if (!Array.isArray(list)) return [];
    return list.filter(function (r) { return r && pick(r, m.row) != null && pick(r, m.col) != null; }).map(function (r) {
      return { row: String(pick(r, m.row)), col: String(pick(r, m.col)), value: m.value ? pick(r, m.value) : true,
               order: m.cols_order ? pick(r, m.cols_order) : null, group: rec.group ? String(pick(r, rec.group) == null ? '' : pick(r, rec.group)) : '',
               tip: m.tip ? fill(m.tip, r) : '', raw: r };
    });
  }

  var SCALE = [[47, 138, 85], [122, 186, 100], [224, 196, 88], [224, 140, 70], [220, 80, 80]];
  function colourOf(rec, v) {
    var c = (rec && rec.colour) || {};
    if (Array.isArray(c.scale) && typeof v === 'number') {
      var lo = c.scale[0], hi = c.scale[1], t = hi === lo ? 0 : Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
      if (c.good === 'high') t = 1 - t;
      var i = Math.min(SCALE.length - 2, Math.floor(t * (SCALE.length - 1))), f = t * (SCALE.length - 1) - i;
      var rgb = [0, 1, 2].map(function (k) { return Math.round(SCALE[i][k] + (SCALE[i + 1][k] - SCALE[i][k]) * f); });
      return 'rgb(' + rgb.join(',') + ')';
    }
    var key = String(v);
    return c[key] || c['*'] || '#3a4250';
  }
  function isOk(rec, v) { var ok = rec && rec.ok; return Array.isArray(ok) ? ok.indexOf(v) >= 0 : !!v && v !== 'failed' && v !== false; }

  function layout(rec, cells) {
    var cols = [], colOrder = {}, rows = [], grid = {};
    cells.forEach(function (c) {
      if (!(c.col in colOrder)) { colOrder[c.col] = c.order != null ? c.order : cols.length; cols.push(c.col); }
      if (rows.indexOf(c.row) < 0) rows.push(c.row);
      grid[c.row + '\u0000' + c.col] = c;                       // the last cell for a (row, col) wins
    });
    cols.sort(function (a, b) { var x = colOrder[a], y = colOrder[b]; return x < y ? -1 : x > y ? 1 : 0; });
    if (rec.window > 0 && cols.length > rec.window) cols = cols.slice(cols.length - rec.window);
    var rowStats = {}, colStats = {};
    rows.forEach(function (r) {
      var n = 0, ok = 0;
      cols.forEach(function (c) { var cell = grid[r + '\u0000' + c]; if (cell) { n++; if (isOk(rec, cell.value)) ok++; } });
      rowStats[r] = { n: n, ok: ok, pct: n ? Math.round(100 * ok / n) : null };
    });
    cols.forEach(function (c) { var ok = 0; rows.forEach(function (r) { var cell = grid[r + '\u0000' + c]; if (cell && isOk(rec, cell.value)) ok++; }); colStats[c] = { ok: ok }; });
    if (rec.sort === 'failures') rows.sort(function (a, b) { return (rowStats[a].pct == null ? 101 : rowStats[a].pct) - (rowStats[b].pct == null ? 101 : rowStats[b].pct) || (a < b ? -1 : 1); });
    else if (rec.sort === 'name') rows.sort();
    var groups = [];
    if (rec.group) {
      var g = {};
      cells.forEach(function (c) { if (!(c.row in g)) g[c.row] = c.group; });
      var names = []; rows.forEach(function (r) { if (names.indexOf(g[r]) < 0) names.push(g[r]); });
      groups = names.map(function (n) { return { name: n, rows: rows.filter(function (r) { return g[r] === n; }) }; });
    } else groups = [{ name: '', rows: rows }];
    return { rows: rows, cols: cols, grid: grid, rowStats: rowStats, colStats: colStats, groups: groups };
  }

  function render(rec, L, o) {
    o = o || {};
    var sel = o.selected || {}, nc = L.cols.length, stats = Array.isArray(rec.ok);   /* % and totals only when 'ok' says what green is */
    if (!L.rows.length) return '<div class="vm-empty">nothing to show yet</div>';
    var head = '<div></div>' + L.cols.map(function (c) { return '<div class="vm-ch" title="' + esc(c) + '">' + esc(c) + '</div>'; }).join('') + '<div></div>';
    var body = L.groups.map(function (g) {
      return (g.name ? '<div class="vm-g" style="grid-column:1/-1">' + esc(g.name) + '</div>' : '') + g.rows.map(function (r) {
        var st = L.rowStats[r], pc = st.pct, pcc = pc == null ? '#8a93a0' : pc >= 85 ? '#3fae6c' : pc >= 60 ? '#e0a458' : '#f87171';
        return '<div class="vm-rh' + (sel.row === r ? ' on' : '') + '" title="' + esc(r) + '">' + esc(r) + '</div>' +
          L.cols.map(function (c) {
            var cell = L.grid[r + '\u0000' + c];
            if (!cell) return '<div class="vm-c vm-none"></div>';
            var on = sel.row === r && sel.col === c;
            return '<div class="vm-c' + (on ? ' on' : '') + '" data-r="' + esc(r) + '" data-c="' + esc(c) + '" title="' + esc(cell.tip || (r + ' · ' + c + ' · ' + cell.value)) + '" style="background:' + colourOf(rec, cell.value) + '"></div>';
          }).join('') + '<div class="vm-rs" style="color:' + pcc + '">' + (pc == null || !stats ? '' : pc + '%') + '</div>';
      }).join('');
    }).join('');
    var foot = stats ? '<div class="vm-fh">green</div>' + L.cols.map(function (c) { return '<div class="vm-cs">' + L.colStats[c].ok + '</div>'; }).join('') + '<div></div>' : '';
    return '<div class="vm" style="display:grid;grid-template-columns:minmax(90px,max-content) repeat(' + nc + ',minmax(10px,1fr)) 40px;gap:3px;align-items:center">' + head + body + foot + '</div>';
  }

  var api = { problems: problems, cellsOf: cellsOf, layout: layout, colourOf: colourOf, render: render, isOk: isOk };
  root.VeraMatrix = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  /* registered in vera/ui/vera_item.js's element registry (DO-0.2), not here: a bare registerBody under a block's name replaced that block's body */
  if (typeof root.customElements === 'undefined' || typeof root.HTMLElement === 'undefined') return;

  var CSS = 'vera-matrix{display:block;font-size:11.5px}vera-matrix .vm-ch{text-align:center;color:#8a93a0;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    'vera-matrix .vm-rh{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}vera-matrix .vm-rh.on{color:#fff;font-weight:600}' +
    'vera-matrix .vm-c{height:16px;border-radius:3px;cursor:pointer;transition:transform .15s}vera-matrix .vm-c:hover{transform:scale(1.3);position:relative;z-index:2}' +
    'vera-matrix .vm-c.on{outline:2px solid #fff;outline-offset:1px}vera-matrix .vm-none{background:#1d2430;cursor:default}' +
    'vera-matrix .vm-rs,vera-matrix .vm-cs{font-family:ui-monospace,monospace;font-size:10.5px;text-align:center;color:#8a93a0}' +
    'vera-matrix .vm-g{font-size:10.5px;letter-spacing:.06em;color:#8a93a0;font-weight:600;padding-top:6px;text-transform:uppercase}vera-matrix .vm-empty{color:#8a93a0}';

  class VeraMatrixEl extends root.HTMLElement {
    constructor() { super(); this._rec = null; this._cells = []; this._sel = {}; }
    static get observedAttributes() { return ['record']; }
    connectedCallback() { this._init(); }
    attributeChangedCallback() { if (this.isConnected) this._init(); }
    set record(r) { this._rec = r; this._read(); }
    get record() { return this._rec; }
    _init() { try { this._rec = JSON.parse(this.getAttribute('record') || 'null'); } catch (_) { this._rec = null; } this._read(); }
    async _read() {
      const rec = this._rec; if (!rec) return;
      if (rec.data) { this._cells = cellsOf(rec, rec.data); this._draw(); return; }
      if (!rec.source) return;
      try {
        const r = await fetch((root._veraBase || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': 'matrix; trigger=auto' },
                              body: JSON.stringify({ name: rec.source.cap, arguments: rec.source.args || {} }) });
        const j = await r.json(); const ans = j && j.content !== undefined ? j.content : j;
        this._cells = ans && ans.error ? [] : cellsOf(rec, ans); this._err = ans && ans.error ? String(ans.error) : '';
      } catch (e) { this._cells = []; this._err = String(e.message || e); }
      this._draw();
    }
    _draw() {
      const L = layout(this._rec, this._cells);
      this.innerHTML = '<style>' + CSS + '</style>' + (this._err ? '<div class="vm-empty">could not read ' + esc(this._rec.source.cap) + ': ' + esc(this._err) + '</div>' : render(this._rec, L, { selected: this._sel }));
      this.querySelectorAll('.vm-c[data-r]').forEach((el) => el.addEventListener('click', () => {
        const r = el.getAttribute('data-r'), c = el.getAttribute('data-c'); this._sel = { row: r, col: c };
        const cell = L.grid[r + '\u0000' + c];
        this.dispatchEvent(new CustomEvent('matrix:select', { bubbles: true, composed: true, detail: { row: r, col: c, cell: cell && cell.raw } }));
        this._draw();
      }));
    }
  }
  if (!root.customElements.get('vera-matrix')) root.customElements.define('vera-matrix', VeraMatrixEl);
})(typeof window !== 'undefined' ? window : globalThis);
