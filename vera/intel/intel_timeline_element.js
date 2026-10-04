/* <vera-intel-timeline topic="<id>" days="90" base="">
 *
 * A topic's history you can stand in. One column per day (height = signals
 * found, colour = the best score that day), diamonds where the assessment was
 * rewritten, squares where a report went out. Click or drag along the rail -
 * or use the arrow keys - and the card below shows the topic AS OF that day:
 * the assessment in force then, what had changed, and that day's signals.
 *
 * No topic = every topic: the card lists the day's events instead.
 * Fires `intel-asof` {date, topic} (bubbles, composed) so a host page can
 * follow the scrubber. Works the same on a dashboard (widget `element` form),
 * on the canvas and inside the Intelligence panel.
 */
(function () {
  if (customElements.get('vera-intel-timeline')) return;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const unwrap = (j) => (j && j.type === 'tool_result' && j.content) ? j.content : j;
  const CSS = `
:host{display:block;font:13px/1.45 var(--font-ui,system-ui,sans-serif);color:var(--fg,#e6e8ee);--it-acc:var(--accent,var(--acc,#7aa2f7));--it-dim:var(--muted,var(--dim,#9aa3b2));--it-bd:var(--line,var(--border,#2a2f3a));--it-card:var(--panel,var(--card,#171a21))}
.bar{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.bar b{font-weight:600}.bar .sp{flex:1}.rng button{background:none;border:1px solid var(--it-bd);color:var(--it-dim);border-radius:6px;padding:2px 8px;cursor:pointer;font:inherit;font-size:12px}
.rng button.on{border-color:var(--it-acc);color:var(--it-acc)}
.rail{position:relative;height:86px;display:flex;align-items:flex-end;gap:1px;padding:16px 0 14px;cursor:crosshair;outline:none;border-bottom:1px solid var(--it-bd);user-select:none}
.rail:focus-visible{box-shadow:0 0 0 2px var(--it-acc)}
.d{flex:1;min-width:1px;position:relative;height:100%;display:flex;align-items:flex-end}
.d i{display:block;width:100%;border-radius:2px 2px 0 0;background:var(--it-acc);opacity:.25;min-height:1px}
.d.sel i{opacity:1;outline:1px solid var(--fg,#fff)}
.d .sn{position:absolute;top:-14px;left:50%;transform:translateX(-50%) rotate(45deg);width:7px;height:7px;background:var(--warn,#e0af68)}
.d .rp{position:absolute;bottom:-12px;left:50%;transform:translateX(-50%);width:6px;height:6px;background:var(--ok,#9ece6a)}
.cur{position:absolute;top:0;bottom:0;width:1px;background:var(--fg,#fff);pointer-events:none;opacity:.6}
.axis{display:flex;justify-content:space-between;color:var(--it-dim);font-size:11px;margin-top:3px}
.card{margin-top:10px;background:var(--it-card);border:1px solid var(--it-bd);border-radius:10px;padding:10px 12px}
.card h4{margin:0 0 4px;font-size:13px}.card .m{color:var(--it-dim);font-size:11.5px}
.card .st{margin:6px 0;white-space:pre-wrap}.card ul{margin:4px 0;padding-left:18px}
.ev{display:grid;grid-template-columns:30px 1fr;gap:8px;padding:5px 0;border-top:1px solid var(--it-bd)}
.ev .s{font-weight:700;text-align:center;color:var(--it-dim)}.ev .s.hi{color:var(--ok,#9ece6a)}
.ev a{color:inherit;text-decoration:none}.ev a:hover{text-decoration:underline}
.empty{color:var(--it-dim);padding:12px 0}
.lg{display:flex;gap:12px;color:var(--it-dim);font-size:11px}.lg span:before{content:"";display:inline-block;width:8px;height:8px;margin-right:4px;vertical-align:middle}
.lg .a:before{background:var(--it-acc)}.lg .b:before{background:var(--warn,#e0af68);transform:rotate(45deg)}.lg .c:before{background:var(--ok,#9ece6a)}`;

  class VeraIntelTimeline extends HTMLElement {
    static get observedAttributes() { return ['topic', 'days', 'base']; }
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._tl = null; this._sel = -1; this._seq = 0;
    }
    connectedCallback() { this._render(); this._load(); }
    attributeChangedCallback(n, a, b) { if (a !== b && this.isConnected) { this._sel = -1; this._load(); } }
    get base() { return (this.getAttribute('base') || '').replace(/\/$/, ''); }
    get topic() { return this.getAttribute('topic') || ''; }
    get days() { return Math.max(7, Math.min(730, +(this.getAttribute('days') || 90))); }
    /** Jump to a date (YYYY-MM-DD) from outside. */
    seek(date) { const i = (this._tl && this._tl.days || []).findIndex((d) => d.date === date); if (i >= 0) this._pick(i); }

    async _get(path) {
      const r = await fetch(this.base + path, { credentials: 'same-origin' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return unwrap(await r.json());
    }
    async _load() {
      const seq = ++this._seq;
      this._msg('loading…');
      try {
        const tl = await this._get('/intel/timeline/get?days=' + this.days + (this.topic ? '&topic_id=' + encodeURIComponent(this.topic) : ''));
        if (seq !== this._seq) return;
        if (tl && tl.error) throw new Error(tl.error);
        this._tl = tl;
        this._render();
        const days = tl.days || [];
        let last = -1; days.forEach((d, i) => { if (d.signals || d.snapshots) last = i; });
        this._pick(this._sel >= 0 ? this._sel : (last >= 0 ? last : days.length - 1), true);
      } catch (e) { if (seq === this._seq) this._msg('timeline unavailable: ' + esc(e.message || e)); }
    }
    _msg(t) { const c = this.shadowRoot.querySelector('.card'); if (c) c.innerHTML = '<div class="empty">' + t + '</div>'; }
    _render() {
      const tl = this._tl || { days: [], events: [] };
      const days = tl.days || [];
      const max = Math.max(1, ...days.map((d) => d.signals || 0));
      const bars = days.map((d, i) => {
        const h = d.signals ? 8 + 92 * (d.signals / max) : 0;
        const op = d.top_score == null ? '' : ';opacity:' + (0.3 + 0.07 * d.top_score).toFixed(2);
        return '<div class="d' + (i === this._sel ? ' sel' : '') + '" data-i="' + i + '" title="' + esc(d.date + ' · ' + (d.signals || 0) + ' signals' + (d.top ? ' · ' + d.top : '')) + '">'
          + (d.snapshots ? '<span class="sn"></span>' : '') + (d.reports ? '<span class="rp"></span>' : '')
          + '<i style="height:' + h.toFixed(1) + '%' + op + '"></i></div>';
      }).join('');
      const R = [30, 90, 365].map((n) => '<button data-r="' + n + '" class="' + (n === this.days ? 'on' : '') + '">' + (n === 365 ? '1y' : n + 'd') + '</button>').join('');
      this.shadowRoot.innerHTML = '<style>' + CSS + '</style>'
        + '<div class="bar"><b>Timeline</b><span class="lg"><span class="a">signals</span><span class="b">assessment</span><span class="c">report</span></span><span class="sp"></span><span class="rng">' + R + '</span></div>'
        + '<div class="rail" tabindex="0" aria-label="topic timeline - arrow keys move through days">' + bars + '</div>'
        + '<div class="axis"><span>' + esc(tl.from || '') + '</span><span>' + esc(tl.to || '') + '</span></div>'
        + '<div class="card"><div class="empty">' + (days.length ? 'pick a day' : 'nothing yet') + '</div></div>';
      const rail = this.shadowRoot.querySelector('.rail');
      const at = (ev) => { const r = rail.getBoundingClientRect(); const x = Math.max(0, Math.min(r.width - 1, ev.clientX - r.left)); return Math.floor(x / r.width * days.length); };
      let drag = false;
      rail.addEventListener('pointerdown', (ev) => { drag = true; rail.setPointerCapture(ev.pointerId); this._pick(at(ev)); });
      rail.addEventListener('pointermove', (ev) => { if (drag) this._mark(at(ev)); });
      rail.addEventListener('pointerup', (ev) => { if (drag) { drag = false; this._pick(at(ev)); } });
      rail.addEventListener('keydown', (ev) => {
        if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); this._pick(Math.max(0, Math.min(days.length - 1, this._sel + (ev.key === 'ArrowLeft' ? -1 : 1)))); }
        if (ev.key === 'End') this._pick(days.length - 1);
        if (ev.key === 'Home') this._pick(0);
      });
      this.shadowRoot.querySelectorAll('.rng button').forEach((b) => b.addEventListener('click', () => this.setAttribute('days', b.dataset.r)));
    }
    _mark(i) {
      this.shadowRoot.querySelectorAll('.d').forEach((el) => el.classList.toggle('sel', +el.dataset.i === i));
    }
    async _pick(i, quiet) {
      const days = (this._tl && this._tl.days) || [];
      if (!days.length || i < 0) return;
      this._sel = i; this._mark(i);
      const d = days[i];
      if (!quiet) this.dispatchEvent(new CustomEvent('intel-asof', { detail: { date: d.date, topic: this.topic }, bubbles: true, composed: true }));
      const seq = ++this._seq;
      if (!this.topic) {
        const evs = (this._tl.events || []).filter((e) => String(e.ts || '').slice(0, 10) === d.date);
        this._card(d.date, null, evs);
        return;
      }
      try {
        const r = await this._get('/intel/asof/get?topic_id=' + encodeURIComponent(this.topic) + '&when=' + d.date);
        if (seq !== this._seq) return;
        this._card(d.date, r.snapshot, (r.signals || []).map((s) => ({ kind: 'signal', title: s.title, url: s.url, score: s.score, sub: s.site })));
      } catch (e) { this._msg('could not read that day: ' + esc(e.message || e)); }
    }
    _card(date, snap, evs) {
      const c = this.shadowRoot.querySelector('.card');
      let h = '<div class="m">' + esc(date) + '</div>';
      if (snap) {
        h += '<h4>' + esc(snap.headline || 'Assessment') + '</h4><div class="m">as assessed ' + esc(String(snap.ts || '').slice(0, 16).replace('T', ' ')) + ' · ' + esc(snap.confidence || '') + ' confidence</div>'
          + (snap.state ? '<div class="st">' + esc(snap.state) + '</div>' : '')
          + ((snap.changed || []).length ? '<div class="m">what had changed</div><ul>' + snap.changed.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>' : '');
      } else if (this.topic) h += '<div class="empty">no assessment yet at this date</div>';
      if (evs.length) {
        h += evs.slice(0, 30).map((e) => {
          const sc = e.score == null ? (e.kind === 'snapshot' ? '◆' : e.kind === 'report' ? '■' : '·') : Math.round(e.score);
          const t = /^(https?:\/\/|\/intel\/)/i.test(String(e.url || '')) ? '<a href="' + esc(e.url) + '" target="_blank" rel="noopener">' + esc(e.title) + '</a>' : esc(e.title);
          return '<div class="ev"><div class="s' + ((e.score || 0) >= 7 ? ' hi' : '') + '">' + sc + '</div><div>' + t + (e.sub ? ' <span class="m">' + esc(e.sub) + '</span>' : '') + '</div></div>';
        }).join('');
      } else h += '<div class="empty">nothing found this day</div>';
      c.innerHTML = h;
    }
  }
  customElements.define('vera-intel-timeline', VeraIntelTimeline);
})();
