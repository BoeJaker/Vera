/* VeraCompare - THE compare workspace (looplab-control §9 Phase K4, 18 Oct 2026). Browser (window.VeraCompare +
   <vera-compare>) and Node (module.exports) - the pure half is the same in both. Draws with THE timeline
   (VeraTimeline.render) and opens items in THE drill panel (<vera-drill>, K3); it has no chart code of its own.

   A PIN is a record - pinned from any surface, kept per viewer:
     { pin: "lane:ll.runs",  kind: "lane", lane: <lane record or id>, label }                       a whole lane
     { pin: "item:event:x",  kind: "item", lane: <lane record or id>, t, end?, label, drill?: "event:x", color? }
   Pins never carry data: a pinned lane is re-read from its own source, so a pin survives a reload.

   Modes (the owner's "side by side / stacked / layered", plus aligned on an event):
     stack  - every pinned lane its own row on one axis; pinned items are guide lines across all of them
     layer  - every pinned lane drawn into ONE composite row, each in its colour
     side   - the pinned items' drill frames in columns
   anchor - a pinned item: the axis centres on it (± window) and the insight rows say, per lane, what changed
            before vs after it, with the nearest items as evidence ("a pinned diff, setting, test and usage chart
            line up on one axis").

     normPin(p) / problems(p)            -> the pin, or why it is not one
     addPin(p) / removePin(id) / pins()  -> the viewer's pins (localStorage "vera-compare-pins"; event compare:pins)
     composite(lanesData)                 -> one lane of every pinned lane's items, coloured by lane (layer mode)
     insights(lanesData, t0, windowMs)    -> [{lane, label, before, after, delta, mean_before, mean_after, evidence[]}]
     overlay(pins)                        -> guide lines for pinned items [{t, color, label}] */
