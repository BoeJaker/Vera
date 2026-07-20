
'use strict';
/* ═══ round 8: mono glyphs · new-tile opens · drag window · accordion LHM ·
   versions · deep tuning · trader director · workspaces · watchlist ═══ */

/* ── monochrome glyphs: colour emoji → text glyphs, applied live everywhere ── */
const EMAP={'💼':'▦','🔔':'◭','🤖':'⌬','🎯':'⊕','📌':'↧','🗄':'⊟','💾':'⎘',
  '🛰':'⌖','⏩':'≫','🎬':'▷','🧠':'Ψ','📊':'▥','🩻':'⌗','🔄':'⇄','🧬':'∞',
  '📰':'¶','🗣':'◍','🏆':'♛','🕯':'▮','🌗':'◐','⚡':'↯','🪟':'▣','🔒':'⊘',
  '📣':'◅','📄':'≡','🜲':'⌭','⚠️':'△','⚠':'△','❌':'✕','✅':'✓','🔥':'↯',
  '⚗️':'⚗','⚖️':'⚖','⚙️':'⚙','▶️':'▶','⏹️':'⏹','⏸️':'⏸','☁':'≋','✚':'＋'};
const EMOJI_RE=new RegExp('['+Object.keys(EMAP).filter(k=>k.length<=2).join('')+']|\\uFE0F','gu');
function deEmojiNode(node){
  if(node.nodeType===3){
    const v=node.nodeValue;
    if(v){
      EMOJI_RE.lastIndex=0;
      const nv=v.replace(EMOJI_RE,ch=>ch==='️'?'':(EMAP[ch]??ch));
      /* CRITICAL: only write when the text actually changed. Some glyphs in
         the class map to themselves (⚗ ▶ ⚙ …, present via their emoji-variant
         keys) — writing an identical nodeValue still fires a characterData
         mutation, and observer→rewrite→observer froze the whole UI. */
      if(nv!==v) node.nodeValue=nv;
    }
    return;
  }
  if(node.nodeType!==1||node.tagName==='SCRIPT'||node.tagName==='STYLE'||
     node.tagName==='CANVAS'||node.tagName==='TEXTAREA'||node.tagName==='INPUT') return;
  for(const c of node.childNodes) deEmojiNode(c);
}
try{ deEmojiNode(document.body); }catch(_){}
try{
  new MutationObserver(muts=>{
    try{
      for(const mu of muts){
        if(mu.type==='characterData') deEmojiNode(mu.target);
        else mu.addedNodes.forEach(deEmojiNode);
      }
    }catch(_){}
  }).observe(document.body,{childList:true,subtree:true,characterData:true});
}catch(_){}

/* ── open-in-new-tile (search / drill / strip / metrics never overwrite) ── */
function openInNewTile(key,tf){
  const empty=Object.values(TILES).find(t=>t.kind!=='ig'&&!t.key);
  const t=empty||addTile();
  tileSetAsset(t,key,tf||null);
  S.activeTile=t.id;
  return t;
}
$('rcRange').onchange=()=>{ $('rcCustomRow').style.display=
  $('rcRange').value==='custom'?'flex':'none'; };

