
'use strict';
/* ═══ round 4: drawing · OSINT/pulse+ · ig tiles · sim templates · copilot v2 · evolve ═══ */

/* ── ✏ drawing on quant charts ── */
const DRAW_TOOLS=[['trendline','╱ trendline',2],['ray','⟿ ray',2],['hline','― level',1],
  ['vline','┃ key date',1],['rect','▭ zone',2],['fib','𝔽 fib',2],['label','◎ label',1]];
function openDrawPop(t,anchor){
  const mine=(t._annRows||[]).filter(a=>a.author==='user'||a.author==='vera');
  const html=`<h4>✏ Draw on chart</h4>
    <div class="row" style="flex-wrap:wrap;gap:4px">${DRAW_TOOLS.map(([k,l])=>
      `<span class="chip ${t._drawTool===k?'on':''}" data-dt="${k}">${l}</span>`).join('')}
      ${t._drawTool?'<span class="chip" data-dt="">✕ stop</span>':''}</div>
    <div class="row muted" style="font-size:11px" id="dwHint">${t._drawTool
      ?'click the chart to place points':'pick a tool, then click the chart — 2 clicks for lines/zones, 1 for levels/dates/labels'}</div>
    <h4>On this chart</h4>
    <div style="max-height:180px;overflow:auto">${mine.map(a=>
      `<div class="row" style="font-size:11px"><span class="chip" style="font-size:9px;border-color:${esc(a.color||'var(--line2)')}">${esc(a.kind)}</span>
       ${esc((a.text||'').slice(0,26))||'<span class="muted">untitled</span>'}
       <span class="muted" style="font-size:9px">${esc(a.author)}</span>
       <span style="flex:1"></span><button class="ghost" data-adel="${esc(a.id)}">✕</button></div>`).join('')
      ||'<div class="row muted" style="font-size:11px">nothing drawn yet</div>'}</div>
    <div class="row"><button class="ghost danger" id="dwClear">clear my drawings</button>
      <span style="flex:1"></span><button class="ghost" id="popClose">done</button></div>`;
  const p=popAt(anchor,html);
  p.querySelectorAll('[data-dt]').forEach(ch=>ch.onclick=()=>{
    t._drawTool=ch.dataset.dt||null; t._drawPts=[];
    t.el.querySelector('.bDraw').classList.toggle('on',!!t._drawTool);
    closePop();
    if(t._drawTool) toast('✏ '+t._drawTool+' — click the chart'+(DRAW_TOOLS.find(x=>x[0]===t._drawTool)[2]===2?' twice':''),'ok');
  });
  p.querySelectorAll('[data-adel]').forEach(b=>b.onclick=async()=>{
    await api('/markets/annotate/remove','POST',{id:b.dataset.adel});
    tileEvents(t); closePop();
  });
  p.querySelector('#dwClear').onclick=async()=>{
    await api('/markets/annotate/remove','POST',{symbol_key:t.key,author:'user'});
    tileEvents(t); closePop();
  };
  p.querySelector('#popClose').onclick=closePop;
}
async function drawClick(t,e){
  const tool=t._drawTool;
  if(!tool||!t.chart||!t.chart.coordsAt) return;
  const rect=e.currentTarget.getBoundingClientRect();
  const c=t.chart.coordsAt(e.clientX-rect.left,e.clientY-rect.top);
  if(!c) return;
  const need=(DRAW_TOOLS.find(x=>x[0]===tool)||[])[2]||1;
  t._drawPts=t._drawPts||[];
  t._drawPts.push(c);
  if(t._drawPts.length<need){
    /* live preview of the pending first point */
    t.chart.setMarkers((t.chart.markers||[]).concat([{t:c.t,kind:'buy',_born:performance.now()}]));
    return;
  }
  const pts=t._drawPts.map(p2=>tool==='hline'?{p:p2.p}:tool==='vline'?{t:p2.t}:{t:p2.t,p:p2.p});
  t._drawPts=[];
  let text='';
  if(tool==='label'||tool==='hline'||tool==='vline')
    text=prompt(tool==='label'?'label text':'name (optional)','')||'';
  const r=await api('/markets/annotate/add','POST',
    {symbol_key:t.key,kind:tool,points:pts,text,author:'user'});
  if(r&&r.ok){ t._drawTool=null;
    t.el.querySelector('.bDraw').classList.remove('on');
    tileEvents(t); toast('drawn — saved to '+esc(t.key),'ok');
    if(t.overlay&&t.overlay.on) tileStratOverlay(t); else t.chart.setMarkers([]);
  } else toast(esc((r&&r.error)||'draw failed'),'err');
}

