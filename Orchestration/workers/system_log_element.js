/**
 * <vera-system-log> — Injectable custom element
 *
 * Full port of the Observe panel's System Log (right side) from
 * capability_orchestration.html.  Self-contained with shadow DOM.
 *
 * Queries the syslog.query capability (/syslog/query) and renders
 * structured log entries with level/category filtering, keyword search,
 * expandable detail + traceback, monitor controls, and the ask-agent
 * feature that sends an error entry to the LLM for diagnosis.
 *
 * Public API:
 *   el.load()              — refresh entries from backend
 *   el.setApiBase(url)     — override backend URL
 *   el.ingestError(ev)     — inject a syslog.error event from WS
 *   el.startAutoRefresh()  — poll every 10s
 *   el.stopAutoRefresh()
 *
 * Events dispatched: vsl:loaded, vsl:error
 */
(function () {
  if (customElements.get('vera-system-log')) return;

  const TMPL = document.createElement('template');
  TMPL.innerHTML = `
<style>
:host{display:block;width:100%;height:100%;overflow:hidden;color:var(--text,#ddd5c8);font-family:var(--sans,'Inter',system-ui,sans-serif);font-size:12px;background:transparent}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
.wrap{display:flex;flex-direction:column;height:100%;min-height:0}
.toolbar{padding:5px 8px;border-bottom:1px solid var(--border,#3a3530);display:flex;align-items:center;gap:5px;flex-wrap:wrap;background:var(--bg1,#1f1d1a);flex-shrink:0}
.title{font-size:10px;font-weight:600;color:var(--text,#ddd5c8)}
select,input{background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);padding:2px 5px;border-radius:3px;font-size:10px;font-family:inherit}
select:focus,input:focus{outline:none;border-color:var(--acc,#5a9e8f)}
select option{background:var(--bg2,#272421)}
.btn{background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);cursor:pointer;padding:2px 7px;border-radius:3px;font-size:9px;font-family:inherit;transition:.12s}
.btn:hover{border-color:var(--acc,#5a9e8f);color:var(--text,#ddd5c8)}
.btn.primary{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}
.btn.teal{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}
.btn.danger{border-color:var(--err,#c96b6b);color:var(--err,#c96b6b)}
.entries{flex:1;overflow-y:auto;padding:5px 8px;display:flex;flex-direction:column;gap:3px}
.ws-box{display:none;flex-direction:column;gap:2px;padding-bottom:6px;border-bottom:1px solid var(--border,#3a3530);margin-bottom:4px}
.entry{padding:7px 10px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);border-radius:4px;cursor:pointer;transition:border-color .15s}
.entry:hover{border-color:var(--acc,#5a9e8f)}
.entry.selected{border-color:var(--acc,#5a9e8f);background:rgba(79,142,247,.06)}
.entry-header{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.level{font-size:9.5px;font-weight:700;font-family:var(--mono,'JetBrains Mono',monospace);text-transform:uppercase;letter-spacing:.5px;flex-shrink:0}
.level.ERROR{color:var(--err,#c96b6b)}.level.WARNING{color:var(--warn,#c9a35a)}.level.INFO{color:var(--acc2,#8fb87a)}.level.DEBUG{color:var(--dim2,#8a7e70)}
.entry-cap{font-size:10px;font-family:var(--mono,'JetBrains Mono',monospace);color:var(--acc2,#8fb87a);font-weight:600}
.entry-ts{font-size:8px;color:var(--dim2,#8a7e70);margin-left:auto;font-family:var(--mono,'JetBrains Mono',monospace)}
.entry-msg{font-size:10px;color:var(--text,#ddd5c8);margin-top:3px;line-height:1.5;word-break:break-word}
.entry-detail{display:none;font-size:9px;color:var(--dim2,#8a7e70);margin-top:4px;white-space:pre-wrap;word-break:break-all;background:var(--bg0,#181614);padding:6px;border-radius:3px;border:1px solid var(--border,#3a3530);max-height:180px;overflow-y:auto;user-select:text;-webkit-user-select:text;cursor:text}
.entry.expanded .entry-detail{display:block}
.entry-traceback{font-size:9px;color:var(--err,#c96b6b);margin-top:3px;white-space:pre-wrap;font-family:var(--mono,'JetBrains Mono',monospace);max-height:140px;overflow-y:auto;background:rgba(201,107,107,.05);padding:4px;border-radius:3px;user-select:text;-webkit-user-select:text;cursor:text}
.footer{border-top:1px solid var(--border,#3a3530);padding:6px 8px;background:var(--bg1,#1f1d1a);display:flex;flex-direction:column;gap:5px;flex-shrink:0}
.footer-row{display:flex;gap:5px;align-items:center;flex-wrap:wrap}
.mon-label{font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.6px}
.meta{font-size:9px;color:var(--dim2,#8a7e70);font-family:var(--mono,'JetBrains Mono',monospace)}
.report{font-size:10px;line-height:1.6;color:var(--fg,#ddd5c8);background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:4px;padding:6px;display:none;white-space:pre-wrap;max-height:120px;overflow-y:auto}
textarea{background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);padding:4px 6px;border-radius:3px;font-size:10px;font-family:inherit;resize:none}
.ask-result{font-size:10px;line-height:1.6;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:4px;padding:6px;display:none;white-space:pre-wrap;max-height:120px;overflow-y:auto}
.entry-copy{font-size:7px;padding:1px 5px;margin-left:auto;flex-shrink:0;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);cursor:pointer;border-radius:3px;font-family:inherit;transition:.12s}
.entry-copy:hover{border-color:var(--acc,#5a9e8f);color:var(--text,#ddd5c8)}
.status-box{font-size:9.5px;color:var(--dim2,#8a7e70)}
</style>
<div class="wrap">
  <div class="toolbar">
    <span class="title">System Log</span>
    <button class="btn primary" id="refreshBtn">↻</button>
    <button class="btn teal" id="autoBtn">Auto</button>
    <span class="meta" id="autoLabel"></span>
    <select id="levelSel">
      <option value="">All levels</option>
      <option value="ERROR">ERROR</option>
      <option value="WARNING">WARNING</option>
      <option value="INFO">INFO</option>
      <option value="DEBUG">DEBUG</option>
    </select>
    <select id="catSel">
      <option value="">All cats</option>
      <option value="cap">cap</option>
      <option value="worker">worker</option>
      <option value="dag">dag</option>
      <option value="system">system</option>
      <option value="agent">agent</option>
    </select>
    <input id="capFilter" placeholder="cap filter…" style="width:80px">
    <input id="keyword" placeholder="keyword…" style="width:80px">
    <input id="limitVal" type="number" value="50" min="5" max="500" style="width:48px">
    <button class="btn danger" id="trimBtn" title="Trim log">✂</button>
  </div>
  <!-- Ask agent — pinned above entries -->
  <div style="padding:5px 8px;border-bottom:1px solid var(--border,#3a3530);background:var(--bg1,#1f1d1a);display:flex;flex-direction:column;gap:4px;flex-shrink:0">
    <div class="footer-row">
      <textarea id="askQuestion" style="flex:1;height:32px" placeholder="Ask agent about selected error…"></textarea>
      <select id="askAgent" style="width:75px"><option value="assistant">assistant</option></select>
      <button class="btn primary" id="askBtn">Ask</button>
    </div>
    <div class="ask-result" id="askResult"></div>
  </div>
  <div class="entries" id="entries">
    <div class="ws-box" id="wsBox"></div>
    <span style="color:var(--dim,#6a6058);font-size:11px" id="loadingMsg">Loading…</span>
  </div>
  <div class="footer">
    <div class="footer-row">
      <span class="mon-label">Monitor</span>
      <button class="btn teal" id="monRunBtn">Run now</button>
      <button class="btn" id="monStartBtn">Start</button>
      <button class="btn danger" id="monStopBtn">Stop</button>
      <input id="monInterval" type="number" value="300" min="60" style="width:50px">
      <span class="meta">s</span>
      <span style="margin-left:auto"><span class="meta" id="entryCount"></span><span class="meta" id="lastTs" style="margin-left:6px"></span></span>
    </div>
    <div class="report" id="monReport"></div>
    <div class="status-box" id="statusBox">—</div>
  </div>
</div>`;

  class VeraSystemLog extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({mode:'open'});
      this.shadowRoot.appendChild(TMPL.content.cloneNode(true));
      this._base = '';
      this._autoTimer = null;
      this._entries = [];
      this._selected = null;
    }

    connectedCallback() {
      const $ = id => this.shadowRoot.getElementById(id);
      $('refreshBtn').addEventListener('click', () => this.load());
      $('autoBtn').addEventListener('click', () => this._toggleAuto());
      $('trimBtn').addEventListener('click', () => this._trim());
      $('levelSel').addEventListener('change', () => this.load());
      $('catSel').addEventListener('change', () => this.load());
      $('capFilter').addEventListener('input', () => this.load());
      $('keyword').addEventListener('input', () => this.load());
      $('limitVal').addEventListener('change', () => this.load());
      $('monRunBtn').addEventListener('click', () => this._monRun());
      $('monStartBtn').addEventListener('click', () => this._monStart());
      $('monStopBtn').addEventListener('click', () => this._monStop());
      $('askBtn').addEventListener('click', () => this._ask());

      // Listen for events from parent
      window.addEventListener('message', e => {
        if (e.data?.type === 'vera:event') {
          const ev = e.data.event || e.data;
          if (ev.type === 'syslog.error') this.ingestError(ev);
          if (ev.type === 'syslog.monitor_report') this._handleMonitorReport(ev);
        }
      });

      setTimeout(() => this.load(), 200);
    }

    setApiBase(url) { this._base = (url || '').replace(/\/$/, ''); }

    _getBase() {
      if (this._base) return this._base;
      const el = document.getElementById('backendUrl');
      return (el ? el.value : '') || window._veraBase || window.location.origin || 'http://llm.int:8999';
    }

    async _api(path, method, body) {
      const opts = {method: method||'GET', headers:{'Content-Type':'application/json'}};
      if (body) opts.body = JSON.stringify(body);
      try { const r = await fetch(this._getBase() + path, opts); return await r.json(); }
      catch (e) { return null; }
    }

    _esc(s) { return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

    async load() {
      const $ = id => this.shadowRoot.getElementById(id);
      const level = $('levelSel').value;
      const category = $('catSel').value;
      const cap_name = $('capFilter').value.trim();
      const keyword = $('keyword').value.trim();
      const limit = parseInt($('limitVal').value) || 50;

      $('statusBox').textContent = 'Loading…';
      const data = await this._api('/syslog/query', 'POST', {limit, level, category, cap_name, keyword});
      if (!data || data.error) {
        $('statusBox').textContent = data?.error || 'Failed to load';
        $('loadingMsg').textContent = 'Error loading syslog';
        return;
      }

      this._entries = data.entries || [];
      $('loadingMsg').style.display = 'none';
      $('entryCount').textContent = this._entries.length + ' entries';
      $('lastTs').textContent = this._entries.length ? (this._entries[0].ts || '').slice(11, 19) : '';
      $('statusBox').textContent = 'Loaded ' + this._entries.length + ' entries';
      this._render();
      this.dispatchEvent(new CustomEvent('vsl:loaded', {detail: {count: this._entries.length}}));
    }

    _render() {
      const container = this.shadowRoot.getElementById('entries');
      const wsBox = this.shadowRoot.getElementById('wsBox');
      const children = Array.from(container.children);
      children.forEach(c => { if (c !== wsBox) c.remove(); });

      // Sort newest first
      const sorted = [...this._entries].sort((a, b) => {
        const ta = a.ts || '', tb = b.ts || '';
        return ta > tb ? -1 : ta < tb ? 1 : 0;
      });

      for (const entry of sorted) {
        const div = document.createElement('div');
        div.className = 'entry';
        const lvl = entry.level || 'INFO';
        const capName = entry.cap_name || entry.name || '';
        const ts = (entry.ts || '').replace('T', ' ').slice(0, 19);
        const msg = entry.message || entry.msg || '';
        const detail = entry.detail || entry.traceback || '';

        div.innerHTML = `<div class="entry-header"><span class="level ${this._esc(lvl)}">${this._esc(lvl)}</span>${capName ? '<span class="entry-cap">'+this._esc(capName)+'</span>' : ''}<span class="entry-ts">${this._esc(ts)}</span><button class="entry-copy">copy</button></div><div class="entry-msg">${this._esc(msg).slice(0, 300)}</div>${detail ? '<div class="entry-detail">'+this._esc(detail).slice(0, 2000)+'</div>' : ''}${entry.traceback ? '<div class="entry-traceback" style="display:none">'+this._esc(entry.traceback).slice(0, 2000)+'</div>' : ''}`;

        div.querySelector('.entry-copy').addEventListener('click', (e) => {
          e.stopPropagation();
          navigator.clipboard.writeText(JSON.stringify(entry, null, 2)).catch(() => {});
        });

        div.addEventListener('click', (e) => {
          // Don't toggle if user is selecting text or clicked inside detail/traceback
          if (window.getSelection && window.getSelection().toString().length > 0) return;
          if (e.target.closest('.entry-detail') || e.target.closest('.entry-traceback')) return;
          if (e.target.classList.contains('entry-copy')) return;
          div.classList.toggle('expanded');
          const tb = div.querySelector('.entry-traceback');
          if (tb) tb.style.display = div.classList.contains('expanded') ? 'block' : 'none';
          container.querySelectorAll('.entry.selected').forEach(e => e.classList.remove('selected'));
          div.classList.add('selected');
          this._selected = entry;
        });

        container.appendChild(div);
      }
    }

    ingestError(ev) {
      const wsBox = this.shadowRoot.getElementById('wsBox');
      if (!wsBox) return;
      wsBox.style.display = 'flex';
      const lvl = ev.level || 'ERROR';
      const msg = ev.message || ev.msg || ev.error || '';
      const logger = ev.logger || ev.cap_name || '';
      const d = ev.ts ? new Date(ev.ts) : new Date();
      const p = (n) => String(n).padStart(2, '0');
      const ts = d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
      const div = document.createElement('div');
      div.style.cssText = 'padding:4px 6px;font-size:9.5px;font-family:var(--mono);border-left:3px solid var(--err,#c96b6b);color:var(--err,#c96b6b);background:rgba(201,107,107,.04);border-radius:2px;line-height:1.4;cursor:pointer;word-break:break-all;display:flex;align-items:flex-start;gap:4px';
      const textSpan = document.createElement('span');
      textSpan.style.cssText = 'flex:1;min-width:0';
      textSpan.textContent = `[${ts}] [${lvl}] ${logger}: ${msg.slice(0, 200)}`;
      div.appendChild(textSpan);
      const copyBtn = document.createElement('button');
      copyBtn.className = 'entry-copy';
      copyBtn.textContent = 'copy';
      copyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(JSON.stringify(ev, null, 2)).catch(() => {});
      });
      div.appendChild(copyBtn);
      div.title = 'Click to copy to ask-agent textarea';
      div.addEventListener('click', (e) => {
        if (e.target.classList.contains('entry-copy')) return;
        const ta = this.shadowRoot.getElementById('askQuestion');
        if (ta) ta.value = `Diagnose this error from ${logger}: ${msg.slice(0, 300)}`;
        this._selected = ev;
      });
      wsBox.insertBefore(div, wsBox.firstChild);
      while (wsBox.children.length > 20) wsBox.removeChild(wsBox.lastChild);
    }

    _toggleAuto() {
      if (this._autoTimer) { this.stopAutoRefresh(); }
      else { this.startAutoRefresh(); }
    }

    startAutoRefresh() {
      if (this._autoTimer) return;
      this._autoTimer = setInterval(() => this.load(), 10000);
      const lbl = this.shadowRoot.getElementById('autoLabel');
      if (lbl) lbl.textContent = 'ON (10s)';
      const btn = this.shadowRoot.getElementById('autoBtn');
      if (btn) btn.textContent = 'Auto ■';
    }

    stopAutoRefresh() {
      if (this._autoTimer) { clearInterval(this._autoTimer); this._autoTimer = null; }
      const lbl = this.shadowRoot.getElementById('autoLabel');
      if (lbl) lbl.textContent = '';
      const btn = this.shadowRoot.getElementById('autoBtn');
      if (btn) btn.textContent = 'Auto';
    }

    async _trim() {
      if (!confirm('Trim system log to last 500 entries?')) return;
      const res = await this._api('/syslog/clear', 'POST', {keep: 500});
      this.shadowRoot.getElementById('statusBox').textContent = res?.status || 'Trimmed';
      this.load();
    }

    async _monRun() {
      this.shadowRoot.getElementById('statusBox').textContent = 'Running monitor check…';
      const res = await this._api('/syslog/monitor/run', 'POST');
      if (res) this._handleMonitorReport(res);
      else this.shadowRoot.getElementById('statusBox').textContent = 'Monitor run failed';
    }

    async _monStart() {
      const interval = parseInt(this.shadowRoot.getElementById('monInterval').value) || 300;
      const res = await this._api('/syslog/monitor/start', 'POST', {interval_s: interval});
      this.shadowRoot.getElementById('statusBox').textContent = res?.status || 'Monitor started';
    }

    async _monStop() {
      const res = await this._api('/syslog/monitor/stop', 'POST');
      this.shadowRoot.getElementById('statusBox').textContent = res?.status || 'Monitor stopped';
    }

    _handleMonitorReport(ev) {
      const report = this.shadowRoot.getElementById('monReport');
      if (!report) return;
      report.style.display = 'block';
      report.textContent = ev.analysis || ev.report || ev.message || JSON.stringify(ev, null, 2).slice(0, 1500);
    }

    async _ask() {
      const $ = id => this.shadowRoot.getElementById(id);
      const question = $('askQuestion').value.trim();
      if (!question) { $('statusBox').textContent = 'Enter a question'; return; }
      const agent = $('askAgent').value || 'assistant';
      $('statusBox').textContent = 'Asking ' + agent + '…';
      const body = {question, agent_name: agent};
      if (this._selected) {
        if (this._selected._redis_id) body.log_id = this._selected._redis_id;
        if (this._selected.cap_name) body.cap_name = this._selected.cap_name;
      }
      const res = await this._api('/syslog/ask', 'POST', body);
      const result = $('askResult');
      if (res && !res.error) {
        result.style.display = 'block';
        result.textContent = res.answer || JSON.stringify(res, null, 2);
        $('statusBox').textContent = '✓ Response received';
      } else {
        result.style.display = 'block';
        result.textContent = res?.error || 'Failed';
        $('statusBox').textContent = '✗ Ask failed';
      }
    }
  }

  customElements.define('vera-system-log', VeraSystemLog);
})();