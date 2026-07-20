
'use strict';
/* ═══ round 5: one panel · ⚙ workspace popover · REAL chat tools · ledger · metrics rail · paper runs ═══ */

/* ── ⚙ chart workspace controls move out of the top bar into a popover ── */
(function mountViewCtl(){
  const bar=$('chartsBar'); if(!bar) return;
  const park=document.createElement('div'); park.id='vcPark'; park.style.display='none';
  document.body.appendChild(park);
  bar.style.display='none'; park.appendChild(bar);
  const btn=document.createElement('button');
  btn.className='ghost'; btn.id='btnViewCtl'; btn.textContent='⚙ workspace';
  $('topbar').insertBefore(btn,$('btnCop'));
  btn.onclick=()=>{
    if(S.view!=='charts') switchView('charts');
    const p=popAt(btn,'<h4>chart workspace</h4><div id="vcHost"></div>');
    p.querySelector('#vcHost').appendChild(bar);
    bar.style.cssText='display:flex;flex-direction:column;align-items:stretch;gap:9px;padding:2px 12px 12px;border:none';
  };
  const _closePopBase=closePop;
  closePop=function(){
    try{ if(_pop&&_pop.querySelector&&_pop.querySelector('#chartsBar')){
      park.appendChild(bar); bar.style.display='none'; } }catch(_){}
    _closePopBase();
  };
})();

/* ── ⛓ pipeline builder as an in-studio overlay (classic panel, scoped) ── */
$('btnPipe').onclick=()=>{
  let ov=document.getElementById('pipeOverlay');
  if(ov){ ov.style.display='flex'; return; }
  ov=document.createElement('div'); ov.id='pipeOverlay';
  ov.style.cssText='position:fixed;inset:18px;z-index:300;background:var(--bg1);'+
    'border:1px solid var(--line2);border-radius:12px;box-shadow:var(--shadow);'+
    'display:flex;flex-direction:column;overflow:hidden';
  ov.innerHTML=`<div style="display:flex;align-items:center;gap:9px;padding:7px 12px;border-bottom:1px solid var(--line)">
      <b>⛓ Pipeline builder</b>
      <span class="muted" style="font-size:10.5px">node-graph strategy pipelines — compiles to a saved strategy usable everywhere in the studio</span>
      <span style="flex:1"></span><button class="ghost" id="pipeOvClose">✕ close</button></div>
    <iframe src="/markets/panel?pipe=1" style="flex:1;border:none"></iframe>`;
  document.body.appendChild(ov);
  ov.querySelector('#pipeOvClose').onclick=()=>{ ov.style.display='none'; loadStrats(); };
};

/* ── UI tools (shared by the chat tool-loop AND the panel bridge) ── */
const CHAT_UI_TOOLS={
  'ui.chart_load':p=>{ const t=activeTile()||addTile();
    if(t.kind==='ig') return {error:'active tile is an infographic'};
    tileSetAsset(t,String(p.symbol_key||p.key),p.tf||p.timeframe||null);
    return {ok:true,loaded:p.symbol_key}; },
  'ui.switch_view':p=>{ switchView(String(p.view||'charts')); return {ok:true,view:S.view}; },
  'ui.overlay_strategy':p=>{ const t=activeTile();
    if(!t||t.kind==='ig') return {error:'no chart tile active'};
    t.overlay={on:true,strategy_id:String(p.strategy_id||p.id)};
    tileStratOverlay(t); return {ok:true}; },
  'ui.pin_infographic':p=>{ addIgTile(String(p.id)); switchView('charts'); return {ok:true}; },
  'ui.open_result':p=>{ switchView('run'); openResult(String(p.id)); return {ok:true}; },
};
function initStudioBridge(){
  if(!window.VeraPanelBridge){ setTimeout(initStudioBridge,2500); return; }
  try{
    VeraPanelBridge.registerStateProvider(()=>{ const t=activeTile()||{};
      return { view:S.view, symbol_key:t.key||null, timeframe:t.tf||null,
        tiles:Object.values(TILES).map(x=>x.kind==='ig'?('ig:'+x.ig_id):x.key).filter(Boolean),
        strategies:S.strategies.length, sim_accounts:S.simAccounts.length }; });
    Object.entries(CHAT_UI_TOOLS).forEach(([name,fn])=>{
      VeraPanelBridge.registerActionHandler(name.replace('ui.',''),p=>fn(p||{}));
    });
  }catch(_){}
}
initStudioBridge();