/* ── 📌 infographic tiles in the Charts area ── */
function addIgTile(igId){
  const id='t'+(++TILE_SEQ);
  const el=document.createElement('div'); el.className='tile'; el.dataset.id=id;
  el.innerHTML=`<div class="tileBar"><span class="sym">📊</span>
    <span class="px igName muted"></span><span class="sp"></span>
    <button class="tIco bX" title="close">✕</button></div>
    <div class="tileBody" style="overflow:auto;padding:10px;position:relative"></div>`;
  $('tiles').appendChild(el);
  const stub={_resize(){},invalidate(){},setCross(){},destroy(){},setMarkers(){},
    setVLines(){},setAnns(){},setRegime(){},setPivots(){},setPosition(){},opts:{}};
  const t={id,el,chart:stub,kind:'ig',ig_id:igId,key:null,tf:'1d',inds:[],
    compare:[],overlay:{on:false},regime:{on:false},pivots:{on:false},events:false};
  TILES[id]=t;
  el.querySelector('.bX').onclick=()=>removeTile(id);
  el.addEventListener('mousedown',()=>{S.activeTile=id;});
  renderIgTile(t); applyGrid();
  return t;
}
async function renderIgTile(t){
  const r=await api('/markets/infographic/list');
  const ig=((r&&r.infographics)||[]).find(x=>x.id===t.ig_id);
  const body=t.el.querySelector('.tileBody');
  if(!ig){ body.innerHTML='<div class="empty">infographic deleted</div>'; return; }
  t.el.querySelector('.igName').textContent=(ig.spec&&ig.spec.title)||ig.name;
  body.innerHTML=`<div class="igPanels">${((ig.spec&&ig.spec.panels)||[])
    .map((p,k)=>igPanelHtml(p,'tile_'+t.id,k)).join('')}</div>`;
  ((ig.spec&&ig.spec.panels)||[]).forEach((p,k)=>igPanelDraw(p,'tile_'+t.id,k));
}
const _tileQuoteBase=tileQuote;
tileQuote=function(t){ if(t.kind==='ig') return; _tileQuoteBase(t); };
const _layoutDataBase=layoutData;
layoutData=function(){ const d=_layoutDataBase();
  d.tiles=Object.values(TILES).map(t=>t.kind==='ig'
    ?{kind:'ig',ig_id:t.ig_id}
    :{key:t.key,tf:t.tf,style:t.style,log:t.log,inds:t.inds,compare:t.compare,
      regime:t.regime,pivots:t.pivots,overlay:t.overlay,events:t.events});
  return d; };
const _applyLayoutBase=applyLayout;
applyLayout=async function(data){
  const igs=(data.tiles||[]).filter(td=>td.kind==='ig');
  data={...data,tiles:(data.tiles||[]).filter(td=>td.kind!=='ig')};
  await _applyLayoutBase(data);
  igs.forEach(td=>addIgTile(td.ig_id));
};

