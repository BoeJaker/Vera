
'use strict';
/* ═══════════════════ Run Center ═══════════════════ */
const RC={ live:{}, curId:null, replay:null, tune:null };

function fillRunSelectors(){
  const sSel=$('rcStrat');
  if(sSel){ const cur=sSel.value;
    sSel.innerHTML=S.strategies.map(s=>
      `<option value="${s.id}">${esc(s.name)}</option>`).join('')||'<option value="">— none saved —</option>';
    if(cur) sSel.value=cur; }
  const aSel=$('rcAsset');
  if(aSel){ const cur=aSel.value;
    aSel.innerHTML=S.watch.filter(w=>w.exchange!=='macro').map(w=>
      `<option value="${w.id}">${esc(w.symbol)} · ${esc(w.exchange)}</option>`).join('');
    if(cur) aSel.value=cur;
    fillTfSel(); }
}
function fillTfSel(){
  const w=S.watch.find(x=>x.id===$('rcAsset').value);
  const tfs=(w&&w.timeframes&&w.timeframes.length)?w.timeframes:['1d','1h'];
  $('rcTf').innerHTML=tfs.map(x=>`<option>${x}</option>`).join('');
}
$('rcAsset')&&($('rcAsset').onchange=fillTfSel);
async function loadEngines(){
  const r=await api('/markets/backtest/engines');
  $('rcEngine').innerHTML=((r&&r.engines)||[{id:'native',available:true}])
    .filter(e=>e.available).map(e=>`<option>${e.id}</option>`).join('');
}
function rcDataset(){
  const w=S.watch.find(x=>x.id===$('rcAsset').value);
  return w?dsId(w.exchange,w.symbol,$('rcTf').value):'';
}
const RUN_STAGES=[['started','queued'],['bars','loading bars'],['signals','evaluating signals'],
  ['simulating','simulating'],['done','done']];
function liveCard(id,name){
  const el=document.createElement('div'); el.className='card'; el.style.margin='10px 0';
  el.dataset.rid=id;
  el.innerHTML=`<b style="font-size:12.5px">▶ ${esc(name||'backtest')}</b>
    <div class="stageList">${RUN_STAGES.map(([k,l])=>
      `<div class="stage" data-st="${k}"><span class="st">○</span>${l}<span class="inf muted" style="margin-left:6px"></span></div>`).join('')}
    </div>`;
  $('liveRuns').prepend(el);
  while($('liveRuns').children.length>3) $('liveRuns').lastChild.remove();
  return el;
}
function rcEvent(ev){
  const st=String(ev.stage||'');
  if(st.startsWith('sweep')||st.startsWith('autotune')) return tuneEvent(ev);
  if(!ev.id) return;
  let card=$('liveRuns').querySelector(`[data-rid="${ev.id}"]`);
  if(!card&&st==='started') card=liveCard(ev.id,ev.name);
  if(!card) return;
  const order=RUN_STAGES.map(x=>x[0]);
  const idx=order.indexOf(st==='error'?'done':st);
  card.querySelectorAll('.stage').forEach((row,i)=>{
    row.classList.toggle('done',i<=idx&&st!=='error');
    row.classList.toggle('run',i===idx+1&&st!=='done'&&st!=='error');
    row.querySelector('.st').textContent=i<=idx?'✓':(i===idx+1?'▹':'○');
  });
  const inf=t=>{const r=card.querySelector(`[data-st="${t}"] .inf`);return r;};
  if(st==='bars') inf('bars').textContent=(ev.bars||0).toLocaleString()+' bars';
  if(st==='signals') inf('signals').textContent=`${ev.entry_count}⇑ ${ev.exit_count}⇓`;
  if(st==='done'){
    inf('done').textContent=ev.stats?fmtPct(ev.stats.total_return_pct)+' · sharpe '+ev.stats.sharpe:'';
    setTimeout(()=>{card.style.opacity='.55';},600);
    loadRunHist(); openResult(ev.id);
  }
  if(st==='error'){ card.querySelector('b').innerHTML='✕ '+esc(ev.error||'failed');
    card.style.borderColor='var(--dn)'; }
}
$('btnRun').onclick=()=>runBacktest(null,null);
function rcWindowBody(){
  /* friendly window picker → start/end ISO (backend takes dates, not bars) */
  const out={limit:100000};
  const v=$('rcRange')?$('rcRange').value:'365';
  if(v==='custom'){
    if($('rcStart').value) out.start=$('rcStart').value+'T00:00:00Z';
    if($('rcEnd').value) out.end=$('rcEnd').value+'T23:59:59Z';
  } else if(v!=='all'){
    out.start=new Date(Date.now()-(+v)*86400000).toISOString().slice(0,10)+'T00:00:00Z';
  }
  return out;
}
async function runBacktest(spec,name){
  const ds=rcDataset();
  if(!ds) return toast('pick an asset (track something first)','err');
  const body={dataset_id:ds,engine:$('rcEngine').value,
    ...rcWindowBody(),name:name||''};
  if(spec) body.spec=spec; else body.strategy_id=$('rcStrat').value;
  if(!body.spec&&!body.strategy_id) return toast('no strategy selected','err');
  $('btnRun').disabled=true;
  const r=await api('/markets/backtest/run','POST',body,300000);
  $('btnRun').disabled=false;
  if(r&&r.error){ toast(esc(r.error),'err'); return; }
  /* events drive the live card; response is the fallback if WS is down */
  if(r&&r.id&&!$('liveRuns').querySelector(`[data-rid="${r.id}"]`)) openResult(r.id);
}

