/**
 * <vera-mesh-nodes refresh="20"> - the ESP32 mesh's nodes: the transports bar (HTTP / WebSocket / MQTT / Serial) and the node
 * list (status dot, name, last seen, modules, channels). The Mesh panel's renderTransports() + renderList() moved here once
 * (Wave 1 of the desk output redesign: panel pieces become shared elements). The panel draws its list through it; the desk
 * mounts it for a mesh.nodes answer (the widget `element` form), seeded with that answer.
 *
 * Choosing a node is the panel's: with the `select` attribute a row calls the host page's own loadNode(), as it always did.
 * A desk reading is display only; `detail` adds each node's board, firmware, address and signal (what the desk's table of
 * mesh.nodes showed), so the element reads at parity or better there.
 *
 * Data:    GET /mesh/nodes (mesh.nodes)
 * API:     el.seed({nodes, transports}); el.load(); el.query = 'filter text'; el.selected = node_id
 * Attributes: refresh (seconds, default 20; 0 = never read by itself), select, detail, list-only, empty, api-base
 */
(function () {
  if (customElements.get('vera-mesh-nodes')) return;

  /* the panel's helpers, renderTransports() and renderList() (vera/mesh/mesh_panel.html), unchanged but for taking their
     inputs as arguments and returning the markup */
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function ago(iso){ if(!iso) return '—'; const s=(Date.now()-Date.parse(iso))/1000;
  if(s<60) return Math.round(s)+'s'; if(s<3600) return Math.round(s/60)+'m'; if(s<86400) return Math.round(s/3600)+'h'; return Math.round(s/86400)+'d'; }

function txHtml(tx){
  const order=[['http','HTTP'],['ws','WebSocket'],['mqtt','MQTT'],['serial','Serial']];
  return order.map(([k,lbl])=>
    `<span class="chip ${tx[k]?'on':'off'}" title="${tx[k]?'available':'not configured'}">${lbl}</span>`).join('');
}

/* the node's board, firmware, address and signal, for `detail` (the desk) */
function detailHtml(n){
  const bits=[n.role, n.board, n.fw_version&&('fw '+n.fw_version), n.ip, n.rssi!=null&&n.rssi!==''&&(n.rssi+' dBm')].filter(Boolean);
  return bits.length?`<div class="muted" style="margin-top:3px">${bits.map(esc).join(' · ')}</div>`:'';
}

function listHtml(_nodes, q, _sel, opts){
  const o=opts||{}, empty=o.empty||'No nodes yet — onboard one above.';
  q=(q||'').toLowerCase();
  const rows=_nodes.filter(n=>!q || (n.name||'').toLowerCase().includes(q) || (n.node_id||'').toLowerCase().includes(q)
     || (n.role||'').toLowerCase().includes(q));
  if(!rows.length){ return `<span class="muted">${esc(empty)}</span>`; }
  return rows.map(n=>{
    const mods=Object.keys(n.modules||{}).slice(0,4).map(m=>`<span class="pill mod">${esc(m)}</span>`).join('');
    return `<div class="nitem ${_sel===n.node_id?'sel':''}"${o.select?` onclick="loadNode('${esc(n.node_id)}')"`:''}>
      <div class="nt"><span class="nm"><i class="dot ${n.status}"></i> ${esc(n.name||n.node_id)}</span>
        <span class="muted">${ago(n.last_seen)}</span></div>
      <div style="margin-top:3px">${mods}<span class="pill">${esc((n.channels||[]).join('/')||'—')}</span></div>${o.detail?detailHtml(n):''}
    </div>`;
  }).join('');
}

  /* the panel's own look (mesh_panel.html: .tx .chip, .pill, .nlist, .nitem, .dot), with fallbacks for pages without its tokens */
  const CSS = `:host{display:block;color:var(--text,#d4dae4);font-family:var(--sans,system-ui,sans-serif);font-size:13px}
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  .tx{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin-bottom:7px}
  .tx:empty{display:none}
  .tx .chip{font-size:9px;padding:2px 7px;border-radius:10px;border:1px solid var(--border,rgba(255,255,255,.1));color:var(--dim2,#6b7585)}
  .tx .chip.on{color:var(--ok,#5ec9a0);border-color:var(--ok,#5ec9a0)}
  .tx .chip.off{color:var(--dim,#4a5262);opacity:.6}
  .muted{color:var(--dim2,#6b7585);font-size:10px}
  .pill{display:inline-block;background:var(--bg0,#0e0f12);border:1px solid var(--border,rgba(255,255,255,.1));border-radius:10px;padding:1px 6px;font-size:9px;color:var(--dim2,#6b7585);margin:1px}
  .pill.mod{border-color:var(--acc4,#a78bfa);color:var(--acc4,#a78bfa)}
  .nlist{display:flex;flex-direction:column;gap:4px}
  .nitem{padding:6px 7px;border:1px solid var(--border,rgba(255,255,255,.1));border-radius:var(--radius-sm,3px);background:var(--bg0,#0e0f12);cursor:pointer}
  .nitem:hover{border-color:var(--acc,#6ea8d8)}
  .nitem.sel{border-color:var(--acc,#6ea8d8);background:var(--bg2,#1a1d24)}
  :host(:not([select])) .nitem{cursor:default}
  :host(:not([select])) .nitem:hover{border-color:var(--border,rgba(255,255,255,.1))}
  .nitem .nt{display:flex;align-items:center;gap:6px;justify-content:space-between}
  .nitem .nm{font-size:11.5px;color:var(--text,#d4dae4);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;display:inline-block}
  .dot.online{background:var(--ok,#5ec9a0)}.dot.stale{background:var(--warn,#e09a55)}.dot.offline{background:var(--err,#e06060)}.dot.new{background:var(--info,#6ea8d8)}`;

  class VeraMeshNodes extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: 'open' }); this._q = ''; this._sel = null; }
    connectedCallback() {
      if (!this._built) { this._built = true; this.shadowRoot.innerHTML = '<style>' + CSS + '</style><div class="tx"></div><div class="nlist"><span class="muted">Loading…</span></div>'; }
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._plan(); }); this._io.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._plan());
      if (!this._ans) this.load(); else this._draw();
    }
    disconnectedCallback() { clearTimeout(this._t); try { this._io.disconnect(); } catch (e) { /* gone */ } document.removeEventListener('visibilitychange', this._vis); }
    get _every() { const v = parseInt(this.getAttribute('refresh') == null ? '20' : this.getAttribute('refresh'), 10); return isFinite(v) ? v : 20; }
    get _base() { return (this.getAttribute('api-base') || '').replace(/\/$/, ''); }
    get query() { return this._q; } set query(v) { this._q = String(v || ''); this._draw(); }
    get selected() { return this._sel; } set selected(v) { this._sel = v || null; this._draw(); }
    /* a mesh.nodes answer {nodes, transports, ...}, or the nodes alone; the panel also passes its filter text and selection */
    seed(x) { if (Array.isArray(x)) x = { nodes: x }; if (!x || typeof x !== 'object') return;
      if ('query' in x) this._q = String(x.query || ''); if ('selected' in x) this._sel = x.selected || null;
      this._ans = this._ans && !x.transports ? Object.assign({}, this._ans, { nodes: x.nodes || [], error: null }) : x; this._draw(); }
    async load() {
      try { const r = await fetch(this._base + '/mesh/nodes', { cache: 'no-store' }); const j = await r.json(); const a = j && j.type === 'tool_result' ? j.content : j;
        if (a && !a.error) this._ans = a; else if (!this._ans) this._ans = { nodes: [], error: (a && a.error) || 'unavailable' }; } catch (e) { if (!this._ans) this._ans = { nodes: [], error: 'unavailable' }; }
      this._draw(); this._plan();
    }
    _plan() { clearTimeout(this._t); if (!this._every || !this._seen || document.visibilityState === 'hidden') return; this._t = setTimeout(() => this.load(), this._every * 1000); }
    _draw() {
      const sr = this.shadowRoot, a = this._ans; if (!sr || !a) return; const tx = sr.querySelector('.tx'), ls = sr.querySelector('.nlist'); if (!ls) return;
      tx.innerHTML = this.hasAttribute('list-only') || !a.transports ? '' : txHtml(a.transports);
      ls.innerHTML = a.error && !(a.nodes || []).length ? '<span class="muted">' + esc(a.error) + '</span>'
        : listHtml(a.nodes || [], this._q, this._sel, { select: this.hasAttribute('select'), detail: this.hasAttribute('detail'), empty: this.getAttribute('empty') || '' });
    }
  }
  customElements.define('vera-mesh-nodes', VeraMeshNodes);
  window.VeraMeshNodes = { listHtml, txHtml };
})();