/* ── ▤ watchlist spark strip on the Charts view ── */
async function renderStrip(){
  if(!$('ckStrip').checked){ $('sparkStrip').style.display='none'; return; }
  $('sparkStrip').style.display='block';
  if(!S.pulse) await loadPulse();
  const assets=allAssets();
  $('sparkStrip').innerHTML=assets.map(a=>
    `<div class="aChip" data-key="${esc(a.key)}" style="display:inline-flex;margin-right:6px">
      <div><div class="s">${esc(a.symbol)}</div>
        <div class="c ${((a.chg_1d??0)>=0)?'up':'dn'}">${fmtPct(a.chg_1d)}</div></div>
      <canvas data-ss="${esc(a.key)}"></canvas></div>`).join('')
    ||'<span class="muted" style="font-size:11px">no tracked assets yet</span>';
  $('sparkStrip').querySelectorAll('.aChip').forEach(el=>el.onclick=()=>{
    openInNewTile(el.dataset.key);
  });
  assets.forEach(a=>{ const cv=$('sparkStrip').querySelector(`[data-ss="${CSS.escape(a.key)}"]`);
    if(cv&&a.spark) drawSpark(cv,a.spark,(a.chg_1d??0)>=0); });
}
$('ckStrip').onchange=renderStrip;
if(pref('strip')){ $('ckStrip').checked=true; setTimeout(renderStrip,1500); }
$('ckStrip').addEventListener('change',()=>pref('strip',$('ckStrip').checked));

