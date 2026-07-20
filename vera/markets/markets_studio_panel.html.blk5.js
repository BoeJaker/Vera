
'use strict';
/* ═══ pipeline builder cross-link (merged-tab sibling iframe) ═══ */
$('btnPipe').onclick=()=>{
  try{
    localStorage.setItem('mkt_open_pipe', String(Date.now()));
    const radio=window.parent&&window.parent.document&&
      window.parent.document.getElementById('msub-terminal');
    if(radio){ radio.checked=true; toast('opening the node-graph pipeline builder in Terminal…'); }
    else toast('pipeline builder lives in the 🖥 Terminal sub-tab');
  }catch(_){ toast('switch to the 🖥 Terminal sub-tab → ⛓ pipeline','err'); }
};

/* ═══ Screener + ML walk-forward (Run Center additions) ═══ */
const SCR={id:null}; const WF={id:null};
(function mountScreener(){
  const host=$('runLeft'); if(!host) return;
  const div=document.createElement('div');
  div.innerHTML=`
  <h4 class="sec">🏆 Multi-market screener</h4>
  <div class="formRow"><label>strategies</label>
    <select id="scrStrats" multiple size="3" style="flex:1"></select></div>
  <label class="chip" style="margin:2px 0"><input type="checkbox" id="scrLib" checked> include whole library</label>
  <div class="formRow"><label>assets</label>
    <select id="scrAssets" multiple size="3" style="flex:1"></select></div>
  <label class="chip" style="margin:2px 0"><input type="checkbox" id="scrAll"> all watchlist</label>
  <div class="formRow"><label>tf</label><select id="scrTf" style="width:70px">
      <option>1d</option><option>1h</option><option>4h</option></select>
    <label style="min-width:0">fine-tune top</label>
    <select id="scrTune" style="width:60px"><option>0</option><option>3</option><option selected>5</option></select></div>
  <button class="pri" id="scrGo" style="width:100%">🏆 find the best plays</button>
  <div id="scrLive"></div>
  <h4 class="sec">🧠 ML walk-forward</h4>
  <p class="muted" style="font-size:10.5px;margin:-2px 0 6px">True out-of-sample: retrain per fold,
    trade only unseen bars — no lookahead.</p>
  <div class="formRow"><label>model</label><select id="wfModel" style="flex:1"></select></div>
  <div class="formRow"><label>folds</label><input id="wfFolds" type="number" value="5" style="width:56px">
    <label style="min-width:0">enter&gt;</label><input id="wfEnter" type="number" step="0.05" value="0.55" style="width:60px">
    <label style="min-width:0">exit&lt;</label><input id="wfExit" type="number" step="0.05" value="0.45" style="width:60px"></div>
  <label class="chip" style="margin:2px 0"><input type="checkbox" id="wfShort"> short side (P&lt;0.35)</label>
  <button class="pri" id="wfGo" style="width:100%">🧠 run walk-forward</button>
  <div id="wfLive"></div>`;
  const tuneEl=$('tuneLive');
  tuneEl.parentNode.insertBefore(div,tuneEl.nextSibling);
})();
function fillScreenerSelectors(){
  const s=$('scrStrats'), a=$('scrAssets'), w=$('wfModel');
  if(s) s.innerHTML=S.strategies.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
  if(a) a.innerHTML=S.watch.filter(x=>x.exchange!=='macro').map(x=>
    `<option value="${x.id}">${esc(x.symbol)}</option>`).join('');
  if(w) w.innerHTML=S.mlModels.filter(x=>x.status==='ready').map(x=>
    `<option value="${x.id}">${esc(x.name)} (${esc(x.task)})</option>`).join('')
    ||'<option value="">— train a model first (Terminal → 🤖 ML) —</option>';
}
$('scrGo').onclick=async()=>{
  const ids=[...$('scrStrats').selectedOptions].map(o=>o.value);
  if($('scrLib').checked) ids.push('library');
  const assets=[...$('scrAssets').selectedOptions].map(o=>o.value);
  const body={strategy_ids:ids, tf:$('scrTf').value, metric:$('rcMetric').value,
    autotune_top:+$('scrTune').value, all_watchlist:$('scrAll').checked};
  if(assets.length&&!$('scrAll').checked) body.assets=assets;
  if(!ids.length) return toast('pick strategies (or keep "library" ticked)','err');
  const r=await api('/markets/backtest/batch','POST',body,120000);
  if(r&&r.ok){ SCR.id=r.batch_id;
    $('scrLive').innerHTML=`<div class="card" style="margin-top:6px"><b>screening ${r.combos} combos…</b>
      <div class="pbar busy" style="margin-top:6px"><i id="scrBar"></i></div>
      <div class="muted" style="font-size:11px" id="scrInfo"></div></div>`;
  } else toast(esc((r&&r.error)||'screen failed'),'err');
};
async function renderScreener(){
  const r=await api('/markets/backtest/batch/status?id='+SCR.id+'&top=60');
  const b=r&&r.batch; if(!b) return;
  $('scrLive').innerHTML='';
  const m=$('runMain');
  m.innerHTML=`<h2 class="sec">🏆 Best plays — ${b.total} combos by ${esc(b.metric)}</h2>
    <table class="tbl" id="scrTbl"><tr><th>#</th><th>strategy</th><th>market</th>
      <th>${esc(b.metric)}</th><th>return</th><th>vs B&amp;H</th><th>trades</th><th>tuned</th><th></th></tr>${
    (b.results||[]).filter(x=>x.stats).map((row,i)=>{
      const st=row.tuned?row.tuned.stats:row.stats;
      return `<tr><td>${i+1}</td><td>${esc(row.strategy)}</td>
        <td>${esc(String(row.dataset_id).replace('mkt.','').replace('.1d',''))}</td>
        <td class="num"><b>${st[b.metric]??'—'}</b></td>
        <td class="${(st.total_return_pct||0)>=0?'up':'dn'}">${fmtPct(st.total_return_pct)}</td>
        <td class="muted">${fmtPct(row.stats.buy_hold_return_pct)}</td>
        <td>${st.trades??row.stats.trades??'—'}</td>
        <td>${row.tuned?'<span class="chip on" title="'+esc(JSON.stringify(row.tuned.values))+'">✦</span>':''}</td>
        <td><button class="ghost" data-run="${esc(row.strategy_id)}" data-ds="${esc(row.dataset_id)}">▶</button></td></tr>`;
    }).join('')}</table>
    <p class="muted" style="font-size:11px;margin-top:8px">▶ runs the full backtest (equity, analytics,
      replay). ✦ hover shows the fine-tuned parameter values.</p>`;
  m.querySelectorAll('[data-run]').forEach((btn,bi)=>btn.onclick=async()=>{
    const row=(b.results||[]).filter(x=>x.stats)[bi];
    const body={dataset_id:btn.dataset.ds,limit:8000,name:row?row.strategy:''};
    if(btn.dataset.run) body.strategy_id=btn.dataset.run;
    else{ /* library template row — resolve its spec by name */
      const tpl=S.library.find(x=>x.name===row.strategy);
      if(!tpl) return toast('template not found','err');
      body.spec=tpl.spec;
    }
    btn.innerHTML='<span class="spin"></span>';
    const r2=await api('/markets/backtest/run','POST',body,300000);
    btn.textContent='▶';
    if(r2&&r2.id) openResult(r2.id);
    else if(r2&&r2.error) toast(esc(r2.error),'err');
  });
}
$('wfGo').onclick=async()=>{
  const body={dataset_id:rcDataset(), model_id:$('wfModel').value,
    folds:+$('wfFolds').value||5, enter_above:+$('wfEnter').value||0.55,
    exit_below:+$('wfExit').value||0.45};
  if($('wfShort').checked) body.short_below=0.35;
  if(!body.model_id) return toast('train an ML model first (Terminal sub-tab → 🤖)','err');
  if(!body.dataset_id) return toast('pick an asset above','err');
  const r=await api('/markets/ml/walkforward','POST',body);
  if(r&&r.ok){ WF.id=r.id;
    $('wfLive').innerHTML=`<div class="card" style="margin-top:6px"><b>walk-forward running…</b>
      <div class="pbar busy" style="margin-top:6px"><i id="wfBar"></i></div>
      <div class="muted" style="font-size:11px" id="wfInfo">training fold 1…</div></div>`; }
  else toast(esc((r&&r.error)||'failed'),'err');
};

