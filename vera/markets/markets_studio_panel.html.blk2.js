
'use strict';
/* ═══════════════════ Strategy Studio ═══════════════════ */
const OPS=[['>','>'],['<','<'],['>=','≥'],['<=','≤'],
  ['crosses_above','crosses ↑'],['crosses_below','crosses ↓']];

async function loadStrats(){
  const r=await api('/markets/strategy/list');
  if(r&&r.strategies) S.strategies=r.strategies;
  $('stratCount').textContent=S.strategies.length+' saved';
  renderStratList(); fillRunSelectors();
}
async function loadLibrary(){
  const r=await api('/markets/strategy/library');
  if(r&&r.templates) S.library=r.templates;
}
async function loadCustomInds(){
  const r=await api('/markets/indicator/custom/list');
  if(r&&r.indicators) S.customInds=r.indicators;
}
async function loadModels(){
  const r=await api('/markets/ml/list');
  if(r&&r.models) S.mlModels=r.models;
}
function renderStratList(){
  $('stratList').innerHTML=S.strategies.map(s=>{
    const st=s.status||'draft';
    return `<div class="sCard ${S.curStrat&&S.curStrat.id===s.id?'on':''}" data-id="${s.id}">
      <div class="nm">${esc(s.name)}
        <span class="chip" style="font-size:9px">${esc(s.kind||'rule')}</span>
        ${st==='accepted'?'<span class="chip on" style="font-size:9px">● live</span>':''}
        ${st==='archived'?'<span class="chip" style="font-size:9px">archived</span>':''}</div>
      <div class="meta">${esc((s.updated_at||'').slice(0,16))}</div></div>`;
  }).join('')||'<div class="empty">no strategies yet — grab one from the library →</div>';
  $('stratList').querySelectorAll('.sCard').forEach(c=>c.onclick=()=>{
    const s=S.strategies.find(x=>x.id===c.dataset.id);
    if(s) openBuilder(s);
  });
}

/* spec ⇄ builder model */
function opndFromSpec(o){
  if(o==null) return {src:'num',value:0};
  if(typeof o==='number') return {src:'num',value:o};
  if(typeof o==='string'){
    if(['close','open','high','low','volume'].includes(o.toLowerCase()))
      return {src:'price',price:o.toLowerCase()};
    const f=parseFloat(o); return isFinite(f)?{src:'num',value:f}:{src:'price',price:'close'};
  }
  if(o.value!=null) return {src:'num',value:+o.value};
  if(o.ml) return {src:'ml',ml:String(o.ml)};
  const kind=String(o.kind||'').toLowerCase();
  if(o.custom||kind.startsWith('cx_'))
    return {src:'ind',kind:String(o.custom||kind),params:{},series:o.series||''};
  return {src:'ind',kind:kind||'ema',params:Object.assign({},o.params||{}),series:o.series||''};
}
function opndToSpec(m){
  if(m.src==='price') return m.price;
  if(m.src==='num') return {value:+m.value||0};
  if(m.src==='ml') return {ml:m.ml};
  const out={kind:m.kind,params:m.params&&Object.keys(m.params).length?m.params:undefined};
  if(m.series) out.series=m.series;
  if(out.params===undefined) delete out.params;
  return out;
}
function newCond(){ return {left:{src:'ind',kind:'ema',params:{n:20},series:''},
  op:'crosses_above', right:{src:'ind',kind:'ema',params:{n:50},series:''}}; }