/* ── 🪟 drag the backtest window on a price strip → live re-run ── */
async function renderWindowStrip(an,full){
  const host=$('rcWindow'); if(!host||!an.dataset_id) return;
  const bars=await api('/markets/bars?dataset_id='+encodeURIComponent(an.dataset_id)+'&limit=100000');
  if(!bars||!bars.t||bars.t.length<50) return;
  host.innerHTML=`<div class="chartBox" style="height:74px;margin:8px 0;cursor:crosshair">
      <span class="cap">backtest window — drag the edges (full history shown)</span>
      <canvas id="rcWinCv"></canvas></div>
    <div class="muted" style="font-size:10.5px;margin:-4px 0 6px" id="rcWinInfo"></div>`;
  const cv=$('rcWinCv'), dpr=devicePixelRatio||1;
  const W=cv.clientWidth||host.clientWidth,H=cv.clientHeight||74;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const t=bars.t,c=bars.c,n=t.length;
  const lo=Math.min(...c),hi=Math.max(...c);
  const X=i=>(i/(n-1))*W, I=x=>Math.max(0,Math.min(n-1,Math.round(x/W*(n-1))));
  /* current window from the stored run */
  let i0=0,i1=n-1;
  const eq_t=(full&&full.equity_t)||[];
  if(eq_t.length){ i0=Math.max(0,t.findIndex(tt=>tt>=eq_t[0]));
    const j=t.findIndex(tt=>tt>eq_t[eq_t.length-1]); i1=j<0?n-1:Math.max(i0+10,j-1); }
  const spec=an.spec||((full||{}).spec)||{};
  let busy=false,queued=false;
  const paint=()=>{
    ctx.clearRect(0,0,W,H);
    ctx.beginPath();
    for(let i=0;i<n;i+=Math.max(1,Math.floor(n/W))){
      const x=X(i),y=6+(1-(c[i]-lo)/((hi-lo)||1))*(H-14);
      i===0?ctx.moveTo(x,y):ctx.lineTo(x,y); }
    ctx.strokeStyle=COL.muted; ctx.lineWidth=1; ctx.stroke();
    ctx.fillStyle=hexA(COL.acc,.14);
    ctx.fillRect(X(i0),0,X(i1)-X(i0),H);
    [[i0,'left'],[i1,'right']].forEach(([i])=>{
      ctx.fillStyle=COL.acc; ctx.fillRect(X(i)-2,0,4,H); });
    $('rcWinInfo').textContent=dayFmt(t[i0])+' → '+dayFmt(t[i1])+
      ' · '+(i1-i0+1).toLocaleString()+' bars — release to re-run';
  };
  paint();
  const rerun=debounce(async()=>{
    if(busy){queued=true;return;}
    busy=true;
    $('rcWinInfo').innerHTML='<span class="spin"></span> re-running window…';
    const r=await api('/markets/backtest/run','POST',
      {dataset_id:an.dataset_id,spec,limit:100000,save:false,
       start:new Date(t[i0]*1000).toISOString(),
       end:new Date(t[i1]*1000).toISOString()},300000);
    busy=false;
    if(queued){queued=false;rerun();return;}
    if(r&&r.stats) updateResultLite(r);
    $('rcWinInfo').textContent=dayFmt(t[i0])+' → '+dayFmt(t[i1])+
      (r&&r.stats?' · '+fmtPct(r.stats.total_return_pct)+' in window':' · run failed');
  },450);
  let drag=null;
  cv.addEventListener('mousedown',e=>{
    const x=e.offsetX;
    drag=Math.abs(x-X(i0))<Math.abs(x-X(i1))?'l':'r';
    e.preventDefault(); e.stopPropagation();
  });
  window.addEventListener('mousemove',e=>{
    if(!drag||!cv.isConnected){ drag=null; return; }
    const rect=cv.getBoundingClientRect();
    const i=I(e.clientX-rect.left);
    if(drag==='l') i0=Math.min(i,i1-10); else i1=Math.max(i,i0+10);
    paint();
  });
  window.addEventListener('mouseup',()=>{
    if(drag){ drag=null; if(cv.isConnected) rerun(); } });
}
function updateResultLite(res){
  const s=res.stats||{};
  const good=(v,inv)=>v==null?'':((inv?v<0:v>=0)?'up':'dn');
  const vals=[[fmtPct(s.total_return_pct),good(s.total_return_pct)],
    [fmtPct(s.buy_hold_return_pct),good(s.buy_hold_return_pct)],
    [fmtPct(s.cagr_pct),good(s.cagr_pct)],[s.sharpe??'—',good(s.sharpe)],
    [s.sortino??'—',good(s.sortino)],[fmtPct(s.max_drawdown_pct,false),'dn'],
    [s.win_rate_pct!=null?s.win_rate_pct+'%':'—',''],
    [s.profit_factor??'—',good((s.profit_factor||0)-1)],
    [s.trades??'—',''],[s.exposure_pct!=null?s.exposure_pct+'%':'—','']];
  const tiles=$('rcTiles')?$('rcTiles').querySelectorAll('.stat .v'):[];
  tiles.forEach((el,i)=>{ if(vals[i]){ el.textContent=vals[i][0];
    el.className='v num '+vals[i][1]; }});
  if(res.equity_t&&res.equity)
    drawSeries($('rcEq'),{series:[{t:res.equity_t,v:res.equity,color:COL.acc,
      width:1.8,fill:true,label:'window'}],fmt:v=>v.toFixed(2)+'×',animate:true});
}