/* ═══ On-the-spot infographics (agent-composed, live) ═══ */
(function mountInfog(){
  const host=$('pulseBody'); if(!host) return;
  const div=document.createElement('div');
  div.innerHTML=`<h4 class="sec" style="display:flex;align-items:center;gap:8px">📊 Infographics
    <span class="muted" style="text-transform:none;letter-spacing:0">— composed live by you or the copilot (markets.infographic.save)</span>
    <span style="flex:1"></span><button class="ghost" id="igRefresh">↻</button></h4>
    <div id="igGrid"></div>`;
  host.appendChild(div);
  div.querySelector('#igRefresh').onclick=loadInfogs;
})();
async function loadInfogs(){
  const r=await api('/markets/infographic/list');
  const grid=$('igGrid'); if(!grid) return;
  const items=(r&&r.infographics)||[];
  grid.innerHTML=items.map(ig=>`
    <div class="igCard" data-ig="${esc(ig.id)}">
      <div style="display:flex;gap:8px;align-items:baseline">
        <h3>${esc((ig.spec&&ig.spec.title)||ig.name)}</h3><span style="flex:1"></span>
        <span class="muted" style="font-size:9.5px">${esc(ig.author||'')}</span>
        <button class="ghost" data-igpin="${esc(ig.id)}" title="pin to the Charts area as a tile" style="padding:1px 6px">📌</button>
        <button class="ghost" data-igdel="${esc(ig.id)}" style="padding:1px 6px">✕</button></div>
      ${ig.spec&&ig.spec.subtitle?`<div class="sub">${esc(ig.spec.subtitle)}</div>`:''}
      <div class="igPanels">${((ig.spec&&ig.spec.panels)||[]).map((p,k)=>igPanelHtml(p,ig.id,k)).join('')}</div>
    </div>`).join('')
    ||'<div class="empty" style="grid-column:1/-1;padding:18px">none yet — ask the 🤖 copilot to "build me an infographic of …"</div>';
  grid.querySelectorAll('[data-igdel]').forEach(b=>b.onclick=async()=>{
    await api('/markets/infographic/delete','POST',{id:b.dataset.igdel}); loadInfogs(); });
  grid.querySelectorAll('[data-igpin]').forEach(b=>b.onclick=()=>{
    if(typeof addIgTile==='function'){ addIgTile(b.dataset.igpin);
      switchView('charts'); toast('infographic pinned to Charts','ok'); }});
  items.forEach(ig=>((ig.spec&&ig.spec.panels)||[]).forEach((p,k)=>igPanelDraw(p,ig.id,k)));
}
function igPanelHtml(p,igid,k){
  const wide=p.wide||['heatmap','bars','spark'].includes(p.type)&&(p.data||[]).length>14;
  const id=`igc_${igid}_${k}`;
  let body='';
  if(p.type==='stat')
    body=`<div class="v" style="color:${esc(p.color||'var(--ink)')}">${esc(String(p.value??'—'))}</div>`+
      (p.delta!=null?`<span class="d ${p.delta>=0?'up':'dn'}">${fmtPct(+p.delta)}</span>`:'');
  else if(p.type==='text') body=`<div style="font-size:11.5px">${esc(p.text||'')}</div>`;
  else if(p.type==='gauge'){
    const v=Math.max(0,Math.min(100,+(p.value??(p.data&&p.data[0])??0)));
    body=`<div class="v">${esc(String(p.value??v))}</div>
      <div class="pbar" style="margin-top:5px"><i style="width:${v}%;background:${esc(p.color||'var(--acc)')}"></i></div>`;
  } else body=`<canvas id="${id}"></canvas>`+
      (p.value!=null?`<div class="d">${esc(String(p.value))}</div>`:'');
  return `<div class="igP${wide?' wide':''}"><div class="l">${esc(p.label||p.type)}</div>${body}</div>`;
}
function igPanelDraw(p,igid,k){
  const cv=document.getElementById(`igc_${igid}_${k}`);
  if(!cv||!Array.isArray(p.data)||!p.data.length) return;
  const dpr=devicePixelRatio||1, W=cv.clientWidth||140, H=cv.clientHeight||46;
  cv.width=W*dpr; cv.height=H*dpr;
  const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const col=p.color||COL.acc;
  if(p.type==='spark'){
    const v=p.data.map(Number).filter(isFinite);
    const lo=Math.min(...v),hi=Math.max(...v);
    ctx.beginPath();
    v.forEach((x,i)=>{const X=i/(v.length-1)*W,Y=H-2-(hi>lo?(x-lo)/(hi-lo):0.5)*(H-4);
      i?ctx.lineTo(X,Y):ctx.moveTo(X,Y);});
    ctx.strokeStyle=col; ctx.lineWidth=1.5; ctx.stroke();
  } else if(p.type==='bars'){
    const v=p.data.map(Number);
    const hi=Math.max(...v.map(Math.abs),1e-9);
    const bw=Math.max(2,W/v.length-2);
    const y0=v.some(x=>x<0)?H/2:H-2;
    v.forEach((x,i)=>{ const bh=Math.abs(x)/hi*(y0===H/2?H/2-2:H-6);
      ctx.fillStyle=hexA(x>=0?(p.color||COL.up):COL.dn,.85);
      ctx.fillRect(i*(bw+2)+1, x>=0?y0-bh:y0, bw, Math.max(1,bh)); });
  } else if(p.type==='donut'){
    const v=p.data.map(Number).filter(x=>x>0);
    const tot=v.reduce((a,b)=>a+b,0)||1;
    const cx=H/2+2, cy=H/2, r=H/2-3;
    let a0=-Math.PI/2;
    v.forEach((x,i)=>{ const a1=a0+x/tot*Math.PI*2;
      ctx.beginPath(); ctx.arc(cx,cy,r,a0,a1); ctx.arc(cx,cy,r*0.55,a1,a0,true);
      ctx.closePath(); ctx.fillStyle=COL.cat[i%COL.cat.length]; ctx.fill(); a0=a1; });
    ctx.font='9px ui-monospace,Consolas,monospace'; ctx.fillStyle=COL.muted; ctx.textAlign='left';
    (p.labels||[]).slice(0,4).forEach((lb,i)=>{
      ctx.fillStyle=COL.cat[i%COL.cat.length]; ctx.fillRect(H+10,4+i*11,7,7);
      ctx.fillStyle=COL.muted; ctx.fillText(String(lb).slice(0,16),H+20,11+i*11); });
  } else if(p.type==='heatmap'){
    const rows=p.data.filter(Array.isArray);
    if(!rows.length) return;
    const flat=rows.flat().map(Number).filter(isFinite);
    const mx=Math.max(...flat.map(Math.abs),1e-9);
    const ch=Math.max(6,(H-2)/rows.length), cw=Math.max(6,(W-2)/rows[0].length);
    rows.forEach((row,ri)=>row.forEach((x,ci)=>{
      ctx.fillStyle=hexA(x>=0?COL.up:COL.dn,.15+Math.min(1,Math.abs(x)/mx)*.75);
      ctx.fillRect(ci*cw+1,ri*ch+1,cw-1,ch-1); }));
  }
}

