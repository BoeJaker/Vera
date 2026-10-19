/**
 * <vera-proxmox-status refresh="20"> - a Proxmox cluster at a glance: node cards (CPU, memory, uptime), core temperatures,
 * guest cards (CPU, memory) and storage. The Proxmox panel's render() moved here once (Wave 1 of the desk output redesign:
 * panel pieces become shared elements). The panel draws through it; the desk mounts it for a proxmox.status answer (the
 * widget `element` form), seeded with that answer.
 *
 * Guest power and console buttons are the panel's controls: they are drawn only when the host sets the `actions` attribute,
 * and they call the host page's own guest() / openConsole(), as they always did. A desk reading is display only.
 *
 * Data:    POST /proxmox/status {cluster_id} (proxmox.status) + GET /nodes/temps (obs.node_temps)
 * API:     el.seed(status | {status, temps}); el.load()
 * Attributes: refresh (seconds, default 20; 0 = never read by itself), cluster (cluster id; none: the first), actions, api-base
 */
(function () {
  if (customElements.get('vera-proxmox-status')) return;

  /* the panel's helpers and render() (vera/proxmox/proxmox_panel.html), unchanged but for taking the snapshot as arguments */
  const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const pct = (a,b) => b>0 ? Math.min(100, Math.round(a/b*100)) : 0;
  function fmtBytes(n){ if(!n) return '0'; const u=['B','K','M','G','T']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(n<10&&i>0?1:0)+u[i]; }
  function fmtUptime(s){ if(!s) return '—'; const d=Math.floor(s/86400),h=Math.floor(s%86400/3600); return d?`${d}d ${h}h`:`${h}h ${Math.floor(s%3600/60)}m`; }
  function barClass(p){ return p>=88?'bar hi':p>=65?'bar mid':'bar'; }
  /* without vera-estate.js (it loads on the desk later) names draw as plain text, and guests still open the drawer */
  const NO_ESTATE = { chip: (r, t) => r ? '<span data-entity="' + esc(r) + '" style="cursor:pointer;border-bottom:1px dotted currentColor" title="Everything the estate knows about this">' + esc(t) + '</span>' : esc(t),
    refFor: () => null };

function renderHtml(SNAP, TEMPS, opts){
  if(!SNAP){ return ''; }
  const veraEstate = window.veraEstate || NO_ESTATE;
  const actions = !!(opts && opts.actions);
  const c=SNAP.cluster||{};
  const quor = c.quorate===true?'<span class="pill run">quorate</span>'
             : c.quorate===false?'<span class="pill warn">no quorum</span>':'';
  let h=`<div class="section-h">Nodes <span class="count">${SNAP.nodes.length}</span> ${quor} <span style="flex:0;color:var(--t3)">${esc(c.name||'')}</span></div><div class="grid">`;
  SNAP.nodes.forEach(n=>{
    const cp=pct(n.cpu*100,(n.maxcpu||1)*100)||Math.round((n.cpu||0)*100);
    const mp=pct(n.mem,n.maxmem);
    const up = n.status==='online';
    h+=`<div class="card">
      <div class="hd"><span class="nm">🖥 ${veraEstate.chip(veraEstate.refFor({node:n.node}), n.node)}</span>
        <span class="pill ${up?'run':'stop'}">${esc(n.status||'?')}</span></div>
      ${metric('CPU',Math.round((n.cpu||0)*100)+'%',Math.round((n.cpu||0)*100))}
      ${metric('Mem',fmtBytes(n.mem)+' / '+fmtBytes(n.maxmem),mp)}
      <div class="hd"><span class="sub" style="margin-left:0">up ${fmtUptime(n.uptime)}</span></div>
    </div>`;
  });
  h+='</div>';

  // Core temps (obs.node_temps — CPU/IPMI/drive sensors over SSH). Shown as
  // its own section rather than fused into each node card above: PVE's node
  // name and the SSH host registry's label aren't reliably the same string,
  // so a per-node join would silently miss hosts as often as it'd match them.
  const tempHosts = (TEMPS&&TEMPS.hosts||[]).filter(t=>Object.keys(t.temps||{}).length)
    .sort((a,b)=>(b.max_c||0)-(a.max_c||0));
  if(tempHosts.length){
    h+=`<div class="section-h">Core Temps <span class="count">${tempHosts.length}</span></div><div class="grid">`;
    tempHosts.forEach(t=>{
      const sensors=Object.entries(t.temps).sort((a,b)=>b[1]-a[1]);
      const top=sensors.slice(0,6);
      const cls=t.max_c>=85?'stop':t.max_c>=70?'warn':'run';
      h+=`<div class="card">
        <div class="hd"><span class="nm">🌡 ${esc(t.label)}</span><span class="pill ${cls}">${t.max_c}°C max</span></div>
        ${top.map(([name,val])=>metric(esc(name), val+'°C', Math.min(100,val))).join('')}
        ${sensors.length>top.length?`<div class="sub" style="margin-top:4px;color:var(--t3)">+${sensors.length-top.length} more sensors</div>`:''}
      </div>`;
    });
    h+='</div>';
  }

  const guests=SNAP.guests.filter(g=>!g.template);
  h+=`<div class="section-h">Guests <span class="count">${guests.length}</span> · running ${SNAP.counts&&SNAP.counts.running||0}</div><div class="grid">`;
  guests.forEach(g=>{
    const run=g.status==='running';
    const mp=pct(g.mem,g.maxmem);
    h+=`<div class="card">
      <div class="hd">
        <span class="nm">${g.type==='lxc'?'📦':'🗔'} ${veraEstate.chip('guest:'+g.vmid, g.name||('vm'+g.vmid))}</span>
        <span class="pill ${run?'run':'stop'}">${esc(g.status)}</span>
      </div>
      <div class="hd"><span class="sub" style="margin-left:0;color:var(--t2)">${g.type} · ${g.vmid} · ${esc(g.node)}</span></div>
      ${run?metric('CPU',Math.round((g.cpu||0)*100)+'%',Math.round((g.cpu||0)*100)):''}
      ${run?metric('Mem',fmtBytes(g.mem)+' / '+fmtBytes(g.maxmem),mp):''}
      ${actions?`<div class="guest-actions">
        ${run
          ? `<button class="btn sm" onclick="guest('${g.node}','${g.type}',${g.vmid},'shutdown')">Shutdown</button>
             <button class="btn sm danger" onclick="guest('${g.node}','${g.type}',${g.vmid},'stop')">Stop</button>
             <button class="btn sm gho" onclick="guest('${g.node}','${g.type}',${g.vmid},'reboot')">Reboot</button>`
          : `<button class="btn sm pri" onclick="guest('${g.node}','${g.type}',${g.vmid},'start')">Start</button>`}
      </div>
      <div class="guest-actions">
        <button class="btn sm" ${run?'':'disabled'} onclick="openConsole('${g.node}','${g.type}',${g.vmid},'term','${esc(g.name||g.vmid)}')">⌨ Terminal</button>
        <button class="btn sm" ${run?'':'disabled'} onclick="openConsole('${g.node}','${g.type}',${g.vmid},'vnc','${esc(g.name||g.vmid)}')">🖵 Desktop</button>
      </div>`:''}
    </div>`;
  });
  h+='</div>';

  if(SNAP.storage&&SNAP.storage.length){
    const seen=new Set();
    h+=`<div class="section-h">Storage</div><div class="grid">`;
    SNAP.storage.forEach(s=>{
      const key=s.storage+'@'+s.node; if(seen.has(s.storage))return; seen.add(s.storage);
      const dp=pct(s.disk,s.maxdisk);
      h+=`<div class="card"><div class="hd"><span class="nm">💾 ${esc(s.storage)}</span>
        <span class="pill ${s.status==='available'?'run':'stop'}">${esc(s.status||'')}</span></div>
        ${metric('Used',fmtBytes(s.disk)+' / '+fmtBytes(s.maxdisk),dp)}</div>`;
    });
    h+='</div>';
  }
  return h;
}

function metric(lab,val,p){
  return `<div class="metric"><div class="lab"><span>${lab}</span><span>${esc(val)}</span></div>
    <div class="${barClass(p)}"><i style="width:${Math.max(2,p)}%"></i></div></div>`;
}

  /* the panel's own card look (proxmox_panel.html), its dusk colours as fallbacks so it reads the same on any page; the
     grid steps follow the element's own width, not the window's, so a desk card gets the columns that fit it */
  const CSS = `:host{display:block;container-type:inline-size;color:var(--t1,#d4dae4);font:13px/1.45 var(--font-ui,inherit)}
  *{box-sizing:border-box;margin:0;padding:0}
  button{font:inherit;cursor:pointer;border:none;background:none;color:inherit}
  .section-h{display:flex;align-items:center;gap:8px;margin:6px 2px 8px;color:var(--t2,#6b7585);font-size:11px;letter-spacing:.08em;text-transform:uppercase}
  .section-h .count{color:var(--t3,#3b4252)}
  .section-h::after{content:"";flex:1;height:1px;background:var(--bd,rgba(255,255,255,.07))}
  .grid{display:grid;grid-template-columns:1fr;gap:10px;margin-bottom:18px}
  @container (min-width:560px){.grid{grid-template-columns:repeat(2,1fr)}}
  @container (min-width:920px){.grid{grid-template-columns:repeat(3,1fr)}}
  @container (min-width:1300px){.grid{grid-template-columns:repeat(4,1fr)}}
  .card{background:var(--s2,#1a1d24);border:1px solid var(--bd,rgba(255,255,255,.07));border-radius:9px;padding:12px;display:flex;flex-direction:column;gap:9px}
  .card .hd{display:flex;align-items:center;gap:8px}
  .card .hd .nm{font-weight:700;color:var(--t1,#d4dae4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .card .hd .sub{color:var(--t3,#3b4252);font-size:11px;margin-left:auto;white-space:nowrap}
  .pill{font-size:10px;padding:2px 8px;border-radius:20px;border:1px solid;white-space:nowrap;flex-shrink:0}
  .pill.run{color:var(--ok,#5ec9a0);border-color:var(--ok,#5ec9a0);background:rgba(94,201,160,.08)}
  .pill.stop{color:var(--t2,#6b7585);border-color:var(--t3,#3b4252)}
  .pill.warn{color:var(--warn,#e09a55);border-color:var(--warn,#e09a55)}
  .bar{height:7px;border-radius:4px;background:var(--s3,#222630);overflow:hidden}
  .bar>i{display:block;height:100%;border-radius:4px;background:var(--ac,#6ea8d8);transition:width .4s}
  .bar.hi>i{background:var(--err,#e06060)}
  .bar.mid>i{background:var(--warn,#e09a55)}
  .metric{display:flex;flex-direction:column;gap:4px}
  .metric .lab{display:flex;justify-content:space-between;font-size:10.5px;color:var(--t2,#6b7585)}
  .guest-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
  .btn{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border-radius:6px;background:var(--s3,#222630);border:1px solid var(--bd2,rgba(255,255,255,.14));color:var(--t1,#d4dae4);font-size:12px;white-space:nowrap}
  .btn:hover{border-color:var(--ac,#6ea8d8)}
  .btn.pri{background:var(--ac,#6ea8d8);border-color:var(--ac,#6ea8d8);color:#06121c;font-weight:700}
  .btn.gho{background:transparent}
  .btn.sm{padding:6px 9px;font-size:11px}
  .btn.danger{color:var(--err,#e06060);border-color:var(--err,#e06060)}
  .btn:disabled{opacity:.4;cursor:not-allowed}
  .msg{color:var(--t2,#6b7585);padding:6px 2px}`;

  const libs = {};
  function ensureLib(src) { return libs[src] || (libs[src] = new Promise((ok) => { const s = document.createElement('script'); s.src = src; s.onload = s.onerror = () => ok(); document.head.appendChild(s); })); }

  class VeraProxmoxStatus extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: 'open' }); }
    connectedCallback() {
      if (!this._built) {
        this._built = true;
        this.shadowRoot.innerHTML = '<style>' + CSS + '</style><div class="body"><div class="msg">reading…</div></div>';
        /* estate chips: the drawer listens on the document, which cannot see inside this element, so it is opened here */
        this.shadowRoot.addEventListener('click', (e) => {
          const t = e.target && e.target.closest && e.target.closest('[data-entity]'); if (!t) return;
          e.preventDefault(); e.stopPropagation(); const ref = t.getAttribute('data-entity');
          const go = () => { try { window.veraEntityDrawer.open(ref); } catch (er) { /* no drawer on this page */ } };
          if (window.veraEntityDrawer) go(); else ensureLib(this._base + '/ui/vera-entity-drawer.js').then(go);
        });
      }
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._plan(); }); this._io.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._plan());
      /* node names become estate chips once the estate's machine list is known (the panel loads it itself) */
      if (!window.veraEstate) ensureLib(this._base + '/ui/vera-estate.js').then(() => this._estate());
      else this._estate();
      if (!this._status) this.load(); else { this._draw(); if (this._temps === undefined && this._every) this._readTemps(); }
    }
    disconnectedCallback() { clearTimeout(this._t); try { this._io.disconnect(); } catch (e) { /* gone */ } document.removeEventListener('visibilitychange', this._vis); }
    get _every() { const v = parseInt(this.getAttribute('refresh') == null ? '20' : this.getAttribute('refresh'), 10); return isFinite(v) ? v : 20; }
    get _base() { return (this.getAttribute('api-base') || '').replace(/\/$/, ''); }
    async _estate() { if (!window.veraEstate || this._estated) return; this._estated = true; try { await window.veraEstate.machines(); } catch (e) { /* plain names */ } this._draw(); }
    /* what it was given: the snapshot alone (a proxmox.status answer), or the panel's {status, temps} */
    seed(x) {
      if (!x || typeof x !== 'object') return;
      if (x.status && typeof x.status === 'object') { this._status = x.status; if ('temps' in x) this._temps = x.temps; }
      else this._status = x;
      this._draw();
      if (this._temps === undefined && this.isConnected && this._every) this._readTemps();
    }
    async _get(path, body) {
      try {
        const r = await fetch(this._base + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' } : { cache: 'no-store' });
        const j = await r.json(); return j && j.type === 'tool_result' ? j.content : j;
      } catch (e) { return null; }
    }
    async _readTemps() { const t = await this._get('/nodes/temps'); this._temps = t; this._draw(); }
    async load() {
      const [s, t] = await Promise.all([this._get('/proxmox/status', { cluster_id: this.getAttribute('cluster') || '' }), this._get('/nodes/temps')]);
      if (s) this._status = s; this._temps = t; this._draw(); this._plan();
    }
    _plan() { clearTimeout(this._t); if (!this._every || !this._seen || document.visibilityState === 'hidden') return; this._t = setTimeout(() => this.load(), this._every * 1000); }
    _draw() {
      const body = this.shadowRoot.querySelector('.body'); if (!body) return; const s = this._status;
      if (s && s.error) { body.innerHTML = '<div class="msg">' + esc(s.error) + '</div>'; return; }
      if (!s) { body.innerHTML = '<div class="msg">unavailable</div>'; return; }
      body.innerHTML = renderHtml({ ...s, nodes: s.nodes || [], guests: s.guests || [] }, this._temps, { actions: this.hasAttribute('actions') });
    }
  }
  customElements.define('vera-proxmox-status', VeraProxmoxStatus);
  window.VeraProxmoxStatus = { renderHtml };
})();
