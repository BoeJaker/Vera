/**
 * <vera-live-event-stream> — Injectable custom element
 *
 * Full port of the Observe panel's Event Stream (left side) from
 * capability_orchestration.html.  Self-contained with shadow DOM —
 * works in any panel, the dashboard sidebar, or standalone.
 *
 * Public API:
 *   el.ingest(event)       — feed one parsed event object
 *   el.clear()             — clear buffer + UI
 *   el.pause() / resume()  — toggle ingestion
 *   el.setApiBase(url)     — override backend URL
 *   el.connectWs()         — open/reconnect WebSocket
 *   el.subscribe(stream)   — subscribe to a Redis stream
 *   el.unsubscribe(stream)
 *
 * Events dispatched: ves:event, ves:error
 */
(function () {
  if (customElements.get('vera-live-event-stream')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;height:100%;overflow:hidden;color:var(--text,#ddd5c8);font-family:var(--sans,'Inter',system-ui,sans-serif);font-size:12px;background:transparent}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
.wrap{display:flex;flex-direction:column;height:100%;min-height:0}
.toolbar{padding:5px 8px;border-bottom:1px solid var(--border,#3a3530);display:flex;align-items:center;gap:6px;flex-wrap:wrap;background:var(--bg1,#1f1d1a);flex-shrink:0}
.toolbar .title{font-size:10px;font-weight:600;color:var(--text,#ddd5c8)}
input{background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);padding:2px 6px;border-radius:3px;font-size:10px;font-family:inherit}
input:focus{outline:none;border-color:var(--acc,#5a9e8f)}
.btn{background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);cursor:pointer;padding:2px 7px;border-radius:3px;font-size:9px;font-family:inherit;transition:.12s}
.btn:hover{border-color:var(--acc,#5a9e8f);color:var(--text,#ddd5c8)}
.btn.primary{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}
.btn.danger{border-color:var(--err,#c96b6b);color:var(--err,#c96b6b)}
.count{font-size:9px;color:var(--dim2,#8a7e70);margin-left:auto;font-family:var(--mono,'JetBrains Mono',monospace)}
.chips{padding:4px 8px;border-bottom:1px solid var(--border,#3a3530);display:flex;flex-wrap:wrap;gap:3px;background:var(--bg1,#1f1d1a);flex-shrink:0}
.chip{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:10px;font-size:8.5px;cursor:pointer;border:1px solid var(--border,#3a3530);background:var(--bg2,#272421);color:var(--dim2,#8a7e70);transition:.15s;user-select:none}
.chip.on{background:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f);color:#fff}
.chip-right{margin-left:auto;display:flex;gap:3px}
.feed{flex:1;overflow-y:auto;padding:4px 6px;display:flex;flex-direction:column;gap:1px}
.row{padding:3px 7px;border-radius:3px;font-size:10px;font-family:var(--mono,'JetBrains Mono',monospace);cursor:pointer;border-left:3px solid transparent;line-height:1.5;transition:.08s}
.row:hover{background:var(--bg2,#272421)}
.row.cat-err{border-left-color:var(--err,#c96b6b);background:rgba(201,107,107,.04)}
.row.cat-ok{border-left-color:var(--ok,#6db87a)}
.row.cat-cap{border-left-color:var(--acc2,#8fb87a)}
.row.cat-research{border-left-color:#a78bfa}
.row.cat-ide{border-left-color:#34d399}
.row.cat-agent{border-left-color:#f472b6}
.row.cat-memory{border-left-color:#38bdf8}
.row.cat-fabric{border-left-color:#fb923c}
.row.cat-system{border-left-color:var(--dim,#6a6058)}
.row.cat-other{border-left-color:var(--acc4,#9e8fa0)}
.detail{font-size:9px;color:var(--dim2,#8a7e70);margin-top:2px;white-space:pre-wrap;word-break:break-all;display:none}
.row.expanded .detail{display:block}
.sub-bar{padding:4px 8px;border-top:1px solid var(--border,#3a3530);display:flex;gap:5px;align-items:center;flex-wrap:wrap;background:var(--bg1,#1f1d1a);flex-shrink:0}
.sub-label{font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.6px}
.subs{font-size:9px;color:var(--dim2,#8a7e70)}
</style>
<div class="wrap">
  <div class="toolbar">
    <span class="title">Event Stream</span>
    <input id="search" placeholder="Filter…" style="width:110px">
    <button class="btn" id="clearBtn">Clear</button>
    <button class="btn" id="pauseBtn">Pause</button>
    <span class="count" id="count">0 events</span>
  </div>
  <div class="chips" id="chips"></div>
  <div class="feed" id="feed"></div>
  <div class="sub-bar">
    <span class="sub-label">Subscribe</span>
    <input id="subInput" placeholder="stream…" style="width:100px">
    <button class="btn" id="subBtn">Sub</button>
    <button class="btn danger" id="clearSubBtn">Clear subs</button>
    <span class="subs" id="activeSubs"></span>
    <span style="margin-left:auto;display:flex;gap:3px">
      <button class="btn" id="subTokens">tokens</button>
      <button class="btn" id="subAll">all events</button>
    </span>
  </div>
</div>`;

  const CATEGORIES = ['cap','ok','err','research','ide','agent','memory','fabric','system','other'];
  const CAT_LABELS = {cap:'cap',ok:'✓ ok',err:'✗ err',research:'research',ide:'ide',agent:'agent',memory:'memory',fabric:'fabric',system:'system',other:'other'};
  const MAX = 500;

  class VeraLiveEventStream extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({mode:'open'});
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._events = [];
      this._paused = false;
      this._filters = {};
      this._subs = new Set();
      this._ws = null;
      this._base = '';
      CATEGORIES.forEach(c => this._filters[c] = true);
    }

    connectedCallback() {
      const $ = id => this.shadowRoot.getElementById(id);
      // Render chips
      const chipHost = $('chips');
      chipHost.innerHTML = CATEGORIES.map(c =>
        `<span class="chip on" data-cat="${c}">${CAT_LABELS[c]||c}</span>`
      ).join('') + '<span class="chip-right"><button class="btn" data-action="all">All</button><button class="btn" data-action="none">None</button><button class="btn" data-action="err">Err only</button></span>';
      chipHost.addEventListener('click', e => {
        const chip = e.target.closest('.chip[data-cat]');
        if (chip) { chip.classList.toggle('on'); this._filters[chip.dataset.cat] = chip.classList.contains('on'); this._refilter(); return; }
        const action = e.target.dataset.action;
        if (action === 'all' || action === 'none') { const on = action === 'all'; chipHost.querySelectorAll('.chip[data-cat]').forEach(c => { c.classList.toggle('on', on); this._filters[c.dataset.cat] = on; }); this._refilter(); }
        if (action === 'err') { chipHost.querySelectorAll('.chip[data-cat]').forEach(c => { const isErr = c.dataset.cat === 'err'; c.classList.toggle('on', isErr); this._filters[c.dataset.cat] = isErr; }); this._refilter(); }
      });

      $('search').addEventListener('input', () => this._refilter());
      $('clearBtn').addEventListener('click', () => this.clear());
      $('pauseBtn').addEventListener('click', () => { this._paused = !this._paused; $('pauseBtn').textContent = this._paused ? 'Resume' : 'Pause'; });
      $('subBtn').addEventListener('click', () => { const v = $('subInput').value.trim(); if (v) this.subscribe(v); });
      $('clearSubBtn').addEventListener('click', () => { this._subs.forEach(s => this._wsSend({action:'unsubscribe',stream:s})); this._subs.clear(); this._renderSubs(); });
      $('subTokens').addEventListener('click', () => this.subscribe('tokens'));
      $('subAll').addEventListener('click', () => this.subscribe('vera:events'));

      // Listen for parent messages
      window.addEventListener('message', e => {
        if (e.data?.type === 'vera:event') this.ingest(e.data.event || e.data);
      });

      // Auto-connect WS
      setTimeout(() => this.connectWs(), 300);
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }

    _getBase() {
      if (this._base) return this._base;
      const el = document.getElementById('backendUrl');
      return (el ? el.value : '') || window._veraBase || window.location.origin || 'http://llm.int:8999';
    }

    connectWs() {
      try {
        const wsUrl = this._getBase().replace(/^http/, 'ws') + '/ws';
        this._ws = new WebSocket(wsUrl);
        this._ws.onmessage = e => { try { this.ingest(JSON.parse(e.data)); } catch (_) {} };
        this._ws.onclose = () => { setTimeout(() => this.connectWs(), 3000); };
        this._ws.onerror = () => { try { this._ws.close(); } catch (_) {} };
      } catch (_) { setTimeout(() => this.connectWs(), 5000); }
    }

    _wsSend(msg) { try { if (this._ws?.readyState === 1) this._ws.send(JSON.stringify(msg)); } catch (_) {} }

    subscribe(stream) { this._wsSend({action:'subscribe',stream}); this._subs.add(stream); this._renderSubs(); }
    unsubscribe(stream) { this._wsSend({action:'unsubscribe',stream}); this._subs.delete(stream); this._renderSubs(); }

    _renderSubs() {
      const el = this.shadowRoot.getElementById('activeSubs');
      if (el) el.textContent = this._subs.size ? [...this._subs].join(', ') : '';
    }

    _categorise(ev) {
      const t = ev.type || '';
      if (t.startsWith('cap.error') || t.startsWith('syslog.error')) return 'err';
      if (t.startsWith('cap.ok')) return 'ok';
      if (t.startsWith('cap.')) return 'cap';
      if (t.startsWith('research.') || t.startsWith('integration.research')) return 'research';
      if (t.startsWith('ide.') || t.startsWith('integration.ide')) return 'ide';
      if (t.startsWith('agent.')) return 'agent';
      if (t.startsWith('memory.')) return 'memory';
      if (t.startsWith('fabric.') || t.startsWith('data.')) return 'fabric';
      if (t.startsWith('heartbeat') || t.startsWith('backend.') || t.startsWith('server.')) return 'system';
      return 'other';
    }

    _visible(ev) {
      const cat = ev._cat;
      if (cat === 'err' || cat === 'warn') return !!this._filters['err'];
      return !!this._filters[cat] || (cat === 'other' && !!this._filters['other']);
    }

    _esc(s) { return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

    _jsonSafe(ev) {
      const safe = {};
      for (const [k, v] of Object.entries(ev || {})) {
        if (k === 'embedding' || k === '_raw') continue;
        if (typeof v === 'string' && v.length > 300) safe[k] = v.slice(0, 300) + '…';
        else if (typeof v === 'object' && v !== null) { const s = JSON.stringify(v); safe[k] = s.length > 300 ? s.slice(0, 300) + '…' : v; }
        else safe[k] = v;
      }
      const out = JSON.stringify(safe, null, 2);
      return out.length > 2000 ? out.slice(0, 2000) + '\n...(truncated)' : out;
    }

    _summary(ev) {
      const t = ev.type || 'event';
      let s = t;
      const cn = ev.name || ev.cap_name || '';
      if (cn) s += ' · ' + cn;
      if (ev.session_id) s += ' · 🔑' + ev.session_id.slice(-8);
      if (ev.elapsed_ms) s += ' · ' + ev.elapsed_ms + 'ms';
      if (ev.elapsed) s += ' · ' + parseFloat(ev.elapsed).toFixed(1) + 's';
      if (ev.preview) s += ' → ' + String(ev.preview).slice(0, 80);
      if (ev.query) s += ' · ' + String(ev.query).slice(0, 50);
      if (ev.path) s += ' · ' + ev.path;
      if (ev.error) s += ' ✗ ' + String(ev.error).slice(0, 80);
      return s;
    }

    ingest(ev) {
      if (this._paused) return;
      ev._cat = this._categorise(ev);
      ev._ts = new Date().toISOString().slice(11, 19);
      this._events.push(ev);
      if (this._events.length > MAX) this._events.shift();
      this.dispatchEvent(new CustomEvent('ves:event', {detail: ev}));
      if (!this._visible(ev)) { this._updateCount(); return; }
      const q = (this.shadowRoot.getElementById('search')?.value || '').toLowerCase();
      if (q && !JSON.stringify(ev).toLowerCase().includes(q)) { this._updateCount(); return; }
      this._renderRow(ev, true);
      this._updateCount();
    }

    _renderRow(ev, prepend) {
      const feed = this.shadowRoot.getElementById('feed');
      if (!feed) return;
      const t = ev.type || 'event';
      const isErr = ev._cat === 'err' || (ev.error && ev._cat !== 'ok');
      const isDone = ev._cat === 'ok';
      const summary = this._summary(ev);
      const div = document.createElement('div');
      div.className = 'row cat-' + ev._cat;
      div.innerHTML = `<div style="display:flex;gap:6px;align-items:baseline"><span style="color:var(--dim2,#8a7e70);flex-shrink:0">${ev._ts}</span><span style="color:${isErr?'var(--err,#c96b6b)':isDone?'var(--ok,#6db87a)':'var(--acc2,#8fb87a)'};flex-shrink:0;font-weight:600">${this._esc(t)}</span><span style="color:var(--text,#ddd5c8);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${this._esc(summary.replace(t,'').trim())}</span></div><div class="detail">${this._esc(this._jsonSafe(ev))}</div>`;
      div.onclick = () => div.classList.toggle('expanded');
      if (prepend) feed.insertBefore(div, feed.firstChild);
      else feed.appendChild(div);
      while (feed.children.length > MAX) feed.removeChild(feed.lastChild);
    }

    _updateCount() {
      const el = this.shadowRoot.getElementById('count');
      if (el) el.textContent = this._events.length + ' events';
    }

    _refilter() {
      const feed = this.shadowRoot.getElementById('feed');
      if (!feed) return;
      feed.innerHTML = '';
      const q = (this.shadowRoot.getElementById('search')?.value || '').toLowerCase();
      let shown = 0;
      for (let i = this._events.length - 1; i >= 0 && shown < MAX; i--) {
        const ev = this._events[i];
        if (!this._visible(ev)) continue;
        if (q && !JSON.stringify(ev).toLowerCase().includes(q)) continue;
        this._renderRow(ev, false);
        shown++;
      }
      const el = this.shadowRoot.getElementById('count');
      if (el) el.textContent = shown + ' / ' + this._events.length + ' events';
    }

    clear() { this._events = []; const f = this.shadowRoot.getElementById('feed'); if (f) f.innerHTML = ''; this._updateCount(); }
    pause() { this._paused = true; const b = this.shadowRoot.getElementById('pauseBtn'); if (b) b.textContent = 'Resume'; }
    resume() { this._paused = false; const b = this.shadowRoot.getElementById('pauseBtn'); if (b) b.textContent = 'Pause'; }
  }

  customElements.define('vera-live-event-stream', VeraLiveEventStream);
})();