/* ── Pulse: extra market internals + OSINT cards ── */
const _renderPulseBase=renderPulse;
renderPulse=function(){
  _renderPulseBase();
  if(!S.pulse) return;
  const assets=allAssets();
  if(assets.length){
    const nearHi=assets.filter(a=>(a.range_52w_pct??0)>=90).length;
    const os=assets.filter(a=>a.rsi!=null&&a.rsi<30).length;
    const ob=assets.filter(a=>a.rsi!=null&&a.rsi>70).length;
    const vols=assets.map(a=>a.vol_30d_pct).filter(v=>v!=null);
    const medVol=vols.length?vols.sort((a,b)=>a-b)[Math.floor(vols.length/2)]:null;
    $('pulseHero').insertAdjacentHTML('beforeend',
      gaugeHtml('near 52w highs',Math.round(nearHi/assets.length*100)+'%',
        nearHi/assets.length*100,'var(--up)',nearHi+' assets in the top decile')+
      gaugeHtml('RSI extremes',`${os}↓ ${ob}↑`,
        (os+ob)/assets.length*100,(ob>os?'var(--dn)':'var(--up)'),
        os+' oversold · '+ob+' overbought')+
      gaugeHtml('median 30d vol',(medVol??'—')+'%',Math.min(100,(medVol||0)*1.4),
        (medVol||0)>40?'var(--dn)':'var(--warn)','annualised, all tracked'));
  }
  mountOsintCards();
};
let _osintMounted=false;
function mountOsintCards(){
  if(_osintMounted){ refreshDynCard(); return; }
  _osintMounted=true;
  const host=$('pulseBody');
  const div=document.createElement('div');
  div.className='grid2'; div.style.margin='14px 0'; div.id='osintRow';
  div.innerHTML=`
    <div class="card"><h4 class="sec" style="margin-top:0;display:flex;gap:8px">⚖ Positioning — open longs vs shorts
      <span style="flex:1"></span><select id="dynSym" style="font-size:10px;width:110px">
        <option>BTC/USDT</option><option>ETH/USDT</option><option>SOL/USDT</option></select>
      <button class="ghost" id="dynGo" style="font-size:10px">↻</button></h4>
      <div id="dynCard" class="muted" style="font-size:11.5px">loading live futures positioning…</div>
      <div class="formRow" style="margin-top:6px"><button class="ghost" id="dynTrack" style="font-size:10px">⇊ store as chartable/backtestable series</button></div>
    </div>
    <div class="card"><h4 class="sec" style="margin-top:0;display:flex;gap:8px">🗣 WSB alpha
      <span style="flex:1"></span><button class="ghost" id="wsbGo" style="font-size:10px">scan now</button></h4>
      <div id="wsbCard" class="muted" style="font-size:11.5px">scan reddit for retail buzz — top tickers become chartable series</div>
    </div>
    <div class="card" style="grid-column:1/-1"><h4 class="sec" style="margin-top:0;display:flex;gap:8px">📰 News &amp; sentiment
      <span style="flex:1"></span><button class="ghost" id="newsGo" style="font-size:10px">↻ market news</button></h4>
      <div id="newsCard" class="muted" style="font-size:11.5px"></div>
    </div>`;
  host.insertBefore(div,$('pulseSectors'));
  $('dynGo').onclick=refreshDynCard;
  $('dynSym').onchange=refreshDynCard;
  $('dynTrack').onclick=async()=>{
    const r=await api('/markets/dynamics/fetch','POST',{symbol:$('dynSym').value});
    toast(r&&r.ok?'positioning series fetching — layer them via ⧉ or use {dataset:…} in strategies':'failed',r&&r.ok?'ok':'err');
  };
  $('wsbGo').onclick=wsbScan;
  $('newsGo').onclick=loadMarketNews;
  refreshDynCard(); loadMarketNews();
}
async function refreshDynCard(){
  const r=await api('/markets/dynamics/snapshot?symbol='+encodeURIComponent($('dynSym').value));
  const el=$('dynCard'); if(!el) return;
  if(!r||r.error){ el.textContent=(r&&r.error)||'unavailable'; return; }
  const acc=r.accounts||{},top=r.top_traders||{};
  const bar=(lp,label)=>`
    <div style="margin:6px 0"><div style="display:flex;justify-content:space-between;font-size:10px">
      <span class="up">▲ long ${lp??'—'}%</span><span class="muted">${label}</span>
      <span class="dn">${lp!=null?(100-lp).toFixed(1):'—'}% short ▼</span></div>
    <div class="pbar" style="height:10px"><i style="width:${lp||50}%;background:linear-gradient(90deg,var(--up),color-mix(in srgb,var(--up) 40%,var(--dn)))"></i></div></div>`;
  el.innerHTML=`
    <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:12px" class="num">
      <span>funding <b class="${(r.funding_pct_8h||0)>=0?'up':'dn'}">${r.funding_pct_8h}%/8h</b></span>
      <span>mark <b>${fmtPx(r.mark_price)}</b></span>
      <span>OI <b>${fmtN(r.open_interest)}</b></span></div>
    ${acc.long_pct!=null?bar(acc.long_pct,'all accounts'):''}
    ${top.long_pct!=null?bar(top.long_pct,'top traders (positions)'):''}
    <div class="muted" style="font-size:10px">${(r.funding_pct_8h||0)>0.03?'⚠ longs paying up — crowded long':(r.funding_pct_8h||0)<-0.01?'⚠ shorts paying — squeeze fuel':'funding neutral'}</div>`;
}
async function wsbScan(){
  $('wsbCard').innerHTML='<span class="spin"></span> scanning reddit…';
  const r=await api('/markets/wsb/scan','POST',{},120000);
  if(!r||r.error){ $('wsbCard').textContent=(r&&r.error)||'scan failed'; return; }
  const mx=Math.max(...r.ranking.map(x=>x.score),1);
  $('wsbCard').innerHTML=
    `<div class="muted" style="font-size:10px;margin-bottom:4px">${r.scanned_posts} posts scanned · ${(r.asof||'').slice(11,16)}Z</div>`+
    r.ranking.slice(0,10).map(x=>`
      <div style="display:flex;align-items:center;gap:7px;font-size:11px;margin-top:3px" title="${esc(x.top_post||'')}">
        <b style="width:46px">${esc(x.ticker)}</b>
        <div class="pbar" style="flex:1;height:7px"><i style="width:${x.score/mx*100}%;background:var(--warn)"></i></div>
        <span class="num muted">${x.mentions}× · ${fmtN(x.ups)}▲</span></div>`).join('');
}
async function loadMarketNews(){
  $('newsCard').innerHTML='<span class="spin"></span> fetching headlines…';
  const [news,sent]=await Promise.all([
    api('/markets/news/feed','POST',{},90000),
    api('/markets/sentiment/map')]);
  let html='';
  if(news&&news.headlines)
    html+=news.headlines.slice(0,6).map(h=>
      `<div class="macroRow"><a href="${esc(h.url)}" target="_blank" rel="noopener"
        style="color:var(--ink);text-decoration:none">${esc(h.title)}</a></div>`).join('');
  else html+='<div class="muted">'+esc((news&&news.error)||'news unavailable')+'</div>';
  const bm=((sent&&sent.benchmarks)||[]).filter(b=>b.score!=null);
  if(bm.length)
    html+='<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:7px">'+
      bm.map(b=>`<span class="chip ${b.score>0.15?'bull':b.score<-0.15?'bear':''}" title="${esc(b.summary||'')}">${esc(b.name)} ${b.score>0?'+':''}${(+b.score).toFixed(2)}</span>`).join('')+'</div>';
  html+='<div class="muted" style="font-size:10px;margin-top:5px">sentiment history feeds backtests: '
    +'run markets.sentiment.to_series on an asset, then use its dataset as a strategy operand.</div>';
  $('newsCard').innerHTML=html;
}

