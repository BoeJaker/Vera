/* VeraTimeline - THE timeline (looplab-control §9 Phase K1, 18 Oct 2026; backbone B5: the desk's Wave 5 moves its five
   timelines onto this one). Browser (window.VeraTimeline + <vera-timeline>) and Node (module.exports) - the pure half
   is the same in both.

   A timeline is an axis and a stack of LANES. A lane is a RECORD a family registers once - it never needs a change here:

     { lane: "errors", label: "Errors", group: "Health", type: "strip",
       source: { cap: "obs.events", args: { level: "error" } },      // read through /mcp/call (quiet caps only)
       map: { items: "events", t: "ts", end: "", value: "count", label: "title", color: "#f87171",
              tip: "{title} · {ts}" },
       drill: { cap: "obs.events", args: { from: "{t}" } },           // what a click opens (the host decides how)
       live: 30 }                                                    // seconds between reads; 0 = read once

   map.where {field: value|[values]} keeps matching rows (one source, several lanes); map.color is a colour, or
   {field, map?, else?} - the field's value looked up in map, or the field's own value when there is no map.

   Types: band (a window), bar (a run: t..end), mark (an event), strip (a metric over time), tiles (a matrix cell group),
   chip (an item on its day), graph (branches: items carry `track` and `parent`). registerLaneType adds more.

     registerLane(spec)       -> the normalised spec (or null with problems in .problems)
     lanes() / lane(id)       -> the registry
     registerLaneType(t, fn)  -> fn(items, ctx) -> svg string; ctx = {x, y0, h, w, color, lane}
     itemsOf(spec, answer)    -> the source's answer through spec.map: [{t, end, value, label, color, tip, track, raw}]
     scale(range, width)      -> {x(t), t(x), from, to}
     render(model)            -> html string: {range:{from,to,now}, width, lanes:[{spec, items}], selected?, overlay?}
                                 overlay = guide lines across every lane: [{t, end?, color, label, anchor?}]
     problems(spec)           -> why a spec is not a lane ([] when it is)

   <vera-timeline lanes="errors,code" from="-24h" to="+12h" height="34">: reads each lane's source, draws, re-reads on
   each lane's live interval, and emits bubbling events timeline:select {lane, item} (click), timeline:brush {from, to}
   (drag across), timeline:hover {lane, item}. .addLane(spec|id), .removeLane(id), .setRange(from, to), .lanes.
   No controls are drawn on the face (controls-not-in-display-widgets): the host puts them in its drill panel. */
