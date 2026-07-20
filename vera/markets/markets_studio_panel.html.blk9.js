
'use strict';
/* ═══ round 6: monitor MANY strategies at once + a proper notification feed ═══ */

/* ── 🔔 alerts bell (topbar) — every monitor's signals land here ── */
(function mountBell(){
  const btn=document.createElement('button');
  btn.className='ghost'; btn.id='btnBell'; btn.style.position='relative';
  btn.title='signal alerts from every monitored strategy';
  btn.innerHTML='🔔<span id="bellBadge" style="display:none;position:absolute;top:-3px;right:-4px;'+
    'font:700 8.5px var(--mono);padding:1px 5px;border-radius:8px;background:var(--dn);color:#fff"></span>';
  $('topbar').insertBefore(btn,$('btnViewCtl'));
  const dd=document.createElement('div');
  dd.className='dd'; dd.id='bellDd';
  dd.style.cssText='position:fixed;right:12px;top:44px;left:auto;min-width:380px;max-width:440px';
  document.body.appendChild(dd);
  btn.onclick=async()=>{
    if(dd.style.display==='block'){ dd.style.display='none'; return; }
    await renderBell(); dd.style.display='block';
  };
  document.addEventListener('click',e=>{
    if(!e.target.closest('#btnBell')&&!e.target.closest('#bellDd')) dd.style.display='none'; });
})();
async function updateBellBadge(n){
  if(n==null){ const r=await api('/markets/alerts?limit=1'); n=(r&&r.unseen)||0; }
  const b=$('bellBadge'); if(!b) return;
  b.textContent=n>99?'99+':String(n);
  b.style.display=n>0?'inline-block':'none';
}
async function renderBell(){
  const r=await api('/markets/alerts?limit=30');
  const list=(r&&r.alerts)||[];
  $('bellDd').innerHTML=`<div class="it" style="cursor:default"><b>🔔 signal alerts</b>
      <span style="flex:1"></span>
      <span class="muted" id="bellMon" style="cursor:pointer;font-size:10.5px">🛰 monitor strategies…</span>
      <span class="muted" id="bellAck" style="cursor:pointer;font-size:10.5px">✓ mark all read</span></div>`+
    (list.map(a=>`<div class="it" style="cursor:default;opacity:${a.seen?'.5':'1'}">
      <span class="cls">${esc(String(a.created_at||'').slice(5,16).replace('T',' '))}</span>
      <span class="nm" style="white-space:normal">${esc(a.message||'')}</span></div>`).join('')
     ||'<div class="it"><span class="nm">no alerts yet — every monitored strategy raises one per new signal</span></div>');
  $('bellAck').onclick=async()=>{ await api('/markets/alerts/ack','POST',{});
    updateBellBadge(0); renderBell(); };
  $('bellMon').onclick=()=>{ $('bellDd').style.display='none';
    openMonitorLauncher(null,$('btnBell')); };
  updateBellBadge(r?r.unseen:null);
}
setTimeout(()=>updateBellBadge(),2500);
setInterval(()=>updateBellBadge(),60000);