let B=null;  /* builder model */
function openBuilder(s){
  S.curStrat=s||null;
  const spec=(s&&s.spec)||{kind:'rule',fee_bps:10,slippage_bps:5,entry:[],exit:[]};
  const mapConds=list=>(list||[]).map(c=>({left:opndFromSpec(c.left),op:c.op||'>',right:opndFromSpec(c.right)}));
  B={ id:s?s.id:'', name:s?s.name:'', kind:spec.kind||s?.kind||'rule',
    regimes:(spec.regimes||[]).map(r=>({when:r.when||'any',strategy_id:r.strategy_id||'',spec:r.spec||null})),
    regime_source:spec.regime_source||{method:'sma',n:200},
    exit_on_regime_change:spec.exit_on_regime_change!==false,
    entry:mapConds(spec.entry), exit:mapConds(spec.exit),
    short_entry:mapConds(spec.short_entry), short_exit:mapConds(spec.short_exit),
    fee_bps:spec.fee_bps??10, slippage_bps:spec.slippage_bps??5, size_pct:spec.size_pct??100,
    stop_loss_pct:spec.stop_loss_pct??0, take_profit_pct:spec.take_profit_pct??0,
    leverage:spec.leverage??1,
    ml_id:spec.ml_id||'', enter_above:spec.enter_above??0.6, exit_below:spec.exit_below??0.45,
    ml_short:spec.short_below!=null, short_below:spec.short_below??0.35,
    short_exit_above:spec.short_exit_above??0.55,
    members:spec.members||[], combine:spec.combine||'all', exit_combine:spec.exit_combine||'any',
    weights:(spec.weights||[]).slice(), enter_threshold:spec.enter_threshold??0.6,
    exit_threshold:spec.exit_threshold??0.34 };
  if(!B.entry.length&&!B.short_entry.length&&B.kind==='rule') B.entry=[newCond()];
  renderStratList(); renderBuilder();
}
function builderSpec(){
  const s={kind:B.kind, fee_bps:+B.fee_bps, slippage_bps:+B.slippage_bps,
    size_pct:+B.size_pct};
  if(+B.stop_loss_pct>0) s.stop_loss_pct=+B.stop_loss_pct;
  if(+B.take_profit_pct>0) s.take_profit_pct=+B.take_profit_pct;
  if(+B.leverage>1) s.leverage=+B.leverage;
  if(B.kind==='ml'){ s.ml_id=B.ml_id; s.enter_above=+B.enter_above; s.exit_below=+B.exit_below;
    if(B.ml_short){ s.short_below=+B.short_below; s.short_exit_above=+B.short_exit_above; } }
  else if(B.kind==='fused'){ s.members=B.members; s.combine=B.combine; s.exit_combine=B.exit_combine;
    if(B.combine==='weighted'){
      s.weights=B.members.map((_,i)=>+((B.weights||[])[i]??1)||1);
      s.enter_threshold=+B.enter_threshold||0.6;
      s.exit_threshold=+B.exit_threshold||0.34; } }
  else if(B.kind==='regime'){
    s.regimes=B.regimes.filter(r=>r.strategy_id||r.spec)
      .map(r=>({when:r.when,...(r.strategy_id?{strategy_id:r.strategy_id}:{spec:r.spec})}));
    s.regime_source=B.regime_source; s.exit_on_regime_change=B.exit_on_regime_change; }
  else { const mc=list=>list.map(c=>({left:opndToSpec(c.left),op:c.op,right:opndToSpec(c.right)}));
    if(B.entry.length) s.entry=mc(B.entry);
    if(B.exit.length) s.exit=mc(B.exit);
    if(B.short_entry.length){ s.short_entry=mc(B.short_entry);
      if(B.short_exit.length) s.short_exit=mc(B.short_exit); } }
  return s;
}
function opndHtml(m,path){
  const srcSel=`<select data-o="${path}.src">
    <option value="price"${m.src==='price'?' selected':''}>price</option>
    <option value="ind"${m.src==='ind'?' selected':''}>indicator</option>
    <option value="ml"${m.src==='ml'?' selected':''}>🧠 model</option>
    <option value="num"${m.src==='num'?' selected':''}>number</option></select>`;
  let body='';
  if(m.src==='price')
    body=`<select data-o="${path}.price">${['close','open','high','low','volume'].map(p=>
      `<option${m.price===p?' selected':''}>${p}</option>`).join('')}</select>`;
  else if(m.src==='num')
    body=`<input data-o="${path}.value" value="${m.value??0}" style="width:70px" class="num">`;
  else if(m.src==='ml')
    body=`<select data-o="${path}.ml">${S.mlModels.filter(x=>x.status==='ready').map(x=>
      `<option value="${x.id}"${m.ml===x.id?' selected':''}>${esc(x.name)}</option>`).join('')||'<option value="">train a model first</option>'}</select>`;
  else{
    const kinds=Object.keys(IND_DEFS).concat(S.customInds.map(c=>c.id));
    const kindSel=`<select data-o="${path}.kind">${kinds.map(k=>{
      const lbl=IND_DEFS[k]?IND_DEFS[k].l:(S.customInds.find(c=>c.id===k)||{}).name||k;
      return `<option value="${k}"${m.kind===k?' selected':''}>${esc(lbl)}</option>`;}).join('')}</select>`;
    const def=IND_DEFS[m.kind];
    const params=def?Object.entries(def.p).filter(([_,v])=>typeof v==='number').map(([pk,pv])=>
      `<span class="pWrap"><span class="pl">${pk}</span>
       <input data-o="${path}.params.${pk}" value="${m.params&&m.params[pk]!=null?m.params[pk]:pv}"></span>`).join(''):'';
    /* multi-series indicators need a series pick */
    const multi={bbands:['bb_mid','bb_upper','bb_lower'],stoch:['stoch_k','stoch_d'],
      macd:['macd','macd_signal','macd_hist'],adx:['adx','plus_di','minus_di'],
      donchian:['dc_upper','dc_mid','dc_lower'],keltner:['kc_mid','kc_upper','kc_lower'],
      supertrend:['supertrend','st_dir'],
      ichimoku:['tenkan','kijun','senkou_a','senkou_b','chikou']}[m.kind];
    const ser=multi?`<select data-o="${path}.series">${multi.map(x=>
      `<option${(m.series||multi[0])===x?' selected':''}>${x}</option>`).join('')}</select>`:'';
    body=kindSel+params+ser;
  }
  return `<span style="display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap">${srcSel}${body}</span>`;
}
function condsHtml(list,which){
  return list.map((c,i)=>`<div class="condRow">
    ${opndHtml(c.left,which+'.'+i+'.left')}
    <select class="opSel" data-o="${which}.${i}.op">${OPS.map(([v,l])=>
      `<option value="${v}"${c.op===v?' selected':''}>${l}</option>`).join('')}</select>
    ${opndHtml(c.right,which+'.'+i+'.right')}
    <span style="flex:1"></span>
    <button class="ghost" data-delc="${which}.${i}">✕</button></div>`).join('');
}
function renderBuilder(){
  if(!B){ renderLibraryHome(); return; }
  const m=$('stratMain');
  let inner='';
  if(B.kind==='rule'){
    inner=`
    <h4 class="sec"><span class="up">▲ Long entry</span> — ALL must be true <button class="ghost" id="bAddEntry" style="float:right">＋ condition</button></h4>
    <div id="bEntry">${condsHtml(B.entry,'entry')||'<div class="muted" style="font-size:12px">none — short-only strategy</div>'}</div>
    <h4 class="sec">Long exit — ANY closes <button class="ghost" id="bAddExit" style="float:right">＋ condition</button></h4>
    <div id="bExit">${condsHtml(B.exit,'exit')||'<div class="muted" style="font-size:12px">none — exits only by stop/take-profit</div>'}</div>
    <h4 class="sec"><span class="dn">▼ Short entry</span> — ALL must be true <button class="ghost" id="bAddSEntry" style="float:right">＋ condition</button></h4>
    <div id="bSEntry">${condsHtml(B.short_entry,'short_entry')||'<div class="muted" style="font-size:12px">none — long-only. Add a condition to enable shorting (native engine).</div>'}</div>
    ${B.short_entry.length?`<h4 class="sec">Short exit (cover) — ANY covers <button class="ghost" id="bAddSExit" style="float:right">＋ condition</button></h4>
    <div id="bSExit">${condsHtml(B.short_exit,'short_exit')||'<div class="muted" style="font-size:12px">none — covers only by stop/take-profit or a long entry signal</div>'}</div>`:''}
    <div class="formRow" style="margin-top:6px">
      <button class="ghost" id="bMirror" title="auto-generate the short side by inverting every long condition (crosses ↑↔↓, &gt;↔&lt;)">⇅ mirror long side → short</button>
    </div>`;
  } else if(B.kind==='regime'){
    const WHENS=['bull','bear','flat','any'];
    inner=`
    <h4 class="sec">🌗 Market-phase classifier</h4>
    <div class="formRow"><label>method</label>
      <select data-rs="method">
        <option value="sma"${B.regime_source.method==='sma'?' selected':''}>SMA side + slope</option>
        <option value="supertrend"${B.regime_source.method==='supertrend'?' selected':''}>Supertrend direction</option>
        <option value="pivots"${B.regime_source.method==='pivots'?' selected':''}>Confirmed pivot legs</option>
      </select>
      ${B.regime_source.method==='pivots'
        ?`<label style="min-width:0">pivot %</label><input data-rs="pct" value="${B.regime_source.pct||5}" style="width:56px">`
        :`<label style="min-width:0">period</label><input data-rs="n" value="${B.regime_source.n||200}" style="width:56px">`}
      <label class="chip"><input type="checkbox" id="bRegExit" ${B.exit_on_regime_change?'checked':''}> exit on phase change</label></div>
    <h4 class="sec">Phases — which strategy runs when <button class="ghost" id="bAddReg" style="float:right">＋ phase</button></h4>
    ${B.regimes.map((r,i)=>`<div class="condRow">
      <select data-rw="${i}">${WHENS.map(w=>`<option${r.when===w?' selected':''}>${w}</option>`).join('')}</select>
      <span class="op">→</span>
      <select data-rst="${i}" style="flex:1">
        ${r.spec?`<option value="__inline" selected>⚙ inline spec (from template)</option>`:''}
        ${S.strategies.filter(x=>x.id!==B.id&&!['fused','regime'].includes(x.kind||'rule')).map(x=>
          `<option value="${x.id}"${r.strategy_id===x.id?' selected':''}>${esc(x.name)}</option>`).join('')}
      </select>
      <button class="ghost" data-rdel="${i}">✕</button></div>`).join('')
      ||'<div class="muted" style="font-size:12px">add a phase — e.g. bull → trend rider, bear → short fader</div>'}
    <p class="muted" style="font-size:11px">Members' entries only fire inside their phase; exits always
      work; a phase change closes open positions (toggle above). Multi-stage / layered / phased
      backtesting in one strategy.</p>`;
  } else if(B.kind==='ml'){
    inner=`
    <h4 class="sec">Model</h4>
    <div class="formRow"><label>trained model</label>
      <select id="bMl">${S.mlModels.filter(x=>x.status==='ready').map(x=>
        `<option value="${x.id}"${B.ml_id===x.id?' selected':''}>${esc(x.name)} (${esc(x.task)})</option>`).join('')||'<option value="">no trained models</option>'}</select></div>
    <div class="formRow"><label>enter above</label>
      <input type="range" id="bEnter" min="0.5" max="0.9" step="0.01" value="${B.enter_above}" style="flex:1">
      <span class="num" id="bEnterV">${B.enter_above}</span></div>
    <div class="formRow"><label>exit below</label>
      <input type="range" id="bExitTh" min="0.1" max="0.6" step="0.01" value="${B.exit_below}" style="flex:1">
      <span class="num" id="bExitV">${B.exit_below}</span></div>
    <div class="formRow"><label class="chip"><input type="checkbox" id="bMlShort" ${B.ml_short?'checked':''}> short side</label></div>
    ${B.ml_short?`
    <div class="formRow"><label>short below</label>
      <input type="range" id="bShortTh" min="0.1" max="0.5" step="0.01" value="${B.short_below}" style="flex:1">
      <span class="num" id="bShortV">${B.short_below}</span></div>
    <div class="formRow"><label>cover above</label>
      <input type="range" id="bSCoverTh" min="0.3" max="0.9" step="0.01" value="${B.short_exit_above}" style="flex:1">
      <span class="num" id="bSCoverV">${B.short_exit_above}</span></div>`:''}
    <p class="muted" style="font-size:11.5px">Long while P(up) stays above the entry threshold;
      ${B.ml_short?'SHORT while it stays below the short threshold;':''} flat in between.</p>`;
  } else {
    const useW=B.combine==='weighted'||((B.weights||[]).length>0);
    inner=`
    <h4 class="sec">Compound / weighted fusion</h4>
    <div class="formRow"><label>mode</label>
      <select id="bFuseMode">
        <option value="all"${B.combine==='all'?' selected':''}>ALL members agree</option>
        <option value="any"${B.combine==='any'?' selected':''}>ANY member fires</option>
        <option value="majority"${B.combine==='majority'?' selected':''}>majority vote</option>
        <option value="weighted"${useW?' selected':''}>⚖ weighted score ≥ threshold</option>
      </select></div>
    ${useW?`
    <div class="formRow"><label>enter ≥</label>
      <input type="range" id="bWEnter" min="0.1" max="1" step="0.05" value="${B.enter_threshold??0.6}" style="flex:1">
      <span class="num" id="bWEnterV">${B.enter_threshold??0.6}</span></div>
    <div class="formRow"><label>exit ≥</label>
      <input type="range" id="bWExit" min="0.05" max="1" step="0.05" value="${B.exit_threshold??0.34}" style="flex:1">
      <span class="num" id="bWExitV">${B.exit_threshold??0.34}</span></div>
    <p class="muted" style="font-size:10.5px">Members vote with their weight; the composite fires when the
      normalised score crosses the threshold. Weights &amp; thresholds are numeric spec paths —
      ✦ autotune and ⌗ sweeps optimise the composition itself.</p>`:''}`;
    if(true) inner+=`
    <h4 class="sec">Members${useW?' &amp; weights':''}</h4>
    <div class="formRow" style="flex-wrap:wrap">${S.strategies.filter(x=>x.id!==B.id&&(x.kind||'rule')!=='fused').map(x=>{
      const mi=B.members.indexOf(x.id);
      return `<label class="chip ${mi>=0?'on':''}" style="gap:4px"><input type="checkbox" data-mem="${x.id}"
        ${mi>=0?'checked':''} style="display:none">${esc(x.name)}${useW&&mi>=0
        ?` <input data-wgt="${mi}" value="${(B.weights||[])[mi]??1}" style="width:38px;padding:1px 4px;font-size:10px" class="num" onclick="event.preventDefault()">`
        :''}</label>`;}).join('')}</div>
    ${useW?'':`<div class="formRow"><label>exit when</label>
      <select id="bExitC">${['any','all'].map(x=>
        `<option${B.exit_combine===x?' selected':''}>${x}</option>`).join('')}</select>
      <span class="muted">member says exit</span></div>`}`;
  }
  m.innerHTML=`
    <div class="formRow" style="margin-top:0">
      <input id="bName" value="${esc(B.name)}" placeholder="strategy name" style="font-size:15px;font-weight:600;flex:1">
      <select id="bKind">${['rule','ml','fused','regime'].map(k=>
        `<option value="${k}"${B.kind===k?' selected':''}>${k==='rule'?'⚙ rules':k==='ml'?'🧠 ML model':k==='fused'?'⛓ fusion':'🌗 regime phases'}</option>`).join('')}</select>
    </div>
    ${inner}
    <h4 class="sec">Risk &amp; costs</h4>
    <div class="grid2">
      <div class="formRow"><label>fee bps</label><input id="bFee" type="number" value="${B.fee_bps}" style="width:70px">
        <label>slippage bps</label><input id="bSlip" type="number" value="${B.slippage_bps}" style="width:70px"></div>
      <div class="formRow"><label>size %</label><input id="bSize" type="number" value="${B.size_pct}" style="width:70px"></div>
      <div class="formRow"><label>stop-loss %</label><input id="bSl" type="number" value="${B.stop_loss_pct}" style="width:70px">
        <span class="muted" style="font-size:10px">0 = off</span></div>
      <div class="formRow"><label>take-profit %</label><input id="bTp" type="number" value="${B.take_profit_pct}" style="width:70px">
        <span class="muted" style="font-size:10px">0 = off</span></div>
      <div class="formRow"><label>leverage</label><input id="bLev" type="number" value="${B.leverage||1}" min="1" max="10" step="0.5" style="width:70px">
        <span class="muted" style="font-size:10px">1 = spot · &gt;1 can liquidate (native engine)</span></div>
    </div>
    <div class="formRow" style="margin-top:14px">
      <button class="pri" id="bSave">💾 save</button>
      <button id="bTest">▶ backtest</button>
      <button id="bPreview">◉ preview signals</button>
      <button id="bTune">✦ autotune</button>
      ${B.id?'<button class="danger" id="bDelete">delete</button>':''}
      <span id="bStatus" class="muted"></span>
    </div>
    <div id="bPreviewOut"></div>
    <h4 class="sec" style="margin-top:22px">Library</h4>
    <div class="libGrid" id="bLib"></div>`;
  bindBuilder(); renderLibraryCards($('bLib'));
}
function bindBuilder(){
  const m=$('stratMain');
  m.querySelectorAll('[data-o]').forEach(el=>{
    el.onchange=()=>{
      const path=el.dataset.o.split('.');
      let cur=B;
      for(let i=0;i<path.length-1;i++){
        const k=path[i]; cur=cur[/^\d+$/.test(k)?+k:k];
        if(cur==null) return;
      }
      const leaf=path[path.length-1];
      let v=el.value;
      if(leaf==='value'||(path.includes('params'))) v=parseFloat(v);
      cur[leaf]=v;
      if(leaf==='src'){ /* re-init operand shape */
        Object.assign(cur,{price:'close',kind:'ema',params:{n:20},series:'',ml:(S.mlModels[0]||{}).id||'',value:0});
        cur.src=el.value; renderBuilder();
      }
      if(leaf==='kind'&&path.length>2){ const def=IND_DEFS[el.value];
        cur.params=def?JSON.parse(JSON.stringify(def.p)):{}; cur.series=''; renderBuilder(); }
    };
  });
  m.querySelectorAll('[data-delc]').forEach(b=>b.onclick=()=>{
    const [w,i]=b.dataset.delc.split('.'); B[w].splice(+i,1); renderBuilder(); });
  const on=(id,fn)=>{const el=m.querySelector('#'+id); if(el) el.onclick=fn;};
  const val=(id,fn)=>{const el=m.querySelector('#'+id); if(el) el.onchange=fn;};
  on('bAddEntry',()=>{B.entry.push(newCond());renderBuilder();});
  on('bAddExit',()=>{B.exit.push({left:{src:'ind',kind:'rsi',params:{n:14},series:''},op:'>',right:{src:'num',value:70}});renderBuilder();});
  on('bAddSEntry',()=>{B.short_entry.push({left:{src:'ind',kind:'rsi',params:{n:14},series:''},op:'crosses_above',right:{src:'num',value:75}});renderBuilder();});
  on('bAddSExit',()=>{B.short_exit.push({left:{src:'ind',kind:'rsi',params:{n:14},series:''},op:'<',right:{src:'num',value:50}});renderBuilder();});
  on('bMirror',()=>{
    const inv={'crosses_above':'crosses_below','crosses_below':'crosses_above',
      '>':'<','<':'>','>=':'<=','<=':'>='};
    const flip=c=>({left:JSON.parse(JSON.stringify(c.left)),op:inv[c.op]||c.op,
      right:JSON.parse(JSON.stringify(c.right))});
    B.short_entry=B.entry.map(flip);
    B.short_exit=B.exit.map(flip);
    renderBuilder();
    toast('short side generated by inverting the long conditions — review the thresholds','ok');
  });
  on('bAddReg',()=>{B.regimes.push({when:B.regimes.length?'bear':'bull',
    strategy_id:(S.strategies[0]||{}).id||'',spec:null});renderBuilder();});
  m.querySelectorAll('[data-rw]').forEach(el=>el.onchange=()=>{
    B.regimes[+el.dataset.rw].when=el.value;});
  m.querySelectorAll('[data-rst]').forEach(el=>el.onchange=()=>{
    const r=B.regimes[+el.dataset.rst];
    if(el.value!=='__inline'){ r.strategy_id=el.value; r.spec=null; }});
  m.querySelectorAll('[data-rdel]').forEach(el=>el.onclick=()=>{
    B.regimes.splice(+el.dataset.rdel,1);renderBuilder();});
  m.querySelectorAll('[data-rs]').forEach(el=>el.onchange=()=>{
    const k=el.dataset.rs;
    B.regime_source[k]=k==='method'?el.value:parseFloat(el.value);
    if(k==='method') renderBuilder();});
  const bre=m.querySelector('#bRegExit');
  if(bre) bre.onchange=e=>B.exit_on_regime_change=e.target.checked;
  val('bName',e=>B.name=e.target.value);
  val('bKind',e=>{B.kind=e.target.value;
    if(B.kind==='rule'&&!B.entry.length&&!B.short_entry.length)B.entry=[newCond()];
    if(B.kind==='regime'&&!B.regimes.length)
      B.regimes=[{when:'bull',strategy_id:(S.strategies[0]||{}).id||'',spec:null}];
    renderBuilder();});
  val('bFee',e=>B.fee_bps=+e.target.value); val('bSlip',e=>B.slippage_bps=+e.target.value);
  val('bSize',e=>B.size_pct=+e.target.value); val('bSl',e=>B.stop_loss_pct=+e.target.value);
  val('bTp',e=>B.take_profit_pct=+e.target.value);
  val('bLev',e=>B.leverage=+e.target.value||1);
  val('bMl',e=>B.ml_id=e.target.value);
  const rng=(id,vid,key)=>{const el=m.querySelector('#'+id);
    if(el) el.oninput=e=>{B[key]=+e.target.value; m.querySelector('#'+vid).textContent=e.target.value;};};
  rng('bEnter','bEnterV','enter_above'); rng('bExitTh','bExitV','exit_below');
  rng('bShortTh','bShortV','short_below'); rng('bSCoverTh','bSCoverV','short_exit_above');
  const mls=m.querySelector('#bMlShort');
  if(mls) mls.onchange=e=>{ B.ml_short=e.target.checked; renderBuilder(); };
  val('bCombine',e=>B.combine=e.target.value); val('bExitC',e=>B.exit_combine=e.target.value);
  val('bFuseMode',e=>{ B.combine=e.target.value; renderBuilder(); });
  rng('bWEnter','bWEnterV','enter_threshold'); rng('bWExit','bWExitV','exit_threshold');
  m.querySelectorAll('[data-wgt]').forEach(inp=>{
    inp.onchange=()=>{ const i=+inp.dataset.wgt;
      B.weights=B.weights||[]; B.weights[i]=parseFloat(inp.value)||1; };
    inp.onclick=e=>e.stopPropagation();
  });
  m.querySelectorAll('[data-mem]').forEach(cb=>cb.onchange=()=>{
    const id=cb.dataset.mem;
    if(cb.checked){ if(!B.members.includes(id)){ B.members.push(id);
      (B.weights=B.weights||[]).push(1); } }
    else{ const i=B.members.indexOf(id);
      if(i>=0){ B.members.splice(i,1); (B.weights||[]).splice(i,1); } }
    cb.closest('label').classList.toggle('on',cb.checked);
    if(B.combine==='weighted') renderBuilder();
  });
  on('bSave',async()=>{
    if(!B.name.trim()) return toast('name the strategy first','err');
    const r=await api('/markets/strategy/save','POST',
      {name:B.name,spec:builderSpec(),kind:B.kind,id:B.id||undefined,
       members:B.kind==='fused'?B.members:undefined});
    if(r&&r.ok){ B.id=r.id; toast('strategy saved','ok'); await loadStrats();
      S.curStrat=S.strategies.find(x=>x.id===r.id)||null; renderStratList(); }
    else toast(esc((r&&r.error)||'save failed'),'err');
  });
  on('bTest',async()=>{
    switchView('run');
    if(B.id) $('rcStrat').value=B.id;
    runBacktest(B.id?null:builderSpec(), B.name||'unsaved');
  });
  on('bPreview',async()=>{
    const t=activeTile();
    const ds=t&&t.key?dsId(t.provider,t.symbol,t.tf):(defaultDs()||'');
    if(!ds) return toast('open a chart first','err');
    m.querySelector('#bStatus').innerHTML='<span class="spin"></span>';
    const r=await api('/markets/backtest/signals','POST',{dataset_id:ds,spec:builderSpec()});
    if(r&&r.ok){
      m.querySelector('#bStatus').textContent=
        `${r.entry_count}▲ ${r.exit_count}▽ over ${r.bars} bars`+
        (r.short_entry_count?` · ${r.short_entry_count}▼ short`:'')+
        (r.entry_now?' · 🔔 entry NOW':'')+(r.short_entry_now?' · 🔔 SHORT now':'');
      if(t&&t.chart){ t.chart.setMarkers(
        (r.entries||[]).map(ts=>({t:ts,kind:'buy'}))
        .concat((r.exits||[]).map(ts=>({t:ts,kind:'sell'})))
        .concat((r.short_entries||[]).map(ts=>({t:ts,kind:'short'})))
        .concat((r.short_exits||[]).map(ts=>({t:ts,kind:'cover'})))); }
      toast('signal markers drawn on the active chart','ok');
    } else m.querySelector('#bStatus').textContent=(r&&r.error)||'failed';
  });
  on('bTune',()=>{ switchView('run'); if(B.id)$('rcStrat').value=B.id; startAutotune(); });
  on('bDelete',async()=>{
    if(!confirm('delete strategy "'+B.name+'"?')) return;
    await api('/markets/strategy/delete','POST',{id:B.id});
    B=null; S.curStrat=null; await loadStrats(); renderBuilder();
  });
}
function renderLibraryHome(){
  $('stratMain').innerHTML=`
    <h2 class="sec">Strategy library</h2>
    <p class="muted" style="margin-bottom:12px;max-width:640px">
      Battle-tested starting points — click <b>use</b> to instantiate one as your own
      strategy (no JSON, ever), then tweak it in the visual builder, backtest it,
      autotune it, and put it live on a monitor or sim account.</p>
    <div class="libGrid" id="libHome"></div>`;
  renderLibraryCards($('libHome'));
}
function renderLibraryCards(host){
  if(!host) return;
  host.innerHTML=S.library.map(tp=>`
    <div class="libCard">
      <span class="ic">${tp.icon||'⚙'}</span><b>${esc(tp.name)}</b>
      <span class="chip" style="align-self:flex-start">${esc(tp.category)}</span>
      <p>${esc(tp.description)}</p>
      <div class="ft"><button class="pri" data-use="${tp.id}">use →</button>
        <span class="muted" style="font-size:10px">${esc(tp.difficulty||'')}</span></div>
    </div>`).join('');
  host.querySelectorAll('[data-use]').forEach(b=>b.onclick=async()=>{
    const tp=S.library.find(x=>x.id===b.dataset.use);
    let overrides={};
    if(tp.needs_model){
      const ready=S.mlModels.filter(x=>x.status==='ready');
      if(!ready.length) return toast('train an ML model first (old Markets tab → ML)','err');
      overrides.ml_id=ready[0].id;
    }
    const name=prompt('name your strategy', tp.name);
    if(!name) return;
    b.innerHTML='<span class="spin"></span>';
    const r=await api('/markets/strategy/from_template','POST',
      {template_id:tp.id,name,overrides});
    if(r&&r.ok){ toast('“'+esc(name)+'” created','ok'); await loadStrats();
      const s=S.strategies.find(x=>x.id===r.id); if(s) openBuilder(s); }
    else toast(esc((r&&r.error)||'failed'),'err');
    b.textContent='use →';
  });
}

