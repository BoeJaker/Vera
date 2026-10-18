/* VeraKit - the drill panel, the live strip and the states (looplab-control §9 Phase K3, 18 Oct 2026). ONE
   implementation every section uses. Browser (window.VeraDrill / VeraLive / VeraStates + <vera-drill>,
   <vera-live-strip>) and Node (module.exports) - the pure half is the same in both.

   STATES - every surface says the same five things the same way:
     VeraStates.html(kind, msg)   kind: loading | empty | error | stale | broken (a measure that failed)
     VeraStates.stale(ageS, everyS) -> true when a reading is more than two intervals old

   DRILL - a path of frames, each {kind, id, tab}; a KIND is a record a family registers once:
     VeraDrill.registerKind('schedule', { label: 'Schedule',
         load: (id, ctx) => ctx.call('evolve.schedule.get', {id}).then((a) => a.schedule),   // optional: no load = the
         title: (rec, id) => rec.title || id,                                                  //   opener passes rec
         tabs: [{id: 'detail', label: 'Detail'}, {id: 'runs', label: 'Runs'}],                 // or (rec) => [...]
         render: (rec, tab, ctx) => html | Promise<html> })
     encode(path) / decode(str)   "schedule:bc6f6c1f8c@runs/run:59616" <-> [{kind, id, tab}]
     hashGet(hash, key) / hashSet(hash, key, val)   the panel's "#page?k=v&drill=..." hash
   <vera-drill url>: .open(kind, id, {rec, tab, root}) pushes a frame (root: start a new path), .back(), .close(),
   .setTab(t), .path. Breadcrumbs, back and close are drawn by the panel itself - it IS the control area (display
   widgets stay control-free). With `url` the path rides in location.hash as drill=..., so a link reopens it.
   Inside a rendered body, <a data-drill="kind:id"> opens a deeper frame. Emits drill:change {path, encoded}.

   LIVE - what is running now, from any number of records (one per source):
     { live: 'runs', label: 'Runs', source: {cap, args}, every: 15,
       map: { items: 'events', where: {running: true}, id: 'id', label: 'title', sub: '{detail}', started: 'start',
              progress: 'progress' | {done: 'done', total: 'total'}, color: '#7aa2f7', drill: 'event', one: false } }
   (map.one: items names a single object - e.g. the census harness - rather than a list)
   VeraLive.problems(rec), itemsOf(rec, answer, now), render(items).
   <vera-live-strip>: .setRecords([...]); reads each source on its own interval; emits live:select {record, item}. */
