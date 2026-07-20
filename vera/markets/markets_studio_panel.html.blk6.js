
'use strict';
/* ═══ round 3: live strategy overlay · Project view · ML lab · screener resilience ═══ */

/* ── 🎯 live strategy overlay on chart tiles ── */
async function tileStratOverlay(t){
  if(!t.overlay||!t.overlay.on||!t.overlay.strategy_id||!t.key){
    if(t._ovlOn){ t.chart.setMarkers([]); t._ovlOn=false; tileOvlBadge(t,null); }
    return;
  }
  const ds=dsId(t.provider,t.symbol,t.tf);
  const r=await api('/markets/backtest/signals','POST',
    {dataset_id:ds,strategy_id:t.overlay.strategy_id,limit:6000});
  if(!r||!r.ok){ if(r&&r.error) toast('overlay: '+esc(r.error),'err'); return; }
  t._ovlOn=true;
  t.chart.setMarkers(
    (r.entries||[]).map(ts=>({t:ts,kind:'buy'}))
    .concat((r.exits||[]).map(ts=>({t:ts,kind:'sell'})))
    .concat((r.short_entries||[]).map(ts=>({t:ts,kind:'short'})))
    .concat((r.short_exits||[]).map(ts=>({t:ts,kind:'cover'}))));
  tileOvlBadge(t, r.entry_now?'▲ LONG NOW':r.short_entry_now?'▼ SHORT NOW'
    :r.exit_now?'▽ EXIT NOW':'live');
}
function tileOvlBadge(t,txt){
  let b=t.el.querySelector('.ovlBadge');
  if(!txt){ if(b) b.remove(); t.el.querySelector('.bStrat').classList.remove('on'); return; }
  if(!b){ b=document.createElement('span'); b.className='ovlBadge chip';
    b.style.cssText='font-size:9px;padding:1px 7px';
    t.el.querySelector('.tileBar .sp').before(b); }
  const hot=txt!=='live';
  b.textContent='🎯 '+txt;
  b.className='ovlBadge chip '+(txt.startsWith('▲')?'bull':txt.startsWith('▼')?'bear':hot?'on':'');
  if(hot&&ANIM_ON) b.style.animation='pulseA 1.6s infinite'; else b.style.animation='';
  t.el.querySelector('.bStrat').classList.add('on');
}
function openStratOverlayPop(t,anchor){
  const html=`<h4>🎯 Live strategy overlay</h4>
    <div class="row"><label class="chip"><input type="checkbox" id="ovOn" ${t.overlay.on?'checked':''}> show signals on this chart</label></div>
    <div class="row"><select id="ovStrat" style="flex:1">${
      S.strategies.map(x=>`<option value="${x.id}"${t.overlay.strategy_id===x.id?' selected':''}>${esc(x.name)}${x.status==='accepted'?' ●':''}</option>`).join('')
      ||'<option value="">— no saved strategies —</option>'}</select></div>
    <div class="row muted" style="font-size:11px">Entry/exit markers (▲▼ long, violet short) render on
      the live chart and refresh with every new bar — the badge pulses when a signal is
      firing RIGHT NOW. ● = under live monitoring.</div>
    <div class="row"><span style="flex:1"></span><button class="ghost" id="popClose">done</button></div>`;
  const p=popAt(anchor,html);
  p.querySelector('#ovOn').onchange=e=>{ t.overlay.on=e.target.checked; tileStratOverlay(t); };
  p.querySelector('#ovStrat').onchange=e=>{ t.overlay.strategy_id=e.target.value;
    if(t.overlay.on) tileStratOverlay(t); };
  p.querySelector('#popClose').onclick=closePop;
}