/* ── run-center LHM → clean accordion ── */
(function accordionize(){
  const left=$('runLeft'); if(!left) return;
  const kids=[...left.childNodes];
  left.innerHTML='';
  let cur=null,first=true;
  const openSet=new Set(['New backtest','History','Live paper runs']);
  const mk=(title)=>{
    const d=document.createElement('details');
    d.style.cssText='border:1px solid var(--line);border-radius:10px;margin-bottom:8px;'+
      'padding:2px 10px 8px;background:var(--bg1)';
    const sum=document.createElement('summary');
    sum.style.cssText='cursor:pointer;font-size:11px;text-transform:uppercase;'+
      'letter-spacing:.7px;color:var(--muted);padding:7px 0;user-select:none;list-style:none';
    sum.textContent=title;
    d.appendChild(sum);
    if([...openSet].some(o=>title.includes(o))) d.open=true;
    left.appendChild(d);
    return d;
  };
  kids.forEach(k=>{
    if(k.nodeType===1&&k.tagName==='H4'){
      cur=mk(k.textContent.trim());
      /* keep any buttons that lived in the header */
      [...k.querySelectorAll('button')].forEach(b=>{
        b.style.float='none'; cur.firstChild.appendChild(b); });
    } else if(cur){ cur.appendChild(k); }
    else if(k.nodeType===1||String(k.textContent||'').trim()){
      (cur=mk('New backtest')).appendChild(k);
    }
  });
})();

/* ── ⟲ strategy versions (auto-snapshotted — restore any setup) ── */
const _renderBuilderR8=renderBuilder;
renderBuilder=function(){
  _renderBuilderR8();
  if(!B||!B.id) return;
  const save=$('stratMain')&&$('stratMain').querySelector('#bSave');
  if(!save||save.parentNode.querySelector('#bVers')) return;
  const b=document.createElement('button');
  b.id='bVers'; b.textContent='⟲ versions';
  b.title='every overwrite (autotune adopt, optimise, manual edit) snapshots the previous setup';
  save.parentNode.insertBefore(b,save.nextSibling);
  b.onclick=async e=>{
    const r=await api('/markets/strategy/versions?id='+B.id);
    const vs=(r&&r.versions)||[];
    const p=popAt(e.currentTarget,'<h4>⟲ setup versions</h4>'+
      (vs.map(v=>`<div class="row"><span class="muted num" style="font-size:10px">${esc(String(v.saved_at||'').slice(0,16).replace('T',' '))}</span>
        <span style="font-size:11px">${esc(v.name||'')}</span><span class="chip" style="font-size:9px">${esc(v.kind||'rule')}</span>
        <span style="flex:1"></span><button class="ghost" data-rev="${v.index}">restore</button></div>`).join('')
       ||'<div class="row muted">no snapshots yet — they appear when the spec is overwritten</div>'));
    p.querySelectorAll('[data-rev]').forEach(bt=>bt.onclick=async()=>{
      const rr=await api('/markets/strategy/revert','POST',{id:B.id,index:+bt.dataset.rev});
      closePop();
      if(rr&&rr.ok){ toast('setup restored (the replaced one was snapshotted too)','ok');
        await loadStrats();
        const s=S.strategies.find(x=>x.id===B.id); if(s) openBuilder(s); }
      else toast(esc((rr&&rr.error)||'restore failed'),'err');
    });
  };
};

