/* activity_timeline_element.js — <vera-activity-timeline>
   ========================================================
   A reusable, high-quality activity timeline for any Vera scope:

     • CARD CAROUSEL (top)   — horizontally-scrolling, snap-aligned info cards,
                               one per event, FULL container height. Each card:
                               kind icon, friendly title, relative time, status
                               pill, markdown summary, action buttons that open
                               the ASSOCIATED UI, drill into a scope INLINE, or
                               WATCH a loop run live (agent-loop-output re-attach,
                               fills the card).
     • TIMELINE RAIL (bottom)— a proportional time axis; every event is a node
                               positioned by timestamp, coloured by kind, hover
                               tooltip, click → focus its card.
     • CONTROLS              — stop a loop, flatten duplicates/stale, list a
                               scope's sandboxes + open terminals, drill in/out.

   Refreshes are NON-DESTRUCTIVE: it preserves scroll position, keeps live
   "watch" streams mounted, and skips repaint entirely when nothing changed —
   so auto-refresh never yanks the view out from under you.

   USAGE
     <script src="/ui/elements/activity_timeline.js"></script>
     <vera-activity-timeline scope="project:my-goal" auto="15"></vera-activity-timeline>

   API: el.setScope(s) · el.load() · el.setAutoRefresh(sec) · el.setApiBase(u)
*/
(function () {
  'use strict';
  if (customElements.get('vera-activity-timeline')) return;

  function apiBase() {
    try { if (window.parent && window.parent._veraBase) return window.parent._veraBase; } catch (e) {}
    return (window.__VERA_BASE__ || window.location.origin || '');
  }
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const mdRender = s => (window.VeraMD ? window.VeraMD.render(s) : esc(s).replace(/\n/g, '<br>'));

  function relTime(ts) {
    if (!ts) return '';
    const t = Date.parse(ts);
    if (isNaN(t)) return String(ts).slice(0, 16).replace('T', ' ');
    const d = (Date.now() - t) / 1000;
    if (d < 0) return 'now';
    if (d < 60) return Math.floor(d) + 's ago';
    if (d < 3600) return Math.floor(d / 60) + 'm ago';
    if (d < 86400) return Math.floor(d / 3600) + 'h ago';
    if (d < 604800) return Math.floor(d / 86400) + 'd ago';
    return new Date(t).toLocaleDateString();
  }

  function fmtDuration(ms) {
    if (!Number.isFinite(ms) || ms < 0) return '—';
    if (ms < 1000) return Math.round(ms) + 'ms';
    if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
    return (ms / 60000).toFixed(1) + 'm';
  }

  function runWaterfall(extra) {
    const lifecycle = Array.isArray(extra.lifecycle) ? extra.lifecycle : [];
    const children = Array.isArray(extra.children) ? extra.children : [];
    const times = lifecycle.map(e => Date.parse(e.occurred_at)).filter(Number.isFinite);
    const childTimes = children.flatMap(c => [Date.parse(c.started_at || c.created_at),
      Date.parse(c.ended_at || '')]).filter(Number.isFinite);
    const start = times.length ? Math.min(...times) : (childTimes.length ? Math.min(...childTimes) : NaN);
    const terminal = ['completed','failed','cancelled','timed_out','skipped'];
    const isTerminal = terminal.includes(String(extra.run_status || ''));
    const knownEnds = lifecycle.concat(children.flatMap(c => c.events || []))
      .map(e => Date.parse(e.occurred_at)).filter(Number.isFinite);
    const end = isTerminal && knownEnds.length ? Math.max(...knownEnds) : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
    const total = Math.max(1, end - start);
    const spans = children.map((child, index) => {
      const s = Date.parse(child.started_at || child.created_at || '');
      let finish = Date.parse(child.ended_at || '');
      if (!Number.isFinite(finish)) {
        const evTimes = (child.events || []).map(e => Date.parse(e.occurred_at)).filter(Number.isFinite);
        finish = evTimes.length ? Math.max(...evTimes) : end;
      }
      if (!Number.isFinite(s)) return null;
      const first = ((child.events || [])[0] || {}).payload || {};
      return { index, child, capability:first.capability || child.kind || 'action',
        start:Math.max(start,s), end:Math.min(end,Math.max(s,finish)) };
    }).filter(Boolean);
    const merged = spans.map(s => [s.start,s.end]).sort((a,b) => a[0]-b[0]).reduce((out,span) => {
      const last = out[out.length-1];
      if (!last || span[0] > last[1]) out.push(span.slice());
      else last[1] = Math.max(last[1], span[1]);
      return out;
    }, []);
    const covered = merged.reduce((sum,span) => sum + Math.max(0,span[1]-span[0]), 0);
    return { start, end, total, spans, unattributed:Math.max(0,total-covered) };
  }

  const KINDS = {
    dream_cycle: { i: '☾', c: '--acc4,#b98adf', l: 'Dream cycle' },
    loop_run:    { i: '↻', c: '--acc,#5a9e8f',  l: 'Loop run' },
    v8_loop:     { i: '⤿', c: '--acc,#5a9e8f',  l: 'V8 loop' },
    loop_live:   { i: '◉', c: '--ok,#6db87a',   l: 'Live loop' },
    program:     { i: '❖', c: '--acc3,#c9955a', l: 'V8 program' },
    artifact:    { i: '▤', c: '--acc2,#8fb87a', l: 'Artifact' },
    cap:         { i: '▸', c: '--dim2,#8a7e70', l: 'Activity' },
    run:         { i: '◇', c: '--acc,#5a9e8f',  l: 'Run' },
    narrator:    { i: '◉', c: '--acc4,#7aa2f7', l: 'Narrator' },
  };
  const kindMeta = k => KINDS[k] || { i: '•', c: '--dim2,#8a7e70', l: k || 'event' };

  const STYLE = `
    :host { display:flex; flex-direction:column; height:100%; min-height:280px;
      color:var(--text,var(--fg,#ddd5c8)); font-family:var(--sans,system-ui,sans-serif);
      font-size:12px; --tl-bg0:var(--bg0,#181614); --tl-bg1:var(--bg1,#1f1d1a);
      --tl-bg2:var(--bg2,#272421); --tl-bd:var(--border,#3a3530); }
    .bar { display:flex; align-items:center; gap:8px; padding:7px 10px;
      border-bottom:1px solid var(--tl-bd); flex-shrink:0; flex-wrap:wrap; }
    .bar .title { font-weight:700; font-size:12px; letter-spacing:.3px; color:var(--acc,#5a9e8f); }
    select, .btn { background:var(--tl-bg2); border:1px solid var(--tl-bd);
      color:var(--text,#ddd); border-radius:5px; font-size:11px; padding:4px 8px;
      cursor:pointer; font-family:inherit; }
    .btn:hover { border-color:var(--acc,#5a9e8f); color:var(--acc,#5a9e8f); }
    .btn.warn:hover { border-color:var(--err,#c96b6b); color:var(--err,#c96b6b); }
    .chips { display:flex; gap:4px; flex-wrap:wrap; }
    .chip { font-size:9.5px; padding:2px 8px; border-radius:11px; cursor:pointer;
      border:1px solid var(--tl-bd); color:var(--dim2,#8a7e70); user-select:none;
      background:transparent; font-family:inherit; }
    .chip.on { background:var(--acc,#5a9e8f); border-color:var(--acc,#5a9e8f);
      color:var(--on-acc,#12100e); font-weight:600; }
    .live { font-size:9.5px; color:var(--ok,#6db87a); display:none; align-items:center; gap:4px; }
    .live.on { display:inline-flex; }
    .live .dot { width:7px; height:7px; border-radius:50%; background:var(--ok,#6db87a);
      animation:vpulse 1.4s ease-in-out infinite; }
    @keyframes vpulse { 0%,100%{opacity:1} 50%{opacity:.3} }
    .triage { display:none;flex-shrink:0;padding:6px 10px;border-bottom:1px solid var(--tl-bd);
      background:rgba(201,107,107,.08);font-size:9.5px;color:var(--dim2,#8a7e70); }
    .triage.on { display:flex;gap:8px;align-items:center;flex-wrap:wrap; }
    .triage b { color:var(--err,#c96b6b); }
    .flex { flex:1; }
    .drawer { flex-shrink:0; border-bottom:1px solid var(--tl-bd); background:var(--tl-bg1);
      max-height:0; overflow:hidden; transition:max-height .18s ease; }
    .drawer.on { max-height:240px; overflow:auto; }
    .drawer-in { padding:8px 10px; }
    .sbx-row { display:flex; align-items:center; gap:8px; font-size:10px; padding:4px 0;
      border-bottom:1px dotted var(--tl-bd); }
    .cards { flex:1 1 auto; display:flex; gap:10px; overflow-x:auto; overflow-y:hidden;
      padding:12px; scroll-snap-type:x proximity; align-items:stretch; min-height:0; }
    .cards::-webkit-scrollbar { height:8px; }
    .cards::-webkit-scrollbar-thumb { background:var(--tl-bd); border-radius:4px; }
    .card { scroll-snap-align:start; flex:0 0 320px; max-width:320px; height:100%;
      display:flex; flex-direction:column; background:var(--tl-bg1);
      border:1px solid var(--tl-bd); border-radius:8px; overflow:hidden;
      transition:border-color .15s, transform .15s; }
    :host([compact]) .card { flex-basis:270px; max-width:270px; }
    .card.focus { border-color:var(--acc,#5a9e8f); }
    .card.watching { flex-basis:460px; max-width:460px; }
    .card-hd { display:flex; align-items:center; gap:7px; padding:8px 10px;
      border-bottom:1px solid var(--tl-bd); flex-shrink:0; }
    .card-ic { font-size:14px; width:20px; text-align:center; flex-shrink:0; }
    .card-tt { font-size:11px; font-weight:600; line-height:1.25; flex:1;
      overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
    .pill { font-size:8.5px; padding:1px 6px; border-radius:9px; text-transform:uppercase;
      letter-spacing:.4px; border:1px solid var(--tl-bd); color:var(--dim2,#8a7e70); white-space:nowrap; }
    .pill.ok,.pill.done,.pill.completed { color:var(--ok,#6db87a); border-color:var(--ok,#6db87a); }
    .pill.running,.pill.live { color:var(--ok,#6db87a); border-color:var(--ok,#6db87a); background:rgba(109,184,122,.12); }
    .pill.failed,.pill.error,.pill.interrupted { color:var(--err,#c96b6b); border-color:var(--err,#c96b6b); }
    .pill.waiting,.pill.pending { color:var(--acc3,#c9955a); border-color:var(--acc3,#c9955a); }
    .pill.retired { color:var(--dim,#6a6058); border-color:var(--dim,#6a6058); }
    .loopplan { flex-shrink:0; padding:6px 10px; border-top:1px dotted var(--tl-bd);
      display:flex; flex-direction:column; gap:4px; max-height:150px; overflow:auto; }
    .loopplan .lp-hd { font-size:8.5px; text-transform:uppercase; letter-spacing:.5px; color:var(--dim,#6a6058); }
    .lp-row { display:flex; align-items:center; gap:6px; font-size:9.5px; min-width:0;
      cursor:pointer; padding:1px 3px; border-radius:4px; }
    .lp-row:hover { background:var(--tl-bg2); }
    .lp-row .lp-seq { color:var(--dim,#6a6058); width:14px; text-align:right; flex-shrink:0; }
    .lp-row .lp-nm { color:var(--text,#ddd); font-weight:600; flex-shrink:0; max-width:110px;
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .lp-row .lp-goal { color:var(--dim2,#8a7e70); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
    .evidence { flex-shrink:0; border-top:1px dotted var(--tl-bd); padding:6px 10px;
      max-height:180px; overflow:auto; font-size:9px; color:var(--dim2,#8a7e70); }
    .evidence summary { cursor:pointer; color:var(--acc,#5a9e8f); font-weight:600; }
    .ev-row { display:grid; grid-template-columns:24px 62px minmax(0,1fr); gap:6px;
      padding:3px 0; border-bottom:1px dotted var(--tl-bd); align-items:start; }
    .ev-seq { color:var(--dim,#6a6058); text-align:right; }
    .ev-type { color:var(--text,#ddd); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .ev-data { overflow-wrap:anywhere; font-family:var(--mono,monospace); }
    .waterfall { flex-shrink:0; border-top:1px dotted var(--tl-bd); padding:6px 10px;
      max-height:170px; overflow:auto; }
    .wf-head { display:flex;justify-content:space-between;gap:8px;font-size:8.5px;
      color:var(--dim,#6a6058);margin-bottom:4px; }
    .wf-row { display:grid;grid-template-columns:92px minmax(90px,1fr) 44px;gap:6px;
      align-items:center;min-height:18px;font-size:8.5px; }
    .wf-label { overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--dim2,#8a7e70); }
    .wf-track { height:8px;border-radius:4px;background:var(--tl-bg2);position:relative;overflow:hidden; }
    .wf-bar { position:absolute;top:0;height:100%;min-width:2px;border-radius:4px;background:var(--acc,#5a9e8f); }
    .wf-bar.fail { background:var(--err,#c96b6b); }
    .wf-bar.retry { box-shadow:inset 0 0 0 1px var(--acc3,#c9955a); }
    .wf-time { text-align:right;color:var(--dim,#6a6058);font-variant-numeric:tabular-nums; }
    .card-meta { font-size:9px; color:var(--dim,#6a6058); padding:4px 10px 0;
      display:flex; gap:8px; flex-wrap:wrap; flex-shrink:0; }
    .card-bd { flex:1 1 auto; padding:8px 10px; font-size:10px; line-height:1.5;
      color:var(--fg2,#bbb); overflow:auto; min-height:0; }
    .card.watching .card-bd { display:none; }
    .card-ft { display:flex; gap:5px; flex-wrap:wrap; padding:6px 10px;
      border-top:1px solid var(--tl-bd); flex-shrink:0; }
    .a { font-size:9.5px; padding:3px 8px; border-radius:5px; cursor:pointer;
      border:1px solid var(--tl-bd); background:var(--tl-bg2); color:var(--dim2,#8a7e70); }
    .a:hover { color:var(--acc,#5a9e8f); border-color:var(--acc,#5a9e8f); }
    .a.watch { color:var(--ok,#6db87a); border-color:var(--ok,#6db87a); }
    .a.stop:hover { color:var(--err,#c96b6b); border-color:var(--err,#c96b6b); }
    .watchbox { display:none; border-top:1px solid var(--tl-bd); }
    .card.watching .watchbox { display:flex; flex-direction:column; flex:1 1 auto; min-height:0; padding:6px 8px; }
    .watchbox vera-agent-loop-output { flex:1 1 auto; min-height:0; overflow:auto; }
    .empty { padding:30px; text-align:center; color:var(--dim,#6a6058); font-size:11px; }
    .rail-wrap { flex-shrink:0; border-top:1px solid var(--tl-bd); padding:6px 12px 10px; background:var(--tl-bg0); }
    .rail-hd { display:flex; justify-content:space-between; font-size:8.5px; color:var(--dim,#6a6058); margin-bottom:5px; }
    .rail { position:relative; height:34px; }
    .rail-line { position:absolute; left:0; right:0; top:16px; height:2px; background:var(--tl-bd); border-radius:2px; }
    .node { position:absolute; top:9px; width:15px; height:15px; margin-left:-7px; border-radius:50%;
      border:2px solid var(--tl-bg0); cursor:pointer; transition:transform .12s; box-sizing:border-box; }
    .node:hover, .node.focus { transform:scale(1.5); z-index:3; }
    .btn:focus-visible,.chip:focus-visible,.a:focus-visible,.node:focus-visible,.card:focus-visible,
    select:focus-visible,summary:focus-visible { outline:2px solid var(--acc,#5a9e8f); outline-offset:2px; }
    .sr-only { position:absolute!important; width:1px!important; height:1px!important; padding:0!important;
      margin:-1px!important; overflow:hidden!important; clip:rect(0,0,0,0)!important; white-space:nowrap!important; border:0!important; }
    @media (max-width:700px) {
      :host { min-height:360px; }.bar { gap:5px; padding:6px; }
      .bar .title { width:100%; }.cards { padding:8px; gap:8px; }
      .card,:host([compact]) .card,.card.watching { flex-basis:min(88vw,360px); max-width:min(88vw,360px); }
      .rail-wrap { padding-left:8px; padding-right:8px; }.wf-row { grid-template-columns:72px minmax(70px,1fr) 38px; }
    }
    @media (prefers-reduced-motion:reduce) { .live .dot { animation:none; }.card,.node,.drawer { transition:none; } }
    .rail-tip { position:absolute; bottom:26px; transform:translateX(-50%); background:var(--tl-bg2);
      border:1px solid var(--tl-bd); border-radius:5px; padding:4px 8px; font-size:9px; color:var(--text,#ddd);
      white-space:nowrap; pointer-events:none; opacity:0; transition:opacity .12s; z-index:5; max-width:260px;
      overflow:hidden; text-overflow:ellipsis; }
    .rail-tip.on { opacity:1; }
  `;

  class VeraActivityTimeline extends HTMLElement {
    constructor() {
      super();
      this._scope = 'all';
      this._scopeStack = [];
      this._events = [];
      this._auto = 0;
      this._timer = null;
      this._apiBase = '';
      this._kindFilter = new Set();
      this._statusFilter = '';
      this._windowMinutes = 0;
      this._failureOnly = false;
      this._pipelines = [];
      this._openWatch = new Set();   // session_ids currently being watched (persist across refresh)
      this._drawerMode = null;       // 'files' | 'sandboxes' | null — kept in sync with the scope
      this._lastSig = '';
      this.attachShadow({ mode: 'open' });
    }
    connectedCallback() {
      this._scope = this.getAttribute('scope') || 'all';
      this._auto = parseInt(this.getAttribute('auto') || '0', 10) || 0;
      this._render();
      this._loadPipelines();
      this.load();
      if (this._auto) this.setAutoRefresh(this._auto);
    }
    disconnectedCallback() { if (this._timer) clearInterval(this._timer); }

    setApiBase(u) { this._apiBase = u; }
    setScope(s, opts) {
      s = s || 'all';
      if (!(opts && opts.noPush) && s !== this._scope) this._scopeStack.push(this._scope);
      this._scope = s;
      this._openWatch.clear();
      this._lastSig = '';
      const sel = this.shadowRoot.querySelector('#scopeSel'); if (sel) sel.value = this._scope;
      this._syncBar();
      this.load();
      // Keep the open Files/Sandboxes drawer in sync with the NEW scope instead
      // of forcing the user to close + reopen the panel to reload it.
      if (this._drawerOpen()) {
        if (this._drawerMode === 'files') this._openFiles();
        else if (this._drawerMode === 'sandboxes') this._openSandboxes();
      }
      this.dispatchEvent(new CustomEvent('activity:scope', { bubbles: true, detail: { scope: s } }));
    }
    setAutoRefresh(sec) {
      this._auto = parseInt(sec, 10) || 0;
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      const l = this.shadowRoot.querySelector('.live');
      if (this._auto > 0) {
        this._timer = setInterval(() => this.load(true), this._auto * 1000);
        if (l) l.classList.add('on');
      } else if (l) l.classList.remove('on');
    }

    async _fetch(path, method, body) {
      const base = this._apiBase || apiBase();
      try {
        const opt = { headers: { 'Content-Type': 'application/json' } };
        if (method) opt.method = method;
        if (body) opt.body = JSON.stringify(body);
        const r = await fetch(base + path, opt);
        if (!r.ok) return null;
        return await r.json();
      } catch (e) { return null; }
    }

    async _loadPipelines() {
      const r = await this._fetch('/activity/pipelines');
      this._pipelines = (r && r.pipelines) || [];
      const sel = this.shadowRoot.querySelector('#scopeSel');
      if (!sel) return;
      sel.innerHTML = this._pipelines.map(p =>
        '<option value="' + esc(p.scope) + '"' + (p.scope === this._scope ? ' selected' : '') + '>' +
        esc(p.label) + (p.status ? ' · ' + esc(p.status) : '') + '</option>').join('');
    }

    async load(quiet) {
      const cards = this.shadowRoot.querySelector('#cards');
      if (cards) cards.setAttribute('aria-busy', 'true');
      if (!quiet) {
        const c = this.shadowRoot.querySelector('#cards');
        if (c && !this._events.length) c.innerHTML = '<div class="empty">Loading…</div>';
      }
      const r = await this._fetch('/activity/timeline?scope=' + encodeURIComponent(this._scope) + '&limit=150');
      this._events = (r && r.events) || [];
      this._paint();
      if (cards) cards.setAttribute('aria-busy', 'false');
      const status = this.shadowRoot.querySelector('#activityStatus');
      if (status && !quiet) status.textContent = this._events.length +
        ' activity event' + (this._events.length === 1 ? '' : 's') + ' loaded';
      this.dispatchEvent(new CustomEvent('activity:loaded',
        { bubbles: true, detail: { scope: this._scope, count: this._events.length } }));
    }

    _visibleEvents() {
      const now = Date.now();
      const groups = {
        active: new Set(['created','queued','running','waiting','approval_pending','retrying','live']),
        failed: new Set(['failed','error','timed_out','cancelled','interrupted']),
        complete: new Set(['completed','done','ok','skipped']),
      };
      return this._events.filter(e => {
        if (this._kindFilter.size && !this._kindFilter.has(e.kind)) return false;
        if (this._statusFilter && !groups[this._statusFilter].has(String(e.status || '').toLowerCase())) return false;
        if (this._windowMinutes) {
          const ts = Date.parse(e.ts);
          if (!Number.isFinite(ts) || now - ts > this._windowMinutes * 60000) return false;
        }
        if (this._failureOnly) {
          const failed = ['failed','error','timed_out','cancelled','interrupted']
            .includes(String(e.status || '').toLowerCase());
          if (!failed && Number((e.extra || {}).failure_count || 0) < 1) return false;
        }
        return true;
      });
    }

    _sig(evs) {
      // Signature that ignores relative-time drift: repaint only on real change.
      const liveBucket = evs.some(e => ['created','queued','running','waiting','approval_pending','retrying']
        .includes(String(e.status || '').toLowerCase())) ? Math.floor(Date.now() / 15000) : '';
      return evs.map(e => e.kind + '|' + (e.session_id || '') + '|' + (e.ts || '') +
        '|' + (e.status || '')).join(';') + '#' + [...this._kindFilter].sort().join(',') +
        '#' + this._statusFilter + '#' + this._windowMinutes + '#' + this._failureOnly + '#' + liveBucket;
    }

    _paint() {
      this._paintChips();
      this._paintTriage();
      const evs = this._visibleEvents();
      const sig = this._sig(evs);
      const box = this.shadowRoot.querySelector('#cards');
      if (sig === this._lastSig && box && box.children.length) {
        // Nothing changed — leave the DOM (and scroll + live watches) untouched;
        // only refresh the relative-time labels + rail so it still feels live.
        this._refreshTimes();
        this._paintRail(evs);
        return;
      }
      // An empty _lastSig means this is either the very first paint or a
      // fresh scope/filter switch (both explicitly clear it) — in either case
      // there's no scroll position worth preserving, and the useful default
      // is showing the newest activity (now the rightmost card) rather than
      // landing on whatever the oldest event happens to be. A background
      // auto-refresh that found genuinely new events, by contrast, DOES have
      // a real _lastSig to fall through to the normal preserve-scroll path.
      const firstPaint = !this._lastSig;
      this._lastSig = sig;
      const scrollLeft = box ? box.scrollLeft : 0;
      this._paintCards(evs);
      if (box) box.scrollLeft = firstPaint ? box.scrollWidth : scrollLeft;
      this._reopenWatches();                        // re-attach any live watches
      this._paintRail(evs);
    }

    _paintTriage() {
      const box=this.shadowRoot.querySelector('#triage'), btn=this.shadowRoot.querySelector('#triageBtn');
      const failures=this._events.filter(e => Number((e.extra||{}).failure_count||0)>0 ||
        ['failed','error','timed_out','cancelled','interrupted'].includes(String(e.status||'').toLowerCase()));
      if(btn){btn.textContent='⚠ Failures '+failures.length;btn.classList.toggle('warn',this._failureOnly);}
      if(!box)return;box.classList.toggle('on',this._failureOnly);if(!this._failureOnly)return;
      const counts={};failures.forEach(e=>((e.extra||{}).failed_capabilities||[]).forEach(cap=>counts[cap]=(counts[cap]||0)+1));
      const ranked=Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
      box.innerHTML='<b>'+failures.length+' affected Run'+(failures.length===1?'':'s')+'</b>'+
        (ranked.length?ranked.map(([cap,n])=>'<span class="pill failed">'+esc(cap)+' ×'+n+'</span>').join(''):'<span>No failed child capability metadata available.</span>')+
        '<span title="Observation only; this UI never executes retries">read-only triage · no automatic retry</span>';
    }

    _refreshTimes() {
      const box = this.shadowRoot.querySelector('#cards');
      if (!box) return;
      const evs = this._visibleEvents();
      box.querySelectorAll('[data-time]').forEach(el => {
        const i = parseInt(el.getAttribute('data-time'), 10);
        if (evs[i]) el.textContent = relTime(evs[i].ts);
      });
    }

    _paintChips() {
      const box = this.shadowRoot.querySelector('#chips');
      if (!box) return;
      const counts = {};
      this._events.forEach(e => { counts[e.kind] = (counts[e.kind] || 0) + 1; });
      const kinds = Object.keys(counts).sort();
      box.innerHTML = kinds.map(k => {
        const m = kindMeta(k);
        return '<button type="button" class="chip' + (this._kindFilter.has(k) ? ' on' : '') +
          '" data-k="' + esc(k) + '" aria-pressed="' + this._kindFilter.has(k) + '">' +
          m.i + ' ' + esc(m.l) + ' ' + counts[k] + '</button>';
      }).join('');
      box.querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => {
        const k = ch.getAttribute('data-k');
        if (this._kindFilter.has(k)) this._kindFilter.delete(k); else this._kindFilter.add(k);
        this._lastSig = '';
        this._paint();
      }));
    }

    _cardHtml(e, i) {
      const m = kindMeta(e.kind);
      const col = 'var(' + m.c + ')';
      const st = (e.status || '').toLowerCase();
      const x = e.extra || {};
      const running = st === 'running' || (e.kind === 'loop_live' && x.running);
      const ui = e.ui || {};
      const watching = e.session_id && this._openWatch.has(e.session_id);
      const acts = [];
      if (ui.reattach || (ui.session_id)) {
        acts.push('<button class="a watch" data-watch="' + i + '">' +
          (watching ? '▣ Stop watch' : (running ? '◉ Watch live' : '↻ Replay')) + '</button>');
      }
      if (running && e.session_id) {
        acts.push('<button class="a stop" data-stop="' + esc(e.session_id) + '">⏹ Stop</button>');
      }
      // Inline drill-in for entities that ARE a scope, + direct Files/Remove.
      const progId = x.program_id || ui.program_id;
      if (progId) {
        acts.push('<button class="a" data-scope="program:' + esc(progId) + '">↳ Timeline</button>');
        acts.push('<button class="a" data-files="program:' + esc(progId) + '">📂 Files</button>');
        acts.push('<button class="a stop" data-remove="program:' + esc(progId) + '">🗑 Remove</button>');
      }
      if (ui.slug) {
        acts.push('<button class="a" data-scope="project:' + esc(ui.slug) + '">↳ Timeline</button>');
        acts.push('<button class="a" data-files="project:' + esc(ui.slug) + '">📂 Files</button>');
      }
      if (ui.run_id) {
        acts.push('<button class="a" data-scope="run:' + esc(ui.run_id) + '">↳ Run details</button>');
        const runQ = encodeURIComponent(ui.run_id);
        const sessionQ = encodeURIComponent(ui.session_id || e.session_id || '');
        if (sessionQ) acts.push('<button class="a" data-open="/chat_panel?session_id=' + sessionQ + '">🗨 Chat context</button>');
        acts.push('<button class="a" data-open="/memgraph/panel?run_id=' + runQ +
          (sessionQ ? '&amp;session_id=' + sessionQ : '') + '">⌘ Memory graph</button>');
      }
      if (e.kind === 'run' && ui.native_url) {
        const nativeQ = [];
        if (x.workflow_id) nativeQ.push('workflow_id=' + encodeURIComponent(x.workflow_id));
        if (x.trace_id) nativeQ.push('trace_id=' + encodeURIComponent(x.trace_id));
        acts.push('<button class="a" data-open="' + esc(ui.native_url) +
          (nativeQ.length ? '?' + nativeQ.join('&amp;') : '') + '">⇗ Native DAG workshop</button>');
      }
      // External associated UI (fabric graph, netmap, …) opens the real panel.
      if (ui.url) acts.push('<button class="a" data-open="' + esc(ui.url) + '">⇗ ' + esc(ui.label || 'Open') + '</button>');
      (x.tool_uis || []).slice(0, 3).forEach(t => {
        if (t.url) acts.push('<button class="a" data-open="' + esc(t.url) + '" title="' + esc(t.cap) + '">⇗ ' + esc(t.label) + '</button>');
      });
      const meta = [];
      if (x.seq != null) meta.push('#' + x.seq);
      if (x.steps != null) meta.push(x.steps + ' steps');
      if (x.loops != null) meta.push(x.loops + ' loops');
      if (x.elapsed_s != null) meta.push(x.elapsed_s + 's');
      if (x.engine) meta.push(esc(x.engine));
      if (x.artifacts) meta.push(x.artifacts + ' artifacts');
      if (x.progress != null) meta.push(Math.round(Number(x.progress) * 100) + '%');
      if (x.storage) meta.push(x.authoritative === false ? 'ephemeral view' : esc(x.storage));
      if (e.kind === 'run') meta.push(esc(x.projection || 'projection'));
      const intent = x.current_intent || {};
      if (e.kind === 'narrator' && x.tier) meta.push(esc(x.tier) + ' take');
      if (e.kind === 'narrator' && intent.focus) meta.push('focus: ' + esc(String(intent.focus).slice(0, 60)));
      if (e.kind === 'narrator' && intent.confidence) meta.push(esc(intent.confidence) + ' confidence');
      if (e.session_id) meta.push('<span title="' + esc(e.session_id) + '">' + esc(String(e.session_id).slice(0, 26)) + '</span>');
      // V8 program: show the WHOLE loop plan (every loop + its state), so a
      // program that declares N loops reads as N loops even before most run.
      let planHtml = '';
      if (e.kind === 'run') {
        planHtml = '<div class="loopplan"><span class="lp-hd">Workflow identity</span>' +
          '<div class="lp-row"><span class="lp-seq">authority</span><span class="pill completed">' +
          esc(x.authority || 'native runtime') + '</span><span class="lp-goal">Execution and control remain authoritative there.</span></div>' +
          '<div class="lp-row"><span class="lp-seq">workflow</span><span class="lp-goal" title="' +
          esc(x.workflow_id || '') + '">' + esc(x.workflow_id || 'not recorded') + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">trace</span><span class="lp-goal" title="' +
          esc(x.trace_id || '') + '">' + esc(x.trace_id || 'not recorded') + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">view</span><span class="lp-goal">' +
          esc(x.projection || 'run protocol projection') + ' · observed, non-authoritative</span></div></div>';
      }
      if (e.kind === 'narrator' && (x.steer || intent.evidence || intent.ts)) {
        planHtml = '<div class="loopplan"><span class="lp-hd">Narrator context · current intent is not historical</span>' +
          (x.steer ? '<div class="lp-row"><span class="lp-seq">steer</span><span class="lp-goal">' + esc(x.steer) + '</span></div>' : '') +
          (intent.evidence ? '<div class="lp-row"><span class="lp-seq">basis</span><span class="lp-goal">' + esc(intent.evidence) + '</span></div>' : '') +
          (intent.ts ? '<div class="lp-row"><span class="lp-seq">as of</span><span class="lp-goal">' + esc(relTime(intent.ts)) + '</span></div>' : '') +
          '</div>';
      }
      if (e.kind === 'program' && Array.isArray(x.loop_plan) && x.loop_plan.length) {
        planHtml = '<div class="loopplan"><span class="lp-hd">' +
          x.loop_plan.length + ' loop' + (x.loop_plan.length === 1 ? '' : 's') + '</span>' +
          x.loop_plan.map((l, li) => {
            const ls = String(l.status || 'pending').toLowerCase();
            return '<div class="lp-row" data-scope="program:' + esc(e.ref) + '" title="open this program\'s loops">' +
              '<span class="lp-seq">' + (li + 1) + '</span>' +
              '<span class="pill ' + esc(ls) + '">' + esc(l.status || 'pending') + '</span>' +
              '<span class="lp-nm" title="' + esc(l.name || '') + '">' + esc(l.name || '') + '</span>' +
              '<span class="lp-goal" title="' + esc(l.goal || '') + '">' + esc(l.goal || '') + '</span>' +
              (l.runs ? '<span style="color:var(--dim);flex-shrink:0">×' + l.runs + '</span>' : '') +
              '</div>';
          }).join('') + '</div>';
      }
      if (e.kind === 'run' && Array.isArray(x.children) && x.children.length) {
        planHtml += '<div class="loopplan"><span class="lp-hd">' +
          x.children.length + ' child node' + (x.children.length === 1 ? '' : 's') +
          ' · non-authoritative</span>' + x.children.map((child, ci) => {
            const cs = String(child.status || 'created').toLowerCase();
            const capability = (((child.events || [])[0] || {}).payload || {}).capability || child.kind || '';
            const failed = ['failed','timed_out','cancelled'].includes(cs);
            const childLink = '/memgraph/panel?run_id=' + encodeURIComponent(child.id || '') +
              (e.session_id ? '&amp;session_id=' + encodeURIComponent(e.session_id) : '');
            return '<div class="lp-row" title="' + esc(child.id || '') + '">' +
              '<span class="lp-seq">' + esc(child.task_id || String(ci + 1)) + '</span>' +
              '<span class="pill ' + esc(cs) + '">' + esc(cs) + '</span>' +
              '<span class="lp-nm" title="' + esc(capability) + '">' + esc(capability) + '</span>' +
              (child.attempt ? '<span style="color:var(--dim);flex-shrink:0">try ' + esc(child.attempt) + '</span>' : '') +
              (child.error && child.error.code ? '<span class="pill failed">' + esc(child.error.code) + '</span>' : '') +
              (failed ? '<button class="a" data-open="' + childLink + '">inspect</button>' : '') +
              '</div>';
          }).join('') + '</div>';
      }
      if (e.kind === 'run' && (x.reconciliation || x.recovery)) {
        const rec = x.reconciliation || {}, recovery = x.recovery || {};
        const catalog = x.catalog_recovery || {};
        const state = rec.verified ? 'verified' : 'unverified';
        const checksum = rec.tip_checksum ? String(rec.tip_checksum).slice(0, 16) + '…' : 'none';
        planHtml += '<div class="loopplan"><span class="lp-hd">Recovery &amp; reconciliation · observation only</span>' +
          '<div class="lp-row"><span class="lp-seq">journal</span><span class="pill ' + (rec.verified ? 'completed' : 'waiting') + '">' + state + '</span>' +
          '<span class="lp-goal">' + esc(rec.event_count == null ? 'event count unavailable' : rec.event_count + ' gap-free events') + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">tip</span><span class="lp-goal" title="' + esc(rec.tip_checksum || '') + '">' + esc(checksum) + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">history</span><span class="lp-goal">' +
          esc((recovery.retry_count || 0) + ' retries · ' + (recovery.resume_count || 0) + ' resumes' + (recovery.interrupted ? ' · interrupted' : '')) + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">rebuild</span><span class="lp-goal">' +
          (rec.rebuild_available ? 'verified evidence can rebuild this shadow projection' : 'verified rebuild evidence unavailable') +
          '; this UI cannot execute or resume the native run</span></div>' +
          (catalog.attempted ? '<div class="lp-row"><span class="lp-seq">startup</span><span class="lp-goal">' +
            esc((catalog.recovered || 0) + ' projections recovered · ' +
                (catalog.quarantined || 0) + ' quarantined · bounded to ' +
                (catalog.bounded_limit || 'configured limit')) +
            '</span></div>' : '<div class="lp-row"><span class="lp-seq">startup</span><span class="lp-goal">' +
            'durable catalog recovery not enabled for this process</span></div>') + '</div>';
      }
      if (e.kind === 'run' && x.policy_state) {
        const policy = x.policy_state || {}, controls = Array.isArray(policy.controls) ? policy.controls : [];
        if (policy.waiting_for_approval || controls.length) {
          planHtml += '<div class="loopplan"><span class="lp-hd">Policy &amp; control evidence · read only</span>' +
            (policy.waiting_for_approval ? '<div class="lp-row"><span class="lp-seq">state</span><span class="pill waiting">approval pending</span><span class="lp-goal">Reason and requested effects are not present in this safe projection.</span></div>' : '') +
            controls.map(c => '<div class="lp-row"><span class="lp-seq">' + esc(c.action || 'control') + '</span>' +
              '<span class="pill ' + esc(c.control_status || c.status || 'waiting') + '">' + esc(c.control_status || c.status || 'recorded') + '</span>' +
              '<span class="lp-goal">' + esc(c.type || '') +
              (c.requested_by ? ' · requested by ' + esc(c.requested_by) : '') +
              (c.acknowledged_by ? ' · acknowledged by ' + esc(c.acknowledged_by) : '') + '</span></div>').join('') +
            '<div class="lp-row"><span class="lp-seq">actions</span><span class="lp-goal">No authoritative approve, reject, cancel or retry action is exposed here.</span></div></div>';
        }
      }
      if (e.kind === 'run' && x.telemetry) {
        const tel = x.telemetry || {};
        const coverage = tel.correlation_coverage == null ? 'n/a' :
          Math.round(Number(tel.correlation_coverage) * 100) + '%';
        planHtml += '<div class="loopplan"><span class="lp-hd">Portable telemetry readiness</span>' +
          '<div class="lp-row"><span class="lp-seq">trace</span><span class="lp-goal" title="' +
          esc(tel.trace_id || '') + '">' + esc(tel.trace_id || 'not recorded') + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">links</span><span class="lp-goal">' +
          esc((tel.linked_event_count || 0) + '/' + (tel.event_count || 0) + ' events linked · ' + coverage + ' causal coverage') +
          (tel.orphan_event_count ? ' · <span class="pill failed">' + esc(tel.orphan_event_count) + ' orphan links</span>' : '') + '</span></div>' +
          '<div class="lp-row"><span class="lp-seq">export</span><span class="pill ' +
          (tel.exported ? 'ok' : 'waiting') + '">' + (tel.exported ? 'accepted' : 'not exported') + '</span>' +
          '<span class="lp-goal">Content-redacted projection · exporter ' +
          esc(tel.exporter || 'not configured') + ' · ' + esc(tel.export_accepted || 0) + ' accepted / ' +
          esc(tel.export_failed || 0) + ' failed' +
          (tel.export_last_duration_ms == null ? '' : ' · last ' + esc(tel.export_last_duration_ms) + ' ms') +
          '.</span></div></div>';
      }
      let evidenceHtml = '';
      let waterfallHtml = '';
      if (e.kind === 'run') {
        const lifecycle = Array.isArray(x.lifecycle) ? x.lifecycle : [];
        const childEvents = (Array.isArray(x.children) ? x.children : []).flatMap(child =>
          (child.events || []).map(event => ({ ...event, task_id: child.task_id })));
        const evidence = lifecycle.concat(childEvents).slice(0, 60);
        const artifacts = Array.isArray(x.artifact_refs) ? x.artifact_refs : [];
        if (evidence.length || artifacts.length) {
          const rows = evidence.map(event => {
            const payload = Object.entries(event.payload || {}).map(([k,v]) =>
              k + '=' + (typeof v === 'object' ? JSON.stringify(v) : String(v))).join(' · ');
            return '<div class="ev-row" title="' + esc(event.id || '') + '">' +
              '<span class="ev-seq">' + esc(event.task_id || event.sequence || '') + '</span>' +
              '<span class="ev-type">' + esc(event.type || event.status || '') + '</span>' +
              '<span class="ev-data">' + esc(payload || event.occurred_at || '') + '</span></div>';
          }).join('');
          const refs = artifacts.map(a => {
            const flags = [a.partial ? 'partial' : 'complete ref',
              a.checksum_recorded ? (a.checksum_algorithm || 'checksum') + ' recorded' : 'no checksum',
              'availability ' + (a.availability || 'unchecked'),
              'content not verified'];
            return '<div class="ev-row"><span class="ev-seq">' + esc(a.task_id || 'ref') + '</span>' +
              '<span class="ev-type">' + esc(a.kind || 'artifact') + '</span><span class="ev-data" title="' +
              esc(a.checksum || '') + '">' + esc(a.uri || a.id || '') + '<br>' +
              esc(flags.join(' · ')) + (a.provenance_scheme ? ' · provenance ' + esc(a.provenance_scheme) : '') +
              (a.media_type ? ' · ' + esc(a.media_type) : '') +
              (a.size_bytes != null ? ' · ' + esc(a.size_bytes) + ' bytes' : '') + '</span></div>';
          }).join('');
          evidenceHtml = '<details class="evidence"><summary>Execution evidence · ' + evidence.length +
            ' lifecycle event' + (evidence.length === 1 ? '' : 's') + ' · ' + artifacts.length +
            ' artifact ref' + (artifacts.length === 1 ? '' : 's') + '</summary>' + rows + refs + '</details>';
        }
        const wf = runWaterfall(x);
        if (wf && wf.spans.length) {
          const rows = wf.spans.map(span => {
            const left = ((span.start - wf.start) / wf.total) * 100;
            const width = Math.max(.5, ((span.end - span.start) / wf.total) * 100);
            const status = String(span.child.status || 'running');
            const cls = ['failed','timed_out','cancelled'].includes(status) ? ' fail' :
              (Number(span.child.attempt || 1) > 1 ? ' retry' : '');
            return '<div class="wf-row"><span class="wf-label" title="' + esc(span.capability) + '">' +
              esc(span.capability) + (span.child.attempt > 1 ? ' · try ' + esc(span.child.attempt) : '') +
              '</span><span class="wf-track"><span class="wf-bar' + cls + '" style="left:' +
              left.toFixed(2) + '%;width:' + width.toFixed(2) + '%"></span></span><span class="wf-time">' +
              fmtDuration(span.end - span.start) + '</span></div>';
          }).join('');
          waterfallHtml = '<details class="waterfall" open><summary class="wf-head"><span>Observed performance waterfall</span>' +
            '<span>' + fmtDuration(wf.total) + ' total · ' + fmtDuration(wf.unattributed) +
            ' unattributed</span></summary>' + rows +
            '<div class="wf-head" title="Time outside observed child capability spans; requires deeper boundaries before attribution">' +
            '<span>Unattributed orchestration gap</span><span>' + fmtDuration(wf.unattributed) + '</span></div></details>';
        }
      }
      return '<article class="card' + (watching ? ' watching' : '') + '" data-card="' + i + '" tabindex="0" aria-label="' + esc(m.l + ': ' + (e.title || 'activity')) + '" style="border-top:2px solid ' + col + '">' +
        '<div class="card-hd"><span class="card-ic" style="color:' + col + '">' + m.i + '</span>' +
        '<span class="card-tt">' + esc(e.title) + '</span>' +
        (e.status ? '<span class="pill ' + esc(st) + '">' + esc(e.status) + '</span>' : '') + '</div>' +
        '<div class="card-meta"><span data-time="' + i + '">' + esc(relTime(e.ts)) + '</span>' +
        meta.map(v => '<span>' + v + '</span>').join('') + '</div>' +
        (e.summary ? '<div class="card-bd">' + mdRender(e.summary) + '</div>' : '<div class="card-bd" style="color:var(--dim)">—</div>') +
        planHtml +
        waterfallHtml +
        evidenceHtml +
        (acts.length ? '<div class="card-ft">' + acts.join('') + '</div>' : '') +
        '<div class="watchbox" data-watchbox="' + i + '"></div>' +
        '</article>';
    }

    _paintCards(evs) {
      const box = this.shadowRoot.querySelector('#cards');
      if (!box) return;
      if (!evs.length) { box.innerHTML = '<div class="empty">No activity for this scope yet.</div>'; return; }
      // evs is newest-first (the /activity/timeline API's own contract), but
      // the timeline RAIL below is a plain chronological axis — oldest at the
      // left (0%), newest at the right (100%). Rendering cards in API order
      // put the newest card at the far LEFT of the carousel, the opposite end
      // from where the rail puts it, so clicking a rail node (always scrolling
      // toward "newest = right") landed you looking the wrong way for a card
      // that was actually leftmost. Render oldest -> newest so both views
      // agree on which side is "now". `i` passed to _cardHtml stays the
      // ORIGINAL evs index (not the display position) — the rail's node
      // index, the open-watch set, and _refreshTimes()'s data-time lookup all
      // key off it, so only the rendering order flips, nothing else.
      const order = evs.map((e, i) => i).reverse();
      box.innerHTML = order.map(i => this._cardHtml(evs[i], i)).join('');
      box.querySelectorAll('[data-open]').forEach(b => b.addEventListener('click', ev => { ev.stopPropagation(); this._openUi(b.getAttribute('data-open')); }));
      box.querySelectorAll('[data-scope]').forEach(b => b.addEventListener('click', ev => { ev.stopPropagation(); this.setScope(b.getAttribute('data-scope')); }));
      box.querySelectorAll('[data-files]').forEach(b => b.addEventListener('click', ev => { ev.stopPropagation(); this.setScope(b.getAttribute('data-files')); this._openFiles(); }));
      box.querySelectorAll('[data-watch]').forEach(b => b.addEventListener('click', ev => { ev.stopPropagation(); this._toggleWatch(parseInt(b.getAttribute('data-watch'), 10)); }));
      box.querySelectorAll('[data-remove]').forEach(b => b.addEventListener('click', async ev => {
        ev.stopPropagation();
        const t = b.getAttribute('data-remove');
        if (!confirm('Remove ' + t + '? This stops its loops and deletes it.')) return;
        await this._fetch('/activity/stream/remove', 'POST', { target: t });
        this._lastSig = ''; this.load(); this._loadPipelines();
      }));
      box.querySelectorAll('[data-stop]').forEach(b => b.addEventListener('click', async ev => {
        ev.stopPropagation();
        if (!confirm('Stop loop ' + b.getAttribute('data-stop') + '?')) return;
        await this._fetch('/activity/loops/stop', 'POST', { session_id: b.getAttribute('data-stop') });
        this._lastSig = ''; this.load();
      }));
    }

    _paintRail(evs) {
      const rail = this.shadowRoot.querySelector('#rail');
      const hd = this.shadowRoot.querySelector('#railHd');
      if (!rail) return;
      if (!evs.length) { rail.innerHTML = '<div class="rail-line"></div>'; if (hd) hd.textContent = ''; return; }
      const times = evs.map(e => Date.parse(e.ts) || Date.now());
      let min = Math.min.apply(null, times), max = Math.max.apply(null, times);
      if (min === max) { min -= 60000; max += 60000; }
      const span = max - min;
      if (hd) hd.innerHTML = '<span>' + esc(new Date(min).toLocaleString()) + '</span><span>' +
        evs.length + ' events</span><span>' + esc(new Date(max).toLocaleString()) + '</span>';
      let html = '<div class="rail-line"></div><div class="rail-tip" id="railTip"></div>';
      evs.forEach((e, i) => {
        const pct = (((Date.parse(e.ts) || max) - min) / span) * 100;
        html += '<button class="node" data-node="' + i + '" style="left:' + pct.toFixed(2) +
          '%;background:var(' + kindMeta(e.kind).c + ')" title="' + esc(e.title) +
          '" aria-label="Focus ' + esc(kindMeta(e.kind).l + ': ' + (e.title || 'activity')) + '"></button>';
      });
      rail.innerHTML = html;
      const tip = rail.querySelector('#railTip');
      rail.querySelectorAll('.node').forEach(n => {
        const i = parseInt(n.getAttribute('data-node'), 10);
        n.addEventListener('mouseenter', () => {
          if (!tip) return;
          const e = evs[i];
          tip.innerHTML = esc(kindMeta(e.kind).l) + ' · ' + esc(relTime(e.ts)) + '<br>' + esc(e.title);
          tip.style.left = n.style.left; tip.classList.add('on');
        });
        n.addEventListener('mouseleave', () => { if (tip) tip.classList.remove('on'); });
        n.addEventListener('click', () => this._focusCard(i));
      });
    }

    _focusCard(i) {
      const box = this.shadowRoot.querySelector('#cards');
      const card = box && box.querySelector('[data-card="' + i + '"]');
      const node = this.shadowRoot.querySelector('.node[data-node="' + i + '"]');
      box && box.querySelectorAll('.card.focus').forEach(c => c.classList.remove('focus'));
      this.shadowRoot.querySelectorAll('.node.focus').forEach(n => n.classList.remove('focus'));
      if (card) { card.classList.add('focus'); card.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' }); }
      if (node) node.classList.add('focus');
    }

    _openUi(url) {
      if (!url) return;
      try {
        if (window.parent && window.parent.veraOpenPanel) { window.parent.veraOpenPanel(url); return; }
        if (window.parent && window.parent.panelOpen) { window.parent.panelOpen(url); return; }
      } catch (e) {}
      const base = this._apiBase || apiBase();
      window.open((url.startsWith('http') ? url : base + url), '_blank', 'noopener');
    }

    _toggleWatch(i) {
      const e = this._visibleEvents()[i];
      if (!e || !e.session_id) return;
      if (this._openWatch.has(e.session_id)) this._openWatch.delete(e.session_id);
      else this._openWatch.add(e.session_id);
      this._lastSig = '';
      this._paint();
    }

    _reopenWatches() {
      // Mount an agent-loop-output for every open watch whose card is present.
      const evs = this._visibleEvents();
      if (!customElements.get('vera-agent-loop-output') && this._openWatch.size) {
        const s = document.createElement('script');
        s.src = (this._apiBase || apiBase()) + '/ui/elements/agent_loop_output.js';
        document.head.appendChild(s);
      }
      evs.forEach((e, i) => {
        if (!e.session_id || !this._openWatch.has(e.session_id)) return;
        const wb = this.shadowRoot.querySelector('[data-watchbox="' + i + '"]');
        if (!wb || wb.firstChild) return;   // already mounted
        const ui = e.ui || {};
        const reattach = ui.reattach ||
          (ui.session_id ? '/workshop/agent_loop/reattach?session_id=' + encodeURIComponent(ui.session_id) : '');
        if (!reattach) { wb.innerHTML = '<div class="empty">No live stream.</div>'; return; }
        const alo = document.createElement('vera-agent-loop-output');
        alo.setAttribute('compact', 'true');
        wb.appendChild(alo);
        const go = () => {
          try {
            if (alo.setApiBase) alo.setApiBase(this._apiBase || apiBase());
            if (alo.setSessionId && ui.session_id) alo.setSessionId(ui.session_id);
            if (alo.bindStream) alo.bindStream(reattach, null, { method: 'GET' });
          } catch (err) {}
        };
        if (alo.bindStream) go(); else setTimeout(go, 400);
      });
    }

    _ping() {
      // Tell the backend a human is active so BACKGROUND loops yield the GPU.
      try { this._fetch('/activity/ping', 'POST', { source: 'activity-ui' }); } catch (e) {}
    }

    _drawerOpen() {
      const d = this.shadowRoot.querySelector('#drawer');
      return !!(d && d.classList.contains('on'));
    }
    _closeDrawer() {
      const d = this.shadowRoot.querySelector('#drawer');
      if (d) d.classList.remove('on');
      this._drawerMode = null;
    }

    async _openSandboxes() {
      this._ping();
      const d = this.shadowRoot.querySelector('#drawer'); if (d) d.classList.add('on');
      const inner = this.shadowRoot.querySelector('#drawerIn');
      if (!inner) return;
      this._drawerMode = 'sandboxes';
      inner.innerHTML = '<div class="empty">Loading sandboxes…</div>';
      const r = await this._fetch('/activity/sandboxes?scope=' + encodeURIComponent(this._scope));
      const boxes = (r && r.sandboxes) || [];
      if (!boxes.length) { inner.innerHTML = '<div class="empty">No sandbox containers for this scope.</div>'; return; }
      inner.innerHTML = '<div style="font-size:9.5px;color:var(--dim2);margin-bottom:4px">Sandbox containers for this scope — open a terminal or inspect its context.</div>' +
        boxes.map(b => {
          const sid = esc(b.session_id || '');
          const state = esc(b.state || (b.active ? 'active' : 'idle'));
          return '<div class="sbx-row">' +
            '<span class="pill ' + (b.state === 'running' ? 'running' : '') + '">' + state + '</span>' +
            '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + sid + '">' +
            '<b>' + esc(b.kind || 'session') + '</b> · ' + esc(b.label || b.session_id || '') + '</span>' +
            '<span style="color:var(--dim2);font-size:9px">' + (b.session_count || (b.sessions ? b.sessions.length : 0)) + ' sess</span>' +
            '<button class="a" data-term="' + esc(b.terminal_url || '') + '">⌨ Terminal</button>' +
            '</div>';
        }).join('');
      inner.querySelectorAll('[data-term]').forEach(btn => btn.addEventListener('click', () => {
        const u = btn.getAttribute('data-term'); if (u) this._openUi(u);
      }));
    }

    async _openFiles(path) {
      this._ping();
      const d = this.shadowRoot.querySelector('#drawer'); if (d) d.classList.add('on');
      const inner = this.shadowRoot.querySelector('#drawerIn');
      if (!inner) return;
      this._drawerMode = 'files';
      inner.innerHTML = '<div class="empty">Loading files…</div>';
      const r = await this._fetch('/activity/files?scope=' + encodeURIComponent(this._scope) +
        (path ? '&path=' + encodeURIComponent(path) : ''));
      if (!r) { inner.innerHTML = '<div class="empty">No files for this scope.</div>'; return; }
      const owner = r.owner || '';
      const entries = r.entries || [];
      const arts = r.artifacts || [];
      const base = this._apiBase || apiBase();
      let head = '<div style="font-size:9.5px;color:var(--dim2);margin-bottom:6px">' +
        (owner ? 'Sandbox <b style="color:var(--acc)">' + esc(owner) + '</b>' +
          (r.container ? ' · ' + esc(r.container) : '') + (r.running ? ' · <span style="color:var(--ok)">running</span>' : '')
          : 'No sandbox container for this scope yet.') + '</div>';
      if (owner) {
        head += '<div style="font-size:9px;color:var(--dim);margin-bottom:4px">' + esc(r.path || '/workspace') +
          (r.parent ? ' <a href="#" data-up="' + esc(r.parent) + '" style="color:var(--acc)">↑ up</a>' : '') + '</div>';
      }
      const fileRows = entries.map(en => {
        const isDir = en.kind === 'directory';
        const p = esc(en.path || '');
        return '<div class="sbx-row">' +
          '<span style="width:16px">' + (isDir ? '📁' : '📄') + '</span>' +
          (isDir
            ? '<a href="#" data-dir="' + p + '" style="flex:1;color:var(--acc);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(en.name) + '</a>'
            : '<a href="' + base + '/activity/file?session_id=' + encodeURIComponent(owner) + '&path=' + encodeURIComponent(en.path || '') +
              '" target="_blank" rel="noopener" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(en.name) + '">' + esc(en.name) + '</a>') +
          '<span style="color:var(--dim2);font-size:9px">' + (en.size || 0) + 'b</span>' +
          '</div>';
      }).join('') || (owner ? '<div class="empty">Empty directory.</div>' : '');
      const artRows = arts.length ? ('<div style="font-size:9px;color:var(--dim);margin:8px 0 3px;text-transform:uppercase;letter-spacing:.5px">Artifacts</div>' +
        arts.map(a => '<div class="sbx-row"><span style="width:16px">▤</span>' +
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(a.name || '') + '">' + esc(a.name || a.type || '') + '</span>' +
          '<span style="color:var(--dim2);font-size:9px">' + (a.size || 0) + 'b</span></div>').join('')) : '';
      inner.innerHTML = head + fileRows + artRows;
      inner.querySelectorAll('[data-dir]').forEach(a => a.addEventListener('click', ev => { ev.preventDefault(); this._openFiles(a.getAttribute('data-dir')); }));
      inner.querySelectorAll('[data-up]').forEach(a => a.addEventListener('click', ev => { ev.preventDefault(); this._openFiles(a.getAttribute('data-up')); }));
    }

    async _flatten() {
      this._ping();
      const dry = await this._fetch('/activity/loops/flatten', 'POST', { scope: this._scope, dry_run: true });
      if (!dry) { alert('Flatten failed.'); return; }
      const msg = 'Flatten background loops for "' + this._scope + '"?\n\n' +
        '• prune ' + (dry.pruned_stale || 0) + ' stale/finished session(s) from the live set\n' +
        '• stop ' + (dry.stopped_duplicates || 0) + ' duplicate running loop(s)\n\nProceed?';
      if (!confirm(msg)) return;
      const r = await this._fetch('/activity/loops/flatten', 'POST', { scope: this._scope, dry_run: false });
      alert('Pruned ' + ((r && r.pruned_stale) || 0) + ' stale, stopped ' + ((r && r.stopped_duplicates) || 0) + ' duplicate loop(s).');
      this._lastSig = ''; this.load();
    }

    async _stopAll() {
      this._ping();
      const evs = this._visibleEvents();
      const loops = evs.filter(e => e.session_id &&
        (String(e.status).toLowerCase() === 'running' || (e.extra && e.extra.running)));
      const progs = evs.filter(e => e.kind === 'program' && String(e.status).toLowerCase() === 'active' && e.ref);
      if (!loops.length && !progs.length) { alert('Nothing running to stop in this view.'); return; }
      if (!confirm('Stop ' + loops.length + ' running loop(s)' +
        (progs.length ? ' and pause ' + progs.length + ' active program(s)' : '') + '?')) return;
      let stopped = 0;
      for (const e of loops) {
        const r = await this._fetch('/activity/loops/stop', 'POST', { session_id: e.session_id });
        if (r && r.ok) stopped++;
      }
      for (const p of progs) await this._fetch('/loops/program/pause', 'POST', { id: p.ref });
      alert('Stopped ' + stopped + ' loop(s)' + (progs.length ? ', paused ' + progs.length + ' program(s)' : '') + '.');
      this._lastSig = ''; this.load();
    }

    async _removeStream() {
      const kind = (this._scope.split(':')[0] || '').toLowerCase();
      if (!['program', 'project', 'goal'].includes(kind)) { alert('Select a program, project or goal to remove.'); return; }
      const archive = (kind !== 'program') &&
        confirm('OK = ARCHIVE (keep record, stop work).\nCancel = DELETE permanently.\n\nArchive "' + this._scope + '"?') === false
        ? false : (kind !== 'program');
      if (!confirm('Remove "' + this._scope + '"?\nThis stops its loops' +
        (kind !== 'program' ? ' and deletes its driving V8 program' : '') +
        (archive ? ' and ARCHIVES it.' : ' and ' + (kind === 'program' ? 'deletes the program.' : 'DELETES the project.')))) return;
      const r = await this._fetch('/activity/stream/remove', 'POST', { target: this._scope, archive: archive });
      if (r && r.ok) { alert('Removed: ' + (r.removed || []).join(', ')); this.setScope('all'); this._loadPipelines(); }
      else alert('Remove failed: ' + ((r && r.error) || '?'));
    }

    _syncBar() {
      const back = this.shadowRoot.querySelector('#back');
      if (back) back.style.display = (this._scope !== 'all' || this._scopeStack.length) ? '' : 'none';
      const sc = this.shadowRoot.querySelector('#scopeLbl');
      if (sc) sc.textContent = this._scope === 'all' ? '' : this._scope;
      const kind = (this._scope.split(':')[0] || '').toLowerCase();
      const rm = this.shadowRoot.querySelector('#rmBtn');
      const fb = this.shadowRoot.querySelector('#filesBtn');
      const isEntity = ['program', 'project', 'goal', 'dream'].includes(kind);
      if (rm) rm.style.display = ['program', 'project', 'goal'].includes(kind) ? '' : 'none';
      if (fb) fb.style.display = isEntity ? '' : 'none';
    }

    _render() {
      const showPicker = this.getAttribute('show-picker') !== 'false';
      this.shadowRoot.innerHTML = '<style>' + STYLE + '</style>' +
        '<div class="bar">' +
        '<button class="btn" id="back" title="Back" style="display:none">←</button>' +
        '<span class="title">Activity</span>' +
        (showPicker ? '<select id="scopeSel"></select>' : '<span id="scopeLbl" style="font-size:10px;color:var(--dim2,#8a7e70);font-family:var(--mono,monospace)"></span>') +
        '<div id="chips" class="chips"></div>' +
        '<select id="statusFilter" title="Filter lifecycle status"><option value="">all status</option><option value="active">active</option><option value="failed">failed</option><option value="complete">complete</option></select>' +
        '<select id="windowFilter" title="Filter event age"><option value="0">all time</option><option value="15">15m</option><option value="60">1h</option><option value="1440">24h</option><option value="10080">7d</option></select>' +
        '<button class="btn" id="triageBtn" aria-pressed="false" title="Show failed parents and failed child actions, including completed parents">⚠ Failures 0</button>' +
        '<span class="flex"></span>' +
        '<button class="btn" id="filesBtn" title="Browse the files + artifacts this scope\'s sandbox produced" style="display:none">📂 Files</button>' +
        '<button class="btn" id="sbxBtn" title="Sandbox containers + terminals for this scope">📦 Sandboxes</button>' +
        '<button class="btn" id="flatBtn" title="Prune stale sessions + stop duplicate loops">⚑ Flatten</button>' +
        '<button class="btn warn" id="stopBtn" title="Stop every running loop + pause active programs shown">⏹ Stop all</button>' +
        '<button class="btn warn" id="rmBtn" title="Remove this stream (stop its work + delete/archive)" style="display:none">🗑 Remove</button>' +
        '<span class="live"><span class="dot"></span>live</span>' +
        '<button class="btn" id="refresh" title="Refresh">↻</button>' +
        '</div>' +
        '<div class="triage" id="triage"></div>' +
        '<div class="drawer" id="drawer"><div class="drawer-in" id="drawerIn"></div></div>' +
        '<div class="sr-only" id="activityStatus" role="status" aria-live="polite"></div>' +
        '<div class="cards" id="cards" role="feed" aria-label="Activity events"></div>' +
        '<div class="rail-wrap"><div class="rail-hd" id="railHd"></div><div class="rail" id="rail" aria-label="Activity time navigation"><div class="rail-line"></div></div></div>';
      const sel = this.shadowRoot.querySelector('#scopeSel');
      if (sel) sel.addEventListener('change', () => this.setScope(sel.value));
      const statusFilter = this.shadowRoot.querySelector('#statusFilter');
      if (statusFilter) statusFilter.addEventListener('change', () => {
        this._statusFilter = statusFilter.value; this._lastSig = ''; this._paint();
      });
      const windowFilter = this.shadowRoot.querySelector('#windowFilter');
      if (windowFilter) windowFilter.addEventListener('change', () => {
        this._windowMinutes = parseInt(windowFilter.value, 10) || 0;
        this._lastSig = ''; this._paint();
      });
      const bind = (id, fn) => { const el = this.shadowRoot.querySelector(id); if (el) el.addEventListener('click', fn); };
      bind('#refresh', () => { this._ping(); this._lastSig = ''; this.load(); });
      bind('#triageBtn', () => { this._failureOnly = !this._failureOnly;
        const b = this.shadowRoot.querySelector('#triageBtn'); if (b) b.setAttribute('aria-pressed', String(this._failureOnly));
        this._lastSig = ''; this._paint(); });
      bind('#filesBtn', () => { if (this._drawerMode === 'files' && this._drawerOpen()) this._closeDrawer(); else this._openFiles(); });
      bind('#sbxBtn', () => { if (this._drawerMode === 'sandboxes' && this._drawerOpen()) this._closeDrawer(); else this._openSandboxes(); });
      bind('#flatBtn', () => this._flatten());
      bind('#stopBtn', () => this._stopAll());
      bind('#rmBtn', () => this._removeStream());
      bind('#back', () => {
        const prev = this._scopeStack.pop() || 'all';
        this.setScope(prev, { noPush: true });
      });
      this._syncBar();
    }
  }

  customElements.define('vera-activity-timeline', VeraActivityTimeline);
})();