/* ── 💬 chat mode with a REAL tool loop (execute via /mcp/call + UI tools) ── */
const CHAT_CAP_ALLOW=/^(markets\.|web\.search$|llm\.summarize$)/;
function extractAction(text){
  const key=text.lastIndexOf('"tool_use"');
  if(key<0) return null;
  for(let i=text.lastIndexOf('{',key);i>=0;i=text.lastIndexOf('{',i-1)){
    const cand=text.slice(i);
    let depth=0,end=-1,inStr=false,escn=false;
    for(let j=0;j<cand.length;j++){
      const ch=cand[j];
      if(escn){escn=false;continue;}
      if(ch==='\\'){escn=true;continue;}
      if(ch==='"'){inStr=!inStr;continue;}
      if(inStr)continue;
      if(ch==='{')depth++;
      else if(ch==='}'){depth--; if(depth===0){end=j+1;break;}}
    }
    if(end<0) continue;
    try{ const o=JSON.parse(cand.slice(0,end));
      if(o&&o.tool_use&&o.tool_use.name) return {obj:o,start:i,end:i+end};
    }catch(_){}
    if(i===0) break;
  }
  return null;
}
async function copChatTurn(msg,holder){
  _copAbort=new AbortController();
  let acc='',think='';
  try{
    const r=await fetch(BASE+'/agents/chat/stream',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,agent_name:COP.agent,
        history:JSON.stringify(COP.hist.slice(-16,-1)),session_id:COP_SID,
        prefer_gpu:true}),
      signal:_copAbort.signal});
    const reader=r.body.getReader(),dec=new TextDecoder(); let buf='';
    while(true){ let done,value;
      try{({done,value}=await reader.read());}catch(_){break;}
      if(done)break;
      buf+=dec.decode(value,{stream:true}); let i;
      while((i=buf.indexOf('\n\n'))>=0){ const chunk=buf.slice(0,i); buf=buf.slice(i+2);
        for(const ln of chunk.split('\n')){
          if(!ln.startsWith('data:'))continue;
          const pl=ln.slice(5).trim();
          if(pl==='[DONE]') break;
          let ev; try{ev=JSON.parse(pl);}catch(_){continue;}
          if(ev.type==='token'&&ev.text){ acc+=ev.text;
            holder.innerHTML=(think?'<div class="muted" style="font-size:10px;border-left:2px solid var(--line2);padding-left:6px;margin-bottom:4px">'+esc(think.slice(-400))+'</div>':'')
              +esc(acc).replace(/\n/g,'<br>');
            $('copLog').scrollTop=1e9; }
          else if(ev.type==='thinking'&&ev.text){ think+=ev.text; }
          else if(ev.type==='error'){ holder.innerHTML='<span class="dn">'+esc(ev.text||'error')+'</span>'; return null; }
        } }
    }
    return acc;
  }catch(e){
    if(String(e).indexOf('bort')<0) holder.innerHTML='<span class="dn">'+esc(String(e))+'</span>';
    return null;
  }
}
/* rebind: the round-4 chat sender becomes a multi-round agentic chat */
copChatSend=async function(text){
  copAdd('user',esc(text));
  const first=COP.hist.length===0;
  COP.hist.push({role:'user',content:text});
  const proto='[Tool protocol: you CAN call tools. Do brief reasoning, then end the reply with EXACTLY ONE '
    +'compact JSON action {"thought":"…","tool_use":{"name":"<tool>","input":{…}}} using EXACT parameter '
    +'names — the result arrives as a [tool_result] message. Besides your markets.* capabilities you have '
    +'UI tools: ui.chart_load{symbol_key,tf}, ui.switch_view{view:charts|strat|run|pulse|proj|sim}, '
    +'ui.overlay_strategy{strategy_id}, ui.pin_infographic{id}, ui.open_result{id}. '
    +'When you are done, answer plainly with NO action.]\n\n';
  let msg=(first?proto:'')+text;
  _copBusy=true; $('copSend').style.display='none'; $('copStop').style.display='';
  try{
    for(let round=0;round<7;round++){
      const holder=copAdd('bot','<span class="spin"></span>');
      const acc=await copChatTurn(msg,holder);
      if(acc==null||!acc.trim()){ if(acc!=null) holder.innerHTML='<span class="muted">…</span>'; break; }
      COP.hist.push({role:'assistant',content:acc});
      const act=extractAction(acc);
      if(!act) break;                          /* plain answer — done */
      const visible=acc.slice(0,act.start).trim();
      const name=act.obj.tool_use.name, input=act.obj.tool_use.input||{};
      holder.innerHTML=(visible?esc(visible).replace(/\n/g,'<br>')+'<br>':'')
        +`<span class="chip on" style="font-size:10px">⚙ ${esc(name)}</span> <span class="spin"></span>`;
      let result;
      if(CHAT_UI_TOOLS[name]){
        try{ result=await CHAT_UI_TOOLS[name](input); }catch(e){ result={error:String(e)}; }
      } else if(CHAT_CAP_ALLOW.test(name)){
        try{
          const r=await fetch(BASE+'/mcp/call',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({name,arguments:input,session_id:COP_SID})});
          const j=await r.json();
          result=(j&&j.content!==undefined)?j.content:j;
        }catch(e){ result={error:String(e)}; }
        try{ copCapRefresh(name); }catch(_){}
      } else result={error:'tool "'+name+'" is not in the chat toolset'};
      const summary=JSON.stringify(result??null);
      holder.innerHTML=(visible?esc(visible).replace(/\n/g,'<br>')+'<br>':'')
        +`<span class="chip on" style="font-size:10px">⚙ ${esc(name)}</span>`
        +` <span class="muted" style="font-size:10px">${esc(summary.slice(0,180))}${summary.length>180?'…':''}</span>`;
      const toolMsg=`[tool_result ${name}] ${summary.slice(0,3800)}`;
      COP.hist.push({role:'user',content:toolMsg});
      msg=toolMsg;
      $('copLog').scrollTop=1e9;
    }
  } finally { copDone(); }
};