/* ── deeper tuning controls ── */
(function tuneControls(){
  const met=$('rcMetric');
  if(met&&![...met.options].some(o=>o.value==='blend')){
    const o=document.createElement('option');
    o.value='blend'; o.textContent='⚖ blend (sharpe+calmar+PF)';
    met.insertBefore(o,met.firstChild); met.value='blend';
  }
  const adopt=$('ckAdopt'); if(!adopt) return;
  const row=document.createElement('div');
  row.innerHTML=`<div class="formRow" style="flex-wrap:wrap;font-size:10.5px">
    <label>rounds</label><input id="atRounds" type="number" value="4" style="width:44px">
    <label style="min-width:0">evals/rd</label><input id="atPer" type="number" value="80" style="width:52px">
    <label style="min-width:0">OOS %</label><input id="atOos" type="number" value="25" style="width:44px">
    <label style="min-width:0">min trades</label><input id="atMin" type="number" value="6" style="width:44px"></div>
    <p class="muted" style="font-size:9.5px;margin:2px 0 0">search never sees the OOS tail; finalists are
    re-picked on a validation slice; the previous setup is auto-snapshotted (⟲) before any adopt.</p>`;
  adopt.closest('label').after(row);
})();
const _startAutotuneR8=startAutotune;
startAutotune=async function(){
  const sid=$('rcStrat').value;
  if(!sid) return toast('pick a saved strategy','err');
  const r=await api('/markets/backtest/autotune','POST',{dataset_id:rcDataset(),
    strategy_id:sid,metric:$('rcMetric').value,
    rounds:+($('atRounds')&&$('atRounds').value)||4,
    per_round:+($('atPer')&&$('atPer').value)||80,
    oos_split:(+($('atOos')&&$('atOos').value)||25)/100,
    min_trades:+($('atMin')&&$('atMin').value)||6,
    explore:12,update_strategy:$('ckAdopt').checked,...rcWindowBody()});
  if(r&&r.ok){ RC.tune={kind:'autotune',id:r.autotune_id,total:r.total_est,done:0,axes:r.axes};
    renderTuneLive();
    toast('✦ deep autotune on '+r.axes.length+' parameters (OOS-guarded)','ok'); }
  else toast(esc((r&&r.error)||'autotune failed'),'err');
};

