/**
 * vera_graph_panel_worldview.js — WorldView sidebar panel for vera_graph.js
 * ============================================================================
 * Fixes in this revision
 * ─────────────────────
 *  • Stage counters (1·GNN 0/20ep …) now increment reliably: uses a direct
 *    SSE EventSource on /events in addition to the WS bus, so progress fires
 *    even before the WS handshake completes. Poll-timer fallback also added.
 *  • Latent map scoped to active sub-worldview: scope banner always visible;
 *    snapshot reflects the active model (swapped by activate). Status line
 *    states clearly what is being mapped.
 *  • Mini-canvas removed — graph IS the map.
 *  • Concepts: two injection modes — "connected" (concept centroid + dashed
 *    edges to members) and "zone" (concept node as orbital anchor, members
 *    placed in a circle around it with no visible edges).
 *  • Loss-history has an explicit "← training log" back button.
 *  • Active subview shown in scope banner at top; auto-plot on activate.
 */
(function(){
  'use strict';
  if (!window.veraUI || !window.veraUI.Graph || !window.veraUI.Graph.registerPanel) return;
  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  window.veraUI.Graph.registerPanel({
    id: 'worldview', title: 'WorldView', icon: '\u25c9', order: 20,
    mount: function(bodyEl, graph, papi){
      var apiBase = (papi && papi.apiBase) || (window._veraBase || '');
      var st = { subviews:[], activeView:'', activeDatasets:[], lastSnapshot:null, concepts:[] };

      async function api(path,method,payload,ms){
        var ctrl=new AbortController(),to=ms?setTimeout(function(){ctrl.abort();},ms):null;
        try{
          var o={method:method||'GET',signal:ctrl.signal,headers:{'Content-Type':'application/json'}};
          if(payload!==undefined&&method&&method!=='GET')o.body=JSON.stringify(payload);
          var r=await fetch(apiBase+path,o);return await r.json();
        }catch(e){return{error:String(e&&e.message||e)};}
        finally{if(to)clearTimeout(to);}
      }
      function $(s){return bodyEl.querySelector(s);}
      function setStatus(msg,type){
        var el=$('.wv-status');if(!el)return;
        el.textContent=msg||'';
        el.style.color=type==='err'?'var(--err,#c96b6b)':type==='ok'?'var(--ok,#8fb87a)':type==='warn'?'var(--warn,#c9955a)':'var(--dim,#6a6058)';
      }
      function _cid(c){return c.idx!==undefined?c.idx:(c.id!==undefined?c.id:c.concept);}

      // ── Markup ──────────────────────────────────────────────────────────────
      bodyEl.innerHTML=
        // SCOPE BANNER
        '<div class="wv-scope-bar"><span class="wv-scope-icon">\u25c9</span>'+
        '<span class="wv-scope-label">global worldview</span>'+
        '<button class="wv-scope-sw" style="display:none">\u00d7 global</button></div>'+

        // MAP
        '<details class="wvs" open><summary class="wvs-hd">\u25c9 Latent Map \u2192 Graph</summary><div class="wvs-body">'+
          '<div class="wr"><label>Limit</label><input class="wv-maplimit" type="number" value="500" min="10" max="5000" step="50">'+
          '<label style="margin-left:6px">Method</label><select class="wv-method"><option value="pca">PCA</option><option value="umap">UMAP</option></select></div>'+
          '<div class="wr"><label>Concepts</label><select class="wv-concept-mode">'+
            '<option value="connected">connected (edges to members)</option>'+
            '<option value="zone">zone (orbital anchor, no edges)</option>'+
            '<option value="none">hide concepts</option>'+
          '</select></div>'+
          '<div style="display:flex;gap:3px;margin-top:4px">'+
            '<button class="wvb wv-plot" style="flex:1">\u25b6 Map active view \u2192 graph</button>'+
            '<button class="wvb wv-clearmap" style="flex:0;padding:4px 8px" title="Clear worldview layer">\u2715</button>'+
          '</div>'+
          '<div class="wv-mapinfo" style="font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);margin-top:3px;min-height:10px"></div>'+
        '</div></details>'+

        // QUERY
        '<details class="wvs"><summary class="wvs-hd">\u2315 Query</summary><div class="wvs-body">'+
          '<input class="wv-qtxt" placeholder="Search in latent space\u2026" style="width:100%;margin-bottom:4px">'+
          '<div class="wr"><label>Top K</label><input class="wv-qtopk" type="number" value="10" min="1" max="100"></div>'+
          '<button class="wvb wv-qrun">\u25b6 Query \u2192 highlight graph</button>'+
          '<div class="wv-qresults" style="max-height:150px;overflow-y:auto;margin-top:4px"></div>'+
        '</div></details>'+

        // ANOMALIES
        '<details class="wvs"><summary class="wvs-hd">\u26a0 Anomalies</summary><div class="wvs-body">'+
          '<div class="wr"><label>Top K</label><input class="wv-atopk" type="number" value="20" min="1" max="100"></div>'+
          '<button class="wvb wv-arun">\u26a0 Detect \u2192 highlight graph</button>'+
          '<div class="wv-aresults" style="max-height:170px;overflow-y:auto;margin-top:4px"></div>'+
        '</div></details>'+

        // CONCEPTS
        '<details class="wvs"><summary class="wvs-hd">\u25cf Concepts</summary><div class="wvs-body">'+
          '<div style="display:flex;gap:3px;margin-bottom:4px">'+
            '<button class="wvb wv-cload" style="flex:1">Load concepts</button>'+
            '<button class="wvb wv-clabel" style="flex:1">\u2699 AI label</button>'+
          '</div>'+
          '<div class="wv-clist" style="max-height:210px;overflow-y:auto"></div>'+
        '</div></details>'+

        // TRAINING
        '<details class="wvs"><summary class="wvs-hd">\u2699 Training</summary><div class="wvs-body">'+
          '<div class="wv-lasttrained" style="font-size:8px;font-family:var(--mono,monospace);color:var(--dim,#6a6058);margin-bottom:5px;padding:3px 5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px">Last trained: checking\u2026</div>'+
          '<div class="wr"><label>GNN epochs</label><input class="wv-gnn" type="number" value="20" min="0" max="200"></div>'+
          '<div class="wr"><label>Codebook</label><input class="wv-codebook" type="number" value="8" min="0" max="100"></div>'+
          '<div class="wr"><label>Dynamics</label><input class="wv-dynamics" type="number" value="15" min="0" max="100"></div>'+
          '<div class="wr"><label>Max records</label><input class="wv-limit" type="number" value="5000" min="100" max="50000" step="500"></div>'+
          '<div class="wv-cbrow"><input class="wv-embed" type="checkbox" checked><span>Back-fill embeddings first</span></div>'+
          '<div class="wv-cbrow"><input class="wv-usenodes" type="checkbox" checked><span>Train on current graph nodes</span></div>'+
          '<button class="wvb wvb-acc wv-train">\u25b6 Train active view</button>'+
          '<div style="display:flex;gap:3px;margin-top:2px">'+
            '<button class="wvb wv-update" style="flex:1">\u21bb Update (incremental)</button>'+
            '<button class="wvb wv-update-global" style="flex:1">\u21bb Update global</button>'+
          '</div>'+
          '<div class="wr" style="margin-top:3px">'+
            '<label style="min-width:0;flex:1;font-size:9px;color:var(--dim,#6a6058)">Auto-update on graph change</label>'+
            '<input class="wv-autoupdate" type="checkbox" style="width:auto;cursor:pointer;accent-color:var(--acc,#5a9e8f)">'+
          '</div>'+
          '<div class="wv-stages" style="display:none;margin-top:6px">'+
            '<div class="wv-stage" data-stage="gnn"></div>'+
            '<div class="wv-stage" data-stage="codebook"></div>'+
            '<div class="wv-stage" data-stage="dynamics"></div>'+
          '</div>'+
          '<div class="wv-trainlog wvlog" style="display:none;margin-top:4px"></div>'+
          // Loss history view — hidden until fetched, has back button
          '<div class="wv-lossview" style="display:none;margin-top:4px">'+
            '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'+
              '<button class="wvb wv-lossback" style="width:auto;padding:2px 8px;font-size:8px">\u2190 training log</button>'+
              '<span style="font-size:8px;color:var(--dim,#6a6058)">Loss history</span>'+
            '</div>'+
            '<div class="wv-lossout wvlog"></div>'+
          '</div>'+
          '<button class="wvb wv-losshistory" style="margin-top:3px">\u2197 Loss history</button>'+
        '</div></details>'+

        // SUBVIEWS
        '<details class="wvs"><summary class="wvs-hd">\u229a Sub-worldviews</summary><div class="wvs-body">'+
          '<div class="wv-sublist" style="max-height:130px;overflow-y:auto;margin-bottom:4px"></div>'+
          '<div style="font-size:8px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;margin:6px 0 3px">Create from current graph</div>'+
          '<input class="wv-newname" placeholder="sub-worldview name" style="width:100%;margin-bottom:4px">'+
          '<div class="wv-graphinfo" style="font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);margin-bottom:4px"></div>'+
          '<button class="wvb wvb-acc wv-create">Save graph + create &amp; train</button>'+
        '</div></details>'+

        // STATS
        '<details class="wvs"><summary class="wvs-hd">\u2139 Stats</summary><div class="wvs-body">'+
          '<button class="wvb wv-stats">Refresh stats</button>'+
          '<div class="wv-statsout wvlog" style="display:none"></div>'+
        '</div></details>'+
        '<div class="wv-status" style="font-size:9px;margin-top:6px;min-height:12px;color:var(--dim,#6a6058)"></div>';

      // ── CSS (once) ────────────────────────────────────────────────────────
      if(!document.getElementById('vg-wv-css')){
        var s=document.createElement('style');s.id='vg-wv-css';
        s.textContent=[
          '.wv-scope-bar{display:flex;align-items:center;gap:5px;padding:4px 7px;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);border-radius:3px;margin-bottom:5px;font-size:9px}',
          '.wv-scope-icon{color:var(--acc,#5a9e8f);font-size:11px;flex-shrink:0}',
          '.wv-scope-label{flex:1;font-family:var(--mono,monospace);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text,#ddd5c8)}',
          '.wv-scope-sw{background:none;border:1px solid var(--border,#3a3530);color:var(--dim,#6a6058);border-radius:3px;padding:1px 6px;font-size:8px;cursor:pointer;flex-shrink:0}',
          '.wv-scope-sw:hover{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
          '.wvs{margin-bottom:4px;border:1px solid var(--border,#3a3530);border-radius:3px;background:var(--bg0,#181614);overflow:hidden}',
          '.wvs-hd{padding:5px 8px;font-size:9.5px;color:var(--text,#ddd5c8);cursor:pointer;list-style:none;background:var(--bg1,#1f1d1a);border-bottom:1px solid transparent;user-select:none}',
          '.wvs-hd::-webkit-details-marker{display:none}',
          '.wvs[open] .wvs-hd{border-bottom-color:var(--border,#3a3530)}',
          '.wvs-body{padding:6px 8px}',
          '.wr{display:flex;align-items:center;gap:6px;margin-bottom:3px;font-size:9px;color:var(--dim2,#8a7e70)}',
          '.wr label{min-width:70px;flex-shrink:0}',
          '.wr input,.wr select{flex:1;min-width:0;font-size:9px;padding:2px 4px;background:var(--bg1,#1f1d1a);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;font-family:var(--mono,monospace)}',
          '.wr input[type=number]{max-width:70px}.wr input[type=checkbox]{flex:0;width:auto}',
          '.wvb{width:100%;font-size:9px;padding:4px;background:rgba(90,158,143,.1);border:1px solid var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);border-radius:3px;cursor:pointer;font-family:var(--mono,monospace);margin-bottom:2px}',
          '.wvb-acc{background:rgba(201,149,90,.12);border-color:var(--acc3,#c9955a);color:var(--acc3,#c9955a)}',
          '.wvlog{font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);max-height:120px;overflow-y:auto;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);border-radius:3px;padding:4px 6px;white-space:pre-wrap}',
          '.wv-row{padding:3px 6px;border-bottom:1px solid var(--border,#3a3530);cursor:pointer;font-size:9.5px;display:flex;gap:6px;align-items:center;transition:.08s}',
          '.wv-row:hover{background:var(--bg2,#272421);color:var(--acc,#5a9e8f)}',
          '.wv-stage{border:1px solid var(--border,#3a3530);border-radius:3px;padding:4px 6px;margin-bottom:3px;background:var(--bg0,#181614)}',
          '.wv-cbrow{display:flex;align-items:center;gap:7px;padding:3px 0;font-size:9px;color:var(--dim2,#8a7e70);cursor:pointer;user-select:none}',
          '.wv-cbrow input[type=checkbox]{width:12px;height:12px;cursor:pointer;accent-color:var(--acc,#5a9e8f);flex-shrink:0}',
          '.wv-cbrow span{flex:1}',
        ].join('\n');
        document.head.appendChild(s);
      }

      // ── Scope banner ─────────────────────────────────────────────────────
      function _updateScopeBanner(){
        var lbl=$('.wv-scope-label'),sw=$('.wv-scope-sw'),ic=$('.wv-scope-icon');
        if(!lbl)return;
        if(st.activeView){
          lbl.textContent=st.activeView;lbl.style.color='var(--acc,#5a9e8f)';lbl.style.fontWeight='600';
          if(ic)ic.textContent='\u25c8';if(sw)sw.style.display='';
        }else{
          lbl.textContent='global worldview';lbl.style.color='var(--dim,#6a6058)';lbl.style.fontWeight='400';
          if(ic)ic.textContent='\u25c9';if(sw)sw.style.display='none';
        }
      }
      var swBtn=$('.wv-scope-sw');
      if(swBtn)swBtn.onclick=function(){activateSubview('');};

      // ── Graph helpers ────────────────────────────────────────────────────
      function _highlightIds(ids){
        var hl=new Set(ids);
        if(graph.state)graph.state.searchHighlight=hl;
        if(typeof graph.draw==='function')graph.draw();
      }
      function _highlightByConcept(cid){
        var hl=new Set();
        var nodes=(graph.state&&graph.state.nodes)||[];
        nodes.forEach(function(n){
          if(n.props&&String(n.props.concept)===String(cid))hl.add(n.id);
          if(n.id==='wv-concept-'+cid)hl.add(n.id);
        });
        if(graph.state)graph.state.searchHighlight=hl;
        if(typeof graph.draw==='function')graph.draw();
        setStatus('Concept '+cid+': '+hl.size+' nodes highlighted',hl.size?'ok':'warn');
      }

      // ── MAP ──────────────────────────────────────────────────────────────
      async function plotSnapshot(){
        var mode   =($('.wv-concept-mode')||{}).value||'connected';
        var method =($('.wv-method')||{}).value||'pca';
        var limit  =parseInt(($('.wv-maplimit')||{}).value||'500');
        var scope  =st.activeView?'"'+st.activeView+'"':'global';
        setStatus('Mapping '+scope+'\u2026');
        var mi=$('.wv-mapinfo');if(mi)mi.textContent='Fetching '+scope+'\u2026';

        var snap=await api('/worldview/snapshot?method='+method+'&limit='+limit);
        if(!snap||snap.error){
          setStatus((snap&&snap.error)||'No snapshot','err');
          if(mi)mi.textContent='Error: '+((snap&&snap.error)||'no data');return;
        }
        st.lastSnapshot=snap;
        var pts=snap.points||[],cons=snap.concepts||[];
        cons.forEach(function(c){if(c.idx===undefined)c.idx=c.id!==undefined?c.id:c.concept;});

        if(!pts.length){
          setStatus('Empty map — train '+scope+' first','err');
          if(mi)mi.textContent='No vectors — train first';return;
        }

        var SP=900,ZONE_R=80;
        var nodes=[],edges=[],positions={};
        var showCons=mode!=='none',zoneMode=mode==='zone';

        // Concept centroid nodes
        var conceptPos={};
        cons.forEach(function(c){
          conceptPos[_cid(c)]={x:(c.x!=null?c.x:0.5)*SP,y:(c.y!=null?c.y:0.5)*SP};
          if(showCons){
            var cid='wv-concept-'+_cid(c);
            nodes.push({id:cid,label:c.label||('C'+_cid(c)),type:'Concept',layer:'worldview',
              x:conceptPos[_cid(c)].x,y:conceptPos[_cid(c)].y,
              r:zoneMode?20:13,
              props:{concept:_cid(c),members:c.count||c.size||0,label:c.label||''}});
            positions[cid]=conceptPos[_cid(c)];
          }
        });

        var memberCount={};
        pts.forEach(function(p){
          var px=(p.x||0)*SP,py=(p.y||0)*SP;
          if(zoneMode&&showCons&&p.concept!==undefined&&p.concept>=0&&conceptPos[p.concept]){
            var cp=conceptPos[p.concept];
            var idx=memberCount[p.concept]||0;memberCount[p.concept]=idx+1;
            var total=(cons.find(function(c){return _cid(c)===p.concept;})||{}).count||1;
            var angle=(idx/Math.max(total,1))*Math.PI*2;
            var r=ZONE_R+Math.floor(idx/12)*32;
            px=cp.x+Math.cos(angle)*r;py=cp.y+Math.sin(angle)*r;
          }
          nodes.push({id:p.id,label:(p.text||p.id||'').slice(0,40),type:'WorldviewPoint',layer:'worldview',
            x:px,y:py,r:6,props:{dataset_id:p.dataset_id,concept:p.concept,concept_label:p.concept_label||'',text:p.text||''}});
          positions[p.id]={x:px,y:py};
          if(!zoneMode&&showCons&&p.concept!==undefined&&p.concept>=0){
            var cid2='wv-concept-'+p.concept;
            if(positions[cid2])edges.push({from:cid2,to:p.id,rel:'IN_CONCEPT',layer:'worldview',_dashed:true});
          }
        });

        // Merge worldview layer into existing graph — do NOT call graph.load()
        // which wipes state.nodes and destroys the existing training graph.
        if(graph.state){
          // Remove old worldview nodes/edges
          graph.state.nodes=(graph.state.nodes||[]).filter(function(n){return n.layer!=='worldview';});
          graph.state.edges=(graph.state.edges||[]).filter(function(e){return e.layer!=='worldview';});
          graph.state.nodeIndex={};
          (graph.state.nodes||[]).forEach(function(n){graph.state.nodeIndex[n.id]=n;});
        }
        // Add new worldview nodes via addNode (preserves existing non-wv nodes)
        nodes.forEach(function(n){if(graph.addNode)graph.addNode(n,true);});
        edges.forEach(function(e){if(graph.addEdge)graph.addEdge(e,true);});
        if(graph.setLatentMap)graph.setLatentMap(positions);
        if(graph.setLayerVisible)graph.setLayerVisible('worldview',true);
        if(typeof graph.draw==='function')graph.draw();

        var modeLabel=zoneMode?'zone':(showCons?'connected':'no concepts');
        var desc=scope+' \u00b7 '+pts.length+' pts \u00b7 '+cons.length+' concepts \u00b7 '+method+' \u00b7 '+modeLabel;
        setStatus(desc,'ok');if(mi)mi.textContent=desc;
      }

      function clearMap(){
        if(graph.setLayerVisible)graph.setLayerVisible('worldview',false);
        if(graph.state&&graph.state.nodes){
          graph.load({
            nodes:(graph.state.nodes||[]).filter(function(n){return n.layer!=='worldview';}),
            edges:(graph.state.edges||[]).filter(function(e){return e.layer!=='worldview';})
          });
        }
        setStatus('Worldview layer cleared','ok');
        var mi=$('.wv-mapinfo');if(mi)mi.textContent='';
      }

      // ── QUERY ────────────────────────────────────────────────────────────
      async function runQuery(){
        var text=($('.wv-qtxt')||{}).value.trim();if(!text){setStatus('Enter query text','err');return;}
        var topk=parseInt(($('.wv-qtopk')||{}).value||'10');
        setStatus('Querying\u2026');
        var res=await api('/worldview/query','POST',{text:text,top_k:topk});
        var el=$('.wv-qresults');if(!el)return;
        if(res.error){el.innerHTML='<div style="color:var(--err,#c96b6b);padding:4px">'+esc(res.error)+'</div>';setStatus(res.error,'err');return;}
        var results=res.results||res.matches||[];
        if(!results.length){el.innerHTML='<div style="color:var(--dim,#6a6058);padding:4px">No results.</div>';setStatus('0 results');return;}
        el.innerHTML=results.map(function(r){
          var sc=r.score!==undefined?(typeof r.score==='number'?r.score.toFixed(3):r.score):'';
          return '<div class="wv-row wv-qrow" data-id="'+esc(r.id||'')+'">'+
            '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc((r.text||r.id||'').slice(0,60))+'</span>'+
            '<span style="font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace)">'+esc(sc)+'</span></div>';
        }).join('');
        _highlightIds(results.map(function(r){return r.id||''}).filter(Boolean));
        el.querySelectorAll('.wv-qrow').forEach(function(row){
          row.onclick=function(){var id=row.getAttribute('data-id');_highlightIds([id]);if(graph.focusNode)graph.focusNode(id);};
        });
        setStatus(results.length+' results \u2192 highlighted','ok');
      }

      // ── ANOMALIES ────────────────────────────────────────────────────────
      async function runAnomalies(){
        var topk=parseInt(($('.wv-atopk')||{}).value||'20');
        setStatus('Detecting\u2026');
        var res=await api('/worldview/anomalies','POST',{top_k:topk});
        var el=$('.wv-aresults');if(!el)return;
        if(res.error){el.innerHTML='<div style="color:var(--err,#c96b6b);padding:4px">'+esc(res.error)+'</div>';setStatus(res.error,'err');return;}
        var items=res.anomalies||res.results||[];
        if(!items.length){el.innerHTML='<div style="color:var(--dim,#6a6058);padding:4px">No anomalies.</div>';setStatus('0 anomalies');return;}
        el.innerHTML=items.map(function(a){
          var sc=a.anomaly_score!==undefined?(typeof a.anomaly_score==='number'?a.anomaly_score.toFixed(3):a.anomaly_score):'';
          return '<div class="wv-row wv-arow" data-id="'+esc(a.id||'')+'">'+
            '<span style="color:var(--err,#c96b6b);font-size:8px;font-family:var(--mono,monospace);flex-shrink:0;min-width:32px">'+esc(sc)+'</span>'+
            '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc((a.text||a.id||'').slice(0,55))+'</span></div>';
        }).join('');
        _highlightIds(items.map(function(a){return a.id||''}).filter(Boolean));
        el.querySelectorAll('.wv-arow').forEach(function(row){
          row.onclick=function(){var id=row.getAttribute('data-id');_highlightIds([id]);if(graph.focusNode)graph.focusNode(id);};
        });
        setStatus(items.length+' anomalies \u2192 highlighted','ok');
      }

      // ── CONCEPTS ─────────────────────────────────────────────────────────
      async function loadConcepts(){
        setStatus('Loading concepts\u2026');
        var res=await api('/worldview/concepts');
        if(res.error){setStatus(res.error,'err');return;}
        var all=res.concepts||[];
        var scopedCids=null;
        if(st.activeView&&st.lastSnapshot&&st.lastSnapshot.points){
          scopedCids=new Set();
          st.lastSnapshot.points.forEach(function(p){if(p.concept!==undefined&&p.concept>=0)scopedCids.add(p.concept);});
        }
        var items=scopedCids?all.filter(function(c){return scopedCids.has(_cid(c));}):all;
        st.concepts=items;
        var el=$('.wv-clist');if(!el)return;
        if(!items.length){
          el.innerHTML='<div style="color:var(--dim,#6a6058);padding:4px;font-size:9px">No concepts'+(st.activeView?' in "'+esc(st.activeView)+'"':'')+'. Train first.</div>';
          setStatus('0 concepts','warn');return;
        }
        el.innerHTML=items.map(function(c){
          var cid=_cid(c);
          return '<div class="wv-row wv-crow" data-cid="'+esc(cid)+'">'+
            '<span style="width:16px;height:16px;border-radius:50%;background:var(--acc,#5a9e8f);display:flex;align-items:center;justify-content:center;font-size:7px;color:#181614;font-weight:700;flex-shrink:0">'+esc(cid)+'</span>'+
            '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(c.label||'Concept '+cid)+'</span>'+
            '<span style="font-size:8px;color:var(--dim,#6a6058);font-family:var(--mono,monospace)">'+(c.count||c.size||'?')+' mbrs</span></div>';
        }).join('');
        el.querySelectorAll('.wv-crow').forEach(function(row){
          row.onclick=function(){_highlightByConcept(row.getAttribute('data-cid'));};
        });
        setStatus(items.length+' concepts'+(scopedCids?' (scoped to "'+esc(st.activeView)+'")':''),'ok');
      }
      async function labelConcepts(){
        setStatus('Labelling with AI\u2026');
        var res=await api('/worldview/label_concepts','POST',{},120000);
        if(res&&res.error){setStatus(res.error,'err');return;}
        setStatus('Labelled '+(res.labelled||0)+' concepts','ok');
        await loadConcepts();
      }

      // ── TRAINING ─────────────────────────────────────────────────────────
      var _stageData={gnn:[],codebook:[],dynamics:[]};
      var _stageMeta={gnn:{epochs:0,done:false},codebook:{epochs:0,done:false},dynamics:{epochs:0,done:false}};

      function _sparkline(vals){
        if(!vals.length)return '';
        var w=160,h=24,n=vals.length;
        var min=Math.min.apply(null,vals),max=Math.max.apply(null,vals),rng=(max-min)||1;
        var pts=vals.map(function(v,i){
          return ((n===1?0:(i/(n-1))*w).toFixed(1))+','+(h-((v-min)/rng)*(h-4)-2).toFixed(1);
        }).join(' ');
        return '<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" style="display:block;margin-top:2px">'+
          '<polyline points="'+pts+'" fill="none" stroke="var(--acc,#5a9e8f)" stroke-width="1.4"/></svg>';
      }

      function _renderStage(stage){
        var el=bodyEl.querySelector('.wv-stage[data-stage="'+stage+'"]');if(!el)return;
        var vals=_stageData[stage],meta=_stageMeta[stage];
        var label={gnn:'1\u00b7GNN',codebook:'2\u00b7Codebook',dynamics:'3\u00b7Dynamics'}[stage];
        var col={gnn:'var(--acc,#5a9e8f)',codebook:'var(--acc2,#8fb87a)',dynamics:'var(--acc3,#c9955a)'}[stage];
        var last=vals.length?vals[vals.length-1]:null;
        var epTxt=meta.epochs>0?(vals.length+'/'+meta.epochs+'ep'):(vals.length?vals.length+'ep':'');
        el.innerHTML=
          '<div style="display:flex;align-items:center;gap:5px;font-size:8px">'+
            '<span style="width:6px;height:6px;border-radius:50%;background:'+col+';opacity:'+(vals.length||meta.done?1:0.25)+'"></span>'+
            '<span style="flex:1;color:var(--text,#ddd5c8)">'+label+'</span>'+
            '<span style="color:var(--dim,#6a6058);font-family:var(--mono,monospace)">'+
              epTxt+(last!=null?' '+(typeof last==='number'?last.toFixed(3):last):'')+(meta.done?' \u2713':'')+
            '</span>'+
          '</div>'+_sparkline(vals);
      }

      function _resetStages(plan){
        _stageData={gnn:[],codebook:[],dynamics:[]};
        _stageMeta={
          gnn:     {epochs:(plan&&plan.gnn_epochs)||0,     done:false},
          codebook:{epochs:(plan&&plan.codebook_epochs)||0,done:false},
          dynamics:{epochs:(plan&&plan.dynamics_epochs)||0,done:false},
        };
        var box=$('.wv-stages');
        if(box){box.style.display='block';} // must be visible BEFORE _renderStage so SVG gets real width
        ['gnn','codebook','dynamics'].forEach(_renderStage);
      }

      function _onProgress(ev){
        var d=ev&&(ev.data||ev)||{};
        var stage=d.stage||'';
        // Ignore the outer envelope type; only care about stage field
        if(!stage||stage==='worldview.progress')return;

        if(stage==='train_plan')                _resetStages(d);
        else if(stage==='gnn_epoch')            {if(d.loss!=null)_stageData.gnn.push(+d.loss);_renderStage('gnn');}
        else if(stage==='codebook_epoch')       {if(d.loss!=null)_stageData.codebook.push(+d.loss);_renderStage('codebook');}
        else if(stage==='dynamics_epoch')       {if(d.loss!=null)_stageData.dynamics.push(+d.loss);_renderStage('dynamics');}
        else if(stage==='stage_codebook')       {_stageMeta.gnn.done=true;_renderStage('gnn');}
        else if(stage==='stage_dynamics')       {_stageMeta.codebook.done=true;_renderStage('codebook');}
        else if(stage==='done'||stage==='complete'){
          _stageMeta.gnn.done=_stageMeta.codebook.done=_stageMeta.dynamics.done=true;
          ['gnn','codebook','dynamics'].forEach(_renderStage);
        }
        var logEl=$('.wv-trainlog');
        if(logEl&&logEl.style.display!=='none'){
          var line=(stage?'['+stage+'] ':'')+
            (d.epoch!==undefined?'ep'+d.epoch+'/'+(d.total||'?')+' ':'')+
            (d.loss!==undefined?'loss '+(typeof d.loss==='number'?d.loss.toFixed(4):d.loss)+' ':'')+
            (d.message||'');
          if(line.trim()){logEl.textContent+=line.trim()+'\n';logEl.scrollTop=logEl.scrollHeight;}
        }
      }

      // ── SSE direct connection (reliable path for training progress) ───────
      var _trainEvtSrc=null;
      function _startTrainSSE(){
        _stopTrainSSE();
        try{
          _trainEvtSrc=new EventSource(apiBase+'/events');
          _trainEvtSrc.onmessage=function(e){
            try{var d=JSON.parse(e.data);if(d&&d.type&&d.type.indexOf('worldview')===0)_onProgress(d);}catch(_){}
          };
          _trainEvtSrc.onerror=function(){_stopTrainSSE();};
        }catch(_){_trainEvtSrc=null;}
      }
      function _stopTrainSSE(){if(_trainEvtSrc){try{_trainEvtSrc.close();}catch(_){}}_trainEvtSrc=null;}

      // ── Poll loss history every 3s as belt-and-braces fallback ───────────
      var _pollTimer=null;
      function _startPoll(){
        _stopPoll();
        _pollTimer=setInterval(async function(){
          var res=await api('/worldview/loss_history');
          if(!res||res.error)return;
          var up=false;
          if(res.gnn&&res.gnn.length>_stageData.gnn.length){_stageData.gnn=res.gnn.map(Number);_renderStage('gnn');up=true;}
          if(res.codebook&&res.codebook.length>_stageData.codebook.length){_stageData.codebook=res.codebook.map(Number);_renderStage('codebook');up=true;}
          if(res.dynamics&&res.dynamics.length>_stageData.dynamics.length){_stageData.dynamics=res.dynamics.map(Number);_renderStage('dynamics');up=true;}
          void up;
        },3000);
      }
      function _stopPoll(){if(_pollTimer){clearInterval(_pollTimer);_pollTimer=null;}}

      // ── Graph summary ─────────────────────────────────────────────────────
      function _activeGraphSummary(){
        var nodes=(graph.state&&graph.state.nodes)||[];
        var labels={},dsIds={},nodeIds=[];
        nodes.forEach(function(n){
          if(n.layer==='worldview')return;
          if(n.type)labels[n.type]=(labels[n.type]||0)+1;
          var ds=n.props&&n.props.dataset_id;if(ds)dsIds[ds]=1;
          if(n.id)nodeIds.push(n.id);
        });
        return{nodeCount:nodeIds.length,labels:Object.keys(labels),datasets:Object.keys(dsIds),nodeIds:nodeIds};
      }
      function _refreshGraphInfo(){
        var s=_activeGraphSummary(),el=$('.wv-graphinfo');
        if(el)el.textContent=s.nodeCount+' nodes \u00b7 '+(s.datasets.length?s.datasets.length+' ds':'no ds');
      }

      // ── Training lock — disables all train/update buttons during a run ──────
      var _TRAIN_BTNS=['.wv-train','.wv-update','.wv-update-global','.wv-create'];
      function _setTrainingLock(locked){
        _TRAIN_BTNS.forEach(function(sel){
          var el=$(sel);if(!el)return;
          el.disabled=locked;
          el.style.opacity=locked?'0.4':'';
          el.style.cursor=locked?'not-allowed':'';
        });
        // Also lock epoch inputs while running so they can't be changed mid-flight
        ['.wv-gnn','.wv-codebook','.wv-dynamics','.wv-limit'].forEach(function(sel){
          var el=$(sel);if(el)el.disabled=locked;
        });
      }

      async function _refreshLastTrained(){
        var el=$('.wv-lasttrained');if(!el)return;
        var res=await api('/worldview/stats');
        if(!res||res.error){el.textContent='Last trained: unknown';return;}
        var ts=res.last_trained||(res.model&&res.model.last_trained)||null;
        var saved=res.model_saved||res.saved||(res.model&&res.model.saved)||false;
        var view=res.active_subview||st.activeView||'global';
        if(!ts){
          el.textContent='Last trained: never — \u26a0 model not persisted';
          el.style.color='var(--warn,#c9955a)';
        }else{
          var ago=_relTime(ts);
          el.textContent='Last trained: '+ago+(saved?' \u2713 saved':' \u26a0 not saved')
            +' \u00b7 '+view;
          el.style.color=saved?'var(--ok,#8fb87a)':'var(--warn,#c9955a)';
        }
      }
      function _relTime(iso){
        try{
          var d=new Date(iso),now=new Date(),diff=Math.floor((now-d)/1000);
          if(diff<60)return diff+'s ago';
          if(diff<3600)return Math.floor(diff/60)+'m ago';
          if(diff<86400)return Math.floor(diff/3600)+'h ago';
          return Math.floor(diff/86400)+'d ago';
        }catch(_){return String(iso);}
      }

      // ── TRAIN ────────────────────────────────────────────────────────────
      async function trainActive(){
        var logEl=$('.wv-trainlog'),lossView=$('.wv-lossview');
        if(lossView)lossView.style.display='none';
        if(logEl){logEl.style.display='block';logEl.textContent='';}
        setStatus('Training \u201c'+(st.activeView||'global')+'\u201d\u2026');

        var gnnEl=$('.wv-gnn'),cbEl=$('.wv-codebook'),dynEl=$('.wv-dynamics'),limEl=$('.wv-limit');
        var gnn     =parseInt(gnnEl&&gnnEl.value||'20',10);
        var codebook=parseInt(cbEl&&cbEl.value||'8',10);
        var dynamics=parseInt(dynEl&&dynEl.value||'15',10);
        var limit   =parseInt(limEl&&limEl.value||'5000',10);
        var embed   =!!($('.wv-embed')||{}).checked;
        var useNodes=!!($('.wv-usenodes')||{}).checked;
        if(logEl)logEl.textContent='GNN='+gnn+' CB='+codebook+' Dyn='+dynamics+' Limit='+limit+'\nStarting\u2026\n';

        _resetStages({gnn_epochs:gnn,codebook_epochs:codebook,dynamics_epochs:dynamics});

        var bus=(papi&&papi.eventBus),unsub=null;
        if(bus&&bus.subscribe)unsub=bus.subscribe('worldview.progress',_onProgress);
        _startTrainSSE();_startPoll();
        _setTrainingLock(true);
        try{
          var summ=_activeGraphSummary();
          var payload={dataset_id:'',gnn_epochs:gnn,codebook_epochs:codebook,dynamics_epochs:dynamics,limit:limit,embed_missing:embed};
          if(useNodes&&summ.nodeIds.length)payload.node_ids=summ.nodeIds.slice(0,limit||5000);
          var res=await api('/worldview/train','POST',payload,7200000);
          if(res&&res.error){
            setStatus(res.error,'err');if(logEl)logEl.textContent+='[error] '+res.error+'\n';
          }else{
            _stageMeta.gnn.done=_stageMeta.codebook.done=_stageMeta.dynamics.done=true;
            ['gnn','codebook','dynamics'].forEach(_renderStage);
            var dur=res.duration_s?' in '+Math.round(res.duration_s)+'s':'';
            setStatus('Trained "'+(st.activeView||'global')+'"'+dur+' \u2014 mapping\u2026','ok');
            if(logEl)logEl.textContent+='[done]'+dur+'\n';
            await _refreshLastTrained();
            await plotSnapshot();
          }
        }catch(e){setStatus(String(e&&e.message||e),'err');}
        finally{
          _stopTrainSSE();_stopPoll();
          if(unsub)try{unsub();}catch(_){}
          _setTrainingLock(false);
        }
      }

      async function fetchLossHistory(){
        var res=await api('/worldview/loss_history');
        var lossView=$('.wv-lossview'),logEl=$('.wv-trainlog'),lossOut=$('.wv-lossout');
        if(!lossOut)return;
        if(logEl)logEl.style.display='none';
        if(lossView)lossView.style.display='block';
        var txt='';
        if(res&&res.gnn&&res.gnn.length){
          _stageData.gnn=res.gnn.map(Number);_renderStage('gnn');
          txt+='GNN:      '+res.gnn.map(function(v){return typeof v==='number'?v.toFixed(4):v;}).join(', ')+'\n';
        }
        if(res&&res.codebook&&res.codebook.length){
          _stageData.codebook=res.codebook.map(Number);_renderStage('codebook');
          txt+='Codebook: '+res.codebook.map(function(v){return typeof v==='number'?v.toFixed(4):v;}).join(', ')+'\n';
        }
        if(res&&res.dynamics&&res.dynamics.length){
          _stageData.dynamics=res.dynamics.map(Number);_renderStage('dynamics');
          txt+='Dynamics: '+res.dynamics.map(function(v){return typeof v==='number'?v.toFixed(4):v;}).join(', ')+'\n';
        }
        lossOut.textContent=txt||'(no history yet)';
      }

      // Loss history back button
      var lossBackBtn=$('.wv-lossback');
      if(lossBackBtn)lossBackBtn.onclick=function(){
        var lossView=$('.wv-lossview'),logEl=$('.wv-trainlog');
        if(lossView)lossView.style.display='none';
        if(logEl)logEl.style.display='block';
      };

      // ── SUBVIEWS ─────────────────────────────────────────────────────────
      async function loadSubviews(skipAutoActivate){
        var res=await api('/worldview/subviews');
        st.subviews=(res&&res.subviews)||[];
        st.activeView=(res&&res.active)||'';
        st.activeDatasets=[];
        _reflectActiveView();

        // On first load, if no sub-worldview is active yet, try to match graph
        if(!skipAutoActivate && !st.activeView){
          var summ=_activeGraphSummary();
          if(summ.datasets.length || summ.nodeCount){
            // Try to find an existing match first
            var match=st.subviews.find(function(sv){
              var svds=(sv.datasets||[]);
              return svds.some(function(d){return summ.datasets.indexOf(String(d))>=0;});
            });
            if(match){
              await activateSubview(match.name||match);
              return;
            }
            // No match — auto-create a blank sub-worldview for this graph
            if(summ.nodeCount>0){
              var autoName=summ.datasets.length?
                'graph_'+(summ.datasets[0]||'').replace(/[^A-Za-z0-9]/g,'_').slice(0,24):
                'graph_'+Date.now().toString(36);
              setStatus('Creating sub-worldview "'+autoName+'"\u2026');
              var crt=await api('/worldview/subviews/create','POST',{
                name:autoName,datasets:summ.datasets,node_ids:summ.nodeIds.slice(0,5000)});
              if(!crt||crt.error){
                setStatus((crt&&crt.error)||'Could not auto-create sub-worldview','warn');
              }else{
                await api('/worldview/subviews/activate','POST',{name:autoName});
                st.activeView=autoName;_reflectActiveView();
                setStatus('Auto-created "'+autoName+'" \u2014 ready to train','ok');
                await loadSubviews(true);
                return;
              }
            }
          }
        }

        var el=$('.wv-sublist');if(!el)return;
        var mkRow=function(name,active){
          var aStyle=active?'background:rgba(90,158,143,.1);font-weight:600;color:var(--acc,#5a9e8f)':'';
          return '<div class="wv-row wv-svrow" data-name="'+esc(name)+'" style="'+aStyle+'">'+
            (name?'<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(name)+'</span>':
                  '<span style="flex:1;color:var(--dim,#6a6058)">\u25c9 global</span>')+
            (active?'<span style="font-size:8px;color:var(--ok,#8fb87a)">\u25cf</span>':'')+
            '</div>';
        };
        var html=mkRow('',st.activeView==='');
        if(!st.subviews.length){
          html+='<div style="color:var(--dim,#6a6058);padding:4px;font-size:9px">No sub-worldviews yet.</div>';
        }else{
          html+=st.subviews.map(function(s){
            var n=s.name||s;var ds=(s.datasets&&s.datasets.length||0);
            return '<div class="wv-row wv-svrow" data-name="'+esc(n)+'" style="'+(n===st.activeView?'background:rgba(90,158,143,.1);font-weight:600;color:var(--acc,#5a9e8f)':'')+'">'+
              '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(n)+'</span>'+
              '<span style="font-size:8px;color:var(--dim,#6a6058)">'+ds+' ds</span>'+
              (n===st.activeView?'<span style="font-size:8px;color:var(--ok,#8fb87a)">\u25cf</span>':'')+
              '</div>';
          }).join('');
        }
        el.innerHTML=html;
        el.querySelectorAll('.wv-svrow').forEach(function(r){
          r.onclick=function(){activateSubview(r.getAttribute('data-name'));};
        });
      }

      async function activateSubview(name){
        setStatus('Activating '+(name||'global')+'\u2026');
        var res=await api('/worldview/subviews/activate','POST',{name:name||''});
        if(res&&res.error){setStatus(res.error,'err');return;}
        st.activeView=name||'';
        st.activeDatasets=(res&&res.datasets)||[];
        st.lastSnapshot=null; // invalidate — different model now
        _reflectActiveView();
        setStatus('Active: '+(name||'global'),'ok');
        await loadSubviews(true);
        await plotSnapshot(); // auto-map the newly active view
      }

      function _reflectActiveView(){
        var btn=$('.wv-train');
        if(btn)btn.textContent='\u25b6 Train '+(st.activeView?'\u201c'+st.activeView+'\u201d':'global');
        _updateScopeBanner();
      }

      async function createSubview(){
        var name=($('.wv-newname')||{}).value.trim();if(!name){setStatus('Enter a name','err');return;}
        var summ=_activeGraphSummary();if(!summ.nodeCount){setStatus('Current graph is empty','err');return;}
        var safe=name.replace(/[^A-Za-z0-9_]/g,'_');
        setStatus('Saving graph\u2026');
        await api('/fabric/graphs/register','POST',{name:safe,
          description:'vera-graph for worldview "'+name+'"',
          node_labels:(summ.labels.length?summ.labels:['Node']).join(',')});
        setStatus('Creating sub-worldview\u2026');
        var crt=await api('/worldview/subviews/create','POST',{
          name:name,datasets:summ.datasets,graph:safe,node_ids:summ.nodeIds.slice(0,5000)});
        if(crt&&crt.error){setStatus('Create: '+crt.error,'err');return;}
        await api('/worldview/subviews/activate','POST',{name:name});
        st.activeView=name;_reflectActiveView();
        await loadSubviews(true);setStatus('Created \u2014 training\u2026','ok');
        await trainActive();
      }

      // ── UPDATE (incremental) ──────────────────────────────────────────────
      // Calls the same /worldview/train endpoint but with embed_missing:true,
      // skip_trained:true and uses current graph nodes as the scope.
      // The backend only embeds records that don't yet have embeddings, then
      // runs a short fine-tune pass (1/3 of the full epoch counts).
      async function updateActive(targetView){
        var view=targetView!==undefined?targetView:st.activeView;
        var logEl=$('.wv-trainlog'),lossView=$('.wv-lossview');
        if(lossView)lossView.style.display='none';
        if(logEl){logEl.style.display='block';logEl.textContent='';}
        setStatus('Updating \u201c'+(view||'global')+'\u201d\u2026');

        var gnnEl=$('.wv-gnn'),cbEl=$('.wv-codebook'),dynEl=$('.wv-dynamics'),limEl=$('.wv-limit');
        // Use 1/3 of the full epoch counts for incremental updates, min 1
        var gnn     =Math.max(1,Math.floor(parseInt(gnnEl&&gnnEl.value||'20',10)/3));
        var codebook=Math.max(1,Math.floor(parseInt(cbEl&&cbEl.value||'8',10)/3));
        var dynamics=Math.max(1,Math.floor(parseInt(dynEl&&dynEl.value||'15',10)/3));
        var limit   =parseInt(limEl&&limEl.value||'5000',10);
        if(logEl)logEl.textContent='[update] GNN='+gnn+' CB='+codebook+' Dyn='+dynamics+'\nStarting\u2026\n';

        _resetStages({gnn_epochs:gnn,codebook_epochs:codebook,dynamics_epochs:dynamics});
        var bus=(papi&&papi.eventBus),unsub=null;
        if(bus&&bus.subscribe)unsub=bus.subscribe('worldview.progress',_onProgress);
        _startTrainSSE();_startPoll();
        _setTrainingLock(true);

        var activeWas=st.activeView;
        // Temporarily switch active view if targeting a different one
        if(targetView!==undefined && targetView!==st.activeView){
          await api('/worldview/subviews/activate','POST',{name:targetView||''});
        }

        var btn=$('.wv-update');if(btn)btn.disabled=true;
        try{
          var summ=_activeGraphSummary();
          var payload={dataset_id:'',gnn_epochs:gnn,codebook_epochs:codebook,
            dynamics_epochs:dynamics,limit:limit,embed_missing:true,skip_trained:true};
          if(summ.nodeIds.length)payload.node_ids=summ.nodeIds.slice(0,limit||5000);
          var res=await api('/worldview/train','POST',payload,7200000);
          if(res&&res.error){
            setStatus(res.error,'err');if(logEl)logEl.textContent+='[error] '+res.error+'\n';
          }else{
            _stageMeta.gnn.done=_stageMeta.codebook.done=_stageMeta.dynamics.done=true;
            ['gnn','codebook','dynamics'].forEach(_renderStage);
            var dur=res.duration_s?' in '+Math.round(res.duration_s)+'s':'';
            setStatus('Updated "'+(view||'global')+'"'+dur,'ok');
            if(logEl)logEl.textContent+='[done]'+dur+'\n';
            // Restore previous active view if we switched
            if(targetView!==undefined && targetView!==activeWas){
              await api('/worldview/subviews/activate','POST',{name:activeWas||''});
              st.activeView=activeWas;_reflectActiveView();
            }
            await _refreshLastTrained();
            await plotSnapshot();
          }
        }catch(e){setStatus(String(e&&e.message||e),'err');}
        finally{
          _stopTrainSSE();_stopPoll();
          if(unsub)try{unsub();}catch(_){}
          _setTrainingLock(false);
        }
      }


      async function showStats(){
        var el=$('.wv-statsout');if(!el)return;
        el.style.display='block';el.textContent='Loading\u2026';
        el.textContent=JSON.stringify(await api('/worldview/stats'),null,2);
      }

      // ── Wire ──────────────────────────────────────────────────────────────
      // Checkbox rows — click anywhere on row toggles the checkbox
      bodyEl.querySelectorAll('.wv-cbrow').forEach(function(row){
        row.addEventListener('click',function(e){
          if(e.target.tagName==='INPUT')return; // native checkbox handles it
          var cb=row.querySelector('input[type=checkbox]');
          if(cb)cb.checked=!cb.checked;
        });
      });

      $('.wv-plot').onclick        =plotSnapshot;
      $('.wv-clearmap').onclick    =clearMap;
      $('.wv-qrun').onclick        =runQuery;
      $('.wv-arun').onclick        =runAnomalies;
      $('.wv-cload').onclick       =loadConcepts;
      $('.wv-clabel').onclick      =labelConcepts;
      $('.wv-train').onclick       =trainActive;
      $('.wv-update').onclick      =function(){updateActive();};
      $('.wv-update-global').onclick=function(){updateActive('');};
      $('.wv-losshistory').onclick  =fetchLossHistory;
      $('.wv-create').onclick       =createSubview;
      $('.wv-stats').onclick        =showStats;

      // Initial load
      loadSubviews();_refreshGraphInfo();_reflectActiveView();_refreshLastTrained();

      // Wire the Latent chip in the main toolbar to auto-load last snapshot
      // when clicked and no latent map is currently loaded.
      try{
        var _chipContainer=bodyEl.closest('.vg-wrap')||document;
        var _chipObs=new MutationObserver(function(){});
        var _latentChips=_chipContainer.querySelectorAll('.vg-view-chip[data-layout="latent-map"]');
        _latentChips.forEach(function(chip){
          chip.addEventListener('click',function(){
            // If no map is loaded yet, trigger a snapshot fetch
            if(graph.state&&!graph.state._latentMap){
              setTimeout(function(){plotSnapshot();},80);
            }
          });
        });
      }catch(_){}

      // Refresh graph-info on graph changes (filter to non-spammy prefixes)
      var _bus=papi&&papi.eventBus;
      var _autoUpdateTimer=null;
      if(_bus&&_bus.subscribe){
        try{_bus.subscribe('graph.',function(){
          _refreshGraphInfo();
          // Auto-update if enabled (debounced 30s to avoid thrash)
          var cb=$('.wv-autoupdate');
          if(cb&&cb.checked){
            if(_autoUpdateTimer)clearTimeout(_autoUpdateTimer);
            _autoUpdateTimer=setTimeout(function(){
              _autoUpdateTimer=null;
              updateActive();
            },30000);
          }
        });}catch(_){}
      }
    },
  });
})();