/* ── 💼 portfolio ledger editing (Project view) ── */
(function mountLedger(){
  const bar=$('pjGo')&&$('pjGo').parentNode; if(!bar) return;
  const b=document.createElement('button');
  b.id='pjLedger'; b.textContent='💼 ledger';
  bar.insertBefore(b,$('pjXray'));
  b.onclick=renderLedger;
})();
async function renderLedger(){
  const r=await api('/markets/portfolio/tx_list?limit=300');
  const txs=(r&&r.transactions)||[];
  /* the ledger renders into the Portfolio page when it exists (its home),
     falling back to the old Project-view mount */
  let host=$('foLedger')||$('pjLedgerOut');
  if(!host){ host=document.createElement('div'); host.id='pjLedgerOut';
    $('pjTiles').before(host); }
  host.innerHTML=`<div class="card" style="margin-bottom:12px">
    <h4 class="sec" style="margin-top:0">💼 portfolio ledger — the REAL book (edits here change your portfolio)</h4>
    <div class="formRow">
      <input id="lgSym" list="lgSymList" placeholder="provider:symbol e.g. yahoo:AAPL" style="width:190px">
      <datalist id="lgSymList">${S.watch.filter(w=>!['macro','dyn'].includes(w.exchange)).map(w=>
        `<option value="${esc(w.id)}">`).join('')}</datalist>
      <select id="lgSide" style="width:70px"><option>buy</option><option>sell</option></select>
      <input id="lgQty" type="number" placeholder="qty" style="width:86px">
      <input id="lgPx" type="number" placeholder="unit price" style="width:96px">
      <input id="lgFee" type="number" placeholder="fees" style="width:70px">
      <input id="lgTs" type="date" style="width:130px">
      <button class="pri" id="lgAdd">＋ record</button></div>
    <div style="max-height:260px;overflow:auto"><table class="tbl" id="lgTbl"></table></div></div>`;
  $('lgTbl').innerHTML='<tr><th>date</th><th>side</th><th>asset</th><th>qty</th><th>price</th><th>fees</th><th></th></tr>'+
    txs.map(tx=>`<tr><td>${esc((tx.ts||'').slice(0,10))}</td>
      <td class="${tx.side==='buy'?'up':'dn'}">${esc(tx.side)}</td>
      <td>${esc(tx.symbol_key)}</td><td>${tx.qty}</td><td>${fmtPx(tx.price)}</td>
      <td>${tx.fees||0}</td>
      <td><button class="ghost danger" data-txdel="${esc(tx.id)}">✕</button></td></tr>`).join('')
    ||'<tr><td colspan="7" class="muted">no transactions recorded — add your holdings below</td></tr>';
  host.querySelectorAll('[data-txdel]').forEach(bt=>bt.onclick=async()=>{
    if(!confirm('delete this transaction?')) return;
    await api('/markets/portfolio/tx_remove','POST',{id:bt.dataset.txdel});
    renderLedger();
  });
  $('lgAdd').onclick=async()=>{
    const body={symbol_key:$('lgSym').value.trim(),side:$('lgSide').value,
      qty:+$('lgQty').value,price:+$('lgPx').value,fees:+$('lgFee').value||0};
    if($('lgTs').value) body.ts=$('lgTs').value+'T12:00:00Z';
    if(!body.symbol_key||!(body.qty>0)) return toast('symbol + qty required','err');
    const r2=await api('/markets/portfolio/tx_add','POST',body);
    if(r2&&r2.ok){ toast('recorded','ok'); renderLedger(); }
    else toast(esc((r2&&r2.error)||'failed'),'err');
  };
}