/* ── ⌭ trader director card ── */
(function mountTrader(){
  const left=$('runLeft'); if(!left) return;
  const d=document.createElement('details');
  d.open=false;
  d.style.cssText='border:1px solid var(--acc);border-radius:10px;margin-bottom:8px;padding:2px 10px 8px;background:var(--bg1)';
  d.innerHTML=`<summary style="cursor:pointer;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--acc);padding:7px 0;list-style:none">⌭ Trader director</summary>
    <div id="tdStatus" class="muted" style="font-size:11px">…</div>
    <div class="formRow"><button class="pri" id="tdToggle">start</button>
      <button id="tdTick">tick now</button>
      <select id="tdMode" style="width:70px"><option value="sim">sim</option><option value="real">real</option></select>
      <select id="tdSim" style="flex:1"></select></div>
    <div class="formRow" style="font-size:10.5px"><label>every</label>
      <input id="tdInt" type="number" value="60" style="width:50px"><span class="muted">min</span>
      <label style="min-width:0">per trade</label><input id="tdPct" type="number" value="15" style="width:44px"><span class="muted">%</span>
      <label style="min-width:0">min metric</label><input id="tdMin" type="number" step="0.1" value="0.5" style="width:48px"></div>
    <label class="chip" style="margin:3px 0"><input type="checkbox" id="tdSteer"> Ψ LLM steering (bounded — never trades)</label>
    <label class="chip" style="margin:3px 0" id="tdAutoRow"><input type="checkbox" id="tdAutolog"> real mode: auto-record signals in the ledger</label>
    <p class="muted" style="font-size:9.5px;margin:3px 0">Deterministic core: monitors the market, rolls a
      strategies×assets results grid (incl. layered composites), and executes fresh signals from proven
      cells. Sim mode sees ONLY its sim account; real mode never touches sim.</p>
    <div id="tdGrid" style="font-size:10px"></div>
    <div id="tdLog" style="font-size:10px;max-height:150px;overflow:auto"></div>`;
  left.insertBefore(d,left.firstChild);
  $('tdToggle').onclick=async()=>{
    const on=$('tdToggle').dataset.on==='1';
    await api('/markets/trader/config/set','POST',{enabled:!on,
      mode:$('tdMode').value,sim_account_id:$('tdSim').value,
      interval_min:+$('tdInt').value||60,per_trade_pct:+$('tdPct').value||15,
      min_metric:+$('tdMin').value||0.5,llm_steer:$('tdSteer').checked,
      real_autolog:$('tdAutolog').checked});
    toast(!on?'⌭ trader director running — it monitors, backtests and trades on its own':'trader stopped','ok');
    tdLoad();
  };
  $('tdTick').onclick=async()=>{
    $('tdTick').innerHTML='<span class="spin"></span>';
    const r=await api('/markets/trader/tick','POST',{},600000);
    $('tdTick').textContent='tick now';
    toast(r&&r.ok?`tick: ${r.grid_cells} grid cells · ${r.signals} signals · ${(r.trades||[]).length} trades`
      :esc((r&&r.error)||r&&r.skipped||'failed'),r&&r.ok?'ok':'err');
    tdLoad();
  };
  ['tdMode','tdSim','tdInt','tdPct','tdMin','tdSteer','tdAutolog'].forEach(id=>{
    const el=$(id); if(el) el.onchange=async()=>{
      await api('/markets/trader/config/set','POST',{
        mode:$('tdMode').value,sim_account_id:$('tdSim').value,
        interval_min:+$('tdInt').value||60,per_trade_pct:+$('tdPct').value||15,
        min_metric:+$('tdMin').value||0.5,llm_steer:$('tdSteer').checked,
        real_autolog:$('tdAutolog').checked});
      $('tdAutoRow').style.display=$('tdMode').value==='real'?'':'none';
    };
  });
})();
async function tdLoad(){
  const r=await api('/markets/trader/status');
  if(!r||!r.config){ $('tdStatus').textContent='trader unavailable'; return; }
  const c=r.config;
  await loadSim();
  $('tdSim').innerHTML=S.simAccounts.map(a=>
    `<option value="${esc(a.id)}"${c.sim_account_id===a.id?' selected':''}>◎ ${esc(a.name)}</option>`).join('')
    ||'<option value="">— create a sim account —</option>';
  $('tdMode').value=c.mode||'sim';
  $('tdInt').value=c.interval_min; $('tdPct').value=c.per_trade_pct;
  $('tdMin').value=c.min_metric; $('tdSteer').checked=!!c.llm_steer;
  $('tdAutolog').checked=!!c.real_autolog;
  $('tdAutoRow').style.display=c.mode==='real'?'':'none';
  $('tdToggle').dataset.on=c.enabled?'1':'0';
  $('tdToggle').textContent=c.enabled?'stop':'start';
  $('tdToggle').classList.toggle('pri',!c.enabled);
  $('tdStatus').innerHTML=(c.enabled
    ?`<span class="up">● ${esc(c.mode)} mode</span> — tick #${c.tick_count||0}, last ${esc(String(c.last_run||'never').slice(5,16).replace('T',' '))}`
    :'<span class="muted">○ stopped</span>')+
    (c.last_brief?`<br><span class="muted">${esc(c.last_brief)}</span>`:'');
  const cells=Object.values((r.grid||{}).cells||{})
    .filter(x=>x&&x.metric!=null).sort((a,b)=>b.metric-a.metric).slice(0,5);
  $('tdGrid').innerHTML=cells.length
    ?'<span class="muted" style="text-transform:uppercase;font-size:9px;letter-spacing:.5px">results grid — best cells</span>'+
     cells.map(x=>`<div style="display:flex;gap:5px;margin-top:2px">
       <span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(x.strategy)} × ${esc(String(x.asset).split(':').pop())}</span>
       <b class="num">${x.metric}</b><span class="muted num">${fmtPct(x.ret_pct)}</span></div>`).join('')
    :'';
  $('tdLog').innerHTML=((r.log)||[]).slice(0,10).map(l=>
    `<div style="margin-top:2px"><span class="muted num">${esc(String(l.ts||'').slice(5,16).replace('T',' '))}</span>
     ${esc(l.msg||'')}</div>`).join('');
}
const _svBase8=switchView;
switchView=function(n){ _svBase8(n); if(n==='run') setTimeout(tdLoad,900); };
const _handleEvR8=handleEvent;
handleEvent=function(ev){
  if(String(ev.type||'')==='markets.trader'&&S.view==='run') setTimeout(tdLoad,400);
  _handleEvR8(ev);
};