(function (root) {
  'use strict';
  if (root.VeraDrill && typeof root.VeraDrill.registerKind === 'function') {
    if (typeof module !== 'undefined' && module.exports) module.exports = { VeraDrill: root.VeraDrill, VeraLive: root.VeraLive, VeraStates: root.VeraStates };
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
  function toMs(v) {
    if (v == null || v === '') return null;
    if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
    var t = Date.parse(String(v)); return isNaN(t) ? null : t;
  }
  function ago(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's'; if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60 ? (s % 60) + 's' : '');
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  /* ── STATES ─────────────────────────────────────────────────────────────── */
  var STATE_TEXT = { loading: 'loading…', empty: 'nothing to show', error: 'could not read this', stale: 'this reading is old', broken: 'a measure failed' };
  var STATE_ICON = { loading: '', empty: '○', error: '!', stale: '◷', broken: '⚠' };
  var VeraStates = {
    kinds: function () { return Object.keys(STATE_TEXT); },
    html: function (kind, msg) {
      var k = STATE_TEXT[kind] ? kind : 'empty';
      return '<div class="vk-state vk-' + k + '" role="status">' + (k === 'loading' ? '<span class="vk-spin"></span>' : '<span class="vk-ic">' + STATE_ICON[k] + '</span>') +
        '<span>' + esc(msg || STATE_TEXT[k]) + '</span></div>';
    },
    stale: function (ageS, everyS) { return +ageS > 2 * Math.max(1, +everyS || 60); },
  };

  /* ── DRILL ──────────────────────────────────────────────────────────────── */
  var KINDS = {};
  function registerKind(kind, spec) {
    if (!kind || !/^[a-z0-9_.-]+$/i.test(kind) || !isObj(spec) || typeof spec.render !== 'function') return null;
    KINDS[kind] = { kind: kind, label: String(spec.label || kind), load: typeof spec.load === 'function' ? spec.load : null,
                    title: typeof spec.title === 'function' ? spec.title : function (rec, id) { return (rec && (rec.title || rec.name)) || id; },
                    tabs: spec.tabs || [{ id: 'detail', label: 'Detail' }], render: spec.render };
    // a drill reopened from the URL may be waiting on this kind (the element upgrades before the page registers it)
    DRILLS.forEach(function (el) { var p = el._path, f = p && p[p.length - 1]; if (f && f.kind === kind && el.isConnected) el._draw(); });
    return KINDS[kind];
  }
  var DRILLS = [];
  function kind(k) { return KINDS[k] || null; }
  function tabsOf(k, rec) { var s = KINDS[k]; if (!s) return []; var t = typeof s.tabs === 'function' ? s.tabs(rec) : s.tabs; return Array.isArray(t) ? t : []; }
  function encode(path) {
    return (path || []).map(function (f) { return f.kind + ':' + encodeURIComponent(String(f.id)) + (f.tab ? '@' + f.tab : ''); }).join('/');
  }
  function decode(str) {
    if (!str) return [];
    return String(str).split('/').map(function (p) {
      var m = /^([a-z0-9_.-]+):([^@]*)(?:@([a-z0-9_-]+))?$/i.exec(p); if (!m) return null;
      var id; try { id = decodeURIComponent(m[2]); } catch (_) { id = m[2]; }
      return { kind: m[1], id: id, tab: m[3] || '' };
    }).filter(Boolean);
  }
  function hashParts(hash) {
    var h = String(hash || '').replace(/^#/, ''), i = h.indexOf('?');
    return { page: i < 0 ? h : h.slice(0, i), q: i < 0 ? '' : h.slice(i + 1) };
  }
  /* the value is stored as given: an encode()d path is already URL-safe (its ids are encoded), and decoding it here
     would turn an id's %2F back into a path separator */
  function hashGet(hash, key) {
    var p = hashParts(hash), out = '';
    p.q.split('&').forEach(function (kv) { var j = kv.indexOf('='); if (j > 0 && kv.slice(0, j) === key) out = kv.slice(j + 1); });
    return out;
  }
  function hashSet(hash, key, val) {
    var p = hashParts(hash);
    var kept = p.q.split('&').filter(function (kv) { return kv && kv.split('=')[0] !== key; });
    if (val) kept.push(key + '=' + String(val).replace(/[&#\s]/g, encodeURIComponent));
    return '#' + p.page + (kept.length ? '?' + kept.join('&') : '');
  }
  var VeraDrill = { registerKind: registerKind, kind: kind, kinds: function () { return Object.keys(KINDS).sort(); }, tabsOf: tabsOf,
                    encode: encode, decode: decode, hashGet: hashGet, hashSet: hashSet };

  /* ── LIVE ───────────────────────────────────────────────────────────────── */
  function liveProblems(rec) {
    var out = [];
    if (!isObj(rec)) return ['a live record is an object'];
    if (!rec.live || !/^[a-z0-9_.:-]+$/i.test(String(rec.live))) out.push('live: an id of letters, digits, _ . : -');
    if (!(isObj(rec.source) && rec.source.cap)) out.push('source: {cap, args}');
    if (rec.map != null && !isObj(rec.map)) out.push('map: an object of field paths');
    return out;
  }
  function matches(raw, where) {
    return Object.keys(where).every(function (k) { var v = pick(raw, k), w = where[k]; return Array.isArray(w) ? w.indexOf(v) >= 0 : v === w; });
  }
  function liveItems(rec, answer, now) {
    var m = (rec && rec.map) || {}, list = m.items ? pick(answer, m.items) : answer;
    if (m.one) list = list == null ? [] : [list];                      // map.one: the path names ONE item, not a list
    if (!Array.isArray(list)) list = isObj(list) ? Object.keys(list).map(function (k) { return list[k]; }) : [];
    now = now || Date.now();
    var out = [];
    list.forEach(function (raw, i) {
      if (raw == null || (isObj(m.where) && !matches(raw, m.where))) return;
      var st = m.started ? toMs(pick(raw, m.started)) : null, pr = null;
      if (isObj(m.progress)) { var d = +pick(raw, m.progress.done), t = +pick(raw, m.progress.total); if (t > 0 && !isNaN(d)) pr = Math.max(0, Math.min(1, d / t)); }
      else if (m.progress) { var v = +pick(raw, m.progress); if (!isNaN(v)) pr = Math.max(0, Math.min(1, v > 1 ? v / 100 : v)); }
      out.push({ id: m.id ? String(pick(raw, m.id)) : String(i), label: String(m.label ? (pick(raw, m.label) == null ? '' : pick(raw, m.label)) : (rec.label || rec.live)),
                 sub: m.sub ? fill(m.sub, raw) : '', since: st, elapsed: st == null ? null : Math.max(0, (now - st) / 1000), progress: pr,
                 color: (m.color && typeof m.color === 'object' && m.color.field ? pick(raw, m.color.field) : m.color) || rec.color || '#7aa2f7',
                 record: rec.live, drill: m.drill || '', raw: raw });
    });
    return out;
  }
  function liveRender(items, opts) {
    opts = opts || {};
    if (!items.length) return '<div class="vk-live vk-live-none">' + esc(opts.none || 'nothing running') + '</div>';
    return '<div class="vk-live">' + items.map(function (it, i) {
      return '<div class="vk-chip" data-i="' + i + '" style="--c:' + esc(it.color) + '" title="' + esc(it.label + (it.sub ? ' · ' + it.sub : '')) + '">' +
        '<span class="vk-dot"></span><span class="vk-l">' + esc(it.label.slice(0, 60)) + '</span>' +
        (it.sub ? '<span class="vk-s">' + esc(it.sub.slice(0, 50)) + '</span>' : '') +
        (it.elapsed != null ? '<span class="vk-t">' + ago(it.elapsed) + '</span>' : '') +
        (it.progress != null ? '<span class="vk-p"><i style="width:' + Math.round(it.progress * 100) + '%"></i></span>' : '') + '</div>';
    }).join('') + '</div>';
  }
  var VeraLive = { problems: liveProblems, itemsOf: liveItems, render: liveRender, ago: ago };

  var api = { VeraDrill: VeraDrill, VeraLive: VeraLive, VeraStates: VeraStates };
  root.VeraDrill = VeraDrill; root.VeraLive = VeraLive; root.VeraStates = VeraStates;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (!root.HTMLElement || !root.customElements) return;

  /* ── the browser half ───────────────────────────────────────────────────── */
  var CSS = '.vk-state{display:flex;align-items:center;gap:8px;padding:10px 12px;color:var(--muted,#8b93a1);font-size:12px}' +
    '.vk-error,.vk-broken{color:var(--err,#c96b6b)}.vk-stale{color:var(--warn,#c9a35a)}.vk-ic{font-weight:700}' +
    '.vk-spin{width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:vkSpin .8s linear infinite}' +
    '@keyframes vkSpin{to{transform:rotate(360deg)}}@keyframes vkPulse{0%,100%{opacity:1}50%{opacity:.3}}' +
    '.vk-live{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.vk-live-none{color:var(--muted,#8b93a1);font-size:11px}' +
    '.vk-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--c);border-radius:12px;padding:2px 9px;font-size:11.5px;cursor:pointer;background:color-mix(in srgb,var(--c) 10%,transparent);max-width:420px}' +
    '.vk-dot{width:7px;height:7px;border-radius:50%;background:var(--c);animation:vkPulse 1.6s infinite;flex:none}' +
    '.vk-l{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vk-s,.vk-t{color:var(--muted,#8b93a1);white-space:nowrap}' +
    '.vk-p{width:46px;height:4px;border-radius:2px;background:var(--border,#2a313c);overflow:hidden;flex:none}.vk-p i{display:block;height:100%;background:var(--c);transition:width .6s}' +
    'vera-drill{display:block}.vk-dh{display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap}' +
    '.vk-crumb{background:none;border:0;color:var(--accent,#7aa2f7);cursor:pointer;font:inherit;padding:0}.vk-crumb.cur{color:inherit;font-weight:600;cursor:default}' +
    '.vk-sep{color:var(--muted,#8b93a1)}.vk-btn{background:none;border:1px solid var(--border,#2a313c);border-radius:5px;color:inherit;cursor:pointer;font:inherit;font-size:11px;padding:1px 7px}' +
    '.vk-tabs{display:flex;gap:2px;border-bottom:1px solid var(--border,#2a313c);margin-bottom:8px}' +
    '.vk-tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted,#8b93a1);cursor:pointer;font:inherit;font-size:12px;padding:3px 9px}.vk-tab.on{color:inherit;border-bottom-color:var(--accent,#7aa2f7)}' +
    '.vk-body{font-size:12px;animation:vkIn .18s ease-out}@keyframes vkIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}';
  function ensureCss() {
    if (root.document.getElementById('vera-kit-css')) return;
    var st = root.document.createElement('style'); st.id = 'vera-kit-css'; st.textContent = CSS; root.document.head.appendChild(st);
  }
  /* one read per source in flight; an answer is reused for 5 s (as the timeline does) */
  var READS = {};
  function call(cap, args) {
    var key = JSON.stringify([cap, args || {}]), now = Date.now(), hit = READS[key];
    if (hit && (hit.pending || now - hit.at < 5000)) return hit.p;
    var p = fetch((root._veraBase || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': 'kit; trigger=auto' },
                  body: JSON.stringify({ name: cap, arguments: args || {} }) })
      .then(function (r) { return r.json(); }).then(function (j) { if (j && j.detail && j.content === undefined) throw new Error(String(j.detail)); return j && j.content !== undefined ? j.content : j; });
    var rec = { p: p, pending: true, at: now }; READS[key] = rec;
    p.then(function () { rec.pending = false; rec.at = Date.now(); }, function () { delete READS[key]; });
    return p;
  }
  VeraDrill.call = call;

  class VeraDrillEl extends root.HTMLElement {
    constructor() { super(); this._path = []; this._recs = {}; this._seq = 0; }
    disconnectedCallback() { var i = DRILLS.indexOf(this); if (i >= 0) DRILLS.splice(i, 1); }
    connectedCallback() {
      ensureCss();
      if (DRILLS.indexOf(this) < 0) DRILLS.push(this);
      if (this.hasAttribute('url')) { var p = decode(hashGet(root.location.hash, 'drill')); if (p.length) { this._path = p; } }
      if (this._wired) { this._draw(); return; }
      this._wired = true;
      this.addEventListener('click', (ev) => {
        var a = ev.target.closest('[data-drill]');
        if (a && this.contains(a)) { ev.preventDefault(); var d = String(a.getAttribute('data-drill')), j = d.indexOf(':'); if (j > 0) this.open(d.slice(0, j), d.slice(j + 1)); return; }
        var b = ev.target.closest('[data-vk]'); if (!b || !this.contains(b)) return;
        var act = b.getAttribute('data-vk');
        if (act === 'back') this.back(); else if (act === 'close') this.close();
        else if (act === 'crumb') { this._path = this._path.slice(0, +b.getAttribute('data-n') + 1); this._changed(); }
        else if (act === 'tab') this.setTab(b.getAttribute('data-t'));
      });
      this._draw();
    }
    get path() { return this._path.slice(); }
    open(k, id, o) {
      o = o || {}; var f = { kind: String(k), id: String(id), tab: o.tab || '' };
      if (o.rec) this._recs[f.kind + ':' + f.id] = o.rec;
      this._path = o.root ? [f] : this._path.concat([f]); this._changed();
    }
    back() { this._path = this._path.slice(0, -1); this._changed(); }
    close() { this._path = []; this._changed(); }
    setTab(t) { var f = this._path[this._path.length - 1]; if (f) { f.tab = t; this._changed(); } }
    _changed() {
      var enc = encode(this._path);
      if (this.hasAttribute('url')) { try { root.history.replaceState(null, '', root.location.pathname + root.location.search + hashSet(root.location.hash, 'drill', enc)); } catch (_) {} }
      this.dispatchEvent(new CustomEvent('drill:change', { bubbles: true, composed: true, detail: { path: this.path, encoded: enc } }));
      this._draw();
    }
    _draw() {
      var f = this._path[this._path.length - 1], seq = ++this._seq;
      this.style.display = f ? '' : 'none';
      if (!f) { this.innerHTML = ''; return; }
      var spec = kind(f.kind);
      if (!spec) { this.innerHTML = this._head(null, f) + VeraStates.html('broken', 'nothing knows how to show a "' + f.kind + '"'); return; }
      var key = f.kind + ':' + f.id, have = this._recs[key];
      var ctx = { call: call, open: (k, id, o) => this.open(k, id, o), esc: esc, states: VeraStates };
      var go = (rec) => {
        if (seq !== this._seq) return;
        var tabs = tabsOf(f.kind, rec), tab = f.tab && tabs.some(function (t) { return t.id === f.tab; }) ? f.tab : (tabs[0] && tabs[0].id) || '';
        var head = this._head(spec.title(rec, f.id), f) + (tabs.length > 1 ? '<div class="vk-tabs">' + tabs.map(function (t) {
          return '<button class="vk-tab' + (t.id === tab ? ' on' : '') + '" data-vk="tab" data-t="' + esc(t.id) + '">' + esc(t.label) + '</button>'; }).join('') + '</div>' : '');
        this.innerHTML = head + '<div class="vk-body">' + VeraStates.html('loading') + '</div>';
        var body = this.querySelector('.vk-body');
        Promise.resolve().then(function () { return spec.render(rec, tab, ctx); })
          .then((html) => { if (seq === this._seq) body.innerHTML = html == null || html === '' ? VeraStates.html('empty') : html; },
                (e) => { if (seq === this._seq) body.innerHTML = VeraStates.html('error', e && e.message); });
      };
      if (have || !spec.load) { go(have || null); return; }
      this.innerHTML = this._head(f.id, f) + VeraStates.html('loading');
      Promise.resolve().then(function () { return spec.load(f.id, ctx); })
        .then((rec) => { this._recs[key] = rec; go(rec); },
              (e) => { if (seq === this._seq) this.innerHTML = this._head(f.id, f) + VeraStates.html('error', e && e.message); });
    }
    _head(title, f) {
      var n = this._path.length;
      var crumbs = this._path.map((p, i) => {
        var s = kind(p.kind), rec = this._recs[p.kind + ':' + p.id], t = i === n - 1 && title != null ? title : (s ? s.title(rec || null, p.id) : p.id);
        return '<button class="vk-crumb' + (i === n - 1 ? ' cur' : '') + '" data-vk="crumb" data-n="' + i + '" title="' + esc((s ? s.label : p.kind) + ' ' + p.id) + '">' +
          '<span class="vk-sep">' + esc(s ? s.label : p.kind) + ' · </span>' + esc(String(t).slice(0, 48)) + '</button>';
      }).join('<span class="vk-sep">›</span>');
      return '<div class="vk-dh">' + (n > 1 ? '<button class="vk-btn" data-vk="back" title="back">‹ back</button>' : '') + crumbs +
        '<button class="vk-btn" data-vk="close" style="margin-left:auto" title="close">×</button></div>';
    }
  }

  class VeraLiveStripEl extends root.HTMLElement {
    constructor() { super(); this._recs = []; this._items = {}; this._timers = []; }
    connectedCallback() {
      ensureCss();
      try { var a = this.getAttribute('records'); if (a && !this._recs.length) this.setRecords(JSON.parse(a)); } catch (_) {}
      if (this._wired) { if (!this._timers.length && this._recs.length) this.setRecords(this._recs); return; }   // moved: restart its reads
      this._wired = true;
      this.addEventListener('click', (ev) => {
        var c = ev.target.closest('.vk-chip'); if (!c || !this.contains(c)) return;
        var it = this._flat()[+c.getAttribute('data-i')]; if (!it) return;
        this.dispatchEvent(new CustomEvent('live:select', { bubbles: true, composed: true, detail: { record: it.record, item: it.raw, drill: it.drill, id: it.id } }));
      });
      this._draw();
    }
    disconnectedCallback() { this._timers.forEach(clearInterval); this._timers = []; }
    setRecords(list) {
      this.disconnectedCallback();
      this._recs = (list || []).filter(function (r) { return !liveProblems(r).length; }); this._items = {}; this._err = {};
      this._recs.forEach((r) => { this._read(r); this._timers.push(setInterval(() => { if (this.isConnected) this._read(r); }, 1000 * Math.max(5, +r.every || 15))); });
      this._timers.push(setInterval(() => this._tick(), 1000));         // elapsed time ticks between reads
      this._draw();
    }
    _tick() {        // only the clocks change between reads: redrawing would restart the pulse
      var items = this._flat();
      this.querySelectorAll('.vk-chip').forEach(function (c) { var it = items[+c.getAttribute('data-i')], t = c.querySelector('.vk-t'); if (it && t && it.elapsed != null) t.textContent = ago(it.elapsed); });
    }
    _read(r) {
      call(r.source.cap, r.source.args || {}).then((a) => { this._items[r.live] = { at: Date.now(), answer: a }; delete this._err[r.live]; this._draw(); },
                                                  (e) => { this._err[r.live] = (e && e.message) || 'error'; this._draw(); });
    }
    _flat() {
      var now = Date.now(), out = [];
      this._recs.forEach((r) => { var x = this._items[r.live]; if (x) out = out.concat(liveItems(r, x.answer, now)); });
      return out;
    }
    _draw() {
      if (!this.isConnected) return;
      var errs = Object.keys(this._err || {});
      if (!Object.keys(this._items).length && !errs.length) { this.innerHTML = VeraStates.html('loading', 'reading what is running…'); return; }
      this.innerHTML = liveRender(this._flat(), { none: this.getAttribute('none') || 'nothing running' }) +
        (errs.length ? VeraStates.html('error', 'could not read: ' + errs.join(', ')) : '');
    }
  }
  if (!root.customElements.get('vera-drill')) root.customElements.define('vera-drill', VeraDrillEl);
  if (!root.customElements.get('vera-live-strip')) root.customElements.define('vera-live-strip', VeraLiveStripEl);
})(typeof window !== 'undefined' ? window : globalThis);