/* ── results dashboard ── */
async function openResult(id){
  RC.curId=id;
  const m=$('runMain');
  m.innerHTML='<div class="empty"><span class="spin"></span> crunching analytics…</div>';
  const [an,full]=await Promise.all([
    api('/markets/backtest/analyze?id='+id),
    api('/markets/backtest/get?id='+id)]);
  if(!an||an.error){ m.innerHTML='<div class="empty">'+esc((an&&an.error)||'failed')+'</div>'; return; }
  const s=an.stats||{}, A=an.analytics||{};
  const good=(v,inv)=>v==null?'':((inv?v<0:v>=0)?'up':'dn');
  const tiles=[
    ['total return',fmtPct(s.total_return_pct),good(s.total_return_pct)],
    ['buy & hold',fmtPct(s.buy_hold_return_pct),good(s.buy_hold_return_pct)],
    ['CAGR',fmtPct(s.cagr_pct),good(s.cagr_pct)],
    ['sharpe',s.sharpe??'—',good(s.sharpe)],
    ['sortino',s.sortino??'—',good(s.sortino)],
    ['max drawdown',fmtPct(s.max_drawdown_pct,false),'dn'],
    ['win rate',s.win_rate_pct!=null?s.win_rate_pct+'%':'—',''],
    ['profit factor',s.profit_factor??'—',good((s.profit_factor||0)-1)],
    ['trades',s.trades??'—',''],
    ['exposure',s.exposure_pct!=null?s.exposure_pct+'%':'—',''],
  ];
  m.innerHTML=`
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <h2 class="sec" style="margin:0">${esc(an.name||'backtest')}</h2>
      <span class="chip">${esc(an.dataset_id||'')}</span>
      <span style="flex:1"></span>
      <button class="pri" id="btnReplay">🎬 replay</button>
      <button id="btnFwd" title="run this strategy LIVE on paper: monitor its signals on fresh bars and auto-trade a sim account">⏩ forward-test</button>
      <button id="btnExportSpec">spec</button>
    </div>
    <div id="rcWindow"></div>
    <div class="statTiles" id="rcTiles">${tiles.map(([l,v,c])=>
      `<div class="stat"><div class="v num ${c}" data-cv="${esc(String(v))}">·</div><div class="l">${l}</div></div>`).join('')}
    </div>
    <div class="grid2">
      <div class="chartBox" style="height:230px"><span class="cap">equity vs buy &amp; hold</span><canvas id="rcEq"></canvas></div>
      <div class="chartBox" style="height:230px"><span class="cap">drawdown</span><canvas id="rcDd"></canvas></div>
    </div>
    <div class="grid2" style="margin-top:10px">
      <div class="card"><h4 class="sec" style="margin-top:0">monthly returns</h4><div class="hmWrap" id="rcHm"></div></div>
      <div>
        <div class="chartBox" style="height:130px"><span class="cap">trade return distribution</span><canvas id="rcHist"></canvas></div>
        <div class="chartBox" style="height:120px;margin-top:8px"><span class="cap">rolling sharpe (${(A.rolling||{}).window_bars||'—'} bars)</span><canvas id="rcRoll"></canvas></div>
      </div>
    </div>
    <div id="rcExtras" class="muted" style="font-size:11.5px;margin:8px 2px"></div>
    <h4 class="sec">trades</h4>
    <div style="max-height:260px;overflow:auto"><table class="tbl" id="rcTrades"></table></div>
    <div id="replayHost"></div>`;
  /* count-up animation on the tiles */
  m.querySelectorAll('[data-cv]').forEach((el,i)=>{
    const target=el.dataset.cv;
    const numMatch=target.match(/-?[\d.]+/);
    if(!numMatch||!ANIM_ON){ el.textContent=target; return; }
    const num=parseFloat(numMatch[0]); const t0=performance.now();
    const tick=now=>{ const p=Math.min(1,(now-t0)/700);
      const eased=1-Math.pow(1-p,3);
      el.textContent=target.replace(numMatch[0],(num*eased).toFixed((numMatch[0].split('.')[1]||'').length));
      if(p<1) requestAnimationFrame(tick); else el.textContent=target; };
    requestAnimationFrame(tick);
  });
  const eq_t=full&&full.equity_t||[], eq=full&&full.equity||[];
  /* B&H overlay from stored bars */
  let bh=null;
  if(an.dataset_id&&eq_t.length){
    const bars=await api('/markets/bars?dataset_id='+encodeURIComponent(an.dataset_id)+'&limit=6000');
    if(bars&&bars.t&&bars.t.length){
      const i0=bars.t.findIndex(tt=>tt>=eq_t[0]);
      if(i0>=0&&bars.c[i0]>0){
        bh={t:bars.t.slice(i0),v:bars.c.slice(i0).map(x=>x/bars.c[i0])};
      }
    }
  }
  drawSeries($('rcEq'),{series:[
    bh?{t:bh.t,v:bh.v,color:COL.muted,width:1.1,dash:[4,3],label:'B&H'}:null,
    {t:eq_t,v:eq,color:COL.acc,width:1.8,fill:true,label:'strategy'},
  ].filter(Boolean),fmt:v=>v.toFixed(2)+'×',animate:true});
  if(A.drawdown) drawSeries($('rcDd'),{series:[
    {t:A.drawdown.t,v:A.drawdown.dd_pct,color:COL.dn,width:1.3,fill:true,base:0}],
    fmt:v=>v.toFixed(1)+'%',animate:true});
  if(A.rolling) drawSeries($('rcRoll'),{series:[
    {t:A.rolling.t,v:A.rolling.sharpe,color:COL.cat[3],width:1.3}],
    hline:0,fmt:v=>v.toFixed(2),animate:true});
  renderHeatmap($('rcHm'),A.monthly||[]);
  renderHistogram($('rcHist'),A.histogram);
  const ex=[];
  if(A.longest_underwater_days!=null) ex.push('longest underwater: <b>'+A.longest_underwater_days+' days</b>');
  if(A.streaks) ex.push(`streaks: <b class="up">${A.streaks.max_wins}W</b> / <b class="dn">${A.streaks.max_losses}L</b>`);
  if(A.exit_reasons) ex.push('exits: '+Object.entries(A.exit_reasons).map(([k,v])=>k+'×'+v).join(', '));
  if(A.avg_trade_bars!=null) ex.push('avg hold: <b>'+A.avg_trade_bars+' bars</b>');
  $('rcExtras').innerHTML=ex.join(' &nbsp;·&nbsp; ');
  const trs=(full&&full.trades)||[];
  $('rcTrades').innerHTML='<tr><th>entry</th><th>exit</th><th>in</th><th>out</th><th>ret</th><th>bars</th><th>why</th></tr>'+
    trs.slice().reverse().map(tr=>
      `<tr><td>${dayFmt(tr.entry_t)}</td><td>${tr.exit_t?dayFmt(tr.exit_t):'—'}</td>
       <td>${fmtPx(tr.entry_px)}</td><td>${fmtPx(tr.exit_px)}</td>
       <td class="${tr.ret_pct>=0?'up':'dn'}">${fmtPct(tr.ret_pct)}</td>
       <td>${tr.bars??''}</td><td>${esc(tr.reason||'')}</td></tr>`).join('');
  m.querySelector('#btnReplay').onclick=()=>openReplay(id);
  m.querySelector('#btnFwd').onclick=()=>forwardTest(an,full);
  if(typeof renderWindowStrip==='function') renderWindowStrip(an,full);
  m.querySelector('#btnExportSpec').onclick=()=>{
    navigator.clipboard&&navigator.clipboard.writeText(JSON.stringify(an.spec||full.spec||{},null,2));
    toast('spec copied to clipboard','ok');
  };
}

