
'use strict';
/* ═══════════════════ app state ═══════════════════ */
const S={ view:'charts', watch:[], quotes:{}, grid:pref('grid')||2, sync:true,
  customInds:[], mlModels:[], macro:[], strategies:[], activeTile:null,
  library:[], curStrat:null, backtests:[], compareSel:[], simAccounts:[], simSel:null,
  pulse:null, pulseWin:'chg_1d' };
const TILES={}; let TILE_SEQ=0;

const TF_ALL=["1m","5m","15m","30m","1h","4h","1d","1w"];
/* client indicator catalog (params + panes + guides) — mirrors the backend */
const IND_DEFS={
  sma:{l:'SMA',pane:'main',p:{n:50}}, ema:{l:'EMA',pane:'main',p:{n:20}},
  hma:{l:'Hull MA',pane:'main',p:{n:21}}, tema:{l:'TEMA',pane:'main',p:{n:21}},
  ribbon:{l:'MA Ribbon',pane:'main',p:{ma:'ema'}},
  bbands:{l:'Bollinger',pane:'main',p:{n:20,k:2}},
  keltner:{l:'Keltner',pane:'main',p:{n:20,mult:2,atr_n:10}},
  donchian:{l:'Donchian',pane:'main',p:{n:20}},
  supertrend:{l:'Supertrend',pane:'main',p:{n:10,mult:3}},
  ichimoku:{l:'Ichimoku',pane:'main',p:{conv:9,base:26,spanb:52}},
  psar:{l:'PSAR',pane:'main',p:{af:0.02,max_af:0.2}},
  vwap:{l:'VWAP',pane:'main',p:{n:0}},
  rsi:{l:'RSI',pane:'sub',p:{n:14},guides:[30,70],range:[0,100]},
  stoch:{l:'Stochastic',pane:'sub',p:{k:14,d:3,smooth:3},guides:[20,80],range:[0,100]},
  macd:{l:'MACD',pane:'sub',p:{fast:12,slow:26,signal:9},hist:'macd_hist'},
  atr:{l:'ATR',pane:'sub',p:{n:14}},
  adx:{l:'ADX / DI',pane:'sub',p:{n:14},guides:[20]},
  cci:{l:'CCI',pane:'sub',p:{n:20},guides:[-100,100]},
  mfi:{l:'MFI',pane:'sub',p:{n:14},guides:[20,80],range:[0,100]},
  willr:{l:'Williams %R',pane:'sub',p:{n:14},guides:[-80,-20],range:[-100,0]},
  zscore:{l:'Z-Score',pane:'sub',p:{n:20},guides:[-2,0,2]},
  obv:{l:'OBV',pane:'sub',p:{}}, roc:{l:'ROC',pane:'sub',p:{n:10},guides:[0]},
};

/* ═══════════════════ tiles ═══════════════════ */
function addTile(key,tf){
  const id='t'+(++TILE_SEQ);
  const el=document.createElement('div'); el.className='tile'; el.dataset.id=id;
  el.innerHTML=`
    <div class="tileBar">
      <span class="sym" title="click to change asset">—</span>
      <span class="px num"></span><span class="chg num"></span>
      <div class="tfRow tfs"></div>
      <span class="sp"></span>
      <button class="tIco bStyle" title="chart style">🕯</button>
      <button class="tIco bLog" title="log scale">㏒</button>
      <button class="tIco bInd" title="indicators">☰</button>
      <button class="tIco bCmp" title="layer / compare series">⧉</button>
      <button class="tIco bFit" title="trend fit &amp; pivots">📐</button>
      <button class="tIco bStrat" title="live strategy overlay — buy/sell signals on this chart">🎯</button>
      <button class="tIco bDraw" title="draw on this chart">✏</button>
      <button class="tIco bEvt on" title="key dates">⚑</button>
      <button class="tIco bMax" title="maximize">⛶</button>
      <button class="tIco bX" title="close">✕</button>
    </div>
    <div class="tileBody"></div>`;
  $('tiles').appendChild(el);
  const chart=new QChart(el.querySelector('.tileBody'));
  const t={ id, el, chart, key:null, provider:null, symbol:null, tf:tf||'1d',
    style:'candles', log:false, events:true,
    inds:[{kind:'ema',params:{n:20},enabled:true},{kind:'ema',params:{n:50},enabled:true},
          {kind:'rsi',params:{n:14},enabled:true}],
    compare:[], regime:{on:false,detail:35},
    pivots:{on:false,method:'zigzag',pct:5,mult:3,n:2,detail:50},
    overlay:{on:false,strategy_id:''} };
  TILES[id]=t;
  chart.onHover=tt=>{ if($('ckSync').checked)
    Object.values(TILES).forEach(o=>{ if(o!==t) o.chart.setCross(tt); }); };
  el.addEventListener('mousedown',()=>{S.activeTile=id;});
  const q=sel=>el.querySelector(sel);
  q('.sym').onclick=()=>{$('gsIn').focus();};
  q('.bX').onclick=()=>removeTile(id);
  q('.bMax').onclick=()=>{el.classList.toggle('max'); setTimeout(()=>chart._resize(),60);};
  q('.bStyle').onclick=()=>{ t.style=t.style==='candles'?'line':t.style==='line'?'area':'candles';
    q('.bStyle').textContent=t.style==='candles'?'🕯':t.style==='line'?'╱':'▨';
    chart.setStyle(t.style); };
  q('.bLog').onclick=()=>{ t.log=!t.log; chart.opts.log=t.log;
    q('.bLog').classList.toggle('on',t.log); chart.invalidate(); };
  q('.bInd').onclick=e=>openIndPop(t,e.currentTarget);
  q('.bCmp').onclick=e=>openCmpPop(t,e.currentTarget);
  q('.bFit').onclick=e=>openFitPop(t,e.currentTarget);
  q('.bStrat').onclick=e=>openStratOverlayPop(t,e.currentTarget);
  q('.bDraw').onclick=e=>openDrawPop(t,e.currentTarget);
  el.querySelector('.tileBody').addEventListener('mousedown',e=>{
    if(t._drawTool&&typeof drawClick==='function'){ drawClick(t,e); e.stopPropagation(); }
  },true);
  q('.bEvt').onclick=()=>{ t.events=!t.events; q('.bEvt').classList.toggle('on',t.events);
    tileEvents(t); };
  if(key) tileSetAsset(t,key,tf);
  applyGrid();
  return t;
}
function removeTile(id){ const t=TILES[id]; if(!t) return;
  t.chart.destroy(); t.el.remove(); delete TILES[id]; applyGrid(); }
