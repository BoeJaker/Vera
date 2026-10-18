/* ONE MARKETS CHART (WSU-75, owner 2026-10-16: "Full parity, shared code").
   The Markets panel's chart pieces - the library loader, the chart options, the series adapters for both library
   versions, the time-synced panes, the indicator drawing and the signal markers - live here once. The panel uses them
   for its chart; <vera-mkt-chart> is the same chart for the desk: candles and volume, the indicators as toggles (EMA,
   SMA, Bollinger, VWAP on the price; RSI, MACD in their own time-synced panes), a crosshair read-out of the bar under
   the pointer (O H L C V), wheel zoom and drag pan with fit, the timeframes the asset's provider has (each re-reads its
   bars), and its last candle kept live (every 15 s) while it is on screen. Data comes the way the panel's does:
   /markets/bars and /markets/indicators (computed on the server). Served at /markets/chart.js. */
(function (root) {
  'use strict';
  const M = root.VeraMktChart = root.VeraMktChart || {};

  /* ── the panel's helpers, shared ─────────────────────────────────────────────────────────────────────────── */
  let loading = null;
  M.ensureLw = function () {
    if (root.LightweightCharts) return Promise.resolve(true);
    if (loading) return loading;
    loading = new Promise((res) => {
      const urls = ['https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js',
                    'https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js'];
      (function go(i) { if (i >= urls.length) { res(false); return; }
        const s = document.createElement('script'); s.src = urls[i];
        s.onload = () => res(!!root.LightweightCharts); s.onerror = () => go(i + 1);
        document.head.appendChild(s); })(0);
    });
    return loading;
  };
  M.TFS = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w'];
  M.YAHOO_TFS = ['1m', '5m', '15m', '30m', '1h', '1d', '1w'];
  M.OVERLAY_COLORS = ['#7aa2f7', '#e8b34d', '#b48ead', '#5ec9b8', '#d1848b', '#8fb87a'];
  M.CHART_OPTS = {
    /* attributionLogo:false - the TV logo repeated on every indicator sub-pane reads as a watermark; a single credit
       lives in the status bar instead. */
    layout: { background: { color: 'transparent' }, textColor: '#8a7e70', fontSize: 10, attributionLogo: false },
    grid: { vertLines: { color: 'rgba(56,51,47,.25)' }, horzLines: { color: 'rgba(56,51,47,.25)' } },
    rightPriceScale: { borderColor: 'rgba(56,51,47,.6)' },
    timeScale: { borderColor: 'rgba(56,51,47,.6)', timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
  };
  M.lwCandle = (c, o) => (c.addCandlestickSeries ? c.addCandlestickSeries(o) : c.addSeries(root.LightweightCharts.CandlestickSeries, o));
  M.lwHist = (c, o) => (c.addHistogramSeries ? c.addHistogramSeries(o) : c.addSeries(root.LightweightCharts.HistogramSeries, o));
  M.lwLine = (c, o) => (c.addLineSeries ? c.addLineSeries(o) : c.addSeries(root.LightweightCharts.LineSeries, o));
  /* whitespace items for nulls keep logical indices aligned across panes */
  M.seriesData = (t, vals) => t.map((tt, i) => (vals[i] == null ? { time: tt } : { time: tt, value: vals[i] }));
  M.hexA = function (hex, a) { const m = /^#?([0-9a-f]{6})$/i.exec(hex || ''); if (!m) return hex; const n = parseInt(m[1], 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')'; };
  M.ribbonColor = function (i, n) { /* sequential teal→blue ramp by period order */ const t = n <= 1 ? 0 : i / (n - 1); return 'hsl(' + (170 - 40 * t) + ' 45% ' + (62 - 26 * t) + '%)'; };
  M.paramStr = (p) => Object.entries(p || {}).filter(([k]) => k !== 'periods').map(([k, v]) => k + '=' + v).join(' ').slice(0, 40);
  M.slug = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'asset';
  M.dsId = (prov, sym, tf) => 'mkt.' + prov + '.' + M.slug(sym) + '.' + tf;
  M.candleOpts = { upColor: '#3fd0a4', downColor: '#e0705a', borderUpColor: '#3fd0a4', borderDownColor: '#e0705a', wickUpColor: '#3fd0a4', wickDownColor: '#e0705a' };
  M.volumeData = (B) => B.t.map((t, i) => ({ time: t, value: B.v[i] || 0, color: B.c[i] >= B.o[i] ? 'rgba(63,208,164,.4)' : 'rgba(224,112,90,.4)' }));
  /* the panes keep one visible range: any of them scrolled or zoomed, the others follow */
  M.syncGroup = function (charts, after) { let busy = false;
    const from = (src) => { if (busy) return; busy = true;
      try { const range = src.getVisibleLogicalRange(); if (range) charts().forEach((c) => { const ts = c.timeScale(); if (ts !== src) { try { ts.setVisibleLogicalRange(range); } catch (e) { /* not drawn yet */ } } }); }
      finally { busy = false; } if (after) after(); };
    return (chart) => chart.timeScale().subscribeVisibleLogicalRangeChange(() => from(chart.timeScale())); };
  /* the signal markers on the price series (library v4 and v5) */
  M.setMarkers = function (series, entries, exits, holder, extra) {
    if (!series) return holder || null;
    const mk = [...(entries || []).map((t) => ({ time: t, position: 'belowBar', color: '#3fd0a4', shape: 'arrowUp' })),
                ...(exits || []).map((t) => ({ time: t, position: 'aboveBar', color: '#e0705a', shape: 'arrowDown' })),
                ...(extra || [])].sort((a, b) => a.time - b.time);
    try { if (typeof series.setMarkers === 'function') { series.setMarkers(mk); return holder || null; }
      if (root.LightweightCharts && root.LightweightCharts.createSeriesMarkers) { if (holder && holder.setMarkers) { holder.setMarkers(mk); return holder; } return root.LightweightCharts.createSeriesMarkers(series, mk); } }
    catch (e) { /* markers are optional */ }
    return holder || null;
  };
  /* EVERY MARKER THE MARKETS SIDE HAS FOR AN ASSET (owner 2026-10-18: "markets chart should be able to get all the markers from
     market"): dated flags (annotations - events, halvings, pinned news), the person's trades, the strategies' alerts and the live
     monitors' last entries and exits, each on the bar it falls in; price levels (hline annotations) as price lines. Read-only
     capabilities, through the same /mcp/call the desk uses. call(name, args) -> the answer's content. */
  M.pyList = (v) => { if (Array.isArray(v)) return v; if (v == null || v === '') return [];
    try { return JSON.parse(String(v).replace(/'/g, '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/\bNone\b/g, 'null')); } catch (e) { return []; } };
  M.pyObj = (v) => { if (v && typeof v === 'object') return v; const l = M.pyList(v); return l && !Array.isArray(l) && typeof l === 'object' ? l : {}; };
  M.toUnix = (v) => { if (v == null || v === '') return null; const n = Number(v); if (isFinite(n) && n > 0) return n > 1e12 ? Math.floor(n / 1000) : Math.floor(n);
    const d = Date.parse(String(v)); return isFinite(d) ? Math.floor(d / 1000) : null; };
  /* the bar a moment falls in: the last bar at or before it (a moment past the last bar but within one bar of it goes on the last) */
  M.snap = (times, t) => { if (!times || !times.length || t == null) return null; const n = times.length;
    if (t < times[0]) return null; const step = n > 1 ? times[n - 1] - times[n - 2] : 86400; if (t > times[n - 1] + step) return null;
    let lo = 0, hi = n - 1; while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (times[mid] <= t) lo = mid; else hi = mid - 1; } return times[lo]; };
  M.shortText = (t, n) => { t = String(t || '').replace(/\s+/g, ' ').trim(); n = n || 22; return t.length > n ? t.slice(0, n - 1) + '…' : t; };
  M.marketMarkers = async function (call, prov, sym, times) {
    const key = prov + ':' + sym, dsp = 'mkt.' + prov + '.' + M.slug(sym) + '.';
    const [ann, tx, al, mon] = await Promise.all([
      call('markets.annotate.list', { symbol_key: key }), call('markets.portfolio.tx_list', { symbol_key: key, limit: 500 }),
      call('markets.alerts.list', { limit: 500 }), call('markets.monitor.status', {})]);
    const markers = [], lines = []; const at = (t) => M.snap(times, M.toUnix(t));
    const put = (t, m) => { const time = at(t); if (time != null) markers.push(Object.assign({ time }, m)); };
    ((ann && ann.annotations) || []).forEach((a) => { const pts = M.pyList(a.points); const c = a.color || '#8a93a6';
      if (a.kind === 'hline') { const p = pts[0] && Number(pts[0].p); if (isFinite(p)) lines.push({ price: p, color: c, title: M.shortText(a.text, 18) }); return; }
      if (a.kind === 'vline' || pts.length === 1) { const t = pts[0] && (pts[0].t != null ? pts[0].t : pts[0].time);
        put(t, { position: 'aboveBar', color: c, shape: 'circle', text: M.shortText(a.text) }); } });
    ((tx && tx.transactions) || []).forEach((x) => { if (x.symbol_key && x.symbol_key !== key) return; const buy = String(x.side || '').toLowerCase() !== 'sell';
      put(x.ts || x.created_at, { position: buy ? 'belowBar' : 'aboveBar', color: buy ? '#3fd0a4' : '#e0705a', shape: buy ? 'arrowUp' : 'arrowDown',
        text: (buy ? 'Buy ' : 'Sell ') + (x.qty || '') + (x.price ? ' @ ' + Number(x.price).toLocaleString() : '') }); });
    ((al && al.alerts) || []).forEach((x) => { if (!String(x.dataset_id || '').startsWith(dsp)) return; const ent = String(x.direction || '') === 'entry';
      put(x.created_at, { position: ent ? 'belowBar' : 'aboveBar', color: ent ? '#7aa2f7' : '#c58af9', shape: ent ? 'arrowUp' : 'arrowDown', text: M.shortText(x.name, 16) }); });
    ((mon && mon.monitors) || []).forEach((m) => { if (!String(m.dataset_id || '').startsWith(dsp)) return; const st = M.pyObj(m.state);
      [['last_entry_ts', true], ['last_exit_ts', false], ['last_short_ts', false], ['last_short_exit_ts', true]].forEach(([k, up]) => {
        if (st[k] != null && st[k] !== '') put(st[k], { position: up ? 'belowBar' : 'aboveBar', color: '#e8b34d', shape: up ? 'arrowUp' : 'arrowDown', text: M.shortText(m.name, 16) }); }); });
    return { markers, lines };
  };
  /* THE MARKETS PAGES' OWN ROWS, ONCE (the redesign plan section 4: from the chart to the asset's news, positions, alerts and
     backtests - drawn by the pages' own row code, moved here unchanged). f = the page's formatters {esc, fmtPx, fmtPct, fmtN}:
     each page passes its own, so its rows read exactly as before; the desk chart passes its own. */
  M.DIR_CHIP = { entry: ['▲ entry', 'bull'], exit: ['▽ exit', 'bear'], short_entry: ['▼ short', 'bear'], short_exit: ['△ cover', 'bull'] };
  M.rows = {
    /* the Markets page's stored backtests (a row's data-btid opens it) */
    backtests: (list, f) => { const esc = f.esc, fmtPct = f.fmtPct; return (list || []).map(b=>{
    const s=b.stats||{};
    return `<div style="display:flex;gap:6px;align-items:center;padding:3px 0;font-size:10.5px;cursor:pointer"
      data-btid="${b.id}">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        <b>${esc(b.name)}</b> <span class="muted">${esc(b.dataset_id)}</span></span>
      <span class="${(s.total_return_pct||0)>=0?'pos':'neg'}" style="font-family:var(--mono)">${fmtPct(s.total_return_pct)}</span>
    </div>`;
  }).join(''); },
    /* the Markets page's backtest trades (the last 40, newest first) */
    trades: (trades, f) => { const esc = f.esc, fmtPct = f.fmtPct; return (trades||[]).slice(-40).reverse().map(t=>{
    const d1=new Date(t.entry_t*1000).toISOString().slice(0,10);
    const d2=t.exit_t?new Date(t.exit_t*1000).toISOString().slice(0,10):'—';
    return `<tr><td>${d1}</td><td>${d2}</td>
      <td class="${t.ret_pct>0?'pos':'neg'}">${fmtPct(t.ret_pct)}</td><td class="tx">${esc(t.reason||'')}</td></tr>`;
  }).join('')||'<tr><td colspan="4" class="tx muted">no trades</td></tr>'; },
    /* the Quant Studio's alert feed */
    alerts: (alerts, f) => { const esc = f.esc, fmtPx = f.fmtPx; return (alerts || []).map(a=>{
    const [lbl,cls]=M.DIR_CHIP[a.direction]||[esc(a.direction||''),''];
    return `<div class="condRow" style="opacity:${a.seen?'.55':'1'}">
      <span class="muted num" style="font-size:10px;width:86px">${esc(String(a.created_at||'').slice(5,16).replace('T',' '))}</span>
      <span class="chip ${cls}" style="font-size:9.5px">${lbl}</span>
      <b style="font-size:11.5px">${esc(a.name||'')}</b>
      <span class="muted" style="font-size:10.5px">${esc(String(a.dataset_id||'').replace('mkt.',''))}</span>
      <span class="num" style="font-size:11px">@ ${fmtPx(a.price)}</span>
      <span style="flex:1"></span></div>`;
  }).join(''); },
    /* the Quant Studio's holdings table (its Portfolio page): head, rows (pulse cells filled after by M.fillHoldings), the empty
       book or the flat sim account */
    holdings: (holdings, f, isReal) => { const esc = f.esc, fmtN = f.fmtN, fmtPct = f.fmtPct;
  const cols=['asset','trend','last','1D','1W','1M','RSI','30d vol','52w','sent','qty','value','uP&L','uP&L %','alloc'];
  const head='<tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
  const rows=(holdings || []).map((p,i)=>{
    const dim=(p.qty||0)>0?'':'opacity:.45';
    return '<tr data-hk="'+esc(p.symbol_key)+'" style="cursor:pointer;'+dim+'" title="open info card">'+
      '<td><b>'+esc(p.symbol_key)+'</b></td>'+
      '<td><canvas class="hspark" width="70" height="22" style="width:70px;height:22px"></canvas></td>'+
      '<td class="hLast num">—</td><td class="hc1d num">—</td><td class="hc1w num">—</td><td class="hc1m num">—</td>'+
      '<td class="hRsi num">—</td><td class="hVol num">—</td>'+
      '<td class="h52" style="min-width:64px">—</td><td class="hSent">—</td>'+
      '<td class="num">'+(p.qty!=null?(+p.qty).toFixed(4):'—')+'</td>'+
      '<td class="num">'+(p.value!=null?'$'+fmtN(p.value):'—')+'</td>'+
      '<td class="num '+((p.unrealized||0)>=0?'up':'dn')+'">'+(p.unrealized!=null?fmtN(p.unrealized):'—')+'</td>'+
      '<td class="num '+((p.unrealized_pct||0)>=0?'up':'dn')+'">'+fmtPct(p.unrealized_pct)+'</td>'+
      '<td class="num">'+(p.alloc!=null?p.alloc+'%':'—')+'</td></tr>';
  }).join('');
  return head+(rows||'<tr><td colspan="'+cols.length+'" class="muted">'+
    (isReal?'empty book — record your holdings in the ledger below':'flat — place a paper order below')+'</td></tr>'); },
    /* the Quant Studio's headlines */
    news: (headlines, f) => { const esc = f.esc; return (headlines || []).map(h=>
      `<div class="macroRow"><a href="${esc(h.url)}" target="_blank" rel="noopener"
        style="color:var(--ink);text-decoration:none">${esc(h.title)}</a></div>`).join(''); },
  };
  /* the Quant Studio's pulse stats from daily closes (every asset class, customs too) */
  M.computeStats = function(c){
  if(!c||c.length<2) return null;
  const n=c.length, last=c[n-1];
  const at=k=>c[Math.max(0,n-1-k)];
  const pct=(a,b)=>(b?((a-b)/b*100):null);
  let rsi=null;
  if(n>15){ let g=0,l=0; for(let i=n-14;i<n;i++){ const d=c[i]-c[i-1]; if(d>=0)g+=d; else l-=d; }
    const rs=l===0?100:g/l; rsi=l===0?100:100-100/(1+rs); }
  let vol=null;
  const rets=[]; for(let i=Math.max(1,n-31);i<n;i++){ if(c[i-1]) rets.push(c[i]/c[i-1]-1); }
  if(rets.length>=8){ const m=rets.reduce((s,x)=>s+x,0)/rets.length;
    const v=rets.reduce((s,x)=>s+(x-m)*(x-m),0)/rets.length; vol=Math.sqrt(v)*Math.sqrt(252)*100; }
  const w52=c.slice(-252), lo=Math.min.apply(null,w52), hi=Math.max.apply(null,w52);
  return {last:last, chg1d:pct(last,at(1)), chg1w:pct(last,at(5)), chg1m:pct(last,at(21)),
    chg3m:pct(last,at(63)), chg1y:pct(last,at(252)),
    rsi:(rsi==null?null:Math.round(rsi)), vol:(vol==null?null:Math.round(vol)),
    lo52:lo, hi52:hi, range52:(hi>lo?Math.round((last-lo)/(hi-lo)*100):50)};
  };
  /* the Quant Studio's sparkline; c = the page's colours {up, dn, hexA} */
  M.drawSpark = function(cv,vals,up,c){
  const dpr=root.devicePixelRatio||1;
  const W=cv.clientWidth||56,H=cv.clientHeight||20;
  cv.width=W*dpr;cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const lo=Math.min(...vals),hi=Math.max(...vals);
  const col=up?c.up:c.dn;
  ctx.beginPath();
  vals.forEach((v,i)=>{
    const x=i/(vals.length-1)*W, y=H-1-(hi>lo?(v-lo)/(hi-lo):0.5)*(H-2);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
  ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.stroke();
  ctx.lineTo(W,H); ctx.lineTo(0,H); ctx.closePath();
  const g=ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,c.hexA(col,.3)); g.addColorStop(1,c.hexA(col,0));
  ctx.fillStyle=g; ctx.fill();
  };
  /* the holdings table's pulse cells, filled row by row. d = {closes(key) -> {c} | null, sent() -> {key: {score}}, fmtPx, fmtPct,
     spark(canvas, values, up)} */
  M.fillHoldings = function(host, d){
  const computeStats=M.computeStats, fmtPx=d.fmtPx, fmtPct=d.fmtPct;
  host.querySelectorAll('tr[data-hk]').forEach(async tr=>{
    const key=tr.getAttribute('data-hk');
    const bars=await d.closes(key);
    const st=bars?computeStats(bars.c):null;
    if(st){
      const cv=tr.querySelector('.hspark');
      if(cv&&bars.c.length>1) d.spark(cv,bars.c.slice(-60),(st.chg1m||0)>=0);
      const q=cls=>tr.querySelector('.'+cls);
      q('hLast').textContent=fmtPx(st.last);
      const setC=(cls,v)=>{ const el=q(cls); if(el){ el.textContent=fmtPct(v); el.classList.add((v||0)>=0?'up':'dn'); } };
      setC('hc1d',st.chg1d); setC('hc1w',st.chg1w); setC('hc1m',st.chg1m);
      q('hRsi').textContent=(st.rsi==null?'—':st.rsi);
      q('hVol').textContent=(st.vol==null?'—':st.vol+'%');
      q('h52').innerHTML='<span class="pbar" style="width:56px;display:inline-block;vertical-align:middle">'+
        '<i style="width:'+st.range52+'%;background:linear-gradient(90deg,var(--dn),var(--warn),var(--up))"></i></span>';
    }
    let sdat=null; try{ sdat=(await d.sent())[key]; }catch(_){}
    const sc=tr.querySelector('.hSent');
    if(sc){ if(sdat&&sdat.score!=null){ const cls=sdat.score>0.15?'bull':sdat.score<-0.15?'bear':'flat';
        sc.innerHTML='<span class="chip '+cls+'" style="font-size:9px">'+(sdat.score>0?'+':'')+Number(sdat.score).toFixed(2)+'</span>'; }
      else sc.innerHTML='<span class="muted">—</span>'; }
  });
  };
  /* THE MARKET MAP (the Quant Studio's Pulse treemap, moved once): /markets/overview's groups as columns, each asset a box sized
     by its move over the window and coloured by it, animated from where the boxes were. cv / box = the canvas and the box that
     sizes it; pulse = the overview answer; Wk = the window (chg_1d, chg_1w, ...); st = the page's state {rects, anim};
     o = {col: {up, dn, ink, muted}, hexA, fmtPct, anim (bool), onPick(key)}. */
  M.drawMarketMap = function(cv, box, pulse, Wk, st, o){
  if(!cv||!pulse) return;
  const allAssets=()=>pulse.groups.flatMap(g=>g.assets.filter(a=>!a.sparse));
  const COL=o.col, hexA=o.hexA, fmtPct=o.fmtPct;
  const dpr=root.devicePixelRatio||1;
  const W=box.clientWidth,H=box.clientHeight;
  cv.width=W*dpr;cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const groups=pulse.groups.map(g=>({name:g.name,
    assets:g.assets.filter(a=>!a.sparse).map(a=>({...a,
      w:Math.max(0.35,Math.min(12,Math.abs(a[Wk]??0)+0.35))}))}))
    .filter(g=>g.assets.length);
  const totalW=groups.reduce((s,g)=>s+g.assets.reduce((x,a)=>x+a.w,0),0)||1;
  /* columns per group, rows per asset */
  const targets=new Map();
  let x=2;
  groups.forEach(g=>{
    const gw=g.assets.reduce((s,a)=>s+a.w,0)/totalW*(W-4-groups.length*2);
    let y=18;
    const gsum=g.assets.reduce((s,a)=>s+a.w,0)||1;
    g.assets.sort((a,b)=>(b.w)-(a.w)).forEach(a=>{
      const ah=a.w/gsum*(H-20-g.assets.length);
      targets.set(a.key,{x,y,w:gw,h:ah,a,gname:g.name});
      y+=ah+1;
    });
    x+=gw+2;
  });
  /* animate from previous rects */
  const t0=performance.now(),dur=o.anim?520:1;
  if(st.anim) cancelAnimationFrame(st.anim);
  const maxAbs=Math.max(2,...allAssets().map(a=>Math.abs(a[Wk]||0)));
  const frame=now=>{
    const p=Math.min(1,(now-t0)/dur), e=1-Math.pow(1-p,3);
    ctx.clearRect(0,0,W,H);
    ctx.font='10px ui-monospace,Consolas,monospace';
    targets.forEach((tgt,key)=>{
      const prev=st.rects.get(key)||{...tgt,w:0,h:0};
      const r={x:prev.x+(tgt.x-prev.x)*e, y:prev.y+(tgt.y-prev.y)*e,
        w:prev.w+(tgt.w-prev.w)*e, h:prev.h+(tgt.h-prev.h)*e};
      const v=tgt.a[Wk]??0;
      const al=0.18+Math.min(1,Math.abs(v)/maxAbs)*0.72;
      ctx.fillStyle=hexA(v>=0?COL.up:COL.dn,al);
      ctx.beginPath(); ctx.roundRect(r.x,r.y,Math.max(1,r.w),Math.max(1,r.h),3); ctx.fill();
      if(r.w>44&&r.h>15){
        ctx.fillStyle=COL.ink; ctx.textAlign='left';
        ctx.fillText(tgt.a.symbol,r.x+5,r.y+12);
        if(r.h>27){ ctx.fillStyle=hexA(COL.ink,.75);
          ctx.fillText(fmtPct(v),r.x+5,r.y+24); }
      }
    });
    /* group captions */
    ctx.fillStyle=COL.muted; ctx.textAlign='left';
    let gx=2;
    groups.forEach(g=>{
      const gw=g.assets.reduce((s,a)=>s+a.w,0)/totalW*(W-4-groups.length*2);
      ctx.fillText(g.name.toUpperCase(),gx+2,12);
      gx+=gw+2;
    });
    if(p<1) st.anim=requestAnimationFrame(frame);
    else { st.rects=targets; st.anim=null; }
  };
  st.anim=requestAnimationFrame(frame);
  cv.onclick=e=>{
    const r=cv.getBoundingClientRect();
    const mx=e.clientX-r.left,my=e.clientY-r.top;
    for(const [key,tgt] of st.rects){
      if(mx>=tgt.x&&mx<=tgt.x+tgt.w&&my>=tgt.y&&my<=tgt.y+tgt.h){ o.onPick(key); break; }
    }
  };
  };
  M.MAP_WINDOWS = [['chg_1d', '1D'], ['chg_1w', '1W'], ['chg_1m', '1M'], ['chg_3m', '3M'], ['chg_ytd', 'YTD'], ['chg_1y', '1Y']];   /* the Studio's Pulse windows */
  /* /markets/indicators' answer drawn: overlays on the price chart, the rest in panes made by addSub(id, labelHtml).
     ctx = { main, bars (columnar), result, addSub, overlays (array the overlay series go in), esc }. */
  M.drawIndicators = function (ctx) {
    const B = ctx.bars, r = ctx.result, esc = ctx.esc || ((s) => String(s));
    const tmap = {}; (r.t || []).forEach((t, i) => { tmap[t] = i; });
    const val = (arr) => B.t.map((t) => { const i = tmap[t]; return i == null ? null : arr[i]; });
    let ci = 0; const range = ctx.main.timeScale().getVisibleLogicalRange(); const seen = new Set();
    for (const ind of (r.indicators || [])) {
      if (ind.error || seen.has(ind.id)) continue;
      seen.add(ind.id);
      const custom = ind.custom === true, baseCol = ind.color || '';
      if (ind.pane === 'main') {
        const names = Object.keys(ind.series || {}), ribbon = ind.kind === 'ribbon';
        names.forEach((nm, k) => {
          const col = ribbon ? M.ribbonColor(k, names.length) : (ind.kind === 'bbands' ? M.hexA(baseCol || '#7aa2f7', nm === 'bb_mid' ? 0.9 : 0.45) : (baseCol || M.OVERLAY_COLORS[ci % M.OVERLAY_COLORS.length]));
          const s = M.lwLine(ctx.main, { color: col, lineWidth: ribbon ? 1 : 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, lineStyle: (ind.kind === 'bbands' && nm !== 'bb_mid') ? 2 : 0 });
          s.setData(M.seriesData(B.t, val(ind.series[nm]))); ctx.overlays.push(s);
          if (!ribbon) ci++;
        });
        if (ribbon) ci++;
      } else {
        const title = custom ? (ind.label || ind.kind) : ind.kind.toUpperCase();
        const sub = ctx.addSub(ind.id, '<b>' + esc(title) + '</b> ' + esc(M.paramStr(ind.params)));
        if (ind.kind === 'macd') {
          const hs = M.lwHist(sub.chart, { priceLineVisible: false, lastValueVisible: false }); const hv = val(ind.series.macd_hist);
          hs.setData(B.t.map((t, i) => (hv[i] == null ? { time: t } : { time: t, value: hv[i], color: hv[i] >= 0 ? 'rgba(63,208,164,.55)' : 'rgba(224,112,90,.55)' })));
          M.lwLine(sub.chart, { color: baseCol || '#7aa2f7', lineWidth: 2, priceLineVisible: false, lastValueVisible: false }).setData(M.seriesData(B.t, val(ind.series.macd)));
          M.lwLine(sub.chart, { color: '#e8b34d', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(M.seriesData(B.t, val(ind.series.macd_signal)));
        } else if (ind.kind === 'stoch') {
          const k = M.lwLine(sub.chart, { color: baseCol || '#7aa2f7', lineWidth: 2, priceLineVisible: false, lastValueVisible: false }); k.setData(M.seriesData(B.t, val(ind.series.stoch_k)));
          M.lwLine(sub.chart, { color: '#e8b34d', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(M.seriesData(B.t, val(ind.series.stoch_d)));
          [20, 80].forEach((lv) => k.createPriceLine({ price: lv, color: 'rgba(138,126,112,.5)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }));
        } else if (ind.kind === 'rsi') {
          const s = M.lwLine(sub.chart, { color: baseCol || '#b48ead', lineWidth: 2, priceLineVisible: false, lastValueVisible: false }); s.setData(M.seriesData(B.t, val(ind.series.rsi)));
          [30, 70].forEach((lv) => s.createPriceLine({ price: lv, color: 'rgba(138,126,112,.5)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false }));
        } else {
          Object.keys(ind.series || {}).forEach((nm, k) => {
            M.lwLine(sub.chart, { color: k === 0 ? (baseCol || '#5ec9b8') : M.OVERLAY_COLORS[k % M.OVERLAY_COLORS.length], lineWidth: k === 0 ? 2 : 1, priceLineVisible: false, lastValueVisible: false })
              .setData(M.seriesData(B.t, val(ind.series[nm])));
          });
        }
        if (range) { try { sub.chart.timeScale().setVisibleLogicalRange(range); } catch (e) { /* follows on the next sync */ } }
      }
    }
  };

  /* ── <vera-mkt-chart symbol provider tf> - the chart on the desk ─────────────────────────────────────────────── */
  const IND = [
    { id: 'ema20', label: 'EMA 20', kind: 'ema', params: { n: 20 } },
    { id: 'sma50', label: 'SMA 50', kind: 'sma', params: { n: 50 } },
    { id: 'bb', label: 'Bollinger', kind: 'bbands', params: { n: 20, k: 2 } },
    { id: 'vwap', label: 'VWAP', kind: 'vwap', params: { n: 0 } },
    { id: 'rsi', label: 'RSI', kind: 'rsi', params: { n: 14 } },
    { id: 'macd', label: 'MACD', kind: 'macd', params: { fast: 12, slow: 26, signal: 9 } },
  ];
  const ON_KEY = 'vera:mktchart:ind';
  /* the chart and the views deeper into its asset (navigation, not actions: nothing here places or changes anything) */
  const VIEWS = [
    { id: 'chart', label: 'Chart', tip: 'The candles' },
    { id: 'news', label: 'News', tip: "The asset's headlines (a web search, run when this is opened)" },
    { id: 'pos', label: 'Position', tip: 'Your holding in this asset and its recorded trades' },
    { id: 'alerts', label: 'Alerts', tip: "The strategies' signals and the monitors on this asset" },
    { id: 'bt', label: 'Backtests', tip: 'Backtests run on this asset - pick one to put its trades on the candles' },
  ];
  const NEWS_TTL = 600000;   /* an asset's headlines are searched again after 10 minutes, not on every opening */
  const newsSeen = new Map();
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  const fmt = (v) => (v == null || !isFinite(v) ? '—' : Math.abs(v) >= 1000 ? v.toFixed(2) : Math.abs(v) >= 1 ? v.toFixed(3) : v.toPrecision(4));
  /* the deeper views' formatters (the Quant Studio's) */
  const F = { esc,
    fmtPx: (v) => { if (v == null || !isFinite(v)) return '—'; const a = Math.abs(v);
      return a >= 100000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : a >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : a >= 1 ? v.toFixed(2) : a >= 0.01 ? v.toFixed(4) : v.toPrecision(4); },
    fmtPct: (v) => (v == null || !isFinite(v) ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%'),
    fmtN: (v) => (v == null ? '—' : Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : (+v).toFixed(2)) };
  const fmtV = (v) => (v == null ? '—' : v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : String(Math.round(v)));
  /* a theme colour as the chart library can read it: the theme's names may be color-mix(...) expressions, which it cannot
     parse - one pixel painted with it says what it is */
  const plainColor = (css, fallback) => { try { const cv = document.createElement('canvas'); cv.width = cv.height = 1; const x = cv.getContext('2d'); x.fillStyle = fallback; x.fillStyle = css; x.fillRect(0, 0, 1, 1);
      const p = x.getImageData(0, 0, 1, 1).data; return 'rgba(' + p[0] + ',' + p[1] + ',' + p[2] + ',' + (p[3] / 255).toFixed(2) + ')'; } catch (e) { return fallback; } };
  /* the theme's colours as a canvas paints them (the sparkline, the market map): up, down, ink, muted - and hexA for either form */
  const themeCol = (el) => { const cs = getComputedStyle(el); const g = (n, fb) => plainColor(cs.getPropertyValue(n).trim() || fb, fb);
    return { up: g('--ok', '#3fd0a4'), dn: g('--err', '#e0705a'), ink: g('--t1', '#e8e0d4'), muted: g('--dim2', '#8a7e70'),
      hexA: (c, a) => (/^rgba?\(/.test(c) ? c.replace(/^rgba?\(([^,]+),([^,]+),([^,)]+)(,[^)]*)?\)$/, 'rgba($1,$2,$3,' + a + ')') : M.hexA(c, a)) }; };
  /* a read that did not reach the server (or the server failing: 5xx) is `failed` - tried again; an answer that says why is not */
  const api = async (path, body) => { try { const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}); if (r.status >= 500) return { error: 'HTTP ' + r.status, failed: true }; return await r.json(); } catch (e) { return { error: String(e), failed: true }; } };


  /* ── <vera-mkt-lookup placeholder limit> - THE asset search (one implementation: the Markets panel's header and the desk chart) ──
     Owner 2026-10-18: "the market chart: the ability for the user to search through other assets and navigate a bit deeper"; the
     redesign plan: elements derived from the panels, one implementation. The Markets panel's own header lookup moved here unchanged:
     /markets/lookup (debounced 250 ms, 8 results), arrow keys / Enter / Escape, a click outside closes it, the asset class and
     "tracked" shown. A pick is told as a 'mkt-pick' event: detail {key, provider, symbol, item}; a host's extra row as 'mkt-extra'. */
  class VeraMktLookup extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: 'open' }); this._items = []; this._act = -1; this._t = 0; }
    connectedCallback() {
      if (this._built) return; this._built = true;
      const ph = this.getAttribute('placeholder') || 'Search assets…';
      this.shadowRoot.innerHTML = `<style>
        :host{display:inline-block;position:relative;min-width:150px;font:inherit}
        .mag{position:absolute;left:8px;top:50%;transform:translateY(-50%);color:var(--dim2,#8a7e70);font-size:11px;pointer-events:none}
        input{width:100%;box-sizing:border-box;background:var(--bg0,#101215);border:1px solid var(--border,var(--bd,rgba(127,127,127,.25)));color:var(--text,var(--t1,#e8e0d4));padding:var(--lk-pad,3px 8px 3px 24px);border-radius:var(--radius,6px);font:inherit;font-size:var(--lk-size,11px)}
        input:focus{outline:none;border-color:var(--acc,#5a9e8f)}
        .dd{position:absolute;left:0;min-width:100%;width:max-content;max-width:min(420px,90vw);top:calc(100% + 3px);z-index:60;background:var(--bg2,#1d2026);border:1px solid var(--border2,var(--bd,rgba(127,127,127,.3)));border-radius:var(--radius-sm,5px);max-height:320px;overflow-y:auto;display:none;box-shadow:0 6px 18px rgba(0,0,0,.35)}   /* z-local: above the element's own siblings only */
        .it{padding:6px 9px;font-size:11.5px;cursor:pointer;display:flex;align-items:center;gap:8px;color:var(--text,var(--t1,#e8e0d4))}
        .it:hover,.it.active{background:var(--bg3,rgba(127,127,127,.14))}
        .it.muted{color:var(--dim2,#8a7e70);cursor:default}
        .cls{font-size:9px;padding:1px 6px;border-radius:8px;background:var(--bg0,#101215);color:var(--dim2,#8a7e70);border:1px solid var(--border,rgba(127,127,127,.25));text-transform:uppercase;letter-spacing:.4px;flex-shrink:0}
        .nm{color:var(--dim2,#8a7e70);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        .track{margin-left:auto;font-size:9px;color:var(--acc2,var(--acc,#5a9e8f))}
      </style><span class="mag">⌕</span><input autocomplete="off" spellcheck="false" placeholder="${esc(ph)}" aria-label="${esc(ph)}"><div class="dd" role="listbox"></div>`;
      const inp = this.shadowRoot.querySelector('input');
      inp.addEventListener('input', () => this._query()); inp.addEventListener('focus', () => this._query());
      inp.addEventListener('keydown', (e) => this._key(e));
      document.addEventListener('click', this._out = (e) => { if (!(e.composedPath ? e.composedPath() : []).includes(this)) this._hide(); });
    }
    disconnectedCallback() { document.removeEventListener('click', this._out); this._built = false; clearTimeout(this._t); }
    focus() { const i = this.shadowRoot && this.shadowRoot.querySelector('input'); if (i) i.focus(); }
    _query() { clearTimeout(this._t); this._t = setTimeout(async () => {
      const inp = this.shadowRoot.querySelector('input'); const q = (inp.value || '').trim(); if (q.length < 1) { this._hide(); return; }
      const lim = parseInt(this.getAttribute('limit') || '8', 10) || 8;
      const r = await api('/markets/lookup?query=' + encodeURIComponent(q) + '&limit=' + lim); if (!r || r.error) return;
      if ((inp.value || '').trim() !== q) return;   /* typed on since: the newer query draws */
      this._extra = (typeof this.extra === 'function' ? (this.extra(q) || []) : []).slice(0, 8);
      this._items = r.results || []; this._act = -1; this._draw(); }, 250); }
    /* the rows: a host's extra rows first (the Studio's metrics: .extra = (q) => [{cls, label, sub, value}]), then the assets */
    _rows() { return (this._extra || []).map((x) => ({ x })).concat(this._items.map((it) => ({ it }))); }
    _draw() { const dd = this.shadowRoot.querySelector('.dd'); const rows = this._rows();
      if (!rows.length) { dd.innerHTML = '<div class="it muted">no matches</div>'; dd.style.display = 'block'; return; }
      dd.innerHTML = rows.map((r, i) => r.x
        ? `<div class="it${i === this._act ? ' active' : ''}" data-r="${i}" role="option"><span class="cls">${esc(r.x.cls || 'more')}</span><b>${esc(r.x.label || '')}</b><span class="nm">${esc(r.x.sub || '')}</span></div>`
        : `<div class="it${i === this._act ? ' active' : ''}" data-r="${i}" role="option"><span class="cls">${esc(String(r.it.asset_class || '?').replace('_', ' '))}</span><b>${esc(r.it.symbol)}</b><span class="nm">${esc(r.it.name || '')}</span>${r.it.tracked ? '<span class="track">tracked</span>' : ''}</div>`).join('');
      dd.style.display = 'block';
      dd.querySelectorAll('.it[data-r]').forEach((d) => { d.onmousedown = (e) => { e.preventDefault(); this._pick(parseInt(d.dataset.r, 10)); }; }); }
    _hide() { const dd = this.shadowRoot && this.shadowRoot.querySelector('.dd'); if (dd) dd.style.display = 'none'; }
    _key(e) { const dd = this.shadowRoot.querySelector('.dd'); if (dd.style.display === 'none') return;
      if (e.key === 'ArrowDown') { this._act = Math.min(this._rows().length - 1, this._act + 1); this._draw(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { this._act = Math.max(0, this._act - 1); this._draw(); e.preventDefault(); }
      else if (e.key === 'Enter' && this._act >= 0) { this._pick(this._act); e.preventDefault(); }
      else if (e.key === 'Escape') this._hide(); }
    _pick(i) { const r = this._rows()[i]; if (!r) return; this._hide(); this.shadowRoot.querySelector('input').value = '';
      if (r.x) { this.dispatchEvent(new CustomEvent('mkt-extra', { bubbles: true, composed: true, detail: r.x })); return; }
      const it = r.it;
      const key = String(it.key || ((it.provider || it.exchange || 'yahoo') + ':' + it.symbol)); const k = key.indexOf(':');
      this.dispatchEvent(new CustomEvent('mkt-pick', { bubbles: true, composed: true, detail: { key, provider: k > 0 ? key.slice(0, k) : 'yahoo', symbol: k > 0 ? key.slice(k + 1) : key, item: it } })); }
  }
  M.Lookup = VeraMktLookup;
  if (root.customElements && !root.customElements.get('vera-mkt-lookup')) root.customElements.define('vera-mkt-lookup', VeraMktLookup);

  class VeraMktChart extends HTMLElement {
    static get observedAttributes() { return ['symbol', 'provider', 'tf']; }
    constructor() { super(); this.attachShadow({ mode: 'open' }); this._subs = []; this._overlays = []; this._seq = 0; }
    connectedCallback() {
      if (this._built) return; this._built = true;
      let on = null; try { on = JSON.parse(localStorage.getItem(ON_KEY) || 'null'); } catch (e) { on = null; }
      this._on = new Set(Array.isArray(on) ? on : ['ema20', 'rsi']); this._vol = true;
      this.shadowRoot.innerHTML = `<style>
        :host{display:block;width:100%;height:100%;min-height:120px}
        /* the layout is the wrapper's: the page round the element may set its display (the canvas's live layer does) */
        .wrap{display:flex;flex-direction:column;width:100%;height:100%;font:11px/1.3 var(--mono,ui-monospace,monospace);color:var(--t2,#b9aea0);background:var(--bg1,#15181d)}   /* its own ground: nothing shows through the chart */
        .hd{display:flex;align-items:center;gap:6px 8px;flex-wrap:wrap;padding:2px 2px 3px}
        .note{color:var(--dim,#6c645a);font-size:9px}
        .hd b{color:var(--t1,#e8e0d4);font-size:12px}
        .hd .lk{flex:0 1 190px;--lk-size:10px;--lk-pad:1px 6px 1px 20px}
        .hd .bk{font:inherit;font-size:11px;padding:0 5px;border-radius:5px;border:1px solid var(--bd,rgba(127,127,127,.25));background:transparent;color:var(--dim2,#8a7e70);cursor:pointer}
        .px{color:var(--t1,#e8e0d4)} .up{color:var(--ok,#3fd0a4)} .dn{color:var(--err,#e0705a)}
        .rd{color:var(--dim2,#8a7e70);flex:1 0 100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px}
        .bar{display:flex;gap:3px;flex-wrap:wrap;align-items:center;padding:0 2px 4px}
        .bar button{font:inherit;font-size:9.5px;padding:0 5px;border-radius:5px;border:1px solid var(--bd,rgba(127,127,127,.25));background:transparent;color:var(--dim2,#8a7e70);cursor:pointer}
        .bar button.on{color:var(--acc,#5a9e8f);border-color:color-mix(in srgb,var(--acc,#5a9e8f) 55%,transparent);background:color-mix(in srgb,var(--acc,#5a9e8f) 12%,transparent)}
        .bar .sp{width:8px}
        .stack{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;gap:2px}
        .pane{position:relative;min-height:0;overflow:hidden} .pane.main{flex:3 1 0} .pane.sub{flex:1 1 0;border-top:1px solid var(--bd,rgba(127,127,127,.18))}
        .pl{position:absolute;left:4px;top:2px;z-index:3;font-size:9.5px;color:var(--dim2,#8a7e70);pointer-events:none}
        .msg{padding:12px;color:var(--dim2,#8a7e70)}
        [hidden]{display:none!important}
        .vw{display:inline-flex;gap:2px;margin-left:auto}
        .vw button{font:inherit;font-size:9.5px;padding:0 6px;border:0;border-bottom:2px solid transparent;background:transparent;color:var(--dim2,#8a7e70);cursor:pointer}
        .vw button.on{color:var(--t1,#e8e0d4);border-bottom-color:var(--acc,#5a9e8f)}
        /* the deeper views: the Markets pages' own rows (M.rows), their classes given here */
        .side{flex:1 1 auto;min-height:0;overflow:auto;padding:2px 4px 6px;font-size:11px}
        .side .muted{color:var(--dim2,#8a7e70)} .side .num{font-family:var(--mono,ui-monospace,monospace)}
        .side .pos,.side .up,.side .bull{color:var(--ok,#3fd0a4)} .side .neg,.side .dn,.side .bear{color:var(--err,#e0705a)}
        .side .chip{display:inline-block;padding:0 6px;border-radius:8px;border:1px solid var(--bd,rgba(127,127,127,.3))}
        .side .condRow{display:flex;align-items:center;gap:6px;margin:4px 0;flex-wrap:wrap;padding:5px 7px;border:1px solid var(--bd,rgba(127,127,127,.2));border-radius:7px}
        .side .macroRow{padding:4px 0;border-bottom:1px dashed var(--bd,rgba(127,127,127,.2))} .side .macroRow a{--ink:var(--t1,#e8e0d4)}
        .side table{border-collapse:collapse;width:100%} .side th,.side td{padding:2px 5px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--bd,rgba(127,127,127,.15))}
        .side th{color:var(--dim2,#8a7e70);font-weight:normal} .side h5{margin:8px 0 3px;font-size:10px;color:var(--dim2,#8a7e70);font-weight:normal;text-transform:uppercase;letter-spacing:.4px}
        .side [data-btid]:hover,.side tr[data-hk]:hover td{background:var(--bg3,rgba(127,127,127,.12))}
        .side .pbar{height:6px;background:rgba(127,127,127,.2);border-radius:3px;overflow:hidden} .side .pbar i{display:block;height:100%}
      </style>
      <div class="wrap"><div class="hd"><button class="bk" data-a="back" title="Back to the asset before" hidden>←</button><b class="sym"></b><vera-mkt-lookup class="lk" placeholder="Search assets…"></vera-mkt-lookup><span class="px"></span><span class="ch"></span>
        <span class="vw">${VIEWS.map((x) => `<button data-a="view" data-v="${x.id}" title="${esc(x.tip)}" class="${x.id === 'chart' ? 'on' : ''}">${esc(x.label)}</button>`).join('')}</span><span class="rd"></span></div>
      <div class="bar"><span class="tfs"></span><span class="sp"></span><span class="inds"></span><span class="sp"></span><button data-a="vol" title="Volume under the candles">Vol</button><button data-a="fit" title="Fit the whole history">Fit</button><span class="note"></span></div>
      <div class="stack"><div class="pane main"></div></div><div class="side" hidden></div></div>`;
      this.shadowRoot.addEventListener('click', (ev) => { const bt = ev.target.closest && ev.target.closest('[data-btid]'); if (bt) { ev.stopPropagation(); this._openBt(bt.dataset.btid); return; }
        if (ev.target.closest && ev.target.closest('.side tr[data-hk]')) { ev.stopPropagation(); this._view('chart'); return; }   /* the holding's row: its candles */
        const b = ev.target.closest && ev.target.closest('button[data-a]'); if (!b) return; ev.stopPropagation(); this._act(b.dataset.a, b.dataset.v); });
      this.shadowRoot.addEventListener('mkt-pick', (ev) => { ev.stopPropagation(); const d = ev.detail || {}; if (!d.symbol) return; this._go(d.provider, d.symbol, true); });
      /* live only while seen: a chart scrolled away, behind a tab or in a pill does not poll */
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._livePlan(); }); this._io.observe(this);
      /* grown past (or shrunk under) the size that has room for the panes: drawn again for it */
      this._ro = new ResizeObserver(() => { this._fit(); const small = (this.clientHeight || 0) < 420; if (this._B && this._small !== undefined && small !== this._small) { clearTimeout(this._rsT); this._rsT = setTimeout(() => this._draw(false), 250); } }); this._ro.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._livePlan());
      this._load(true);
    }
    disconnectedCallback() { clearTimeout(this._liveT); clearTimeout(this._retryT); try { this._io.disconnect(); this._ro.disconnect(); } catch (e) { /* gone */ } document.removeEventListener('visibilitychange', this._vis); this._teardown(); this._built = false; }
    attributeChangedCallback(name) { if (name !== 'tf') this._tfsOf = null; if (this._built) this._load(true); }
    get _sym() { return this.getAttribute('symbol') || ''; } get _prov() { return this.getAttribute('provider') || 'yahoo'; }
    get _tf() { return this.getAttribute('tf') || '1d'; }
    /* another asset in this chart (the search, or Back): history kept; the host hears 'mkt-symbol' {provider, symbol} so it may keep it */
    _go(prov, sym, push) { if (!sym || (sym === this._sym && prov === this._prov)) return;
      if (push) (this._hist = this._hist || []).push({ provider: this._prov, symbol: this._sym });
      /* a backtest's trades belong to the asset it ran on */
      this._tfsOf = null; this._mkt = null; this._entries = []; this._exits = []; this._btNote = ''; this.setAttribute('provider', prov || 'yahoo'); this.setAttribute('symbol', sym);
      const bk = this.shadowRoot.querySelector('.bk'); if (bk) bk.hidden = !(this._hist && this._hist.length);
      if (this._vw && this._vw !== 'chart') this._side();   /* the deeper view open stays open, for the new asset */
      this.dispatchEvent(new CustomEvent('mkt-symbol', { bubbles: true, composed: true, detail: { provider: prov, symbol: sym } })); }
    _act(a, v) {
      if (a === 'back') { const h = (this._hist || []).pop(); if (h) this._go(h.provider, h.symbol, false); return; }
      if (a === 'view') { this._view(v); return; }
      if (a === 'tf') { this.setAttribute('tf', v); return; }
      if (a === 'ind') { if (this._on.has(v)) this._on.delete(v); else this._on.add(v); try { localStorage.setItem(ON_KEY, JSON.stringify([...this._on])); } catch (e) { /* this page only */ } this._draw(false); return; }
      if (a === 'vol') { this._vol = !this._vol; this._draw(false); return; }
      if (a === 'fit') { try { this._main && this._main.timeScale().fitContent(); } catch (e) { /* not drawn */ } }
    }
    _teardown() { try { this._main && this._main.remove(); } catch (e) { /* gone */ } this._subs.forEach((s) => { try { s.chart.remove(); } catch (e) { /* gone */ } try { s.el.remove(); } catch (e) { /* gone */ } });
      this._main = null; this._candle = null; this._subs = []; this._overlays = []; }
    _bars(r) { return r && !r.error && r.count ? { t: r.t, o: r.o, h: r.h, l: r.l, c: r.c, v: r.v || [] } : null; }
    async _load(fit) {
      const seq = ++this._seq; const sr = this.shadowRoot;
      /* the timeframes the asset has stored (its watchlist row); every one its provider offers when that is not known */
      if (!this._tfsOf) { const w = await api('/markets/watchlist'); const row = ((w && w.watchlist) || []).find((x) => x && (x.id === this._prov + ':' + this._sym || (x.symbol === this._sym && x.exchange === this._prov)));
        this._tfsOf = row && Array.isArray(row.timeframes) && row.timeframes.length ? row.timeframes : null; if (seq !== this._seq) return;
        /* the watchlist not read (the server away): not known yet - asked again on the next load, never taken as "not listed" */
        const wFailed = !w || !!w.failed || !Array.isArray(w.watchlist);
        /* NOT ON THE WATCHLIST: only the timeframes that have bars (owner 2026-10-16: "the controls in the revised ohlcv element dont
           work" - a symbol the watchlist does not hold offered every timeframe its provider has; most had no bars, and one pressed
           tore the chart down, so the indicators, Vol and Fit had nothing to draw on). One bar read for each, once. */
        if (!this._tfsOf && !wFailed) { const cands = this._prov === 'yahoo' ? M.YAHOO_TFS : (this._prov === 'custom' ? ['1d'] : M.TFS);
          const got = await Promise.all(cands.map((tf) => api('/markets/bars?dataset_id=' + encodeURIComponent(M.dsId(this._prov, this._sym, tf)) + '&limit=1').then((x) => (x && !x.error && x.count ? tf : (!x || x.failed || typeof x.count !== 'number' ? undefined : null)))));
          /* a timeframe whose probe failed is not known to be empty: none settled until every probe answered */
          if (got.some((g) => g === undefined)) { this._tfsOf = undefined; } else
          { if (seq !== this._seq) return; const have = got.filter(Boolean); this._tfsOf = have.length ? have : null; } }
        if (wFailed) this._tfsOf = undefined;
        /* ANOTHER ASSET WITHOUT THIS TIMEFRAME (18 Oct, the design mirror: BTC-USD on 1h, then ETH-USD, which has no 1h bars - the
           chart said "No stored bars" for good): it opens on a timeframe the asset has - the daily, else its first */
        if (Array.isArray(this._tfsOf) && !this._tfsOf.includes(this._tf)) { this.setAttribute('tf', this._tfsOf.includes('1d') ? '1d' : this._tfsOf[0]); return; } }
      /* not known yet (a read failed): only the timeframe on show - offering every one the provider has led to presses with nothing behind them */
      const tfs = this._tfsOf || (this._tfsOf === undefined ? [...new Set([this._goodTf || this._tf, this._tf])] : (this._prov === 'yahoo' ? M.YAHOO_TFS : (this._prov === 'custom' ? ['1d'] : M.TFS)));
      sr.querySelector('.sym').textContent = this._sym; sr.querySelector('.tfs').innerHTML = tfs.map((tf) => `<button data-a="tf" data-v="${tf}" class="${tf === this._tf ? 'on' : ''}">${tf}</button>`).join('');
      sr.querySelector('.inds').innerHTML = IND.map((d) => `<button data-a="ind" data-v="${d.id}" class="${this._on.has(d.id) ? 'on' : ''}" title="${esc(d.kind)} ${esc(M.paramStr(d.params))}">${esc(d.label)}</button>`).join('');
      const ok = await M.ensureLw(); if (seq !== this._seq) return;
      if (!ok) { this._msg('The chart library did not load (offline?)'); return; }
      const r = await api('/markets/bars?dataset_id=' + encodeURIComponent(M.dsId(this._prov, this._sym, this._tf)) + '&limit=2000'); if (seq !== this._seq) return;
      /* A READ THAT FAILED IS NOT "NO BARS" (owner 2026-10-17, WSU-86 / WSU-92: the desk chart "cant zoom, pan, use the buttons";
         measured: the server restarting under a press of 1h - the read failed, the chart fell back to 1d, that read failed too
         and the drawing was torn down to "Error: TypeError: Failed to fetch", with nothing left to zoom, pan or press). The
         drawing stays as it is, the note says the read failed, and it is read again after 2, 4, 8 ... s (at most 60). */
      /* ...and an answer that is not a bars answer (no count, no error saying why - what a server starting up answers) is failed too:
         a stored dataset with no bars says count 0 */
      if (!r || r.failed || (typeof r.count !== 'number' && !r.error)) { const n = sr.querySelector('.note'); if (n) n.textContent = 'read failed - retrying';
        this._retryN = Math.min(5, (this._retryN || 0) + 1); clearTimeout(this._retryT);
        this._retryT = setTimeout(() => { if (this.isConnected) this._load(fit); }, 1000 * Math.min(60, 2 ** this._retryN));
        if (!this._B) this._msg('Reading ' + this._sym + ' · ' + this._tf + ' failed - trying again');
        else sr.querySelectorAll('.tfs button').forEach((b) => b.classList.toggle('on', b.dataset.v === (this._goodTf || this._tf)));
        return; }
      this._retryN = 0; clearTimeout(this._retryT);
      const B = this._bars(r);
      /* a timeframe with no bars never leaves the chart empty: it stays on the last that drew, and says so */
      if (!B && this._B && this._goodTf && this._goodTf !== this._tf) { const tf = this._tf; this.setAttribute('tf', this._goodTf); const n = sr.querySelector('.note'); if (n) n.textContent = 'no ' + tf + ' bars stored'; return; }
      this._B = B;
      /* NO BARS IS CHECKED AGAIN (17 Oct, the design mirror just after a restart: BTC-USD 1d answered count 0, then 2000 a minute later -
         the chart said `No stored bars` for good): an empty answer is read again after 15, 30 and 60 s before it is believed */
      if (!this._B) { this._teardown(); const again = !(r && r.error) && (this._emptyN || 0) < 3;
        this._msg(r && r.error ? 'Error: ' + r.error : 'No stored bars for ' + this._sym + ' · ' + this._tf + (again ? ' - checking again' : ''));
        if (again) { this._emptyN = (this._emptyN || 0) + 1; this._tfsOf = this._tfsOf || undefined; clearTimeout(this._retryT); this._retryT = setTimeout(() => { if (this.isConnected) this._load(fit); }, 15000 * 2 ** (this._emptyN - 1)); }
        return; }
      this._emptyN = 0;
      this._goodTf = this._tf; this._draw(fit);
    }
    _msg(t) { const st = this.shadowRoot.querySelector('.stack'); st.innerHTML = `<div class="msg">${esc(t)}</div>`; }
    async _draw(fit) {
      const B = this._B; if (!B) return; const seq = ++this._seq; const sr = this.shadowRoot;
      sr.querySelectorAll('.inds button').forEach((b) => b.classList.toggle('on', this._on.has(b.dataset.v)));
      const vb = sr.querySelector('button[data-a="vol"]'); if (vb) vb.classList.toggle('on', this._vol);
      /* A CARD SHOWS THE PRICE, FULL SIZE SHOWS IT ALL (the plan: card = candles + volume; max = the indicator panes too): under
         420 px the panes of their own (RSI, MACD) wait for room; the overlays on the price stay */
      this._small = (this.clientHeight || 0) < 420; const PANE = { rsi: 1, macd: 1 };
      const want = IND.filter((d) => this._on.has(d.id) && !(this._small && PANE[d.kind])).map((d) => ({ id: d.id, kind: d.kind, params: d.params }));
      const noteEl = sr.querySelector('.note'); if (noteEl) noteEl.textContent = [this._btNote, this._small && IND.some((d) => this._on.has(d.id) && PANE[d.kind]) ? 'panes when larger' : ''].filter(Boolean).join(' · ');
      const r = want.length ? await api('/markets/indicators', { dataset_id: M.dsId(this._prov, this._sym, this._tf), indicators: want, limit: 2000 }) : null;
      if (seq !== this._seq) return;
      const keep = this._main ? this._main.timeScale().getVisibleLogicalRange() : null;
      this._teardown();
      const st = sr.querySelector('.stack'); st.innerHTML = '<div class="pane main"></div>';
      const mainEl = st.querySelector('.pane.main'); const L = root.LightweightCharts;
      const tc = plainColor(getComputedStyle(this).getPropertyValue('--dim2').trim() || M.CHART_OPTS.layout.textColor, M.CHART_OPTS.layout.textColor);
      /* each chart takes its pane's size and follows it (the panes had no height yet when the charts were made: they drew over each other) */
      const opts = () => Object.assign({}, M.CHART_OPTS, { layout: Object.assign({}, M.CHART_OPTS.layout, { textColor: tc }), autoSize: true });
      const charts = () => [this._main].concat(this._subs.map((s) => s.chart)).filter(Boolean);
      const sync = M.syncGroup(charts);
      this._main = L.createChart(mainEl, opts()); sync(this._main);
      this._candle = M.lwCandle(this._main, M.candleOpts);
      this._candle.setData(B.t.map((t, i) => ({ time: t, open: B.o[i], high: B.h[i], low: B.l[i], close: B.c[i] })));
      if (this._vol && B.v.some((x) => x > 0)) { this._volS = M.lwHist(this._main, { priceFormat: { type: 'volume' }, priceScaleId: 'vol' }); this._main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } }); this._volS.setData(M.volumeData(B)); } else this._volS = null;
      const addSub = (id, labelHtml) => { const el = document.createElement('div'); el.className = 'pane sub'; el.innerHTML = '<div class="pl">' + labelHtml + '</div>'; st.appendChild(el);
        const chart = L.createChart(el, opts()); sync(chart); const s = { el, chart, id }; this._subs.push(s); return s; };
      if (r && !r.error) M.drawIndicators({ main: this._main, bars: B, result: r, addSub, overlays: this._overlays, esc });
      this._markers = M.setMarkers(this._candle, this._entries, this._exits, null, this._mkt && this._mkt.markers);
      this._mktLoad(seq);
      /* the bar under the pointer, in words */
      this._main.subscribeCrosshairMove((p) => { const i = p && p.time != null ? B.t.indexOf(p.time) : -1; this._readout(i >= 0 ? i : B.t.length - 1); });
      this._readout(B.t.length - 1); this._head();
      requestAnimationFrame(() => { this._fit(); if (keep && !fit) { try { this._main.timeScale().setVisibleLogicalRange(keep); } catch (e) { /* refit */ } } else { try { this._main.timeScale().fitContent(); } catch (e) { /* not drawn */ } } });
      this._livePlan();
    }
    _head() { const B = this._B, sr = this.shadowRoot; if (!B) return; const n = B.c.length; const last = B.c[n - 1], prev = n > 1 ? B.c[n - 2] : last; const ch = prev ? (last - prev) / prev * 100 : 0;
      sr.querySelector('.px').textContent = fmt(last); const c = sr.querySelector('.ch'); c.textContent = (ch >= 0 ? '+' : '') + ch.toFixed(2) + '%'; c.className = 'ch ' + (ch >= 0 ? 'up' : 'dn'); }
    _readout(i) { const B = this._B; if (!B || i < 0) return; const d = new Date(B.t[i] * 1000);
      this.shadowRoot.querySelector('.rd').textContent = d.toISOString().slice(0, this._tf.endsWith('d') || this._tf.endsWith('w') ? 10 : 16).replace('T', ' ') + '  O ' + fmt(B.o[i]) + ' H ' + fmt(B.h[i]) + ' L ' + fmt(B.l[i]) + ' C ' + fmt(B.c[i]) + ' V ' + fmtV(B.v[i]); }
    _fit() { /* the charts size themselves to their panes (autoSize) */ }
    /* the last candle grows while the chart is seen: the newest bars every 15 s, the last updated in place */
    _livePlan() { clearTimeout(this._liveT); if (!this._seen || document.visibilityState === 'hidden' || !this._B) return; this._liveT = setTimeout(() => this._tick(), 15000); }
    async _tick() {
      const B = this._B; if (!B || !this._candle) return this._livePlan();
      const r = await api('/markets/bars?dataset_id=' + encodeURIComponent(M.dsId(this._prov, this._sym, this._tf)) + '&limit=3'); const N = this._bars(r);
      if (N) N.t.forEach((t, j) => { const bar = { time: t, open: N.o[j], high: N.h[j], low: N.l[j], close: N.c[j] };
        const i = B.t.indexOf(t); if (i < 0 && t < B.t[B.t.length - 1]) return;
        if (i >= 0) { B.o[i] = N.o[j]; B.h[i] = N.h[j]; B.l[i] = N.l[j]; B.c[i] = N.c[j]; B.v[i] = N.v[j]; } else { B.t.push(t); B.o.push(N.o[j]); B.h.push(N.h[j]); B.l.push(N.l[j]); B.c.push(N.c[j]); B.v.push(N.v[j]); }
        try { this._candle.update(bar); if (this._volS) this._volS.update({ time: t, value: N.v[j] || 0, color: N.c[j] >= N.o[j] ? 'rgba(63,208,164,.4)' : 'rgba(224,112,90,.4)' }); } catch (e) { /* older than the last bar */ } });
      this._head(); this._livePlan();
    }
    /* signals from a backtest or a strategy: entries and exits as unix times */
    setSignals(entries, exits) { this._entries = entries || []; this._exits = exits || []; if (this._candle) this._markers = M.setMarkers(this._candle, this._entries, this._exits, this._markers, this._mkt && this._mkt.markers); }
    /* a read-only capability through the same /mcp/call the desk uses: the answer's content, or null. Tagged with the surface it
       came from (DO-0.5: the desk, a canvas panel or the Markets page) and whether a person's action started it */
    async _call(name, args) {
      const surf = (() => { try { const h = this.getRootNode && this.getRootNode().host; return h && /^VERA-CANVAS$/i.test(h.tagName) ? (h.deskAlign && h.deskAlign() ? 'desk' : 'panel:canvas') : 'panel:markets'; } catch (e) { return 'panel:markets'; } })();
      try { const r = await fetch('/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Vera-Surface': surf + '; trigger=' + (root.navigator && root.navigator.userActivation && root.navigator.userActivation.isActive ? 'person' : 'auto') }, body: JSON.stringify({ name, arguments: args || {} }) });
        const j = await r.json(); return j && j.type === 'tool_result' ? j.content : j; } catch (e) { return null; } }
    /* GOING DEEPER (owner 2026-10-18: "navigate a bit deeper"; the redesign plan section 4): the asset's news, position, alerts and
       backtests as tabs of the chart. Each is read when its tab is opened or the asset changes under it (never on a timer) and drawn
       by the Markets pages' own rows (M.rows). A backtest picked puts its trades on the candles. */
    _view(v) { this._vw = VIEWS.some((x) => x.id === v) ? v : 'chart'; const sr = this.shadowRoot, chart = this._vw === 'chart';
      sr.querySelectorAll('.vw button').forEach((b) => b.classList.toggle('on', b.dataset.v === this._vw));
      sr.querySelector('.bar').hidden = !chart; sr.querySelector('.stack').hidden = !chart; sr.querySelector('.side').hidden = chart;
      if (!chart) this._side(); }
    async _side() {
      const v = this._vw, prov = this._prov, sym = this._sym, key = prov + ':' + sym, dsp = 'mkt.' + prov + '.' + M.slug(sym) + '.';
      const side = this.shadowRoot.querySelector('.side'), want = v + '|' + key; this._sideId = want;
      side.innerHTML = `<div class="msg">${v === 'news' ? 'Searching headlines for ' + esc(sym) + '…' : 'Reading…'}</div>`;
      const none = (t) => `<div class="msg">${esc(t)}</div>`; let html = '', after = null;
      if (v === 'news') {
        let r = newsSeen.get(key); if (!r || Date.now() - r.at > NEWS_TTL) { r = await this._call('markets.news.feed', { symbol_key: key, limit: 8 }); if (r && r.headlines) newsSeen.set(key, Object.assign({ at: Date.now() }, r)); }
        html = r && r.headlines && r.headlines.length ? M.rows.news(r.headlines, F) + (r.query ? `<div class="muted" style="font-size:9.5px;margin-top:5px">searched: ${esc(r.query)}</div>` : '')
          : none((r && r.error) || 'No headlines found for ' + sym);
      } else if (v === 'pos') {
        const [p, tx] = await Promise.all([this._call('markets.portfolio.positions', {}), this._call('markets.portfolio.tx_list', { symbol_key: key, limit: 50 })]);
        /* the real book's row for this asset, as the Quant Studio's Portfolio page maps and draws it */
        const mine = ((p && p.positions) || []).filter((x) => x.symbol_key === key).map((x) => ({ symbol_key: x.symbol_key, qty: x.qty, avg_cost: x.avg_cost, last: x.last,
          value: x.market_value, unrealized: x.unrealized_pnl, unrealized_pct: x.unrealized_pct, realized: x.realized_pnl, alloc: x.allocation_pct }));
        const txs = ((tx && tx.transactions) || []).filter((x) => !x.symbol_key || x.symbol_key === key);
        if (mine.length) after = (host) => M.fillHoldings(host, { fmtPx: F.fmtPx, fmtPct: F.fmtPct, spark: (cv, vals, up) => M.drawSpark(cv, vals, up, this._sparkCol()),
          closes: async (k) => { const r = await api('/markets/bars?dataset_id=' + encodeURIComponent(M.dsId(prov, sym, '1d')) + '&limit=300'); return r && r.c && r.c.length ? r : null; },
          sent: async () => { const r = await this._call('markets.sentiment.map', {}); const idx = {}; ((r && r.tracked) || []).concat((r && r.benchmarks) || []).forEach((x) => { if (x && x.key) idx[x.key] = x; }); return idx; } });
        html = (mine.length ? `<div style="overflow-x:auto"><table>${M.rows.holdings(mine, F, true)}</table></div>` : none('No position in ' + sym + ' in the real book'))
          + (txs.length ? '<h5>recorded trades</h5><table><tr><th>when</th><th>side</th><th>qty</th><th>price</th></tr>' + txs.map((x) => `<tr><td>${esc(String(x.ts || x.created_at || '').slice(0, 16).replace('T', ' '))}</td><td class="${String(x.side || '').toLowerCase() === 'sell' ? 'dn' : 'up'}">${esc(x.side || '')}</td><td>${esc(x.qty)}</td><td>${F.fmtPx(Number(x.price))}</td></tr>`).join('') + '</table>' : '');
      } else if (v === 'alerts') {
        const [al, mon] = await Promise.all([this._call('markets.alerts.list', { limit: 500 }), this._call('markets.monitor.status', {})]);
        const mine = ((al && al.alerts) || []).filter((x) => String(x.dataset_id || '').startsWith(dsp));
        const mons = ((mon && mon.monitors) || []).filter((m) => String(m.dataset_id || '').startsWith(dsp));
        html = (mons.length ? `<h5>monitored by</h5><div>${mons.map((m) => `<span class="chip">${esc(m.name || m.id)} · ${esc(String(m.dataset_id || '').split('.').pop())}</span>`).join(' ')}</div>` : '')
          + (mine.length ? '<h5>signals</h5>' + M.rows.alerts(mine, F) : none('No alerts for ' + sym + (mons.length ? ' yet' : ' - no strategy monitors it')));
      } else if (v === 'bt') {
        const r = await this._call('markets.backtest.list', { limit: 200 });
        const mine = ((r && r.backtests) || []).filter((b) => String(b.dataset_id || '').startsWith(dsp));
        html = mine.length ? M.rows.backtests(mine, F) + '<div class="muted" style="font-size:9.5px;margin-top:5px">pick one to put its trades on the candles</div>' : none('No backtests run on ' + sym + ' (of the latest 200)');
      }
      if (this._sideId !== want) return; side.innerHTML = html; if (after) after(side); }
    /* the sparkline in the desk's theme colours */
    _sparkCol() { return themeCol(this); }
    /* a backtest's trades on the candles, on the timeframe it ran on */
    async _openBt(id) {
      const r = await this._call('markets.backtest.get', { id }); if (!r || r.error) return;
      const tr = r.trades || []; const tf = String(r.dataset_id || '').split('.').pop();
      this._entries = tr.map((t) => t.entry_t).filter((t) => t != null); this._exits = tr.map((t) => t.exit_t).filter((t) => t != null);
      this._view('chart');
      if (tf && tf !== this._tf && M.TFS.includes(tf)) this.setAttribute('tf', tf);   /* the markers go on with the new bars */
      else this.setSignals(this._entries, this._exits);
      this._btNote = 'backtest ' + M.shortText(r.name, 24) + ': ' + tr.length + ' trades'; const n = this.shadowRoot.querySelector('.note'); if (n) n.textContent = this._btNote; }
    /* the market's own markers (M.marketMarkers): read once a minute per asset and timeframe, then drawn with the signals */
    async _mktLoad(seq) {
      const B = this._B; if (!B || !this._candle) return; const id = this._prov + ':' + this._sym + ':' + this._tf;
      if (!this._mkt || this._mkt.id !== id || Date.now() - this._mkt.at > 60000) {
        let got = null; try { got = await M.marketMarkers((n, a) => this._call(n, a), this._prov, this._sym, B.t.slice()); } catch (e) { got = null; }
        if (seq !== this._seq || !got) return; this._mkt = Object.assign({ id, at: Date.now() }, got); }
      if (!this._candle) return;
      this._markers = M.setMarkers(this._candle, this._entries, this._exits, this._markers, this._mkt.markers);
      (this._plines || []).forEach((l) => { try { this._candle.removePriceLine(l); } catch (e) { /* gone with the chart */ } });
      this._plines = (this._mkt.lines || []).map((l) => { try { return this._candle.createPriceLine({ price: l.price, color: l.color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: l.title }); } catch (e) { return null; } }).filter(Boolean);
    }
  }
  if (root.customElements && !root.customElements.get('vera-mkt-chart')) root.customElements.define('vera-mkt-chart', VeraMktChart);

  /* ── <vera-mkt-map window> - the Quant Studio's market map on the desk (the redesign plan section 4: the Studio's views as
     elements, one implementation). /markets/overview (cached 60 s on the server) drawn by M.drawMarketMap with the Studio's windows;
     read again every 60 s while it is seen. An asset picked opens its <vera-mkt-chart> in place - "← Map" returns. ── */
  class VeraMktMap extends HTMLElement {
    static get observedAttributes() { return ['window']; }
    constructor() { super(); this.attachShadow({ mode: 'open' }); this._st = { rects: new Map(), anim: null }; }
    get _win() { const w = this.getAttribute('window') || 'chg_1d'; return M.MAP_WINDOWS.some((x) => x[0] === w) ? w : 'chg_1d'; }
    connectedCallback() {
      if (this._built) return; this._built = true;
      this.shadowRoot.innerHTML = `<style>
        :host{display:block;width:100%;height:100%;min-height:160px}
        [hidden]{display:none!important}
        .wrap{display:flex;flex-direction:column;width:100%;height:100%;font:11px/1.3 var(--mono,ui-monospace,monospace);color:var(--t2,#b9aea0);background:var(--bg1,#15181d)}
        .hd{display:flex;align-items:center;gap:4px 8px;flex-wrap:wrap;padding:2px 2px 4px}
        .hd b{color:var(--t1,#e8e0d4);font-size:12px} .info{color:var(--dim2,#8a7e70);font-size:10px;margin-left:auto}
        .hd button{font:inherit;font-size:9.5px;padding:0 5px;border-radius:5px;border:1px solid var(--bd,rgba(127,127,127,.25));background:transparent;color:var(--dim2,#8a7e70);cursor:pointer}
        .hd button.on{color:var(--acc,#5a9e8f);border-color:color-mix(in srgb,var(--acc,#5a9e8f) 55%,transparent);background:color-mix(in srgb,var(--acc,#5a9e8f) 12%,transparent)}
        .box{position:relative;flex:1 1 auto;min-height:120px} .box canvas{position:absolute;inset:0;width:100%;height:100%;cursor:pointer}
        .deep{flex:1 1 auto;min-height:0;display:flex} .deep vera-mkt-chart{flex:1 1 auto}
        .msg{padding:12px;color:var(--dim2,#8a7e70)} .box .msg{position:absolute;inset:0;pointer-events:none}
      </style>
      <div class="wrap"><div class="hd"><button class="bk" data-a="map" title="Back to the market map" hidden>← Map</button><b class="ttl">Market map</b>
        <span class="wins">${M.MAP_WINDOWS.map(([w, l]) => `<button data-a="win" data-v="${w}">${l}</button>`).join('')}</span><span class="info"></span></div>
        <div class="box"><canvas></canvas></div><div class="deep" hidden></div></div>`;
      this.shadowRoot.addEventListener('click', (ev) => { const b = ev.target.closest && ev.target.closest('button[data-a]'); if (!b) return; ev.stopPropagation();
        if (b.dataset.a === 'win') this.setAttribute('window', b.dataset.v); else if (b.dataset.a === 'map') this._map(); });
      this._io = new IntersectionObserver((es) => { this._seen = es.some((e) => e.isIntersecting); this._livePlan(); }); this._io.observe(this);
      this._ro = new ResizeObserver(() => { clearTimeout(this._rsT); this._rsT = setTimeout(() => this._draw(false), 150); }); this._ro.observe(this);
      document.addEventListener('visibilitychange', this._vis = () => this._livePlan());
      this._load();
    }
    disconnectedCallback() { clearTimeout(this._liveT); clearTimeout(this._rsT); try { this._io.disconnect(); this._ro.disconnect(); } catch (e) { /* gone */ }
      document.removeEventListener('visibilitychange', this._vis); if (this._st.anim) cancelAnimationFrame(this._st.anim); this._built = false; }
    attributeChangedCallback() { if (this._built) this._draw(true); }
    async _load() {
      const info = this.shadowRoot.querySelector('.info'); if (!this._pulse) info.textContent = 'reading…';
      const r = await api('/markets/overview');
      if (r && r.groups) { this._pulse = r; this._draw(true); }
      else if (!this._pulse) { info.textContent = (r && r.error) || 'overview failed'; }
      this._livePlan(); }
    _livePlan() { clearTimeout(this._liveT); if (!this._seen || document.visibilityState === 'hidden') return; this._liveT = setTimeout(() => this._load(), 60000); }
    _draw(anim) { const sr = this.shadowRoot, P = this._pulse, w = this._win;
      sr.querySelectorAll('.wins button').forEach((b) => b.classList.toggle('on', b.dataset.v === w));
      if (!P) return; sr.querySelector('.info').textContent = P.count + ' assets · ' + String(P.asof || '').slice(11, 16) + 'Z';
      const box = sr.querySelector('.box'); if (box.hidden || !box.clientWidth) return;
      /* nothing with bars yet (an empty overview, or every asset still backfilling): said, not an empty box */
      const none = !(P.groups || []).some((g) => (g.assets || []).some((a) => !a.sparse)); const em = box.querySelector('.msg');
      if (none) { if (!em) box.insertAdjacentHTML('beforeend', '<div class="msg">No assets with bars in the overview yet</div>'); } else if (em) em.remove();
      const col = themeCol(this);
      M.drawMarketMap(box.querySelector('canvas'), box, P, w, this._st, { col, hexA: col.hexA, fmtPct: F.fmtPct, anim: !!anim, onPick: (key) => this._open(key) }); }
    /* an asset picked: its chart, here */
    _open(key) { const sr = this.shadowRoot, i = String(key).indexOf(':'); const prov = i > 0 ? key.slice(0, i) : 'yahoo', sym = i > 0 ? key.slice(i + 1) : key;
      const deep = sr.querySelector('.deep'); deep.innerHTML = ''; const c = document.createElement('vera-mkt-chart');
      c.setAttribute('provider', prov); c.setAttribute('symbol', sym); c.setAttribute('tf', '1d'); deep.appendChild(c);
      sr.querySelector('.box').hidden = true; sr.querySelector('.wins').hidden = true; deep.hidden = false; sr.querySelector('.bk').hidden = false; sr.querySelector('.ttl').textContent = sym;
      this.dispatchEvent(new CustomEvent('mkt-symbol', { bubbles: true, composed: true, detail: { provider: prov, symbol: sym } })); }
    _map() { const sr = this.shadowRoot; sr.querySelector('.deep').hidden = true; sr.querySelector('.deep').innerHTML = ''; sr.querySelector('.box').hidden = false;
      sr.querySelector('.wins').hidden = false; sr.querySelector('.bk').hidden = true; sr.querySelector('.ttl').textContent = 'Market map'; this._st.rects = new Map(); this._draw(true); }
  }
  M.Map = VeraMktMap;
  if (root.customElements && !root.customElements.get('vera-mkt-map')) root.customElements.define('vera-mkt-map', VeraMktMap);
})(window);