/* tiny series painter for result charts */
function drawSeries(cv,opts){
  if(!cv) return;
  const dpr=devicePixelRatio||1;
  const W=cv.clientWidth||cv.parentElement.clientWidth,H=cv.clientHeight||cv.parentElement.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const all=opts.series.flatMap(s=>s.v.filter(v=>v!=null&&isFinite(v)));
  if(!all.length) return;
  let lo=Math.min(...all),hi=Math.max(...all);
  if(opts.hline!=null){lo=Math.min(lo,opts.hline);hi=Math.max(hi,opts.hline);}
  if(opts.base!=null){hi=Math.max(hi,opts.base);}
  if(hi-lo<1e-12){hi+=1;lo-=1;}
  const pad=(hi-lo)*.08; lo-=pad; hi+=pad;
  const t0=Math.min(...opts.series.map(s=>s.t[0])), t1=Math.max(...opts.series.map(s=>s.t[s.t.length-1]));
  const X=t=>8+(t-t0)/((t1-t0)||1)*(W-58), Y=v=>10+(1-(v-lo)/(hi-lo))*(H-30);
  const paint=prog=>{
    ctx.clearRect(0,0,W,H);
    ctx.strokeStyle=COL.grid;
    [lo+(hi-lo)*.25,lo+(hi-lo)*.5,lo+(hi-lo)*.75].forEach(v=>{
      ctx.beginPath();ctx.moveTo(8,Y(v));ctx.lineTo(W-50,Y(v));ctx.stroke();
      ctx.fillStyle=COL.muted;ctx.font='9.5px ui-monospace,Consolas,monospace';ctx.textAlign='left';
      ctx.fillText((opts.fmt||fmtN)(v),W-46,Y(v)+3); });
    if(opts.hline!=null){ ctx.strokeStyle=COL.axis; ctx.setLineDash([3,3]);
      ctx.beginPath();ctx.moveTo(8,Y(opts.hline));ctx.lineTo(W-50,Y(opts.hline));ctx.stroke();ctx.setLineDash([]); }
    opts.series.forEach(s=>{
      const n=Math.floor(s.v.length*prog);
      ctx.beginPath(); let started=false;
      for(let i=0;i<n;i++){ const v=s.v[i];
        if(v==null||!isFinite(v)){started=false;continue;}
        const x=X(s.t[i]),y=Y(v);
        started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true); }
      if(s.fill&&n>1){
        ctx.save();
        ctx.lineTo(X(s.t[n-1]),Y(opts.base??lo)); ctx.lineTo(X(s.t[0]),Y(opts.base??lo)); ctx.closePath();
        const g=ctx.createLinearGradient(0,0,0,H);
        g.addColorStop(0,hexA(s.color,.25)); g.addColorStop(1,hexA(s.color,0));
        ctx.fillStyle=g; ctx.fill(); ctx.restore();
        ctx.beginPath(); started=false;
        for(let i=0;i<n;i++){ const v=s.v[i];
          if(v==null||!isFinite(v)){started=false;continue;}
          const x=X(s.t[i]),y=Y(v);
          started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true); }
      }
      ctx.strokeStyle=s.color; ctx.lineWidth=s.width||1.4;
      if(s.dash)ctx.setLineDash(s.dash);
      ctx.stroke(); ctx.setLineDash([]);
    });
    /* legend for ≥2 series */
    const labeled=opts.series.filter(s=>s.label);
    if(labeled.length>1){ let lx=14;
      ctx.font='10px ui-monospace,Consolas,monospace';
      labeled.forEach(s=>{ ctx.fillStyle=s.color; ctx.fillRect(lx,6,8,8);
        ctx.fillStyle=COL.muted; ctx.textAlign='left'; ctx.fillText(s.label,lx+12,13);
        lx+=20+ctx.measureText(s.label).width; }); }
  };
  if(opts.animate&&ANIM_ON){
    const t0a=performance.now();
    const tick=now=>{ const p=Math.min(1,(now-t0a)/650); paint(1-Math.pow(1-p,2));
      if(p<1) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  } else paint(1);
}
function renderHeatmap(host,monthly){
  if(!host) return;
  if(!monthly.length){ host.innerHTML='<span class="muted">not enough history</span>'; return; }
  const years={};
  monthly.forEach(m=>{ const [y,mo]=m.ym.split('-'); (years[y]=years[y]||{})[+mo]=m.ret_pct; });
  const ys=Object.keys(years).sort();
  const maxAbs=Math.max(3,...monthly.map(m=>Math.abs(m.ret_pct)));
  const cell=v=>{
    if(v==null) return '<div class="hmCell" style="background:transparent"></div>';
    const a=Math.min(1,Math.abs(v)/maxAbs);
    const col=v>=0?hexA(COL.up,.15+a*.75):hexA(COL.dn,.15+a*.75);
    return `<div class="hmCell" style="background:${col}" title="${v}%">${v>=0?'+':''}${v.toFixed(1)}</div>`; };
  host.innerHTML=`<div class="hmGrid" style="grid-template-columns:auto repeat(12,1fr)">`+
    `<div></div>${['J','F','M','A','M','J','J','A','S','O','N','D'].map(m=>
      `<div class="hmY" style="justify-content:center">${m}</div>`).join('')}`+
    ys.map(y=>`<div class="hmY">${y}</div>`+
      Array.from({length:12},(_,i)=>cell(years[y][i+1])).join('')).join('')+`</div>`;
}
function renderHistogram(cv,h){
  if(!cv||!h||!h.counts) return;
  const dpr=devicePixelRatio||1;
  const W=cv.clientWidth,H=cv.clientHeight;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const max=Math.max(...h.counts,1);
  const bw=(W-20)/h.counts.length;
  h.counts.forEach((cnt,i)=>{
    const mid=(h.edges[i]+h.edges[i+1])/2;
    const bh=(H-26)*cnt/max;
    ctx.fillStyle=hexA(mid>=0?COL.up:COL.dn,.75);
    ctx.fillRect(10+i*bw,H-18-bh,Math.max(1,bw-2),bh);
  });
  ctx.fillStyle=COL.muted; ctx.font='9px ui-monospace,Consolas,monospace';
  ctx.textAlign='left'; ctx.fillText(h.edges[0].toFixed(1)+'%',8,H-6);
  ctx.textAlign='right'; ctx.fillText(h.edges[h.edges.length-1].toFixed(1)+'%',W-8,H-6);
  ctx.textAlign='center'; ctx.fillText('0',10+(0-h.edges[0])/(h.edges[h.edges.length-1]-h.edges[0])*(W-20),H-6);
}