function applyGrid(){
  const n=Object.keys(TILES).length||1;
  const g=S.grid;
  const cols=g===1?1:g===2?2:g===3?3:g===4?2:3;
  $('tiles').style.gridTemplateColumns=`repeat(${Math.min(cols,Math.max(1,n))},1fr)`;
  pref('grid',S.grid);
  setTimeout(()=>Object.values(TILES).forEach(t=>t.chart._resize()),60);
}
$('gridPicker').querySelectorAll('button').forEach(b=>b.onclick=()=>{
  S.grid=+b.dataset.g;
  $('gridPicker').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
  const want=S.grid, have=Object.keys(TILES).length;
  for(let i=have;i<want;i++) addTile(S.watch[i]?S.watch[i].id:null);
  applyGrid();
});
$('btnAddTile').onclick=()=>addTile(S.watch[Object.keys(TILES).length]?.id||null);
$('ckAnim').onchange=e=>{ANIM_ON=e.target.checked;};

async function tileSetAsset(t,key,tf){
  const [prov,sym]=keySplit(key);
  t.key=key; t.provider=prov; t.symbol=sym;
  if(tf) t.tf=tf;
  const row=S.watch.find(w=>w.id===key);
  const tfs=row&&row.timeframes&&row.timeframes.length?row.timeframes
    :(prov==='yahoo'?['1d','1h']:prov==='macro'?['1d']:['1d','1h']);
  if(!tfs.includes(t.tf)) t.tf=tfs[0];
  const bar=t.el.querySelector('.tileBar');
  bar.querySelector('.sym').textContent=sym;
  bar.querySelector('.tfs').innerHTML=tfs.map(x=>
    `<button class="${x===t.tf?'on':''}" data-tf="${x}">${x}</button>`).join('');
  bar.querySelectorAll('.tfs button').forEach(b=>b.onclick=()=>{
    t.tf=b.dataset.tf;
    bar.querySelectorAll('.tfs button').forEach(x=>x.classList.toggle('on',x===b));
    tileLoad(t); });
  await tileLoad(t);
}
async function tileLoad(t,keepView){
  if(!t.key) return;
  const ds=dsId(t.provider,t.symbol,t.tf);
  const r=await api('/markets/bars?dataset_id='+encodeURIComponent(ds)+'&limit=6000');
  if(!r||r.error||!r.t||!r.t.length){
    t.chart.bars=null; t.chart.invalidate();
    if(r&&r.error) toast('bars: '+esc(r.error),'err');
    return;
  }
  t.bars=r;
  t.chart.setStyle(t.style); t.chart.opts.log=t.log;
  t.chart.setBars({t:r.t,o:r.o,h:r.h,l:r.l,c:r.c,v:r.v},!keepView);
  tileQuote(t);
  tileIndicators(t); tileCompare(t); tileEvents(t); tileRegime(t); tilePivots(t);
  if(typeof tileStratOverlay==='function') tileStratOverlay(t);
}
async function tilePivots(t){
  if(!t.pivots||!t.pivots.on||!t.key){ t.chart.setPivots(null); return; }
  const ds=dsId(t.provider,t.symbol,t.tf);
  const p=t.pivots;
  const r=await api('/markets/analysis/pivots','POST',
    {dataset_id:ds,method:p.method,pct:p.pct,mult:p.mult,n:p.n,detail:p.detail,limit:6000});
  if(r&&r.ok) t.chart.setPivots(r);
  else if(r&&r.error) toast('pivots: '+esc(r.error),'err');
}
function tileQuote(t){
  const q=S.quotes[t.key];
  const bar=t.el.querySelector('.tileBar');
  const last=q&&q.last!=null?q.last:(t.bars?t.bars.c[t.bars.c.length-1]:null);
  bar.querySelector('.px').textContent=fmtPx(last);
  const chg=q?q.change_pct:(t.bars&&t.bars.c.length>1?
    (t.bars.c[t.bars.c.length-1]/t.bars.c[t.bars.c.length-2]-1)*100:null);
  const el=bar.querySelector('.chg');
  el.textContent=fmtPct(chg); el.className='chg num '+(chg>=0?'up':'dn');
}
async function tileIndicators(t){
  const active=t.inds.filter(i=>i.enabled);
  const ds=dsId(t.provider,t.symbol,t.tf);
  const overlays=[],subs=[];
  let ci=0;
  const mlInds=active.filter(i=>i.kind.startsWith('ml:'));
  const stdInds=active.filter(i=>!i.kind.startsWith('ml:'));
  if(stdInds.length){
    const r=await api('/markets/indicators','POST',{dataset_id:ds,limit:6000,
      indicators:stdInds.map((i,k)=>({id:i.kind+'_'+k,kind:i.kind,params:i.params}))});
    if(r&&!r.error&&t.bars){
      /* align: response t should match bars t (same source) — index map by ts */
      const tIdx=new Map(); t.bars.t.forEach((tt,i)=>tIdx.set(tt,i));
      (r.indicators||[]).forEach(ind=>{
        if(ind.error){ return; }
        const def=IND_DEFS[ind.kind]||{pane:ind.pane||'sub',l:ind.label||ind.kind};
        const col=stdInds[ci]&&stdInds[ci].color;
        const align=vals=>{ const out=new Array(t.bars.t.length).fill(null);
          (r.t||[]).forEach((tt,j)=>{ const bi=tIdx.get(tt); if(bi!=null) out[bi]=vals[j]; });
          return out; };
        const names=Object.keys(ind.series||{});
        const pane=ind.pane||def.pane;
        if(pane==='main'){
          names.forEach((nm,si)=>{
            overlays.push({id:ind.id+'_'+nm,label:nm,
              color:col||COL.cat[(overlays.length)%COL.cat.length],
              vals:align(ind.series[nm]),
              width:names.length>1&&si>0?1.1:1.5,
              dash:/upper|lower|senkou_b/.test(nm)?[4,3]:null});
          });
        }else{
          const series=names.map((nm,si)=>({
            name:nm, color:col||COL.cat[(subs.length+si)%COL.cat.length],
            vals:align(ind.series[nm]),
            type:(def.hist===nm||nm==='macd_hist')?'hist':'line'}));
          subs.push({id:ind.id,label:(def.l||ind.kind)+' '+paramStr(ind.params),
            series,guides:def.guides,range:def.range});
        }
        ci++;
      });
    } else if(r&&r.error) toast('indicators: '+esc(r.error),'err');
  }
  for(const mi of mlInds){
    const mid=mi.kind.slice(3);
    const r=await api('/markets/ml/series','POST',{id:mid,dataset_id:ds,limit:6000});
    if(r&&!r.error&&t.bars){
      const tIdx=new Map(); t.bars.t.forEach((tt,i)=>tIdx.set(tt,i));
      const vals=new Array(t.bars.t.length).fill(null);
      (r.t||[]).forEach((tt,j)=>{const bi=tIdx.get(tt); if(bi!=null) vals[bi]=r.signal[j];});
      subs.push({id:'ml_'+mid,label:'🧠 '+(r.name||'model'),
        series:[{name:'P(up)',color:COL.cat[2],vals}],
        guides:r.task==='classify'?[0.5]:[0],
        range:r.task==='classify'?[0,1]:null});
    }
  }
  t.chart.setOverlays(overlays); t.chart.setSubs(subs);
}
function paramStr(p){ if(!p)return''; const v=Object.values(p).filter(x=>typeof x!=='object');
  return v.length?('('+v.join(',')+')'):''; }