/* ── custom indicator lab ── */
const FX_FUNCS=['sma(x,n)','ema(x,n)','wilder(x,n)','stdev(x,n)','highest(x,n)','lowest(x,n)',
  'median(x,n)','sum(x,n)','rsi(n)','atr(n)','tr()','vwap(n)','obv()','roc(x,n)',
  'shift(x,n)','cross_up(a,b)','cross_dn(a,b)','where(cond,a,b)','abs(x)','log(x)','sqrt(x)','nz(x)'];
$('btnIndLab').onclick=()=>{ S.curStrat=null; B=null; renderStratList(); renderIndLab(); };
function renderIndLab(){
  $('stratMain').innerHTML=`
    <h2 class="sec">ƒx Indicator lab</h2>
    <p class="muted" style="max-width:640px">Invent indicators from vector math over the bar
      arrays <code>o h l c v</code>. They become first-class: chartable on any tile, usable
      as operands in the strategy builder, and sweepable by the backtester.</p>
    <div class="grid2" style="margin-top:12px">
      <div class="card">
        <div class="formRow"><label>name</label><input id="fxName" placeholder="e.g. Momentum Pulse" style="flex:1"></div>
        <div class="formRow"><label>pane</label><select id="fxPane"><option value="sub">sub pane</option><option value="main">on price</option></select></div>
        <textarea id="fxExpr" class="exprBox" placeholder="(ema(c,9)-ema(c,21))/c*100"></textarea>
        <div class="fnChips">${FX_FUNCS.map(f=>`<span class="chip" data-fn="${f}">${f}</span>`).join('')}</div>
        <div class="formRow">
          <button id="fxTest">⚗ test on active chart</button>
          <button class="pri" id="fxSave">💾 save indicator</button>
          <span id="fxStatus" class="muted"></span></div>
        <div id="fxOut" class="num" style="font-size:11px;margin-top:6px"></div>
      </div>
      <div class="card">
        <h4 class="sec" style="margin-top:0">Your indicators</h4>
        <div id="fxList"></div>
      </div>
    </div>`;
  const m=$('stratMain');
  m.querySelectorAll('[data-fn]').forEach(ch=>ch.onclick=()=>{
    const ta=m.querySelector('#fxExpr'); ta.value+=(ta.value?' ':'')+ch.dataset.fn; ta.focus(); });
  const renderList=()=>{
    m.querySelector('#fxList').innerHTML=S.customInds.map(cxi=>
      `<div class="condRow"><b>${esc(cxi.name)}</b>
       <span class="muted" style="font-size:10px">${esc(Object.values(cxi.series||{})[0]||'').slice(0,44)}</span>
       <span style="flex:1"></span>
       <button class="ghost" data-edit="${cxi.id}">edit</button>
       <button class="ghost danger" data-del="${cxi.id}">✕</button></div>`).join('')
      ||'<div class="muted">none yet</div>';
    m.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>{
      const cxi=S.customInds.find(x=>x.id===b.dataset.edit);
      m.querySelector('#fxName').value=cxi.name;
      m.querySelector('#fxName').dataset.id=cxi.id;
      m.querySelector('#fxPane').value=cxi.pane||'sub';
      m.querySelector('#fxExpr').value=Object.values(cxi.series||{})[0]||'';
    });
    m.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
      await api('/markets/indicator/custom/delete','POST',{id:b.dataset.del});
      await loadCustomInds(); renderList();
    });
  };
  renderList();
  m.querySelector('#fxTest').onclick=async()=>{
    const t=activeTile();
    const ds=t&&t.key?dsId(t.provider,t.symbol,t.tf):defaultDs();
    if(!ds) return toast('open a chart first','err');
    m.querySelector('#fxStatus').innerHTML='<span class="spin"></span>';
    const r=await api('/markets/indicator/custom/test','POST',
      {expr:m.querySelector('#fxExpr').value,dataset_id:ds});
    m.querySelector('#fxStatus').textContent=r&&r.ok?('ok · '+r.bars+' bars'):(r&&r.error||'failed');
    if(r&&r.ok) m.querySelector('#fxOut').textContent=
      'tail: '+Object.entries(r.tail).map(([k,v])=>k+'=['+v.slice(-5).map(x=>x==null?'·':(+x).toFixed(3)).join(', ')+']').join('  ');
  };
  m.querySelector('#fxSave').onclick=async()=>{
    const name=m.querySelector('#fxName').value.trim();
    if(!name) return toast('name it first','err');
    const r=await api('/markets/indicator/custom/save','POST',
      {name,expr:m.querySelector('#fxExpr').value,
       pane:m.querySelector('#fxPane').value,
       id:m.querySelector('#fxName').dataset.id||''});
    if(r&&r.ok){ toast('indicator saved — it now appears in every indicator menu','ok');
      m.querySelector('#fxName').dataset.id='';
      await loadCustomInds(); renderList(); }
    else toast(esc((r&&r.error)||'failed'),'err');
  };
}
$('btnNewStrat').onclick=()=>openBuilder(null);
function defaultDs(){
  const w=S.watch[0]; return w?dsId(w.exchange,w.symbol,'1d'):'';
}
