
'use strict';
/* ═══════════════════ Market Pulse ═══════════════════ */
const TM={rects:new Map(), anim:null};   /* treemap animation state */

async function loadPulse(refresh){
  $('pulseInfo').innerHTML='<span class="spin"></span>';
  const r=await api('/markets/overview'+(refresh?'?refresh=true':''),'GET',null,120000);
  if(r&&r.groups){ S.pulse=r; renderPulse(); }
  else $('pulseInfo').textContent=(r&&r.error)||'overview failed';
}
$('btnPulseRefresh').onclick=()=>loadPulse(true);
$('btnBaseline').onclick=async()=>{
  $('btnBaseline').innerHTML='<span class="spin"></span> tracking…';
  const r=await api('/markets/baseline/ensure','POST',{},180000);
  $('btnBaseline').textContent='🛰 track baseline estate';
  if(r&&r.ok){ toast(`baseline: +${r.added.length} added, ${r.backfilling.length} backfilling — sectors go live as bars land`,'ok');
    loadWatch(); setTimeout(()=>loadPulse(true),4000); }
  else toast(esc((r&&r.error)||'failed'),'err');
};
$('pulseWin').querySelectorAll('button').forEach(b=>b.onclick=()=>{
  S.pulseWin=b.dataset.w;
  $('pulseWin').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
  renderPulse();
});
function allAssets(){ return S.pulse?S.pulse.groups.flatMap(g=>g.assets.filter(a=>!a.sparse)):[]; }
function renderPulse(){
  if(!S.pulse) return;
  const W=S.pulseWin;
  $('pulseInfo').textContent=S.pulse.count+' assets · '+(S.pulse.asof||'').slice(11,16)+'Z';
  /* hero gauges */
  const hero=[];
  const assets=allAssets();
  const idx=S.pulse.groups.find(g=>g.name==='Index');
  const sect=S.pulse.groups.find(g=>g.name==='Sectors');
  const ups=assets.filter(a=>(a[W]??0)>0).length;
  hero.push(gaugeHtml('market breadth',assets.length?Math.round(ups/assets.length*100)+'%':'—',
    assets.length?ups/assets.length*100:0, ups>=assets.length/2?'var(--up)':'var(--dn)',
    `${ups}/${assets.length} advancing`));
  if(sect) hero.push(gaugeHtml('sector breadth',(sect.breadth_1d??'—')+'%',sect.breadth_1d||0,
    (sect.breadth_1d||0)>=50?'var(--up)':'var(--dn)','1-day advancers'));
  const spy=assets.find(a=>a.symbol==='SPY'), vix=assets.find(a=>a.symbol==='^VIX');
  if(spy) hero.push(gaugeHtml('S&P 500',fmtPct(spy[W]),50+Math.max(-50,Math.min(50,(spy[W]||0)*8)),
    (spy[W]||0)>=0?'var(--up)':'var(--dn)','RSI '+(spy.rsi??'—')));
  if(vix) hero.push(gaugeHtml('VIX fear',fmtPx(vix.last),Math.min(100,(vix.last||0)*2),
    (vix.last||0)>25?'var(--dn)':(vix.last||0)>18?'var(--warn)':'var(--up)',fmtPct(vix[W])+' today'));
  const btc=assets.find(a=>a.symbol==='BTC-USD');
  if(btc) hero.push(gaugeHtml('Bitcoin',fmtPct(btc[W]),50+Math.max(-50,Math.min(50,(btc[W]||0)*3)),
    (btc[W]||0)>=0?'var(--up)':'var(--dn)',btc.trend+' · '+fmtPx(btc.last)));
  const movers=assets.slice().sort((a,b)=>Math.abs(b[W]||0)-Math.abs(a[W]||0)).slice(0,3);
  hero.push(`<div class="gauge"><div class="gl">top movers</div>${movers.map(a=>
    `<div style="display:flex;gap:6px;font-size:11.5px;margin-top:3px"><b>${esc(a.symbol)}</b>
     <span class="num ${a[W]>=0?'up':'dn'}">${fmtPct(a[W])}</span></div>`).join('')}</div>`);
  $('pulseHero').innerHTML=hero.join('');
  requestAnimationFrame(()=>{ $('pulseHero').querySelectorAll('.gbar>i').forEach(el=>{
    el.style.width=el.dataset.w+'%'; }); });
  drawTreemap();
  /* sector strips */
  $('pulseSectors').innerHTML=S.pulse.groups.map(g=>`
    <div class="sectorRow">
      <div class="hd"><b>${esc(g.name)}</b>
        <span class="chip">${g.count}</span>
        ${g.median_1d!=null?`<span class="num ${g.median_1d>=0?'up':'dn'}" style="font-size:11px">med ${fmtPct(g.median_1d)}</span>`:''}
        ${g.breadth_1d!=null?`<span class="muted" style="font-size:10.5px">${g.breadth_1d}% adv</span>`:''}</div>
      <div class="assetChips">${g.assets.map(a=>a.sparse
        ?`<div class="aChip muted" title="no bars yet — backfilling"><span class="s">${esc(a.symbol)}</span><span style="font-size:10px">…</span></div>`
        :`<div class="aChip" data-key="${esc(a.key)}">
           <div><div class="s">${esc(a.name||a.symbol)}</div>
             <div class="c ${((a[W]??0)>=0)?'up':'dn'}">${fmtPct(a[W])}</div></div>
           <canvas data-spark="${esc(a.key)}"></canvas>
           <span class="chip ${a.trend}" style="font-size:8.5px;padding:1px 5px">${a.trend}</span>
          </div>`).join('')}</div>
    </div>`).join('');
  $('pulseSectors').querySelectorAll('.aChip[data-key]').forEach(el=>{
    el.onclick=()=>assetDrill(el.dataset.key);
  });
  /* sparklines */
  assets.forEach(a=>{
    const cv=$('pulseSectors').querySelector(`[data-spark="${CSS.escape(a.key)}"]`);
    if(cv&&a.spark) drawSpark(cv,a.spark,(a[W]??0)>=0);
  });
}
function gaugeHtml(label,val,pct,color,sub){
  return `<div class="gauge"><div class="gl">${label}</div>
    <div class="gv num">${val}</div>
    <div class="gbar"><i data-w="${Math.max(2,Math.min(100,pct||0))}" style="background:${color};width:0"></i></div>
    <div class="gl" style="margin-top:4px;text-transform:none;letter-spacing:0">${sub||''}</div></div>`;
}
function drawSpark(cv,vals,up){
  const dpr=devicePixelRatio||1;
  const W=cv.clientWidth||56,H=cv.clientHeight||20;
  cv.width=W*dpr;cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const lo=Math.min(...vals),hi=Math.max(...vals);
  const col=up?COL.up:COL.dn;
  ctx.beginPath();
  vals.forEach((v,i)=>{
    const x=i/(vals.length-1)*W, y=H-1-(hi>lo?(v-lo)/(hi-lo):0.5)*(H-2);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
  ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.stroke();
  ctx.lineTo(W,H); ctx.lineTo(0,H); ctx.closePath();
  const g=ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,hexA(col,.3)); g.addColorStop(1,hexA(col,0));
  ctx.fillStyle=g; ctx.fill();
}
/* animated slice-and-dice treemap grouped by sector */
function drawTreemap(){
  const cv=$('treemapCv'); if(!cv||!S.pulse) return;
  const box=$('treemapBox');
  const dpr=devicePixelRatio||1;
  const W=box.clientWidth,H=box.clientHeight;
  cv.width=W*dpr;cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const Wk=S.pulseWin;
  const groups=S.pulse.groups.map(g=>({name:g.name,
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
  const t0=performance.now(),dur=ANIM_ON?520:1;
  if(TM.anim) cancelAnimationFrame(TM.anim);
  const maxAbs=Math.max(2,...allAssets().map(a=>Math.abs(a[Wk]||0)));
  const frame=now=>{
    const p=Math.min(1,(now-t0)/dur), e=1-Math.pow(1-p,3);
    ctx.clearRect(0,0,W,H);
    ctx.font='10px ui-monospace,Consolas,monospace';
    targets.forEach((tgt,key)=>{
      const prev=TM.rects.get(key)||{...tgt,w:0,h:0};
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
    if(p<1) TM.anim=requestAnimationFrame(frame);
    else { TM.rects=targets; TM.anim=null; }
  };
  TM.anim=requestAnimationFrame(frame);
  cv.onclick=e=>{
    const r=cv.getBoundingClientRect();
    const mx=e.clientX-r.left,my=e.clientY-r.top;
    for(const [key,tgt] of TM.rects){
      if(mx>=tgt.x&&mx<=tgt.x+tgt.w&&my>=tgt.y&&my<=tgt.y+tgt.h){ assetDrill(key); break; }
    }
  };
}
async function assetDrill(key){
  const a=allAssets().find(x=>x.key===key);
  if(!a) return;
  const html=`<h4>${esc(a.name||a.symbol)} <span class="muted">· ${esc(a.key)}</span></h4>
    <div class="row"><span class="gv num" style="font-size:20px">${fmtPx(a.last)}</span>
      <span class="chip ${a.trend}">${a.trend} ${fmtPct(a.trend_slope_pct_year,false)}/yr</span></div>
    <div class="row" style="flex-wrap:wrap;gap:4px">${['chg_1d','chg_1w','chg_1m','chg_3m','chg_ytd','chg_1y'].map(w=>
      `<span class="chip"><span class="muted">${w.slice(4).toUpperCase()}</span>
       <b class="num ${((a[w]??0)>=0)?'up':'dn'}">${fmtPct(a[w])}</b></span>`).join('')}</div>
    <div class="row kv" style="display:grid">
      <span class="k">RSI-14</span><span class="num">${a.rsi??'—'}</span>
      <span class="k">30d vol (ann.)</span><span class="num">${a.vol_30d_pct??'—'}%</span>
      <span class="k">52-week range</span><span class="num">${fmtPx(a.lo_52w)} … ${fmtPx(a.hi_52w)} (at ${a.range_52w_pct??'—'}%)</span>
      <span class="k">bars stored</span><span class="num">${a.bars}</span></div>
    <div class="row"><div class="pbar" style="flex:1"><i style="width:${a.range_52w_pct||0}%;background:linear-gradient(90deg,var(--dn),var(--warn),var(--up))"></i></div></div>
    <div class="row">
      <button class="pri" id="adOpen">open in Charts →</button>
      <button id="adBt">backtest here</button></div>`;
  const p=popAt($('tmCap'),html);
  p.querySelector('#adOpen').onclick=()=>{ closePop(); switchView('charts');
    openInNewTile(key); };
  p.querySelector('#adBt').onclick=()=>{ closePop(); switchView('run');
    $('rcAsset').value=key; fillTfSel(); };
}

/* ═══════════════════ Sim accounts ═══════════════════ */
async function loadSim(){
  const r=await api('/markets/sim/list');
  if(r&&r.accounts){ S.simAccounts=r.accounts; renderSim(); }
}
$('btnSimRefresh').onclick=loadSim;
$('btnSimNew').onclick=async()=>{
  const name=prompt('sim account name','paper-1'); if(!name) return;
  const cash=parseFloat(prompt('starting cash','100000')||'100000');
  const r=await api('/markets/sim/create','POST',{name,cash});
  if(r&&r.ok){ toast('sim account created','ok'); loadSim(); }
};
function renderSim(){
  $('simGrid').innerHTML=S.simAccounts.map(a=>`
    <div class="simCard ${S.simSel===a.id?'on':''}" data-id="${a.id}">
      <div style="display:flex;align-items:center;gap:8px"><b>${esc(a.name)}</b>
        <span style="flex:1"></span>
        <span class="num ${a.ret_pct>=0?'up':'dn'}">${fmtPct(a.ret_pct)}</span></div>
      <div class="v num">$${(a.value||0).toLocaleString(undefined,{maximumFractionDigits:0})}</div>
      <div class="muted" style="font-size:10.5px">cash $${(a.cash||0).toLocaleString(undefined,{maximumFractionDigits:0})}
        · ${(a.positions||[]).filter(p=>p.qty>0).length} positions</div>
      <canvas data-eq="${a.id}"></canvas>
    </div>`).join('')||'<div class="empty"><span class="big">◎</span>No sim accounts yet — create one and let strategies or agents paper-trade it.</div>';
  $('simGrid').querySelectorAll('.simCard').forEach(el=>el.onclick=()=>{
    S.simSel=el.dataset.id; renderSim(); renderSimDetail(); });
  S.simAccounts.forEach(async a=>{
    const cv=$('simGrid').querySelector(`[data-eq="${a.id}"]`);
    if(!cv) return;
    const eq=await api('/markets/sim/equity?account_id='+a.id+'&limit=300');
    if(eq&&eq.value&&eq.value.length>1) drawSpark(cv,eq.value,(a.ret_pct||0)>=0);
  });
  if(S.simSel) renderSimDetail();
}
async function renderSimDetail(){
  const a=S.simAccounts.find(x=>x.id===S.simSel);
  const host=$('simDetail');
  if(!a){ host.innerHTML=''; return; }
  const eq=await api('/markets/sim/equity?account_id='+a.id+'&limit=1000');
  host.innerHTML=`
    <div class="card">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <h2 class="sec" style="margin:0">${esc(a.name)}</h2>
        <span class="chip">fee ${a.fee_bps} bps</span>
        <span style="flex:1"></span>
        <button id="simReset" class="danger">reset</button>
        <button id="simDel" class="danger">delete</button></div>
      <div class="grid2" style="margin-top:10px">
        <div class="chartBox" style="height:180px"><span class="cap">equity</span><canvas id="simEqCv"></canvas></div>
        <div>
          <h4 class="sec" style="margin-top:0">order ticket</h4>
          <div class="formRow"><label>symbol</label><select id="soSym" style="flex:1">${
            S.watch.filter(w=>w.exchange!=='macro').map(w=>
              `<option value="${w.id}">${esc(w.symbol)}</option>`).join('')}</select></div>
          <div class="formRow"><label>side</label>
            <select id="soSide"><option value="buy">buy</option><option value="sell">sell</option></select>
            <label style="min-width:0">size</label>
            <input id="soPct" type="number" value="25" style="width:60px"><span class="muted">% of ${'cash/position'}</span></div>
          <div class="formRow"><button class="pri" id="soGo" style="flex:1">place sim order</button></div>
          <h4 class="sec">auto-trade a strategy</h4>
          <div class="formRow"><select id="soStrat" style="flex:1">${
            S.strategies.filter(s=>s.status==='accepted').map(s=>
              `<option value="${s.id}">${esc(s.name)}</option>`).join('')||'<option value="">no live-monitored strategies</option>'}</select>
            <button id="soLink">link →</button></div>
          <p class="muted" style="font-size:10.5px">Linking makes every monitor signal place a
            paper order here (buy = size% of cash, sell = close).</p>
        </div>
      </div>
      <h4 class="sec">positions</h4>
      <table class="tbl" id="simPos"></table>
      <h4 class="sec">orders</h4>
      <div style="max-height:200px;overflow:auto"><table class="tbl" id="simOrd"></table></div>
    </div>`;
  if(eq&&eq.t&&eq.t.length>1)
    drawSeries($('simEqCv'),{series:[{t:eq.t.map(x=>Math.floor(new Date(x).getTime()/1000)),
      v:eq.value,color:COL.acc,width:1.6,fill:true}],fmt:v=>'$'+fmtN(v),animate:true});
  $('simPos').innerHTML='<tr><th>asset</th><th>qty</th><th>avg cost</th><th>last</th><th>value</th><th>unrl</th><th>realized</th></tr>'+
    (a.positions||[]).map(p=>`<tr><td>${esc(p.symbol_key)}</td><td>${p.qty}</td>
      <td>${fmtPx(p.avg_cost)}</td><td>${fmtPx(p.last)}</td><td>${fmtN(p.value)}</td>
      <td class="${(p.unrealized||0)>=0?'up':'dn'}">${fmtN(p.unrealized)}</td>
      <td class="${(p.realized||0)>=0?'up':'dn'}">${fmtN(p.realized)}</td></tr>`).join('')
    ||'<tr><td colspan="7" class="muted">flat</td></tr>';
  $('simOrd').innerHTML='<tr><th>time</th><th>side</th><th>asset</th><th>qty</th><th>price</th><th>src</th></tr>'+
    ((eq&&eq.orders)||[]).map(o=>`<tr><td>${esc((o.ts||'').slice(0,16))}</td>
      <td class="${o.side==='buy'?'up':'dn'}">${o.side}</td><td>${esc(o.symbol_key)}</td>
      <td>${(+o.qty).toFixed(6)}</td><td>${fmtPx(o.price)}</td><td>${esc(o.source||'')}</td></tr>`).join('');
  host.querySelector('#simReset').onclick=async()=>{
    if(!confirm('reset account — wipe orders & history?')) return;
    await api('/markets/sim/reset','POST',{account_id:a.id}); loadSim(); };
  host.querySelector('#simDel').onclick=async()=>{
    if(!confirm('delete account?')) return;
    await api('/markets/sim/delete','POST',{account_id:a.id}); S.simSel=null; loadSim(); };
  host.querySelector('#soGo').onclick=async()=>{
    const r=await api('/markets/sim/order','POST',{account_id:a.id,
      symbol_key:host.querySelector('#soSym').value,
      side:host.querySelector('#soSide').value,
      pct:+host.querySelector('#soPct').value||25});
    if(r&&r.ok){ toast(`${r.order.side} ${(+r.order.qty).toFixed(6)} @ ${fmtPx(r.order.price)}`,'ok'); loadSim(); }
    else toast(esc((r&&r.error)||'order failed'),'err');
  };
  host.querySelector('#soLink').onclick=async()=>{
    const sid=host.querySelector('#soStrat').value;
    if(!sid) return toast('accept a strategy for monitoring first (Strategy tab)','err');
    const r=await api('/markets/strategy/accept','POST',
      {id:sid,sim_account_id:a.id,sim_pct:+host.querySelector('#soPct').value||25});
    toast(r&&r.ok?'linked — monitor signals now paper-trade here':esc((r&&r.error)||'failed'),
      r&&r.ok?'ok':'err');
  };
}

/* ═══════════════════ events (WS + poll fallback) ═══════════════════ */
let _ws=null,_wsOk=false,_pollT=null,_lastEvt=0;
function connectWS(){
  let url;
  try{ url=(BASE||location.origin).replace(/^http/,'ws')+'/ws/mcp'; }
  catch(_){ pollStart(); return; }
  try{ _ws=new WebSocket(url); }catch(_){ pollStart(); return; }
  _ws.onopen=()=>{ _wsOk=true; $('wsDot').classList.add('on');
    try{_ws.send(JSON.stringify({action:'subscribe_events'}));}catch(_){} };
  _ws.onmessage=e=>{ let m;
    try{ m=JSON.parse(e.data);}catch(_){return;}
    if(m&&m.type==='event'&&m.data) handleEvent(m.data); };
  const drop=()=>{ if(!_ws)return; _ws=null;_wsOk=false; $('wsDot').classList.remove('on');
    setTimeout(connectWS,6000); pollStart(); };
  _ws.onclose=drop; _ws.onerror=drop;
}
function pollStart(){
  if(_pollT) return;
  _pollT=setInterval(async()=>{
    if(_wsOk){clearInterval(_pollT);_pollT=null;return;}
    const evs=await api('/events?limit=40');
    if(Array.isArray(evs)){
      const fresh=evs.filter(ev=>ev.ts&&ev.ts>_lastEvt).reverse();
      if(evs[0]&&evs[0].ts)_lastEvt=evs[0].ts;
      fresh.forEach(handleEvent);
    }
  },4000);
}
function handleEvent(ev){
  const ty=String(ev.type||'');
  if(ty==='markets.backtest') rcEvent(ev);
  else if(ty==='markets.fetch'){
    $('tbStatus').textContent=(ev.symbol||'')+' '+(ev.timeframe||'')+' '+(ev.stage||'')+
      (typeof ev.fetched==='number'?' '+ev.fetched:'');
    if(ev.stage==='done'){ loadWatch();
      Object.values(TILES).forEach(t=>{
        if(t.key===((ev.exchange||'')+':'+(ev.symbol||''))) tileLoad(t,true); });
      if($('dataDrawer').classList.contains('on')) fillDrawer();
      if(S.macro.length) loadMacro().then(()=>{ if($('dataDrawer').classList.contains('on')) renderMacroList(); });
    }
  }
  else if(ty==='markets.tick'){
    (ev.ticks||[]).forEach(tk=>{ const q=S.quotes[tk.key];
      if(q){q.last=tk.price;q.ts=tk.ts;} else S.quotes[tk.key]={key:tk.key,last:tk.price}; });
    Object.values(TILES).forEach(tileQuote);
  }
  else if(ty==='markets.annotate'){
    Object.values(TILES).forEach(t=>{ if(t.key===ev.symbol_key) tileEvents(t); });
  }
  else if(ty==='markets.sim'){ if(S.view==='sim') loadSim(); }
  else if(ty==='markets.ml'){ if(ev.stage==='trained'){ loadModels(); toast('🧠 model trained','ok'); } }
  else if(ty==='markets.alert'){ toast('🔔 '+esc(ev.message||'signal'),'ok'); }
  else if(ty==='markets.sentiment'||ty==='markets.baseline'){ /* pulse picks it up on refresh */ }
}

/* ═══════════════════ view switching + init ═══════════════════ */
const VIEW_TITLES={charts:'Charts',strat:'Strategy Studio',run:'Backtest Run Center',
  pulse:'Market Pulse',sim:'Sim Accounts'};
function switchView(name){
  S.view=name;
  document.querySelectorAll('.railBtn').forEach(b=>b.classList.toggle('on',b.dataset.view===name));
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('on',v.id===name+'View'));
  $('tbTitle').textContent=VIEW_TITLES[name]||name;
  if(name==='strat'){ loadStrats(); loadLibrary().then(()=>{ if(!B) renderLibraryHome(); }); loadModels(); }
  if(name==='run'){ loadStrats(); loadEngines(); loadRunHist(); }
  if(name==='pulse'&&!S.pulse) loadPulse();
  if(name==='sim') loadSim();
  if(name==='charts') setTimeout(()=>Object.values(TILES).forEach(t=>t.chart._resize()),60);
}
document.querySelectorAll('.railBtn').forEach(b=>b.onclick=()=>switchView(b.dataset.view));

async function init(){
  readTheme();
  await loadWatch();
  await Promise.all([loadQuotes(),loadMacro(),loadCustomInds(),loadModels(),loadLibrary()]);
  /* restore auto-layout or open a default pair of tiles */
  const auto=pref('autolayout');
  if(auto&&auto.tiles&&auto.tiles.length) await applyLayout(auto);
  else{
    const t1=addTile(); const t2=addTile();
    if(S.watch[0]) await tileSetAsset(t1,S.watch[0].id);
    if(S.watch[1]) await tileSetAsset(t2,S.watch[1].id);
  }
  connectWS();
  setInterval(loadQuotes,30000);
  setInterval(()=>{ if(S.view==='pulse') loadPulse(true); },60000);
  setInterval(()=>{ pref('autolayout',layoutData()); },20000);
  window.addEventListener('beforeunload',()=>pref('autolayout',layoutData()));
}
init();