async function tileCompare(t){
  if(!t.compare.length){ t.chart.setCompare([]); return; }
  const out=[];
  for(const [k,cmp] of t.compare.entries()){
    const r=await api('/markets/bars?dataset_id='+encodeURIComponent(cmp.ds)+'&limit=6000');
    if(r&&!r.error&&r.t&&r.t.length)
      out.push({id:cmp.ds,label:cmp.label,color:COL.cat[(k+1)%COL.cat.length],t:r.t,v:r.c});
  }
  t.chart.setCompare(out);
}
async function tileEvents(t){
  if(!t.events||!t.key){ t.chart.setVLines([]); t.chart.setAnns&&t.chart.setAnns([]); return; }
  const r=await api('/markets/annotate/list?symbol_key='+encodeURIComponent(t.key));
  const vls=[],anns=[];
  t._annRows=((r&&r.annotations)||[]);
  t._annRows.forEach(a=>{
    if(a.kind==='vline'){
      const p=(a.points||[]).find(x=>x.t!=null);
      if(p) vls.push({t:p.t,color:a.color,label:a.text});
    } else anns.push(a);
  });
  t.chart.setVLines(vls);
  if(t.chart.setAnns) t.chart.setAnns(anns);
}
async function tileRegime(t){
  if(!t.regime.on||!t.key){ t.chart.setRegime(null); return; }
  const ds=dsId(t.provider,t.symbol,t.tf);
  const r=await api('/markets/analysis/trendfit','POST',
    {dataset_id:ds,detail:t.regime.detail,limit:6000,log_scale:true});
  if(r&&!r.error) t.chart.setRegime(r);
}