/* ── 🩻 Portfolio X-ray in the Project view ── */
(function mountXray(){
  const bar=$('pjGo').parentNode;
  const b=document.createElement('button');
  b.id='pjXray'; b.textContent='🩻 x-ray';
  bar.insertBefore(b,$('pjGo').nextSibling);
  b.onclick=portfolioXray;
})();
async function portfolioXray(){
  $('pjInfo').innerHTML='<span class="spin"></span>';
  const src=$('pjSource').value;
  let positions=[],cash=0,total=0,histSeries=null;
  if(src.startsWith('sim:')){
    await loadSim();
    const a=S.simAccounts.find(x=>'sim:'+x.id===src);
    if(!a){ $('pjInfo').textContent='account not found'; return; }
    positions=(a.positions||[]).filter(p=>p.qty>0).map(p=>({key:p.symbol_key,
      value:p.value,pnl:p.unrealized,realized:p.realized}));
    cash=a.cash; total=a.value;
    const eq=await api('/markets/sim/equity?account_id='+a.id+'&limit=500');
    if(eq&&eq.t&&eq.t.length>1) histSeries={t:eq.t.map(x=>Math.floor(new Date(x).getTime()/1000)),v:eq.value};
  } else {
    const r=await api('/markets/portfolio/positions');
    positions=((r&&r.positions)||[]).filter(p=>p.qty>0).map(p=>({key:p.symbol_key,
      value:p.market_value||0,pnl:p.unrealized_pnl,realized:p.realized_pnl}));
    total=(r&&r.totals&&r.totals.value)||0;
    const h=await api('/markets/portfolio/history?days=365');
    if(h&&h.t&&h.t.length>1) histSeries={t:h.t,v:h.value};
  }
  $('pjInfo').textContent='';
  if(!positions.length&&!cash){ toast('nothing to x-ray in this source','err'); return; }
  const hhi=positions.reduce((s,p)=>s+Math.pow(p.value/Math.max(1,total),2),0);
  const top=positions.slice().sort((a,b)=>b.value-a.value)[0];
  let host=$('pjXrayOut');
  if(!host){ host=document.createElement('div'); host.id='pjXrayOut';
    $('pjAssets').after(host); }
  host.innerHTML=`<h4 class="sec">🩻 portfolio x-ray</h4>
    <div class="statTiles">
      <div class="stat"><div class="v num">$${fmtN(total)}</div><div class="l">total value</div></div>
      <div class="stat"><div class="v num">${positions.length}</div><div class="l">positions</div></div>
      <div class="stat"><div class="v num ${hhi>0.35?'dn':'up'}">${(hhi*100).toFixed(0)}</div><div class="l">concentration (HHI)</div></div>
      <div class="stat"><div class="v num">${top?esc(top.key.split(':').pop()):'—'}</div><div class="l">top holding ${top?Math.round(top.value/Math.max(1,total)*100)+'%':''}</div></div>
      <div class="stat"><div class="v num">$${fmtN(cash)}</div><div class="l">cash</div></div>
    </div>
    <div class="grid2">
      <div class="chartBox" style="height:190px"><span class="cap">allocation</span><canvas id="xrDonut"></canvas></div>
      <div class="chartBox" style="height:190px"><span class="cap">unrealized P&amp;L by position</span><canvas id="xrPnl"></canvas></div>
    </div>
    ${histSeries?'<div class="chartBox" style="height:170px;margin-top:10px"><span class="cap">value history</span><canvas id="xrHist"></canvas></div>':''}`;
  igPanelDraw({type:'donut',data:positions.map(p=>p.value).concat(cash>0?[cash]:[]),
    labels:positions.map(p=>p.key.split(':').pop()).concat(cash>0?['cash']:[])},'xr',0);
  const dn=$('xrDonut'); /* reuse donut painter onto our canvas */
  if(dn){ dn.id='igc_xr_0'; igPanelDraw({type:'donut',
    data:positions.map(p=>p.value).concat(cash>0?[cash]:[]),
    labels:positions.map(p=>p.key.split(':').pop()).concat(cash>0?['cash']:[])},'xr',0); }
  const pnlCv=$('xrPnl');
  if(pnlCv){ pnlCv.id='igc_xr_1'; igPanelDraw({type:'bars',
    data:positions.map(p=>p.pnl||0),labels:positions.map(p=>p.key)},'xr',1); }
  if(histSeries) drawSeries($('xrHist'),{series:[{t:histSeries.t,v:histSeries.v,
    color:COL.acc,width:1.6,fill:true}],fmt:v=>'$'+fmtN(v),animate:true});
}