/* ═══ 🤖 Copilot dock (same v5 agent-loop pattern as the Terminal panel) ═══ */
(function mountCopilot(){
  const bar=$('topbar');
  const btn=document.createElement('button');
  btn.className='ghost'; btn.id='btnCop'; btn.textContent='🤖 copilot';
  bar.insertBefore(btn,$('btnLayouts').parentNode);
  const dock=document.createElement('div');
  dock.id='copDock';
  dock.innerHTML=`
    <div id="copHead"><b>🤖 Quant copilot</b>
      <span class="muted" style="font-size:10.5px">v5 loop · full markets toolkit</span>
      <span style="flex:1"></span><button class="ghost" id="copClose">✕</button></div>
    <div id="copLog"></div>
    <div class="qchips">
      <span class="chip" data-q="Screen the whole strategy library across my watchlist on 1d bars and tell me the 3 best plays.">🏆 best plays</span>
      <span class="chip" data-q="Build a custom indicator that measures trend quality, test it, and put it on my active chart.">ƒx invent indicator</span>
      <span class="chip" data-q="Compose an infographic summarising today's market: breadth, sector moves, BTC, and anything unusual.">📊 build infographic</span>
      <span class="chip" data-q="Autotune my most recently saved strategy on its dataset and report before/after stats.">✦ autotune</span>
    </div>
    <div id="copIn"><textarea id="copText" rows="2" placeholder="ask anything — it can run every markets.* capability…"></textarea>
      <button class="pri" id="copSend">➤</button><button id="copStop" style="display:none">◼</button></div>`;
  document.body.appendChild(dock);
  btn.onclick=()=>dock.classList.toggle('on');
  dock.querySelector('#copClose').onclick=()=>dock.classList.remove('on');
  dock.querySelectorAll('.qchips .chip').forEach(ch=>ch.onclick=()=>{
    $('copText').value=ch.dataset.q; copSend(); });
  $('copSend').onclick=()=>copSend();
  $('copText').addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); copSend(); }});
  $('copStop').onclick=()=>{ try{_copAbort&&_copAbort.abort();}catch(_){} copDone(); };
})();
const COP_SID=(pref('cop_sid')||('qs_'+Math.random().toString(36).slice(2,10)));
pref('cop_sid',COP_SID);
let _copAbort=null,_copBusy=false;
const COP_TOOLKIT=[
 'markets.lookup','markets.asset.add','markets.fetch','markets.jobs','markets.bars','markets.quotes',
 'markets.watchlist.list','markets.update_now','markets.history.audit','markets.history.repair',
 'markets.indicators','markets.indicator_config.get','markets.indicator_config.set',
 'markets.indicator.custom.save','markets.indicator.custom.list','markets.indicator.custom.test',
 'markets.annotate.add','markets.annotate.list','markets.annotate.remove',
 'markets.sentiment.analyze','markets.sentiment.map','markets.sentiment.history',
 'markets.ml.create','markets.ml.list','markets.ml.train','markets.ml.predict','markets.ml.series',
 'markets.ml.walkforward',
 'markets.strategy.save','markets.strategy.list','markets.strategy.delete',
 'markets.strategy.library','markets.strategy.from_template',
 'markets.strategy.accept','markets.strategy.archive','markets.monitor.status',
 'markets.alerts.list','markets.alerts.ack',
 'markets.backtest.run','markets.backtest.list','markets.backtest.get','markets.backtest.signals',
 'markets.backtest.analyze','markets.backtest.sweep','markets.backtest.sweep_status',
 'markets.backtest.autotune','markets.backtest.autotune_status',
 'markets.backtest.batch','markets.backtest.batch_status',
 'markets.analysis.trendfit','markets.analysis.pivots','markets.overview',
 'markets.baseline.ensure','markets.events.detect','markets.events.apply',
 'markets.macro.catalog','markets.macro.fetch',
 'markets.sim.create','markets.sim.list','markets.sim.order','markets.sim.equity',
 'markets.infographic.save','markets.infographic.list','markets.infographic.delete',
 'web.search'];