/* ── popovers ── */
let _pop=null;
function popAt(anchor,html){
  closePop();
  const p=document.createElement('div'); p.className='pop'; p.innerHTML=html;
  document.body.appendChild(p);
  const r=anchor.getBoundingClientRect();
  p.style.top=Math.min(window.innerHeight-p.offsetHeight-10,r.bottom+6)+'px';
  p.style.left=Math.max(8,Math.min(window.innerWidth-340,r.right-330))+'px';
  _pop=p;
  setTimeout(()=>document.addEventListener('mousedown',popOutside),0);
  return p;
}
function popOutside(e){ if(_pop&&!_pop.contains(e.target)) closePop(); }
function closePop(){ if(_pop){_pop.remove();_pop=null;
  document.removeEventListener('mousedown',popOutside);} }

function openIndPop(t,anchor){
  const groups=[['Price overlays',Object.keys(IND_DEFS).filter(k=>IND_DEFS[k].pane==='main')],
                ['Oscillators',Object.keys(IND_DEFS).filter(k=>IND_DEFS[k].pane==='sub')]];
  let html='<h4>Active indicators</h4><div class="actList"></div>';
  html+='<h4>Add indicator</h4>';
  groups.forEach(([g,kinds])=>{
    html+=`<div class="row" style="font-size:10px;color:var(--faint)">${g}</div>`;
    html+='<div class="row" style="flex-wrap:wrap;gap:4px">'+
      kinds.map(k=>`<span class="chip addInd" data-k="${k}">${IND_DEFS[k].l}</span>`).join('')+'</div>';
  });
  if(S.customInds.length)
    html+='<div class="row" style="font-size:10px;color:var(--faint)">Custom (ƒx lab)</div>'+
      '<div class="row" style="flex-wrap:wrap;gap:4px">'+
      S.customInds.map(cxi=>`<span class="chip addInd" data-k="${cxi.id}">${esc(cxi.name)}</span>`).join('')+'</div>';
  if(S.mlModels.filter(m=>m.status==='ready').length)
    html+='<div class="row" style="font-size:10px;color:var(--faint)">ML models</div>'+
      '<div class="row" style="flex-wrap:wrap;gap:4px">'+
      S.mlModels.filter(m=>m.status==='ready').map(m=>
        `<span class="chip addInd" data-k="ml:${m.id}">🧠 ${esc(m.name)}</span>`).join('')+'</div>';
  html+='<div class="row"><span style="flex:1"></span><button class="ghost" id="popClose">done</button></div>';
  const p=popAt(anchor,html);
  const renderActive=()=>{
    p.querySelector('.actList').innerHTML=t.inds.map((ind,k)=>{
      const def=IND_DEFS[ind.kind]||{};
      const label=ind.kind.startsWith('ml:')
        ?('🧠 '+(S.mlModels.find(m=>'ml:'+m.id===ind.kind)||{}).name)
        :(def.l||((S.customInds.find(c=>c.id===ind.kind)||{}).name)||ind.kind);
      const params=Object.entries(ind.params||{}).filter(([_,v])=>typeof v==='number')
        .map(([pk,pv])=>`<label>${pk}<input data-k="${k}" data-p="${pk}" value="${pv}"></label>`).join('');
      return `<div class="indCard"><div class="hd">
        <input type="checkbox" data-en="${k}" ${ind.enabled?'checked':''}>
        <b>${esc(label)}</b><span style="flex:1"></span>
        <button class="ghost" data-del="${k}">✕</button></div>
        ${params?`<div class="params">${params}</div>`:''}</div>`;
    }).join('')||'<div class="row muted">none — add one below</div>';
    p.querySelectorAll('[data-en]').forEach(cb=>cb.onchange=()=>{
      t.inds[+cb.dataset.en].enabled=cb.checked; tileIndicators(t); });
    p.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>{
      t.inds.splice(+b.dataset.del,1); renderActive(); tileIndicators(t); });
    p.querySelectorAll('.params input').forEach(inp=>{
      inp.onchange=()=>{ const ind=t.inds[+inp.dataset.k];
        const v=parseFloat(inp.value); if(isFinite(v)) ind.params[inp.dataset.p]=v;
        tileIndicators(t); };
    });
  };
  renderActive();
  p.querySelectorAll('.addInd').forEach(ch=>ch.onclick=()=>{
    const k=ch.dataset.k;
    const def=IND_DEFS[k];
    t.inds.push({kind:k,params:def?JSON.parse(JSON.stringify(def.p)):{},enabled:true});
    renderActive(); tileIndicators(t);
  });
  p.querySelector('#popClose').onclick=closePop;
}