/* ── ＋ custom asset (folded in from the classic panel) ── */
(function mountCustom(){
  const dd=$('dataDrawer'); if(!dd) return;
  const div=document.createElement('div');
  div.innerHTML=`<h4>＋ custom asset</h4>
    <div class="formRow"><input id="caName" placeholder="e.g. Charizard 1st Ed" style="flex:1">
      <select id="caClass" style="width:110px"><option>collectable</option><option>trading_card</option>
        <option>video_game</option><option>other</option></select></div>
    <div class="formRow"><input id="caPx" type="number" placeholder="current value" style="width:110px">
      <button class="pri" id="caGo">create &amp; track</button></div>`;
  dd.appendChild(div);
  div.querySelector('#caGo').onclick=async()=>{
    const r=await api('/markets/custom/create','POST',
      {name:$('caName').value.trim(),asset_class:$('caClass').value,
       initial_price:+$('caPx').value||0});
    if(r&&!r.error){ toast('custom asset created — add price points any time via the copilot','ok');
      $('caName').value=''; loadWatch(); }
    else toast(esc((r&&r.error)||'failed'),'err');
  };
})();

/* ── 📊 Metrics rail in Pulse (separate from the market map) ── */
const _metricBarCache={};
async function renderMetricsRail(){
  const rows=S.watch.filter(w=>['macro','dyn'].includes(w.exchange)).slice(0,16);
  let host=$('metricsRail');
  if(!rows.length){ if(host) host.remove(); return; }
  if(!host){ host=document.createElement('div'); host.id='metricsRail';
    const anchor=$('osintRow')||$('pulseSectors');
    anchor.before(host); }
  host.innerHTML=`<h4 class="sec">📊 Metrics <span class="muted" style="text-transform:none;letter-spacing:0">— macro · positioning · social (never mixed into the market map)</span></h4>
    <div class="assetChips" style="margin-bottom:12px">${rows.map(w=>{
      const label=String(w.symbol).replace('fred:','').replace('#',' · ').replace('wsb_','WSB ');
      return `<div class="aChip" data-mkey="${esc(w.exchange+':'+w.symbol)}">
        <div><div class="s">${esc(label)}</div><div class="c num" data-mlast="${esc(w.symbol)}">…</div></div>
        <canvas data-mspark="${esc(w.symbol)}"></canvas></div>`;}).join('')}</div>`;
  host.querySelectorAll('.aChip').forEach(el=>el.onclick=()=>{
    switchView('charts');
    const t=addTile(); t.style='area'; t.inds=[];
    tileSetAsset(t,el.dataset.mkey);
  });
  for(const w of rows){
    const ds=dsId(w.exchange,w.symbol,'1d');
    let bars=_metricBarCache[ds];
    if(!bars){ bars=await api('/markets/bars?dataset_id='+encodeURIComponent(ds)+'&limit=90');
      if(bars&&bars.c) _metricBarCache[ds]=bars; }
    if(!bars||!bars.c||!bars.c.length) continue;
    const lastEl=host.querySelector(`[data-mlast="${CSS.escape(w.symbol)}"]`);
    if(lastEl) lastEl.textContent=fmtN(bars.c[bars.c.length-1]);
    const cv=host.querySelector(`[data-mspark="${CSS.escape(w.symbol)}"]`);
    if(cv&&bars.c.length>2) drawSpark(cv,bars.c,bars.c[bars.c.length-1]>=bars.c[0]);
  }
}
const _renderPulseR5=renderPulse;
renderPulse=function(){ _renderPulseR5(); renderMetricsRail(); };