/* ── replay — play the backtest through the chart ── */
async function openReplay(id){
  const host=$('replayHost');
  host.innerHTML='<div class="empty"><span class="spin"></span> preparing full-resolution replay…</div>';
  host.scrollIntoView({behavior:'smooth',block:'nearest'});
  const r=await api('/markets/backtest/analyze?id='+id+'&replay=true&limit=20000',
    'GET',null,180000);
  if(!r||r.error||!r.replay){ host.innerHTML='<div class="empty">'+esc((r&&r.error)||r&&r.replay_error||'replay unavailable')+'</div>'; return; }
  const rep=r.replay;
  const bars=await api('/markets/bars?dataset_id='+encodeURIComponent(r.dataset_id)+'&limit=20000');
  if(!bars||!bars.t){ host.innerHTML='<div class="empty">bars unavailable</div>'; return; }
  /* align bars to replay t range */
  const tIdx=new Map(); bars.t.forEach((tt,i)=>tIdx.set(tt,i));
  host.innerHTML=`
    <h4 class="sec">🎬 replay — ${esc(r.name||'')}</h4>
    <div id="replayBar">
      <button class="rbtn" id="rpPlay">▶</button>
      <button id="rpSpeed" title="speed">×4</button>
      <button id="rpNextTrade" title="jump to next trade">⇥ trade</button>
      <input type="range" id="rpScrub" min="1" max="${rep.t.length}" value="1">
      <span class="rpStat" id="rpDate">—</span>
      <span class="rpStat">equity <b class="num" id="rpEq">1.00×</b></span>
      <span class="rpStat">pos <b id="rpPos">flat</b></span>
      <span class="rpStat">trades <b class="num" id="rpTr">0</b></span>
    </div>
    <div class="chartBox" style="height:430px"><div id="rpChart" style="position:absolute;inset:0"></div></div>`;
  const chart=new QChart($('rpChart'),{live:false});
  chart.setBars({t:rep.t,
    o:rep.t.map(tt=>bars.o[tIdx.get(tt)]??null),
    h:rep.t.map(tt=>bars.h[tIdx.get(tt)]??null),
    l:rep.t.map(tt=>bars.l[tIdx.get(tt)]??null),
    c:rep.t.map(tt=>bars.c[tIdx.get(tt)]??null),
    v:rep.t.map(tt=>bars.v[tIdx.get(tt)]??0)},false);
  chart.setPosition(rep.position);
  chart.setSubs([{id:'equity',label:'equity',series:[{name:'eq',color:COL.acc,vals:rep.equity}]}]);
  const markers=[];
  (rep.trades||[]).forEach(tr=>{
    const sh=tr.side==='short';
    markers.push({t:tr.entry_t,kind:sh?'short':'buy'});
    if(tr.exit_t) markers.push({t:tr.exit_t,
      kind:tr.reason==='stop'?'stop':(sh?'cover':'sell')});
  });
  markers.sort((a,b)=>a.t-b.t);
  const R=RC.replay={chart,rep,markers,i:1,playing:false,speed:4,shown:0};
  const span=Math.min(rep.t.length,220);
  const setI=i=>{
    R.i=Math.max(1,Math.min(rep.t.length,Math.round(i)));
    chart.setClip(R.i);
    /* camera follows the playhead */
    const vw=chart.view;
    if(vw&&(R.i>vw.i1-span*0.12||R.i<vw.i0))
      chart.view={i0:Math.max(0,R.i-span*0.82),i1:R.i+span*0.18};
    /* pop-in markers as we pass them */
    while(R.shown<markers.length&&markers[R.shown].t<=rep.t[R.i-1]){
      markers[R.shown]._born=performance.now(); R.shown++; }
    for(let k=R.shown;k<markers.length;k++) delete markers[k]._born;
    chart.setMarkers(markers.slice(0,R.shown));
    chart.invalidate();
    $('rpScrub').value=R.i;
    $('rpDate').textContent=dayFmt(rep.t[R.i-1]);
    $('rpEq').textContent=(rep.equity[R.i-1]||1).toFixed(3)+'×';
    const inpos=rep.position[R.i-1]===1;
    $('rpPos').textContent=inpos?'LONG':'flat';
    $('rpPos').className=inpos?'up':'muted';
    $('rpTr').textContent=String((rep.trades||[]).filter(tr=>tr.exit_t&&tr.exit_t<=rep.t[R.i-1]).length);
  };
  setI(Math.min(60,rep.t.length));
  chart.fitAll(); chart.view={i0:0,i1:span};
  let raf=null,acc=0;
  const step=()=>{
    if(!R.playing) return;
    acc+=R.speed/2;
    if(acc>=1){ const di=Math.floor(acc); acc-=di;
      if(R.i>=rep.t.length){ R.playing=false; $('rpPlay').textContent='↻'; return; }
      setI(R.i+di); }
    raf=requestAnimationFrame(step);
  };
  $('rpPlay').onclick=()=>{
    if(R.i>=rep.t.length){ R.shown=0; setI(1); }
    R.playing=!R.playing;
    $('rpPlay').textContent=R.playing?'⏸':'▶';
    if(R.playing) raf=requestAnimationFrame(step);
  };
  const speeds=[1,2,4,8,16,48];
  $('rpSpeed').onclick=()=>{ R.speed=speeds[(speeds.indexOf(R.speed)+1)%speeds.length];
    $('rpSpeed').textContent='×'+R.speed; };
  $('rpScrub').oninput=e=>{ R.shown=0; markers.forEach(mm=>delete mm._born); setI(+e.target.value); };
  $('rpNextTrade').onclick=()=>{
    const nxt=markers.find(mm=>mm.t>rep.t[R.i-1]);
    if(nxt){ const j=rep.t.findIndex(tt=>tt>=nxt.t); if(j>=0) setI(j+1); }
  };
}