/* ── sim template picker ── */
$('btnSimNew').onclick=async()=>{
  const r=await api('/markets/sim/templates');
  const tpls=(r&&r.templates)||[];
  const p=popAt($('btnSimNew'),`<h4>＋ new sim account</h4>
    <div class="row"><input id="stName" placeholder="account name" style="flex:1"></div>
    <div class="row"><span class="muted" style="font-size:11px">cash $</span>
      <input id="stCash" type="number" placeholder="template default" style="flex:1"></div>
    <h4>profile</h4>
    ${tpls.map(t2=>`<div class="row" style="cursor:pointer" data-tpl="${t2.id}">
      <b style="width:120px">${esc(t2.name)}</b>
      <span class="muted" style="font-size:10.5px;flex:1">${esc(t2.desc)}</span></div>`).join('')}
    <div class="row" style="cursor:pointer" data-tpl=""><b style="width:120px">Blank</b>
      <span class="muted" style="font-size:10.5px;flex:1">cash only, no template</span></div>`);
  p.querySelectorAll('[data-tpl]').forEach(row=>row.onclick=async()=>{
    const name=p.querySelector('#stName').value.trim()||
      (tpls.find(x=>x.id===row.dataset.tpl)||{}).name||'paper';
    const body={name,template_id:row.dataset.tpl||''};
    const cashV=parseFloat(p.querySelector('#stCash').value);
    if(isFinite(cashV)&&cashV>0) body.cash=cashV;
    closePop();
    toast('creating '+esc(name)+'…');
    const r2=await api('/markets/sim/create','POST',body,120000);
    if(r2&&r2.ok){ toast('account ready'+(r2.seeded&&r2.seeded.length?' — seeded '+r2.seeded.length+' positions':''),'ok');
      if(r2.skipped&&r2.skipped.length) toast('skipped (no stored price): '+r2.skipped.map(s=>s.symbol_key).join(', '),'err');
      S.simSel=r2.id; loadSim(); }
    else toast(esc((r2&&r2.error)||'failed'),'err');
  });
};

