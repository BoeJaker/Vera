/**
 * <vera-calendar> — a reusable month / week / day calendar.
 *
 * The Calendar panel (vera/calendar/calendar_panel.html) draws its own grid
 * wired to /cal/events; nothing else in Vera could show events on a
 * calendar. This element is that grid, made reusable: it draws whatever
 * events it is given and tells its host what was clicked, and knows nothing
 * about where events come from. Loop Lab's Schedule page is the first host
 * (2026-09-21); the Calendar panel can adopt it later.
 *
 * Feed it either way:
 *   el.events = [{id, title, start, end, all_day, color, read_only, ...}]
 *   <vera-calendar src="/evolve/schedule/events"></vera-calendar>
 *       -> GET src + "&start=<iso>&end=<iso>" for the visible range, expects
 *          {events:[...]}; refresh() re-fetches; polls every `poll` ms while
 *          on screen (0 = no polling).
 *
 * Attributes: view="month|week|day" (default month), date="YYYY-MM-DD"
 * (the focused day, default today), src, poll (ms), hour-start / hour-end
 * (the hours drawn in week/day view, default 0..24), compact (smaller),
 * fill (take the host's whole height: month rows share it, week/day hours
 * are sized to fit it - the hours scroll only below a readable minimum).
 * Properties: events (get/set), view, date, filter (a function(ev) -> bool
 * the host sets to show some events only - its layers; the element keeps
 * every event and redraws). Methods: refresh(), goto(date), setView(v),
 * today(), prev(), next().
 *
 * An event may carry display: "background" (a band behind the others - a
 * schedule's window, the time something MAY run: full width, faint, drawn
 * under the chips) or "solid" (an opaque chip - a result that should read
 * at a glance). Overlapping timed events sit side by side, not on top of
 * one another. `detail` is added to a chip's hover text.
 *
 * Events (bubbling CustomEvents, detail as shown):
 *   slot-select  {date:"YYYY-MM-DD", hour, start:iso, end:iso}  a click on
 *                an empty cell / hour row (month: hour 9) — "create here"
 *   event-open   {event}   a click on an event chip
 *   range-change {view, start:iso, end:iso, date}   the visible range moved
 *
 * Times are the browser's local time; ISO strings with an offset are
 * honoured. No shadow DOM for the grid's colours: the element uses the
 * page's vera-ui tokens (--bg1/--bg2/--border/--text/--dim2/--acc) so it
 * looks native in every panel, with fallbacks for a bare page.
 */