/* ── sweep + autotune ── */
function specNumericPaths(spec){
  const found=[];
  const walk=(node,path)=>{
    if(Array.isArray(node)) node.forEach((v,i)=>walk(v,path?path+'.'+i:String(i)));
    else if(node&&typeof node==='object')
      Object.entries(node).forEach(([k,v])=>{
        const p=path?path+'.'+k:k;
        if(typeof v==='number'&&!['fee_bps','slippage_bps','size_pct'].includes(k))
          found.push({path:p,value:v});
        else if(typeof v==='object') walk(v,p);
      });
  };
  walk(spec,'');
  found.sort((a,b)=>(b.path.includes('.params.')?1:0)-(a.path.includes('.params.')?1:0));
  return found;
}
$('btnSweep').onclick=async()=>{
  const sid=$('rcStrat').value;
  const s=S.strategies.find(x=>x.id===sid);
  if(!s) return toast('pick a saved strategy','err');
  const axes=specNumericPaths(s.spec||{}).slice(0,3);
  if(!axes.length) return toast('no numeric parameters to sweep','err');
  const p=popAt($('btnSweep'),`<h4>Sweep axes (≤3)</h4>`+
    axes.map((a,i)=>`<div class="row">
      <span style="font-size:10px;flex:1" class="num">${esc(a.path)}</span>
      <input style="width:46px" id="swF${i}" value="${Math.max(1,Math.round(a.value*0.5))}">
      <span class="muted">→</span>
      <input style="width:46px" id="swT${i}" value="${Math.round(a.value*1.5)||1}">
      <span class="muted">step</span>
      <input style="width:40px" id="swS${i}" value="${Math.max(1,Math.round(a.value*0.25))||1}"></div>`).join('')+
    `<div class="row"><button class="pri" id="swGo" style="flex:1">⌗ run sweep</button></div>`);
  p.querySelector('#swGo').onclick=async()=>{
    const params=axes.map((a,i)=>({path:a.path,
      from:+p.querySelector('#swF'+i).value,to:+p.querySelector('#swT'+i).value,
      step:+p.querySelector('#swS'+i).value}));
    closePop();
    const r=await api('/markets/backtest/sweep','POST',{dataset_id:rcDataset(),
      strategy_id:sid,params,metric:$('rcMetric').value});
    if(r&&r.ok){ RC.tune={kind:'sweep',id:r.sweep_id,total:r.combos,done:0};
      renderTuneLive(); toast('sweep started — '+r.combos+' combos','ok'); }
    else toast(esc((r&&r.error)||'sweep failed'),'err');
  };
};
$('btnAutotune').onclick=()=>startAutotune();
async function startAutotune(){
  const sid=$('rcStrat').value;
  if(!sid) return toast('pick a saved strategy','err');
  const r=await api('/markets/backtest/autotune','POST',{dataset_id:rcDataset(),
    strategy_id:sid,metric:$('rcMetric').value,rounds:3,per_round:60,
    update_strategy:$('ckAdopt').checked});
  if(r&&r.ok){ RC.tune={kind:'autotune',id:r.autotune_id,total:r.total_est,done:0,axes:r.axes};
    renderTuneLive();
    toast('autotune started on '+r.axes.length+' parameters','ok'); }
  else toast(esc((r&&r.error)||'autotune failed'),'err');
}
function renderTuneLive(){
  const t=RC.tune;
  $('tuneLive').innerHTML=t?`
    <div class="card" style="margin-top:8px">
      <b style="font-size:12px">${t.kind==='sweep'?'⌗ sweep':'✦ autotune'} running</b>
      <div class="pbar busy" style="margin:7px 0"><i id="tuneBar"></i></div>
      <div class="muted" style="font-size:11px" id="tuneInfo">starting…</div>
    </div>`:'';
}
function tuneEvent(ev){
  const st=String(ev.stage||'');
  const t=RC.tune;
  if(!t) return;
  const idMatch=(t.kind==='sweep'&&ev.sweep_id===t.id)||(t.kind==='autotune'&&ev.autotune_id===t.id);
  if(!idMatch) return;
  if(st.endsWith('_progress')){
    t.done=ev.done||t.done;
    const total=ev.total||t.total||1;
    const bar=$('tuneBar'); if(bar) bar.style.width=Math.min(100,t.done/total*100)+'%';
    const inf=$('tuneInfo'); if(inf) inf.textContent=
      `${t.done}/${total} evaluated`+(ev.best_metric!=null?` · best ${ev.metric||''} ${(+ev.best_metric).toFixed(3)}`:'');
  }
  if(st==='autotune_round'){
    const inf=$('tuneInfo'); if(inf) inf.textContent=
      `round ${ev.round} ${ev.improved?'improved ✓':'no gain — widening'}`;
  }
  if(st.endsWith('_done')){ finishTune(); }
  if(st.endsWith('_error')){ $('tuneLive').innerHTML=
    `<div class="card" style="border-color:var(--dn)">✕ ${esc(ev.error||'failed')}</div>`;
    RC.tune=null; }
}
async function finishTune(){
  const t=RC.tune; if(!t) return;
  const r=t.kind==='sweep'
    ?await api('/markets/backtest/sweep/status?id='+t.id+'&top=12')
    :await api('/markets/backtest/autotune/status?id='+t.id);
  RC.tune=null;
  const box=$('tuneLive');
  if(t.kind==='autotune'&&r&&r.autotune){
    const a=r.autotune, best=a.best||{};
    const oos=a.stats_oos;
    const oosHtml=oos&&!oos.error
      ?`<div style="font-size:11.5px;margin:4px 0;padding:6px 8px;border:1px solid var(--line2);border-radius:7px">
          🔒 <b>out-of-sample</b> (unseen ${a.oos_bars||'?'} bars):
          ${a.metric} <b class="num ${((oos[a.metric]||0)>=((a.baseline||{})[a.metric]||0))?'up':'dn'}">${oos[a.metric]??'—'}</b>
          · ret <b class="num ${((oos.total_return_pct||0)>=0)?'up':'dn'}">${fmtPct(oos.total_return_pct)}</b>
          vs B&amp;H ${fmtPct(oos.buy_hold_return_pct)} · ${oos.trades??'—'} trades
          ${((oos[a.metric]??0) < ((best.stats||{})[a.metric]??0)*0.4)?'<span class="chip bear">⚠ overfit risk</span>':'<span class="chip bull">holds up</span>'}
        </div>`
      :(oos&&oos.error?`<div class="muted" style="font-size:10.5px">OOS: ${esc(oos.error)}</div>`:'');
    const sens=(a.sensitivity_data||[]).map(sx=>{
      const vals=(sx.metric||[]).map(v=>v==null?0:v);
      const mx=Math.max(...vals.map(Math.abs),1e-9);
      return `<div style="display:flex;align-items:center;gap:6px;font-size:10px;margin-top:3px">
        <span class="muted" style="width:110px;overflow:hidden;text-overflow:ellipsis">${esc(sx.path.split('.').slice(-2).join('.'))}</span>
        <span style="display:flex;gap:1px;align-items:flex-end;height:18px">${vals.map(v=>
          `<i style="display:inline-block;width:7px;border-radius:1px;background:${v>=0?'var(--up)':'var(--dn)'};height:${Math.max(2,Math.abs(v)/mx*18)}px" title="${v}"></i>`).join('')}</span>
        <span class="muted num">${esc(String(sx.values[0]))}…${esc(String(sx.values[sx.values.length-1]))}</span></div>`;
    }).join('');
    box.innerHTML=`<div class="card" style="margin-top:8px">
      <b>✦ autotune done</b>
      <div style="font-size:11.5px;margin:5px 0">
        in-sample baseline ${a.metric}: <b class="num">${((a.baseline||{})[a.metric]??'—')}</b> →
        best: <b class="num up">${(best.stats||{})[a.metric]??'—'}</b>
        ${a.strategy_updated?'<span class="chip on">params written back</span>':''}</div>
      ${oosHtml}
      <div class="num" style="font-size:10.5px;color:var(--muted)">${
        Object.entries(best.values||{}).map(([k,v])=>k.split('.').pop()+'='+v).join(' · ')}</div>
      ${sens?`<div style="margin-top:5px"><span class="muted" style="font-size:9.5px;text-transform:uppercase;letter-spacing:.5px">parameter sensitivity (metric across ±40%)</span>${sens}</div>`:''}
      ${a.best_backtest_id?`<button class="pri" style="margin-top:6px" onclick="openResult('${a.best_backtest_id}')">open best run →</button>`:''}
      ${!a.strategy_updated&&a.strategy_id&&a.best_spec?`<button style="margin-top:6px" id="tuneAdopt">adopt params</button>`:''}
    </div>`;
    const ad=box.querySelector('#tuneAdopt');
    if(ad) ad.onclick=async()=>{
      const s=S.strategies.find(x=>x.id===a.strategy_id);
      const rr=await api('/markets/strategy/save','POST',
        {name:(s&&s.name)||a.name,spec:a.best_spec,id:a.strategy_id,kind:a.best_spec.kind||'rule'});
      toast(rr&&rr.ok?'strategy updated with tuned params':'failed',rr&&rr.ok?'ok':'err');
      loadStrats();
    };
  } else if(r&&r.sweep){
    const sw=r.sweep;
    box.innerHTML=`<div class="card" style="margin-top:8px"><b>⌗ sweep done</b>
      <table class="tbl" style="margin-top:6px">${(sw.results||[]).slice(0,8).map(row=>
        `<tr><td style="font-size:10px">${esc(Object.values(row.values||{}).join(', '))}</td>
         <td class="num">${row.stats?(row.stats[sw.metric]??'—'):'err'}</td></tr>`).join('')}</table>
      ${sw.best_backtest_id?`<button class="pri" style="margin-top:6px" onclick="openResult('${sw.best_backtest_id}')">open best →</button>`:''}
    </div>`;
  }
  loadRunHist();
}

