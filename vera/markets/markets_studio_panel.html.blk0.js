
'use strict';
/* ═══════════════════ core helpers ═══════════════════ */
const BASE = (() => {
  try { if (window.parent && window.parent !== window && window.parent._veraBase)
    return String(window.parent._veraBase).replace(/\/$/, ''); } catch(_) {}
  try { if (window._veraBase) return String(window._veraBase).replace(/\/$/, ''); } catch(_) {}
  try { const s = localStorage.getItem('vera_base'); if (s) return s.replace(/\/$/, ''); } catch(_) {}
  return '';
})();
async function api(path, method='GET', body=null, timeoutMs=60000){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body) opts.body=JSON.stringify(body);
  const ctl=new AbortController(); const timer=setTimeout(()=>ctl.abort(),timeoutMs);
  opts.signal=ctl.signal;
  try{
    const r=await fetch(BASE+path,opts);
    const t=await r.text(); clearTimeout(timer);
    if(!r.ok) return {error:'HTTP '+r.status+(t?': '+t.slice(0,160):'')};
    return t.trim()?JSON.parse(t):null;
  }catch(e){ clearTimeout(timer);
    return {error:(e&&e.name==='AbortError')?'timeout':String(e&&e.message||e)}; }
}
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const debounce=(fn,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);};};
const fmtPx=v=>{ if(v==null||!isFinite(v))return '—'; const a=Math.abs(v);
  return a>=100000?v.toLocaleString(undefined,{maximumFractionDigits:0})
    :a>=1000?v.toLocaleString(undefined,{maximumFractionDigits:2})
    :a>=1?v.toFixed(2):a>=0.01?v.toFixed(4):v.toPrecision(4); };
const fmtPct=(v,s=true)=>v==null||!isFinite(v)?'—':(s&&v>=0?'+':'')+Number(v).toFixed(2)+'%';
const fmtN=v=>v==null?'—':Math.abs(v)>=1e9?(v/1e9).toFixed(2)+'B':Math.abs(v)>=1e6?(v/1e6).toFixed(2)+'M':Math.abs(v)>=1e3?(v/1e3).toFixed(1)+'K':(+v).toFixed(2);
const slug=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')||'asset';
const dsId=(p,s,tf)=>'mkt.'+p+'.'+slug(s)+'.'+tf;
const keySplit=k=>{const i=k.indexOf(':');return i<0?['binance',k]:[k.slice(0,i),k.slice(i+1)];};
const pref=(k,v)=>{ if(v===undefined){try{return JSON.parse(localStorage.getItem('qs_'+k));}catch(_){return null;}}
  try{localStorage.setItem('qs_'+k,JSON.stringify(v));}catch(_){}};
function toast(msg,kind=''){ const d=document.createElement('div');
  d.className='toast '+kind; d.innerHTML=msg;
  $('toasts').appendChild(d); setTimeout(()=>{d.style.opacity='0';d.style.transition='opacity .4s';
    setTimeout(()=>d.remove(),450);}, kind==='err'?7000:4200); }
const dayFmt=t=>new Date(t*1000).toISOString().slice(0,10);

/* live theme colors read from CSS vars (theme-aware canvas painting) */
const COL={};
function readTheme(){
  const cs=getComputedStyle(document.documentElement);
  const g=(n,f)=>{const v=cs.getPropertyValue(n).trim();return v||f;};
  Object.assign(COL,{
    bg:g('--bg1','#0b0f17'), bg0:g('--bg0','#070a10'), ink:g('--ink','#d9e1ed'),
    muted:g('--muted','#7e8ba0'), faint:'rgba(126,139,160,.45)',
    grid:'rgba(126,139,160,.10)', axis:'rgba(126,139,160,.35)',
    up:g('--up','#2fbf8f'), dn:g('--dn','#e05f5f'), acc:g('--acc','#5b8cff'),
    warn:g('--warn','#e8b34d'),
    cat:[g('--c1','#5b8cff'),g('--c2','#e8b34d'),g('--c3','#b48ead'),
         g('--c4','#4fc1b0'),g('--c5','#d1848b'),g('--c6','#8fb87a')],
  });
}
readTheme();
/* Repaint every canvas when the Vera theme changes (vera-ui.js stamps
   data-theme + inline vars on <html>) or the OS scheme flips. */
const _rethemeAll=debounce(()=>{ readTheme();
  try{ Object.values(TILES).forEach(t=>t.chart&&t.chart.invalidate()); }catch(_){}
  try{ if(S.pulse&&S.view==='pulse') renderPulse(); }catch(_){}
},120);
try{ new MutationObserver(_rethemeAll).observe(document.documentElement,
  {attributes:true,attributeFilter:['data-theme','style']}); }catch(_){}