(function (root) {
  'use strict';
  if (root.VeraCompare && typeof root.VeraCompare.insights === 'function') {
    if (typeof module !== 'undefined' && module.exports) module.exports = root.VeraCompare;
    return;
  }
  var KEY = 'vera-compare-pins';
  function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function toMs(v) { if (v == null || v === '') return null; if (typeof v === 'number') return v < 1e12 ? v * 1000 : v; var t = Date.parse(String(v)); return isNaN(t) ? null : t; }
  function laneId(l) { return isObj(l) ? String(l.lane || '') : String(l || ''); }

  function problems(p) {
    var out = [];
    if (!isObj(p)) return ['a pin is an object'];
    if (p.kind !== 'lane' && p.kind !== 'item') out.push('kind: lane or item');
    if (!laneId(p.lane)) out.push('lane: a lane record or id');
    if (p.kind === 'item' && toMs(p.t) == null) out.push('t: when the item happened');
    return out;
  }
  function normPin(p) {
    if (problems(p).length) return null;
    var lid = laneId(p.lane), t = p.kind === 'item' ? toMs(p.t) : null;
    return { pin: String(p.pin || (p.kind === 'lane' ? 'lane:' + lid : 'item:' + (p.drill || lid + ':' + t))), kind: p.kind,
             lane: isObj(p.lane) ? p.lane : lid, label: String(p.label || lid), t: t, end: p.kind === 'item' ? toMs(p.end) : null,
             drill: String(p.drill || ''), color: String(p.color || (isObj(p.lane) && p.lane.color) || '#7aa2f7') };
  }

  /* the viewer's pins: browser storage is a per-viewer convenience; every read and write tolerates its absence */
  var MEM = [];
  function load() { try { var s = root.localStorage && root.localStorage.getItem(KEY); if (s) { var a = JSON.parse(s); if (Array.isArray(a)) return a.map(normPin).filter(Boolean); } } catch (_) {} return MEM.slice(); }
  function save(list) { MEM = list.slice(); try { if (root.localStorage) root.localStorage.setItem(KEY, JSON.stringify(list)); } catch (_) {}
    try { if (root.dispatchEvent && root.CustomEvent) root.dispatchEvent(new root.CustomEvent('compare:pins', { detail: { pins: list.slice() } })); } catch (_) {} }
  function pins() { return load(); }
  function addPin(p) { var n = normPin(p); if (!n) return null; var l = load().filter(function (x) { return x.pin !== n.pin; }); l.push(n); save(l); return n; }
  function removePin(id) { save(load().filter(function (x) { return x.pin !== id; })); }
  function clearPins() { save([]); }

  /* layer mode: one row holding every pinned lane's items, each keeping its lane's colour */
  function composite(lanesData) {
    var items = [];
    (lanesData || []).forEach(function (L) { (L.items || []).forEach(function (it) { items.push(Object.assign({}, it, { color: it.color || L.spec.color, tip: (L.spec.label + ' · ' + (it.tip || it.label || '')).trim(), lane_of: L.spec.lane })); }); });
    items.sort(function (a, b) { return a.t - b.t; });
    return items;
  }
  function mean(a) { return a.length ? a.reduce(function (s, v) { return s + v; }, 0) / a.length : null; }
  /* the window either side of the anchor actually compared: never past now (the future has no items, so an after
     window running into it would read as a fall), and the same length before as after */
  function spanFor(t0, windowMs, now) {
    var w = Math.max(60e3, +windowMs || 6 * 3600e3);
    return now == null ? w : Math.max(0, Math.min(w, toMs(now) - toMs(t0)));
  }
  /* before vs after an anchor, per lane, in equal windows - and the items nearest the anchor as the evidence */
  function insights(lanesData, t0, windowMs, now) {
    t0 = toMs(t0);
    if (t0 == null) return [];
    var w = spanFor(t0, windowMs, now);
    if (w < 60e3) return [];
    return (lanesData || []).map(function (L) {
      var b = [], a = [];
      (L.items || []).forEach(function (it) { if (it.t >= t0 - w && it.t < t0) b.push(it); else if (it.t >= t0 && it.t <= t0 + w) a.push(it); });
      var vb = b.filter(function (i) { return i.value != null; }).map(function (i) { return +i.value; }), va = a.filter(function (i) { return i.value != null; }).map(function (i) { return +i.value; });
      var ev = b.concat(a).sort(function (x, y) { return Math.abs(x.t - t0) - Math.abs(y.t - t0); }).slice(0, 3)
        .map(function (i) { return { t: i.t, offset_min: Math.round((i.t - t0) / 60e3), label: i.label || i.tip || '', color: i.color }; });
      var mb = mean(vb), ma = mean(va);
      return { lane: L.spec.lane, label: L.spec.label, color: L.spec.color, before: b.length, after: a.length, delta: a.length - b.length,
               mean_before: mb, mean_after: ma, mean_delta: mb != null && ma != null ? ma - mb : null, evidence: ev };
    });
  }
  function overlay(list) {
    return (list || []).filter(function (p) { return p.kind === 'item' && p.t != null; }).map(function (p) { return { t: p.t, end: p.end, color: p.color, label: p.label }; });
  }
  function ago(min) { var s = min < 0 ? '−' : '+', m = Math.abs(min); return s + (m >= 120 ? Math.round(m / 60) + 'h' : m + 'm'); }
  function spanText(ms) { var m = Math.round(ms / 60e3); return m >= 120 ? Math.round(m / 60) + ' h' : m + ' min'; }
  function insightRows(rows, spanMs, asked) {
    if (!rows.length) return spanMs != null && spanMs < 60e3 ? '<div class="vc-empty">The anchor is too recent to compare: nothing has happened after it yet.</div>' : '';
    return '<div class="vc-mut" style="font-size:11px;margin-top:8px">' + spanText(spanMs) + ' before vs ' + spanText(spanMs) + ' after the anchor' +
      (asked && spanMs < asked ? ' (the ± window is cut to now, so both sides are the same length)' : '') + '</div>' +
      '<table class="vc-ins"><tr><th>lane</th><th>before</th><th>after</th><th>change</th><th>evidence (nearest the anchor)</th></tr>' + rows.map(function (r) {
      var d = r.mean_delta != null ? (r.mean_delta >= 0 ? '+' : '') + (Math.round(r.mean_delta * 100) / 100) + ' (mean)' : (r.delta >= 0 ? '+' : '') + r.delta + ' item(s)';
      var cls = (r.mean_delta != null ? r.mean_delta : r.delta) === 0 ? '' : ((r.mean_delta != null ? r.mean_delta : r.delta) > 0 ? ' up' : ' down');
      return '<tr><td><i style="background:' + esc(r.color) + '"></i>' + esc(r.label) + '</td><td>' + r.before + (r.mean_before != null ? ' · ' + (Math.round(r.mean_before * 100) / 100) : '') + '</td><td>' +
        r.after + (r.mean_after != null ? ' · ' + (Math.round(r.mean_after * 100) / 100) : '') + '</td><td class="vc-d' + cls + '">' + esc(d) + '</td><td>' +
        (r.evidence.length ? r.evidence.map(function (e) { return '<span class="vc-ev" style="border-color:' + esc(e.color) + '">' + esc(ago(e.offset_min)) + ' ' + esc(String(e.label).slice(0, 48)) + '</span>'; }).join(' ') : '<span class="vc-mut">nothing in the window</span>') + '</td></tr>';
    }).join('') + '</table>';
  }

  var api = { problems: problems, normPin: normPin, pins: pins, addPin: addPin, removePin: removePin, clearPins: clearPins,
              composite: composite, insights: insights, spanFor: spanFor, overlay: overlay, insightRows: insightRows, KEY: KEY };
  root.VeraCompare = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (!root.HTMLElement || !root.customElements) return;

  var CSS = 'vera-compare{display:block}.vc-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}' +
    '.vc-seg{display:inline-flex;border:1px solid var(--border,#2a313c);border-radius:6px;overflow:hidden}.vc-seg button{background:none;border:0;color:var(--muted,#8b93a1);font:inherit;font-size:11.5px;padding:3px 9px;cursor:pointer}.vc-seg button.on{background:var(--accent,#7aa2f7);color:#0b0e13}' +
    '.vc-pins{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}.vc-pin{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--c);border-radius:11px;padding:1px 4px 1px 8px;font-size:11px}' +
    '.vc-pin.anchor{box-shadow:0 0 0 2px var(--c)}.vc-pin button{background:none;border:0;color:var(--muted,#8b93a1);cursor:pointer;font:inherit;padding:0 3px}' +
    '.vc-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}.vc-col{border:1px solid var(--border,#2a313c);border-radius:8px;padding:8px}' +
    '.vc-ins{border-collapse:collapse;width:100%;font-size:12px;margin-top:10px}.vc-ins th{text-align:left;color:var(--muted,#8b93a1);font-weight:500;padding:3px 6px;border-bottom:1px solid var(--border,#2a313c)}' +
    '.vc-ins td{padding:4px 6px;border-bottom:1px solid var(--border,#2a313c);vertical-align:top}.vc-ins i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}' +
    '.vc-ins td:nth-child(-n+4){white-space:nowrap}.vc-d.up{color:var(--ok,#6db87a)}.vc-d.down{color:var(--err,#c96b6b)}.vc-ev{display:inline-block;border-left:3px solid;padding:0 6px;margin:1px 4px 1px 0;font-size:11px}.vc-mut{color:var(--muted,#8b93a1)}' +
    '.vc-empty{color:var(--muted,#8b93a1);font-size:12px;padding:14px 4px}';
  function ensureCss() { if (root.document.getElementById('vera-compare-css')) return; var st = root.document.createElement('style'); st.id = 'vera-compare-css'; st.textContent = CSS; root.document.head.appendChild(st); }
  function call(cap, args) {
    if (root.VeraDrill && root.VeraDrill.call) return root.VeraDrill.call(cap, args);
    return fetch((root._veraBase || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': 'compare; trigger=auto' }, body: JSON.stringify({ name: cap, arguments: args || {} }) })
      .then(function (r) { return r.json(); }).then(function (j) { return j && j.content !== undefined ? j.content : j; });
  }
  var WINDOWS = { '1h': 3600e3, '6h': 6 * 3600e3, '1d': 864e5, '7d': 7 * 864e5 };

  class VeraCompareEl extends root.HTMLElement {
    constructor() { super(); this._data = {}; this._mode = 'stack'; this._anchor = ''; this._win = '6h'; this._onPins = () => this._refresh(); }
    connectedCallback() {
      ensureCss();
      this._mode = this.getAttribute('mode') || this._mode;
      root.addEventListener('compare:pins', this._onPins);
      if (!this._wired) {
        this._wired = true;
        this.addEventListener('click', (ev) => {
          var b = ev.target.closest('[data-vc]'); if (!b || !this.contains(b)) return;
          var a = b.getAttribute('data-vc'), v = b.getAttribute('data-v');
          if (a === 'mode') this._mode = v; else if (a === 'win') this._win = v;
          else if (a === 'unpin') { if (this._anchor === v) this._anchor = ''; removePin(v); return; }
          else if (a === 'anchor') this._anchor = this._anchor === v ? '' : v;
          else if (a === 'clear') { this._anchor = ''; clearPins(); return; }
          this._draw();
        });
      }
      this._refresh();
    }
    disconnectedCallback() { root.removeEventListener('compare:pins', this._onPins); }
    _spec(p) { var V = root.VeraTimeline; return isObj(p.lane) ? (V ? V.registerLane(p.lane) || p.lane : p.lane) : (V && V.lane(p.lane)); }
    _refresh() {
      var list = pins(), seen = {};
      list.forEach((p) => {
        var s = this._spec(p); if (!s || seen[s.lane]) return; seen[s.lane] = 1;
        if (!s.source) { this._data[s.lane] = { spec: s, items: [] }; return; }
        if (!this._data[s.lane]) this._data[s.lane] = { spec: s, items: [], loading: true };
        call(s.source.cap, s.source.args).then((ans) => { this._data[s.lane] = { spec: s, items: root.VeraTimeline.itemsOf(s, ans) }; this._draw(); },
                                               (e) => { this._data[s.lane] = { spec: s, items: [], error: String((e && e.message) || e).slice(0, 120) }; this._draw(); });
      });
      this._draw();
    }
    _lanes(list) { var seen = {}, out = []; list.forEach((p) => { var s = this._spec(p); if (s && !seen[s.lane]) { seen[s.lane] = 1; out.push(this._data[s.lane] || { spec: s, items: [] }); } }); return out; }
    _draw() {
      if (!this.isConnected) return;
      var list = pins(), V = root.VeraTimeline, S = root.VeraStates;
      if (!V) { this.innerHTML = S ? S.html('loading', 'loading the timeline…') : 'loading…'; return; }
      var anchor = list.find((p) => p.pin === this._anchor && p.kind === 'item') || null;
      var bar = '<div class="vc-bar"><div class="vc-seg">' + [['stack', 'stacked'], ['layer', 'layered'], ['side', 'side by side']].map((m) =>
        '<button data-vc="mode" data-v="' + m[0] + '" class="' + (this._mode === m[0] ? 'on' : '') + '">' + m[1] + '</button>').join('') + '</div>' +
        (anchor ? '<div class="vc-seg" title="the window either side of the anchor">' + Object.keys(WINDOWS).map((k) => '<button data-vc="win" data-v="' + k + '" class="' + (this._win === k ? 'on' : '') + '">± ' + k + '</button>').join('') + '</div>' : '') +
        '<span class="vc-mut" style="font-size:11px">' + (anchor ? 'aligned on <b>' + esc(anchor.label.slice(0, 50)) + '</b>' : 'click ⚓ on a pinned item to align on it') + '</span>' +
        (list.length ? '<button class="vk-btn" data-vc="clear" style="margin-left:auto" title="remove every pin">clear pins</button>' : '') + '</div>';
      var chips = '<div class="vc-pins">' + list.map((p) => '<span class="vc-pin' + (p.pin === this._anchor ? ' anchor' : '') + '" style="--c:' + esc(p.color) + '" title="' + esc(p.kind === 'lane' ? 'a whole lane' : new Date(p.t).toLocaleString()) + '">' +
        esc((p.kind === 'lane' ? '≡ ' : '◆ ') + p.label.slice(0, 40)) + (p.kind === 'item' ? '<button data-vc="anchor" data-v="' + esc(p.pin) + '" title="align on this">⚓</button>' : '') +
        '<button data-vc="unpin" data-v="' + esc(p.pin) + '" title="unpin">×</button></span>').join('') + '</div>';
      if (!list.length) { this.innerHTML = bar + '<div class="vc-empty">Nothing pinned yet. Pin a lane or an item from the Overview (its drill panel has “pin to Compare”), then line them up here.</div>'; return; }
      var lanes = this._lanes(list), w = Math.max(500, this.clientWidth || 1000), now = Date.now(), win = WINDOWS[this._win];
      var range = anchor ? { from: anchor.t - win, to: anchor.t + win, now: now } : this._range(list, lanes, now);
      var ov = overlay(list).map((o) => Object.assign(o, { anchor: anchor && o.t === anchor.t }));
      var body = '';
      if (this._mode === 'side') {
        var items = list.filter((p) => p.kind === 'item' && p.drill);
        body = items.length ? '<div class="vc-cols">' + items.map((p, i) => '<div class="vc-col"><vera-drill data-vc-col="' + i + '"></vera-drill></div>').join('') + '</div>'
          : '<div class="vc-empty">Side by side shows pinned ITEMS in their drill panels; pin an item (an event, a test run) to see it here.</div>';
      } else {
        var model = this._mode === 'layer'
          ? [{ spec: { lane: 'compare.layer', label: 'Layered · ' + lanes.length + ' lane(s)', type: 'mark', color: '#7aa2f7', height: 46 }, items: composite(lanes), index: 0 }]
          : lanes.map((L, i) => Object.assign({ index: i }, L));
        body = V.render({ width: w, range: range, lanes: model, overlay: ov });
      }
      var ins = anchor ? insightRows(insights(lanes, anchor.t, win, now), spanFor(anchor.t, win, now), win) : '';
      var errs = lanes.filter((L) => L.error).map((L) => L.spec.label + ': ' + L.error);
      this.innerHTML = bar + chips + body + (errs.length && S ? S.html('error', 'could not read ' + errs.join('; ')) : '') + ins;
      if (this._mode === 'side') {
        list.filter((p) => p.kind === 'item' && p.drill).forEach((p, i) => { var el = this.querySelector('[data-vc-col="' + i + '"]'), j = p.drill.indexOf(':');
          if (el && el.open && j > 0) el.open(p.drill.slice(0, j), p.drill.slice(j + 1), { root: true }); });
      }
    }
    _range(list, lanes, now) {
      var ts = [];
      list.forEach((p) => { if (p.t != null) { ts.push(p.t); if (p.end) ts.push(p.end); } });
      if (!ts.length) lanes.forEach((L) => (L.items || []).forEach((it) => { if (it.t >= now - 864e5) ts.push(it.t); }));
      if (!ts.length) return { from: now - 864e5, to: now + 2 * 3600e3, now: now };
      var a = Math.min.apply(null, ts), b = Math.max.apply(null, ts), pad = Math.max(3600e3, (b - a) * 0.15);
      return { from: a - pad, to: b + pad, now: now };
    }
  }
  if (!root.customElements.get('vera-compare')) root.customElements.define('vera-compare', VeraCompareEl);
})(typeof window !== 'undefined' ? window : globalThis);
