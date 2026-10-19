/**
 * <vera-certs refresh="0"> - every certificate in the estate: the findings, then each certificate with where it is served, its
 * issuer, the names it covers, its expiry, days left and state. The certs panel's render() moved here once (Wave 1 of the desk
 * output redesign: panel pieces become shared elements). The panel draws through it; the desk mounts it for a certs.list answer
 * (the widget `element` form), seeded with that answer.
 *
 * The panel keeps its controls (show superseded, Check now) and hands the toggle in. `counts` adds a by-state row (what the
 * desk's composite of certs.list showed), so the element reads at parity or better there.
 *
 * Data:    certs.list (POST /mcp/call)
 * API:     el.seed(answer); el.load(refresh); el.showOld = bool
 * Attributes: refresh (seconds, default 0 = never read by itself; the answer is cached 5 minutes), show-old, counts, api-base
 */
(function () {
  if (customElements.get('vera-certs')) return;

  /* the panel's helpers and render() (vera/security/certs_panel.html), unchanged but for taking the answer as arguments */
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const STATE_KIND = {ok:'ok', 'renew soon':'warn', expiring:'warn', expired:'err', revoked:'err',
                    unreachable:'', superseded:'', recorded:'', 'not issued':'', unknown:''};
const SOURCE = {service:'service', freeipa:'FreeIPA', 'step-ca':'step-ca', netctl:'netctl'};
function when(epoch){ return epoch ? new Date(epoch*1000).toISOString().slice(0,10) : ''; }
function findingsHtml(DATA){
  const fs = DATA.findings||[];
  return fs.length ? fs.map(f => `<div class="finding"><span class="pill ${f.severity==='error'?'err':f.severity==='warn'?'warn':''}">${esc(f.severity)}</span>
      <div>${esc(f.message)} ${f.detail?`<span class="muted">${esc(f.detail)}</span>`:''}</div></div>`).join('')
    : '<div class="muted">No certificate problems found.</div>';
}
function certsHtml(DATA, showOld){
  const rows = (DATA.certs||[]).filter(c => showOld || c.state !== 'superseded');
  return rows.length ? '<table><tr><th>Name</th><th>Where</th><th>Issuer</th><th>Covers</th><th>Expires</th><th>Days left</th><th>State</th></tr>'
    + rows.map(c => `<tr><td><span data-entity="cert:${esc(c.name)}" title="Everything the estate knows about this certificate" style="cursor:pointer;border-bottom:1px dotted var(--dim)">${esc(c.name)}</span></td><td class="muted">${esc(SOURCE[c.source]||c.source)} · ${esc(c.where)}</td>
      <td>${esc(c.issuer_kind||'')}</td><td><code>${esc((c.names||[]).slice(0,4).join(', '))}</code></td>
      <td class="num">${esc(when(c.not_after))}</td><td class="num">${c.days_left==null?'':esc(c.days_left)}</td>
      <td><span class="pill ${STATE_KIND[c.state]||''}">${esc(c.state)}</span></td></tr>`).join('') + '</table>'
    : '<div class="empty">No certificates found.</div>';
}
function metaText(DATA){
  const skipped = (DATA.skipped||[]).join(' · ');
  return `checked ${new Date((DATA.checked_at||0)*1000).toLocaleString()}` + (DATA.cached?' (cached)':'') + (skipped?` · ${skipped}`:'');
}
/* the count per state, in the state pills' own colours (`counts`: the desk's composite showed it) */
function countsHtml(DATA){
  const c = DATA.counts||{}; const ks = Object.keys(c).filter(k => c[k]);
  return ks.length ? '<div class="counts">' + ks.map(k => `<span class="pill ${STATE_KIND[k]||''}">${esc(k)} ${esc(c[k])}</span>`).join(' ') + '</div>' : '';
}

  /* the panel's own look (certs_panel.html), its colours as fallbacks so it reads the same on any page */
  const CSS = `:host{display:block;color:var(--text,var(--fg,#d8dde3));font:12px/1.55 -apple-system,BlinkMacSystemFont,Inter,system-ui,sans-serif}
  *{box-sizing:border-box}
  .card{background:var(--bg1,#14181d);border:1px solid var(--border,#232a33);border-radius:8px;padding:14px 16px;margin-bottom:14px}
  .card h2{margin:0 0 8px;font-size:12px;font-weight:600}
  .tbl{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:11px}
  th{text-align:left;color:var(--dim,var(--dim2,#5f6975));font-weight:500;padding:6px 8px;border-bottom:1px solid var(--border,#232a33);white-space:nowrap}
  td{padding:6px 8px;border-bottom:1px solid var(--border,#232a33);vertical-align:top}
  tr:last-child td{border-bottom:none}
  td.num{font-family:var(--mono,ui-monospace,Menlo,Consolas,monospace);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
  code{font-family:var(--mono,ui-monospace,Menlo,Consolas,monospace);font-size:10px;color:var(--purple,#a78bfa)}
  .pill{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10px;white-space:nowrap;
    border:1px solid var(--border2,#2e3742);background:var(--bg2,#1a1f26);color:var(--fg2,#9aa3ad)}
  .pill.ok{color:var(--ok,#28c28a);border-color:var(--ok,#28c28a)}
  .pill.warn{color:var(--warn,#f5b341);border-color:var(--warn,#f5b341)}
  .pill.err{color:var(--err,#ef5b5b);border-color:var(--err,#ef5b5b)}
  .muted{color:var(--dim,var(--dim2,#5f6975))}
  .empty{color:var(--dim,var(--dim2,#5f6975));text-align:center;padding:16px}
  .finding{display:flex;gap:10px;align-items:baseline;padding:6px 0;border-top:1px dashed var(--border,#232a33)}
  .finding:first-child{border-top:none}
  .counts{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}`;

  const libs = {};
  function ensureLib(src) { return libs[src] || (libs[src] = new Promise((ok) => { const s = document.createElement('script'); s.src = src; s.onload = s.onerror = () => ok(); document.head.appendChild(s); })); }

  class VeraCerts extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: 'open' }); }
    connectedCallback() {
      if (!this._built) {
        this._built = true;
        this.shadowRoot.innerHTML = '<style>' + CSS + '</style><div class="card"><h2>Findings</h2><div class="findings"><div class="empty">checking…</div></div></div>'
          + '<div class="card"><h2>Every certificate</h2><div class="cnt"></div><div class="tbl"><div class="empty">checking…</div></div><div class="muted meta" style="margin-top:8px"></div></div>';
        /* certificate chips: the drawer listens on the document, which cannot see inside this element, so it is opened here */
        this.shadowRoot.addEventListener('click', (e) => {
          const t = e.target && e.target.closest && e.target.closest('[data-entity]'); if (!t) return;
          e.preventDefault(); e.stopPropagation(); const ref = t.getAttribute('data-entity');
          const go = () => { try { window.veraEntityDrawer.open(ref); } catch (er) { /* no drawer on this page */ } };
          if (window.veraEntityDrawer) go(); else ensureLib(this._base + '/ui/vera-entity-drawer.js').then(go);
        });
      }
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._plan(); }); this._io.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._plan());
      if (!this._data) this.load(false); else this._draw();
    }
    disconnectedCallback() { clearTimeout(this._t); try { this._io.disconnect(); } catch (e) { /* gone */ } document.removeEventListener('visibilitychange', this._vis); }
    get _every() { const v = parseInt(this.getAttribute('refresh') == null ? '0' : this.getAttribute('refresh'), 10); return isFinite(v) ? v : 0; }
    get _base() { return (this.getAttribute('api-base') || '').replace(/\/$/, ''); }
    get showOld() { return this.hasAttribute('show-old'); }
    set showOld(v) { if (v) this.setAttribute('show-old', ''); else this.removeAttribute('show-old'); this._draw(); }
    seed(x) { if (!x || typeof x !== 'object') return; this._data = x; this._draw(); }
    async load(refresh) {
      const m = this.shadowRoot && this.shadowRoot.querySelector('.meta'); if (m) m.textContent = 'checking…';
      try {
        const r = await fetch(this._base + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'certs.list', arguments: { refresh: !!refresh } }) });
        const j = await r.json(); this._data = (j && j.content !== undefined) ? j.content : j;
      } catch (e) { if (!this._data) this._data = { error: 'unavailable' }; }
      this._draw(); this._plan();
    }
    _plan() { clearTimeout(this._t); if (!this._every || !this._seen || document.visibilityState === 'hidden') return; this._t = setTimeout(() => this.load(false), this._every * 1000); }
    _draw() {
      const sr = this.shadowRoot, D = this._data; if (!sr || !D || !sr.querySelector('.tbl')) return;
      if (D.error) { sr.querySelector('.tbl').innerHTML = '<div class="empty">' + esc(D.error) + '</div>'; sr.querySelector('.meta').textContent = ''; return; }
      sr.querySelector('.findings').innerHTML = findingsHtml(D);
      sr.querySelector('.cnt').innerHTML = this.hasAttribute('counts') ? countsHtml(D) : '';
      sr.querySelector('.tbl').innerHTML = certsHtml(D, this.showOld);
      sr.querySelector('.meta').textContent = metaText(D);
    }
  }
  customElements.define('vera-certs', VeraCerts);
  window.VeraCerts = { findingsHtml, certsHtml, metaText, countsHtml };
})();