function copContext(){
  const parts=['You are the Quant Studio copilot.'];
  const t=activeTile();
  if(t&&t.key) parts.push(`Active chart: ${t.key} (${t.tf}, dataset ${dsId(t.provider,t.symbol,t.tf)}).`);
  parts.push(`Current view: ${S.view}. ${S.strategies.length} saved strategies, `+
    `${S.watch.length} tracked assets.`);
  parts.push('Prefer these studio capabilities: markets.strategy.library/from_template '+
    '(never hand-write rule JSON when a template fits); markets.backtest.run then '+
    'markets.backtest.analyze for deep stats; markets.backtest.autotune to optimise; '+
    'markets.backtest.batch to screen strategies across many markets; '+
    'markets.ml.walkforward for honest out-of-sample ML tests; markets.analysis.trendfit '+
    'and markets.analysis.pivots for regime/pivot structure; markets.overview for the '+
    'whole market; markets.sim.* to paper-trade. To SHOW results visually, compose an '+
    'infographic with markets.infographic.save (panels: stat/spark/bars/donut/gauge/'+
    'heatmap/text) — it renders live in the Pulse tab. You may draw on charts with '+
    'markets.annotate.add. Finish with a concise summary.');
  return parts.join(' ');
}
function copAdd(cls,html){ const d=document.createElement('div');
  d.className='cmsg '+cls; d.innerHTML=html;
  $('copLog').appendChild(d); $('copLog').scrollTop=1e9; return d; }