/* ── ⏩ forward-test + 🛰 live paper runs ── */
async function forwardTest(an,full){
  let sid=an.strategy_id;
  if(!sid){
    const spec=an.spec||((full||{}).spec)||{};
    const r=await api('/markets/strategy/save','POST',
      {name:(an.name||'forward')+' (fwd)',spec,kind:spec.kind||'rule'});
    if(r&&r.ok){ sid=r.id; loadStrats(); }
    else return toast('could not save the strategy first: '+esc((r&&r.error)||''),'err');
  }
  await loadSim();
  let acct=S.simAccounts.find(a=>a.name==='forward-tests');
  if(!acct){
    const c=await api('/markets/sim/create','POST',{name:'forward-tests',cash:100000});
    if(!(c&&c.ok)) return toast('could not create the forward-tests sim account','err');
    acct={id:c.id};
  }
  const acc=await api('/markets/strategy/accept','POST',
    {id:sid,dataset_id:an.dataset_id,interval_min:15,
     sim_account_id:acct.id,sim_pct:20});
  if(acc&&acc.ok){
    toast('⏩ LIVE on paper — signals re-evaluated every 15 min trade the "forward-tests" account (its own sleeve)','ok');
    loadPaperRuns();
  } else toast(esc((acc&&acc.error)||'failed'),'err');
}
(function mountPaperRuns(){
  const host=$('runLeft'); if(!host) return;
  const div=document.createElement('div');
  div.innerHTML='<h4 class="sec">🛰 Live paper runs</h4><div id="paperRuns" class="muted" style="font-size:11px">—</div>';
  host.appendChild(div);
})();
async function loadPaperRuns(){
  const r=await api('/markets/monitor/status');
  const mons=(r&&r.monitors)||[];
  const el=$('paperRuns'); if(!el) return;
  el.innerHTML=mons.map(m=>{
    const st=m.state||{};
    const pos=st.position||'flat';
    return `<div class="condRow" style="font-size:11px">
      <b style="max-width:110px;overflow:hidden;text-overflow:ellipsis">${esc(m.name||m.id)}</b>
      <span class="muted" style="font-size:9.5px">${esc(String(m.dataset_id||'').replace('mkt.',''))}</span>
      <span class="chip ${pos==='long'?'bull':pos==='short'?'bear':''}" style="font-size:9px">${esc(pos)}</span>
      ${(m.channels||[]).length||m.enabled?'':''}
      ${m.sim_account_id||((m.state||{}).sim)?'<span class="chip on" style="font-size:9px">◎ sim</span>':''}
      <span class="muted" style="font-size:9px">${esc(String(st.last_signal||''))}</span>
      <span style="flex:1"></span>
      <button class="ghost danger" data-mstop="${esc(m.id)}" title="stop monitoring">⏹</button></div>`;
  }).join('')||'<span class="muted">none — open a backtest result and hit ⏩ forward-test</span>';
  el.querySelectorAll('[data-mstop]').forEach(b=>b.onclick=async()=>{
    await api('/markets/strategy/archive','POST',{id:b.dataset.mstop});
    loadPaperRuns(); loadStrats();
  });
}
const _svBase5=switchView;
switchView=function(n){ _svBase5(n); if(n==='run') setTimeout(loadPaperRuns,700); };
const _handleEvR5=handleEvent;
handleEvent=function(ev){
  const ty=String(ev.type||'');
  if((ty==='markets.alert'||ty==='markets.monitor')&&S.view==='run')
    setTimeout(loadPaperRuns,400);
  _handleEvR5(ev);
};