/* ── ⧗ Project & optimize view ── */
VIEW_TITLES.proj='Project & Optimize';
let _projFilled=false;
async function projInit(){
  if(_projFilled) return; _projFilled=true;
  const r=await api('/markets/sim/list');
  const sel=$('pjSource');
  ((r&&r.accounts)||[]).forEach(a=>{
    const o=document.createElement('option');
    o.value='sim:'+a.id; o.textContent='sim: '+a.name;
    sel.appendChild(o); });
}
function drawBands(cv,pj){
  const dpr=devicePixelRatio||1;
  const W=cv.clientWidth||cv.parentElement.clientWidth,H=cv.clientHeight||cv.parentElement.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const td=pj.t_days,N=pj.nominal,R=pj.real;
  const all=N.p90.concat(N.p10,[pj.value_now||pj.start_value||0]);
  let lo=Math.min(...all),hi=Math.max(...all);
  const pad=(hi-lo)*.08; lo-=pad; hi+=pad;
  const X=i=>10+(td[i]/td[td.length-1])*(W-76);
  const Y=v=>12+(1-(v-lo)/(hi-lo))*(H-40);
  ctx.font='9.5px ui-monospace,Consolas,monospace';
  [lo+(hi-lo)*.25,lo+(hi-lo)*.5,lo+(hi-lo)*.75].forEach(v=>{
    ctx.strokeStyle=COL.grid; ctx.beginPath();ctx.moveTo(10,Y(v));ctx.lineTo(W-66,Y(v));ctx.stroke();
    ctx.fillStyle=COL.muted; ctx.textAlign='left'; ctx.fillText('$'+fmtN(v),W-62,Y(v)+3); });
  const band=(a,b,alpha)=>{ ctx.beginPath();
    a.forEach((v,i)=>i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)));
    for(let i=b.length-1;i>=0;i--) ctx.lineTo(X(i),Y(b[i]));
    ctx.closePath(); ctx.fillStyle=hexA(COL.acc,alpha); ctx.fill(); };
  band(N.p90,N.p10,.10); band(N.p75,N.p25,.16);
  const line=(vals,col,w,dash)=>{ ctx.beginPath();
    vals.forEach((v,i)=>i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)));
    ctx.strokeStyle=col; ctx.lineWidth=w; if(dash)ctx.setLineDash(dash);
    ctx.stroke(); ctx.setLineDash([]); };
  line(N.p50,COL.acc,2);
  line(R.p50,COL.cat[1],1.5,[5,4]);
  /* start marker + axis */
  ctx.fillStyle=COL.muted; ctx.textAlign='center';
  [0.25,0.5,0.75,1].forEach(f=>{ const i=Math.min(td.length-1,Math.round(td.length*f)-1);
    ctx.fillText(td[i]>=360?((td[i]/365).toFixed(1)+'y'):(td[i]+'d'),X(i),H-8); });
  ctx.textAlign='left'; ctx.fillStyle=COL.ink;
  ctx.font='10px ui-monospace,Consolas,monospace';
  ctx.fillText('now $'+fmtN(pj.value_now||pj.start_value),12,16);
  /* legend */
  ctx.fillStyle=COL.acc; ctx.fillRect(12,H-30,9,3);
  ctx.fillStyle=COL.muted; ctx.fillText('nominal median',25,H-25);
  ctx.fillStyle=COL.cat[1]; ctx.fillRect(130,H-30,9,3);
  ctx.fillStyle=COL.muted; ctx.fillText('real (inflation-adj.)',143,H-25);
}
$('pjGo').onclick=async()=>{
  $('pjInfo').innerHTML='<span class="spin"></span>';
  const body={source:$('pjSource').value,horizon_days:+$('pjHorizon').value,
    annual_costs_pct:+$('pjCosts').value||0};
  const infl=$('pjInfl').value.trim();
  if(infl) body.inflation_pct=parseFloat(infl);
  const r=await api('/markets/project/portfolio','POST',body,120000);
  if(!r||r.error){ $('pjInfo').textContent=(r&&r.error)||'failed';
    if(r&&/no positions/.test(r.error||''))
      toast('no holdings found — record portfolio transactions (Terminal → 💼) or pick a sim account','err');
    return; }
  $('pjInfo').textContent=`inflation ${r.inflation.pct}% (${r.inflation.source})`;
  drawBands($('pjCv'),r);
  const horizon=r.t_days[r.t_days.length-1];
  const median=r.nominal.p50[r.nominal.p50.length-1];
  const realm=r.real.p50[r.real.p50.length-1];
  $('pjTiles').innerHTML=[
    ['now','$'+fmtN(r.value_now),''],
    ['median @ '+(horizon>=360?(horizon/365).toFixed(1)+'y':horizon+'d'),'$'+fmtN(median),median>=r.value_now?'up':'dn'],
    ['in real terms','$'+fmtN(realm),realm>=r.value_now?'up':'dn'],
    ['pessimistic p10','$'+fmtN(r.nominal.p10[r.nominal.p10.length-1]),'dn'],
    ['optimistic p90','$'+fmtN(r.nominal.p90[r.nominal.p90.length-1]),'up'],
    ['cash','$'+fmtN(r.cash),''],
  ].map(([l,v,c])=>`<div class="stat"><div class="v num ${c}">${v}</div><div class="l">${l}</div></div>`).join('');
  $('pjAssets').innerHTML='<h4 class="sec">per-asset assumptions</h4>'+
    '<table class="tbl"><tr><th>asset</th><th>value</th><th>drives it</th><th>μ/yr</th><th>σ/yr</th></tr>'+
    (r.per_asset||[]).map(a=>`<tr><td>${esc(a.symbol_key)}</td><td>$${fmtN(a.value)}</td>
      <td>${esc(a.mode||a.error||'')}</td>
      <td class="${(a.mu_annual_pct||0)>=0?'up':'dn'}">${fmtPct(a.mu_annual_pct)}</td>
      <td>${a.sigma_annual_pct??'—'}%</td></tr>`).join('')+'</table>'+
    '<p class="muted" style="font-size:10.5px;margin-top:4px">Tip: link a strategy\'s backtest to an asset '+
    'via the copilot ("project my portfolio using strategy X for BTC") — its μ/σ then come from the '+
    'strategy\'s equity curve, fees included, instead of buy&amp;hold history.</p>';
};
$('opGo').onclick=async()=>{
  $('opOut').innerHTML='<div class="muted"><span class="spin"></span> sampling the frontier…</div>';
  const r=await api('/markets/portfolio/optimize','POST',
    {candidates:'watchlist',source:$('pjSource').value,
     objective:$('opObj').value,max_weight:(+$('opMaxW').value||40)/100,
     fee_bps:+$('opFee').value||10},180000);
  if(!r||r.error){ $('opOut').innerHTML='<div class="dn" style="font-size:11.5px">'+esc((r&&r.error)||'failed')+'</div>'; return; }
  const b=r.best,c=r.current;
  const wRows=Object.keys(b.weights).map((k,i)=>{
    const tw=b.weights[k],cw=(c.weights||{})[k]||0;
    return `<div style="display:flex;align-items:center;gap:6px;font-size:10.5px;margin-top:3px">
      <span style="width:110px;overflow:hidden;text-overflow:ellipsis">${esc(k.split(':').pop())}</span>
      <div class="pbar" style="flex:1;height:9px"><i style="width:${tw}%;background:${COL.cat[i%COL.cat.length]}"></i></div>
      <span class="num" style="width:78px">${cw}% → <b>${tw}%</b></span></div>`;
  }).join('');
  $('opOut').innerHTML=`
    <div style="font-size:11.5px;margin:6px 0">
      ${esc(r.objective)}: <b class="num up">${b.ret_pct}%/yr</b> @ ${b.vol_pct}% vol
      · Sharpe <b class="num">${b.sharpe}</b>
      <span class="muted">(current: ${c.ret_pct}% @ ${c.vol_pct}%)</span></div>
    ${wRows}
    ${r.trades.length?`<h4 class="sec">rebalance plan <span class="muted" style="text-transform:none">· est. fees ${r.est_fee_pct}%</span></h4>
    <table class="tbl">${r.trades.map(tr=>
      `<tr><td class="${tr.action==='buy'?'up':'dn'}">${tr.action}</td>
       <td>${esc(tr.symbol_key)}</td><td>$${fmtN(tr.value)}</td>
       <td class="muted">${tr.weight_from}%→${tr.weight_to}%</td></tr>`).join('')}</table>
    <div class="formRow" style="margin-top:6px">
      <select id="opApplySim">${S.simAccounts.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('')||'<option value="">no sim accounts</option>'}</select>
      <button class="pri" id="opApply">⚖ execute on paper</button>
      <span class="muted" style="font-size:10px">or record manually in the real ledger</span></div>`
    :'<div class="muted" style="font-size:11px;margin-top:5px">already near-optimal — no trades worth the fees</div>'}`;
  const ap=$('opOut').querySelector('#opApply');
  if(ap) ap.onclick=async()=>{
    const aid=$('opOut').querySelector('#opApplySim').value;
    if(!aid) return toast('create a sim account first (◎ Sim tab)','err');
    ap.innerHTML='<span class="spin"></span>';
    const r2=await api('/markets/portfolio/optimize','POST',
      {candidates:'watchlist',source:'sim:'+aid,objective:$('opObj').value,
       max_weight:(+$('opMaxW').value||40)/100,fee_bps:+$('opFee').value||10,
       apply:'sim:'+aid},180000);
    ap.textContent='⚖ execute on paper';
    if(r2&&r2.applied) toast('rebalanced on paper: '+r2.applied.filter(x=>x.ok).length+'/'+r2.applied.length+' orders filled','ok');
    else toast(esc((r2&&r2.error)||'apply failed'),'err');
    loadSim&&loadSim();
  };
};
$('rotGo').onclick=async()=>{
  $('rotOut').innerHTML='<div class="muted"><span class="spin"></span> scoring assets…</div>';
  const mlIds=$('rotMl').checked?S.mlModels.filter(m=>m.status==='ready').slice(0,2).map(m=>m.id):[];
  const r=await api('/markets/rotation/scan','POST',
    {assets:'watchlist',source:$('pjSource').value,
     use_strategies:$('rotStrats').checked,ml_ids:mlIds},180000);
  if(!r||r.error){ $('rotOut').innerHTML='<div class="dn" style="font-size:11.5px">'+esc((r&&r.error)||'failed')+'</div>'; return; }
  const mx=Math.max(...r.ranking.map(x=>Math.abs(x.score)),0.1);
  $('rotOut').innerHTML=
    (r.switches||[]).map(sw=>`
      <div class="condRow" style="border-color:var(--acc)">
        <b>${esc(sw.from.split(':').pop())}</b><span class="op">→</span>
        <b class="up">${esc(sw.to.split(':').pop())}</b>
        <span class="chip on">edge ${sw.edge}</span>
        <span class="muted" style="font-size:10px">fees ~${sw.est_fee_pct}%</span>
        <canvas data-rspark='${esc(JSON.stringify(sw.ratio_spark||[]))}' style="width:70px;height:20px"></canvas>
        <span style="flex:1"></span></div>
      <div class="muted" style="font-size:10px;margin:-3px 0 5px 4px">${esc(sw.reason)} · ratio falling = destination outperforming</div>`).join('')+
    `<table class="tbl" style="margin-top:6px"><tr><th>asset</th><th>score</th><th>mom z</th><th>trend</th><th>strat</th><th>ML</th></tr>${
      r.ranking.map(x=>`<tr${(r.held||[]).includes(x.key)?' style="background:color-mix(in srgb, var(--acc) 6%, transparent)"':''}>
        <td>${esc(x.key.split(':').pop())}${(r.held||[]).includes(x.key)?' <span class="muted">(held)</span>':''}</td>
        <td><div style="display:flex;align-items:center;gap:5px"><div class="pbar" style="width:60px;height:6px"><i style="width:${Math.abs(x.score)/mx*100}%;background:${x.score>=0?'var(--up)':'var(--dn)'}"></i></div><span class="num">${x.score}</span></div></td>
        <td class="num">${x.momentum_z}</td>
        <td>${x.trend>0?'<span class="up">bull</span>':x.trend<0?'<span class="dn">bear</span>':'flat'}</td>
        <td>${x.strat_signal==null?'—':x.strat_signal>0?'<span class="up">▲</span>':x.strat_signal<0?'<span class="dn">▼</span>':'·'}</td>
        <td class="num">${x.ml==null?'—':(x.ml*100).toFixed(0)+'%'}</td></tr>`).join('')}</table>`;
  $('rotOut').querySelectorAll('[data-rspark]').forEach(cv=>{
    try{ const d=JSON.parse(cv.dataset.rspark); if(d.length>2) drawSpark(cv,d,d[d.length-1]>=d[0]); }catch(_){}
  });
};