/* ── workspaces: layouts + ACCOUNT-BOUND workspaces + watchlist manager ── */
(function mountWorkspace(){
  const sel=document.createElement('select');
  sel.id='wsSel'; sel.style.cssText='max-width:170px;font-size:11px';
  $('topbar').insertBefore(sel,$('btnBell'));
  async function fill(){
    const [lr]=await Promise.all([api('/markets/layout/list'),loadSim()]);
    const layouts=(lr&&lr.layouts)||[];
    S._layouts=layouts;
    sel.innerHTML='<option value="">▤ workspace…</option>'+
      '<optgroup label="saved">'+layouts.map(l=>
        `<option value="l:${esc(l.key)}">${esc(l.name)}${(l.data||{}).account?' ⌖':''}</option>`).join('')+'</optgroup>'+
      '<optgroup label="accounts">'+S.simAccounts.map(a=>
        `<option value="a:sim:${esc(a.id)}">◎ ${esc(a.name)}</option>`).join('')+
      '<option value="a:portfolio">▦ real portfolio</option></optgroup>'+
      '<option value="__save">⎘ save current as workspace…</option>';
  }
  fill(); setInterval(fill,90000);
  sel.onchange=async()=>{
    const v=sel.value; sel.value='';
    if(!v) return;
    if(v==='__save'){
      const name=prompt('workspace name'+(S.wsAccount?' (bound to '+S.wsAccount+')':''));
      if(!name) return;
      const data=layoutData(); if(S.wsAccount) data.account=S.wsAccount;
      const r=await api('/markets/layout/save','POST',{name,data});
      toast(r&&r.ok?'workspace saved':'save failed',r&&r.ok?'ok':'err'); fill(); return;
    }
    if(v.startsWith('l:')){
      const l=(S._layouts||[]).find(x=>x.key===v.slice(2));
      if(l&&l.data){ S.wsAccount=(l.data||{}).account||null;
        switchView('charts'); await applyLayout(l.data); }
      return;
    }
    if(v.startsWith('a:')){
      const key=v.slice(2);
      S.wsAccount=key;
      const bound=(S._layouts||[]).find(x=>((x.data||{}).account)===key);
      switchView('charts');
      if(bound){ await applyLayout(bound.data);
        toast('workspace for '+esc(key)+' loaded','ok'); return; }
      /* no bound workspace — generate one from the account's holdings */
      let keys=[];
      if(key==='portfolio'){
        const r=await api('/markets/portfolio/positions');
        keys=((r&&r.positions)||[]).filter(p=>(p.qty||0)>0)
          .sort((a,b)=>(b.market_value||0)-(a.market_value||0))
          .map(p=>p.symbol_key);
      } else {
        const a=S.simAccounts.find(x=>'sim:'+x.id===key);
        keys=((a&&a.positions)||[]).filter(p=>(p.qty||0)>0)
          .sort((x,y)=>(y.value||0)-(x.value||0)).map(p=>p.symbol_key);
      }
      Object.keys(TILES).forEach(removeTile);
      if(!keys.length){ addTile(S.watch[0]?S.watch[0].id:null);
        toast('account has no positions — blank workspace; ⎘ save to bind one','err'); return; }
      for(const k of keys.slice(0,6)) openInNewTile(k);
      toast('workspace generated from holdings — ⎘ save to keep it bound to this account','ok');
    }
  };
})();