function copDone(){ _copBusy=false; $('copSend').style.display='';
  $('copStop').style.display='none'; _copAbort=null; }
async function copSend(){
  const text=$('copText').value.trim();
  if(!text||_copBusy) return;
  $('copText').value='';
  copAdd('user',esc(text));
  const holder=copAdd('bot','<span class="muted"><span class="spin"></span> working…</span>');
  if(!(window.customElements&&customElements.get('vera-agent-loop-output'))){
    if(!document.getElementById('alo-script')){
      const s=document.createElement('script'); s.id='alo-script';
      s.src='/ui/elements/agent_loop_output.js'; document.head.appendChild(s); }
    const t0=Date.now();
    while(Date.now()-t0<5000&&!(window.customElements&&customElements.get('vera-agent-loop-output')))
      await new Promise(r=>setTimeout(r,200));
  }
  if(!(window.customElements&&customElements.get('vera-agent-loop-output'))){
    holder.innerHTML='<span class="dn">agent-loop renderer unavailable</span>'; return; }
  holder.innerHTML='';
  const el=document.createElement('vera-agent-loop-output');
  el.setAttribute('compact','true'); el.setAttribute('max-height','360');
  holder.appendChild(el);
  el.setApiBase(BASE); el.setSessionId(COP_SID);
  el.setHitlEndpoint('/workshop/agent_loop/hitl/respond');
  el.setShowThinking(true);
  el.addEventListener('alo:done',copDone); el.addEventListener('alo:error',copDone);
  _copBusy=true; $('copSend').style.display='none'; $('copStop').style.display='';
  const runId='qs_'+Date.now().toString(36);
  el.appendEvent({type:'start',version:'v5',max_cycles:12,agent_name:'quant-copilot',goal:text});
  const req={ goal:'[Context: '+copContext()+']\n\nRequest: '+text,
    allowed_caps:COP_TOOLKIT.join(','), base_toolkit:COP_TOOLKIT.join(','),
    max_cycles:12, version:'v5', session_id:COP_SID, run_id:runId,
    record_history:true, record_agent_name:'quant-copilot',
    satisfaction_check:true, enable_expand:true, require_approval:false,
    prefer_gpu:true, triage_top_k:16 };
  _copAbort=new AbortController();
  let r;
  try{ r=await fetch(BASE+'/workshop/agent_loop/stream',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(req),
    signal:_copAbort.signal});
  }catch(e){ el.appendEvent({type:'error',error:'connection failed: '+(e.message||e)}); copDone(); return; }
  if(!r.ok){ el.appendEvent({type:'error',error:'HTTP '+r.status}); copDone(); return; }
  const reader=r.body.getReader(); const dec=new TextDecoder(); let buf='';
  try{
    while(true){
      let done,value;
      try{ ({done,value}=await reader.read()); }catch(_){ break; }
      if(done) break;
      buf+=dec.decode(value,{stream:true});
      let i;
      while((i=buf.indexOf('\n\n'))>=0){
        const chunk=buf.slice(0,i); buf=buf.slice(i+2);
        for(const ln of chunk.split('\n')){
          if(!ln.startsWith('data:')) continue;
          const pl=ln.slice(5).trim();
          if(pl==='[DONE]'){ copDone(); return; }
          let ev; try{ ev=JSON.parse(pl); }catch(_){ continue; }
          if(ev.run_id&&ev.run_id!==runId) continue;
          el.appendEvent(ev);
          const ty=String(ev.type||'');
          if(ty.indexOf('tool_done')>=0||ty.indexOf('tool_ok')>=0)
            copCapRefresh(String(ev.tool||ev.cap||ev.name||''));
        }
      }
      $('copLog').scrollTop=1e9;
    }
  }finally{ copDone(); }
}
function copCapRefresh(cap){
  if(cap.startsWith('markets.infographic')){ loadInfogs(); }
  else if(cap.startsWith('markets.annotate')) Object.values(TILES).forEach(tileEvents);
  else if(cap.startsWith('markets.strategy')) loadStrats();
  else if(cap.startsWith('markets.backtest')) loadRunHist();
  else if(cap.startsWith('markets.sim')&&S.view==='sim') loadSim();
  else if(cap.startsWith('markets.indicator.custom')) loadCustomInds();
  else if(cap.startsWith('markets.watchlist')||cap==='markets.asset.add'||cap==='markets.fetch') loadWatch();
}

