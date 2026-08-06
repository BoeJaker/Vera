/**
 * <vera-error-radar> — compact, truthful live-error badge for any panel.
 *
 * "Truthful animation" contract: the dot ONLY pulses (via window.veraUI
 * .pulseOnce) when a REAL new error event arrives over the live WS bus, or
 * a genuinely new perf.stalls hang/stall entry is found on poll. Idle = zero
 * motion, always. No decorative loop anywhere in this file.
 *
 * Data sources (see documentation/36-agentic-loop-v7-evaluation.md §26-27
 * for how these were found/confirmed this session):
 *   - Live: same /ws endpoint + {action:'subscribe',stream:'vera:events'}
 *     protocol as <vera-live-event-stream> (live_event_stream_element.js).
 *     Filters for cap.error (fires on ANY capability exception, anywhere —
 *     capability_orchestration.py's universal invocation wrapper),
 *     ollama.request_error, and evolve.pipeline.done with an error set.
 *   - Poll (perf.stalls has no live push): GET /perf/stalls every 20s,
 *     diff-gated on `ts` so an unchanged stall list never re-pulses.
 *
 * Public API:
 *   el.setApiBase(url)
 *   el.clear()
 * Events dispatched: radar:error (detail = the raw event/stall record)
 */
(function () {
  if (customElements.get('vera-error-radar')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:inline-flex;align-items:center;position:relative;font-family:var(--sans,system-ui,sans-serif);font-size:11px}
*,*::before,*::after{box-sizing:border-box}
.badge{display:flex;align-items:center;gap:5px;padding:3px 9px;border-radius:12px;cursor:pointer;
  border:1px solid var(--border,var(--bd2,#3a3530));background:var(--bg2,var(--s2,#272421));
  color:var(--dim2,var(--t3,#8a7e70));user-select:none;transition:border-color .12s,color .12s}
.badge:hover{border-color:var(--acc,var(--ac,#5a9e8f))}
.badge.has-err{border-color:var(--err,var(--ac4,#c96b6b));color:var(--err,var(--ac4,#c96b6b))}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok,var(--ac2,#6db87a));flex-shrink:0;--vera-pulse-color:var(--err,#c96b6b)}
.badge.has-err .dot{background:var(--err,var(--ac4,#c96b6b))}
.count{font-variant-numeric:tabular-nums}
.panel{display:none;position:absolute;top:26px;right:0;width:380px;max-height:420px;z-index:50;
  background:var(--bg1,var(--s1,#1f1d1a));border:1px solid var(--border,var(--bd2,#3a3530));
  border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.5);overflow:hidden}
.panel.open{display:block}
.panel-h{padding:6px 9px;border-bottom:1px solid var(--border,var(--bd2,#3a3530));display:flex;
  justify-content:space-between;align-items:center;color:var(--text,var(--t1,#ddd5c8))}
.panel-h button{background:none;border:none;color:var(--dim2,var(--t3,#8a7e70));cursor:pointer;font-size:9px}
.feed{max-height:378px;overflow-y:auto;padding:4px 6px}
.row{padding:5px 7px;border-radius:4px;font-family:var(--mono,'JetBrains Mono',monospace);
  border-left:3px solid var(--err,var(--ac4,#c96b6b));margin-bottom:3px;
  background:rgba(201,107,107,.06);cursor:pointer}
.row.hang{border-left-color:var(--warn,var(--ac3,#d9a441));background:rgba(217,164,65,.06)}
.row .hdr{display:flex;gap:6px;font-weight:600;color:var(--err,var(--ac4,#c96b6b))}
.row.hang .hdr{color:var(--warn,var(--ac3,#d9a441))}
.row .msg{color:var(--text,var(--t1,#ddd5c8));margin-top:2px}
.row .sub{color:var(--dim2,var(--t3,#8a7e70));margin-top:3px;white-space:pre-wrap;word-break:break-all;
  display:none;max-height:200px;overflow-y:auto}
.row.expanded .sub{display:block}
.empty{padding:16px;text-align:center;color:var(--dim2,var(--t3,#8a7e70))}
</style>
<div class="badge" id="badge" title="Live error radar — click for detail"><span class="dot" id="dot"></span><span id="label">no errors</span></div>
<div class="panel" id="panel">
  <div class="panel-h"><span>Errors &amp; stalls — live</span><button id="clearBtn">clear</button></div>
  <div class="feed" id="feed"><div class="empty">Listening for real cap.error / ollama.request_error / pipeline failures / event-loop stalls…</div></div>
</div>`;

  const WS_ERROR_TYPES = new Set(['cap.error', 'ollama.request_error']);
  const STALL_POLL_MS = 20000;

  class VeraErrorRadar extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._items = [];       // newest first, {kind:'error'|'hang'|'stall', ts, ...}
      this._ws = null;
      this._base = '';
      this._lastStallTs = '';
      this._stallTimer = null;
      this._stallsPrimed = false;  // first poll seeds the baseline silently — no pulse for pre-existing history
    }

    connectedCallback() {
      const $ = id => this.shadowRoot.getElementById(id);
      $('badge').addEventListener('click', () => $('panel').classList.toggle('open'));
      $('clearBtn').addEventListener('click', e => { e.stopPropagation(); this.clear(); });
      this._outsideClick = e => {
        if (!this.contains(e.target)) $('panel').classList.remove('open');
      };
      document.addEventListener('click', this._outsideClick);
      this._connect();
      this._pollStalls();
      this._stallTimer = setInterval(() => this._pollStalls(), STALL_POLL_MS);
    }

    disconnectedCallback() {
      document.removeEventListener('click', this._outsideClick);
      if (this._stallTimer) clearInterval(this._stallTimer);
      try { this._ws && this._ws.close(); } catch (_) {}
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }

    _getBase() {
      return this._base || window._veraBase || window.location.origin ||
        (window.__VERA_BASE__ || ('http://' + location.hostname + ':8999'));
    }

    _connect() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws';
        this._ws = new WebSocket(wsUrl);
        this._ws.onopen = () => { try { this._ws.send(JSON.stringify({ action: 'subscribe', stream: 'vera:events' })); } catch (_) {} };
        this._ws.onmessage = e => { try { this._ingestLive(JSON.parse(e.data)); } catch (_) {} };
        this._ws.onclose = () => { setTimeout(() => this._connect(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this._connect(), 5000); }
    }

    async _pollStalls() {
      try {
        const r = await fetch(this._getBase() + '/perf/stalls?limit=20');
        const d = await r.json();
        const events = (d && d.events) || [];
        if (!this._stallsPrimed) {
          // First poll just establishes the baseline (whatever's already in
          // /perf/stalls is pre-existing history, not a new real-time event) —
          // seed _lastStallTs from it without pulsing or listing anything.
          this._stallsPrimed = true;
          const tsList = events.filter(e => e.kind === 'hang' || e.kind === 'stall').map(e => e.ts);
          if (tsList.length) this._lastStallTs = tsList.reduce((a, b) => (a > b ? a : b));
          return;
        }
        // Real-diff gate: only entries strictly newer than the last one we've
        // already shown are genuinely new — never re-pulse on an unchanged list.
        let newest = this._lastStallTs;
        for (const ev of events) {
          if (ev.kind !== 'hang' && ev.kind !== 'stall') continue;
          if (this._lastStallTs && ev.ts <= this._lastStallTs) continue;
          this._items.unshift({
            kind: ev.kind, ts: ev.ts, name: (ev.kind === 'hang' ? 'event-loop hang' : 'event-loop stall'),
            error: (ev.stalled_ms ? ev.stalled_ms + 'ms' : '') + (ev.where ? ' at ' + ev.where : ''),
            traceback: ev.stack || ev.threads || '',
          });
          if (!newest || ev.ts > newest) newest = ev.ts;
          this._pulse = true;
        }
        if (this._items.length > 150) this._items.length = 150;
        if (newest !== this._lastStallTs) {
          this._lastStallTs = newest;
          this._render(true);
        }
      } catch (_) { /* best-effort — a failed poll is not itself an error to surface */ }
    }

    _isWsErrorEvent(ev) {
      const t = (ev && ev.type) || '';
      if (WS_ERROR_TYPES.has(t)) return true;
      if (t === 'evolve.pipeline.done' && ev.error) return true;
      return false;
    }

    _ingestLive(ev) {
      if (!this._isWsErrorEvent(ev)) return;
      this._items.unshift({
        kind: 'error', ts: new Date().toISOString(),
        name: ev.name || ev.cap_name || ev.type,
        error: String(ev.error || '').slice(0, 300),
        traceback: ev.traceback || '',
      });
      if (this._items.length > 150) this._items.length = 150;
      this._render(true);
      this.dispatchEvent(new CustomEvent('radar:error', { detail: ev }));
    }

    _render(isNew) {
      const $ = id => this.shadowRoot.getElementById(id);
      const n = this._items.length;
      $('label').textContent = n ? (n + (n === 1 ? ' item' : ' items')) : 'no errors';
      $('badge').classList.toggle('has-err', n > 0);
      const feed = $('feed');
      if (!n) {
        feed.innerHTML = '<div class="empty">Listening for real cap.error / ollama.request_error / pipeline failures / event-loop stalls…</div>';
      } else {
        feed.innerHTML = this._items.slice(0, 60).map(it => {
          const t = (it.ts || '').slice(11, 19);
          const cls = it.kind === 'error' ? 'row' : 'row hang';
          return '<div class="' + cls + '"><div class="hdr"><span>' + this._esc(t) + '</span><span>' +
            this._esc(it.name) + '</span></div><div class="msg">' + this._esc(it.error) + '</div>' +
            (it.traceback ? '<div class="sub">' + this._esc(it.traceback) + '</div>' : '') + '</div>';
        }).join('');
        feed.querySelectorAll('.row').forEach(r => { r.onclick = () => r.classList.toggle('expanded'); });
      }
      if (isNew && window.veraUI && window.veraUI.pulseOnce) {
        window.veraUI.pulseOnce($('dot'));
      }
    }

    _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

    clear() { this._items = []; this._render(false); }
  }

  customElements.define('vera-error-radar', VeraErrorRadar);
})();
