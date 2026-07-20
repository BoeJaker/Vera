
'use strict';
/* ═══ round 7: Portfolio + Alerts as first-class pages ═══ */
VIEW_TITLES.folio='Portfolio';
VIEW_TITLES.alerts='Alerts & Monitors';

/* the Project view keeps projections/optimizer/rotation; the ledger + x-ray
   now live on the Portfolio page — drop their old Project-view entry points */
if($('pjLedger')) $('pjLedger').remove();
if($('pjXray')) $('pjXray').remove();

/* ── 💼 Portfolio page ── */
async function renderFolio(){
  const [pos,hist]=await Promise.all([
    api('/markets/portfolio/positions'),
    api('/markets/portfolio/history?days=365')]);
  const positions=((pos&&pos.positions)||[]);
  const tot=(pos&&pos.totals)||{};
  const open=positions.filter(p=>(p.qty||0)>0);
  const hhi=open.reduce((s,p)=>s+Math.pow((p.market_value||0)/Math.max(1,tot.value||1),2),0);
  $('foTiles').innerHTML=[
    ['total value','$'+fmtN(tot.value||0),''],
    ['cost basis','$'+fmtN(tot.cost||0),''],
    ['unrealized',(tot.unrealized>=0?'+':'')+'$'+fmtN(Math.abs(tot.unrealized||0)),(tot.unrealized||0)>=0?'up':'dn'],
    ['realized',(tot.realized>=0?'+':'')+'$'+fmtN(Math.abs(tot.realized||0)),(tot.realized||0)>=0?'up':'dn'],
    ['positions',String(open.length),''],
    ['concentration',open.length?(hhi*100).toFixed(0)+' HHI':'—',hhi>0.35?'dn':'up'],
  ].map(([l,v,c])=>`<div class="stat"><div class="v num ${c}">${v}</div><div class="l">${l}</div></div>`).join('');
  /* visuals — reuse the infographic painters via the id contract */
  const dn=$('foDonut'); if(dn&&dn.id!=='igc_fo_0') dn.id='igc_fo_0';
  const pb=$('foPnl')||document.getElementById('igc_fo_1');
  if(pb&&pb.id!=='igc_fo_1') pb.id='igc_fo_1';
  if(open.length){
    igPanelDraw({type:'donut',data:open.map(p=>p.market_value||0),
      labels:open.map(p=>p.symbol_key.split(':').pop())},'fo',0);
    igPanelDraw({type:'bars',data:open.map(p=>p.unrealized_pnl||0),
      labels:open.map(p=>p.symbol_key)},'fo',1);
  }
  if(hist&&hist.t&&hist.t.length>1)
    drawSeries($('foHist'),{series:[
      {t:hist.t,v:hist.cost,color:COL.muted,width:1.1,dash:[4,3],label:'cost'},
      {t:hist.t,v:hist.value,color:COL.acc,width:1.7,fill:true,label:'value'}],
      fmt:v=>'$'+fmtN(v),animate:true});
  $('foPos').innerHTML='<tr><th>asset</th><th>qty</th><th>avg cost</th><th>last</th><th>value</th><th>uP&L</th><th>uP&L %</th><th>realized</th><th>alloc</th></tr>'+
    positions.map(p=>`<tr style="${(p.qty||0)>0?'':'opacity:.5'}">
      <td>${esc(p.symbol_key)}</td><td>${p.qty}</td><td>${fmtPx(p.avg_cost)}</td>
      <td>${fmtPx(p.last)}</td><td>${p.market_value!=null?'$'+fmtN(p.market_value):'—'}</td>
      <td class="${(p.unrealized_pnl||0)>=0?'up':'dn'}">${p.unrealized_pnl!=null?fmtN(p.unrealized_pnl):'—'}</td>
      <td class="${(p.unrealized_pct||0)>=0?'up':'dn'}">${fmtPct(p.unrealized_pct)}</td>
      <td class="${(p.realized_pnl||0)>=0?'up':'dn'}">${fmtN(p.realized_pnl)}</td>
      <td>${p.allocation_pct!=null?p.allocation_pct+'%':'—'}</td></tr>`).join('')
    ||'<tr><td colspan="9" class="muted">empty book — record your holdings in the ledger below</td></tr>';
  renderLedger();                         /* mounts into #foLedger */
}
$('folioRefresh').onclick=renderFolio;
$('folioProject').onclick=()=>{ switchView('proj'); $('pjSource').value='portfolio';
  setTimeout(()=>$('pjGo').click(),300); };
$('folioOptimize').onclick=()=>{ switchView('proj'); $('pjSource').value='portfolio';
  setTimeout(()=>$('opGo').click(),300); };

/* ── 🔔 Alerts & monitors page ── */
const DIR_CHIP={entry:['▲ entry','bull'],exit:['▽ exit','bear'],
  short_entry:['▼ short','bear'],short_exit:['△ cover','bull']};