function openCmpPop(t,anchor){
  let html='<h4>Layered series</h4>';
  html+=t.compare.map((c,k)=>
    `<div class="row"><span class="sw" style="width:9px;height:9px;border-radius:2px;background:${COL.cat[(k+1)%COL.cat.length]}"></span>
     ${esc(c.label)}<span style="flex:1"></span><button class="ghost" data-del="${k}">✕</button></div>`).join('')
    ||'<div class="row muted">none — layer anything below</div>';
  html+='<h4>Assets</h4><div class="row" style="flex-wrap:wrap;gap:4px">'+
    S.watch.filter(w=>w.id!==t.key&&w.exchange!=='macro').slice(0,30).map(w=>
      `<span class="chip addC" data-ds="${dsId(w.exchange,w.symbol,'1d')}" data-l="${esc(w.symbol)}">${esc(w.symbol)}</span>`).join('')+'</div>';
  html+='<h4>Macro · economics · on-chain</h4>';
  const fetched=S.macro.filter(m=>m.fetched);
  html+=fetched.length
    ?'<div class="row" style="flex-wrap:wrap;gap:4px">'+fetched.map(m=>
      `<span class="chip addC" data-ds="${m.dataset_id}" data-l="${esc(m.name)}">${esc(m.name)}</span>`).join('')+'</div>'
    :'<div class="row muted" style="font-size:11px">none fetched yet — open 🗄 data &amp; layers to pull rates, inflation, hash-rate…</div>';
  html+='<div class="row"><span style="flex:1"></span><button class="ghost" id="popClose">done</button></div>';
  const p=popAt(anchor,html);
  p.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>{
    t.compare.splice(+b.dataset.del,1); closePop(); tileCompare(t); });
  p.querySelectorAll('.addC').forEach(ch=>ch.onclick=()=>{
    if(t.compare.length>=4) return toast('max 4 layers','err');
    t.compare.push({ds:ch.dataset.ds,label:ch.dataset.l});
    closePop(); tileCompare(t); });
  p.querySelector('#popClose').onclick=closePop;
}

function openFitPop(t,anchor){
  const pv=t.pivots;
  const html=`<h4>Trend fit &amp; pivots</h4>
    <div class="row"><label class="chip"><input type="checkbox" id="fitOn" ${t.regime.on?'checked':''}> regime / trend fit</label></div>
    <div class="row"><span class="muted" style="font-size:11px">detail — coarse regimes ⟷ fine segments</span></div>
    <div class="row"><input type="range" id="fitDetail" min="0" max="100" value="${t.regime.detail}" style="flex:1">
      <span class="num" id="fitDetailV">${t.regime.detail}</span></div>
    <div class="row" id="fitInfo" style="font-size:11px"></div>
    <h4>Pivot points</h4>
    <div class="row"><label class="chip"><input type="checkbox" id="pvOn" ${pv.on?'checked':''}> show pivots</label>
      <select id="pvMethod" style="flex:1">
        <option value="zigzag"${pv.method==='zigzag'?' selected':''}>ZigZag (% reversal)</option>
        <option value="atr_zigzag"${pv.method==='atr_zigzag'?' selected':''}>ATR ZigZag (volatility-adaptive)</option>
        <option value="fractal"${pv.method==='fractal'?' selected':''}>Williams fractals</option>
        <option value="rdp"${pv.method==='rdp'?' selected':''}>RDP (path simplification)</option>
      </select></div>
    <div class="row" id="pvParams"></div>
    <div class="row muted" style="font-size:11px">Every method returns the same shape —
      ◆ high (red) / low (green) markers + the swing polyline — so new detectors
      slot straight into this menu.</div>
    <div class="row"><span style="flex:1"></span><button class="ghost" id="popClose">done</button></div>`;
  const p=popAt(anchor,html);
  const applyFit=debounce(async()=>{ await tileRegime(t);
    const r=t.chart.regime;
    if(r&&r.segments) p.querySelector('#fitInfo').innerHTML=
      `<span class="chip ${r.regime_now}">${r.regime_now} now</span>&nbsp; ${r.segments.length} segments · overall ${fmtPct(r.overall.slope_pct_year)}/yr · R² ${r.overall.r2}`;
  },300);
  const applyPv=debounce(()=>tilePivots(t),300);
  const syncBtn=()=>t.el.querySelector('.bFit').classList.toggle('on',t.regime.on||pv.on);
  const renderPvParams=()=>{
    const m=pv.method;
    p.querySelector('#pvParams').innerHTML=
      m==='zigzag'?`<span class="muted" style="font-size:11px">reversal %</span>
        <input type="range" id="pvPct" min="1" max="20" step="0.5" value="${pv.pct}" style="flex:1">
        <span class="num" id="pvPctV">${pv.pct}%</span>`
      :m==='atr_zigzag'?`<span class="muted" style="font-size:11px">ATR ×</span>
        <input type="range" id="pvMult" min="1" max="8" step="0.5" value="${pv.mult}" style="flex:1">
        <span class="num" id="pvMultV">${pv.mult}</span>`
      :m==='fractal'?`<span class="muted" style="font-size:11px">bars each side</span>
        <input type="range" id="pvN" min="1" max="8" value="${pv.n}" style="flex:1">
        <span class="num" id="pvNV">${pv.n}</span>`
      :`<span class="muted" style="font-size:11px">detail</span>
        <input type="range" id="pvDetail" min="0" max="100" value="${pv.detail}" style="flex:1">
        <span class="num" id="pvDetailV">${pv.detail}</span>`;
    const bind=(id,vid,key,suffix)=>{const el=p.querySelector('#'+id);
      if(el) el.oninput=e=>{ pv[key]=+e.target.value;
        p.querySelector('#'+vid).textContent=e.target.value+(suffix||'');
        if(pv.on) applyPv(); };};
    bind('pvPct','pvPctV','pct','%'); bind('pvMult','pvMultV','mult');
    bind('pvN','pvNV','n'); bind('pvDetail','pvDetailV','detail');
  };
  renderPvParams();
  p.querySelector('#fitOn').onchange=e=>{ t.regime.on=e.target.checked; syncBtn(); applyFit(); };
  p.querySelector('#fitDetail').oninput=e=>{ t.regime.detail=+e.target.value;
    p.querySelector('#fitDetailV').textContent=e.target.value; if(t.regime.on) applyFit(); };
  p.querySelector('#pvOn').onchange=e=>{ pv.on=e.target.checked; syncBtn();
    pv.on?applyPv():t.chart.setPivots(null); };
  p.querySelector('#pvMethod').onchange=e=>{ pv.method=e.target.value;
    renderPvParams(); if(pv.on) applyPv(); };
  p.querySelector('#popClose').onclick=closePop;
}