/* ── ★ watchlist manager + max-history fetching ── */
(function mountWatchBtn(){
  const bar=document.getElementById('chartsBar'); if(!bar) return;
  const b=document.createElement('button');
  b.id='btnWatchMgr'; b.textContent='★ watchlist';
  bar.insertBefore(b,bar.firstChild);
  b.onclick=e=>openWatchMgr(e.currentTarget);
})();
async function openWatchMgr(anchor){
  await loadWatch();
  const rows=S.watch.filter(w=>!['macro','dyn'].includes(w.exchange));
  const p=popAt(anchor||document.body,`<h4>★ watchlist
      <span style="float:right"><button class="ghost" id="wmAll" style="font-size:10px"
        title="ensure 1d+1w on every asset and backfill EVERYTHING available">⇊ max history, all assets</button></span></h4>
    <div class="row muted" style="font-size:10.5px">add assets via the top search — they land here.
      ⇊ fetches every daily + weekly bar the source has (crypto from 2013, stocks from listing).</div>
    <div style="max-height:340px;overflow:auto">${rows.map(w=>{
      const cnt=Object.entries(w.counts||{}).map(([tf,n])=>tf+':'+(n>=1000?(n/1000).toFixed(1)+'k':n)).join(' ');
      return `<div class="row" style="font-size:11px">
        <b style="width:96px;overflow:hidden;text-overflow:ellipsis">${esc(w.symbol)}</b>
        <span class="muted" style="font-size:9.5px;width:52px">${esc(w.exchange)}</span>
        <span class="muted num" style="font-size:9px;flex:1">${esc(cnt)}</span>
        <label class="chip" style="font-size:9px" title="auto-refresh"><input type="checkbox" data-wau="${esc(w.id)}" ${w.auto_update?'checked':''}>auto</label>
        <button class="ghost" data-wfull="${esc(w.id)}" title="fetch ALL history, 1d+1w" style="font-size:10px">⇊</button>
        <button class="ghost danger" data-wdel="${esc(w.id)}" style="font-size:10px">✕</button></div>`;
    }).join('')||'<div class="row muted">empty — search an asset above to track it</div>'}</div>`);
  const doFull=async(w)=>{
    const tfs=[...new Set((w.timeframes||['1d']).concat(['1d','1w']))];
    await api('/markets/watchlist/config','POST',
      {exchange:w.exchange,symbol:w.symbol,timeframes:tfs,
       auto_update:w.auto_update!==false,update_interval_min:w.update_interval_min||60});
    return api('/markets/fetch','POST',
      {exchange:w.exchange,symbol:w.symbol,timeframes:tfs,full:true});
  };
  p.querySelectorAll('[data-wfull]').forEach(b2=>b2.onclick=async()=>{
    const w=rows.find(x=>x.id===b2.dataset.wfull); if(!w) return;
    b2.innerHTML='<span class="spin"></span>';
    const r=await doFull(w); b2.textContent='⇊';
    toast(r&&r.ok?'⇊ full 1d+1w backfill running for '+esc(w.symbol):'failed',r&&r.ok?'ok':'err');
  });
  p.querySelector('#wmAll').onclick=async()=>{
    closePop();
    toast('⇊ fetching maximum history (1d+1w) for '+rows.length+' assets — watch the toasts');
    for(const w of rows){ await doFull(w); await new Promise(r2=>setTimeout(r2,700)); }
    toast('all backfills queued — bars land as jobs finish','ok');
  };
  p.querySelectorAll('[data-wau]').forEach(cb=>cb.onchange=async()=>{
    const w=rows.find(x=>x.id===cb.dataset.wau); if(!w) return;
    await api('/markets/watchlist/config','POST',
      {exchange:w.exchange,symbol:w.symbol,auto_update:cb.checked,
       update_interval_min:w.update_interval_min||60,timeframes:w.timeframes});
  });
  p.querySelectorAll('[data-wdel]').forEach(b2=>b2.onclick=async()=>{
    const w=rows.find(x=>x.id===b2.dataset.wdel); if(!w) return;
    if(!confirm('untrack '+w.symbol+'? (stored bars are kept)')) return;
    await api('/markets/watchlist/remove','POST',{exchange:w.exchange,symbol:w.symbol});
    closePop(); loadWatch();
  });
}
