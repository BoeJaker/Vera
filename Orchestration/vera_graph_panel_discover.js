/**
 * vera_graph_panel_discover.js
 * Discover+ sidebar panel — 1:1 port of all controls from fabric_discovery_panel.html
 * plus: toggle switches, history grouped by topic, overwrite/expand/enhance modal
 */
(function(){
'use strict';
if(!window.veraUI||!window.veraUI.Graph||!window.veraUI.Graph.registerPanel)return;
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

window.veraUI.Graph.registerPanel({
  id:'discover',title:'Discover+',icon:'⊕',order:15,
  mount:function(bodyEl,graph,papi){
    var apiBase=(papi&&papi.apiBase)||(window._veraBase||'');
    async function api(path,method,body,ms){
      var ctrl=new AbortController();var to=setTimeout(function(){ctrl.abort();},ms||30000);
      try{var o={method:method||'GET',headers:{'Content-Type':'application/json'},signal:ctrl.signal};
        if(body)o.body=JSON.stringify(body);var r=await fetch(apiBase+path,o);var txt=await r.text();
        try{return JSON.parse(txt);}catch(e){return{error:txt||('HTTP '+r.status)};}
      }catch(e){return{error:(e&&e.name==='AbortError')?'timeout':(e&&e.message)||'net err'};}
      finally{clearTimeout(to);}
    }
    function q(sel){return bodyEl.querySelector(sel);}
    function intv(sel,def,mn,mx){var v=parseInt((q(sel)||{}).value||def,10);return Math.max(mn,Math.min(mx,isNaN(v)?def:v));}
    function chk(sel,def){var el=q(sel);return el?el.checked:def;}

    // ── State ────────────────────────────────────────────────────────────
    var seenNodes={},seenEdges={},lastGraph={nodes:[],edges:[]},nodeById={},degree={};
    var active={crawlId:'',datasetId:'',running:false};
    var pollTimer=null,lastCrawls=[],logLines=[];
    // Persist active crawl ID across panel reloads (sidebar gets re-mounted on tab switches)
    (function(){
      try {
        var saved = sessionStorage.getItem('vera_discover_active');
        if (saved) { var s = JSON.parse(saved); active.crawlId = s.crawlId || ''; active.datasetId = s.datasetId || ''; }
      } catch(_) {}
    })();
    function _saveActive() {
      try { sessionStorage.setItem('vera_discover_active', JSON.stringify({crawlId:active.crawlId, datasetId:active.datasetId})); } catch(_) {}
    }

    // ── CSS ──────────────────────────────────────────────────────────────
    if(!document.getElementById('vg-dc4-css')){
      var st=document.createElement('style');st.id='vg-dc4-css';
      st.textContent=
        '.vg-sb-panel-body:has(>.dc4){padding:0!important}'+
        '.dc4{font-size:10px;color:var(--text)}'+
        // section — MUST reset fabric_panel global .sec which applies display:flex etc.
        '.dc4 .sec{margin-bottom:3px!important;border:1px solid var(--border)!important;border-radius:3px!important;background:var(--bg0)!important;overflow:hidden!important;display:block!important;text-transform:none!important;letter-spacing:normal!important;gap:0!important;align-items:initial!important}'+
        '.dc4 .sec::after{display:none!important}'+
        '.dc4 .sec-hd{padding:5px 8px!important;font-size:9.5px!important;cursor:pointer;list-style:none;background:var(--bg1)!important;user-select:none;display:flex!important;align-items:center!important;justify-content:space-between;color:var(--text)!important;text-transform:none!important;letter-spacing:normal!important}'+
        '.dc4 .sec-hd::-webkit-details-marker{display:none}'+
        '.dc4 .sec[open] .sec-hd{border-bottom:1px solid var(--border)}'+
        '.dc4 .sec-body{padding:6px 8px;display:flex;flex-direction:column;gap:3px}'+
        // row
        '.dc4 .r{display:flex!important;align-items:center;gap:5px;font-size:9px;color:var(--dim2);flex-wrap:wrap;margin-bottom:0!important}'+
        '.dc4 .r label{min-width:72px!important;flex-shrink:0;color:var(--dim2);font-size:9px!important;width:auto!important;padding:0!important}'+
        '.dc4 .r input[type=text],.dc4 .r input:not([type]),.dc4 .r select{flex:1;min-width:0;font-size:9px!important;padding:3px 5px!important;background:var(--bg2)!important;border:1px solid var(--border)!important;color:var(--text)!important;border-radius:3px!important;font-family:var(--mono);outline:none;width:auto!important}'+
        '.dc4 .r input[type=text]:focus,.dc4 .r input:not([type]):focus,.dc4 .r select:focus{border-color:var(--acc)!important}'+
        '.dc4 .r input[type=number]{font-size:9px!important;padding:3px 4px!important;background:var(--bg2)!important;border:1px solid var(--border)!important;color:var(--text)!important;border-radius:3px!important;font-family:var(--mono);width:52px!important;text-align:right;outline:none}'+
        '.dc4 .r input[type=number]:focus{border-color:var(--acc)!important}'+
        // sub-label inline
        '.dc4 .lbl{color:var(--dim2);font-size:9px;flex-shrink:0}'+
        // toggle switch
        '.dc4 .tog-row{display:flex;align-items:center;gap:5px;font-size:9px;color:var(--dim2);padding:1px 0;cursor:pointer}'+
        '.dc4 .tog{position:relative;width:28px;height:15px;flex-shrink:0}'+
        '.dc4 .tog input{opacity:0;width:0;height:0;position:absolute}'+
        '.dc4 .tog-sl{position:absolute;inset:0;background:var(--bg3);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:.15s}'+
        '.dc4 .tog-sl::before{content:"";position:absolute;width:9px;height:9px;left:2px;top:2px;background:var(--dim);border-radius:50%;transition:.15s}'+
        '.dc4 .tog input:checked+.tog-sl{background:rgba(90,158,143,.2);border-color:var(--acc)}'+
        '.dc4 .tog input:checked+.tog-sl::before{background:var(--acc);transform:translateX(13px)}'+
        // wrap-row for inline toggles
        '.dc4 .wr{display:flex;flex-wrap:wrap;gap:4px 10px;padding:2px 0}'+
        '.dc4 .wr .tog-row{width:auto}'+
        // button
        '.dc4 .btn{font-size:9px;padding:4px 8px;background:rgba(90,158,143,.08);border:1px solid var(--acc);color:var(--acc);border-radius:3px;cursor:pointer;font-family:var(--mono);transition:.12s;white-space:nowrap}'+
        '.dc4 .btn:hover{background:rgba(90,158,143,.18)}'+
        '.dc4 .btn.teal{border-color:var(--acc2);color:var(--acc2)}'+
        '.dc4 .btn.teal:hover{background:rgba(143,184,122,.15)}'+
        '.dc4 .btn.warn{border-color:var(--err);color:var(--err)}'+
        '.dc4 .btn.warn:hover{background:rgba(201,107,107,.15)}'+
        '.dc4 .btn.map{border-color:var(--acc3);color:var(--acc3)}'+
        '.dc4 .btn.map:hover{background:rgba(201,149,90,.15)}'+
        '.dc4 .btn-row{display:flex;gap:4px;flex-wrap:wrap}'+
        // status
        '.dc4 .status{font-size:9px;min-height:12px;color:var(--dim2)}'+
        '.dc4 .status.ok{color:var(--ok)}.dc4 .status.err{color:var(--err)}'+
        // hint
        '.dc4 .hint{font-size:8.5px;color:var(--dim2);line-height:1.4;background:var(--bg1);border:1px solid var(--border);border-left:2px solid var(--acc3);border-radius:3px;padding:4px 7px}'+
        // list items
        '.dc4 .item{padding:4px 6px;border-bottom:1px solid var(--border);cursor:pointer;font-size:9px;display:flex;gap:5px;align-items:center}'+
        '.dc4 .item:hover{background:var(--bg2)}.dc4 .item.sel{border-left:2px solid var(--acc)}'+
        // topic groups in history
        '.dc4 .tgrp{border-bottom:1px solid var(--border)}'+
        '.dc4 .tgrp-hd{display:flex;align-items:center;gap:5px;padding:5px 7px;cursor:pointer;font-size:9px;background:var(--bg1);user-select:none}'+
        '.dc4 .tgrp-hd:hover{background:var(--bg2)}'+
        '.dc4 .tgrp-hd .tlbl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}'+
        '.dc4 .tgrp-runs{display:none;padding-left:12px}'+
        '.dc4 .tgrp.open .tgrp-runs{display:block}'+
        '.dc4 .run-item{display:flex;align-items:center;gap:5px;padding:3px 6px;cursor:pointer;font-size:8.5px;border-bottom:1px solid var(--border)}'+
        '.dc4 .run-item:hover{background:var(--bg2)}.dc4 .run-item.sel{border-left:2px solid var(--acc)}'+
        // dot
        '.dc4 .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--dim)}'+
        '.dc4 .dot.running{background:var(--ok);animation:dc4p 1s infinite}'+
        '.dc4 .dot.done{background:var(--ok)}.dc4 .dot.paused{background:var(--acc3)}.dc4 .dot.error{background:var(--err)}'+
        '@keyframes dc4p{0%,100%{opacity:1}50%{opacity:.3}}'+
        // pill
        '.dc4 .pill{font-size:7.5px;padding:1px 5px;border-radius:2px;border:1px solid;flex-shrink:0}'+
        // overlay
        '.dc4 .ov{position:sticky;top:0;z-index:5;background:var(--bg1);border-bottom:1px solid var(--border);padding:4px 8px;display:none;font-size:9px;gap:6px;align-items:center}'+
        // current strip
        '.dc4 .cur{padding:4px 8px;font-size:9px;border-bottom:1px solid var(--border);display:none;align-items:center;gap:6px;cursor:pointer}'+
        '.dc4 .cur:hover{background:var(--bg2)}'+
        // log
        '.dc4 .log{font-size:8px;color:var(--dim);font-family:var(--mono);max-height:100px;overflow-y:auto;background:var(--bg0);border:1px solid var(--border);border-radius:3px;padding:4px 6px;line-height:1.6}'+
        // models list
        '.dc4 .model-item{background:var(--bg1);border:1px solid var(--border);border-radius:3px;padding:5px 7px;margin-bottom:3px;cursor:pointer}'+
        '.dc4 .model-item:hover{border-color:var(--acc)}'+
        '.dc4 .model-item .mt{font-size:10px;font-weight:500;color:var(--text)}'+
        '.dc4 .model-item .ms{font-size:8.5px;color:var(--dim2);margin-top:1px}'+
        // choice modal
        '.dc4-modal{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:none;align-items:center;justify-content:center}'+
        '.dc4-modal.open{display:flex}'+
        '.dc4-mbox{width:min(400px,92vw);background:var(--bg1);border:1px solid var(--border);border-radius:5px;box-shadow:0 8px 40px rgba(0,0,0,.6);overflow:hidden}'+
        '.dc4-mhd{padding:10px 14px;border-bottom:1px solid var(--border);font-size:11px;font-weight:600;color:var(--text)}'+
        '.dc4-mbody{padding:12px 14px;display:flex;flex-direction:column;gap:6px}'+
        '.dc4-mft{padding:8px 14px;border-top:1px solid var(--border);display:flex;gap:6px;justify-content:flex-end}'+
        '.dc4-opt{display:flex;align-items:flex-start;gap:8px;padding:7px 9px;border:1px solid var(--border);border-radius:3px;cursor:pointer;transition:.12s}'+
        '.dc4-opt:hover{border-color:var(--acc);background:rgba(90,158,143,.06)}'+
        '.dc4-opt.sel{border-color:var(--acc);background:rgba(90,158,143,.12)}'+
        '.dc4-opt-ic{font-size:14px;flex-shrink:0}'+
        '.dc4-opt strong{display:block;font-size:9.5px;color:var(--text);margin-bottom:1px}'+
        '.dc4-opt span{font-size:8.5px;color:var(--dim2)}';
      document.head.appendChild(st);
    }

    // ── Helper: build a toggle row ────────────────────────────────────────
    function togRow(id,label,checked,title){
      return '<label class="tog-row"'+(title?' title="'+esc(title)+'"':'')+'>'+
        '<label class="tog"><input type="checkbox" class="'+id+'"'+(checked?' checked':'')+'>'+
        '<span class="tog-sl"></span></label>'+
        '<span>'+label+'</span></label>';
    }

    // ── Markup ───────────────────────────────────────────────────────────
    bodyEl.className=(bodyEl.className||'')+' dc4';
    bodyEl.innerHTML=
      // overlay / current strip
      '<div class="ov dc-ov"><span class="dot dc-ov-dot"></span><span class="dc-ov-line" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span><span class="dc-ov-counts" style="color:var(--dim);font-size:8px"></span></div>'+
      '<div class="cur dc-cur"><span class="dot dc-cur-dot"></span><span class="dc-cur-lbl" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span><span class="btn teal" style="padding:1px 6px;font-size:8px">↩ return</span></div>'+

      // ── TOPIC DISCOVERY ──────────────────────────────────────────────
      '<details class="sec" open><summary class="sec-hd"><span>⊕ Topic Discovery</span></summary><div class="sec-body">'+
        '<div class="r"><label>Topic</label><input type="text" class="dc-topic" placeholder="zelda lore, LLM inference…"></div>'+
        '<div class="r"><label>Seed URLs</label><input type="text" class="dc-seeds" placeholder="optional — comma-sep, skips search"></div>'+
        '<div class="r"><label>Max sources</label><input type="number" class="dc-max" value="5" min="1" max="15">'+
          '<span class="lbl">Type</span><select class="dc-type"><option value="all">all</option><option value="rss">rss</option><option value="api">api</option><option value="web">web</option><option value="scrape">scrape</option></select></div>'+
        '<div class="r"><label>Pages</label><input type="number" class="dc-pages" value="120" min="1" max="2000">'+
          '<span class="lbl">Dropoff</span><input type="number" class="dc-drop" value="3" min="0" max="20"></div>'+
        '<div class="r" title="Concurrent fetches and background entity workers"><label>Concurrency</label><input type="number" class="dc-conc" value="4" min="1" max="16">'+
          '<span class="lbl">Ent workers</span><input type="number" class="dc-workers" value="3" min="1" max="8"></div>'+
        '<div class="r" title="0 = unlimited. Page text cap limits text extracted per page (chars); record cap limits stored text."><label>Text cap</label><input type="number" class="dc-textcap" value="0" min="0" max="200000" title="Max chars extracted per page (0=unlimited)"><span class="lbl" style="white-space:nowrap">Record cap</span><input type="number" class="dc-reccap" value="0" min="0" max="200000" title="Max chars stored per record (0=unlimited)"></div>'+
        '<div class="r"><label>Angles</label><input type="number" class="dc-angles" value="6" min="1" max="50">'+
          '<span class="lbl">Expand rounds</span><input type="number" class="dc-rounds" value="2" min="0" max="30"></div>'+
        '<div class="r"><label>Min relevance</label><input type="number" class="dc-minrel" value="6" min="0" max="90">'+
          '<span style="font-size:8.5px;color:var(--dim2)">% — drop off-topic pages</span></div>'+
        '<div class="r"><label>Neighbor depth</label><input type="number" class="dc-ndepth" value="1" min="0" max="3"></div>'+

        '<div class="wr">'+
          togRow('dc-autosyn','Auto-synthesize 3rd-order',false,'Generate the 3rd-order topic model automatically when crawl finishes')+
          togRow('dc-inferedges','Infer complex edges',true,'Infer non-explicit relationships between entities')+
          togRow('dc-consol','Consolidate in tandem',true,'Merge cross-type/alias duplicates while crawling')+
        '</div>'+
        '<div class="wr">'+
          togRow('dc-drift','Topic-drift guard',true,'Reject pages that don\'t cover enough distinctive topic terms')+
          togRow('dc-llmdrift','LLM drift gate',false,'LLM second-opinion on borderline pages')+
          togRow('dc-swallow','Swallow rich domains',true,'Crawl a whole domain when it proves rich/authoritative')+
          togRow('dc-nodepth','Unlimited depth',false,'Ignore max depth — follow relevance & drop-off only')+
          togRow('dc-brief','LLM brief (goal refine)',false,'LLM builds & refines a research brief to steer relevance')+
        '</div>'+
        '<div class="wr">'+
          togRow('dc-promote','Auto-promote sources',false)+
          togRow('dc-same','Same domain only',false)+
          togRow('dc-noent','Skip entities',false)+
          togRow('dc-noloom','Skip loom',false)+
          togRow('dc-nocross','Loom this dataset only',false)+
          togRow('dc-nollm','No LLM query expansion',false)+
          togRow('dc-llmtag','LLM tag & score pages',true)+
          togRow('dc-llment','LLM entity extraction',true)+
        '</div>'+

        '<div class="r" style="padding-top:3px;border-top:1px dashed var(--border)"><span class="lbl" style="min-width:auto">Map depth</span>'+
          '<select class="dc-mapdepth" style="width:auto;flex:none"><option value="quick">quick</option><option value="standard" selected>standard</option><option value="deep">deep</option><option value="exhaustive">exhaustive</option></select>'+
          togRow('dc-steer','LLM steer',false,'LLM steers entity extraction focus')+
        '</div>'+
        '<div class="r"><label>Map focus</label><input type="text" class="dc-mapfocus" placeholder="optional — bias entity extraction (e.g. people & orgs)"></div>'+
        '<div class="r"><label>Map dataset</label><input type="text" class="dc-mapds" placeholder="optional dataset id for the map"></div>'+

        '<div style="font-size:8.5px;color:var(--dim2);margin:2px 0">Search sites</div>'+
        '<div class="wr">'+
          togRow('dc-site-all','All',false)+
          togRow('dc-site-reddit','Reddit',true)+
          togRow('dc-site-x','X',true)+
          togRow('dc-site-youtube','YouTube',true)+
          togRow('dc-site-news','News',true)+
          togRow('dc-site-github','GitHub',false)+
          togRow('dc-site-stackoverflow','StackOverflow',false)+
          togRow('dc-site-hackernews','HN',false)+
          togRow('dc-site-blogs','Blogs',false)+
          togRow('dc-site-wikipedia','Wikipedia',false)+
          togRow('dc-site-academic','Academic',false)+
          togRow('dc-site-forums','Forums',false)+
          togRow('dc-site-podcasts','Podcasts',false)+
          togRow('dc-site-mastodon','Mastodon',false)+
          togRow('dc-site-vera','Vera sources',true)+
        '</div>'+

        '<div class="r"><label>Exclude words</label><input type="text" class="dc-neg" placeholder="comma-sep — drop pages containing these"></div>'+
        '<div class="r"><label>Exclude URLs</label><input type="text" class="dc-negurl" placeholder="comma-sep url substrings to skip"></div>'+
        '<div class="r" title="If set, only accept pages where this word appears (exact or fuzzy match)"><label>Require word</label><input type="text" class="dc-reqword" placeholder="optional — must appear in page">'+
          '<select class="dc-reqmode" style="width:auto;flex:none"><option value="fuzzy">fuzzy</option><option value="exact">exact</option></select></div>'+
        '<div class="r"><label>Dataset</label><input type="text" class="dc-ds" placeholder="optional dataset id"></div>'+

        '<div class="btn-row" style="margin-top:2px">'+
          '<button class="btn dc-topicgo">⊕ Discover</button>'+
          '<button class="btn teal dc-topiccont">⇄ Action</button>'+
          '<button class="btn map dc-topicmap">◉ Map topic</button>'+
        '</div>'+
        '<div class="status dc-topic-status"></div>'+
        '<div class="hint">Topic discovery runs web searches across query angles, crawls all results, lets strong concepts trigger further searches — extracting entities, detecting surfaces, pulling sub-tables, and using Loom to tie to other datasets. Continue resumes the saved frontier.</div>'+
      '</div></details>'+

      // ── CRAWL URL ────────────────────────────────────────────────────
      '<details class="sec"><summary class="sec-hd"><span>⇗ Crawl URL</span></summary><div class="sec-body">'+
        '<div class="r"><label>Seed URL</label><input type="text" class="dc-curl" placeholder="https://example.com/docs"></div>'+
        '<div class="r"><label>Dataset</label><input type="text" class="dc-curlds" placeholder="auto from host"></div>'+
        '<div class="r"><label>Topic focus</label><input type="text" class="dc-curltopic" placeholder="optional keyword"></div>'+
        '<div class="r"><label>Pages</label><input type="number" class="dc-curlpages" value="60" min="1" max="2000">'+
          '<span class="lbl">Depth</span><input type="number" class="dc-curldepth" value="4" min="1" max="20"></div>'+
        '<div class="r" title="Concurrent fetches and entity workers"><label>Concurrency</label><input type="number" class="dc-curlconc" value="4" min="1" max="16">'+
          '<span class="lbl">Ent workers</span><input type="number" class="dc-curlworkers" value="3" min="1" max="8"></div>'+
        '<div class="wr">'+
          togRow('dc-curl-autosyn','Auto-synthesize 3rd-order',false)+
          togRow('dc-curl-inferedges','Infer edges',true)+
          togRow('dc-curl-consol','Consolidate',true)+
          '<div class="r" style="width:auto"><span class="lbl">Neighbor depth</span><input type="number" class="dc-curl-ndepth" value="1" min="0" max="3"></div>'+
        '</div>'+
        '<div class="wr">'+
          togRow('dc-curl-drift','Topic-drift guard',true)+
          togRow('dc-curl-llmdrift','LLM drift gate',false)+
          togRow('dc-curl-swallow','Swallow rich domains',true)+
          togRow('dc-curl-nodepth','Unlimited depth',false)+
          togRow('dc-curl-brief','LLM brief',false)+
        '</div>'+
        '<div class="wr">'+
          togRow('dc-curl-surf','Surfaces',true)+
          togRow('dc-curl-sub','Sub-tables',true)+
          togRow('dc-curl-same','Same domain',true)+
          togRow('dc-curl-promote','Auto-promote',false)+
          togRow('dc-curl-noent','Skip entities',false)+
        '</div>'+
        '<div class="r"><label>Exclude words</label><input type="text" class="dc-curl-neg" placeholder="comma-sep"></div>'+
        '<div class="r"><label>Exclude URLs</label><input type="text" class="dc-curl-negurl" placeholder="comma-sep url substrings"></div>'+
        '<div class="r" title="If set, only accept pages where this word appears"><label>Require word</label><input type="text" class="dc-curl-reqword" placeholder="optional — must appear in page">'+
          '<select class="dc-curl-reqmode" style="width:auto;flex:none"><option value="fuzzy">fuzzy</option><option value="exact">exact</option></select></div>'+
        '<div class="btn-row" style="margin-top:2px">'+
          '<button class="btn dc-crawlgo">⊕ Crawl</button>'+
          '<button class="btn teal dc-crawlcont">↻ Continue</button>'+
        '</div>'+
        '<div class="status dc-curl-status"></div>'+
      '</div></details>'+

      // ── HISTORY ──────────────────────────────────────────────────────
      '<details class="sec"><summary class="sec-hd"><span>⏰ History</span><span class="dc-hct" style="font-size:8px;color:var(--dim)"></span></summary><div class="sec-body" style="padding:2px 0">'+
        '<div style="display:flex;gap:4px;padding:4px 6px;border-bottom:1px solid var(--border)">'+
          '<button class="btn dc-hist-refresh" style="font-size:8.5px;padding:2px 6px">↻ Refresh</button>'+
          '<button class="btn warn dc-hist-clear" style="font-size:8.5px;padding:2px 6px">✕ Clear all</button>'+
          '<span class="status dc-hist-status" style="flex:1;padding-left:4px"></span>'+
        '</div>'+
        '<div class="dc-hlist"></div>'+
      '</div></details>'+

      // ── SURFACES ─────────────────────────────────────────────────────
      '<details class="sec"><summary class="sec-hd"><span>⬡ Surfaces</span><span class="dc-sct" style="font-size:8px;color:var(--dim)"></span></summary><div class="sec-body" style="padding:0">'+
        '<div class="dc-slist" style="max-height:160px;overflow-y:auto"></div>'+
      '</div></details>'+

      // ── SUB-TABLES ───────────────────────────────────────────────────
      '<details class="sec"><summary class="sec-hd"><span>▣ Sub-tables</span><span class="dc-tct" style="font-size:8px;color:var(--dim)"></span></summary><div class="sec-body" style="padding:0">'+
        '<div class="dc-tlist" style="max-height:160px;overflow-y:auto"></div>'+
      '</div></details>'+

      // ── 3RD-ORDER MODELS ─────────────────────────────────────────────
      '<details class="sec"><summary class="sec-hd"><span>◈ 3rd-order models</span><span class="dc-mct" style="font-size:8px;color:var(--dim)"></span></summary><div class="sec-body">'+
        '<div style="display:flex;gap:4px;margin-bottom:4px">'+
          '<button class="btn dc-models-refresh" style="font-size:8.5px;padding:2px 6px">↻ Refresh</button>'+
        '</div>'+
        '<div style="font-size:8.5px;color:var(--dim2);margin-bottom:4px">Distilled coherent topic representations. Build via Auto-synthesize or Map topic.</div>'+
        '<div class="dc-mlist"></div>'+
        '<div class="status dc-models-status"></div>'+
      '</div></details>'+

      // ── NER BACKEND ──────────────────────────────────────────────────
      '<details class="sec dc-ner-sec"><summary class="sec-hd"><span>○ NER backend</span><span class="dc-ner-active" style="font-size:8px;color:var(--dim);margin-left:4px"></span></summary><div class="sec-body">'+
        '<div style="font-size:8.5px;color:var(--dim2);margin-bottom:5px">Entity extraction backend. Changes apply to discovery crawls and entity graph builds.</div>'+
        '<div class="btn-row" style="margin-bottom:4px;gap:4px;flex-wrap:wrap">'+
          '<select class="dc-ner-backend" style="font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
            '<option value="auto">Auto (best available)</option>'+
            '<option value="gliner">GLiNER</option>'+
            '<option value="spacy">spaCy</option>'+
            '<option value="heuristic">Heuristic only</option>'+
          '</select>'+
          '<button class="btn dc-ner-apply" style="font-size:8.5px;padding:2px 8px">Apply</button>'+
          '<button class="btn dc-ner-status-btn" style="font-size:8.5px;padding:2px 6px">Status</button>'+
        '</div>'+
        '<div class="btn-row" style="margin-bottom:4px;gap:4px">'+
          '<input class="dc-ner-model" placeholder="Model override (e.g. en_core_web_trf, urchade/gliner_large)" style="flex:1;font-size:8.5px;padding:2px 5px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
        '</div>'+
        '<details class="sec" style="margin:4px 0"><summary class="sec-hd" style="font-size:8.5px">Install model package</summary><div class="sec-body" style="padding:4px 0">'+
          '<div style="font-size:8px;color:var(--dim2);margin-bottom:4px">pip install gliner or spacy, then optionally download a spaCy language model.</div>'+
          '<div class="btn-row" style="gap:4px;margin-bottom:4px;flex-wrap:wrap">'+
            '<select class="dc-ner-install-pkg" style="font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
              '<option value="gliner">gliner (pip)</option>'+
              '<option value="spacy">spacy (pip)</option>'+
              '<option value="">custom (see below)</option>'+
            '</select>'+
            '<input class="dc-ner-install-custom" placeholder="Custom pip spec" style="flex:1;min-width:100px;font-size:8.5px;padding:2px 5px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
          '</div>'+
          '<div class="btn-row" style="gap:4px;margin-bottom:4px">'+
            '<input class="dc-ner-install-spmodel" placeholder="spaCy model to download (e.g. en_core_web_sm)" style="flex:1;font-size:8.5px;padding:2px 5px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
            '<button class="btn dc-ner-install-btn" style="font-size:8.5px;padding:2px 8px">Install</button>'+
          '</div>'+
          '<div class="status dc-ner-install-st" style="font-size:8px"></div>'+
          '<div class="dc-ner-install-log" style="margin-top:4px;font-size:7.5px;color:var(--dim2);max-height:80px;overflow-y:auto;font-family:monospace;display:none"></div>'+
        '</div></details>'+
        '<details class="sec" style="margin:4px 0"><summary class="sec-hd" style="font-size:8.5px">GLiNER labels &amp; threshold</summary><div class="sec-body" style="padding:4px 0">'+
          '<div style="font-size:8px;color:var(--dim2);margin-bottom:4px">Comma-separated entity types GLiNER will extract. Leave blank to use defaults.</div>'+
          '<textarea class="dc-ner-labels" rows="3" placeholder="person, organisation, location, technology, product, event, date, number, ..." style="width:100%;box-sizing:border-box;font-size:8.5px;padding:3px 5px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px;resize:vertical;font-family:inherit"></textarea>'+
          '<div class="btn-row" style="gap:4px;margin-top:4px">'+
            '<label style="font-size:8.5px;color:var(--dim2)">Threshold</label>'+
            '<input type="number" class="dc-ner-threshold" value="0.4" min="0.05" max="0.95" step="0.05" style="width:56px;font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
            '<button class="btn dc-ner-labels-apply" style="font-size:8.5px;padding:2px 8px">Apply</button>'+
            '<button class="btn dc-ner-labels-load" style="font-size:8.5px;padding:2px 6px">Load current</button>'+
          '</div>'+
          '<div class="status dc-ner-labels-st" style="font-size:8px;margin-top:3px"></div>'+
        '</div></details>'+
        '<div class="status dc-ner-st" style="font-size:8px"></div>'+
        '<div class="dc-ner-info" style="margin-top:5px;font-size:8.5px;color:var(--dim2);line-height:1.5;display:none"></div>'+
      '</div></details>'+

      // ── ENTITY EXTRACTION ─────────────────────────────────────────────
      '<details class="sec dc-extract-sec"><summary class="sec-hd"><span>⧙ Extract entities</span><span class="dc-extract-active" style="font-size:8px;color:var(--dim);margin-left:4px"></span></summary><div class="sec-body">'+
        '<div style="font-size:8.5px;color:var(--dim2);margin-bottom:5px">Run entity and relationship extraction on the active dataset.</div>'+
        '<div class="btn-row" style="gap:4px;margin-bottom:4px;flex-wrap:wrap">'+
          '<input type="number" class="dc-extract-maxrec" value="500" min="10" max="5000" style="width:60px;font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px" title="Max records to process">'+
          '<span style="font-size:8px;color:var(--dim2);align-self:center">max recs</span>'+
          '<label style="display:flex;align-items:center;gap:3px;font-size:8.5px;color:var(--dim2)">'+
            '<input type="checkbox" class="dc-extract-llm" checked style="width:12px;height:12px">LLM</label>'+
          '<button class="btn dc-extract-run" style="font-size:8.5px;padding:2px 8px">Extract</button>'+
        '</div>'+
        '<div class="status dc-extract-st" style="font-size:8px"></div>'+
      '</div></details>'+

      // ── ASK / QUERY ──────────────────────────────────────────────────
      '<details class="sec dc-ask-sec"><summary class="sec-hd"><span>◎ Ask</span></summary><div class="sec-body">'+
        '<div style="font-size:8.5px;color:var(--dim2);margin-bottom:5px">Ask the LLM a question about what was discovered in the current dataset.</div>'+
        '<textarea class="dc-ask-q" rows="3" placeholder="e.g. What topics did you find? What entities appeared most?" style="width:100%;box-sizing:border-box;font-size:9.5px;padding:4px 6px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px;resize:vertical;font-family:inherit"></textarea>'+
        '<div class="btn-row" style="margin-top:4px">'+
          '<button class="btn dc-ask-go" style="font-size:8.5px;padding:2px 8px">Ask</button>'+
          '<span class="status dc-ask-status" style="flex:1;margin-left:6px"></span>'+
        '</div>'+
        '<div class="dc-ask-answer" style="margin-top:6px;font-size:9.5px;line-height:1.6;color:var(--text);white-space:pre-wrap;display:none;background:var(--bg1);border:1px solid var(--border);border-radius:3px;padding:6px 8px;max-height:220px;overflow-y:auto"></div>'+
      '</div></details>'+

      // ── COMPILE DOCUMENT ─────────────────────────────────────────────
      '<details class="sec dc-compile-sec"><summary class="sec-hd"><span>⊟ Compile</span></summary><div class="sec-body">'+
        '<div style="font-size:8.5px;color:var(--dim2);margin-bottom:5px">Compile a multi-section document from crawled pages about the topic.</div>'+
        '<div class="btn-row" style="margin-bottom:4px;gap:4px;flex-wrap:wrap">'+
          '<select class="dc-compile-style" style="font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'+
            '<option value="report">Report</option>'+
            '<option value="wiki">Wiki</option>'+
            '<option value="guide">Guide</option>'+
          '</select>'+
          '<input type="number" class="dc-compile-maxpages" value="40" min="5" max="120" style="width:48px;font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px" title="Max pages">'+
          '<span style="font-size:8px;color:var(--dim2);align-self:center">pages</span>'+
          '<input type="number" class="dc-compile-maxsec" value="6" min="2" max="12" style="width:40px;font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px" title="Max sections">'+
          '<span style="font-size:8px;color:var(--dim2);align-self:center">sections</span>'+
          '<button class="btn dc-compile-go" style="font-size:8.5px;padding:2px 8px">Compile</button>'+
        '</div>'+
        '<div class="status dc-compile-status"></div>'+
      '</div></details>'+

      // ── LOG ──────────────────────────────────────────────────────────
      '<details class="sec"><summary class="sec-hd"><span>▶ Log</span></summary><div class="sec-body">'+
        '<div class="log dc-log"></div>'+
        '<div class="btn-row" style="margin-top:3px;align-items:center">'+
          '<button class="btn" style="font-size:8.5px;padding:2px 6px" onclick="this.closest(\'.dc4\').querySelector(\'.dc-log\').innerHTML=\'\'">Clear</button>'+
          '<label class="tog-row dc-autodrawer-tog" title="Auto-open bottom drawer table/content panel when clicking a node" style="margin-left:auto">'+
            '<label class="tog"><input type="checkbox" class="dc-autodrawer" checked><span class="tog-sl"></span></label>'+
            '<span>Open drawer on click</span></label>'+
        '</div>'+
      '</div></details>';

    // ── Choice modal ─────────────────────────────────────────────────────
    var _choiceModal=document.createElement('div');_choiceModal.className='dc4-modal';
    _choiceModal.innerHTML=
      '<div class="dc4-mbox">'+
        '<div class="dc4-mhd">Topic already discovered</div>'+
        '<div class="dc4-mbody">'+
          '<div style="font-size:10px;color:var(--text);margin-bottom:4px">Existing data for <strong class="dc4-etopic" style="color:var(--acc)"></strong>. How should we proceed?</div>'+
          '<div class="dc4-opt sel" data-mode="expand"><span class="dc4-opt-ic">⊕</span><div><strong>Expand</strong><span>Add new pages to the existing dataset, skipping known URLs.</span></div></div>'+
          '<div class="dc4-opt" data-mode="enhance"><span class="dc4-opt-ic">✦</span><div><strong>Enhance</strong><span>Re-crawl with different angles, merging into existing graph.</span></div></div>'+
          '<div class="dc4-opt" data-mode="overwrite"><span class="dc4-opt-ic">↺</span><div><strong>Overwrite</strong><span>Clear existing dataset and start fresh. Old data will be lost.</span></div></div>'+
        '</div>'+
        '<div class="dc4-mft">'+
          '<button class="btn warn dc4-cancel" style="padding:3px 12px">Cancel</button>'+
          '<button class="btn dc4-confirm" style="padding:3px 16px">Continue</button>'+
        '</div>'+
      '</div>';
    (document.body||document.documentElement).appendChild(_choiceModal);
    var _choiceMode='expand';
    _choiceModal.querySelectorAll('.dc4-opt').forEach(function(o){
      o.onclick=function(){_choiceModal.querySelectorAll('.dc4-opt').forEach(function(x){x.classList.remove('sel');});o.classList.add('sel');_choiceMode=o.getAttribute('data-mode');};
    });
    _choiceModal.querySelector('.dc4-cancel').onclick=function(){_choiceModal.classList.remove('open');};

    // ── Browse modal ─────────────────────────────────────────────────────
    var _browseModal=null;
    function openBrowser(ds,label){
      if(!_browseModal){
        _browseModal=document.createElement('div');
        _browseModal.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9998;display:flex;align-items:center;justify-content:center';
        _browseModal.innerHTML=
          '<div style="width:min(700px,92vw);max-height:80vh;display:flex;flex-direction:column;background:var(--bg1);border:1px solid var(--border);border-radius:5px;box-shadow:0 8px 40px rgba(0,0,0,.6);overflow:hidden">'+
          '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border)">'+
            '<span class="dc-m-title" style="flex:1;font-size:11px;font-weight:600;color:var(--text)"></span>'+
            '<input class="dc-m-search" placeholder="Search…" style="width:140px;font-size:9px;padding:3px 6px;background:var(--bg0);border:1px solid var(--border);color:var(--text);border-radius:3px">'+
            '<button style="background:none;border:none;color:var(--dim);font-size:16px;cursor:pointer" class="dc-m-close">×</button>'+
          '</div><div class="dc-m-body" style="padding:10px 12px;overflow-y:auto;flex:1;font-size:10px"></div></div>';
        (document.body||document.documentElement).appendChild(_browseModal);
        _browseModal.onclick=function(ev){if(ev.target===_browseModal)_browseModal.style.display='none';};
        _browseModal.querySelector('.dc-m-close').onclick=function(){_browseModal.style.display='none';};
        var si=_browseModal.querySelector('.dc-m-search');si.onkeydown=function(ev){if(ev.key==='Enter')loadBrowse(_browseModal._ds,si.value);};
      }
      _browseModal._ds=ds;_browseModal.querySelector('.dc-m-title').textContent=label||ds;
      _browseModal.querySelector('.dc-m-search').value='';_browseModal.style.display='flex';loadBrowse(ds,'');
    }
    async function loadBrowse(ds,search){
      var body=_browseModal&&_browseModal.querySelector('.dc-m-body');if(!body)return;
      body.innerHTML='<span style="color:var(--dim)">Loading…</span>';
      var res=await api('/fabric/browse','POST',{dataset_id:ds,limit:100,offset:0,search:search||''},30000);
      if(!res||res.error||!res.records||!res.records.length){body.innerHTML='<span style="color:var(--dim)">'+((res&&res.error)?esc(res.error):'No records.')+'</span>';return;}
      var cols=[],seen2={};res.records.forEach(function(r){var d=r.data||r;if(typeof d==='string'){try{d=JSON.parse(d);}catch(e){d={};}}Object.keys(d).forEach(function(k){if(!seen2[k]&&k!=='text'&&!k.startsWith('_')){seen2[k]=1;cols.push(k);}});});cols=cols.slice(0,8);
      var html='<div style="font-size:9px;color:var(--dim);margin-bottom:4px">'+(res.total||res.records.length)+' records</div>';
      html+='<table style="width:100%;border-collapse:collapse;font-size:9px"><thead><tr>'+cols.map(function(c){return '<th style="text-align:left;padding:2px 4px;border-bottom:1px solid var(--border);color:var(--dim2)">'+esc(c)+'</th>';}).join('')+'</tr></thead><tbody>';
      res.records.forEach(function(r){var d=r.data||r;if(typeof d==='string'){try{d=JSON.parse(d);}catch(e){d={};}}
        html+='<tr>'+cols.map(function(c){var v=d[c];if(v&&typeof v==='object')v=JSON.stringify(v);var s=String(v==null?'':v);var isU=/^https?:\/\//.test(s);
          return '<td style="padding:2px 4px;border-bottom:1px solid var(--border);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(isU?'<a href="'+esc(s)+'" target="_blank" style="color:var(--acc)">'+esc(s.slice(0,60))+'</a>':esc(s.slice(0,160)))+'</td>';}).join('')+'</tr>';});
      html+='</tbody></table>';body.innerHTML=html;
    }

    // ── Helpers ──────────────────────────────────────────────────────────
    function log(msg,type){
      var c={ok:'var(--ok)',err:'var(--err)',warn:'var(--acc3)',acc:'var(--acc)',info:'var(--acc2)'}[type]||'var(--dim)';
      var t=new Date().toLocaleTimeString();
      logLines.push('<span style="color:'+c+'">['+t+'] '+esc(msg)+'</span>');
      if(logLines.length>200)logLines=logLines.slice(-120);
      var el=q('.dc-log');if(el){el.innerHTML=logLines.join('<br>');el.scrollTop=el.scrollHeight;}
      // Also push to bottom drawer terminal if available
      try{if(graph&&graph.bottomDrawer&&graph.bottomDrawer.log)graph.bottomDrawer.log(msg,type);}catch(e){}
    }
    function setStatus(sel,msg,type){var el=q(sel);if(!el)return;el.textContent=msg;el.className='status'+(type?' '+type:'');}
    function overlay(line,counts,state){
      var ov=q('.dc-ov');if(!ov)return;ov.style.display='flex';
      if(line!=null){var l=q('.dc-ov-line');if(l)l.textContent=line;}
      if(counts!=null){var c=q('.dc-ov-counts');if(c)c.textContent=counts;}
      var d=q('.dc-ov-dot');if(d&&state)d.className='dot '+state;
    }

    // ── Palette ─────────────────────────────────────────────────────────
    try{var C=window.veraUI.Graph.colors||{};
      C.Page=C.Page||'#6b9bd2';C.Surface=C.Surface||'#c9955a';C.Subtable=C.Subtable||'#5ec9a0';
      C.Search=C.Search||'#facc15';C.person=C.person||'#e8a87c';C.organisation=C.organisation||'#c98f5a';
      C.technology=C.technology||'#5a9e8f';C.product=C.product||'#7fb37f';C.location=C.location||'#9ec96b';
      C.domain=C.domain||'#8f9ed9';C.event=C.event||'#c9b15a';
    }catch(e){}

    // ── Graph helpers (unchanged) ────────────────────────────────────────
    function nodeRadius(n){var p=(n&&n.props)||{};if(n.type==='Dataset'||p.root)return 15;return({Entity:1}[n.type]||!{Dataset:1,Page:1,Surface:1,Subtable:1,Search:1}[n.type])?8:10;}
    function remember(payload){
      (payload.nodes||[]).forEach(function(n){var t=n.type||'Page';if(t==='Entity'&&n.props&&n.props.type)t=n.props.type;
        if(nodeById[n.id]){var ex=nodeById[n.id];if(n.label)ex.label=n.label;if(t)ex.type=t;if(n.props)Object.assign(ex.props,n.props);}
        else{nodeById[n.id]={id:n.id,label:n.label||n.id,type:t,props:n.props||{}};lastGraph.nodes.push(nodeById[n.id]);}
      });
      (payload.edges||[]).forEach(function(e){var k=e.from+'|'+e.to+'|'+e.rel;
        if(!seenEdges['L'+k]){seenEdges['L'+k]=true;lastGraph.edges.push({from:e.from,to:e.to,rel:e.rel||'LINKS_TO'});
          degree[e.from]=(degree[e.from]||0)+1;degree[e.to]=(degree[e.to]||0)+1;}
      });
    }
    function applyGraph(payload,incremental){
      if(!graph||!payload)return;
      if(!incremental){graph.clear();seenNodes={};seenEdges={};lastGraph={nodes:[],edges:[]};nodeById={};degree={};}
      remember(payload);
      (payload.nodes||[]).forEach(function(n){var spec=nodeById[n.id]||n;
        if(seenNodes[n.id]){var ex=graph.state&&graph.state.nodeIndex&&graph.state.nodeIndex[n.id];
          if(ex){if(spec.label)ex.label=spec.label;if(spec.props)Object.assign(ex.props,spec.props);if(spec.type)ex.type=spec.type;ex.r=nodeRadius(spec);}return;}
        seenNodes[n.id]=true;
        var added=graph.addNode({id:spec.id,label:spec.label||spec.id,type:spec.type||'Page',props:spec.props||{},r:nodeRadius(spec)});
        if(added&&incremental&&graph.pulseNode)graph.pulseNode(n.id);
      });
      (payload.edges||[]).forEach(function(e){var k=e.from+'|'+e.to+'|'+e.rel;if(seenEdges[k])return;seenEdges[k]=true;
        graph.addEdge({from:e.from,to:e.to,rel:e.rel||'LINKS_TO'});});
      try{if(graph.rebuildChips)graph.rebuildChips();}catch(e){}
      try{if(graph.draw)graph.draw();}catch(e){}
    }

    // ── Polling ─────────────────────────────────────────────────────────
    async function pollOnce(){
      if(!active.crawlId)return;
      var g=await api('/fabric/discover/graph?crawl_id='+encodeURIComponent(active.crawlId)+(active.datasetId?'&dataset_id='+encodeURIComponent(active.datasetId):''));
      if(g&&!g.error){if(g.dataset_id){active.datasetId=g.dataset_id;_saveActive();}applyGraph(g,true);
        var st=g.stats||{};overlay(null,(st.pages||0)+' pages · '+(st.surfaces||0)+' surfaces · '+(st.entities||0)+' entities',active.running?'running':'done');}
      updateCurStrip();
    }
    function startPolling(){stopPolling();pollTimer=setInterval(function(){pollOnce();refreshSideLists();},2600);}
    function stopPolling(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}

    function updateCurStrip(){
      var bar=q('.dc-cur');if(!bar)return;
      if(!active.crawlId){bar.style.display='none';return;}
      var row=(lastCrawls||[]).find(function(c){return c.crawl_id===active.crawlId;})||{};
      var lbl=row.topic||row.seed_url||active.datasetId||active.crawlId;
      var lab=q('.dc-cur-lbl');if(lab)lab.textContent=(active.running?'Crawling: ':'Loaded: ')+lbl;
      var dot=q('.dc-cur-dot');if(dot)dot.className='dot '+(active.running?'running':'done');
      bar.style.display='flex';
    }
    q('.dc-cur').onclick=function(){returnToCurrent();};
    async function returnToCurrent(){
      if(!active.crawlId)return;
      var g=await api('/fabric/discover/graph?crawl_id='+encodeURIComponent(active.crawlId)+(active.datasetId?'&dataset_id='+encodeURIComponent(active.datasetId):''));
      if(g&&!g.error){applyGraph(g,false);var st=g.stats||{};overlay('Current',(st.pages||0)+' pages · '+(st.surfaces||0)+' surfaces',active.running?'running':'done');}
      refreshSideLists();
    }

    // ── Run a crawl ──────────────────────────────────────────────────────
    function newCrawlId(){return 'disc_'+Date.now().toString(36)+Math.random().toString(36).slice(2,8);}
    async function runCrawl(reqPromise,crawlId,label,overwrite,statusSel){
      active.crawlId=crawlId;active.datasetId='';active.running=true;_saveActive();
      if(overwrite){graph.clear();seenNodes={};seenEdges={};lastGraph={nodes:[],edges:[]};nodeById={};degree={};}
      updateCurStrip();overlay(label||'Crawling…','','running');
      log((label||('Starting '+crawlId))+(overwrite?' [overwrite]':''),'ok');
      if(statusSel)setStatus(statusSel,'Running…');
      setTimeout(pollOnce,700);startPolling();
      var res=await reqPromise;
      if(res&&res.error&&/timeout/i.test(res.error)){log('HTTP timed out — continuing server-side','warn');}
      else{active.running=false;stopPolling();await pollOnce();}
      if(res&&res.dataset_id)active.datasetId=res.dataset_id;
      var pages=res&&(res.pages_fetched||res.pages_acquired||0);
      var msg='Done: '+(pages||'?')+' pages'+(res&&res.surfaces_found?' · '+res.surfaces_found+' surfaces':'')+
          (res&&res.entities_found?' · '+res.entities_found+' entities':'');
      log(msg,'ok');
      if(statusSel)setStatus(statusSel,msg,'ok');
      overlay('Done',(pages||0)+' pages',active.running?'running':'done');
      updateCurStrip();loadHistory(false);refreshSideLists();loadModels();
      return res;
    }

    // ── Collect search sites ─────────────────────────────────────────────
    function getSearchSites(){
      if(chk('.dc-site-all',false))return 'all';
      var groups=['reddit','x','youtube','news','github','stackoverflow','hackernews','blogs','wikipedia','academic','forums','podcasts','mastodon'];
      var selected=groups.filter(function(g){return chk('.dc-site-'+g,false);});
      if(chk('.dc-site-vera',true))selected.push('vera');
      return selected.length?selected.join(','):'all';
    }

    // ── Topic discovery ──────────────────────────────────────────────────
    async function discoverTopic(modeOverride,cont){
      var topic=(q('.dc-topic')||{}).value.trim();if(!topic){log('Enter a topic','err');return;}
      if(!modeOverride&&!cont){
        var existing=(lastCrawls||[]).filter(function(c){return c.topic&&c.topic.toLowerCase()===topic.toLowerCase();});
        if(existing.length){
          _choiceMode='expand';
          _choiceModal.querySelectorAll('.dc4-opt').forEach(function(o){o.classList.toggle('sel',o.getAttribute('data-mode')==='expand');});
          _choiceModal.querySelector('.dc4-etopic').textContent=topic;
          _choiceModal.classList.add('open');
          _choiceModal.querySelector('.dc4-confirm').onclick=function(){
            _choiceModal.classList.remove('open');discoverTopic(_choiceMode,_choiceMode==='expand'||_choiceMode==='enhance');};
          return;
        }
      }
      var cid=newCrawlId();
      var overwrite=(modeOverride==='overwrite');
      var payload={
        topic:topic,crawl_id:cid,
        seed_urls:(q('.dc-seeds')||{}).value.trim()||'',
        max_sources:intv('.dc-max',5,1,15),
        content_type:(q('.dc-type')||{}).value||'all',
        max_pages:intv('.dc-pages',120,1,2000),
        dropoff:intv('.dc-drop',3,0,20),
        max_concurrency:intv('.dc-conc',4,1,16),
        entity_workers:intv('.dc-workers',3,1,8),
        search_angles:intv('.dc-angles',6,1,50),
        expansion_rounds:intv('.dc-rounds',2,0,30),
        min_relevance:intv('.dc-minrel',6,0,90)/100,
        synth_neighbor_depth:intv('.dc-ndepth',1,0,3),
        page_text_cap:intv('.dc-textcap',0,0,200000),
        max_record_chars:intv('.dc-reccap',0,0,200000),
        auto_synthesize:chk('.dc-autosyn',false),
        synth_infer_edges:chk('.dc-inferedges',true),
        consolidate_entities:chk('.dc-consol',true),
        topic_drift:chk('.dc-drift',true),
        llm_drift_gate:chk('.dc-llmdrift',false),
        swallow_domains:chk('.dc-swallow',true),
        no_max_depth:chk('.dc-nodepth',false),
        topic_brief:chk('.dc-brief',false),
        auto_promote:chk('.dc-promote',false),
        same_domain_only:chk('.dc-same',false),
        extract_entities:!chk('.dc-noent',false),
        loom:!chk('.dc-noloom',false),
        loom_cross:!chk('.dc-nocross',false),
        llm_query_expansion:!chk('.dc-nollm',false),
        llm_tagging:chk('.dc-llmtag',true),
        use_llm_entities:chk('.dc-llment',true),
        map_depth:(q('.dc-mapdepth')||{}).value||'standard',
        llm_steering:chk('.dc-steer',false),
        focus:(q('.dc-mapfocus')||{}).value.trim()||'',
        search_sites:getSearchSites(),
        include_vera_sources:chk('.dc-site-vera',true),
        negative_keywords:(q('.dc-neg')||{}).value.trim()||'',
        exclude_urls:(q('.dc-negurl')||{}).value.trim()||'',
        required_keyword:(q('.dc-reqword')||{}).value.trim()||'',
        required_keyword_mode:(q('.dc-reqmode')||{}).value||'fuzzy',
        dataset_id:(q('.dc-ds')||{}).value.trim()||'',
        overwrite:overwrite,
        continue_existing:cont||false,
        mode:modeOverride||'new',
        detect_surfaces:true,extract_subtables:true,
      };
      var overwriteGraph = overwrite;
      if (cont && active.crawlId && !overwrite) {
        // Continue: resume the saved frontier without re-searching
        var contPayload = {
          crawl_id: active.crawlId,
          additional_pages: intv('.dc-pages',60,1,2000),
          page_text_cap: intv('.dc-textcap',0,0,200000),
          max_record_chars: intv('.dc-reccap',0,0,200000),
        };
        await runCrawl(api('/fabric/discover/continue','POST',contPayload,600000),
          active.crawlId, 'Continuing "'+topic+'"', false, '.dc-topic-status');
        return;
      }
      // Expand (cont=true, no active crawl) or fresh: merge into existing dataset
      if (cont && active.datasetId) payload.dataset_id = active.datasetId;
      await runCrawl(api('/fabric/discover/topic','POST',payload,600000),cid,
        (overwrite?'Overwriting':cont?'Expanding':'Discovering')+' "'+topic+'"',overwriteGraph,'.dc-topic-status');
    }

    // ── 3-way action modal ────────────────────────────────────────────────
    // Shown when "Action" is clicked — lets user pick: Continue / Expand / Overwrite / Fresh
    var _actionModal = document.createElement('div');
    _actionModal.className = 'dc4-modal';
    _actionModal.innerHTML =
      '<div class="dc4-mbox" style="width:min(440px,94vw)">'+
        '<div class="dc4-mhd">What would you like to do?</div>'+
        '<div class="dc4-mbody">'+
          '<div style="font-size:10px;color:var(--dim2);margin-bottom:6px">Topic: <strong class="dc4a-topic" style="color:var(--acc)"></strong></div>'+
          '<div class="dc4a-prev" style="font-size:8.5px;color:var(--dim2);margin-bottom:8px;padding:4px 7px;background:var(--bg0);border:1px solid var(--border);border-left:2px solid var(--acc3);border-radius:3px"></div>'+
          '<div class="dc4-opt sel" data-amode="continue">'+
            '<span class="dc4-opt-ic">↻</span><div><strong>Continue</strong>'+
            '<span>Resume crawl from its saved frontier — add more pages using the same discovery approach.</span></div></div>'+
          '<div class="dc4-opt" data-amode="expand">'+
            '<span class="dc4-opt-ic">⊕</span><div><strong>Expand</strong>'+
            '<span>Add new pages from fresh search angles, skipping already-visited URLs. Good for broadening coverage.</span></div></div>'+
          '<div class="dc4-opt" data-amode="overwrite">'+
            '<span class="dc4-opt-ic">↺</span><div><strong>Overwrite</strong>'+
            '<span>Clear the existing dataset and start a completely fresh crawl. Previous data will be lost.</span></div></div>'+
          '<div class="dc4-opt" data-amode="fresh">'+
            '<span class="dc4-opt-ic">✦</span><div><strong>New run</strong>'+
            '<span>Keep existing data untouched and start a new parallel run under a different crawl ID.</span></div></div>'+
        '</div>'+
        '<div class="dc4-mft">'+
          '<button class="btn warn dc4a-cancel" style="padding:3px 12px">Cancel</button>'+
          '<button class="btn dc4a-confirm" style="padding:3px 16px">Go</button>'+
        '</div>'+
      '</div>';
    (document.body||document.documentElement).appendChild(_actionModal);
    var _actionMode = 'continue';
    _actionModal.querySelectorAll('.dc4-opt').forEach(function(o){
      o.onclick=function(){
        _actionModal.querySelectorAll('.dc4-opt').forEach(function(x){x.classList.remove('sel');});
        o.classList.add('sel');_actionMode=o.getAttribute('data-amode');
      };
    });
    _actionModal.querySelector('.dc4a-cancel').onclick=function(){_actionModal.classList.remove('open');};
    _actionModal.querySelector('.dc4a-confirm').onclick=function(){
      _actionModal.classList.remove('open');
      if(_actionMode==='continue') discoverTopic('expand',true);  // continue = resume frontier
      else if(_actionMode==='expand') discoverTopic('expand',true);
      else if(_actionMode==='overwrite') discoverTopic('overwrite',false);
      else discoverTopic(null,false);  // fresh = brand new crawl
    };

    function openActionModal(){
      // Try to get topic from: active crawl > history > text field
      var topic=(q('.dc-topic')||{}).value.trim();
      if(!topic&&active.crawlId){
        var row=(lastCrawls||[]).find(function(c){return c.crawl_id===active.crawlId;});
        if(row){topic=row.topic||row.seed_url||'';}
      }
      if(!topic){log('Enter a topic first, or load a crawl from History','err');return;}
      // Populate topic field if empty
      var tf=q('.dc-topic');if(tf&&!tf.value.trim())tf.value=topic;
      // Show prev run info
      var prevDiv=_actionModal.querySelector('.dc4a-prev');
      var row=(lastCrawls||[]).find(function(c){return c.topic&&c.topic.toLowerCase()===topic.toLowerCase();});
      if(row){
        prevDiv.style.display='';
        var ts=row.started_at||row.created_at||'';
        prevDiv.innerHTML='Last run: <span style="color:var(--text)">'+(ts?ts.slice(0,16).replace('T',' '):'?')+'</span>'+
          (row.pages_fetched?' &bull; '+row.pages_fetched+'p':'')+(row.status?' &bull; '+row.status:'');
      } else {prevDiv.style.display='none';}
      _actionModal.querySelector('.dc4a-topic').textContent=topic;
      // Reset selection
      _actionModal.querySelectorAll('.dc4-opt').forEach(function(x){x.classList.remove('sel');});
      _actionModal.querySelector('[data-amode="continue"]').classList.add('sel');
      _actionMode='continue';
      _actionModal.classList.add('open');
    }

    async function continueTopic(){
      // Legacy path — just open the action modal
      openActionModal();
    }


    async function mapTopic(){
      var topic=(q('.dc-topic')||{}).value.trim();if(!topic){log('Enter a topic','err');return;}
      var cid=newCrawlId();
      var payload={
        topic:topic,crawl_id:cid,
        dataset_id:(q('.dc-mapds')||{}).value.trim()||'',
        focus:(q('.dc-mapfocus')||{}).value.trim()||'',
        llm_steering:chk('.dc-steer',false),
        depth:(q('.dc-mapdepth')||{}).value||'standard',
        synth_neighbor_depth:intv('.dc-ndepth',1,0,3),
        auto_synthesize:chk('.dc-autosyn',false),
        synth_infer_edges:chk('.dc-inferedges',true),
        consolidate_entities:chk('.dc-consol',true),
        entity_workers:intv('.dc-workers',3,1,8),
      };
      await runCrawl(api('/fabric/discover/map_topic','POST',payload,600000),cid,
        'Mapping "'+topic+'"',true,'.dc-topic-status');
    }

    // ── Crawl URL ────────────────────────────────────────────────────────
    async function crawlUrl(cont){
      var url=(q('.dc-curl')||{}).value.trim();if(!url){log('Enter a URL','err');return;}
      var cid=newCrawlId();
      var payload={
        url:url,crawl_id:cid,
        dataset_id:(q('.dc-curlds')||{}).value.trim()||'',
        topic:(q('.dc-curltopic')||{}).value.trim()||'',
        max_pages:intv('.dc-curlpages',60,1,2000),
        max_depth:intv('.dc-curldepth',4,1,20),
        max_concurrency:intv('.dc-curlconc',4,1,16),
        entity_workers:intv('.dc-curlworkers',3,1,8),
        auto_synthesize:chk('.dc-curl-autosyn',false),
        synth_neighbor_depth:intv('.dc-curl-ndepth',1,0,3),
        synth_infer_edges:chk('.dc-curl-inferedges',true),
        consolidate_entities:chk('.dc-curl-consol',true),
        topic_drift:chk('.dc-curl-drift',true),
        llm_drift_gate:chk('.dc-curl-llmdrift',false),
        swallow_domains:chk('.dc-curl-swallow',true),
        no_max_depth:chk('.dc-curl-nodepth',false),
        topic_brief:chk('.dc-curl-brief',false),
        detect_surfaces:chk('.dc-curl-surf',true),
        extract_subtables:chk('.dc-curl-sub',true),
        same_domain_only:chk('.dc-curl-same',true),
        auto_promote:chk('.dc-curl-promote',false),
        extract_entities:!chk('.dc-curl-noent',false),
        negative_keywords:(q('.dc-curl-neg')||{}).value.trim()||'',
        exclude_urls:(q('.dc-curl-negurl')||{}).value.trim()||'',
        required_keyword:(q('.dc-curl-reqword')||{}).value.trim()||'',
        required_keyword_mode:(q('.dc-curl-reqmode')||{}).value||'fuzzy',
        page_text_cap:intv('.dc-textcap',0,0,200000),
        max_record_chars:intv('.dc-reccap',0,0,200000),
        continue_existing:cont||false,
      };
      if (cont && active.crawlId) {
        var contP = { crawl_id: active.crawlId,
          additional_pages: intv('.dc-curlpages',60,1,2000),
          page_text_cap: intv('.dc-textcap',0,0,200000),
          max_record_chars: intv('.dc-reccap',0,0,200000),
        };
        await runCrawl(api('/fabric/discover/continue','POST',contP,300000),
          active.crawlId,'Continuing: '+url,false,'.dc-curl-status');
        return;
      }
      await runCrawl(api('/fabric/discover/crawl','POST',payload,300000),cid,
        'Crawling: '+url,false,'.dc-curl-status');
    }

    // ── History ──────────────────────────────────────────────────────────
    async function loadHistory(silent){
      var res=await api('/fabric/discover/history');
      lastCrawls=(res&&res.crawls)||[];
      var el=q('.dc-hlist');var ct=q('.dc-hct');
      if(ct)ct.textContent=lastCrawls.length||'';
      if(!el)return;
      if(!lastCrawls.length){el.innerHTML='<div style="padding:6px 8px;font-size:9px;color:var(--dim)">No history.</div>';return;}
      // Group by topic
      var groups={},order=[];
      lastCrawls.slice(0,100).forEach(function(c){
        var key=c.topic||c.seed_url||c.crawl_id;
        if(!groups[key]){groups[key]=[];order.push(key);}
        groups[key].push(c);
      });
      var html='';
      order.forEach(function(key){
        var runs=groups[key];var latest=runs[0];
        var isActive=runs.some(function(r){return r.crawl_id===active.crawlId;});
        html+='<div class="tgrp'+(isActive?' open':'')+'" data-key="'+esc(key)+'">'+
          '<div class="tgrp-hd" onclick="this.parentElement.classList.toggle(\'open\')">'+
            '<span class="dot '+(latest.status||'done')+'"></span>'+
            '<span class="tlbl">'+esc(key)+'</span>'+
            '<span style="font-size:8px;color:var(--dim);flex-shrink:0">'+runs.length+'×</span>'+
          '</div><div class="tgrp-runs">';
        runs.forEach(function(c){
          var ts=(c.started_at||c.created_at||'').slice(0,16).replace('T',' ');
          html+='<div class="run-item'+(c.crawl_id===active.crawlId?' sel':'')+'" data-cid="'+esc(c.crawl_id)+'" data-ds="'+esc(c.dataset_id||'')+'">'+
            '<span class="dot '+(c.status||'done')+'"></span>'+
            '<span style="flex:1;color:var(--dim2)">'+esc(ts||c.crawl_id.slice(-8))+'</span>'+
            '<span style="color:var(--dim)">'+(c.pages_fetched||0)+'p</span>'+
            '<button class="btn ri-del" data-cid="'+esc(c.crawl_id)+'" data-ds="'+esc(c.dataset_id||'')+'" '+
              'style="font-size:7.5px;padding:1px 5px;background:transparent;border:1px solid var(--border);color:var(--dim);margin-left:3px" '+
              'title="Delete this scan (and optionally its dataset)">\u2715</button>'+
          '</div>';
        });
        html+='</div></div>';
      });
      el.innerHTML=html;
      el.querySelectorAll('.run-item').forEach(function(r){
        r.onclick=function(ev){
          if(ev.target.classList.contains('ri-del'))return;
          ev.stopPropagation();selectCrawl(r.getAttribute('data-cid'),r.getAttribute('data-ds'));
        };
      });
      el.querySelectorAll('.ri-del').forEach(function(btn){
        btn.onclick=function(ev){
          ev.stopPropagation();
          var cid=btn.getAttribute('data-cid'),ds=btn.getAttribute('data-ds')||'';
          var msg='Delete scan '+cid+'?'+(ds?'\n\nAlso delete the full dataset (all pages/entities)?\n[OK = delete dataset too, Cancel = scan only]':'');
          var delDs = ds && confirm(msg);
          if (!confirm(delDs?'Confirm: delete scan + full dataset '+ds+'?':'Confirm: delete scan '+cid+' only?')) return;
          api('/fabric/discover/delete_scan','POST',{crawl_id:cid,delete_dataset:!!delDs},30000)
            .then(function(r){
              if(r&&r.ok){
                log('\u2715 deleted: '+cid+(delDs?' + dataset':''),'warn');
                if(active.crawlId===cid){active.crawlId='';active.datasetId='';_saveActive();graph.clear();seenNodes={};seenEdges={};lastGraph={nodes:[],edges:[]};nodeById={};degree={};}
                loadHistory(false);
              } else log('Delete failed: '+((r&&r.error)||'?'),'err');
            });
        };
      });
      if(!silent)log('History: '+lastCrawls.length+' runs','info');
    }
    async function selectCrawl(cid,ds){
      active.crawlId=cid;active.datasetId=ds||'';active.running=false;_saveActive();
      // Populate topic field from history entry so Action modal knows what topic is loaded
      var row=(lastCrawls||[]).find(function(c){return c.crawl_id===cid;});
      if(row&&row.topic){var tf=q('.dc-topic');if(tf&&!tf.value.trim())tf.value=row.topic;}
      log('Loading map for '+cid,'info');overlay('Loading…','','done');
      var g=await api('/fabric/discover/graph?crawl_id='+encodeURIComponent(cid)+(ds?'&dataset_id='+encodeURIComponent(ds):''));
      if(g&&!g.error){applyGraph(g,false);var st=g.stats||{};overlay('Loaded',(st.pages||0)+' pages · '+(st.surfaces||0)+' surfaces','done');
        log('Map: '+(st.pages||0)+' pages, '+(st.surfaces||0)+' surfaces','ok');}
      else log('Failed: '+((g&&g.error)||'?'),'err');
      refreshSideLists();updateCurStrip();loadHistory(true);
    }
    async function clearHistory(){
      if(!confirm('Clear all discovery history?'))return;
      var res=await api('/fabric/discover/clear_history','POST',{});
      if(res&&!res.error){lastCrawls=[];loadHistory(true);setStatus('.dc-hist-status','Cleared','ok');}
      else setStatus('.dc-hist-status','Error: '+((res&&res.error)||'?'),'err');
    }

    // ── Surfaces + Subtables ─────────────────────────────────────────────
    async function refreshSideLists(){
      var ds=active.datasetId;if(!ds)return;
      var sr=await api('/fabric/surfaces?parent_dataset='+encodeURIComponent(ds)+'&limit=60');
      renderSurfaces((sr&&sr.surfaces)||[]);
      var tr=await api('/fabric/subtables?parent_dataset='+encodeURIComponent(ds)+'&limit=60');
      renderSubtables((tr&&tr.subtables)||[]);
    }
    function renderSurfaces(rows){
      var el=q('.dc-slist');var ct=q('.dc-sct');if(ct)ct.textContent=rows.length||'';if(!el)return;
      if(!rows.length){el.innerHTML='<div style="padding:6px 8px;font-size:9px;color:var(--dim)">No surfaces.</div>';return;}
      el.innerHTML=rows.map(function(s){
        var canP=s.source_type&&!s.promoted&&s.kind!=='db';
        return '<div class="item" data-sid="'+esc(s.id)+'">'+
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(s.label||s.url)+'</span>'+
          '<span style="font-size:7.5px;color:var(--dim)">'+esc(s.kind||'')+'</span>'+
          (s.promoted?'<span class="pill" style="border-color:var(--ok);color:var(--ok)">src</span>':
           canP?'<button class="btn" style="padding:1px 5px;font-size:7.5px" data-promote="'+esc(s.id)+'">promote</button>':'')+
          '</div>';
      }).join('');
      el.querySelectorAll('[data-promote]').forEach(function(b){b.onclick=function(ev){ev.stopPropagation();promote(b.getAttribute('data-promote'),b);};});
      el.querySelectorAll('.item[data-sid]').forEach(function(r){r.onclick=function(){if(graph.focusNode)graph.focusNode(r.getAttribute('data-sid'));};});
    }
    async function promote(sid,btn){
      var res=await api('/fabric/surfaces/promote','POST',{surface_id:sid});
      if(res&&!res.error){log('Promoted '+sid,'ok');if(btn)btn.textContent='✓';refreshSideLists();}
      else log('Promote failed: '+((res&&res.error)||'?'),'err');
    }
    function renderSubtables(rows){
      var el=q('.dc-tlist');var ct=q('.dc-tct');if(ct)ct.textContent=rows.length||'';if(!el)return;
      if(!rows.length){el.innerHTML='<div style="padding:6px 8px;font-size:9px;color:var(--dim)">No sub-tables.</div>';return;}
      el.innerHTML=rows.map(function(t){
        return '<div class="item" data-sub="'+esc(t.sub_dataset||'')+'" data-lbl="'+esc(t.title||t.kind)+'">'+
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(t.title||t.kind)+'</span>'+
          '<span style="font-size:7.5px;color:var(--dim)">'+(t.row_count||0)+' rows</span></div>';
      }).join('');
      el.querySelectorAll('.item[data-sub]').forEach(function(r){
        r.onclick=function(){var ds=r.getAttribute('data-sub');if(ds)openBrowser(ds,r.getAttribute('data-lbl'));};
      });
    }

    // ── 3rd-order models ─────────────────────────────────────────────────
    async function loadModels(){
      var el=q('.dc-mlist');var ct=q('.dc-mct');var st=q('.dc-models-status');
      if(!el)return;
      var res=await api('/fabric/synthesize/list');
      var models=(res&&res.models)||[];
      if(ct)ct.textContent=models.length||'';
      if(!models.length){el.innerHTML='<div style="font-size:9px;color:var(--dim)">No models yet. Use Auto-synthesize or Map topic.</div>';return;}
      el.innerHTML=models.map(function(m){
        var ds=m.dataset_id||m.source_dataset||'';
        return '<div class="model-item" data-mid="'+esc(m.model_id||m.id)+'">'+
          '<div class="mt">'+esc(m.topic||m.model_id||'?')+'</div>'+
          '<div class="ms">'+esc(ds)+(m.entry_count?' · '+m.entry_count+' entries':'')+
            (m.created_at?' · '+String(m.created_at).slice(0,16).replace('T',' '):'')+'</div>'+
        '</div>';
      }).join('');
      el.querySelectorAll('.model-item').forEach(function(r){
        r.onclick=function(){loadModelDetail(r.getAttribute('data-mid'));};
      });
      if(st)st.textContent='';
    }
    async function loadModelDetail(mid){
      if(!mid)return;
      var res=await api('/fabric/synthesize/get?model_id='+encodeURIComponent(mid));
      if(!res||res.error){log('Model load failed: '+((res&&res.error)||'?'),'err');return;}
      // Build a mini-graph of the model
      graph.clear();seenNodes={};seenEdges={};lastGraph={nodes:[],edges:[]};nodeById={};degree={};
      var nodes=[];var edges=[];
      (res.entries||[]).forEach(function(e){
        nodes.push({id:'concept:'+e.id,label:e.concept||e.term||e.id,type:'Concept',props:e});
      });
      (res.relations||[]).forEach(function(r){
        edges.push({from:'concept:'+r.from_id,to:'concept:'+r.to_id,rel:r.relation||'RELATES_TO'});
      });
      applyGraph({nodes:nodes,edges:edges},false);
      overlay('Model: '+(res.topic||mid),nodes.length+' concepts · '+edges.length+' relations','done');
      log('Model "'+esc(res.topic||mid)+'": '+nodes.length+' concepts, '+edges.length+' relations','ok');
    }

    // ── Live events — dual subscription: parent postMessage + direct WS bus ─
    // Path 1: parent harness relays events as postMessages (iframe embed)
    // Path 2: veraUI.Graph.eventBus() opens its own WS — works standalone too
    function _handleDiscoverEvent(e) {
      try {
        var t = e.type || '';
        if(t==='fabric.discover.progress'){
          var ss=e.stage||'';
          var rel=(e.relevance!=null)?(' rel='+(+e.relevance).toFixed(2)):'';
          var dep=(e.depth!=null)?(' d='+e.depth):'';
          if(ss==='starting')           log('▶ crawl start: '+(e.url||'')+' max='+(e.max_pages||'?')+' depth='+(e.max_depth||'?')+(e.resumed?' (resumed)':''),'ok');
          else if(ss==='map_start')     log('▶ '+(e.message||'mapping topic'),'ok');
          else if(ss==='seeding')       log('↗ seeding'+(e.queries?' · '+e.queries.length+' angles':'')+(e.message?': '+e.message:''),'info');
          else if(ss==='expanding')     log('↻ concept expansion round '+(e.round||'?')+(e.concepts&&e.concepts.length?' · '+e.concepts.slice(0,4).join(', '):''),'info');
          else if(ss==='page_fetching') log('… fetch '+((e.url||'').slice(0,100))+dep,'dim');
          else if(ss==='content_extracted') log('≡ '+(e.chars||0)+' chars · '+(e.links||0)+' links — '+((e.url||'').slice(0,70)),'dim');
          else if(ss==='llm_action')    log('⚙ LLM '+(e.action||'')+((e.url)?' @ '+e.url.slice(0,60):'')+(e.message?' — '+e.message:''),'acc');
          else if(ss==='page_added')    log('+ '+((e.title||e.url||'').slice(0,80))+rel+dep+(e.usefulness!=null?' use='+(+e.usefulness).toFixed(2):'')+(e.source_type?' ['+e.source_type+']':'')+(e.entities_queued?' · '+e.entities_queued+' ents queued':''),'info');
          else if(ss==='page_skipped')  log('⊘ skip '+((e.url||'').slice(0,80))+' — '+(e.reason||'')+rel,'dim');
          else if(ss==='entity_found'||ss==='entity_extracted') log('⊙ '+(e.count||0)+' entities'+(e.backend?' ['+e.backend+']':'')+(e.names?': '+e.names.slice(0,8).join(', '):'')+((e.url)?' @ '+(e.url||'').slice(0,50):''),'info');
          else if(ss==='surface_detected') log('◆ surface ['+(e.kind||'')+'] '+((e.label||e.surface_url||'').slice(0,80))+(e.confidence?' conf='+(+e.confidence).toFixed(2):''),'ok');
          else if(ss==='subtable_added'||ss==='data_detected') log('▦ data ['+(e.kind||'')+'] → '+(e.dataset_id||e.sub_dataset||'')+(e.rows?' · '+e.rows+' rows':''),'ok');
          else if(ss==='repetition_dropoff') log('■ stopped: repetition/saturation at '+(e.pages||0)+' pages','warn');
          else if(ss==='topic_description') log('≣ topic: '+(e.description||'').slice(0,120),'ok');
          else if(ss==='progress')      log('… '+(e.pages||0)+'p · '+(e.queued||0)+' queued · '+(e.surfaces||0)+' surfaces'+(e.concurrency?' · '+e.concurrency+'×':'')+(e.entities_found?' · '+e.entities_found+' ents':''),'dim');
          else if(ss==='scanning')      log('⇉ parallel scan: in-flight='+(e.in_flight||0)+' queued='+(e.queued||0),'acc');
          else if(ss==='consolidated')  log('⧖ consolidated: merged '+(e.merged||0)+' duplicate entities','ok');
          else if(ss==='consolidating') log('⧖ consolidating entities…','acc');
          else if(ss==='queued')        log('⏳ queued behind another crawl for this topic','dim');
          else if(ss==='brief')         log('✎ research brief built'+(e.topic?': '+e.topic.slice(0,80):''),'acc');
          else if(ss==='brief_refined') log('✎ brief refined (round '+(e.round||'?')+')','acc');
          else if(ss==='git_paths')     log('⌗ git: '+(e.message||'enumerating repos for '+(e.domain||'')),'info');
          else if(ss==='domain_rich')   log('⊙ rich source: '+(e.domain||'')+(e.score?' score='+(+e.score).toFixed(2):''),'ok');
          else if(ss==='domain_dropoff')log('↘ drop-off: '+(e.domain||'')+(e.pages_from_domain?' after '+e.pages_from_domain+'p':''),'dim');
          else if(ss==='structured_area_detected') log('▦ structured area: '+(e.prefix||'')+' ('+( e.pages||0)+' pages, rel '+(e.avg_rel||'').toString().slice(0,4)+') — full-text on','ok');
          else if(ss==='parent_enqueued') log('↱ non-HTML '+(e.ext||'')+' — queuing parent: '+((e.url||'').slice(0,80)),'info');
          else if(ss==='loom')          log('⧖ loom: '+(e.message||'stitching relations'),'acc');
          else if(ss==='seeded')        log('✓ seeded: '+(e.message||''),'ok');
          else if(ss==='synthesizing')  log('◈ auto-synthesizing 3rd-order…','acc');
          else if(ss==='synthesized')   log('✓ 3rd-order model ready'+(e.entries?' · '+e.entries+' entries':''),'ok');
          else if(ss==='synthesize_error') log('⚠ synthesis failed: '+(e.error||e.message||'unknown'),'err');
          else if(ss==='required_keyword_missing') log('⊘ skip '+((e.url||'').slice(0,70))+' — '+esc(e.message||'keyword not found'),'dim');
          else if(ss==='done')          log('✓ done — '+(e.pages||0)+'p · '+(e.surfaces||0)+' surfaces · '+(e.subtables||0)+' subtables · '+(e.entities||0)+' entities','ok');
          else if(e.message)            log(e.message,'info');
          if(ss==='page_added'||ss==='surface_detected'||ss==='subtable_added'||ss==='done')pollOnce();
          if(ss==='synthesized'||ss==='done')loadModels();
        } else if(t==='fabric.entity_graph.progress'){
          var es=e.stage||'';var bk=e.backend?' ['+e.backend+']':'';
          if(es==='extracting')         log('⊙ extracting from '+(e.count||0)+' records'+bk+(e.use_llm?' + LLM':''),'info');
          else if(es==='extracted')     log('✓ extracted: '+(e.total||0)+' entities ('+(e.new_entities||0)+' new)'+bk,'ok');
          else if(es==='ner_batch')     log('⊙ NER batch '+(e.batch||0)+'/'+(e.total_batches||'?')+bk+(e.entities_found?' · '+e.entities_found+' found':''),'dim');
          else if(es==='aliased')       log('⧗ alias merge: '+(e.merged||0)+' ('+(e.before||0)+'→'+(e.after||0)+')','ok');
          else if(es==='gliner_load')   log('⚙ loading GLiNER model…','warn');
          else if(es==='gliner_ready')  log('✓ GLiNER ready'+(e.model?' · '+e.model:''),'ok');
          else if(es==='spacy_load')    log('⚙ loading spaCy…','warn');
          else if(es==='spacy_ready')   log('✓ spaCy ready'+(e.model?' · '+e.model:''),'ok');
          else if(es==='consolidated')  log('⧖ entity graph consolidated: merged '+(e.merged||0)+(e.linked?', +'+e.linked+' relations':''),'ok');
          else if(es==='done')          log('✓ entity extraction done — '+(e.entities||0)+' entities, '+(e.relations||0)+' relations (persisted '+(e.persisted||0)+')'+bk,'ok');
          else if(e.message)            log('⚙ entity: '+e.message,'acc');
          // Entity/consolidation done: refresh graph so new entity nodes appear
          if(es==='done'||es==='consolidated')setTimeout(pollOnce,800);
        } else if(t==='fabric.synthesize.progress'){
          var sv=e.stage||'';
          if(sv==='start')              log('◈ synthesising: '+(e.topic||e.message||''),'ok');
          else if(sv==='loaded')        log('◈ scope: '+(e.entities||0)+' entities, '+(e.relations||0)+' relations','info');
          else if(sv==='planning')      log('⚙ planning topic structure (LLM)…','acc');
          else if(sv==='planned')       log('◈ planned: '+(e.entry_type||'')+' · '+(e.expected||0)+' expected','ok');
          else if(sv==='synthesising')  log('⚙ distilling entries ('+(e.done||0)+'/'+(e.total||0)+')…','acc');
          else if(sv==='done')          log('✓ 3rd-order — '+(e.entries||0)+' entries, '+(e.relations||0)+' relations','ok');
          else if(e.message)            log('◈ '+e.message,'acc');
          if(sv==='done')loadModels();
        } else if(t==='fabric.loom.progress'){
          var lv=e.stage||'';
          if(lv==='done') log('✓ loom: +'+(e.internal||0)+' internal, +'+(e.cross||0)+' cross-dataset links','ok');
          else if(e.message) log('⧖ loom: '+e.message,'acc');
        } else if(t==='fabric.discover.surface'||t==='fabric.discover.subtable'){
          pollOnce();
        }
      } catch(_){}
    }

    // Path 1: harness postMessage relay (iframe parent)
    window.addEventListener('message', function(ev){
      try {
        if(!ev.data||ev.data.type!=='vera_fabric_event')return;
        var e=ev.data.event;if(!e)return;
        _handleDiscoverEvent(e);
      } catch(_){}
    });

    // Path 2: veraUI.Graph shared event bus — direct WS connection.
    // This fires even when there is no parent harness (standalone page or sidebar
    // mounted in the graph host page directly). The bus manages one WS per page.
    (function(){
      try {
        var _bus = null;
        if(window.veraUI&&window.veraUI.Graph&&window.veraUI.Graph.eventBus){
          _bus = window.veraUI.Graph.eventBus();
        } else if(papi&&papi.eventBus){
          _bus = (typeof papi.eventBus==='function') ? papi.eventBus() : papi.eventBus;
        }
        if(_bus && _bus.subscribe){
          // Subscribe to all fabric.* events — the handler filters by type itself
          _bus.subscribe('fabric.', _handleDiscoverEvent);
        }
      } catch(_busErr){}
    })();

        // ── Page content reader ───────────────────────────────────────────────
    // Shown when the user selects a Page node that has text in its props.
    var _reader = document.createElement('div');
    _reader.style.cssText = (
        'position:fixed;top:0;right:0;width:min(560px,55vw);height:100vh;' +
        'background:var(--bg1,#1f1d1a);border-left:1px solid var(--border,#3a3530);' +
        'z-index:8000;display:none;flex-direction:column;' +
        'box-shadow:-4px 0 20px rgba(0,0,0,.5)'
    );
    _reader.innerHTML =
        '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);background:var(--bg2);flex-shrink:0">' +
            '<div style="flex:1;min-width:0">' +
                '<div class="rd-title" style="font-size:11px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>' +
                '<div class="rd-meta" style="font-size:9px;color:var(--dim2);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>' +
            '</div>' +
            '<a class="rd-url" href="#" target="_blank" rel="noopener" style="font-size:9px;color:var(--acc);flex-shrink:0;white-space:nowrap">↗ Open</a>' +
            '<button class="rd-close" style="background:none;border:none;color:var(--dim);font-size:16px;cursor:pointer;padding:0 4px">×</button>' +
        '</div>' +
        // Stats bar
        '<div class="rd-stats" style="display:flex;gap:10px;padding:5px 12px;border-bottom:1px solid var(--border);font-size:9px;color:var(--dim2);flex-shrink:0;flex-wrap:wrap"></div>' +
        // Headings TOC
        '<div class="rd-toc-wrap" style="display:none;border-bottom:1px solid var(--border);background:var(--bg0);flex-shrink:0">' +
            '<div style="padding:4px 12px;font-size:8.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;cursor:pointer" class="rd-toc-toggle">▶ Contents</div>' +
            '<div class="rd-toc" style="display:none;padding:4px 12px 8px;max-height:160px;overflow-y:auto"></div>' +
        '</div>' +
        // Tags
        '<div class="rd-tags" style="display:none;padding:5px 12px;border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;gap:4px"></div>' +
        // Content
        '<div class="rd-body" style="flex:1;overflow-y:auto;padding:12px 14px;font-size:11px;line-height:1.65;color:var(--text);white-space:pre-wrap;word-break:break-word;font-family:var(--sans)"></div>';

    (document.body || document.documentElement).appendChild(_reader);

    _reader.querySelector('.rd-close').onclick = function(){ _reader.style.display = 'none'; };
    _reader.querySelector('.rd-toc-toggle').onclick = function(){
        var toc = _reader.querySelector('.rd-toc');
        var open = toc.style.display !== 'none';
        toc.style.display = open ? 'none' : 'block';
        this.textContent = (open ? '▶' : '▼') + ' Contents';
    };

    function _openReader(node) {
        var p = node.props || {};
        var text = p.text || '';
        if (!text) return;

        _reader.querySelector('.rd-title').textContent = p.title || node.label || node.id;
        var urlEl = _reader.querySelector('.rd-url');
        if (p.url) { urlEl.href = p.url; urlEl.style.display = ''; }
        else { urlEl.style.display = 'none'; }

        // Stats bar
        var stats = [];
        if (p.word_count) stats.push(p.word_count + ' words');
        if (p.relevance != null) stats.push('relevance ' + (p.relevance * 100).toFixed(0) + '%');
        if (p.depth != null) stats.push('depth ' + p.depth);
        if (p.source_type) stats.push(p.source_type);
        _reader.querySelector('.rd-stats').innerHTML = stats.map(function(s){
            return '<span>' + esc(s) + '</span>';
        }).join('');

        // TOC from headings
        var headings = p.headings || [];
        var tocWrap = _reader.querySelector('.rd-toc-wrap');
        var tocEl = _reader.querySelector('.rd-toc');
        if (headings.length > 2) {
            tocEl.innerHTML = headings.map(function(h){
                var level = typeof h === 'object' ? (h.level || 1) : 1;
                var txt = typeof h === 'object' ? (h.text || h.heading || '') : String(h);
                return '<div style="padding:1px 0 1px ' + ((level-1)*10) + 'px;font-size:9px;color:var(--dim2);cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" ' +
                    'onclick="(function(el,t){var body=el.closest(\'[style*=fixed]\').querySelector(\'.rd-body\');var idx=body.textContent.indexOf(t);if(idx>=0){var range=document.createRange();var tw=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);var node,pos=0;while(node=tw.nextNode()){if(pos+node.length>idx){var span=document.createElement(\'span\');node.parentNode.insertBefore(span,node);span.scrollIntoView({behavior:\'smooth\'});setTimeout(function(){span.parentNode&&span.parentNode.removeChild(span);},2000);break;}pos+=node.length;}};})(this,\'' + txt.replace(/'/g, "\\'").slice(0, 60) + '\')">' +
                    esc(txt.slice(0, 80)) + '</div>';
            }).join('');
            tocWrap.style.display = 'block';
        } else {
            tocWrap.style.display = 'none';
        }

        // Tags
        var tags = p.tags || [];
        var tagsEl = _reader.querySelector('.rd-tags');
        if (tags.length) {
            tagsEl.style.display = 'flex';
            tagsEl.innerHTML = tags.slice(0, 20).map(function(t){
                return '<span style="font-size:8px;padding:1px 6px;border:1px solid var(--border);border-radius:8px;color:var(--dim2)">' + esc(t) + '</span>';
            }).join('');
        } else {
            tagsEl.style.display = 'none';
        }

        // Meta line (url)
        _reader.querySelector('.rd-meta').textContent = p.url || '';

        // Render body — highlight headings if present
        var bodyEl = _reader.querySelector('.rd-body');
        if (headings.length) {
            // Mark up headings inline so they stand out
            var headingTexts = headings.map(function(h){
                return typeof h === 'object' ? (h.text || h.heading || '') : String(h);
            }).filter(Boolean);
            var html = esc(text);
            headingTexts.forEach(function(ht){
                var escaped = esc(ht);
                html = html.split(escaped).join(
                    '<strong style="display:block;color:var(--acc);margin:12px 0 4px;font-size:12px">' + escaped + '</strong>');
            });
            bodyEl.innerHTML = html;
        } else {
            bodyEl.textContent = text;
        }

        _reader.style.display = 'flex';
        _reader.querySelector('.rd-body').scrollTop = 0;
    }

    // Hook into graph node selection — show content/links in the bottom drawer
    // Setting: auto-open drawer on node click (opt-in)
    var _autoDrawer = true;  // can be toggled from the Log section

    if (graph && graph.state) {
        var _prevShowDetail = graph.showDetail ? graph.showDetail.bind(graph) : null;
        if (_prevShowDetail) {
            graph.showDetail = function(node) {
                _prevShowDetail(node);
                _handleNodeSelect(node);
            };
        }
    }

    // ── Handle __local actions dispatched by vera_graph.js ─────────────────
    window.addEventListener('vg:action', function(ev) {
        var d = ev && ev.detail; if (!d) return;
        var action = d.action_id;
        var node = d.node || {};
        var p = node.props || {};

        if (action === 'view_content') {
            // Show page text in both the reader panel and the bottom drawer
            var text = p.text || p.full_text || p.content || p.body || '';
            if (!text) {
                // Try fetching from server
                var url = p.url || node.id || '';
                if (url && /^https?:/.test(url)) {
                    api('/fabric/discover/scrape_page', 'POST', {
                        url: url,
                        dataset_id: active.datasetId || '',
                        max_links: 50
                    }, 30000).then(function(r) {
                        if (r && r.full_text) {
                            p.text = r.full_text;
                            p.title = r.title || p.title;
                            p.headings = r.headings || [];
                            p.tags = r.tags || [];
                            _openReader(node);
                            if (graph && graph.bottomDrawer)
                                graph.bottomDrawer.showContent((r.title || url) + ' — content', r.full_text);
                            log('\u25a6 Scraped ' + url + ' (' + r.word_count + ' words)', 'ok');
                        } else {
                            log('No content for ' + url, 'warn');
                        }
                    });
                } else {
                    log('No content available for this node.', 'warn');
                }
                return;
            }
            _openReader(node);
            if (graph && graph.bottomDrawer)
                graph.bottomDrawer.showContent((p.title || node.label || node.id) + ' — content', text);
        }

        if (action === 'browse' || action === 'open_record') {
            var ds = p.dataset_id || p.sub_dataset ||
                     ((node.type === 'Dataset' || node.type === 'Subtable') ? node.id : '') || '';
            if (ds) { openBrowser(ds, node.label || node.id); return; }
            log('No dataset ID for browse action.', 'warn');
        }

        if (action === 'open_url' || action === 'view_source') {
            var u = p.url || node.id || '';
            if (u && /^https?:/.test(u)) window.open(u, '_blank');
        }
    });

    // ── Handle completed server actions ─────────────────────────────────
    window.addEventListener('vg:action:done', function(ev) {
        var d = ev && ev.detail; if (!d) return;
        var action = d.action_id;
        var node = d.node || {};
        var result = d.result || {};
        var payload = result.result || result;

        if (action === 'scrape_content' && payload && payload.full_text) {
            var title = payload.title || (node && node.label) || payload.url || 'Scraped';
            if (graph && graph.bottomDrawer)
                graph.bottomDrawer.showContent(title + ' \u2014 content', payload.full_text);
            log('\u25a6 Scraped: ' + title + ' (' + (payload.word_count||0) + ' words, ' +
                (payload.headings && payload.headings.length || 0) + ' headings)', 'ok');
            // Patch the node in the graph so content is available on next click
            try {
                if (graph && graph.state && graph.state.nodeIndex && node.id) {
                    var gn = graph.state.nodeIndex[node.id];
                    if (gn) {
                        gn.props = gn.props || {};
                        gn.props.text = payload.full_text;
                        gn.props.title = payload.title || gn.props.title;
                        gn.props.headings = payload.headings || [];
                        gn.props.tags = payload.tags || gn.props.tags;
                        gn.props.word_count = payload.word_count;
                        gn.props.links = payload.links || gn.props.links;
                    }
                }
            } catch(_) {}
        }

        if (action === 'synthesize' && payload && payload.ok) {
            log('\u25c8 3rd-order synthesis done: ' + (payload.topic || (node && node.label) || '') +
                (payload.entries ? ' \u00b7 ' + payload.entries + ' entries' : ''), 'ok');
            try { loadModels(); } catch(_) {}
        }
    });
    function _handleNodeSelect(node) {
        if (!node) return;
        var p = node.props || {};
        var text = p.text || p.content || p.body || '';
        var ntype = node.type || '';

        // Dataset node: show page list in content panel + surfaces in table
        if (ntype === 'Dataset') {
            var dsId = node.id || p.id || '';
            if (dsId && graph && graph.bottomDrawer) {
                // Fetch pages for this dataset and show as page-list
                api('/fabric/discover/graph?dataset_id=' + encodeURIComponent(dsId) + '&include_entities=false', 'GET')
                    .then(function(g) {
                        if (!g || g.error) return;
                        var pages = (g.nodes || []).filter(function(n){ return n.type === 'Page'; })
                            .map(function(n){ return n.props || {}; })
                            .sort(function(a,b){ return (b.relevance||0)-(a.relevance||0); });
                        if (pages.length && graph.bottomDrawer.showContent) {
                            graph.bottomDrawer.showContent(
                                (node.label || dsId) + ' \u2014 ' + pages.length + ' pages',
                                null,
                                { pages: pages }
                            );
                        }
                        // Also show surfaces in table if any
                        var surfaces = (g.nodes||[]).filter(function(n){ return n.type==='Surface'||n.type==='Subtable'; });
                        if (surfaces.length) {
                            var cols = ['label','kind','source_type','relevance'];
                            var rows = surfaces.map(function(s){
                                var sp=s.props||{};
                                return [s.label||sp.label||s.id, sp.kind||'', sp.source_type||'', sp.relevance!=null?Math.round((sp.relevance||0)*100)+'%':''];
                            });
                            graph.bottomDrawer.showTable(cols, rows, (node.label||dsId)+' surfaces ('+surfaces.length+')');
                        }
                    });
            }
            return;
        }

        // Subtable/Surface node: show records in table panel
        if (ntype === 'Subtable' || ntype === 'Surface') {
            var subId = node.id || p.id || '';
            try {
                if (graph && graph.bottomDrawer) {
                    var records = p.records || p.rows || p.data_rows;
                    if (Array.isArray(records) && records.length) {
                        var cols2 = Object.keys(records[0] || {}).filter(function(k){ return k !== 'text' && k[0] !== '_'; }).slice(0, 12);
                        var rows2 = records.slice(0, 500).map(function(r){ return cols2.map(function(c){ return r[c] == null ? '' : String(r[c]); }); });
                        graph.bottomDrawer.showTable(cols2, rows2, (node.label || node.id) + ' (' + records.length + ' rows)');
                        return;
                    }
                    // Fetch from server if no cached records
                    if (subId) {
                        api('/fabric/surfaces/records?sub_dataset=' + encodeURIComponent(subId) + '&limit=300', 'GET')
                            .then(function(r) {
                                if (!r || r.error || !r.records) return;
                                var recs = r.records;
                                if (!recs.length) return;
                                var cols3 = Object.keys(recs[0]).filter(function(k){ return k[0]!=='_'&&k!=='text'; }).slice(0,12);
                                var rows3 = recs.map(function(rec){ return cols3.map(function(c){ return rec[c]==null?'':String(rec[c]); }); });
                                graph.bottomDrawer.showTable(cols3, rows3, (node.label||subId)+' ('+recs.length+' rows)');
                            });
                    }
                }
            } catch(e3) {}
            return;
        }

        // Links table (outbound links from page node)
        try {
            if (graph && graph.bottomDrawer && graph.bottomDrawer.showTable) {
                var links = p.links || p._links;
                if (Array.isArray(links) && links.length) {
                    var lRows = links.slice(0, 300).map(function(l){
                        return [l.url || '', l.anchor || ''];
                    });
                    graph.bottomDrawer.showTable(['url','anchor'], lRows,
                        (p.title || node.label || node.id || 'Page') + ' \u2014 links (' + links.length + ')');
                    if (text && text.length > 80) _openReader(node);
                    // Also populate content panel with full text
                    if (text && text.length > 80 && graph.bottomDrawer.showContent) {
                        graph.bottomDrawer.showContent(
                            (p.title || node.label || node.id) + ' \u2014 content', text);
                    }
                    return;
                }
            }
        } catch(e3) {}

        // Page content — show in sidebar reader + content panel
        if (text && text.length > 80) {
            _openReader(node);
            try {
                if (graph && graph.bottomDrawer && graph.bottomDrawer.showContent) {
                    graph.bottomDrawer.showContent((p.title || node.label || node.id) + ' \u2014 content', text);
                }
            } catch(e2) {}
        }
    }


    // Wire buttons
    q('.dc-topicgo').onclick=function(){discoverTopic(null,false);};
    q('.dc-topiccont').onclick=function(){openActionModal();};
    q('.dc-topicmap').onclick=function(){mapTopic();};
    q('.dc-crawlgo').onclick=function(){crawlUrl(false);};
    q('.dc-crawlcont').onclick=function(){crawlUrl(true);};
    q('.dc-hist-refresh').onclick=function(){loadHistory(false);};
    q('.dc-hist-clear').onclick=clearHistory;
    q('.dc-models-refresh').onclick=loadModels;

    var _togDrawer = q('.dc-autodrawer');
    if (_togDrawer) { _togDrawer.onchange = function(){ _autoDrawer = _togDrawer.checked; }; }

    // ── Entity extraction ────────────────────────────────────────────────
    if (q('.dc-extract-run')) {
        q('.dc-extract-run').onclick = async function() {
            var dsId = active.datasetId || '';
            if (!dsId) { setStatus('.dc-extract-st', 'No active dataset \u2014 run a crawl first', 'err'); return; }
            var maxRec = parseInt((q('.dc-extract-maxrec') && q('.dc-extract-maxrec').value) || '500', 10);
            var useLlm = !!(q('.dc-extract-llm') && q('.dc-extract-llm').checked);
            setStatus('.dc-extract-st', 'Extracting\u2026 (may take a while)', '');
            q('.dc-extract-run').disabled = true;
            log('\u29d9 Extracting entities from ' + dsId + ' (max ' + maxRec + ' recs' + (useLlm ? ', LLM' : '') + ')\u2026', 'info');
            var r = await api('/fabric/discover/entity_extract', 'POST', {
                dataset_id: dsId, max_records: maxRec, use_llm: useLlm
            }, 300000);
            q('.dc-extract-run').disabled = false;
            if (!r || r.error) {
                setStatus('.dc-extract-st', (r && r.error) || 'Failed', 'err');
                log('Entity extract failed: ' + ((r && r.error) || '?'), 'err');
            } else {
                var msg = (r.entities || 0) + ' entities, ' + (r.relations || 0) + ' relations' +
                    (r.backend ? ' [' + r.backend + ']' : '');
                setStatus('.dc-extract-st', msg, 'ok');
                log('\u2713 Entities: ' + msg, 'ok');
                var actEl = q('.dc-extract-active');
                if (actEl) actEl.textContent = r.entities || '';
                setTimeout(pollOnce, 500); // refresh graph with new entity nodes
            }
        };
    }

    // ── NER backend ──────────────────────────────────────────────────────
    async function loadNerStatus(silent) {
        var r = await api('/fabric/entity_graph/ner', 'POST', {}, 15000);
        if (!r) { if (!silent) setStatus('.dc-ner-st', 'Failed to reach NER endpoint', 'err'); return; }
        var active_be = r.active_backend || r.backend || '?';
        var actEl = q('.dc-ner-active');
        if (actEl) actEl.textContent = active_be;
        // Sync the selector to actual backend
        var sel = q('.dc-ner-backend');
        if (sel) {
            for (var i = 0; i < sel.options.length; i++) {
                if (sel.options[i].value === active_be) { sel.selectedIndex = i; break; }
            }
        }
        if (!silent) {
            var infoEl = q('.dc-ner-info');
            if (infoEl) {
                var avail = r.available || {};
                var models = r.models || {};
                var lines = [
                    'Active: ' + active_be,
                    'GLiNER: ' + (avail.gliner ? '\u2713 available (' + (models.gliner || 'default') + ')' : '\u2717 not installed'),
                    'spaCy:  ' + (avail.spacy  ? '\u2713 available (' + (models.spacy  || 'default') + ')' : '\u2717 not installed'),
                ];
                if (r.self_test && r.self_test.entities) {
                    lines.push('Self-test: ' + r.self_test.entities.length + ' entities found');
                    var sample = r.self_test.entities.slice(0, 4).map(function(e) {
                        return e.name + ' [' + e.type + ']';
                    });
                    if (sample.length) lines.push('\u00bb ' + sample.join(', '));
                }
                infoEl.innerHTML = lines.map(function(l) { return esc(l); }).join('<br>');
                infoEl.style.display = '';
            }
            setStatus('.dc-ner-st', 'Active: ' + active_be, 'ok');
        }
    }
    q('.dc-ner-apply').onclick = async function() {
        var be = (q('.dc-ner-backend') && q('.dc-ner-backend').value) || 'auto';
        var model = (q('.dc-ner-model') && q('.dc-ner-model').value.trim()) || '';
        var body = { backend: be };
        if (model) {
            if (be === 'gliner') body.gliner_model = model;
            else if (be === 'spacy') body.spacy_model = model;
        }
        setStatus('.dc-ner-st', 'Applying\u2026', '');
        q('.dc-ner-apply').disabled = true;
        var r = await api('/fabric/entity_graph/ner', 'POST', body, 30000);
        q('.dc-ner-apply').disabled = false;
        if (!r || r.error) {
            setStatus('.dc-ner-st', r && r.error || 'Failed', 'err');
        } else {
            var ab = r.active_backend || r.backend || be;
            setStatus('.dc-ner-st', 'Active: ' + ab, 'ok');
            var actEl = q('.dc-ner-active');
            if (actEl) actEl.textContent = ab;
            log('\u25cb NER backend set: ' + ab, 'ok');
            await loadNerStatus(false);
        }
    };
    q('.dc-ner-status-btn').onclick = function() { loadNerStatus(false); };
    loadNerStatus(true);  // quietly populate the selector on load

    // ── NER model install ─────────────────────────────────────────────
    q('.dc-ner-install-btn').onclick = async function() {
        var sel = q('.dc-ner-install-pkg');
        var custom = (q('.dc-ner-install-custom') && q('.dc-ner-install-custom').value.trim()) || '';
        var pkg = custom || (sel && sel.value) || '';
        var spmodel = (q('.dc-ner-install-spmodel') && q('.dc-ner-install-spmodel').value.trim()) || '';
        if (!pkg && !spmodel) { setStatus('.dc-ner-install-st', 'Specify a package or spaCy model', 'warn'); return; }
        setStatus('.dc-ner-install-st', 'Installing\u2026 (may take a minute)', '');
        q('.dc-ner-install-btn').disabled = true;
        var logEl = q('.dc-ner-install-log');
        if (logEl) { logEl.innerHTML = ''; logEl.style.display = ''; }
        log('\u25cb NER install: pkg=' + (pkg||'') + ' model=' + (spmodel||''), 'info');

        var r = await api('/fabric/entity_graph/ner_install', 'POST', {
            package: pkg, model_name: spmodel, force_reinstall: false
        }, 300000);
        q('.dc-ner-install-btn').disabled = false;

        if (!r || r.error) {
            setStatus('.dc-ner-install-st', (r && r.error) || 'Failed', 'err');
            log('NER install failed: ' + ((r && r.error) || 'unknown'), 'err');
        } else if (!r.ok) {
            setStatus('.dc-ner-install-st', r.error || 'Failed (exit ' + r.returncode + ')', 'err');
            if (logEl && r.stdout) {
                logEl.innerHTML = esc(r.stdout.slice(-600));
                logEl.style.display = '';
            }
        } else {
            setStatus('.dc-ner-install-st', 'Installed OK', 'ok');
            log('\u2713 NER install done: ' + (pkg || spmodel), 'ok');
            if (logEl && r.steps) {
                var out = r.steps.map(function(s) { return (s.step || '') + ': ' + (s.stdout || '').slice(-200); }).join('\n');
                if (out) { logEl.innerHTML = esc(out); logEl.style.display = ''; }
            }
            await loadNerStatus(false);
        }
    };

    // ── GLiNER labels & threshold ────────────────────────────────────────
    q('.dc-ner-labels-load').onclick = async function() {
        var r = await api('/fabric/entity_graph/ner_labels', 'POST', {}, 10000);
        if (r && r.labels) {
            var labEl = q('.dc-ner-labels');
            if (labEl) labEl.value = r.labels.join(', ');
            var thrEl = q('.dc-ner-threshold');
            if (thrEl) thrEl.value = (r.threshold || 0.4).toString();
            setStatus('.dc-ner-labels-st', 'Loaded (' + r.labels.length + ' labels)', 'ok');
        } else {
            setStatus('.dc-ner-labels-st', 'Failed to load', 'err');
        }
    };
    q('.dc-ner-labels-apply').onclick = async function() {
        var labEl = q('.dc-ner-labels');
        var thrEl = q('.dc-ner-threshold');
        var labels = (labEl && labEl.value.trim()) || '';
        var thr = parseFloat((thrEl && thrEl.value) || '0.4');
        if (!labels && isNaN(thr)) { setStatus('.dc-ner-labels-st', 'Nothing to apply', 'warn'); return; }
        setStatus('.dc-ner-labels-st', 'Applying\u2026', '');
        var body = {};
        if (labels) body.labels = labels;
        if (!isNaN(thr) && thr > 0) body.threshold = thr;
        var r = await api('/fabric/entity_graph/ner_labels', 'POST', body, 10000);
        if (r && r.ok) {
            setStatus('.dc-ner-labels-st', r.labels.length + ' labels, threshold=' + r.threshold, 'ok');
            log('\u25cb GLiNER: ' + r.labels.length + ' labels, threshold=' + r.threshold, 'ok');
        } else {
            setStatus('.dc-ner-labels-st', (r && r.error) || 'Failed', 'err');
        }
    };


    q('.dc-ask-go').onclick = async function() {
        var qEl = q('.dc-ask-q');
        var question = (qEl && qEl.value || '').trim();
        if (!question) { setStatus('.dc-ask-status', 'Enter a question', 'warn'); return; }
        var dsId = active.crawlId ? '' : (active.datasetId || '');
        var crawlId = active.crawlId || '';
        setStatus('.dc-ask-status', 'Thinking\u2026', '');
        var ansEl = q('.dc-ask-answer');
        if (ansEl) { ansEl.style.display = 'none'; ansEl.textContent = ''; }
        q('.dc-ask-go').disabled = true;
        var r = await api('/fabric/discover/query', 'POST', {
            question: question,
            dataset_id: dsId,
            crawl_id: crawlId,
            max_context_pages: 40
        }, 90000);
        q('.dc-ask-go').disabled = false;
        if (!r || r.error) {
            setStatus('.dc-ask-status', r && r.error || 'Failed', 'err');
        } else {
            setStatus('.dc-ask-status', r.context_pages_used + ' pages used', 'ok');
            if (ansEl) {
                ansEl.textContent = r.answer || '(no answer)';
                ansEl.style.display = '';
                // Show in bottom drawer content panel too
                try {
                    if (graph && graph.bottomDrawer) {
                        graph.bottomDrawer.showContent('Ask: ' + question.slice(0, 60), r.answer || '');
                    }
                } catch(_) {}
            }
        }
    };
    var _askQ = q('.dc-ask-q');
    if (_askQ) {
        _askQ.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { q('.dc-ask-go').click(); }
        });
    }

    // ── Compile Document ─────────────────────────────────────────────────
    q('.dc-compile-go').onclick = async function() {
        var dsId = active.datasetId || '';
        if (!dsId) { setStatus('.dc-compile-status', 'No dataset active \u2014 discover a topic first', 'err'); return; }
        var style = (q('.dc-compile-style') && q('.dc-compile-style').value) || 'report';
        var maxPages = parseInt((q('.dc-compile-maxpages') && q('.dc-compile-maxpages').value) || '40', 10);
        var maxSec   = parseInt((q('.dc-compile-maxsec') && q('.dc-compile-maxsec').value) || '6', 10);
        setStatus('.dc-compile-status', 'Compiling\u2026 (this may take a while)', '');
        q('.dc-compile-go').disabled = true;
        log('Starting document compilation for ' + dsId + ' (' + style + ')\u2026', 'info');
        var r = await api('/fabric/discover/compile', 'POST', {
            dataset_id: dsId,
            style: style,
            max_pages: maxPages,
            max_sections: maxSec
        }, 360000);
        q('.dc-compile-go').disabled = false;
        if (!r || r.error) {
            setStatus('.dc-compile-status', r && r.error || 'Failed', 'err');
            log('Compile failed: ' + (r && r.error || 'unknown'), 'err');
        } else {
            setStatus('.dc-compile-status', r.sections.length + ' sections, ' + r.pages_used + ' pages', 'ok');
            log('\u25a6 Compiled: ' + r.title + ' (' + r.sections.length + ' sections, ' + r.pages_used + ' pages)', 'ok');
            // Show document in bottom drawer content panel
            try {
                if (graph && graph.bottomDrawer) {
                    graph.bottomDrawer.showContent(r.title || 'Compiled document', r.document || '');
                }
            } catch(_) {}
        }
    };

    loadHistory(true);
    loadModels();
    log('Discover+ ready','ok');
    // If we have a saved active crawl from a previous session, restore graph and strip
    if (active.crawlId) {
      log('\u21ba restoring session: ' + active.crawlId, 'info');
      updateCurStrip();
      setTimeout(pollOnce, 400);
      setTimeout(refreshSideLists, 600);
    }
    // Auto-activate this panel when first mounted (makes Discover the default open tab)
    try { if(papi&&papi.activate) setTimeout(papi.activate, 0); } catch(_) {}
  },
});
})();