/* ── 🧠 ML lab inside the studio ── */
(function mountMlBtn(){
  const row=$('btnIndLab')&&$('btnIndLab').parentNode;
  if(!row) return;
  const b=document.createElement('button');
  b.id='btnMlLab'; b.title='ML lab — train predictors, walk-forward test them';
  b.textContent='🧠';
  row.insertBefore(b,$('btnPipe'));
  b.onclick=()=>{ S.curStrat=null; B=null; renderStratList(); renderMlLab(); };
})();
const ML_FEATS=['ret_1','ret_5','ret_10','rsi','macd_hist','stoch_k','bb_pctb','atr_norm','vol_z','ema_ratio','roc','dow'];
async function renderMlLab(){
  await loadModels();
  const m=$('stratMain');
  m.innerHTML=`
    <h2 class="sec">🧠 ML lab</h2>
    <p class="muted" style="max-width:640px">Train predictors over any stored bars, then use them
      honestly: chart them as indicators, gate strategies on them, and judge them with
      <b>walk-forward</b> (out-of-sample) backtests — never by a backtest of a fully-trained model.</p>
    <div class="grid2" style="margin-top:12px">
      <div class="card">
        <h4 class="sec" style="margin-top:0">new predictor</h4>
        <div class="formRow"><label>name</label><input id="mlName" placeholder="e.g. BTC daily direction" style="flex:1"></div>
        <div class="formRow"><label>asset</label><select id="mlAsset" style="flex:1">${
          S.watch.filter(w=>w.exchange!=='macro').map(w=>
            `<option value="${dsId(w.exchange,w.symbol,'1d')}">${esc(w.symbol)} 1d</option>`).join('')}</select></div>
        <div class="formRow"><label>task</label>
          <select id="mlTask"><option value="classify">direction (classify)</option><option value="regress">return (regress)</option></select>
          <label style="min-width:0">model</label>
          <select id="mlKind"><option>gbt</option><option>rf</option><option>logreg</option><option>ridge</option></select>
          <label style="min-width:0">horizon</label><input id="mlHor" type="number" value="5" style="width:50px"></div>
        <div class="fnChips" id="mlFeats">${ML_FEATS.map(f=>
          `<span class="chip on" data-f="${f}">${f}</span>`).join('')}</div>
        <button class="pri" id="mlCreate" style="width:100%">🧠 create &amp; train</button>
        <div id="mlStatus" class="muted" style="font-size:11px;margin-top:5px"></div>
      </div>
      <div class="card"><h4 class="sec" style="margin-top:0">your models</h4><div id="mlList"></div></div>
    </div>`;
  m.querySelectorAll('#mlFeats .chip').forEach(ch=>ch.onclick=()=>ch.classList.toggle('on'));
  const renderModels=()=>{
    m.querySelector('#mlList').innerHTML=S.mlModels.map(x=>{
      const mt=x.metrics||{};
      return `<div class="sCard" style="cursor:default">
        <div class="nm">🧠 ${esc(x.name)}
          <span class="chip ${x.status==='ready'?'on':''}" style="font-size:9px">${esc(x.status)}</span>
          <span class="muted" style="font-size:9.5px">${esc(x.task)}·${esc(x.model_kind)}·h${x.horizon}</span></div>
        <div class="meta">${x.status==='ready'
          ?(x.task==='classify'
            ?`acc ${mt.accuracy??'—'} · edge ${mt.edge??'—'} · signal sharpe ${mt.signal_sharpe??'—'}`
            :`R² ${mt.r2??'—'} · signal sharpe ${mt.signal_sharpe??'—'}`)
          :(mt.error?esc(String(mt.error).slice(0,60)):'…')}</div>
        <div style="display:flex;gap:5px;margin-top:5px;flex-wrap:wrap">
          <button class="ghost" data-mwf="${x.id}" style="font-size:10px">walk-forward</button>
          <button class="ghost" data-mstrat="${x.id}" style="font-size:10px">→ strategy</button>
          <button class="ghost" data-mchart="${x.id}" style="font-size:10px">→ chart</button>
          <button class="ghost" data-mretrain="${x.id}" style="font-size:10px">retrain</button>
          <button class="ghost danger" data-mdel="${x.id}" style="font-size:10px">✕</button></div></div>`;
    }).join('')||'<div class="muted" style="font-size:11.5px">none yet</div>';
    m.querySelectorAll('[data-mwf]').forEach(b=>b.onclick=()=>{
      switchView('run'); $('wfModel').value=b.dataset.mwf; $('wfGo').click(); });
    m.querySelectorAll('[data-mstrat]').forEach(b=>b.onclick=()=>{
      const mm=S.mlModels.find(x=>x.id===b.dataset.mstrat);
      openBuilder(null); B.kind='ml'; B.ml_id=mm.id; B.name=mm.name+' strategy';
      renderBuilder(); });
    m.querySelectorAll('[data-mchart]').forEach(b=>b.onclick=()=>{
      const t=activeTile(); if(!t||!t.key) return toast('open a chart first','err');
      t.inds.push({kind:'ml:'+b.dataset.mchart,params:{},enabled:true});
      tileIndicators(t); switchView('charts');
      toast('model plotted as a chart indicator','ok'); });
    m.querySelectorAll('[data-mretrain]').forEach(b=>b.onclick=async()=>{
      await api('/markets/ml/train','POST',{id:b.dataset.mretrain});
      toast('retraining…'); });
    m.querySelectorAll('[data-mdel]').forEach(b=>b.onclick=async()=>{
      await api('/markets/ml/delete','POST',{id:b.dataset.mdel});
      await loadModels(); renderModels(); });
  };
  renderModels();
  m.querySelector('#mlCreate').onclick=async()=>{
    const feats=[...m.querySelectorAll('#mlFeats .chip.on')].map(ch=>ch.dataset.f);
    const r=await api('/markets/ml/create','POST',
      {name:m.querySelector('#mlName').value||'predictor',
       dataset_id:m.querySelector('#mlAsset').value,
       task:m.querySelector('#mlTask').value,
       model_kind:m.querySelector('#mlKind').value,
       horizon:+m.querySelector('#mlHor').value||5,
       features:feats});
    m.querySelector('#mlStatus').textContent=r&&r.ok
      ?'training in the background — this card updates when done':(r&&r.error||'failed');
    if(r&&r.ok) setTimeout(async()=>{ await loadModels(); renderModels(); },3000);
  };
  /* live refresh on training events */
  window._mlLabRefresh=async()=>{ if($('mlList')){ await loadModels(); renderModels(); } };
}