async function renderAlertsPage(){
  const [mon,al]=await Promise.all([
    api('/markets/monitor/status'),
    api('/markets/alerts?limit=200'+($('alUnseen').checked?'&unseen_only=true':''))]);
  const mons=(mon&&mon.monitors)||[];
  $('alMons').innerHTML='<tr><th>strategy</th><th>market</th><th>every</th><th>channels</th><th>position</th><th>last signal</th><th>paper</th><th></th></tr>'+
    mons.map(m=>{
      const st=m.state||{};
      const pos=st.position||'flat';
      return `<tr>
        <td><b>${esc(m.name||m.id)}</b> <span class="muted" style="font-size:9px">${esc(m.kind||'')}</span></td>
        <td>${esc(String(m.dataset_id||'').replace('mkt.',''))}</td>
        <td>${m.interval_min||'—'}m</td>
        <td>${(m.channels||[]).map(c=>c==='telegram'?'📣':'🔔').join(' ')||'🔔'}</td>
        <td><span class="chip ${pos==='long'?'bull':pos==='short'?'bear':''}" style="font-size:9px">${esc(pos)}</span></td>
        <td class="muted" style="font-size:10px">${esc(String(st.last_signal||'—'))} ${esc(String(st.last_signal_at||'').slice(5,16).replace('T',' '))}</td>
        <td>${m.sim_account_id?'<span class="chip on" style="font-size:9px">◎ '+(m.sim_pct||'')+'%</span>':'—'}</td>
        <td><button class="ghost danger" data-alstop="${esc(m.id)}" title="stop monitoring">⏹</button></td></tr>`;
    }).join('')
    ||'<tr><td colspan="8" class="muted">no active monitors — hit 🛰 monitor strategies…</td></tr>';
  $('alMons').querySelectorAll('[data-alstop]').forEach(b=>b.onclick=async()=>{
    await api('/markets/strategy/archive','POST',{id:b.dataset.alstop});
    renderAlertsPage(); loadStrats();
  });
  const alerts=(al&&al.alerts)||[];
  $('alFeed').innerHTML=alerts.map(a=>{
    const [lbl,cls]=DIR_CHIP[a.direction]||[esc(a.direction||''),''];
    return `<div class="condRow" style="opacity:${a.seen?'.55':'1'}">
      <span class="muted num" style="font-size:10px;width:86px">${esc(String(a.created_at||'').slice(5,16).replace('T',' '))}</span>
      <span class="chip ${cls}" style="font-size:9.5px">${lbl}</span>
      <b style="font-size:11.5px">${esc(a.name||'')}</b>
      <span class="muted" style="font-size:10.5px">${esc(String(a.dataset_id||'').replace('mkt.',''))}</span>
      <span class="num" style="font-size:11px">@ ${fmtPx(a.price)}</span>
      <span style="flex:1"></span></div>`;
  }).join('')
    ||'<div class="empty">no alerts'+($('alUnseen').checked?' unseen':'')+' — signals from every monitor land here</div>';
  updateBellBadge(al?al.unseen:null);
  const rb=$('railAlBadge');
  if(rb){ const n=(al&&al.unseen)||0;
    rb.textContent=n>99?'99+':String(n); rb.style.display=n>0?'inline-block':'none'; }
}
$('alRefresh').onclick=renderAlertsPage;
$('alUnseen').onchange=renderAlertsPage;
$('alAck').onclick=async()=>{ await api('/markets/alerts/ack','POST',{});
  renderAlertsPage(); };
$('alMonBtn').onclick=e=>openMonitorLauncher(null,e.currentTarget);

/* bell dropdown links to the full page; rail badge mirrors the bell */
const _renderBellR7=renderBell;
renderBell=async function(){
  await _renderBellR7();
  const dd=$('bellDd');
  const row=document.createElement('div');
  row.className='it';
  row.innerHTML='<b style="color:var(--acc)">📄 open the Alerts page →</b>';
  row.onclick=()=>{ dd.style.display='none'; switchView('alerts'); };
  dd.insertBefore(row,dd.children[1]||null);
};
const _updateBellR7=updateBellBadge;
updateBellBadge=async function(n){
  await _updateBellR7(n);
  const rb=$('railAlBadge'),b=$('bellBadge');
  if(rb&&b){ rb.textContent=b.textContent; rb.style.display=b.style.display; }
};

/* lazy loads + live refresh */
const _svBase7=switchView;
switchView=function(n){ _svBase7(n);
  if(n==='folio') renderFolio();
  if(n==='alerts') renderAlertsPage();
};
const _handleEvR7=handleEvent;
handleEvent=function(ev){
  const ty=String(ev.type||'');
  if((ty==='markets.alert'||ty==='markets.monitor')&&S.view==='alerts')
    setTimeout(renderAlertsPage,300);
  if(ty==='markets.portfolio'&&S.view==='folio')
    setTimeout(renderFolio,300);
  _handleEvR7(ev);
};