/* ── 🤖 copilot v2: chat agent + agentic loop, specialist agents, skill ── */
const COP={mode:'loop',agent:'quant-strategist',hist:[]};
(function copV2(){
  const head=$('copHead'); if(!head) return;
  const ctl=document.createElement('span');
  ctl.style.cssText='display:flex;gap:4px;align-items:center';
  ctl.innerHTML=`
    <span class="chip on" id="copModeLoop" style="cursor:pointer">⟳ loop</span>
    <span class="chip" id="copModeChat" style="cursor:pointer">💬 chat</span>
    <select id="copAgent" style="font-size:10px;max-width:130px">
      <option value="quant-strategist">📈 quant-strategist</option>
      <option value="market-visualizer">📊 market-visualizer</option>
      <option value="indicator-smith">ƒ indicator-smith</option>
    </select>`;
  head.insertBefore(ctl,head.querySelector('span[style]'));
  const sync=()=>{
    $('copModeLoop').classList.toggle('on',COP.mode==='loop');
    $('copModeChat').classList.toggle('on',COP.mode==='chat');
    const chips=document.querySelector('#copDock .qchips');
    chips.innerHTML=(COP.mode==='chat'
      ?[['How is the market looking today?','market read'],
        ['Explain my last backtest results and what to try next','explain backtest'],
        ['Which of my strategies deserves live monitoring?','pick a strategy'],
        ['What does BTC positioning (funding, longs vs shorts) say right now?','positioning']]
      :[['Screen the whole strategy library across my watchlist on 1d bars, autotune the top 3 and report out-of-sample stats.','🏆 best plays'],
        ["Build me an infographic summarising the market today: breadth, sector moves, positioning and anything unusual.",'📊 infographic'],
        ['Invent a custom indicator for trend quality, test it, put it on my chart and backtest a strategy around it.','ƒx invent'],
        ['Draw support/resistance and the current regime on my active chart.','✏ annotate chart']]
      ).map(([q,l])=>`<span class="chip" data-q="${esc(q)}">${l}</span>`).join('');
    chips.querySelectorAll('.chip').forEach(ch=>ch.onclick=()=>{
      $('copText').value=ch.dataset.q; copSend(); });
  };
  $('copModeLoop').onclick=()=>{COP.mode='loop';sync();};
  $('copModeChat').onclick=()=>{COP.mode='chat';sync();};
  $('copAgent').onchange=e=>{COP.agent=e.target.value;COP.hist=[];};
  sync();
})();
/* chat mode — /agents/chat/stream with the specialist agent */
async function copChatSend(text){
  copAdd('user',esc(text)); COP.hist.push({role:'user',content:text});
  const holder=copAdd('bot','<span class="spin"></span>');
  _copBusy=true; $('copSend').style.display='none'; $('copStop').style.display='';
  _copAbort=new AbortController();
  let acc='',think='';
  try{
    const r=await fetch(BASE+'/agents/chat/stream',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text,agent_name:COP.agent,
        history:JSON.stringify(COP.hist.slice(-14)),session_id:COP_SID,
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
            holder.innerHTML=(think?'<div class="muted" style="font-size:10px;border-left:2px solid var(--line2);padding-left:6px;margin-bottom:4px">'+esc(think)+'</div>':'')
              +esc(acc).replace(/\n/g,'<br>'); $('copLog').scrollTop=1e9; }
          else if(ev.type==='thinking'&&ev.text){ think+=ev.text; }
          else if(ev.type==='error'){ holder.innerHTML='<span class="dn">'+esc(ev.text||'error')+'</span>'; }
        } }
    }
    if(acc) COP.hist.push({role:'assistant',content:acc});
  }catch(e){ if(String(e).indexOf('bort')<0)
    holder.innerHTML='<span class="dn">'+esc(String(e))+'</span>'; }
  finally{ copDone(); }
}
/* loop mode — v6 with the specialist agent + the quant-visuals skill attached */
const _copLoopSend=copSend;
copSend=async function(){
  const text=$('copText').value.trim();
  if(!text||_copBusy) return;
  if(COP.mode==='chat'){ $('copText').value=''; return copChatSend(text); }
  return _copLoopSend();
};
/* upgrade the loop request in place: v6 + specialist agent + skill */
(function patchLoopReq(){
  const origFetch=window.fetch.bind(window);
  window.fetch=function(url,opts){
    try{
      if(typeof url==='string'&&url.indexOf('/workshop/agent_loop/stream')>=0&&
         opts&&opts.body&&String(opts.body).indexOf('"run_id":"qs_')>=0){
        const b=JSON.parse(opts.body);
        b.version='v6'; b.agent_name=COP.agent;
        b.attach_skills='sys-quant-visuals';
        b.loop_profile='markets-quant';
        b.record_agent_name=COP.agent;
        opts=Object.assign({},opts,{body:JSON.stringify(b)});
      }
    }catch(_){}
    return origFetch(url,opts);
  };
})();

