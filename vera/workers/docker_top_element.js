/**
 * <vera-docker-top limit="12" refresh="30"> - the busiest containers across every Docker host, each with its cpu % and
 * memory trend over the last ~10 minutes. The main dashboard's "Busiest containers" tile moved here once (Wave 1 of the
 * desk output redesign: panel pieces become shared elements): the dashboard draws through it, and the desk mounts it for a
 * docker.stats.top answer (the widget `element` form), which seeds it so it shows at once.
 *
 * Data:    GET /workers/docker/stats/top (docker.stats.top - sampled server-side every 30 s, the busiest N per host)
 * API:     el.seed(answer) - draw an answer it was given; el.load() - read it now
 * Attributes: limit (rows, default 12), refresh (seconds, default 30; 0 = never read by itself), api-base
 */
(function () {
  if (customElements.get('vera-docker-top')) return;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

  // the dashboard's sparkline, unchanged. Tiny inline-SVG polyline, deliberately NOT a <vera-sparkline> instance per row: up to
  // 12 containers x 2 series would mean 24 parallel canvas+RAF+ResizeObserver components for what's just a glance-value trend - a
  // plain static SVG string computed once per refresh is far cheaper here.
  function miniSpark(vals, w, h, color){
    const nums = (vals||[]).filter(v=>v!=null);
    if(nums.length<2) return `<svg width="${w}" height="${h}"></svg>`;
    let lo=Math.min(...nums), hi=Math.max(...nums);
    if(lo===hi){ lo-=1; hi+=1; }
    const n = vals.length;
    const pts = vals.map((v,i)=>{
      const x = n<=1 ? w/2 : (i/(n-1))*w;
      const y = v==null ? null : h - ((v-lo)/(hi-lo))*h;
      return [x,y];
    });
    let d = '', pen=false;
    pts.forEach(([x,y])=>{
      if(y==null){ pen=false; return; }
      d += (pen?'L':'M') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
      pen = true;
    });
    return `<svg width="${w}" height="${h}" style="display:block"><path d="${d.trim()}" fill="none" stroke="${color}" stroke-width="1.2"/></svg>`;
  }

  /* the dashboard's rows, unchanged: every host's containers, busiest first. col(name, fallback) reads a theme colour as a
     concrete value (an SVG stroke attribute does not resolve var()). '' when there is nothing sampled yet. */
  function rowsHtml(r, limit, col){
    const hosts = (r && r.hosts) || {};
    const rows = [];
    Object.entries(hosts).forEach(([hostId, h])=>{
      ((h && h.containers)||[]).forEach(c=>rows.push(c));
    });
    if(!rows.length) return '';
    rows.sort((a,b)=>(b.cpu_pct||0)-(a.cpu_pct||0));
    const memSparkColor = col('--acc2', '#8fb87a');
    const cpuColorHex = v => v==null?'#7e8ba0':(v<60?col('--ok', '#5a9e8f'):(v<90?col('--warn', '#c9a45a'):col('--err', '#c75a5a')));
    const cpuColor = v => v==null?'var(--dim2)':(v<60?'var(--ok)':(v<90?'var(--warn)':'var(--err)'));
    return `<div style="font-size:8.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px">Busiest containers — cpu / mem trend, last ~10min</div>` +
      rows.slice(0,limit).map(c=>{
        const hist = c.history||[];
        const cpuVals = hist.map(p=>p.cpu_pct);
        const memVals = hist.map(p=>p.mem_mb);
        return `<div class="sm-row" style="padding:3px 0;gap:8px">
        <span class="nm" title="${esc(c.image||'')}">${esc(c.name||c.id)}</span>
        <span title="cpu% trend">${miniSpark(cpuVals, 36, 14, cpuColorHex(c.cpu_pct))}</span>
        <span title="memory (MB) trend">${miniSpark(memVals, 36, 14, memSparkColor)}</span>
        <span class="meta">${c.mem_mb!=null?Math.round(c.mem_mb)+'MB':''}</span>
        <span style="width:34px;text-align:right;color:${cpuColor(c.cpu_pct)};font-family:var(--mono)">${c.cpu_pct!=null?c.cpu_pct+'%':'—'}</span>
      </div>`;
      }).join('');
  }

  // the dashboard's own row look (capability_orchestration.html .sm-row)
  const CSS = `:host{display:block;font-family:var(--mono,ui-monospace,monospace);color:var(--text,#d8dde3)}
  .sm-row{display:flex;align-items:center;gap:8px;padding:5px 2px;border-bottom:1px solid rgba(58,53,48,.25);font-family:var(--mono);font-size:10px}
  .sm-row:last-child{border-bottom:none}
  .sm-row .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
  .sm-row .meta{color:var(--dim2);font-size:9px;white-space:nowrap}
  .none{color:var(--dim2);font-size:9.5px}`;

  class VeraDockerTop extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: 'open' }); }
    connectedCallback() {
      if (!this._built) { this._built = true; this.shadowRoot.innerHTML = '<style>' + CSS + '</style><div class="body"><span class="none">reading…</span></div>'; }
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._plan(); }); this._io.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._plan());
      if (!this._data) this.load(); else this._draw();
    }
    disconnectedCallback() { clearTimeout(this._t); try { this._io.disconnect(); } catch (e) { /* gone */ } document.removeEventListener('visibilitychange', this._vis); }
    get _limit() { return Math.max(1, parseInt(this.getAttribute('limit') || '12', 10) || 12); }
    get _every() { const v = parseInt(this.getAttribute('refresh') == null ? '30' : this.getAttribute('refresh'), 10); return isFinite(v) ? v : 30; }
    /* an answer it was given (the dashboard's own read, or the desk's: the widget element form) */
    seed(r) { if (r && typeof r === 'object' && r.hosts) { this._data = r; this._draw(); } }
    async load() {
      const base = (this.getAttribute('api-base') || '').replace(/\/$/, '');
      try { const res = await fetch(base + '/workers/docker/stats/top', { cache: 'no-store' }); const r = await res.json(); this.seed(r && r.type === 'tool_result' ? r.content : r); } catch (e) { /* the last drawing stays */ }
      this._plan(); }
    _plan() { clearTimeout(this._t); if (!this._every || !this._seen || document.visibilityState === 'hidden') return; this._t = setTimeout(() => this.load(), this._every * 1000); }
    _draw() { const b = this.shadowRoot.querySelector('.body'); if (!b) return; const cs = getComputedStyle(this);
      const col = (n, fb) => (cs.getPropertyValue(n) || '').trim() || fb;
      b.innerHTML = rowsHtml(this._data, this._limit, col) || '<span class="none">no container stats yet</span>'; }
  }
  customElements.define('vera-docker-top', VeraDockerTop);
  window.VeraDockerTop = { rowsHtml, miniSpark };
})();