/* ═══════════════════ data & layers drawer ═══════════════════ */
function qsDrawer(open){ $('dataDrawer').classList.toggle('on',open!==false); if(open!==false) fillDrawer(); }
$('btnData').onclick=()=>qsDrawer(true);
function activeTile(){ return TILES[S.activeTile]||Object.values(TILES)[0]||null; }
async function fillDrawer(){
  const t=activeTile();
  const dd=$('ddAsset');
  if(!t||!t.key){ dd.innerHTML='<span class="muted">no active chart</span>'; return; }
  dd.innerHTML=`<b style="font-size:14px">${esc(t.symbol)}</b> <span class="muted">· ${esc(t.provider)} · ${esc(t.tf)}</span>`;
  const row=S.watch.find(w=>w.id===t.key);
  const cov=$('ddCoverage'); cov.innerHTML='';
  if(row&&row.counts){
    Object.entries(row.counts).forEach(([tf,n])=>{
      cov.innerHTML+=`<span class="k">${tf}</span><span class="num">${n.toLocaleString()} bars</span>`; });
  } else cov.innerHTML='<span class="k">tracked</span><span>no — fetch to store history</span>';
  $('ddFetchFull').onclick=async()=>{
    const r=await api('/markets/fetch','POST',{exchange:t.provider,symbol:t.symbol,
      timeframes:row?row.timeframes:['1d','1h'],full:true});
    $('ddJob').textContent=r&&r.ok?'⏳ full backfill started — live progress below':(r&&r.error||'failed');
  };
  $('ddRefresh').onclick=async()=>{
    const r=await api('/markets/update_now','POST',{exchange:t.provider,symbol:t.symbol});
    $('ddJob').textContent=r&&r.ok?'⏳ refreshing…':(r&&r.error||'failed');
  };
  $('ddAudit').onclick=async()=>{
    const r=await api('/markets/history/audit','POST',{symbol_key:t.key});
    $('ddJob').textContent=r&&r.error?r.error:('audit: '+esc(JSON.stringify(r&&r.summary||r).slice(0,220)));
  };
  $('ddRepair').onclick=async()=>{
    const r=await api('/markets/history/repair','POST',{symbol_key:t.key});
    $('ddJob').textContent=r&&r.error?r.error:'repair started ⏳';
  };
  $('ddEvents').onclick=async()=>{
    $('ddEvStatus').innerHTML='<span class="spin"></span>';
    const r=await api('/markets/events/apply','POST',{symbol_key:t.key});
    $('ddEvStatus').textContent=r&&r.ok?`applied ${r.applied}, ${r.skipped} already there`:(r&&r.error||'failed');
    tileEvents(t);
  };
  const det=await api('/markets/events/detect?symbol_key='+encodeURIComponent(t.key));
  $('ddEvList').innerHTML=((det&&det.events)||[]).slice(-14).map(e=>
    `<div class="macroRow"><span class="chip" style="border-color:${e.kind==='halving'?'var(--warn)':'var(--line2)'}">${e.kind}</span>
     ${esc(e.text)}<span class="sp"></span><span class="muted num">${dayFmt(e.t)}</span></div>`).join('')
    ||'<span class="muted">none detected</span>';
  renderMacroList();
}
async function loadMacro(){ const r=await api('/markets/macro/catalog');
  if(r&&r.series) S.macro=r.series; }