(function () {
  if (customElements.get('vera-calendar')) return;

  const DAY_MS = 86400000;
  const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
  const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const pad = n => (n < 10 ? '0' : '') + n;
  const ymd = d => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  const parseYmd = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(); };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const toDate = s => { if (!s) return null; const d = new Date(s); return isNaN(d) ? null : d; };
  const hm = d => pad(d.getHours()) + ':' + pad(d.getMinutes());
  const monday = d => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); const w = (x.getDay() + 6) % 7; x.setDate(x.getDate() - w); return x; };
  const isoLocal = d => { const off = -d.getTimezoneOffset(); const s = off >= 0 ? '+' : '-'; const a = Math.abs(off);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':00' + s + pad(Math.floor(a / 60)) + ':' + pad(a % 60); };

  const CSS = `
  :host{display:block;font-family:var(--sans,system-ui,sans-serif);font-size:12px;color:var(--text,#ddd);--vc-line:var(--border,#333);--vc-bg:var(--bg1,#151515);--vc-bg2:var(--bg2,#1d1d1d);--vc-dim:var(--dim2,#888);--vc-acc:var(--acc,#7aa2f7)}
  :host([compact]){font-size:10.5px}
  :host([fill]){display:flex;flex-direction:column;height:100%;min-height:0}
  :host([fill]) .bar{flex:none}
  :host([fill]) .body{flex:1 1 0;min-height:0;overflow:auto}
  :host([fill]) .grid.month{height:100%;grid-template-rows:auto repeat(6,minmax(0,1fr))}
  :host([fill]) .cell{min-height:0}
  .bar{display:flex;align-items:center;gap:6px;padding:4px 0 6px;flex-wrap:wrap}
  .bar .t{font-weight:600;font-size:13px;min-width:150px}
  .bar button{background:var(--vc-bg2);border:1px solid var(--vc-line);color:var(--vc-dim);border-radius:5px;padding:2px 8px;cursor:pointer;font:inherit}
  .bar button.on,.bar button:hover{color:var(--text,#ddd);border-color:var(--vc-acc)}
  .sp{flex:1}
  .grid{display:grid;border:1px solid var(--vc-line);border-radius:6px;overflow:hidden;background:var(--vc-bg)}
  .month{grid-template-columns:repeat(7,1fr)}
  .hd{padding:4px 6px;color:var(--vc-dim);border-bottom:1px solid var(--vc-line);background:var(--vc-bg2);font-weight:600;text-align:center}
  .cell{min-height:74px;border-right:1px solid var(--vc-line);border-bottom:1px solid var(--vc-line);padding:3px 4px;cursor:pointer;position:relative;overflow:hidden}
  .cell:nth-child(7n){border-right:0}
  .cell.other{opacity:.45}
  .cell.today .dn{background:var(--vc-acc);color:#000;border-radius:9px;padding:0 5px}
  .cell.wknd{background:rgba(127,127,127,.05)}
  .dn{font-size:11px;color:var(--vc-dim);display:inline-block;margin-bottom:2px}
  .chip{display:block;border-radius:3px;padding:1px 4px;margin:1px 0;font-size:11px;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#111;cursor:pointer;border-left:3px solid transparent}
  .chip.off{opacity:.45;text-decoration:line-through}
  .more{font-size:10.5px;color:var(--vc-dim);padding-left:3px;cursor:zoom-in}
  .more:hover{color:var(--text,#ddd);text-decoration:underline}
  .chip.solid,.ev.solid{color:#10131a;font-weight:600}
  .bands{display:flex;gap:2px;margin:0 0 2px;height:4px}
  .bands i{flex:1;border-radius:2px;opacity:.75}
  .bg{position:absolute;left:0;right:0;border-radius:0;z-index:1;border-left:3px solid transparent;box-sizing:border-box;pointer-events:none;background-image:repeating-linear-gradient(135deg,rgba(255,255,255,.05) 0 6px,transparent 6px 12px)}
  .bg span{position:absolute;right:4px;top:2px;font-size:10.5px;opacity:.85;pointer-events:auto;cursor:pointer;white-space:nowrap;overflow:hidden;max-width:90%}
  .week{grid-template-columns:44px repeat(7,1fr)}
  .day{grid-template-columns:44px 1fr}
  .hr{border-right:1px solid var(--vc-line);border-bottom:1px solid var(--vc-line);color:var(--vc-dim);font-size:10.5px;text-align:right;padding:2px 4px 0 0;height:var(--vc-rh,34px);box-sizing:border-box}
  .slot{border-right:1px solid var(--vc-line);border-bottom:1px solid var(--vc-line);height:var(--vc-rh,34px);box-sizing:border-box;position:relative;cursor:pointer}
  .slot:hover{background:rgba(127,127,127,.08)}
  .col{position:relative}
  .ev{position:absolute;left:2px;right:2px;border-radius:4px;padding:2px 4px;font-size:11px;overflow:hidden;color:#111;cursor:pointer;border-left:3px solid transparent;box-sizing:border-box;z-index:2}
  .ev.off{opacity:.45;text-decoration:line-through}
  .ev b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ev:hover{z-index:4;box-shadow:0 2px 10px rgba(0,0,0,.35)}
  .allday{padding:2px 4px;border-bottom:1px solid var(--vc-line);min-height:18px}
  .empty{padding:14px;color:var(--vc-dim);text-align:center}
  .now{position:absolute;left:0;right:0;height:0;border-top:2px solid var(--vc-acc);z-index:3;pointer-events:none}
  `;

  class VeraCalendar extends HTMLElement {
    static get observedAttributes() { return ['view', 'date', 'src', 'poll', 'hour-start', 'hour-end']; }
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._events = [];
      this._view = 'month';
      this._date = new Date();
      this._pollTimer = null;
      this._loading = false;
      this._filter = null;
      this._bodyH = 0;
      this.shadowRoot.innerHTML = '<style>' + CSS + '</style><div class="bar"></div><div class="body"></div>';
    }
    connectedCallback() {
      this._view = this.getAttribute('view') || this._view;
      if (this.getAttribute('date')) this._date = parseYmd(this.getAttribute('date'));
      this.render();
      if (this.getAttribute('src')) this.refresh();
      const p = parseInt(this.getAttribute('poll') || '0', 10);
      if (p > 0) this._pollTimer = setInterval(() => { if (this.offsetParent !== null && this.getAttribute('src')) this.refresh(); }, p);
      // fill: the hours are sized to the height the host gives, so a resize redraws (once a frame, only on a real change)
      if (window.ResizeObserver && !this._ro) {
        this._ro = new ResizeObserver(() => {
          const b = this.shadowRoot.querySelector('.body'); const h = b ? b.clientHeight : 0;
          if (this.hasAttribute('fill') && Math.abs(h - this._bodyH) > 6) { this._bodyH = h; cancelAnimationFrame(this._raf); this._raf = requestAnimationFrame(() => this.render()); }
        });
        this._ro.observe(this);
      }
    }
    disconnectedCallback() { if (this._pollTimer) clearInterval(this._pollTimer); this._pollTimer = null; if (this._ro) { this._ro.disconnect(); this._ro = null; } }
    attributeChangedCallback(n, _o, v) {
      if (!this.isConnected) return;
      if (n === 'view') { this._view = v || 'month'; this.render(); }
      else if (n === 'date') { this._date = parseYmd(v); this.render(); }
      else if (n === 'src') this.refresh();
      else this.render();
    }
    get events() { return this._events; }
    set events(v) { this._events = Array.isArray(v) ? v.slice() : []; this.render(); }
    get filter() { return this._filter; }
    set filter(fn) { this._filter = typeof fn === 'function' ? fn : null; this.render(); }
    _shown() { const f = this._filter; return f ? this._events.filter(ev => { try { return f(ev) !== false; } catch (e) { return true; } }) : this._events; }
    get view() { return this._view; }
    set view(v) { this.setView(v); }
    get date() { return ymd(this._date); }
    set date(v) { this.goto(v); }

    setView(v) { if (['month', 'week', 'day'].indexOf(v) < 0) return; this._view = v; this.setAttribute('view', v); this.render(); this._emitRange(); if (this.getAttribute('src')) this.refresh(); }
    goto(d) { this._date = d instanceof Date ? d : parseYmd(d); this.setAttribute('date', ymd(this._date)); this.render(); this._emitRange(); if (this.getAttribute('src')) this.refresh(); }
    today() { this.goto(new Date()); }
    prev() { this._step(-1); }
    next() { this._step(1); }
    _step(dir) {
      const d = new Date(this._date);
      if (this._view === 'month') d.setMonth(d.getMonth() + dir);
      else if (this._view === 'week') d.setDate(d.getDate() + 7 * dir);
      else d.setDate(d.getDate() + dir);
      this.goto(d);
    }
    /** [start, end) of the visible range, local Dates. */
    range() {
      const d = this._date;
      if (this._view === 'month') {
        const first = new Date(d.getFullYear(), d.getMonth(), 1);
        const s = monday(first); const e = new Date(s); e.setDate(e.getDate() + 42);
        return [s, e];
      }
      if (this._view === 'week') { const s = monday(d); const e = new Date(s); e.setDate(e.getDate() + 7); return [s, e]; }
      const s = new Date(d.getFullYear(), d.getMonth(), d.getDate()); const e = new Date(s); e.setDate(e.getDate() + 1); return [s, e];
    }
    _emitRange() {
      const [s, e] = this.range();
      this.dispatchEvent(new CustomEvent('range-change', { bubbles: true, detail: { view: this._view, start: isoLocal(s), end: isoLocal(e), date: ymd(this._date) } }));
    }
    async refresh() {
      const src = this.getAttribute('src');
      if (!src || this._loading) return;
      this._loading = true;
      try {
        const [s, e] = this.range();
        const u = src + (src.indexOf('?') >= 0 ? '&' : '?') + 'start=' + encodeURIComponent(isoLocal(s)) + '&end=' + encodeURIComponent(isoLocal(e));
        const r = await fetch(u); const j = await r.json();
        this._events = Array.isArray(j) ? j : (j.events || []);
        this._meta = Array.isArray(j) ? {} : j;
        this.render();
        this.dispatchEvent(new CustomEvent('events-loaded', { bubbles: true, detail: { count: this._events.length, meta: this._meta } }));
      } catch (e) { /* keep the last good render */ }
      this._loading = false;
    }

    // ── rendering ────────────────────────────────────────────────────────
    _bar() {
      const d = this._date;
      const title = this._view === 'month' ? d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
        : this._view === 'week' ? (() => { const s = monday(d); const e = new Date(s); e.setDate(e.getDate() + 6); return s.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ' – ' + e.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }); })()
        : d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
      const b = this.shadowRoot.querySelector('.bar');
      b.innerHTML = '<button data-a="prev" title="previous">‹</button><button data-a="today">today</button><button data-a="next" title="next">›</button>'
        + '<span class="t">' + esc(title) + '</span><span class="sp"></span>'
        + ['month', 'week', 'day'].map(v => '<button data-v="' + v + '" class="' + (v === this._view ? 'on' : '') + '">' + v + '</button>').join('');
      b.querySelectorAll('button').forEach(x => x.addEventListener('click', ev => {
        const a = ev.currentTarget.dataset.a, v = ev.currentTarget.dataset.v;
        if (v) this.setView(v); else if (a === 'prev') this.prev(); else if (a === 'next') this.next(); else this.today();
      }));
    }
    _byDay() {
      const m = {};
      for (const ev of this._shown()) {
        const s = toDate(ev.start); if (!s) continue;
        const e = toDate(ev.end) || new Date(s.getTime() + 3600000);
        // A multi-day event lands on every day it covers.
        let d = new Date(s.getFullYear(), s.getMonth(), s.getDate());
        for (let i = 0; i < 31 && d < e; i++) { (m[ymd(d)] = m[ymd(d)] || []).push(ev); d = addDays(d, 1); }
      }
      for (const k in m) m[k].sort((a, b) => String(a.start).localeCompare(String(b.start)));
      return m;
    }
    _chipStyle(ev) {
      const c = ev.color || 'var(--vc-acc)';
      if (ev.display === 'solid') return 'background:' + c + ';border-left-color:' + c;
      if (ev.display === 'background') return 'background-color:' + c + '1f;border-left-color:' + c + '99;color:var(--text,#ddd)';
      return 'background:' + c + '33;border-left-color:' + c + ';color:var(--text,#ddd)';
    }
    _cls(ev) { return (ev.enabled === false ? ' off' : '') + (ev.display === 'solid' ? ' solid' : ''); }
    _tip(ev) { return esc(ev.title + (ev.detail ? ' · ' + ev.detail : '')); }
    _wire(root) {
      root.querySelectorAll('[data-slot]').forEach(el => el.addEventListener('click', e => {
        if (e.target.closest('[data-ev]')) return;
        if (e.target.closest('.more')) { this._date = parseYmd(el.dataset.slot); this.setView('day'); return; }   // +N more: that day, whole
        const date = el.dataset.slot, hour = parseInt(el.dataset.hour || '9', 10);
        const s = parseYmd(date); s.setHours(hour, 0, 0, 0); const en = new Date(s.getTime() + 3600000);
        this.dispatchEvent(new CustomEvent('slot-select', { bubbles: true, detail: { date, hour, start: isoLocal(s), end: isoLocal(en) } }));
      }));
      root.querySelectorAll('[data-ev]').forEach(el => el.addEventListener('click', e => {
        e.stopPropagation();
        const ev = this._events[parseInt(el.dataset.ev, 10)];
        if (ev) this.dispatchEvent(new CustomEvent('event-open', { bubbles: true, detail: { event: ev } }));
      }));
    }
    render() {
      this._bar();
      const body = this.shadowRoot.querySelector('.body');
      if (this._view === 'month') body.innerHTML = this._month();
      else body.innerHTML = this._timeGrid(this._view === 'week' ? 7 : 1);
      // again once the Text setting has rescaled the new chips (vera-ui rewrites them a beat after they land)
      if (this._view === 'month' && this.hasAttribute('fill')) { this._fitMonth(body); setTimeout(() => this._fitMonth(body), 350); }
      this._wire(body);
    }
    _fitMonth(body) {
      body.querySelectorAll('.cell').forEach(cell => {
        if (cell.scrollHeight <= cell.clientHeight + 1) return;
        const chips = [...cell.querySelectorAll('.chip')];
        let more = cell.querySelector('.more'); let hidden = more ? parseInt(more.textContent.slice(1), 10) || 0 : 0;
        if (!more) { more = document.createElement('span'); more.className = 'more'; cell.appendChild(more); }
        more.textContent = '+' + hidden + ' more';
        while (chips.length && cell.scrollHeight > cell.clientHeight + 1) { chips.pop().remove(); hidden++; more.textContent = '+' + hidden + ' more'; }
      });
    }
    _month() {
      const [s] = this.range(); const by = this._byDay(); const today = ymd(new Date()); const mon = this._date.getMonth();
      // filling: every chip is drawn and the cells trim what their share of the height cannot hold (_fitMonth) -
      // measured, so a larger text size still fits
      const per = this.hasAttribute('fill') ? 40 : 4;
      let h = '<div class="grid month">' + DOW.map(d => '<div class="hd">' + d + '</div>').join('');
      for (let i = 0; i < 42; i++) {
        const d = addDays(s, i); const k = ymd(d); const evs = by[k] || [];
        const cls = 'cell' + (d.getMonth() !== mon ? ' other' : '') + (k === today ? ' today' : '') + (d.getDay() === 0 || d.getDay() === 6 ? ' wknd' : '');
        const bgs = evs.filter(e => e.display === 'background'), fg = evs.filter(e => e.display !== 'background');
        h += '<div class="' + cls + '" data-slot="' + k + '" data-hour="9"><span class="dn">' + d.getDate() + '</span>';
        if (bgs.length) h += '<div class="bands">' + bgs.slice(0, 6).map(ev => { const st = toDate(ev.start), en = toDate(ev.end); return '<i data-ev="' + this._events.indexOf(ev) + '" style="background:' + (ev.color || 'var(--vc-acc)') + '" title="' + this._tip(ev) + (st && en ? ' ' + hm(st) + '–' + hm(en) : '') + '"></i>'; }).join('') + '</div>';
        fg.slice(0, per).forEach(ev => { const st = toDate(ev.start); const t = ev.all_day || !st ? '' : hm(st) + ' ';
          h += '<span class="chip' + this._cls(ev) + '" data-ev="' + this._events.indexOf(ev) + '" style="' + this._chipStyle(ev) + '" title="' + this._tip(ev) + '">' + esc(t + ev.title) + '</span>'; });
        if (fg.length > per) h += '<span class="more">+' + (fg.length - per) + ' more</span>';
        h += '</div>';
      }
      return h + '</div>';
    }
    _timeGrid(days) {
      const [s] = this.range(); const by = this._byDay(); const today = ymd(new Date());
      const h0 = Math.max(0, parseInt(this.getAttribute('hour-start') || '0', 10)), h1 = Math.min(24, parseInt(this.getAttribute('hour-end') || '24', 10));
      const cols = []; for (let i = 0; i < days; i++) cols.push(addDays(s, i));
      let h = '<div class="grid ' + (days === 7 ? 'week' : 'day') + '"><div class="hd"></div>';
      cols.forEach(d => { h += '<div class="hd' + (ymd(d) === today ? ' today' : '') + '">' + (days === 7 ? DOW[(d.getDay() + 6) % 7] + ' ' + d.getDate() : '') + '</div>'; });
      // all-day row
      h += '<div class="hr" style="height:auto">all day</div>';
      cols.forEach(d => { const k = ymd(d); const ad = (by[k] || []).filter(e => e.all_day);
        h += '<div class="allday" data-slot="' + k + '" data-hour="9">' + ad.map(ev => '<span class="chip' + this._cls(ev) + '" data-ev="' + this._events.indexOf(ev) + '" style="' + this._chipStyle(ev) + '" title="' + this._tip(ev) + '">' + esc(ev.title) + '</span>').join('') + '</div>'; });
      const hours = Math.max(1, h1 - h0);
      const rowH = this.hasAttribute('fill') && this._bodyH ? Math.max(26, Math.floor((this._bodyH - 58) / hours)) : 34;
      h = h.replace('<div class="grid ', '<div style="--vc-rh:' + rowH + 'px" class="grid ');
      const now = new Date();
      for (let hr = h0; hr < h1; hr++) {
        h += '<div class="hr">' + pad(hr) + ':00</div>';
        cols.forEach(d => {
          const k = ymd(d); let inner = '';
          if (hr === h0) {
            // Timed events for the day are positioned absolutely inside the first row's column wrapper.
            const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate(), h0); const dayEnd = new Date(d.getFullYear(), d.getMonth(), d.getDate(), h1);
            const span = ev => { const st = toDate(ev.start); const en = toDate(ev.end) || new Date(st.getTime() + 3600000);
              return { ev, st, en, a: Math.max(st.getTime(), dayStart.getTime()), b: Math.min(en.getTime(), dayEnd.getTime()) }; };
            const timed = (by[k] || []).filter(e => !e.all_day).map(span).filter(x => x.b > x.a);
            // background bands first, under everything: the windows work MAY run in
            timed.filter(x => x.ev.display === 'background').forEach(x => {
              const top = (x.a - dayStart.getTime()) / 3600000 * rowH, hgt = Math.max(8, (x.b - x.a) / 3600000 * rowH);
              inner += '<div class="bg' + this._cls(x.ev) + '" data-ev="' + this._events.indexOf(x.ev) + '" style="top:' + top + 'px;height:' + hgt + 'px;' + this._chipStyle(x.ev) + '" title="' + this._tip(x.ev) + ' ' + esc(hm(x.st) + '–' + hm(x.en)) + '"><span>' + esc(x.ev.title) + '</span></div>';
            });
            // the rest side by side where they overlap: clusters of overlapping events, a column each inside one
            const fg = timed.filter(x => x.ev.display !== 'background').sort((p, q) => p.a - q.a || q.b - p.b);
            let cluster = [], cEnd = -Infinity;
            const flush = () => {
              const cols = [];
              cluster.forEach(x => { let c = cols.findIndex(end => end <= x.a); if (c < 0) { c = cols.length; cols.push(0); } cols[c] = x.b; x.col = c; });
              cluster.forEach(x => {
                const n = cols.length, w = 100 / n;
                const top = (x.a - dayStart.getTime()) / 3600000 * rowH, hgt = Math.max(16, (x.b - x.a) / 3600000 * rowH - 2);
                inner += '<div class="ev' + this._cls(x.ev) + '" data-ev="' + this._events.indexOf(x.ev) + '" style="top:' + top + 'px;height:' + hgt + 'px;left:calc(' + (x.col * w) + '% + 2px);right:auto;width:calc(' + w + '% - 4px);' + this._chipStyle(x.ev) + '" title="' + this._tip(x.ev) + '"><b>' + esc(x.ev.title) + '</b>' + (hgt > 30 ? esc(hm(x.st) + '–' + hm(x.en)) : '') + '</div>';
              });
              cluster = []; cEnd = -Infinity;
            };
            fg.forEach(x => { if (cluster.length && x.a >= cEnd) flush(); cluster.push(x); cEnd = Math.max(cEnd, x.b); });
            if (cluster.length) flush();
            if (k === today && now.getHours() >= h0 && now.getHours() < h1) inner += '<div class="now" style="top:' + ((now.getHours() - h0) * rowH + now.getMinutes() / 60 * rowH) + 'px"></div>';
          }
          h += '<div class="slot col" data-slot="' + k + '" data-hour="' + hr + '">' + inner + '</div>';
        });
      }
      return h + '</div>';
    }
  }
  customElements.define('vera-calendar', VeraCalendar);
})();