/* ═══ event-wrapper: batch / walk-forward / infographic streams ═══ */
const _handleEventBase=handleEvent;
handleEvent=function(ev){
  const ty=String(ev.type||''), st=String(ev.stage||'');
  if(ty==='markets.backtest'&&st.startsWith('batch')){
    if(SCR.id&&ev.batch_id===SCR.id){
      if(st==='batch_progress'){ const b=$('scrBar');
        if(b) b.style.width=Math.min(100,(ev.done||0)/(ev.total||1)*100)+'%';
        const inf=$('scrInfo'); if(inf) inf.textContent=`${ev.done}/${ev.total} backtested`; }
      if(st==='batch_tuned'){ const inf=$('scrInfo');
        if(inf) inf.textContent='fine-tuning '+(ev.strategy||'')+' …'; }
      if(st==='batch_done') renderScreener();
      if(st==='batch_error'){ $('scrLive').innerHTML=
        `<div class="card" style="border-color:var(--dn)">✕ ${esc(ev.error||'failed')}</div>`; }
    }
    return;
  }
  if(ty==='markets.ml'&&st.startsWith('wf_')){
    if(WF.id&&ev.id===WF.id){
      if(st==='wf_progress'){ const b=$('wfBar');
        if(b) b.style.width=Math.min(100,(ev.fold||0)/(ev.folds||1)*100)+'%';
        const inf=$('wfInfo'); if(inf) inf.textContent=
          `fold ${ev.fold}/${ev.folds} — train ${ev.train}, test ${ev.test} bars`; }
      if(st==='wf_error'){ $('wfLive').innerHTML=
        `<div class="card" style="border-color:var(--dn)">✕ ${esc(ev.error||'failed')}</div>`; WF.id=null; }
    }
    return;
  }
  if(ty==='markets.backtest'&&st==='done'&&WF.id&&ev.id===WF.id){
    $('wfLive').innerHTML=''; WF.id=null;
    loadRunHist(); openResult(ev.id);
    toast('🧠 walk-forward complete — '+fmtPct((ev.stats||{}).total_return_pct)+' out-of-sample','ok');
    return;
  }
  if(ty==='markets.infographic'){ loadInfogs();
    if(st==='saved') toast('📊 infographic “'+esc(ev.name||'')+'” updated','ok');
    return; }
  _handleEventBase(ev);
};
/* screener/model selectors track data loads */
const _loadStratsBase=loadStrats;
loadStrats=async function(){ await _loadStratsBase(); fillScreenerSelectors(); };
const _loadModelsBase=loadModels;
loadModels=async function(){ await _loadModelsBase(); fillScreenerSelectors(); };
setTimeout(()=>{ fillScreenerSelectors(); loadInfogs(); },1200);