function renderMacroList(){
  const groups={};
  S.macro.forEach(m=>{(groups[m.group]=groups[m.group]||[]).push(m);});
  $('ddMacro').innerHTML=Object.entries(groups).map(([g,items])=>
    `<div style="font-size:10px;color:var(--faint);margin:7px 0 3px;text-transform:uppercase">${esc(g)}</div>`+
    items.map(m=>`<div class="macroRow">
      <span>${esc(m.name)}</span><span class="u">${esc(m.unit||'')}</span><span class="sp"></span>
      ${m.fetched?`<span class="chip on" title="last ${esc(m.last||'')}">ready</span>
        <button class="ghost" data-lay="${m.dataset_id}" data-l="${esc(m.name)}">⧉ layer</button>`
       :`<button data-fetch="${m.id}">⇊ fetch</button>`}</div>`).join('')).join('');
  $('ddMacro').querySelectorAll('[data-fetch]').forEach(b=>b.onclick=async()=>{
    b.textContent='⏳'; const r=await api('/markets/macro/fetch','POST',{id:b.dataset.fetch});
    if(r&&r.ok) toast('fetching '+esc(b.dataset.fetch)+' — will appear as a layer','ok');
    else toast(esc((r&&r.error)||'fetch failed'),'err');
  });
  $('ddMacro').querySelectorAll('[data-lay]').forEach(b=>b.onclick=()=>{
    const t=activeTile(); if(!t) return;
    if(t.compare.length>=4) return toast('max 4 layers','err');
    t.compare.push({ds:b.dataset.lay,label:b.dataset.l});
    tileCompare(t); toast('layered '+esc(b.dataset.l),'ok');
  });
}

/* ═══════════════════ layouts ═══════════════════ */
function layoutData(){
  return { grid:S.grid,
    tiles:Object.values(TILES).map(t=>({key:t.key,tf:t.tf,style:t.style,log:t.log,
      inds:t.inds,compare:t.compare,regime:t.regime,pivots:t.pivots,
      overlay:t.overlay,events:t.events})) };
}
async function applyLayout(data){
  Object.keys(TILES).forEach(removeTile);
  S.grid=data.grid||2;
  $('gridPicker').querySelectorAll('button').forEach(x=>x.classList.toggle('on',+x.dataset.g===S.grid));
  for(const td of (data.tiles||[])){
    const t=addTile();
    Object.assign(t,{style:td.style||'candles',log:!!td.log,
      inds:td.inds||t.inds,compare:td.compare||[],regime:td.regime||t.regime,
      pivots:td.pivots||t.pivots,overlay:td.overlay||t.overlay,
      events:td.events!==false});
    if(td.key) await tileSetAsset(t,td.key,td.tf);
  }
  applyGrid();
}
$('btnLayouts').onclick=async()=>{
  const dd=$('layoutDd');
  if(dd.style.display==='block'){dd.style.display='none';return;}
  const r=await api('/markets/layout/list');
  const rows=((r&&r.layouts)||[]).map(l=>
    `<div class="it" data-k="${esc(l.key)}"><b>${esc(l.name)}</b>
     <span class="nm">${esc((l.updated_at||'').slice(0,16))}</span>
     <span style="flex:1"></span><span class="del muted" data-del="${esc(l.key)}">✕</span></div>`).join('');
  dd.innerHTML=`<div class="it" id="ldSave"><b>💾 save current…</b></div>${rows}`;
  dd.style.display='block';
  dd.querySelector('#ldSave').onclick=async()=>{
    const name=prompt('layout name'); if(!name) return;
    const rr=await api('/markets/layout/save','POST',{name,data:layoutData()});
    toast(rr&&rr.ok?'layout saved':'save failed',rr&&rr.ok?'ok':'err');
    dd.style.display='none';
  };
  dd.querySelectorAll('.it[data-k]').forEach(it=>it.onclick=async e=>{
    if(e.target.dataset.del){
      await api('/markets/layout/delete','POST',{key:e.target.dataset.del});
      it.remove(); return; }
    const rr=await api('/markets/layout/list');
    const l=((rr&&rr.layouts)||[]).find(x=>x.key===it.dataset.k);
    if(l&&l.data) applyLayout(l.data);
    dd.style.display='none';
  });
};
document.addEventListener('click',e=>{
  if(!e.target.closest('#btnLayouts')&&!e.target.closest('#layoutDd'))
    $('layoutDd').style.display='none';
});

/* ═══════════════════ global search ═══════════════════ */
let _gsItems=[],_gsAct=-1;
/* metric search: macro series (hashrate, CPI…), positioning metrics (shorts/
   longs/funding/OI) and WSB buzz are first-class search results alongside
   assets — picking one opens it as its own chart tile (its own infographic). */