/* ── 🛰 multi-strategy monitor launcher ── */
async function openMonitorLauncher(preselectId,anchor){
  await loadStrats(); await loadSim();
  const strats=S.strategies.filter(s=>(s.status||'draft')!=='archived');
  if(!strats.length) return toast('save a strategy first (Strategy view)','err');
  const p=popAt(anchor||$('btnBell'),`<h4>🛰 monitor strategies</h4>
    <div class="row muted" style="font-size:11px">Every selected strategy gets its OWN live monitor:
      signals re-checked on fresh bars, each new entry/exit raises a 🔔 alert (optional Telegram push),
      and can auto-trade its own sleeve of a sim account. Run as many at once as you like.</div>
    <div class="row"><select id="mnStrats" multiple size="6" style="flex:1">${
      strats.map(s=>`<option value="${s.id}"${s.id===preselectId?' selected':''}>${esc(s.name)}${s.status==='accepted'?' ● live':''}</option>`).join('')}</select></div>
    <div class="row"><span class="muted" style="font-size:11px">asset</span>
      <select id="mnAsset" style="flex:1">${S.watch.filter(w=>!['macro','dyn'].includes(w.exchange)).map(w=>
        `<option value="${esc(w.id)}">${esc(w.symbol)} · ${esc(w.exchange)}</option>`).join('')}</select>
      <select id="mnTf" style="width:66px"><option>15m</option><option>1h</option><option>4h</option><option selected>1d</option></select></div>
    <div class="row"><span class="muted" style="font-size:11px">check every</span>
      <input id="mnInt" type="number" value="15" style="width:56px"><span class="muted">min</span>
      <label class="chip"><input type="checkbox" id="mnTg"> 📣 telegram</label></div>
    <div class="row"><span class="muted" style="font-size:11px">paper-trade</span>
      <select id="mnSim" style="flex:1"><option value="">— alerts only —</option>${
        S.simAccounts.map(a=>`<option value="${esc(a.id)}">◎ ${esc(a.name)}</option>`).join('')}
        <option value="__new">＋ create "forward-tests"</option></select>
      <input id="mnPct" type="number" value="20" style="width:52px"><span class="muted">% cash each</span></div>
    <div class="row"><button class="pri" id="mnGo" style="flex:1">🛰 start monitoring</button></div>`);
  p.querySelector('#mnGo').onclick=async()=>{
    /* read EVERYTHING before the popover closes */
    const ids=[...p.querySelector('#mnStrats').selectedOptions].map(o=>o.value);
    const w=S.watch.find(x=>x.id===p.querySelector('#mnAsset').value);
    const tf=p.querySelector('#mnTf').value;
    const interval=+p.querySelector('#mnInt').value||15;
    const tg=p.querySelector('#mnTg').checked;
    let sim=p.querySelector('#mnSim').value;
    const pct=+p.querySelector('#mnPct').value||20;
    if(!ids.length) return toast('select at least one strategy','err');
    if(!w) return toast('pick an asset','err');
    closePop();
    if(sim==='__new'){
      const c=await api('/markets/sim/create','POST',{name:'forward-tests',cash:100000});
      sim=(c&&c.id)||'';
      if(!sim) toast('sim account creation failed — monitoring with alerts only','err');
    }
    const ds=dsId(w.exchange,w.symbol,tf);
    const channels=['event'].concat(tg?['telegram']:[]);
    let ok=0,errs=[];
    for(const id of ids){
      const body={id,dataset_id:ds,interval_min:interval,channels};
      if(sim){ body.sim_account_id=sim; body.sim_pct=pct; }
      const r=await api('/markets/strategy/accept','POST',body);
      if(r&&r.ok) ok++; else errs.push((r&&r.error)||'?');
    }
    toast(`🛰 ${ok}/${ids.length} strategies now live on ${esc(w.symbol)} ${esc(tf)}`+
      (sim?' → paper-trading their own sleeves':''),ok?'ok':'err');
    if(errs.length) toast(esc(errs[0]),'err');
    loadStrats(); loadPaperRuns(); updateBellBadge();
  };
}
/* entry points: paper-runs header + strategy-view button row */
(function mountLauncherButtons(){
  document.querySelectorAll('#runLeft h4').forEach(h=>{
    if(h.textContent.includes('Live paper runs')){
      const b=document.createElement('button');
      b.className='ghost'; b.style.cssText='float:right;font-size:10px';
      b.textContent='＋ monitor…';
      b.onclick=e=>openMonitorLauncher(null,e.currentTarget);
      h.appendChild(b);
    }
  });
  const row=$('btnIndLab')&&$('btnIndLab').parentNode;
  if(row){
    const b=document.createElement('button');
    b.id='btnMonLaunch'; b.title='live-monitor strategies (multi-select) with alerts / telegram / paper trading';
    b.textContent='🛰';
    row.insertBefore(b,$('btnMlLab')||null);
    b.onclick=e=>openMonitorLauncher(S.curStrat?S.curStrat.id:null,e.currentTarget);
  }
})();
/* live badge + open-feed refresh on alerts */
const _handleEvR6=handleEvent;
handleEvent=function(ev){
  if(String(ev.type||'')==='markets.alert'){
    updateBellBadge();
    const dd=$('bellDd');
    if(dd&&dd.style.display==='block') renderBell();
  }
  _handleEvR6(ev);
};