/* ── 🧬 self-improve loop (evolve) in the Run Center ── */
(function mountEvolve(){
  const host=$('runLeft'); if(!host) return;
  const div=document.createElement('div');
  div.innerHTML=`
    <h4 class="sec">🧬 Self-improve loop</h4>
    <div class="card" id="evCard">
      <div id="evStatus" class="muted" style="font-size:11.5px">…</div>
      <div class="formRow" style="margin-top:6px">
        <button class="pri" id="evToggle">start</button>
        <button id="evTick" title="run one improvement iteration now">tick now</button>
        <label style="min-width:0">every</label>
        <input id="evInterval" type="number" value="180" style="width:56px"><span class="muted">min</span></div>
      <div class="formRow"><label class="chip" title="also periodically run Loop-Lab improve sessions on the markets agent loop"><input type="checkbox" id="evImprove" checked> improve the agent loop too</label></div>
      <div id="evBoard" style="font-size:10.5px;margin-top:5px"></div>
    </div>`;
  host.appendChild(div);
  $('evToggle').onclick=async()=>{
    const running=$('evToggle').dataset.running==='1';
    const r=await api('/markets/evolve/'+(running?'stop':'start'),'POST',{});
    if(r&&r.ok){ toast(running?'self-improve loop stopped':'🧬 self-improve loop running — it will sweep, tune and promote strategies on its own','ok'); evLoad(); }
  };
  $('evTick').onclick=async()=>{
    $('evTick').innerHTML='<span class="spin"></span>';
    const r=await api('/markets/evolve/tick','POST',{},600000);
    $('evTick').textContent='tick now';
    toast(r&&!r.error?'iteration done':'tick failed',r&&!r.error?'ok':'err');
    evLoad();
  };
  $('evInterval').onchange=async e=>{
    await api('/markets/evolve/config/set','POST',{interval_minutes:+e.target.value||180});
  };
  $('evImprove').onchange=async e=>{
    await api('/markets/evolve/config/set','POST',{improve_agent_loop:e.target.checked});
  };
})();
async function evLoad(){
  const r=await api('/markets/evolve/status');
  if(!r||r.error){ $('evStatus').textContent='evolve unavailable'; return; }
  const cfg=r.config||{};
  $('evToggle').dataset.running=r.running?'1':'0';
  $('evToggle').textContent=r.running?'stop':'start';
  $('evToggle').classList.toggle('pri',!r.running);
  $('evInterval').value=cfg.interval_minutes||180;
  $('evImprove').checked=cfg.improve_agent_loop!==false;
  $('evStatus').innerHTML=(r.running
    ?'<span class="up">● running</span> — hill-climbing strategy params via sweeps, promoting winners to live monitoring'
    :'<span class="muted">○ stopped</span> — turn it on to continuously tune indicators-parameters/strategies and retrain the loop')
    +(r.tick_running?' · <span class="spin"></span> iterating now':'');
  const board=(r.leaderboard||[]).slice(0,5);
  $('evBoard').innerHTML=board.length
    ?'<span class="muted" style="font-size:9.5px;text-transform:uppercase;letter-spacing:.5px">leaderboard</span>'+
     board.map(b=>`<div style="display:flex;gap:6px;margin-top:2px">
       <span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(b.name||b.strategy_id)}</span>
       <b class="num">${(b.metric??0).toFixed?(b.metric).toFixed(3):b.metric}</b></div>`).join('')
    :'';
}
const _svBase4=switchView;
switchView=function(n){ _svBase4(n); if(n==='run') setTimeout(evLoad,600); };
/* ig tiles refresh on infographic events */
const _handleEvR4=handleEvent;
handleEvent=function(ev){
  if(String(ev.type||'')==='markets.infographic')
    Object.values(TILES).filter(t=>t.kind==='ig').forEach(renderIgTile);
  if(String(ev.type||'')==='markets.osint'&&S.view==='pulse'&&ev.stage==='wsb_scan')
    toast('🗣 WSB scan done — top: '+esc((ev.top||[]).join(', ')),'ok');
  _handleEvR4(ev);
};