(function (root) {
  'use strict';
  if (root.VeraTimeline && typeof root.VeraTimeline.registerLane === 'function') {
    if (typeof module !== 'undefined' && module.exports) module.exports = root.VeraTimeline;
    return;
  }

  var TYPES = {};
  var LANES = {};
  var TYPE_NAMES = ['band', 'bar', 'mark', 'strip', 'tiles', 'chip', 'graph'];
  var LABEL_W = 150;

  function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function pick(obj, path) {
    if (!path) return obj;
    var cur = obj, parts = String(path).split('.');
    for (var i = 0; i < parts.length; i++) {
      if (cur == null) return undefined;
      var m = /^(\w+)\[(-?\d+)\]$/.exec(parts[i]);
      if (m) { cur = cur[m[1]]; if (!Array.isArray(cur)) return undefined; var k = +m[2]; cur = cur[k < 0 ? cur.length + k : k]; }
      else cur = cur[parts[i]];
    }
    return cur;
  }
  function toMs(v) {
    if (v == null || v === '') return null;
    if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;          // epoch seconds or ms
    var t = Date.parse(String(v)); return isNaN(t) ? null : t;
  }
  /* a time the element itself holds is already milliseconds; only SOURCE data may be epoch seconds (toMs) */
  function msOf(v) { return typeof v === 'number' ? v : toMs(v); }
  function fill(tpl, raw) { return String(tpl || '').replace(/\{(\w+(?:\.\w+)*)\}/g, function (_, p) { var v = pick(raw, p); return v == null ? '' : String(v); }); }

  function problems(spec) {
    var out = [];
    if (!isObj(spec)) return ['a lane is an object'];
    if (!spec.lane || !/^[a-z0-9_.:-]+$/i.test(String(spec.lane))) out.push('lane: an id of letters, digits, _ . : -');
    if (!TYPES[spec.type] && TYPE_NAMES.indexOf(spec.type) < 0) out.push('type: one of ' + Object.keys(TYPES).join(', '));
    if (spec.source != null && !(isObj(spec.source) && spec.source.cap)) out.push('source: {cap, args}');
    if (spec.map != null && !isObj(spec.map)) out.push('map: an object of field paths');
    return out;
  }
  function registerLane(spec) {
    var p = problems(spec);
    if (p.length) { registerLane.lastProblems = p; return null; }
    var s = { lane: String(spec.lane), label: String(spec.label || spec.lane), group: String(spec.group || ''),
              type: String(spec.type), sub: String(spec.sub || ''), color: String(spec.color || (spec.map && spec.map.color) || '#7aa2f7'),
              source: spec.source ? { cap: String(spec.source.cap), args: isObj(spec.source.args) ? spec.source.args : {} } : null,
              map: isObj(spec.map) ? spec.map : {}, drill: isObj(spec.drill) ? spec.drill : null,
              live: Math.max(0, +spec.live || 0), height: Math.max(18, Math.min(120, +spec.height || 34)) };
    LANES[s.lane] = s; return s;
  }
  function lanes() { return Object.keys(LANES).sort().map(function (k) { return LANES[k]; }); }
  function lane(id) { return LANES[String(id)] || null; }
  function registerLaneType(t, fn) { if (t && typeof fn === 'function') TYPES[String(t)] = fn; }

  /* map.where: {field: value | [values]} - keep only the answer's rows that match (one source, several lanes) */
  function matches(raw, where) {
    return Object.keys(where).every(function (k) { var v = pick(raw, k), w = where[k]; return Array.isArray(w) ? w.indexOf(v) >= 0 : v === w; });
  }
  function itemsOf(spec, answer) {
    var m = (spec && spec.map) || {};
    var list = m.items ? pick(answer, m.items) : answer;
    if (!Array.isArray(list)) list = isObj(list) ? Object.keys(list).map(function (k) { return list[k]; }) : [];
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var raw = list[i]; if (raw == null) continue;
      if (isObj(m.where) && !matches(raw, m.where)) continue;
      var t = toMs(pick(raw, m.t || 't'));
      if (t == null) continue;
      var end = m.end ? toMs(pick(raw, m.end)) : null;
      var color = m.color && typeof m.color === 'object' && m.color.field ? (m.color.map ? m.color.map[pick(raw, m.color.field)] : pick(raw, m.color.field)) || m.color.else || spec.color
        : (typeof m.color === 'string' ? m.color : spec.color);
      out.push({ t: t, end: end, value: m.value ? +pick(raw, m.value) || 0 : null, label: m.label ? String(pick(raw, m.label) == null ? '' : pick(raw, m.label)) : '',
                 color: color || spec.color, tip: m.tip ? fill(m.tip, raw) : '', track: m.track ? String(pick(raw, m.track) == null ? '' : pick(raw, m.track)) : '',
                 parent: m.parent ? String(pick(raw, m.parent) == null ? '' : pick(raw, m.parent)) : '', id: m.id ? String(pick(raw, m.id)) : String(i), raw: raw });
    }
    out.sort(function (a, b) { return a.t - b.t; });
    return out;
  }

  function scale(range, width) {
    var from = msOf(range.from), to = msOf(range.to), w = Math.max(1, (width || 1000) - LABEL_W);
    var span = Math.max(1, to - from);
    return { from: from, to: to, w: w,
             x: function (t) { return LABEL_W + Math.max(0, Math.min(w, (t - from) * w / span)); },
             t: function (x) { return from + (x - LABEL_W) * span / w; } };
  }

  /* the seven lane types: each draws its items into its row (y0 .. y0+h) and returns svg */
  function mark(it, ctx, i) { return ' data-lane="' + esc(ctx.lane.lane) + '" data-i="' + i + '"><title>' + esc(it.tip || it.label) + '</title>'; }
  registerLaneType('band', function (items, ctx) {
    return items.map(function (it, i) { var x0 = ctx.x(it.t), x1 = ctx.x(it.end || it.t + 3600e3);
      return '<rect x="' + x0.toFixed(1) + '" y="' + (ctx.y0 + 4) + '" width="' + Math.max(2, x1 - x0).toFixed(1) + '" height="' + (ctx.h - 8) + '" rx="3" fill="' + esc(it.color) + '" fill-opacity=".28"' + mark(it, ctx, i) + '</rect>'; }).join('');
  });
  registerLaneType('bar', function (items, ctx) {
    return items.map(function (it, i) { var x0 = ctx.x(it.t), x1 = ctx.x(it.end || ctx.now || it.t + 600e3), w = Math.max(3, x1 - x0);
      var running = !it.end;
      return '<g><rect x="' + x0.toFixed(1) + '" y="' + (ctx.y0 + 5) + '" width="' + w.toFixed(1) + '" height="' + (ctx.h - 10) + '" rx="3" fill="' + esc(it.color) + '"' + (running ? ' class="vt-run"' : '') + mark(it, ctx, i) + '</rect>' +
        (w > 46 && it.label ? '<text x="' + (x0 + 4).toFixed(1) + '" y="' + (ctx.y0 + ctx.h / 2 + 4) + '" font-size="10.5" fill="#0c0f13" font-weight="600" pointer-events="none">' + esc(it.label.slice(0, Math.floor(w / 6))) + '</text>' : '') + '</g>'; }).join('');
  });
  registerLaneType('mark', function (items, ctx) {
    return items.map(function (it, i) { var x = ctx.x(it.t), g = it.label && it.label.length <= 2 ? it.label : '◆';
      return '<text x="' + x.toFixed(1) + '" y="' + (ctx.y0 + ctx.h / 2 + 5) + '" font-size="13" text-anchor="middle" fill="' + esc(it.color) + '"' + mark(it, ctx, i) + esc(g) + '</text>'; }).join('');
  });
  registerLaneType('strip', function (items, ctx) {
    if (!items.length) return '';
    var vals = items.map(function (it) { return it.value || 0; }), mx = Math.max.apply(null, vals.concat([1e-9])), mn = Math.min.apply(null, vals.concat([0]));
    var pts = items.map(function (it) { return ctx.x(it.t).toFixed(1) + ',' + (ctx.y0 + ctx.h - 4 - ((it.value || 0) - mn) * (ctx.h - 10) / ((mx - mn) || 1)).toFixed(1); });
    var area = 'M ' + pts[0].split(',')[0] + ' ' + (ctx.y0 + ctx.h - 3) + ' L ' + pts.join(' L ') + ' L ' + pts[pts.length - 1].split(',')[0] + ' ' + (ctx.y0 + ctx.h - 3) + ' Z';
    return '<path d="' + area + '" fill="' + esc(ctx.color) + '" fill-opacity=".14"/><polyline points="' + pts.join(' ') + '" fill="none" stroke="' + esc(ctx.color) + '" stroke-width="1.6"/>' +
      items.map(function (it, i) { var p = pts[i].split(','); return '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="6" fill="transparent"' + mark(it, ctx, i) + '</circle>'; }).join('');
  });
  registerLaneType('tiles', function (items, ctx) {
    return items.map(function (it, i) { var x = ctx.x(it.t), cells = Array.isArray(pick(it.raw, 'cells')) ? pick(it.raw, 'cells') : [it.color], cols = 6;
      return '<g' + mark(it, ctx, i) + cells.map(function (c, k) { return '<rect x="' + (x + (k % cols) * 6).toFixed(1) + '" y="' + (ctx.y0 + 6 + Math.floor(k / cols) * 6) + '" width="5" height="5" rx="1" fill="' + esc(c) + '"/>'; }).join('') + '</g>'; }).join('');
  });
  registerLaneType('chip', function (items, ctx) {
    return items.map(function (it, i) { var x0 = ctx.x(it.t), x1 = it.end ? ctx.x(it.end) : x0 + Math.min(160, 8 + 6 * (it.label || '').length);
      return '<g' + mark(it, ctx, i) + '<rect x="' + x0.toFixed(1) + '" y="' + (ctx.y0 + 6) + '" width="' + Math.max(12, x1 - x0).toFixed(1) + '" height="' + (ctx.h - 12) + '" rx="4" fill="#1b222c" stroke="' + esc(it.color) + '"/>' +
        '<text x="' + (x0 + 5).toFixed(1) + '" y="' + (ctx.y0 + ctx.h / 2 + 4) + '" font-size="10.5" fill="#e4e7ec">' + esc((it.label || '').slice(0, 28)) + '</text></g>'; }).join('');
  });
  registerLaneType('graph', function (items, ctx) {
    var tracks = []; items.forEach(function (it) { if (tracks.indexOf(it.track) < 0) tracks.push(it.track); });
    var gap = Math.max(6, (ctx.h - 8) / Math.max(1, tracks.length)), yOf = function (tr) { return ctx.y0 + 4 + gap * (tracks.indexOf(tr) + 0.5); };
    var lines = tracks.map(function (tr) { return '<line x1="' + LABEL_W + '" x2="' + (LABEL_W + ctx.w) + '" y1="' + yOf(tr).toFixed(1) + '" y2="' + yOf(tr).toFixed(1) + '" stroke="' + esc(ctx.color) + '" stroke-opacity=".3" stroke-width="2"/>'; }).join('');
    var byId = {}; items.forEach(function (it) { byId[it.id] = it; });
    var links = items.filter(function (it) { return it.parent && byId[it.parent]; }).map(function (it) { var p = byId[it.parent];
      return '<path d="M ' + ctx.x(p.t).toFixed(1) + ' ' + yOf(p.track).toFixed(1) + ' C ' + ctx.x(it.t).toFixed(1) + ' ' + yOf(p.track).toFixed(1) + ', ' + ctx.x(p.t).toFixed(1) + ' ' + yOf(it.track).toFixed(1) + ', ' + ctx.x(it.t).toFixed(1) + ' ' + yOf(it.track).toFixed(1) + '" fill="none" stroke="' + esc(it.color) + '" stroke-width="1.6"/>'; }).join('');
    return lines + links + items.map(function (it, i) { return '<circle cx="' + ctx.x(it.t).toFixed(1) + '" cy="' + yOf(it.track).toFixed(1) + '" r="3.6" fill="' + esc(it.color) + '"' + mark(it, ctx, i) + '</circle>'; }).join('');
  });

  /* the items that overlap the range: what is drawn, and what a click's index refers to */
  function visible(items, s) { return (items || []).filter(function (it) { return (it.end || it.t) >= s.from && it.t <= s.to; }); }

  function render(model) {
    var W = model.width || 1000, s = scale(model.range, W), now = msOf(model.range.now) || Date.now();
    var y = 22, rows = '', defs = '<pattern id="vt-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="4" height="8" fill="rgba(122,162,247,.06)"/></pattern>';
    (model.lanes || []).forEach(function (L) {
      var spec = L.spec, h = spec.height, ctx = { x: s.x, y0: y, h: h, w: s.w, color: spec.color, lane: spec, now: now };
      var fn = TYPES[spec.type] || TYPES.mark;
      rows += '<g class="vt-lane" data-lane="' + esc(spec.lane) + '"><rect x="0" y="' + y + '" width="' + W + '" height="' + h + '" fill="' + (L.index % 2 ? '#12161c' : '#141a21') + '"/>' +
        '<rect x="0" y="' + y + '" width="3" height="' + h + '" fill="' + esc(spec.color) + '"/>' +
        '<text x="10" y="' + (y + 14) + '" font-size="11.5" font-weight="600" fill="#e4e7ec">' + esc(spec.label) + '</text>' +
        (spec.sub ? '<text x="10" y="' + (y + 26) + '" font-size="9.5" fill="#8a93a0">' + esc(spec.sub) + '</text>' : '') +
        (L.error ? '<text x="' + (LABEL_W + 6) + '" y="' + (y + h / 2 + 4) + '" font-size="10.5" fill="#f87171">' + esc('could not read ' + (spec.source && spec.source.cap) + ': ' + L.error) + '</text>' : fn(visible(L.items, s), ctx)) + '</g>';
      y += h + 1;
    });
    var ticks = '', span = s.to - s.from, step = [3600e3, 3 * 3600e3, 6 * 3600e3, 24 * 3600e3, 7 * 24 * 3600e3].filter(function (v) { return span / v <= 12; })[0] || 7 * 24 * 3600e3;
    for (var t = Math.ceil(s.from / step) * step; t <= s.to; t += step) {
      var d = new Date(t), lab = step >= 24 * 3600e3 ? d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' }) : String(d.getHours()).padStart(2, '0') + ':00';
      ticks += '<line x1="' + s.x(t).toFixed(1) + '" x2="' + s.x(t).toFixed(1) + '" y1="16" y2="' + y + '" stroke="#1f252e" stroke-dasharray="3 4"/><text x="' + (s.x(t) + 3).toFixed(1) + '" y="12" font-size="10" fill="#8a93a0">' + esc(lab) + '</text>';
    }
    var nx = s.x(now), fut = now < s.to ? '<rect x="' + nx.toFixed(1) + '" y="16" width="' + (LABEL_W + s.w - nx).toFixed(1) + '" height="' + (y - 16) + '" fill="url(#vt-hatch)" pointer-events="none"/>' : '';
    var nowl = now >= s.from && now <= s.to ? '<line class="vt-now" x1="' + nx.toFixed(1) + '" x2="' + nx.toFixed(1) + '" y1="14" y2="' + y + '" stroke="#e0a458" stroke-width="2"/><text x="' + (nx + 4).toFixed(1) + '" y="12" font-size="10" fill="#e0a458" font-weight="600">now</text>' : '';
    var sel = model.selected && model.selected.t ? '<line x1="' + s.x(msOf(model.selected.t)).toFixed(1) + '" x2="' + s.x(msOf(model.selected.t)).toFixed(1) + '" y1="16" y2="' + y + '" stroke="#7aa2f7" stroke-width="1.5"/>' : '';
    /* model.overlay: guide lines across every lane - [{t, end?, color, label, anchor?}] (Compare's pinned items);
       labels step up in three rows so pins close together stay readable */
    var ov = (model.overlay || []).map(function (o, i) {
      var t = msOf(o.t); if (t == null || t < s.from || t > s.to) return '';
      var x = s.x(t), c = esc(o.color || '#a78bfa'), band = o.end && msOf(o.end) > t ? '<rect x="' + x.toFixed(1) + '" y="16" width="' + Math.max(1, s.x(msOf(o.end)) - x).toFixed(1) + '" height="' + (y - 16) + '" fill="' + c + '" fill-opacity=".07" pointer-events="none"/>' : '';
      return band + '<line class="vt-guide" x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="16" y2="' + y + '" stroke="' + c + '" stroke-width="' + (o.anchor ? 2.2 : 1.2) + '"' + (o.anchor ? '' : ' stroke-dasharray="4 3"') + ' pointer-events="none"/>' +
        '<text x="' + (x + 3).toFixed(1) + '" y="' + (y - 4 - (i % 3) * 11) + '" font-size="9.5" fill="' + c + '" pointer-events="none">' + esc(String(o.label || '').slice(0, 28)) + '</text>';
    }).join('');
    return '<svg class="vt" viewBox="0 0 ' + W + ' ' + (y + 2) + '" width="100%" style="display:block;font-family:inherit"><defs>' + defs + '</defs>' + ticks + rows + fut + ov + sel + nowl + '</svg>';
  }

  var api = { registerLane: registerLane, lanes: lanes, lane: lane, registerLaneType: registerLaneType, types: function () { return Object.keys(TYPES).sort(); },
              itemsOf: itemsOf, scale: scale, render: render, visible: visible, problems: problems, pick: pick, toMs: toMs, LABEL_W: LABEL_W };
  root.VeraTimeline = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  /* the item contract (the desk's ADR): THE timeline is a body every surface can host */
  /* registered in vera/ui/vera_item.js's element registry (DO-0.2), not here: a bare registerBody under a block's name replaced that block's body */

  if (typeof root.customElements === 'undefined' || typeof root.HTMLElement === 'undefined') return;

  function rel(v, now) {           /* "-24h" / "+12h" / "-7d" / an ISO time */
    var m = /^([+-])(\d+(?:\.\d+)?)([hdm])$/.exec(String(v || '').trim());
    if (!m) return toMs(v) || now;
    var n = +m[2] * ({ m: 60e3, h: 3600e3, d: 86400e3 })[m[3]];
    return m[1] === '-' ? now - n : now + n;
  }

  /* lanes that share a source share one read: in flight it is one request, and an answer is reused for 5 s */
  var READS = {};
  function readSource(src) {
    var key = JSON.stringify([src.cap, src.args || {}]), now = Date.now(), hit = READS[key];
    if (hit && (hit.pending || now - hit.at < 5000)) return hit.p;
    var p = fetch((root._veraBase || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': 'timeline; trigger=auto' },
                  body: JSON.stringify({ name: src.cap, arguments: src.args || {} }) })
      .then(function (r) { return r.json(); }).then(function (j) {
        // an answer with no content is a failed read (e.g. 404 unknown capability), never an empty lane
        if (j && j.content === undefined && (j.detail || j.error)) throw new Error(String(j.detail || j.error));
        return j && j.content !== undefined ? j.content : j; });
    var rec = { p: p, pending: true, at: now }; READS[key] = rec;
    p.then(function () { rec.pending = false; rec.at = Date.now(); }, function () { delete READS[key]; });
    return p;
  }

  class VeraTimelineEl extends root.HTMLElement {
    constructor() { super(); this._lanes = []; this._data = {}; this._timers = {}; this._sel = null; }
    static get observedAttributes() { return ['lanes', 'from', 'to']; }
    connectedCallback() { this._init(); }
    disconnectedCallback() { Object.keys(this._timers).forEach((k) => clearInterval(this._timers[k])); this._timers = {}; }
    attributeChangedCallback() { if (this.isConnected) this._init(); }
    get lanes() { return this._lanes.map((l) => l.lane); }
    addLane(spec) { const s = typeof spec === 'string' ? lane(spec) : registerLane(spec); if (s && !this._lanes.some((l) => l.lane === s.lane)) { this._lanes.push(s); this._read(s); } return s; }
    removeLane(id) { this._lanes = this._lanes.filter((l) => l.lane !== id); clearInterval(this._timers[id]); delete this._timers[id]; this._draw(); }
    setRange(from, to) { this.setAttribute('from', from); this.setAttribute('to', to); }
    _range() { const now = Date.now(); return { from: rel(this.getAttribute('from') || '-24h', now), to: rel(this.getAttribute('to') || '+12h', now), now: now }; }
    _init() {
      this.disconnectedCallback();
      this._lanes = String(this.getAttribute('lanes') || '').split(',').map((x) => x.trim()).filter(Boolean).map(lane).filter(Boolean);
      this._lanes.forEach((s) => this._read(s));
      this._draw();
    }
    async _read(s) {
      if (!s.source) { this._draw(); return; }
      const go = async () => {
        try {
          const ans = await readSource(s.source);
          this._data[s.lane] = ans && ans.error ? { error: String(ans.error).slice(0, 120) } : { items: itemsOf(s, ans) };
        } catch (e) { this._data[s.lane] = { error: String(e.message || e).slice(0, 120) }; }
        this._draw();
      };
      await go();
      if (s.live && !this._timers[s.lane]) this._timers[s.lane] = setInterval(go, s.live * 1000);
    }
    _draw() {
      const w = Math.max(400, this.clientWidth || 1000);
      this.innerHTML = '<style>vera-timeline{display:block}vera-timeline .vt-run{animation:vt-p 1.6s ease-in-out infinite}@keyframes vt-p{50%{opacity:.5}}vera-timeline [data-i]{cursor:pointer}</style>' +
        render({ width: w, range: this._range(), selected: this._sel, lanes: this._lanes.map((s, i) => Object.assign({ spec: s, index: i }, this._data[s.lane] || { items: [] })) });
      const svg = this.querySelector('svg'); if (!svg) return;
      const at = (ev) => { const b = svg.getBoundingClientRect(); return (ev.clientX - b.left) * w / b.width; };
      svg.addEventListener('click', (ev) => { const el = ev.target.closest('[data-i]'); if (!el) return;
        const ln = el.getAttribute('data-lane'), it = visible((this._data[ln] || {}).items, scale(this._range(), w))[+el.getAttribute('data-i')];
        if (!it) return; this._sel = { t: it.t }; this._draw();
        this.dispatchEvent(new CustomEvent('timeline:select', { bubbles: true, composed: true, detail: { lane: ln, item: it.raw, t: it.t, spec: lane(ln) } })); });
      let x0 = null;
      svg.addEventListener('mousedown', (ev) => { if (!ev.target.closest('[data-i]')) x0 = at(ev); });
      svg.addEventListener('mouseup', (ev) => { if (x0 == null) return; const x1 = at(ev); const sc = scale(this._range(), w);
        if (Math.abs(x1 - x0) > 6) this.dispatchEvent(new CustomEvent('timeline:brush', { bubbles: true, composed: true, detail: { from: sc.t(Math.min(x0, x1)), to: sc.t(Math.max(x0, x1)) } }));
        x0 = null; });
    }
  }
  if (!root.customElements.get('vera-timeline')) root.customElements.define('vera-timeline', VeraTimelineEl);
})(typeof window !== 'undefined' ? window : globalThis);
