/**
 * <vera-mimic-status refresh="20"> - the Ollama mimic proxy at a glance: state, routing, the node a request would go to now,
 * nodes online, active / limit, the queue, the recent request rate and the last three requests. The main dashboard's Mimic
 * strip moved here once (Wave 1 of the desk output redesign: panel pieces become shared elements) - with the queue added, so it
 * shows everything the desk's key-value reading of cluster.mimic.status did (parity or better). The dashboard draws through it;
 * the desk mounts it for a cluster.mimic.status answer (the widget `element` form), seeded with that answer.
 *
 * Data:    GET /cluster/mimic/status (cluster.mimic.status) + GET /cluster/mimic/requests?limit=5
 * API:     el.seed(status | {status, requests}); el.load()
 * Attributes: refresh (seconds, default 20; 0 = never read by itself), api-base
 */
(function () {
  if (customElements.get('vera-mimic-status')) return;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

  /* the dashboard's strip (capability_orchestration.html loadMimicSummary), unchanged, plus the queue */
  function stripHtml(status, reqs){
    if(!status) return '<span style="color:var(--dim2)">unavailable</span>';
    const stateColor = !status.mounted ? 'var(--dim2)' : (status.paused ? 'var(--warn)' : 'var(--ok)');
    const stateLbl = !status.mounted ? 'not mounted' : (status.paused ? 'paused' : 'running');
    const online = status.online_nodes||[];
    let html = `
    <div class="cluster-stat"><div class="cluster-stat-v" style="font-size:16px;color:${stateColor}">${stateLbl}</div><div class="cluster-stat-l">State</div></div>
    <div class="cluster-stat"><div class="cluster-stat-v">${status.routing||'—'}</div><div class="cluster-stat-l">Routing</div></div>
    <div class="cluster-stat"><div class="cluster-stat-v">${status.current_target||'—'}</div><div class="cluster-stat-l">Current target</div></div>
    <div class="cluster-stat"><div class="cluster-stat-v">${online.length}</div><div class="cluster-stat-l">Nodes online</div></div>
    <div class="cluster-stat"><div class="cluster-stat-v">${status.active??0}/${status.max_concurrency??'—'}</div><div class="cluster-stat-l">Active/limit</div></div>`;
    /* the queue (added: the desk's reading showed it) */
    if(status.queue_depth!=null||status.queue_max!=null)
      html += `<div class="cluster-stat" title="${status.queue_timeout!=null?'a queued request waits at most '+esc(status.queue_timeout)+'s':''}"><div class="cluster-stat-v" style="color:${(status.queue_depth||0)>0?'var(--warn)':'var(--acc)'}">${status.queue_depth??0}/${status.queue_max??'—'}</div><div class="cluster-stat-l">Queued/max</div></div>`;
    const entries = (reqs&&reqs.entries)||[];
    if(entries.length){
      const newest = entries[0], oldest = entries[entries.length-1];
      const spanMin = Math.max(1, (Date.parse(newest.ts)-Date.parse(oldest.ts))/60000);
      const rate = (entries.length/spanMin).toFixed(1);
      html += `<div class="cluster-stat"><div class="cluster-stat-v">${rate}/min</div><div class="cluster-stat-l">Recent rate (${reqs.total??entries.length} total)</div></div>`;
      html += `<div style="width:100%;font-size:9px;color:var(--dim2);font-family:var(--mono);margin-top:4px">` +
        entries.slice(0,3).map(e=>`${(e.ts||'').slice(11,19)} · ${e.instance||'?'} · ${e.model||'?'}`).join('<br>') +
        `</div>`;
    }
    return html;
  }

  // the dashboard's own stat look (capability_orchestration.html .cluster-stat) and #mimicSummary's row
  const CSS = `:host{display:block}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .cluster-stat{display:flex;flex-direction:column;align-items:center;padding:4px 12px;background:var(--bg0);border:1px solid var(--border);border-radius:4px}
  .cluster-stat-v{font-size:18px;font-weight:700;font-family:var(--mono);color:var(--acc);line-height:1}
  .cluster-stat-l{font-size:9px;color:var(--dim2);margin-top:2px;white-space:nowrap}`;

  class VeraMimicStatus extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: 'open' }); }
    connectedCallback() {
      if (!this._built) { this._built = true; this.shadowRoot.innerHTML = '<style>' + CSS + '</style><div class="row"><span style="color:var(--dim2)">reading…</span></div>'; }
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._plan(); }); this._io.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._plan());
      if (!this._status) this.load(); else { this._draw(); if (!this._reqs && this._every) this._readReqs(); }
    }
    disconnectedCallback() { clearTimeout(this._t); try { this._io.disconnect(); } catch (e) { /* gone */ } document.removeEventListener('visibilitychange', this._vis); }
    get _every() { const v = parseInt(this.getAttribute('refresh') == null ? '20' : this.getAttribute('refresh'), 10); return isFinite(v) ? v : 20; }
    get _base() { return (this.getAttribute('api-base') || '').replace(/\/$/, ''); }
    /* what it was given: the status alone (a cluster.mimic.status answer), or the dashboard's {status, requests} */
    seed(x) { if (!x || typeof x !== 'object') return; if (x.status || x.requests) { this._status = x.status || this._status; if (x.requests) this._reqs = x.requests; } else this._status = x;
      this._draw(); if (!x.requests && !x.status && this.isConnected && this._every) this._readReqs(); }
    async _get(path) { try { const r = await fetch(this._base + path, { cache: 'no-store' }); const j = await r.json(); return j && j.type === 'tool_result' ? j.content : j; } catch (e) { return null; } }
    async _readReqs() { const q = await this._get('/cluster/mimic/requests?limit=5'); if (q) { this._reqs = q; this._draw(); } }
    async load() { const [s, q] = await Promise.all([this._get('/cluster/mimic/status'), this._get('/cluster/mimic/requests?limit=5')]);
      if (s) this._status = s; if (q) this._reqs = q; this._draw(); this._plan(); }
    _plan() { clearTimeout(this._t); if (!this._every || !this._seen || document.visibilityState === 'hidden') return; this._t = setTimeout(() => this.load(), this._every * 1000); }
    _draw() { const row = this.shadowRoot.querySelector('.row'); if (row) row.innerHTML = stripHtml(this._status, this._reqs); }
  }
  customElements.define('vera-mimic-status', VeraMimicStatus);
  window.VeraMimicStatus = { stripHtml };
})();