try{ matchMedia('(prefers-color-scheme: dark)').addEventListener('change',_rethemeAll); }catch(_){}
function hexA(hex,a){ if(!/^#/.test(hex)) return hex;
  const n=parseInt(hex.slice(1),16),r=n>>16&255,g=n>>8&255,b=n&255;
  return `rgba(${r},${g},${b},${a})`; }
let ANIM_ON=true;

/* ═══════════════════ QChart — canvas chart engine ═══════════════════
   Panes: main (candles/line/area + overlays + volume + regime + markers +
   position shading + vlines) then N sub panes (own scale) then time axis.
   Compare series render on a secondary right scale in the main pane.
   No third-party code, no watermark — and every layer can animate.        */
class QChart{
  constructor(host,opts={}){
    this.host=host; this.opts=Object.assign({log:false,style:'candles',live:true},opts);
    this.cv=document.createElement('canvas'); host.appendChild(this.cv);
    this.ctx=this.cv.getContext('2d');
    this.bars=null; this.overlays=[]; this.subs=[]; this.compare=[];
    this.markers=[]; this.vlines=[]; this.regime=null; this.position=null;
    this.clipN=null; this.view=null;            /* {i0,i1} float index range */
    this.cross=null; this.crossExt=null;        /* local + external crosshair */
    this.anims={intro:0, draw:{}, regime:0};
    this._dirty=true; this._raf=null; this._animUntil=0;
    this._ro=new ResizeObserver(()=>{this._resize();});
    this._ro.observe(host);
    this._bind();
    this._resize();
    this._loop=this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }
  destroy(){ this._dead=true; try{this._ro.disconnect();}catch(_){}
    try{this.cv.remove();}catch(_){} }
  invalidate(){ this._dirty=true; }
  _animate(ms){ this._animUntil=Math.max(this._animUntil,performance.now()+ms); this._dirty=true; }
  _resize(){
    const r=this.host.getBoundingClientRect();
    const dpr=window.devicePixelRatio||1;
    this.W=Math.max(60,r.width); this.H=Math.max(60,r.height);
    this.cv.width=this.W*dpr; this.cv.height=this.H*dpr;
    this.ctx.setTransform(dpr,0,0,dpr,0,0);
    this._dirty=true;
  }
  setBars(b,fit=true){
    this.bars=b; this.clipN=null;
    if(fit||!this.view) this.fit();
    if(ANIM_ON){ this.anims.intro=0; this._animate(650); } else this.anims.intro=1;
    this._dirty=true;
  }
  setStyle(s){ this.opts.style=s; this._dirty=true; }
  setOverlays(o){ this.overlays=o||[];
    if(ANIM_ON) o.forEach(s=>{ if(!(s.id in this.anims.draw)){ this.anims.draw[s.id]=0; this._animate(700);} });
    this._dirty=true; }
  setSubs(s){ this.subs=s||[]; this._dirty=true; }
  setCompare(c){ this.compare=c||[];
    if(ANIM_ON) c.forEach(s=>{ if(!(('cmp_'+s.id) in this.anims.draw)){ this.anims.draw['cmp_'+s.id]=0; this._animate(700);} });
    this._dirty=true; }
  setMarkers(m){ this.markers=m||[]; this._dirty=true; }
  setVLines(v){ this.vlines=v||[]; this._dirty=true; }
  setRegime(r){ this.regime=r; if(r&&ANIM_ON){ this.anims.regime=0; this._animate(900);} this._dirty=true; }
  setPivots(p){ this.pivots=p; if(p&&ANIM_ON){ this.anims.regime=0; this._animate(900);} this._dirty=true; }
  setAnns(a){ this.anns=a||[]; this._dirty=true; }
  coordsAt(x,y){
    /* inverse transforms: pixel → {t,p} on the main pane (drawing tools) */
    if(!this.bars||!this.view) return null;
    const b=this.bars,{i0,i1}=this.view;
    const fi=i0+(x-this.padL)/this.plotW*(i1-i0);
    const dt=b.t.length>1?(b.t[1]-b.t[0]):86400;
    let t;
    const lo2=Math.max(0,Math.min(this.n-1,Math.floor(fi)));
    if(fi<0) t=b.t[0]+fi*dt;
    else if(fi>=this.n-1) t=b.t[this.n-1]+(fi-(this.n-1))*dt;
    else t=b.t[lo2]+(fi-lo2)*(b.t[Math.min(this.n-1,lo2+1)]-b.t[lo2]);
    const lo=this.plo,hi=this.phi;
    let p;
    if(this.opts.log&&lo>0)
      p=Math.exp(Math.log(lo)+(1-y/this.mainH)*(Math.log(hi)-Math.log(lo)));
    else p=lo+(1-y/this.mainH)*(hi-lo);
    return {t:Math.round(t),p};
  }
  _annXY(pt,lo,hi){
    const b=this.bars;
    const dt=b.t.length>1?(b.t[1]-b.t[0]):86400;
    let a=0,z=this.n-1;
    while(a<z){const m=(a+z)>>1; b.t[m]<pt.t?a=m+1:z=m;}
    /* fractional index incl. beyond-edge extrapolation */
    let fi=a;
    if(pt.t<b.t[0]) fi=(pt.t-b.t[0])/dt;
    else if(pt.t>b.t[this.n-1]) fi=(this.n-1)+(pt.t-b.t[this.n-1])/dt;
    else if(a>0){ const t0=b.t[a-1],t1=b.t[a];
      fi=a-1+(pt.t-t0)/Math.max(1,t1-t0); }
    return [this._x(fi), pt.p!=null?this._yOf(pt.p,lo,hi,0,this.mainH):null];
  }
  _annLayer(lo,hi){
    if(!this.anns||!this.anns.length) return;
    const ctx=this.ctx;
    this.anns.forEach(a=>{
      const pts=(a.points||[]).map(p=>this._annXY(p,lo,hi));
      const col=a.color||(a.author==='vera'?COL.warn:COL.acc);
      ctx.strokeStyle=col; ctx.fillStyle=col; ctx.lineWidth=1.5;
      const k=a.kind;
      if(k==='hline'){
        const p=(a.points||[]).find(x=>x.p!=null);
        if(p==null) return;
        const y=this._yOf(p.p,lo,hi,0,this.mainH);
        ctx.setLineDash([6,4]);
        ctx.beginPath(); ctx.moveTo(this.padL,y); ctx.lineTo(this.padL+this.plotW,y); ctx.stroke();
        ctx.setLineDash([]);
        if(a.text){ ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign='left';
          ctx.fillText(a.text.slice(0,40),this.padL+6,y-4); }
      } else if((k==='trendline'||k==='ray')&&pts.length>=2){
        let [x1,y1]=pts[0],[x2,y2]=pts[1];
        if(k==='ray'&&x2!==x1){ /* extend to the right edge */
          const slope=(y2-y1)/(x2-x1);
          const xe=this.padL+this.plotW;
          y2=y1+slope*(xe-x1); x2=xe;
        }
        ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
        if(a.text){ ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign='left';
          ctx.fillText(a.text.slice(0,40),Math.min(x1,x2)+4,Math.min(y1,y2)-5); }
      } else if(k==='rect'&&pts.length>=2){
        const x=Math.min(pts[0][0],pts[1][0]),y=Math.min(pts[0][1],pts[1][1]);
        const w=Math.abs(pts[1][0]-pts[0][0]),h=Math.abs(pts[1][1]-pts[0][1]);
        ctx.fillStyle=hexA(col,.10); ctx.fillRect(x,y,w,h);
        ctx.strokeRect(x,y,w,h);
        if(a.text){ ctx.fillStyle=col; ctx.font='10px ui-monospace,Consolas,monospace';
          ctx.textAlign='left'; ctx.fillText(a.text.slice(0,40),x+4,y+12); }
      } else if((k==='label'||k==='arrow')&&pts.length>=1){
        const [x,y]=pts[0];
        ctx.beginPath(); ctx.arc(x,y,3,0,7); ctx.fill();
        if(a.text){ ctx.font='10.5px ui-monospace,Consolas,monospace'; ctx.textAlign='left';
          const w=ctx.measureText(a.text.slice(0,48)).width;
          ctx.fillStyle=hexA(COL.bg0.startsWith('#')?COL.bg0:'#070a10',.85);
          ctx.fillRect(x+6,y-16,w+8,14);
          ctx.fillStyle=col; ctx.fillText(a.text.slice(0,48),x+10,y-5); }
      } else if(k==='fib'&&pts.length>=2){
        const [x1,y1]=pts[0],[x2,y2]=pts[1];
        [0,0.236,0.382,0.5,0.618,0.786,1].forEach(f=>{
          const y=y1+(y2-y1)*f;
          ctx.strokeStyle=hexA(col,f===0||f===1?.7:.35);
          ctx.beginPath(); ctx.moveTo(Math.min(x1,x2),y);
          ctx.lineTo(this.padL+this.plotW,y); ctx.stroke();
          ctx.fillStyle=hexA(col,.8); ctx.font='9px ui-monospace,Consolas,monospace';
          ctx.textAlign='left'; ctx.fillText(f.toFixed(3),this.padL+this.plotW-34,y-2);
        });
      }
    });
  }
  setPosition(p){ this.position=p; this._dirty=true; }
  setClip(n){ this.clipN=n; this._dirty=true; }
  setCross(t){ this.crossExt=t; this._dirty=true; }
  get n(){ return this.bars? (this.clipN!=null?Math.min(this.clipN,this.bars.t.length):this.bars.t.length) : 0; }
  fit(){ if(!this.bars) return; const n=this.n||this.bars.t.length;
    this.view={i0:Math.max(0,n-Math.min(n,260)),i1:n}; this._dirty=true; }
  fitAll(){ if(!this.bars) return; this.view={i0:0,i1:this.n}; this._dirty=true; }
  zoomAt(f,px){
    if(!this.view) return;
    const {i0,i1}=this.view, span=i1-i0;
    const frac=(px-this.padL)/this.plotW;
    const pivot=i0+span*Math.max(0,Math.min(1,frac));
    let ns=Math.max(8,Math.min(this.n*1.05,span*f));
    this.view={i0:pivot-(pivot-i0)*(ns/span), i1:pivot+(i1-pivot)*(ns/span)};
    this._clampView(); this._dirty=true;
  }
  pan(dpx){
    if(!this.view) return;
    const span=this.view.i1-this.view.i0;
    const di=dpx/this.plotW*span;
    this.view.i0-=di; this.view.i1-=di; this._clampView(); this._dirty=true;
  }
  _clampView(){
    const n=this.n, span=this.view.i1-this.view.i0;
    if(this.view.i0<-span*0.6) {this.view.i0=-span*0.6; this.view.i1=this.view.i0+span;}
    if(this.view.i1>n+span*0.25){this.view.i1=n+span*0.25; this.view.i0=this.view.i1-span;}
  }
  _bind(){
    const cv=this.cv;
    cv.addEventListener('wheel',e=>{ e.preventDefault();
      this.zoomAt(Math.pow(1.0016,e.deltaY), e.offsetX); },{passive:false});
    let drag=null;
    cv.addEventListener('mousedown',e=>{drag={x:e.clientX};});
    window.addEventListener('mousemove',e=>{
      if(drag){ this.pan(e.clientX-drag.x); drag.x=e.clientX; }});
    window.addEventListener('mouseup',()=>drag=null);
    cv.addEventListener('mousemove',e=>{
      this.cross={x:e.offsetX,y:e.offsetY}; this._dirty=true;
      if(this.onHover&&this.bars){
        const i=this._iAt(e.offsetX);
        this.onHover(i!=null?this.bars.t[i]:null);
      }});
    cv.addEventListener('mouseleave',()=>{ this.cross=null; this._dirty=true;
      if(this.onHover) this.onHover(null); });
    cv.addEventListener('dblclick',()=>this.fitAll());
  }
  _iAt(x){
    if(!this.view||!this.n) return null;
    const {i0,i1}=this.view;
    let i=Math.round(i0+(x-this.padL)/this.plotW*(i1-i0));
    return Math.max(0,Math.min(this.n-1,i));
  }
  _loop(ts){
    if(this._dead) return;
    const animating=ts<this._animUntil || (this.opts.live&&ANIM_ON&&this.bars&&!document.hidden&&(ts%100<50));
    if(this._dirty||ts<this._animUntil||this._pulseTick(ts)){
      this._advance(ts); this._render(); this._dirty=false;
    }
    requestAnimationFrame(this._loop);
  }
  _pulseTick(ts){ /* ~8fps repaint for the live-price pulse only */
    if(!(this.opts.live&&ANIM_ON&&this.bars)||document.hidden) return false;
    if(!this._lastPulse||ts-this._lastPulse>120){ this._lastPulse=ts; return true; }
    return false;
  }
  _advance(){
    const step=ANIM_ON?0.055:1;
    if(this.anims.intro<1) this.anims.intro=Math.min(1,this.anims.intro+step);
    if(this.anims.regime<1) this.anims.regime=Math.min(1,this.anims.regime+step*0.8);
    for(const k in this.anims.draw)
      if(this.anims.draw[k]<1) this.anims.draw[k]=Math.min(1,this.anims.draw[k]+step*0.9);
  }
  /* ── scales ── */
  _layout(){
    this.padL=6; this.padAxis=56; this.axisH=20;
    this.plotW=this.W-this.padL-this.padAxis;
    const subH=Math.min(110,Math.max(56,this.H*0.16));
    this.subTop=this.H-this.axisH-this.subs.length*subH;
    this.subH=subH;
    this.mainH=this.subTop-4;
  }
  _x(i){ const {i0,i1}=this.view; return this.padL+(i-i0)/(i1-i0)*this.plotW; }
  _priceRange(){
    const b=this.bars,{i0,i1}=this.view;
    let lo=Infinity,hi=-Infinity;
    const a=Math.max(0,Math.floor(i0)), z=Math.min(this.n-1,Math.ceil(i1));
    for(let i=a;i<=z;i++){ if(b.l[i]<lo)lo=b.l[i]; if(b.h[i]>hi)hi=b.h[i]; }
    this.overlays.forEach(o=>{ for(let i=a;i<=z&&i<o.vals.length;i++){
      const v=o.vals[i]; if(v==null||!isFinite(v))continue; if(v<lo)lo=v; if(v>hi)hi=v; }});
    if(!isFinite(lo)||!isFinite(hi)){lo=0;hi=1;}
    if(hi-lo<1e-12) {hi+=1;lo-=1;}
    const pad=(hi-lo)*0.07; return [lo-pad,hi+pad];
  }
  _yOf(v,lo,hi,top,h){
    if(this.opts.log&&lo>0){ const l=Math.log(lo),g=Math.log(hi);
      return top+h-(Math.log(Math.max(1e-12,v))-l)/(g-l)*h; }
    return top+h-(v-lo)/(hi-lo)*h;
  }
  /* ── render ── */
  _render(){
    this._layout();
    const ctx=this.ctx; ctx.clearRect(0,0,this.W,this.H);
    if(!this.bars||!this.n||!this.view){ this._renderEmpty(); return; }
    const [lo,hi]=this._priceRange();
    this.plo=lo; this.phi=hi;
    this._grid(lo,hi);
    this._positionShade();
    this._regimeLayer(lo,hi);
    this._pivotLayer(lo,hi);
    this._candles(lo,hi);
    this._overlayLines(lo,hi);
    this._compareLines();
    this._volume();
    this._subPanes();
    this._vlineLayer();
    this._annLayer(lo,hi);
    this._markerLayer(lo,hi);
    this._lastPrice(lo,hi);
    this._timeAxis();
    this._crosshair(lo,hi);
    this._legend();
  }
  _renderEmpty(){
    const ctx=this.ctx; ctx.fillStyle=COL.faint; ctx.font='12px '+getComputedStyle(document.body).fontFamily;
    ctx.textAlign='center'; ctx.fillText('no data — pick an asset',this.W/2,this.H/2);
  }
  _grid(lo,hi){
    const ctx=this.ctx; ctx.strokeStyle=COL.grid; ctx.lineWidth=1;
    ctx.fillStyle=COL.muted; ctx.font='10px '+'ui-monospace,Consolas,monospace'; ctx.textAlign='left';
    const ticks=this._priceTicks(lo,hi,Math.max(3,Math.floor(this.mainH/56)));
    ticks.forEach(v=>{
      const y=this._yOf(v,lo,hi,0,this.mainH);
      ctx.beginPath(); ctx.moveTo(this.padL,y+.5); ctx.lineTo(this.padL+this.plotW,y+.5); ctx.stroke();
      ctx.fillText(fmtPx(v),this.padL+this.plotW+6,y+3);
    });
  }
  _priceTicks(lo,hi,n){
    const span=hi-lo, raw=span/n, mag=Math.pow(10,Math.floor(Math.log10(raw)));
    const step=[1,2,2.5,5,10].map(m=>m*mag).find(s=>span/s<=n+1)||raw;
    const out=[]; for(let v=Math.ceil(lo/step)*step; v<hi; v+=step) out.push(v);
    return out;
  }
  _colRange(){ /* visible integer bar range */
    const a=Math.max(0,Math.floor(this.view.i0)), z=Math.min(this.n-1,Math.ceil(this.view.i1));
    return [a,z];
  }
  _candles(lo,hi){
    const ctx=this.ctx,b=this.bars,[a,z]=this._colRange();
    const barW=this.plotW/(this.view.i1-this.view.i0);
    const introN=a+(z-a+1)*(this.anims.intro); /* left→right reveal */
    const style=this.opts.style;
    if(style==='line'||style==='area'||barW<1.4){
      ctx.beginPath(); let started=false;
      for(let i=a;i<=z;i++){ if(i>introN)break;
        const x=this._x(i),y=this._yOf(b.c[i],lo,hi,0,this.mainH);
        started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true); }
      if(style==='area'){
        ctx.save(); ctx.lineTo(this._x(Math.min(z,introN)),this.mainH); ctx.lineTo(this._x(a),this.mainH); ctx.closePath();
        const g=ctx.createLinearGradient(0,0,0,this.mainH);
        g.addColorStop(0,hexA(COL.acc,.28)); g.addColorStop(1,hexA(COL.acc,0));
        ctx.fillStyle=g; ctx.fill(); ctx.restore();
        ctx.beginPath(); started=false;
        for(let i=a;i<=z;i++){ if(i>introN)break;
          const x=this._x(i),y=this._yOf(b.c[i],lo,hi,0,this.mainH);
          started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true); }
      }
      ctx.strokeStyle=COL.acc; ctx.lineWidth=1.6; ctx.stroke();
      return;
    }
    const w=Math.max(1,Math.min(13,barW*0.72));
    for(let i=a;i<=z;i++){
      if(i>introN) break;
      const x=this._x(i);
      const up=b.c[i]>=b.o[i];
      ctx.strokeStyle=ctx.fillStyle=up?COL.up:COL.dn;
      const yh=this._yOf(b.h[i],lo,hi,0,this.mainH), yl=this._yOf(b.l[i],lo,hi,0,this.mainH);
      ctx.beginPath(); ctx.moveTo(x,yh); ctx.lineTo(x,yl); ctx.lineWidth=1; ctx.stroke();
      const yo=this._yOf(b.o[i],lo,hi,0,this.mainH), yc=this._yOf(b.c[i],lo,hi,0,this.mainH);
      const t=Math.min(yo,yc), h=Math.max(1,Math.abs(yc-yo));
      if(up){ ctx.fillStyle=hexA(COL.up,.9); }
      ctx.fillRect(x-w/2,t,w,h);
    }
  }
  _overlayLines(lo,hi){
    const ctx=this.ctx,[a,z]=this._colRange();
    this.overlays.forEach((o,k)=>{
      const prog=this.anims.draw[o.id]??1;
      const upto=a+(z-a)*prog;
      ctx.beginPath(); let started=false;
      for(let i=a;i<=z&&i<o.vals.length;i++){
        if(i>upto)break;
        const v=o.vals[i];
        if(v==null||!isFinite(v)){started=false;continue;}
        const x=this._x(i),y=this._yOf(v,lo,hi,0,this.mainH);
        started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true);
      }
      ctx.strokeStyle=o.color||COL.cat[k%COL.cat.length];
      ctx.lineWidth=o.width||1.4;
      if(o.dash)ctx.setLineDash(o.dash);
      ctx.stroke(); ctx.setLineDash([]);
    });
  }
  _compareLines(){
    if(!this.compare.length) return;
    const ctx=this.ctx,b=this.bars,{i0,i1}=this.view;
    const t0=b.t[Math.max(0,Math.floor(Math.max(0,i0)))], t1=b.t[Math.min(this.n-1,Math.ceil(Math.min(this.n-1,i1)))];
    this.compare.forEach((s,k)=>{
      let lo=Infinity,hi=-Infinity;
      const xs=[],ys=[];
      for(let j=0;j<s.t.length;j++){
        if(s.t[j]<t0||s.t[j]>t1) continue;
        const v=s.v[j]; if(v==null||!isFinite(v))continue;
        if(v<lo)lo=v; if(v>hi)hi=v;
        xs.push(s.t[j]); ys.push(v);
      }
      if(!xs.length||!isFinite(lo)) return;
      if(hi-lo<1e-12){hi+=1;lo-=1;}
      const span=(t1-t0)||1;
      const prog=this.anims.draw['cmp_'+s.id]??1;
      ctx.beginPath();
      const upto=xs.length*prog;
      for(let j=0;j<xs.length;j++){
        if(j>upto)break;
        const x=this.padL+(xs[j]-t0)/span*this.plotW;
        const y=8+ (this.mainH-16) * (1-(ys[j]-lo)/(hi-lo));
        j===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
      }
      ctx.strokeStyle=s.color||COL.cat[(k+1)%COL.cat.length];
      ctx.lineWidth=1.3; ctx.setLineDash([5,3]); ctx.stroke(); ctx.setLineDash([]);
    });
  }
  _volume(){
    const b=this.bars; if(!b.v) return;
    const ctx=this.ctx,[a,z]=this._colRange();
    let vmax=0; for(let i=a;i<=z;i++) if(b.v[i]>vmax)vmax=b.v[i];
    if(vmax<=0) return;
    const h=this.mainH*0.14, top=this.mainH-h;
    const barW=Math.max(1,this.plotW/(this.view.i1-this.view.i0)*0.6);
    for(let i=a;i<=z;i++){
      const x=this._x(i), vh=b.v[i]/vmax*h;
      ctx.fillStyle=hexA(b.c[i]>=b.o[i]?COL.up:COL.dn,.22);
      ctx.fillRect(x-barW/2,top+h-vh,barW,vh);
    }
  }
  _subPanes(){
    const ctx=this.ctx,[a,z]=this._colRange();
    this.subs.forEach((sub,si)=>{
      const top=this.subTop+si*this.subH, h=this.subH-6;
      ctx.strokeStyle=COL.axis; ctx.beginPath();
      ctx.moveTo(this.padL,top+.5); ctx.lineTo(this.padL+this.plotW+this.padAxis,top+.5); ctx.stroke();
      let lo=Infinity,hi=-Infinity;
      if(sub.range){ [lo,hi]=sub.range; }
      else sub.series.forEach(s=>{ for(let i=a;i<=z&&i<s.vals.length;i++){
        const v=s.vals[i]; if(v==null||!isFinite(v))continue; if(v<lo)lo=v; if(v>hi)hi=v; }});
      if(!isFinite(lo)){lo=0;hi=1;} if(hi-lo<1e-12){hi+=1;lo-=1;}
      const pad=(hi-lo)*0.12; lo-=pad; hi+=pad;
      sub._lo=lo; sub._hi=hi; sub._top=top; sub._h=h;
      /* guide lines (e.g. RSI 30/70) */
      (sub.guides||[]).forEach(gv=>{
        const y=top+6+(h-12)*(1-(gv-lo)/(hi-lo));
        ctx.strokeStyle=COL.grid; ctx.setLineDash([3,4]);
        ctx.beginPath(); ctx.moveTo(this.padL,y); ctx.lineTo(this.padL+this.plotW,y); ctx.stroke();
        ctx.setLineDash([]);
      });
      sub.series.forEach((s,k)=>{
        const col=s.color||COL.cat[k%COL.cat.length];
        const prog=this.anims.draw[sub.id]??1;
        const upto=a+(z-a)*prog;
        if(s.type==='hist'){
          const barW=Math.max(1,this.plotW/(this.view.i1-this.view.i0)*0.55);
          const y0=top+6+(h-12)*(1-(0-lo)/(hi-lo));
          for(let i=a;i<=z&&i<s.vals.length;i++){
            if(i>upto)break;
            const v=s.vals[i]; if(v==null||!isFinite(v))continue;
            const y=top+6+(h-12)*(1-(v-lo)/(hi-lo));
            ctx.fillStyle=hexA(v>=0?COL.up:COL.dn,.65);
            ctx.fillRect(this._x(i)-barW/2,Math.min(y,y0),barW,Math.max(1,Math.abs(y-y0)));
          }
        } else {
          ctx.beginPath(); let started=false;
          for(let i=a;i<=z&&i<s.vals.length;i++){
            if(i>upto)break;
            const v=s.vals[i];
            if(v==null||!isFinite(v)){started=false;continue;}
            const x=this._x(i), y=top+6+(h-12)*(1-(v-lo)/(hi-lo));
            started?ctx.lineTo(x,y):(ctx.moveTo(x,y),started=true);
          }
          ctx.strokeStyle=col; ctx.lineWidth=1.3; ctx.stroke();
        }
      });
      ctx.fillStyle=COL.faint; ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign='left';
      ctx.fillText(sub.label||sub.id, this.padL+4, top+13);
      ctx.textAlign='left';
      ctx.fillStyle=COL.muted;
      ctx.fillText(fmtN(hi), this.padL+this.plotW+6, top+14);
      ctx.fillText(fmtN(lo), this.padL+this.plotW+6, top+h-2);
    });
  }
  _positionShade(){
    if(!this.position) return;
    const ctx=this.ctx,[a,z]=this._colRange();
    ctx.fillStyle=hexA(COL.up,.06);
    let start=null;
    for(let i=a;i<=Math.min(z+1,this.position.length);i++){
      const inpos=i<=z&&this.position[i]===1&&(this.clipN==null||i<this.clipN);
      if(inpos&&start==null) start=i;
      if(!inpos&&start!=null){
        ctx.fillRect(this._x(start),0,this._x(i)-this._x(start),this.mainH); start=null; }
    }
    if(start!=null) ctx.fillRect(this._x(start),0,this._x(z)-this._x(start),this.mainH);
  }
  _regimeLayer(lo,hi){
    const r=this.regime; if(!r||!r.segments) return;
    const ctx=this.ctx,b=this.bars;
    const tToI=t=>{ /* binary search into bar times */
      let a=0,z=this.n-1;
      while(a<z){const m=(a+z)>>1; b.t[m]<t?a=m+1:z=m;}
      return a; };
    const prog=this.anims.regime;
    /* overall fit + channel */
    if(r.overall&&this.showOverall!==false){
      const o=r.overall;
      const x0=this._x(tToI(o.t0)), x1=this._x(tToI(o.t1));
      const y=(p)=>this._yOf(p,lo,hi,0,this.mainH);
      ctx.save(); ctx.globalAlpha=Math.min(1,prog*1.4);
      ctx.strokeStyle=hexA(COL.acc,.75); ctx.lineWidth=1.4; ctx.setLineDash([7,5]);
      ctx.beginPath(); ctx.moveTo(x0,y(o.p0)); ctx.lineTo(x1,y(o.p1)); ctx.stroke();
      ctx.setLineDash([2,5]); ctx.strokeStyle=hexA(COL.acc,.35);
      ctx.beginPath(); ctx.moveTo(x0,y(o.upper0)); ctx.lineTo(x1,y(o.upper1)); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x0,y(o.lower0)); ctx.lineTo(x1,y(o.lower1)); ctx.stroke();
      ctx.setLineDash([]); ctx.restore();
    }
    /* piecewise segments — draw sequentially with the anim progress */
    const segs=r.segments, nSeg=segs.length;
    segs.forEach((s,k)=>{
      const segProg=Math.max(0,Math.min(1,prog*nSeg-k));
      if(segProg<=0) return;
      const x0=this._x(tToI(s.t0)), x1f=this._x(tToI(s.t1));
      const x1=x0+(x1f-x0)*segProg;
      const y0=this._yOf(s.p0,lo,hi,0,this.mainH);
      const y1f=this._yOf(s.p1,lo,hi,0,this.mainH);
      const y1=y0+(y1f-y0)*segProg;
      const col=s.label==='bull'?COL.up:s.label==='bear'?COL.dn:COL.muted;
      ctx.save();
      ctx.shadowColor=hexA(col,.6); ctx.shadowBlur=6;
      ctx.strokeStyle=col; ctx.lineWidth=2.2;
      ctx.beginPath(); ctx.moveTo(x0,y0); ctx.lineTo(x1,y1); ctx.stroke();
      ctx.restore();
      /* pivot dot */
      ctx.fillStyle=col;
      ctx.beginPath(); ctx.arc(x0,y0,2.6,0,7); ctx.fill();
    });
  }
  _pivotLayer(lo,hi){
    const P=this.pivots; if(!P||!(P.pivots||[]).length) return;
    const ctx=this.ctx,b=this.bars;
    const X=t=>{ let a=0,z=this.n-1;
      while(a<z){const m=(a+z)>>1; b.t[m]<t?a=m+1:z=m;} return this._x(a); };
    const Y=p=>this._yOf(p,lo,hi,0,this.mainH);
    const prog=this.anims.regime;
    /* zigzag polyline draws in sequentially */
    const line=P.line||[];
    if(line.length>1){
      ctx.save();
      ctx.strokeStyle=hexA(COL.acc,.8); ctx.lineWidth=1.6;
      ctx.shadowColor=hexA(COL.acc,.5); ctx.shadowBlur=5;
      ctx.beginPath();
      const upto=Math.max(2,Math.floor(line.length*prog));
      line.slice(0,upto).forEach((p,i)=>{
        const x=X(p.t),y=Y(p.p);
        i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
      ctx.stroke(); ctx.restore();
    }
    (P.pivots||[]).forEach((p,i)=>{
      if(i/Math.max(1,P.pivots.length)>prog) return;
      const x=X(p.t),y=Y(p.p);
      if(x<this.padL-4||x>this.padL+this.plotW+4) return;
      const col=p.kind==='high'?COL.dn:p.kind==='low'?COL.up:COL.acc;
      ctx.fillStyle=col;
      ctx.beginPath();
      ctx.moveTo(x,y-4); ctx.lineTo(x+4,y); ctx.lineTo(x,y+4); ctx.lineTo(x-4,y);
      ctx.closePath(); ctx.fill();
      ctx.strokeStyle=hexA(COL.bg0.startsWith('#')?COL.bg0:'#070a10',.8);
      ctx.lineWidth=1; ctx.stroke();
    });
  }
  _vlineLayer(){
    if(!this.vlines.length) return;
    const ctx=this.ctx,b=this.bars;
    this.vlines.forEach(v=>{
      if(v.t<b.t[0]||v.t>b.t[this.n-1]+((b.t[1]-b.t[0])||0)*30) return;
      let a=0,z=this.n-1;
      while(a<z){const m=(a+z)>>1; b.t[m]<v.t?a=m+1:z=m;}
      const x=this._x(a);
      if(x<this.padL-2||x>this.padL+this.plotW+2) return;
      ctx.strokeStyle=hexA(v.color||COL.warn,.55); ctx.setLineDash([4,4]); ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,this.H-this.axisH); ctx.stroke(); ctx.setLineDash([]);
      if(v.label){
        ctx.save(); ctx.translate(x-3,26); ctx.rotate(-Math.PI/2);
        ctx.fillStyle=hexA(v.color||COL.warn,.9); ctx.font='9.5px ui-monospace,Consolas,monospace';
        ctx.textAlign='right'; ctx.fillText(String(v.label).slice(0,26),0,0); ctx.restore();
      }
    });
  }
  _markerLayer(lo,hi){
    if(!this.markers.length) return;
    const ctx=this.ctx,b=this.bars;
    const lastT=this.clipN!=null&&this.clipN>0?b.t[this.clipN-1]:Infinity;
    this.markers.forEach(m=>{
      if(m.t>lastT) return;
      let a=0,z=this.n-1;
      while(a<z){const mm=(a+z)>>1; b.t[mm]<m.t?a=mm+1:z=mm;}
      const x=this._x(a);
      if(x<this.padL-8||x>this.padL+this.plotW+8) return;
      /* buy ▲ below (up) · sell ▼ above (dn) · short ▼ above (violet)
         · cover ▲ below (violet) · stop ▼ above (warn) */
      const buy=m.kind==='buy'||m.kind==='cover';
      const y=this._yOf(buy?b.l[a]:b.h[a],lo,hi,0,this.mainH)+(buy?12:-12);
      const col=m.kind==='buy'?COL.up
        :m.kind==='stop'?COL.warn
        :(m.kind==='short'||m.kind==='cover')?COL.cat[2]:COL.dn;
      /* pop ring for fresh replay markers */
      if(m._born && performance.now()-m._born<600){
        const p=(performance.now()-m._born)/600;
        ctx.strokeStyle=hexA(col,1-p); ctx.lineWidth=2;
        ctx.beginPath(); ctx.arc(x,y,4+p*14,0,7); ctx.stroke();
        this._animate(60);
      }
      ctx.fillStyle=col;
      ctx.beginPath();
      if(buy){ ctx.moveTo(x,y-5); ctx.lineTo(x-4.5,y+3); ctx.lineTo(x+4.5,y+3); }
      else   { ctx.moveTo(x,y+5); ctx.lineTo(x-4.5,y-3); ctx.lineTo(x+4.5,y-3); }
      ctx.closePath(); ctx.fill();
    });
  }
  _lastPrice(lo,hi){
    const b=this.bars, i=this.n-1;
    if(i<0) return;
    const y=this._yOf(b.c[i],lo,hi,0,this.mainH);
    const up=i>0? b.c[i]>=b.c[i-1] : true;
    const col=up?COL.up:COL.dn;
    const ctx=this.ctx;
    ctx.strokeStyle=hexA(col,.5); ctx.setLineDash([2,3]);
    ctx.beginPath(); ctx.moveTo(this.padL,y); ctx.lineTo(this.padL+this.plotW,y); ctx.stroke();
    ctx.setLineDash([]);
    /* price tag */
    ctx.fillStyle=col;
    const txt=fmtPx(b.c[i]);
    ctx.font='bold 10px ui-monospace,Consolas,monospace';
    const w=ctx.measureText(txt).width+10;
    ctx.beginPath(); ctx.roundRect(this.padL+this.plotW+2,y-8,w,16,4); ctx.fill();
    ctx.fillStyle=COL.bg0; ctx.textAlign='left'; ctx.fillText(txt,this.padL+this.plotW+7,y+3.5);
    /* live pulse dot on the last candle */
    if(this.opts.live&&ANIM_ON&&this.clipN==null){
      const x=this._x(i);
      const ph=(performance.now()%1600)/1600;
      ctx.fillStyle=hexA(col,.85*(1-ph));
      ctx.beginPath(); ctx.arc(x,y,3+ph*9,0,7); ctx.fill();
      ctx.fillStyle=col;
      ctx.beginPath(); ctx.arc(x,y,2.6,0,7); ctx.fill();
    }
  }
  _timeAxis(){
    const ctx=this.ctx,b=this.bars,{i0,i1}=this.view;
    const y=this.H-this.axisH;
    ctx.strokeStyle=COL.axis;
    ctx.beginPath(); ctx.moveTo(0,y+.5); ctx.lineTo(this.W,y+.5); ctx.stroke();
    ctx.fillStyle=COL.muted; ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign='center';
    const [a,z]=this._colRange();
    if(z<=a) return;
    const spanS=(b.t[z]-b.t[a])||1;
    const px=this.plotW/(i1-i0);
    const every=Math.max(1,Math.round(72/px));
    for(let i=a;i<=z;i+=every){
      const x=this._x(i);
      const d=new Date(b.t[i]*1000);
      const lbl=spanS>86400*370? d.toISOString().slice(0,7)
        : spanS>86400*3? d.toISOString().slice(5,10)
        : d.toISOString().slice(11,16);
      ctx.fillText(lbl,x,y+14);
      ctx.strokeStyle=COL.grid;
      ctx.beginPath(); ctx.moveTo(x,y); ctx.lineTo(x,y-4); ctx.stroke();
    }
  }
  _crosshair(lo,hi){
    const t=this.crossExt, c=this.cross;
    const ctx=this.ctx,b=this.bars;
    let i=null,x=null;
    if(c){ i=this._iAt(c.x); x=c.x; }
    else if(t!=null){ /* external sync: locate t */
      let a=0,z=this.n-1;
      while(a<z){const m=(a+z)>>1; b.t[m]<t?a=m+1:z=m;}
      if(Math.abs(b.t[a]-t)<= (b.t[1]-b.t[0]||1)*2){ i=a; x=this._x(a); }
    }
    if(i==null) return;
    ctx.strokeStyle=hexA(COL.ink,.25); ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(this._x(i),0); ctx.lineTo(this._x(i),this.H-this.axisH); ctx.stroke();
    if(c){
      ctx.beginPath(); ctx.moveTo(this.padL,c.y); ctx.lineTo(this.padL+this.plotW,c.y); ctx.stroke();
      if(c.y<this.mainH){
        const v=this.opts.log&&lo>0
          ? Math.exp(Math.log(lo)+(1-(c.y)/this.mainH)*(Math.log(hi)-Math.log(lo)))
          : lo+(1-c.y/this.mainH)*(hi-lo);
        ctx.fillStyle=COL.bg0===''?'#000':COL.ink;
        ctx.fillStyle=COL.muted;
        ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign='left';
        ctx.fillText(fmtPx(v),this.padL+this.plotW+6,c.y-4);
      }
    }
    ctx.setLineDash([]);
    /* tooltip */
    if(c&&i!=null){
      const rows=[['O',fmtPx(b.o[i])],['H',fmtPx(b.h[i])],['L',fmtPx(b.l[i])],['C',fmtPx(b.c[i])]];
      if(b.v&&b.v[i]!=null) rows.push(['V',fmtN(b.v[i])]);
      this.overlays.slice(0,5).forEach(o=>{
        const v=o.vals[i]; if(v!=null&&isFinite(v)) rows.push([o.label||o.id,fmtPx(v)]); });
      this.compare.slice(0,3).forEach(s=>{
        /* nearest compare value */
        let a2=0,z2=s.t.length-1; if(z2<0)return;
        while(a2<z2){const m=(a2+z2)>>1; s.t[m]<b.t[i]?a2=m+1:z2=m;}
        if(s.v[a2]!=null) rows.push([s.label||s.id,fmtN(s.v[a2])]); });
      const w=118, lh=13, h=rows.length*lh+14;
      let tx=this._x(i)+12, ty=12;
      if(tx+w>this.W-this.padAxis) tx=this._x(i)-w-12;
      ctx.fillStyle=hexA(COL.bg0.startsWith('#')?COL.bg0:'#070a10',.92);
      ctx.strokeStyle=COL.axis;
      ctx.beginPath(); ctx.roundRect(tx,ty,w,h,7); ctx.fill(); ctx.stroke();
      ctx.font='10px ui-monospace,Consolas,monospace';
      ctx.fillStyle=COL.muted; ctx.textAlign='left';
      ctx.fillText(dayFmt(b.t[i]),tx+8,ty+11);
      rows.forEach((r,k)=>{
        ctx.fillStyle=COL.muted; ctx.fillText(String(r[0]).slice(0,9),tx+8,ty+24+k*lh);
        ctx.fillStyle=COL.ink; ctx.textAlign='right'; ctx.fillText(r[1],tx+w-8,ty+24+k*lh);
        ctx.textAlign='left';
      });
    }
  }
  _legend(){
    const ctx=this.ctx;
    if(!this.overlays.length&&!this.compare.length) return;
    ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign='left';
    let y=14;
    const item=(col,txt)=>{ ctx.fillStyle=col; ctx.fillRect(this.padL+4,y-7,8,8);
      ctx.fillStyle=COL.muted; ctx.fillText(txt,this.padL+17,y); y+=14; };
    this.overlays.slice(0,6).forEach((o,k)=>item(o.color||COL.cat[k%COL.cat.length],o.label||o.id));
    this.compare.slice(0,4).forEach((s,k)=>item(s.color||COL.cat[(k+1)%COL.cat.length],'⧉ '+(s.label||s.id)));
  }
}
if(!CanvasRenderingContext2D.prototype.roundRect){
  CanvasRenderingContext2D.prototype.roundRect=function(x,y,w,h,r){
    this.moveTo(x+r,y); this.arcTo(x+w,y,x+w,y+h,r); this.arcTo(x+w,y+h,x,y+h,r);
    this.arcTo(x,y+h,x,y,r); this.arcTo(x,y,x+w,y,r); this.closePath(); return this; };
}