function metricMatches(q){
  const out=[];
  const ql=q.toLowerCase();
  (S.macro||[]).forEach(m2=>{
    if(m2.name.toLowerCase().includes(ql)||m2.id.toLowerCase().includes(ql)||
       (m2.group||'').toLowerCase().includes(ql))
      out.push({metric:'macro',id:m2.id,label:m2.name,
                sub:m2.group+(m2.fetched?'':' · not fetched yet'),
                fetched:m2.fetched,ds:m2.dataset_id});
  });
  const dynDefs=[['funding','Funding rate'],['oi','Open interest'],
    ['ls_acct','Long/Short accounts (open shorts vs longs)'],
    ['ls_top','Top-trader long/short positions']];
  dynDefs.forEach(([mid,lbl])=>{
    if(lbl.toLowerCase().includes(ql)||mid.includes(ql)||
       'shorts longs positioning open interest'.includes(ql))
      out.push({metric:'dyn',id:mid,label:lbl,
                sub:'BTC/USDT futures · pick asset after',fetched:null});
  });
  if('wsb reddit social buzz mentions alpha'.includes(ql)&&ql.length>2)
    out.push({metric:'wsb',id:'wsb',label:'WSB buzz scan',
              sub:'reddit mentions ranking',fetched:null});
  return out.slice(0,5);
}
$('gsIn').addEventListener('input',debounce(async()=>{
  const q=$('gsIn').value.trim();
  if(!q){$('gsDd').style.display='none';return;}
  const mets=q.length>=3?metricMatches(q):[];
  const r=await api('/markets/lookup?query='+encodeURIComponent(q)+'&limit=8');
  _gsItems=(r&&r.results)||[]; _gsAct=-1;
  $('gsDd').innerHTML=
    mets.map((m2,i)=>
      `<div class="it" data-m="${i}"><span class="cls">📊 metric</span>
       <b>${esc(m2.label)}</b><span class="nm">${esc(m2.sub||'')}</span></div>`).join('')+
    (_gsItems.map((it,i)=>
    `<div class="it" data-i="${i}"><span class="cls">${esc((it.asset_class||'?').replace('_',' '))}</span>
     <b>${esc(it.symbol)}</b><span class="nm">${esc(it.name||'')}</span>
     ${it.tracked?'<span class="chip on">tracked</span>':''}</div>`).join('')
    ||(mets.length?'':'<div class="it"><span class="nm">no matches</span></div>'));
  $('gsDd').style.display='block';
  $('gsDd').querySelectorAll('.it[data-i]').forEach(d=>d.onmousedown=e=>{
    e.preventDefault(); pickSearch(+d.dataset.i); });
  $('gsDd').querySelectorAll('.it[data-m]').forEach(d=>d.onmousedown=e=>{
    e.preventDefault(); pickMetric(mets[+d.dataset.m]); });
},260));
async function pickMetric(m2){
  $('gsDd').style.display='none'; $('gsIn').value='';
  if(S.view!=='charts') switchView('charts');
  if(m2.metric==='macro'){
    if(!m2.fetched){
      toast('fetching '+esc(m2.label)+'…');
      await api('/markets/macro/fetch','POST',{id:m2.id});
      return;                        /* markets.fetch done event will land it */
    }
    const t=addTile(); t.style='area'; t.inds=[];
    await tileSetAsset(t,'macro:'+m2.id);
  } else if(m2.metric==='dyn'){
    const sym=prompt('which pair? (Binance futures)', 'BTC/USDT');
    if(!sym) return;
    toast('fetching '+esc(m2.label)+' for '+esc(sym)+'…');
    const r=await api('/markets/dynamics/fetch','POST',{symbol:sym,metrics:[m2.id]});
    if(r&&r.ok){ const t=addTile(); t.style='area'; t.inds=[];
      setTimeout(()=>tileSetAsset(t,'dyn:'+sym+'#'+m2.id),2500); }
    else toast(esc((r&&r.error)||'failed'),'err');
  } else if(m2.metric==='wsb'){
    switchView('pulse');
    toast('running WSB scan — see the Pulse tab');
    if(typeof wsbScan==='function') wsbScan();
  }
}
$('gsIn').addEventListener('keydown',e=>{
  if($('gsDd').style.display!=='block')return;
  if(e.key==='ArrowDown'){_gsAct=Math.min(_gsItems.length-1,_gsAct+1);}
  else if(e.key==='ArrowUp'){_gsAct=Math.max(0,_gsAct-1);}
  else if(e.key==='Enter'&&_gsAct>=0){pickSearch(_gsAct);e.preventDefault();return;}
  else if(e.key==='Escape'){$('gsDd').style.display='none';return;}
  else return;
  e.preventDefault();
  $('gsDd').querySelectorAll('.it').forEach((d,i)=>d.classList.toggle('act',i===_gsAct));
});
document.addEventListener('click',e=>{ if(!e.target.closest('#gsearch'))$('gsDd').style.display='none';});
async function pickSearch(i){
  const it=_gsItems[i]; if(!it)return;
  $('gsDd').style.display='none'; $('gsIn').value='';
  if(!it.tracked){
    toast('adding '+esc(it.symbol)+' to the watchlist + backfilling…');
    const [prov,sym]=keySplit(it.key);
    await api('/markets/asset/add','POST',{provider:prov,symbol:sym});
    await loadWatch();
  }
  if(S.view!=='charts') switchView('charts');
  openInNewTile(it.key);
}
async function loadWatch(){
  const r=await api('/markets/watchlist');
  if(r&&r.watchlist) S.watch=r.watchlist;
  fillRunSelectors&&fillRunSelectors();
}
async function loadQuotes(){
  const r=await api('/markets/quotes');
  if(r&&r.quotes){ r.quotes.forEach(q=>S.quotes[q.key]=q);
    Object.values(TILES).forEach(tileQuote); }
}