/* ── history + compare ── */
async function loadRunHist(){
  const r=await api('/markets/backtest/list?limit=40');
  if(r&&r.backtests) S.backtests=r.backtests;
  renderRunHist();
}
function renderRunHist(){
  $('runHist').innerHTML=S.backtests.map(b=>{
    const s=b.stats||{};
    return `<div class="runHistItem ${RC.curId===b.id?'sel':''}" data-id="${b.id}">
      ${S.cmpMode?`<input type="checkbox" data-cmp="${b.id}" ${S.compareSel.includes(b.id)?'checked':''}>`:''}
      <div style="flex:1;min-width:0">
        <div style="font-weight:600;overflow:hidden;text-overflow:ellipsis">${esc(b.name||'backtest')}</div>
        <div class="muted" style="font-size:10px">${esc((b.dataset_id||'').replace('mkt.',''))} · ${esc((b.created_at||'').slice(0,16))}</div>
      </div>
      <span class="num ${s.total_return_pct>=0?'up':'dn'}" style="font-size:11.5px">${fmtPct(s.total_return_pct)}</span>`+
      `</div>`;
  }).join('')||'<div class="muted" style="font-size:11px">none yet</div>';
  $('runHist').querySelectorAll('.runHistItem').forEach(el=>el.onclick=e=>{
    if(e.target.dataset.cmp){
      const id=e.target.dataset.cmp;
      if(e.target.checked){ if(S.compareSel.length>=4){e.target.checked=false;return;}
        S.compareSel.push(id); }
      else S.compareSel=S.compareSel.filter(x=>x!==id);
      if(S.compareSel.length>=2) renderCompare();
      return;
    }
    openResult(el.dataset.id); renderRunHist();
  });
}
$('btnCompare').onclick=()=>{ S.cmpMode=!S.cmpMode; S.compareSel=[]; renderRunHist(); };
async function renderCompare(){
  const m=$('runMain');
  m.innerHTML=`<h2 class="sec">⇄ compare runs</h2>
    <div class="chartBox" style="height:340px"><span class="cap">equity, normalised</span><canvas id="cmpCv"></canvas></div>
    <div class="statTiles" id="cmpTiles"></div>`;
  const series=[],tiles=[];
  for(const [k,id] of S.compareSel.entries()){
    const r=await api('/markets/backtest/get?id='+id);
    if(r&&r.equity){ series.push({t:r.equity_t,v:r.equity,color:COL.cat[k%COL.cat.length],
      width:1.6,label:(r.name||id).slice(0,18)});
      const s=r.stats||{};
      tiles.push(`<div class="stat"><div class="v num" style="color:${COL.cat[k%COL.cat.length]}">${fmtPct(s.total_return_pct)}</div>
        <div class="l">${esc((r.name||'').slice(0,20))} · sh ${s.sharpe??'—'} · dd ${s.max_drawdown_pct??'—'}%</div></div>`); }
  }
  drawSeries($('cmpCv'),{series,fmt:v=>v.toFixed(2)+'×',animate:true});
  $('cmpTiles').innerHTML=tiles.join('');
}