/* ── screener resilience: poll fallback + last leaderboard on open ── */
let _scrPoll=null;
const _scrGoBase=$('scrGo').onclick;
$('scrGo').onclick=async()=>{
  await _scrGoBase();
  if(SCR.id&&!_scrPoll){
    _scrPoll=setInterval(async()=>{
      if(!SCR.id){ clearInterval(_scrPoll); _scrPoll=null; return; }
      const r=await api('/markets/backtest/batch/status?id='+SCR.id+'&top=1');
      const b=r&&r.batch;
      if(!b){ clearInterval(_scrPoll); _scrPoll=null; return; }
      const bar=$('scrBar'); if(bar) bar.style.width=Math.min(100,(b.done||0)/(b.total||1)*100)+'%';
      const inf=$('scrInfo'); if(inf&&b.status==='running') inf.textContent=`${b.done}/${b.total} backtested`;
      if(b.status!=='running'){ clearInterval(_scrPoll); _scrPoll=null;
        if(b.status==='done') renderScreener();
        else $('scrLive').innerHTML=`<div class="card" style="border-color:var(--dn)">✕ ${esc(b.error||b.status)}</div>`; }
    },3500);
  }
};
async function loadLastLeaderboard(){
  if(RC.curId||SCR.id) return;
  const r=await api('/markets/backtest/batch/status');
  const last=r&&r.last;
  if(last&&last.results&&last.results.length&&$('runMain').querySelector('.empty')){
    SCR.id=last.id;
    const fake={batch:{...last,total:last.results.length,status:'done'}};
    /* reuse the renderer against the stored leaderboard */
    const orig=api;
    try{
      window.api=async(p,...a2)=>p.startsWith('/markets/backtest/batch/status')?fake:orig(p,...a2);
      await renderScreener();
    } finally { window.api=orig; SCR.id=null; }
  }
}
const _svBase=switchView;
switchView=function(n){ _svBase(n);
  if(n==='proj'){ projInit(); loadSim&&loadSim(); }
  if(n==='run') setTimeout(loadLastLeaderboard,400);
};
/* ML training events refresh the lab if open */
const _handleEvR3=handleEvent;
handleEvent=function(ev){
  if(String(ev.type||'')==='markets.ml'&&ev.stage==='trained'&&window._mlLabRefresh)
    window._mlLabRefresh();
  _handleEvR3(ev);
};
/* live overlays follow fresh bars */
const _tileLoadNote=true;
