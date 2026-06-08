/**
 * vera_graph_panel_loom.js — Loom workbench as a modular sidebar panel
 * ============================================================================
 * Ports the Data-Fabric "Loom Panel" drawer (fabric_panel.html) into the
 * vera_graph.js modular sidebar system, 1:1. Load after vera_graph.js:
 *
 *   <script src="/static/vera_graph.js"></script>
 *   <script src="/static/vera_graph_panel_loom.js"></script>
 *
 * Every graph then gains a "Loom" tab in its left rail. The panel:
 *   • View controls    — pick source (entities / stitched / combined), dataset,
 *                        type filter, include records/datasets → loads the host
 *                        graph instance directly.
 *   • Items list       — Entities / Relations / Edges tabs with search + detail.
 *   • Dataset Config   — save/load per-dataset pipeline config.
 *   • Pipeline stages  — Entity extraction, Loom stitching, Graph extraction,
 *                        AI link analysis — same backend endpoints as the
 *                        original drawer.
 *
 * The panel is fully self-contained: its own state, CSS, and API helper. It
 * drives whichever graph instance it is mounted on (the one passed to mount()),
 * so it works everywhere the graph is embedded.
 * ----------------------------------------------------------------------------
 * Backend endpoints used (unchanged from the original):
 *   GET  /fabric/entity_graph/snapshot
 *   GET  /fabric/graphs/snapshot
 *   POST /fabric/graph/query
 *   POST /fabric/datasets/config
 *   POST /fabric/entity_graph/extract
 *   POST /fabric/loom/run
 *   GET  /fabric/datasets
 *   POST /fabric/browse
 *   POST /mcp/call          (AI link analysis)
 */
(function(){
  'use strict';

  if (!window.veraUI || !window.veraUI.Graph || !window.veraUI.Graph.registerPanel) {
    if (typeof console !== 'undefined') {
      console.warn('vera_graph_panel_loom: veraUI.Graph.registerPanel not found — ' +
                   'load vera_graph.js before this file.');
    }
    return;
  }

  // ── Shared helpers ─────────────────────────────────────────────────────────
  function esc(s){
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function _edgeSwatchCol(rel){
    if (window.veraUI && window.veraUI.Graph && window.veraUI.Graph.edgeColor) {
      try { return window.veraUI.Graph.edgeColor(rel); } catch(e){}
    }
    return 'var(--dim2,#8a7e70)';
  }

  // ── One-time CSS injection (loom-* / ent-* classes) ─────────────────────────
  function _injectCSS(){
    if (document.getElementById('vg-loom-panel-css')) return;
    var s = document.createElement('style');
    s.id = 'vg-loom-panel-css';
    s.textContent = [
      '.lmp .row{display:flex;align-items:center;gap:6px;margin-bottom:4px}',
      '.lmp .row label{font-size:9.5px;color:var(--dim2,#8a7e70);min-width:70px;flex-shrink:0}',
      '.lmp .row input,.lmp .row select{font-size:10px;padding:3px 5px;flex:1;min-width:0;width:auto;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px;font-family:var(--mono,monospace)}',
      '.lmp .row input[type=number]{max-width:80px}',
      '.lmp .row input[type=checkbox]{flex:0;width:auto}',
      '.lmp .status-bar{font-size:10px;margin-top:5px;min-height:14px;color:var(--dim,#6a6058)}',
      '.lmp .status-bar.ok{color:var(--ok,#8fb87a)} .lmp .status-bar.err{color:var(--err,#c96b6b)} .lmp .status-bar.warn{color:var(--acc3,#c9955a)}',
      '.lmp .lbtn{font-size:9px;padding:3px 8px;background:var(--bg2,#272421);border:1px solid var(--border,#3a3530);color:var(--dim2,#8a7e70);border-radius:3px;cursor:pointer;font-family:var(--mono,monospace);transition:.12s}',
      '.lmp .lbtn:hover{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.lmp .lbtn.active,.lmp .lbtn.on{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.lmp .lbtn.primary{background:rgba(90,158,143,.12);border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f)}',
      '.lmp .lbtn.teal{background:rgba(143,184,122,.12);border-color:var(--acc2,#8fb87a);color:var(--acc2,#8fb87a)}',
      // collapsible sections (mimic <details> styling from fabric_panel)
      '.lmp .loom-section{margin-bottom:6px;border:1px solid var(--border,#3a3530);border-radius:4px;background:var(--bg0,#181614);overflow:hidden}',
      '.lmp .loom-section-head{display:flex;align-items:center;gap:6px;padding:6px 8px;cursor:pointer;user-select:none;list-style:none;background:var(--bg1,#1f1d1a);border-bottom:1px solid transparent;transition:.12s}',
      '.lmp .loom-section-head::-webkit-details-marker{display:none}',
      '.lmp .loom-section-head::before{content:"";display:inline-block;width:0;height:0;border-left:5px solid var(--dim,#6a6058);border-top:4px solid transparent;border-bottom:4px solid transparent;transition:transform .15s;flex-shrink:0}',
      '.lmp .loom-section[open] .loom-section-head::before{transform:rotate(90deg)}',
      '.lmp .loom-section[open] .loom-section-head{border-bottom-color:var(--border,#3a3530)}',
      '.lmp .loom-section-head:hover{background:var(--bg2,#272421)}',
      '.lmp .loom-stage-num{width:18px;height:18px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:9.5px;font-weight:700;flex-shrink:0}',
      '.lmp .loom-section-title{font-size:10.5px;font-weight:600;color:var(--text,#ddd5c8);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.lmp .loom-section-sub{font-size:8.5px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;font-weight:500;flex-shrink:0;font-family:var(--mono,monospace)}',
      '.lmp .loom-section-body{padding:7px 9px;background:var(--bg1,#1f1d1a)}',
      '.lmp .loom-sub-head{font-size:8.5px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.6px;font-weight:600;margin:7px 0 4px 0;padding-bottom:2px;border-bottom:1px dotted var(--border,#3a3530)}',
      '.lmp .loom-sub-head:first-child{margin-top:0}',
      '.lmp .loom-check{display:flex;align-items:flex-start;gap:6px;font-size:9.5px;color:var(--dim2,#8a7e70);cursor:pointer;padding:3px 0;line-height:1.35}',
      '.lmp .loom-check input[type=checkbox]{margin:1px 0 0 0;flex:0 0 auto;width:auto}',
      '.lmp .loom-check span{flex:1;min-width:0}',
      '.lmp .loom-check:hover{color:var(--text,#ddd5c8)}',
      '.lmp .loom-unit{font-size:8.5px;color:var(--dim,#6a6058);align-self:center;flex-shrink:0}',
      '.lmp .loom-hint{font-size:9px;color:var(--dim2,#8a7e70);margin-bottom:6px;line-height:1.4;font-style:italic}',
      // list rows
      '.lmp .loom-list-row{padding:5px 7px;border-bottom:1px solid var(--border,#3a3530);cursor:pointer;display:flex;align-items:center;gap:6px;transition:.08s}',
      '.lmp .loom-list-row:hover{background:var(--bg2,#272421);color:var(--acc,#5a9e8f)}',
      '.lmp .loom-list-row .lr-name{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px}',
      '.lmp .loom-list-row .lr-meta{font-size:8.5px;color:var(--dim,#6a6058);font-family:var(--mono,monospace);flex-shrink:0}',
      '.lmp .loom-list-row .lr-edgeswatch{width:14px;height:2px;border-radius:1px;flex-shrink:0}',
      // detail KV
      '.lmp .loom-detail-kv{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border,#3a3530);font-size:10px}',
      '.lmp .loom-detail-kv .k{min-width:80px;color:var(--dim2,#8a7e70);font-size:9px;text-transform:uppercase;letter-spacing:.4px}',
      '.lmp .loom-detail-kv .v{flex:1;color:var(--text,#ddd5c8);word-break:break-word}',
      '.lmp .loom-detail-sec{font-size:9px;color:var(--dim,#6a6058);text-transform:uppercase;letter-spacing:.5px;font-weight:600;margin:8px 0 4px 0}',
      // entity type badges
      '.lmp .ent-type-badge{display:inline-block;font-size:7.5px;padding:1px 5px;border-radius:2px;text-transform:uppercase;letter-spacing:.3px;font-weight:600}',
      '.lmp .ent-tb-person{background:rgba(143,184,122,.15);color:var(--acc2,#8fb87a)}',
      '.lmp .ent-tb-organisation{background:rgba(201,149,90,.15);color:var(--acc3,#c9955a)}',
      '.lmp .ent-tb-technology{background:rgba(90,158,143,.15);color:var(--acc,#5a9e8f)}',
      '.lmp .ent-tb-date,.lmp .ent-tb-year{background:rgba(56,189,248,.12);color:#38bdf8}',
      '.lmp .ent-tb-domain{background:rgba(168,139,250,.12);color:#a78bfa}',
      '.lmp .ent-tb-named_entity{background:rgba(244,114,182,.12);color:#f472b6}',
      '.lmp .ent-tb-class,.lmp .ent-tb-function,.lmp .ent-tb-module{background:rgba(250,204,21,.12);color:#facc15}',
      '.lmp .ent-tb-type,.lmp .ent-tb-type_name,.lmp .ent-tb-constant{background:rgba(100,116,139,.15);color:#94a3b8}',
      '.lmp .ent-tb-entity{background:rgba(201,122,90,.15);color:var(--acc,#c97a5a)}',
      // item detail pop-over
      '.lmp-itemdetail{position:absolute;top:50px;left:14px;width:300px;max-height:calc(100% - 70px);background:var(--bg1,#1f1d1a);border:1px solid var(--acc,#5a9e8f);border-radius:4px;box-shadow:0 4px 16px rgba(0,0,0,.5);z-index:40;overflow-y:auto;display:none}',
      '.lmp-itemdetail-hd{position:sticky;top:0;background:var(--bg1,#1f1d1a);padding:7px 10px;border-bottom:1px solid var(--border,#3a3530);display:flex;align-items:center;gap:6px;z-index:2}',
    ].join('\n');
    document.head.appendChild(s);
  }

  // ── The panel definition ─────────────────────────────────────────────────
  window.veraUI.Graph.registerPanel({
    id:    'loom',
    title: 'Loom',
    icon:  '\u29d6',          // ⧖-ish knot glyph
    order: 10,
    mount: function(bodyEl, graph, papi){
      _injectCSS();
      var apiBase = (papi && papi.apiBase) || (window._veraBase || '');

      // Per-panel state (was global in fabric_panel.html)
      var st = {
        listTab: 'entities',                       // entities | relations | edges
        data:    { entities: [], relations: [], edges: [], _stitchedNodes: null },
        datasets: [],                              // cached dataset list
      };

      // ── API helper (self-contained) ──────────────────────────────────────
      async function api(path, method, payload, timeoutMs){
        var ctrl = new AbortController();
        var to = timeoutMs ? setTimeout(function(){ ctrl.abort(); }, timeoutMs) : null;
        try {
          var opts = { method: method || 'GET', signal: ctrl.signal,
                       headers: { 'Content-Type': 'application/json' } };
          if (payload !== undefined && method && method !== 'GET') {
            opts.body = JSON.stringify(payload);
          }
          var res = await fetch(apiBase + path, opts);
          var data = await res.json();
          return data;
        } catch (e) {
          return { error: String(e && e.message || e) };
        } finally {
          if (to) clearTimeout(to);
        }
      }

      function $(sel){ return bodyEl.querySelector(sel); }
      function setStatus(el, msg, type){
        if (!el) return;
        el.textContent = msg;
        el.className = 'status-bar' + (type ? ' ' + type : '');
      }

      // ── Build the panel markup (the Loom drawer, ported) ──────────────────
      bodyEl.className = (bodyEl.className || '') + ' lmp';
      bodyEl.style.position = 'relative';
      bodyEl.innerHTML =
        // VIEW
        '<details class="loom-section" open>' +
          '<summary class="loom-section-head"><span class="loom-section-title">View</span><span class="loom-section-sub">canvas filter</span></summary>' +
          '<div class="loom-section-body">' +
            '<div class="row"><label>Source</label>' +
              '<select class="lm-viewsrc">' +
                '<option value="entities" selected>2nd-order entities</option>' +
                '<option value="stitched">Stitched edges (Loom)</option>' +
                '<option value="combined">Combined (both)</option>' +
              '</select></div>' +
            '<div class="row"><label>Dataset</label>' +
              '<select class="lm-viewds"><option value="">(all datasets)</option></select></div>' +
            '<div class="row"><label>Type filter</label>' +
              '<select class="lm-typefilter">' +
                '<option value="">All entity types</option>' +
                '<option value="person">Person</option>' +
                '<option value="organisation">Organisation</option>' +
                '<option value="technology">Technology</option>' +
                '<option value="date">Date / Year</option>' +
                '<option value="domain">Domain</option>' +
                '<option value="named_entity">Named entity</option>' +
              '</select></div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-increcords"><span>Include records</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-incdatasets"><span>Include datasets</span></label>' +
            '<button class="lbtn teal lm-refresh" style="width:100%;margin-top:5px">\u21bb Refresh view</button>' +
            '<div class="status-bar lm-viewstat" style="font-size:8.5px"></div>' +
          '</div>' +
        '</details>' +
        // ITEMS
        '<details class="loom-section" open>' +
          '<summary class="loom-section-head"><span class="loom-section-title">Items</span><span class="loom-section-sub lm-listcount"></span></summary>' +
          '<div class="loom-section-body" style="padding:5px 6px">' +
            '<div style="display:flex;gap:3px;margin-bottom:5px">' +
              '<button class="lbtn active lm-tab-ent"   style="flex:1">Entities</button>' +
              '<button class="lbtn lm-tab-rel"   style="flex:1">Relations</button>' +
              '<button class="lbtn lm-tab-edges" style="flex:1">Edges</button>' +
            '</div>' +
            '<input class="lm-listsearch" placeholder="Filter..." style="width:100%;font-size:10px;padding:3px 6px;margin-bottom:5px;background:var(--bg0,#181614);border:1px solid var(--border,#3a3530);color:var(--text,#ddd5c8);border-radius:3px">' +
            '<div class="lm-listcontent" style="max-height:280px;overflow-y:auto;font-size:10px;border:1px solid var(--border,#3a3530);border-radius:3px;background:var(--bg0,#181614)">' +
              '<div style="text-align:center;padding:18px;color:var(--dim,#6a6058);font-size:10px">No data yet.</div>' +
            '</div>' +
          '</div>' +
        '</details>' +
        // DATASET CONFIG TARGET
        '<details class="loom-section" open>' +
          '<summary class="loom-section-head"><span class="loom-section-title">Dataset Config</span><span class="loom-section-sub">target &amp; actions</span></summary>' +
          '<div class="loom-section-body">' +
            '<div class="row"><label>Dataset</label>' +
              '<select class="lm-cfgds"><option value="">Select dataset...</option></select></div>' +
            '<div style="display:flex;gap:4px;margin-top:5px">' +
              '<button class="lbtn primary lm-cfgsave" style="flex:1">Save</button>' +
              '<button class="lbtn lm-cfgload" style="flex:1">Load</button>' +
              '<button class="lbtn teal lm-runpipe" style="flex:1">Run</button>' +
            '</div>' +
            '<div class="status-bar lm-cfgstat"></div>' +
          '</div>' +
        '</details>' +
        // AUTOMATIC TRIGGERS
        '<details class="loom-section">' +
          '<summary class="loom-section-head"><span class="loom-section-title">Automatic Triggers</span><span class="loom-section-sub">on-ingest</span></summary>' +
          '<div class="loom-section-body">' +
            '<div class="loom-hint">Which stages fire automatically when records arrive.</div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-autoExtract"><span>Entity extraction on ingest</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-autoLoom"><span>Loom stitching on ingest</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-autoGraph"><span>Graph extraction on ingest</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-autoSource" checked><span>Auto-register as source</span></label>' +
          '</div>' +
        '</details>' +
        // STAGE 1 — ENTITY EXTRACTION
        '<details class="loom-section">' +
          '<summary class="loom-section-head"><span class="loom-stage-num" style="background:rgba(90,158,143,.15);color:var(--acc,#5a9e8f)">1</span><span class="loom-section-title">Entity Extraction</span><span class="loom-section-sub">NLP / regex</span></summary>' +
          '<div class="loom-section-body">' +
            '<label class="loom-check"><input type="checkbox" class="lm-extract" checked><span>Enable this stage</span></label>' +
            '<div class="loom-sub-head">Source</div>' +
            '<div class="row"><label>Content</label><select class="lm-contentType"><option value="text">Text (articles, docs)</option><option value="code">Code (Python, JS)</option><option value="web">Web pages</option></select></div>' +
            '<div class="row"><label>Max recs</label><input class="lm-extractLimit" type="number" value="500" min="1" max="5000"></div>' +
            '<div class="row"><label>Scope</label><select class="lm-entityScope"><option value="internal">Internal</option><option value="cross">Cross-dataset</option></select></div>' +
            '<div class="row"><label>Persist</label><select class="lm-extractPersist"><option value="true">Write to graph</option><option value="false">Preview only</option></select></div>' +
            '<div class="loom-sub-head">Entity types</div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-entPerson" checked><span>People / titles</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-entOrg" checked><span>Organisations</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-entTech" checked><span>Technologies</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-entDate" checked><span>Dates / years</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-entDomain" checked><span>Domains / URLs</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-entNamed" checked><span>Named (caps phrases)</span></label>' +
            '<div class="loom-sub-head">Tuning</div>' +
            '<div class="row"><label>Min len</label><input class="lm-entMinLen" type="number" value="2" min="1" max="20"><span class="loom-unit">chars</span></div>' +
            '<div class="row"><label>Co-occur</label><input class="lm-cooccurDist" type="number" value="200" min="50" max="1000" step="50"><span class="loom-unit">chars</span></div>' +
            '<div class="row"><label>Max ents/rec</label><input class="lm-maxEntsPerRec" type="number" value="50" min="1" max="500"></div>' +
            '<div class="row"><label>Min mentions</label><input class="lm-minMentions" type="number" value="1" min="1" max="100"></div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-dedupeAcrossDs" checked><span>Deduplicate across datasets</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-normaliseCase" checked><span>Case-normalise names</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-filterStop" checked><span>Filter stop-words / common terms</span></label>' +
          '</div>' +
        '</details>' +
        // STAGE 2 — LOOM STITCHING
        '<details class="loom-section">' +
          '<summary class="loom-section-head"><span class="loom-stage-num" style="background:rgba(143,184,122,.15);color:var(--acc2,#8fb87a)">2</span><span class="loom-section-title">Record Stitching (Loom)</span><span class="loom-section-sub">text similarity</span></summary>' +
          '<div class="loom-section-body">' +
            '<label class="loom-check"><input type="checkbox" class="lm-loom"><span>Enable this stage</span></label>' +
            '<div class="loom-sub-head">Matching</div>' +
            '<div class="row"><label>Mode</label><select class="lm-mode"><option value="hybrid">Hybrid (Jaccard)</option><option value="entity">Entity (keyword)</option><option value="semantic">Semantic (overlap)</option><option value="tag">Tag overlap</option></select></div>' +
            '<div class="row"><label>Min score</label><input class="lm-minScore" type="number" value="0.4" min="0" max="1" step="0.05"></div>' +
            '<div class="row"><label>Max matches</label><input class="lm-maxMatches" type="number" value="100" min="1" max="2000"></div>' +
            '<div class="row"><label>Scope</label><select class="lm-loomScope"><option value="internal">Internal</option><option value="cross">Cross-dataset</option></select></div>' +
            '<div class="loom-sub-head">Edge classification</div>' +
            '<div class="row"><label>Edge type</label><select class="lm-edgeType"><option value="auto">Auto-classify</option><option value="RELATED_TO">RELATED_TO</option><option value="SIMILAR_TO">SIMILAR_TO</option><option value="REFERENCES">REFERENCES</option><option value="DEPENDS_ON">DEPENDS_ON</option><option value="DERIVED_FROM">DERIVED_FROM</option><option value="SHARES_TOPIC">SHARES_TOPIC</option></select></div>' +
            '<div class="row"><label>Target graph</label><select class="lm-targetGraph"><option value="fabric">fabric (default)</option><option value="memory">memory</option><option value="net">net (network)</option></select></div>' +
            '<div class="loom-sub-head">Filtering</div>' +
            '<div class="row"><label>Tag filter</label><input class="lm-tagFilter" placeholder="e.g. security, threat"></div>' +
            '<div class="row"><label>Min text len</label><input class="lm-minTextLen" type="number" value="40" min="10" max="500"><span class="loom-unit">chars</span></div>' +
            '<div class="row"><label>Batch size</label><input class="lm-batchSize" type="number" value="200" min="10" max="1000"></div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-persist" checked><span>Persist edges to graph</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-onlyNew"><span>Only newly ingested records</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-skipSelf" checked><span>Skip self-matches</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-dedupeEdges" checked><span>Deduplicate edges (idempotent)</span></label>' +
          '</div>' +
        '</details>' +
        // STAGE 3 — GRAPH EXTRACTION
        '<details class="loom-section">' +
          '<summary class="loom-section-head"><span class="loom-stage-num" style="background:rgba(201,149,90,.15);color:var(--acc3,#c9955a)">3</span><span class="loom-section-title">Graph Extraction</span><span class="loom-section-sub">relationship discovery</span></summary>' +
          '<div class="loom-section-body">' +
            '<label class="loom-check"><input type="checkbox" class="lm-graphExtract"><span>Enable this stage</span></label>' +
            '<div class="loom-sub-head">Engine</div>' +
            '<div class="row"><label>Mode</label><select class="lm-graphMode"><option value="nlp">NLP (fast, regex)</option><option value="llm">LLM (deep, slow)</option><option value="hybrid">Hybrid (NLP + LLM)</option></select></div>' +
            '<div class="row"><label>Limit</label><input class="lm-graphLimit" type="number" value="100" min="1" max="1000"><span class="loom-unit">records</span></div>' +
            '<div class="row"><label>LLM model</label><select class="lm-graphLlmModel"><option value="auto">Auto (cluster default)</option><option value="llama3:8b">llama3:8b (CPU)</option><option value="llama3:70b">llama3:70b (GPU)</option><option value="mixtral">mixtral</option></select></div>' +
            '<div class="row"><label>Temp</label><input class="lm-graphTemp" type="number" value="0.2" min="0" max="2" step="0.1"></div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-graphPersist" checked><span>Write to graph</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-graphInferTypes" checked><span>Infer relationship types</span></label>' +
          '</div>' +
        '</details>' +
        // STAGE 4 — AI LINK ANALYSIS
        '<details class="loom-section">' +
          '<summary class="loom-section-head"><span class="loom-stage-num" style="background:rgba(158,143,160,.15);color:var(--acc4,#9e8fa0)">4</span><span class="loom-section-title">AI Link Analysis</span><span class="loom-section-sub">LLM-driven</span></summary>' +
          '<div class="loom-section-body">' +
            '<label class="loom-check"><input type="checkbox" class="lm-aiAnalyse"><span>Enable this stage</span></label>' +
            '<div class="row"><label>Max pairs</label><input class="lm-aiPairs" type="number" value="8" min="1" max="30"></div>' +
            '<div class="row"><label>Min score</label><input class="lm-aiMinScore" type="number" value="0.5" min="0" max="1" step="0.1"></div>' +
            '<div class="row"><label>Strategy</label><select class="lm-aiStrategy"><option value="bridge">Bridge weak clusters</option><option value="dense">Densify connections</option><option value="explore">Explore unconnected</option></select></div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-aiAutoStitch"><span>Auto-stitch suggestions</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-aiExplain" checked><span>Include explanations</span></label>' +
          '</div>' +
        '</details>' +
        // PIPELINE LOG
        '<details class="loom-section">' +
          '<summary class="loom-section-head">'
            + '<span class="loom-section-title">NER Backend</span>'
            + '<span class="loom-section-sub lm-ner-active"></span>'
          + '</summary>' +
          '<div class="loom-section-body">' +
            '<div style="font-size:8.5px;color:var(--dim2);margin-bottom:5px">Entity extraction backend for all pipeline stages.</div>' +
            '<div style="display:flex;gap:4px;margin-bottom:4px;flex-wrap:wrap">'
              + '<select class="lm-ner-backend" style="font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'
                + '<option value="auto">Auto (best available)</option>'
                + '<option value="gliner">GLiNER</option>'
                + '<option value="spacy">spaCy</option>'
                + '<option value="heuristic">Heuristic only</option>'
              + '</select>'
              + '<button class="lbtn lm-ner-apply" style="font-size:8.5px">Apply</button>'
              + '<button class="lbtn lm-ner-status" style="font-size:8.5px">Status</button>'
            + '</div>' +
            '<div style="display:flex;gap:4px;margin-bottom:4px">'
              + '<input class="lm-ner-model" placeholder="Model override (e.g. en_core_web_trf or urchade/gliner_large)"'
                + ' style="flex:1;font-size:8.5px;padding:2px 5px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'
            + '</div>' +
            '<div style="display:flex;gap:4px;margin-bottom:3px;flex-wrap:wrap">'
              + '<select class="lm-ner-install-pkg" style="font-size:8.5px;padding:2px 4px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'
                + '<option value="gliner">gliner (pip)</option>'
                + '<option value="spacy">spacy (pip)</option>'
                + '<option value="">custom pkg below</option>'
              + '</select>'
              + '<input class="lm-ner-spmodel" placeholder="spaCy model (e.g. en_core_web_sm)"'
                + ' style="flex:1;min-width:80px;font-size:8.5px;padding:2px 5px;background:var(--bg0);border:1px solid var(--border2);color:var(--text);border-radius:3px">'
              + '<button class="lbtn lm-ner-install" style="font-size:8.5px">Install</button>'
            + '</div>' +
            '<div class="status-bar lm-ner-st"></div>' +
          '</div>' +
        '</details>' +

        '<details class="loom-section lm-loglog" style="display:none">' +
          '<summary class="loom-section-head"><span class="loom-section-title">Pipeline Log</span><span class="loom-section-sub">execution output</span></summary>' +
          '<div class="loom-section-body">' +
            '<div class="lm-logcontent" style="font-size:9.5px;color:var(--dim,#6a6058);min-height:30px;max-height:240px;overflow-y:auto;font-family:var(--mono,monospace);line-height:1.55;background:var(--bg0,#181614);padding:5px;border-radius:3px"></div>' +
            '<button class="lbtn lm-logclear" style="margin-top:6px;width:100%;font-size:9px">Clear log</button>' +
          '</div>' +
        '</details>' +
        // ITEM DETAIL POP-OVER
        '<div class="lmp-itemdetail lm-itemdetail">' +
          '<div class="lmp-itemdetail-hd"><span class="lm-itemtitle" style="flex:1;font-size:11px;font-weight:600;color:var(--acc,#5a9e8f);overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>' +
          '<button class="lbtn lm-itemclose" style="font-size:13px;padding:0 7px;line-height:1.2">\u00d7</button></div>' +
          '<div class="lm-itembody" style="padding:8px 10px;font-size:10px"></div>' +
        '</div>';

      // ── Element refs ──────────────────────────────────────────────────────
      var elViewSrc   = $('.lm-viewsrc'),    elViewDs    = $('.lm-viewds');
      var elTypeFilt  = $('.lm-typefilter'), elIncRec    = $('.lm-increcords');
      var elIncDs     = $('.lm-incdatasets'),elViewStat  = $('.lm-viewstat');
      var elListCount = $('.lm-listcount'),  elListSearch= $('.lm-listsearch');
      var elListCont  = $('.lm-listcontent');
      var elCfgDs     = $('.lm-cfgds'),      elCfgStat   = $('.lm-cfgstat');
      var elItemDet   = $('.lm-itemdetail'), elItemTitle = $('.lm-itemtitle');
      var elItemBody  = $('.lm-itembody');
      var elLogWrap   = $('.lm-loglog'),     elLogContent= $('.lm-logcontent');

      // ── Dataset list population ───────────────────────────────────────────
      async function populateDatasets(){
        var res = await api('/fabric/datasets');
        st.datasets = (res && res.datasets) || [];
        // Also pull discovery history datasets
        var dres = await api('/fabric/discover/history');
        var discCrawls = (dres && dres.crawls) || [];
        var discDs = {}; // dedup by dataset_id
        discCrawls.forEach(function(c){
          if (c.dataset_id && !discDs[c.dataset_id])
            discDs[c.dataset_id] = {dataset_id: c.dataset_id,
              record_count: c.pages_fetched || '?',
              topic: c.topic || '', _discovery: true};
        });
        // Merge: fabric_datasets take priority, add discovery-only ones
        var allDs = st.datasets.slice();
        Object.keys(discDs).forEach(function(did){
          if (!allDs.some(function(d){ return d.dataset_id === did; }))
            allDs.push(discDs[did]);
        });
        var opts = '<option value="">(all datasets)</option>';
        var cfgOpts = '<option value="">Select dataset...</option>';
        var discSep = false;
        allDs.forEach(function(d){
          var lbl = esc(d.dataset_id) + (d.topic ? ' ['+esc(d.topic.slice(0,30))+']' : '') +
                    ' (' + (d.record_count || '?') + ')';
          if (d._discovery && !discSep) {
            opts += '<option disabled>― Discovery datasets ―</option>';
            discSep = true;
          }
          opts    += '<option value="' + esc(d.dataset_id) + '">' + lbl + '</option>';
          cfgOpts += '<option value="' + esc(d.dataset_id) + '">' + lbl + '</option>';
        });
        if (elViewDs) { var v = elViewDs.value; elViewDs.innerHTML = opts; elViewDs.value = v; }
        if (elCfgDs)  { var c = elCfgDs.value;  elCfgDs.innerHTML  = cfgOpts; elCfgDs.value = c; }
      }

      // ── View refresh — fetch entities / stitched edges, drive the graph ───
      async function refreshView(){
        var dsId = elViewDs ? elViewDs.value : '';
        var src  = elViewSrc ? elViewSrc.value : 'entities';
        var typeFilter = elTypeFilt ? elTypeFilt.value : '';
        var includeRecords  = !!(elIncRec && elIncRec.checked);
        var includeDatasets = !!(elIncDs && elIncDs.checked);
        if (!dsId) includeDatasets = true;

        st.data = { entities: [], relations: [], edges: [], _stitchedNodes: null };

        if (src === 'entities' || src === 'combined') {
          var qs = '?limit=500';
          if (dsId) qs += '&dataset_id=' + encodeURIComponent(dsId);
          if (typeFilter) qs += '&entity_type=' + encodeURIComponent(typeFilter);
          if (includeRecords) qs += '&include_records=1';
          if (includeDatasets) qs += '&include_datasets=1';
          var entRes = await api('/fabric/entity_graph/snapshot' + qs);
          if (entRes && entRes.nodes) {
            st.data.entities = entRes.nodes.map(function(n){
              return { id: n.id,
                       name: n.name || (n.props && n.props.title) || (n.props && n.props.url) || n.id,
                       type: n.type || (n.labels && n.labels[0]) || 'entity',
                       mention_count: (n.props && (n.props.mention_count || n.props.count)) || 1,
                       props: n.props || {} };
            });
          }
          if (entRes && entRes.edges) {
            st.data.relations = entRes.edges.map(function(e){
              return { from: e.from, to: e.to,
                       from_name: e.from_name || e.from, to_name: e.to_name || e.to,
                       rel: e.rel || 'REL', distance: e.props && e.props.distance,
                       props: e.props || {} };
            });
          }
        }
        if (src === 'stitched' || src === 'combined') {
          var qs2 = '?graph=fabric&limit=500';
          if (dsId) qs2 += '&dataset_id=' + encodeURIComponent(dsId);
          var snapRes = await api('/fabric/graphs/snapshot' + qs2);
          var stitchRels = ['RELATED_TO','LINKS_TO','SIMILAR_TO','REFERENCES','DEPENDS_ON','DERIVED_FROM','SHARES_TOPIC'];
          if (snapRes && snapRes.edges) {
            st.data.edges = snapRes.edges.filter(function(e){ return stitchRels.indexOf(e.rel) >= 0; });
          }
          if (src === 'stitched' && snapRes && snapRes.nodes) {
            st.data._stitchedNodes = snapRes.nodes;
          }
        }

        // stats line
        if (elViewStat) {
          var parts = [];
          if (st.data.entities.length) parts.push(st.data.entities.length + ' ent');
          if (st.data.relations.length) parts.push(st.data.relations.length + ' rel');
          if (st.data.edges.length) parts.push(st.data.edges.length + ' edges');
          elViewStat.textContent = parts.length ? parts.join(' \u00b7 ') : 'empty';
        }
        renderList();
        await loadGraph(src, dsId);
      }

      // ── Push the current data into the host graph instance ────────────────
      async function loadGraph(src, dsId){
        var nodes = [], edges = [], nodeMap = {};
        function _addNode(id, name, type, props){
          if (!id || nodeMap[id]) return;
          nodeMap[id] = true;
          nodes.push({ id: id, label: String(name || id || '').slice(0,40),
                       type: type || 'Node', props: props || {} });
        }
        if (src === 'entities' || src === 'combined') {
          st.data.entities.forEach(function(e){ _addNode(e.id, e.name, e.type || 'Entity', e.props); });
          st.data.relations.forEach(function(r){
            if (r.from && r.to) edges.push({ from: r.from, to: r.to, rel: r.rel || 'RELATED_TO', props: r.props || {} });
          });
        }
        if (src === 'stitched' || src === 'combined') {
          if (st.data._stitchedNodes) {
            st.data._stitchedNodes.forEach(function(n){
              _addNode(n.id, n.name || (n.props && n.props.title) || n.id,
                       (n.labels && n.labels[0]) || n.type || 'FabricRecord', n.props || {});
            });
          }
          st.data.edges.forEach(function(e){
            if (!nodeMap[e.from]) _addNode(e.from, e.from, 'FabricRecord', {});
            if (!nodeMap[e.to])   _addNode(e.to,   e.to,   'FabricRecord', {});
            edges.push({ from: e.from, to: e.to, rel: e.rel || 'RELATED_TO', props: e.props || {} });
          });
        }

        // Enrich record nodes with friendly names via /fabric/browse
        var recordNodes = nodes.filter(function(n){
          return (n.type === 'FabricRecord' || n.type === 'Record') && n.label === n.id;
        });
        if (recordNodes.length) {
          var dsIds = {};
          recordNodes.forEach(function(n){
            var did = (n.props && n.props.dataset_id) || dsId || '';
            if (did) dsIds[did] = true;
          });
          var dsIdList = Object.keys(dsIds);
          if (!dsIdList.length) dsIdList = st.datasets.map(function(d){ return d.dataset_id; });
          var browseMap = {};
          for (var di = 0; di < dsIdList.length; di++) {
            var bRes = await api('/fabric/browse', 'POST',
              { dataset_id: dsIdList[di], limit: 300, offset: 0, search: '', lite: false }, 15000);
            if (bRes && bRes.records) {
              bRes.records.forEach(function(r){
                var t = r.title || r.name || (r.data && (r.data.title || r.data.name)) || (r.text||'').slice(0,80) || r.id;
                var u = r.url || r.link || (r.data && (r.data.url || r.data.link)) || '';
                browseMap[r.id] = { title: t, url: u };
              });
            }
          }
          nodes.forEach(function(n){
            if ((n.type === 'FabricRecord' || n.type === 'Record') && n.label === n.id) {
              var en = browseMap[n.id];
              if (en) {
                n.label = String(en.title || en.url || n.id).slice(0,50);
                if (en.title) n.props.title = en.title;
                if (en.url) n.props.url = en.url;
              }
            }
          });
        }

        // Drive the host graph. Use the panel's own edge style so stitched
        // edges get the right dash/spring treatment.
        if (graph && graph.load) {
          graph.load({ nodes: nodes, edges: edges });
          // Open the panel so the user sees its controls alongside the graph.
          if (papi && papi.isActive && !papi.isActive() && papi.activate) papi.activate();
        }
      }

      // ── Items list rendering ──────────────────────────────────────────────
      function renderList(){
        if (!elListCont) return;
        var filter = (elListSearch && elListSearch.value || '').toLowerCase();
        var rows = [];
        if (st.listTab === 'entities') {
          rows = (st.data.entities || []).filter(function(e){
            return !filter || ((e.name||'')+' '+(e.type||'')).toLowerCase().indexOf(filter) >= 0;
          });
          if (elListCount) elListCount.textContent = rows.length + '/' + (st.data.entities||[]).length;
          elListCont.innerHTML = rows.length ? rows.slice(0,500).map(function(e, i){
            var tname = e.type || 'entity';
            return '<div class="loom-list-row" data-kind="entity" data-idx="' + i + '" title="' + esc(e.name) + '">' +
              '<span class="ent-type-badge ent-tb-' + esc(tname) + '">' + esc(tname.slice(0,4)) + '</span>' +
              '<span class="lr-name">' + esc(e.name || '(unnamed)') + '</span>' +
              '<span class="lr-meta">\u00d7' + (e.mention_count || e.count || 1) + '</span></div>';
          }).join('') : '<div style="text-align:center;padding:20px;color:var(--dim,#6a6058);font-size:10px">No entities. Run extraction.</div>';
        } else if (st.listTab === 'relations') {
          rows = (st.data.relations || []).filter(function(r){
            return !filter || ((r.from_name||'')+' '+(r.to_name||'')+' '+(r.rel||'')).toLowerCase().indexOf(filter) >= 0;
          });
          if (elListCount) elListCount.textContent = rows.length + '/' + (st.data.relations||[]).length;
          elListCont.innerHTML = rows.length ? rows.slice(0,500).map(function(r, i){
            var col = _edgeSwatchCol(r.rel);
            return '<div class="loom-list-row" data-kind="relation" data-idx="' + i + '" title="' + esc((r.from_name||'?')+' \u2192 '+(r.to_name||'?')) + '">' +
              '<span class="lr-edgeswatch" style="background:' + col + '"></span>' +
              '<span class="lr-name"><b>' + esc(r.from_name||'?') + '</b> <span style="color:var(--dim,#6a6058);font-size:9px">' + esc(r.rel||'REL') + '</span> ' + esc(r.to_name||'?') + '</span>' +
              '<span class="lr-meta">' + (r.distance ? 'd'+r.distance : '') + '</span></div>';
          }).join('') : '<div style="text-align:center;padding:20px;color:var(--dim,#6a6058);font-size:10px">No relations. Run graph extraction.</div>';
        } else {
          rows = (st.data.edges || []).filter(function(e){
            return !filter || ((e.from||'')+' '+(e.to||'')+' '+(e.rel||'')).toLowerCase().indexOf(filter) >= 0;
          });
          if (elListCount) elListCount.textContent = rows.length + '/' + (st.data.edges||[]).length;
          elListCont.innerHTML = rows.length ? rows.slice(0,500).map(function(e, i){
            var score = (e.props && (e.props.score || e.props.weight)) || '';
            var col = _edgeSwatchCol(e.rel);
            return '<div class="loom-list-row" data-kind="edge" data-idx="' + i + '" title="' + esc((e.from||'')+' \u2192 '+(e.to||'')) + '">' +
              '<span class="lr-edgeswatch" style="background:' + col + '"></span>' +
              '<span class="lr-name"><span style="color:var(--dim,#6a6058);font-size:9px;font-family:var(--mono,monospace)">' + esc((e.from||'').slice(0,12)) + '</span> <span style="color:' + col + ';font-size:9px;font-weight:600">' + esc(e.rel||'EDGE') + '</span> <span style="color:var(--dim,#6a6058);font-size:9px;font-family:var(--mono,monospace)">' + esc((e.to||'').slice(0,12)) + '</span></span>' +
              (score ? '<span class="lr-meta">' + (typeof score === 'number' ? score.toFixed(2) : esc(String(score))) + '</span>' : '') + '</div>';
          }).join('') : '<div style="text-align:center;padding:20px;color:var(--dim,#6a6058);font-size:10px">No edges. Run Loom stitching.</div>';
        }
      }

      function setListTab(tab){
        st.listTab = tab;
        $('.lm-tab-ent').classList.toggle('active', tab === 'entities');
        $('.lm-tab-rel').classList.toggle('active', tab === 'relations');
        $('.lm-tab-edges').classList.toggle('active', tab === 'edges');
        renderList();
      }

      // ── Item detail pop-over ──────────────────────────────────────────────
      function _kv(k, v){
        return '<div class="loom-detail-kv"><div class="k">' + esc(k) + '</div><div class="v">' +
               (v == null ? '<span style="color:var(--dim,#6a6058)">\u2014</span>' : v) + '</div></div>';
      }
      function showItemDetail(kind, idx){
        if (!elItemDet) return;
        elItemDet.style.display = 'block';
        var html = '';
        if (kind === 'entity') {
          var e = st.data.entities[idx]; if (!e) { elItemDet.style.display='none'; return; }
          elItemTitle.textContent = e.name || '(unnamed)';
          html += _kv('Type', '<span class="ent-type-badge ent-tb-' + esc(e.type||'entity') + '">' + esc(e.type||'entity') + '</span>');
          html += _kv('Mentions', String(e.mention_count || 1));
          html += _kv('ID', '<code style="font-size:9px;color:var(--dim2,#8a7e70)">' + esc(e.id||'') + '</code>');
          var props = e.props || {};
          var pk = Object.keys(props).filter(function(k){ return k!=='mention_count' && k!=='count'; });
          if (pk.length) { html += '<div class="loom-detail-sec">Properties</div>';
            pk.forEach(function(k){ var v = props[k];
              v = (typeof v === 'object') ? '<code style="font-size:9px">' + esc(JSON.stringify(v).slice(0,200)) + '</code>' : esc(String(v).slice(0,300));
              html += _kv(k, v); }); }
          html += '<div class="loom-detail-sec">Actions</div><div style="display:flex;gap:4px;flex-wrap:wrap">' +
                  '<button class="lbtn lm-act-focus" data-id="' + esc(e.id||'') + '">Focus in graph</button>' +
                  '<button class="lbtn lm-act-mentions" data-id="' + esc(e.id||'') + '">Load mentions</button>' +
                  '<button class="lbtn lm-act-related" data-id="' + esc(e.id||'') + '">Load related</button></div>' +
                  '<div class="loom-detail-sec">Records mentioning</div>' +
                  '<div class="lm-itemreclist" style="font-size:10px;color:var(--dim2,#8a7e70)">Click "Load mentions" to fetch.</div>';
        } else if (kind === 'relation') {
          var r = st.data.relations[idx]; if (!r) { elItemDet.style.display='none'; return; }
          elItemTitle.textContent = (r.from_name||'?') + ' \u2192 ' + (r.to_name||'?');
          var col = _edgeSwatchCol(r.rel);
          html += _kv('Type', '<span style="color:' + col + ';font-weight:600">' + esc(r.rel||'REL') + '</span>');
          html += _kv('From', esc(r.from_name||'?'));
          html += _kv('To', esc(r.to_name||'?'));
          if (r.distance) html += _kv('Distance', String(r.distance) + ' chars');
          var rp = r.props || {}; var rpk = Object.keys(rp);
          if (rpk.length) { html += '<div class="loom-detail-sec">Properties</div>';
            rpk.forEach(function(k){ html += _kv(k, esc(String(rp[k]).slice(0,200))); }); }
        } else if (kind === 'edge') {
          var ed = st.data.edges[idx]; if (!ed) { elItemDet.style.display='none'; return; }
          elItemTitle.textContent = 'Loom edge';
          var ecol = _edgeSwatchCol(ed.rel);
          html += _kv('Type', '<span style="color:' + ecol + ';font-weight:600">' + esc(ed.rel||'EDGE') + '</span>');
          html += _kv('From', '<code style="font-size:9px">' + esc(ed.from||'') + '</code>');
          html += _kv('To', '<code style="font-size:9px">' + esc(ed.to||'') + '</code>');
          var ep = ed.props || {}; var epk = Object.keys(ep);
          if (epk.length) { html += '<div class="loom-detail-sec">Properties</div>';
            epk.forEach(function(k){ var v = ep[k];
              if (typeof v === 'number' && v.toFixed) v = v.toFixed(3);
              html += _kv(k, esc(String(v).slice(0,200))); }); }
          html += '<div class="loom-detail-sec">Actions</div><div style="display:flex;gap:4px;flex-wrap:wrap">' +
                  '<button class="lbtn lm-act-focus" data-id="' + esc(ed.from||'') + '">Focus source</button>' +
                  '<button class="lbtn lm-act-focus" data-id="' + esc(ed.to||'') + '">Focus target</button></div>';
        }
        elItemBody.innerHTML = html;
      }

      function focusInGraph(id){ if (graph && graph.focusNode) graph.focusNode(id); }

      async function loadEntityMentions(eid){
        var listEl = elItemBody.querySelector('.lm-itemreclist'); if (!listEl) return;
        listEl.innerHTML = '<span style="color:var(--dim,#6a6058)">Loading\u2026</span>';
        var res = await api('/fabric/graph/query', 'POST', {
          cypher: 'MATCH (e:Entity {id:$eid})-[:MENTIONED_IN|HAS_ENTITY]-(r:FabricRecord) RETURN r LIMIT 50',
          params: { eid: eid }
        });
        var nodes = (res && res.nodes) || [];
        listEl.innerHTML = nodes.length ? nodes.map(function(n){
          var t = (n.props && (n.props.title || n.props.name || n.props.url)) || n.id;
          return '<div class="loom-list-row" style="padding:3px 5px"><span class="lr-name">' + esc(String(t).slice(0,80)) + '</span></div>';
        }).join('') : '<span style="color:var(--dim,#6a6058)">No mentions found.</span>';
      }
      async function loadEntityRelated(eid){
        var listEl = elItemBody.querySelector('.lm-itemreclist'); if (!listEl) return;
        listEl.innerHTML = '<span style="color:var(--dim,#6a6058)">Loading related entities\u2026</span>';
        var res = await api('/fabric/graph/query', 'POST', {
          cypher: 'MATCH (e:Entity {id:$eid})-[r:CO_OCCURS|RELATED_TO|SIMILAR_TO]-(e2:Entity) RETURN e2, r LIMIT 30',
          params: { eid: eid }
        });
        var nodes = (res && res.nodes) || [];
        listEl.innerHTML = nodes.length ? nodes.map(function(n){
          var t = n.name || (n.props && n.props.name) || n.id;
          var tp = n.type || (n.labels && n.labels[0]) || 'entity';
          return '<div class="loom-list-row" style="padding:3px 5px"><span class="ent-type-badge ent-tb-' + esc(tp) + '">' + esc(tp.slice(0,4)) + '</span><span class="lr-name">' + esc(String(t).slice(0,80)) + '</span></div>';
        }).join('') : '<span style="color:var(--dim,#6a6058)">No related entities.</span>';
      }

      // ── Pipeline config save / load ───────────────────────────────────────
      function gatherCfg(){
        function ck(c){ var e = $(c); return !!(e && e.checked); }
        function vv(c, d){ var e = $(c); return (e && e.value) || d; }
        function iv(c, d){ var e = $(c); return parseInt((e && e.value) || d); }
        function fv(c, d){ var e = $(c); return parseFloat((e && e.value) || d); }
        return {
          auto_extract_on_ingest: ck('.lm-autoExtract'),
          auto_loom_on_ingest:    ck('.lm-autoLoom'),
          auto_graph_on_ingest:   ck('.lm-autoGraph'),
          auto_register_source:   ck('.lm-autoSource'),
          auto_extract_entities:  ck('.lm-extract'),
          content_type:           vv('.lm-contentType','text'),
          extract_limit:          iv('.lm-extractLimit','500'),
          entity_scope:           vv('.lm-entityScope','internal'),
          extract_persist:        vv('.lm-extractPersist','true') !== 'false',
          ent_types: {
            person:       ck('.lm-entPerson'),  organisation: ck('.lm-entOrg'),
            technology:   ck('.lm-entTech'),    date:         ck('.lm-entDate'),
            domain:       ck('.lm-entDomain'),  named_entity: ck('.lm-entNamed'),
          },
          ent_min_len:       iv('.lm-entMinLen','2'),
          cooccur_distance:  iv('.lm-cooccurDist','200'),
          max_ents_per_rec:  iv('.lm-maxEntsPerRec','50'),
          min_mentions:      iv('.lm-minMentions','1'),
          dedupe_across_ds:  ck('.lm-dedupeAcrossDs'),
          normalise_case:    ck('.lm-normaliseCase'),
          filter_stop_words: ck('.lm-filterStop'),
          auto_loom:         ck('.lm-loom'),
          loom_mode:         vv('.lm-mode','hybrid'),
          loom_min_score:    fv('.lm-minScore','0.4'),
          loom_max_matches:  iv('.lm-maxMatches','100'),
          loom_scope:        vv('.lm-loomScope','internal'),
          loom_edge_type:    vv('.lm-edgeType','auto'),
          loom_target_graph: vv('.lm-targetGraph','fabric'),
          loom_tag_filter:   vv('.lm-tagFilter',''),
          loom_persist:      ck('.lm-persist'),
          loom_only_new:     ck('.lm-onlyNew'),
          loom_min_text_len: iv('.lm-minTextLen','40'),
          loom_batch_size:   iv('.lm-batchSize','200'),
          loom_skip_self:    ck('.lm-skipSelf'),
          loom_dedupe_edges: ck('.lm-dedupeEdges'),
          graph_extract:         ck('.lm-graphExtract'),
          graph_extract_mode:    vv('.lm-graphMode','nlp'),
          graph_extract_limit:   iv('.lm-graphLimit','100'),
          graph_extract_persist: ck('.lm-graphPersist'),
          graph_llm_model:       vv('.lm-graphLlmModel','auto'),
          graph_temp:            fv('.lm-graphTemp','0.2'),
          graph_infer_types:     ck('.lm-graphInferTypes'),
          ai_analyse:     ck('.lm-aiAnalyse'),
          ai_max_pairs:   iv('.lm-aiPairs','8'),
          ai_min_score:   fv('.lm-aiMinScore','0.5'),
          ai_auto_stitch: ck('.lm-aiAutoStitch'),
          ai_strategy:    vv('.lm-aiStrategy','bridge'),
          ai_explain:     ck('.lm-aiExplain'),
        };
      }
      function applyCfg(c){
        function setck(s, on){ var e = $(s); if (e) e.checked = on; }
        function setv(s, v){ var e = $(s); if (e) e.value = v; }
        setck('.lm-autoExtract', !!c.auto_extract_on_ingest);
        setck('.lm-autoLoom', !!c.auto_loom_on_ingest);
        setck('.lm-autoGraph', !!c.auto_graph_on_ingest);
        setck('.lm-autoSource', c.auto_register_source !== false);
        setck('.lm-extract', c.auto_extract_entities !== false);
        setv('.lm-contentType', c.content_type || 'text');
        setv('.lm-extractLimit', c.extract_limit || 500);
        setv('.lm-entityScope', c.entity_scope || 'internal');
        setv('.lm-extractPersist', c.extract_persist === false ? 'false' : 'true');
        var et = c.ent_types || {};
        setck('.lm-entPerson', et.person !== false);
        setck('.lm-entOrg', et.organisation !== false);
        setck('.lm-entTech', et.technology !== false);
        setck('.lm-entDate', et.date !== false);
        setck('.lm-entDomain', et.domain !== false);
        setck('.lm-entNamed', et.named_entity !== false);
        setv('.lm-entMinLen', c.ent_min_len || 2);
        setv('.lm-cooccurDist', c.cooccur_distance || 200);
        setv('.lm-maxEntsPerRec', c.max_ents_per_rec || 50);
        setv('.lm-minMentions', c.min_mentions || 1);
        setck('.lm-dedupeAcrossDs', c.dedupe_across_ds !== false);
        setck('.lm-normaliseCase', c.normalise_case !== false);
        setck('.lm-filterStop', c.filter_stop_words !== false);
        setck('.lm-loom', !!c.auto_loom);
        setv('.lm-mode', c.loom_mode || 'hybrid');
        setv('.lm-minScore', c.loom_min_score || 0.4);
        setv('.lm-maxMatches', c.loom_max_matches || 100);
        setv('.lm-loomScope', c.loom_scope || 'internal');
        setv('.lm-edgeType', c.loom_edge_type || 'auto');
        setv('.lm-targetGraph', c.loom_target_graph || 'fabric');
        setv('.lm-tagFilter', c.loom_tag_filter || '');
        setck('.lm-persist', c.loom_persist !== false);
        setck('.lm-onlyNew', !!c.loom_only_new);
        setv('.lm-minTextLen', c.loom_min_text_len || 40);
        setv('.lm-batchSize', c.loom_batch_size || 200);
        setck('.lm-skipSelf', c.loom_skip_self !== false);
        setck('.lm-dedupeEdges', c.loom_dedupe_edges !== false);
        setck('.lm-graphExtract', !!c.graph_extract);
        setv('.lm-graphMode', c.graph_extract_mode || 'nlp');
        setv('.lm-graphLimit', c.graph_extract_limit || 100);
        setck('.lm-graphPersist', c.graph_extract_persist !== false);
        setv('.lm-graphLlmModel', c.graph_llm_model || 'auto');
        setv('.lm-graphTemp', c.graph_temp || 0.2);
        setck('.lm-graphInferTypes', c.graph_infer_types !== false);
        setck('.lm-aiAnalyse', !!c.ai_analyse);
        setv('.lm-aiPairs', c.ai_max_pairs || 8);
        setv('.lm-aiMinScore', c.ai_min_score || 0.5);
        setck('.lm-aiAutoStitch', !!c.ai_auto_stitch);
        setv('.lm-aiStrategy', c.ai_strategy || 'bridge');
        setck('.lm-aiExplain', c.ai_explain !== false);
      }

      async function saveCfg(){
        var dsId = elCfgDs ? elCfgDs.value : '';
        if (!dsId) { setStatus(elCfgStat, 'Select a dataset', 'err'); return false; }
        setStatus(elCfgStat, 'Saving...', '');
        var res = await api('/fabric/datasets/config', 'POST', { dataset_id: dsId, config: gatherCfg() });
        if (res && !res.error) { setStatus(elCfgStat, 'Saved', 'ok'); return true; }
        setStatus(elCfgStat, (res && res.error) || 'Failed', 'err'); return false;
      }
      async function loadCfg(){
        var dsId = elCfgDs ? elCfgDs.value : '';
        if (!dsId) { setStatus(elCfgStat, 'Select a dataset', 'err'); return; }
        setStatus(elCfgStat, 'Loading...', '');
        var res = await api('/fabric/datasets/config', 'POST', { dataset_id: dsId });
        if (res && res.config) { applyCfg(res.config); setStatus(elCfgStat, 'Loaded', 'ok'); }
        else setStatus(elCfgStat, (res && res.error) || 'Failed', 'err');
      }

      // ── Pipeline log ──────────────────────────────────────────────────────
      function pipeLog(msg, type){
        if (elLogWrap) { elLogWrap.style.display = 'block'; elLogWrap.open = true; }
        if (!elLogContent) return;
        var ts = new Date().toLocaleTimeString();
        var color = type === 'ok' ? 'var(--ok,#8fb87a)' : type === 'err' ? 'var(--err,#c96b6b)' : 'var(--dim2,#8a7e70)';
        elLogContent.innerHTML += '<div style="color:' + color + '">' + ts + ' ' + esc(msg) + '</div>';
        elLogContent.scrollTop = elLogContent.scrollHeight;
      }

      // ── Run the full pipeline (stages 1–4) ────────────────────────────────
      async function runPipeline(){
        var dsId = elCfgDs ? elCfgDs.value : '';
        if (!dsId) { setStatus(elCfgStat, 'Select a dataset', 'err'); return; }
        await saveCfg();
        if (elLogContent) elLogContent.innerHTML = '';
        pipeLog('Pipeline started for ' + dsId);
        var done = [];
        function ck(c){ var e = $(c); return !!(e && e.checked); }
        function vv(c, d){ var e = $(c); return (e && e.value) || d; }
        function iv(c, d){ var e = $(c); return parseInt((e && e.value) || d); }
        function fv(c, d){ var e = $(c); return parseFloat((e && e.value) || d); }

        if (ck('.lm-extract')) {
          setStatus(elCfgStat, 'Stage 1/4: Entity extraction...', '');
          pipeLog('Stage 1: Entity extraction');
          var eres = await api('/fabric/discover/entity_extract', 'POST', {
            dataset_id: dsId,
            max_records: iv('.lm-extractLimit', '500'),
            use_llm: true,
            worker_batch: 8,
          }, 300000);
          done.push('entities: ' + ((eres && (eres.entities || eres.entity_count)) || 0));
          pipeLog('Extracted ' + ((eres && (eres.entities || eres.entity_count))||0) + ' entities, ' + ((eres&&eres.relation_count)||0) + ' relations', 'ok');
        }
        if (ck('.lm-loom')) {
          setStatus(elCfgStat, 'Stage 2/4: Loom stitching...', '');
          var scope = vv('.lm-loomScope','internal');
          var loomArgs = {
            dataset_a: dsId, dataset_b: scope === 'cross' ? '' : dsId,
            mode: vv('.lm-mode','hybrid'), min_score: fv('.lm-minScore','0.4'),
            max_matches: iv('.lm-maxMatches','100'), edge_type: vv('.lm-edgeType','auto'),
            graph: vv('.lm-targetGraph','fabric'), tag_filter: vv('.lm-tagFilter',''),
            persist: ck('.lm-persist'),
          };
          pipeLog('Stage 2: Loom (mode=' + loomArgs.mode + ', min=' + loomArgs.min_score + ')');
          var lres = await api('/fabric/loom/run', 'POST', loomArgs, 300000);
          done.push('loom: ' + ((lres&&lres.total)||0) + ' matches');
          pipeLog('Stitched ' + ((lres&&lres.total)||0) + ' matches, ' + ((lres&&lres.persisted)||0) + ' persisted', 'ok');
        }
        if (ck('.lm-graphExtract')) {
          setStatus(elCfgStat, 'Stage 3/4: Graph extraction...', '');
          pipeLog('Stage 3: Graph extraction');
          var gres = await api('/fabric/entity_graph/extract', 'POST', {
            dataset_id: dsId, limit: iv('.lm-graphLimit','100'),
            mode: vv('.lm-graphMode','nlp'), persist: ck('.lm-graphPersist'),
          }, 300000);
          done.push('graph: ' + ((gres&&gres.relation_count)||0) + ' rels');
          pipeLog('Graph: ' + ((gres&&gres.relation_count)||0) + ' relations', 'ok');
        }
        if (ck('.lm-aiAnalyse')) {
          setStatus(elCfgStat, 'Stage 4/4: AI analysis...', '');
          pipeLog('Stage 4: AI link analysis');
          var ares = await api('/mcp/call', 'POST', {
            name: 'fabric.ai_analyse_links',
            arguments: { max_pairs: iv('.lm-aiPairs','8'), min_score: fv('.lm-aiMinScore','0.5'),
                         auto_stitch: ck('.lm-aiAutoStitch') }
          }, 120000);
          var aiContent = ares && ares.content, aiSugg = 0;
          try { if (typeof aiContent === 'string') aiSugg = (JSON.parse(aiContent).suggestions || []).length; } catch(e){}
          try { if (typeof aiContent === 'object' && aiContent && aiContent.suggestions) aiSugg = aiContent.suggestions.length; } catch(e){}
          done.push('ai: ' + aiSugg + ' suggestions');
          pipeLog('AI: ' + aiSugg + ' suggestions', 'ok');
        }
        if (!done.length) { setStatus(elCfgStat, 'No pipeline stages enabled', 'warn'); pipeLog('No stages enabled', 'err'); }
        else { setStatus(elCfgStat, 'Done: ' + done.join(', '), 'ok'); pipeLog('Done: ' + done.join(', '), 'ok'); }
        // Refresh the view so new entities/edges appear in the graph
        await refreshView();
      }

      // ── Wire events ───────────────────────────────────────────────────────
      if (elViewSrc)  elViewSrc.onchange  = refreshView;
      if (elViewDs)   elViewDs.onchange   = refreshView;
      if (elTypeFilt) elTypeFilt.onchange = refreshView;
      if (elIncRec)   elIncRec.onchange   = refreshView;
      if (elIncDs)    elIncDs.onchange    = refreshView;
      $('.lm-refresh').onclick = refreshView;

      $('.lm-tab-ent').onclick   = function(){ setListTab('entities'); };
      $('.lm-tab-rel').onclick   = function(){ setListTab('relations'); };
      $('.lm-tab-edges').onclick = function(){ setListTab('edges'); };
      if (elListSearch) elListSearch.oninput = renderList;

      // List rows → detail (event delegation)
      elListCont.addEventListener('click', function(ev){
        var row = ev.target.closest('.loom-list-row'); if (!row) return;
        var kind = row.getAttribute('data-kind'), idx = parseInt(row.getAttribute('data-idx'));
        if (kind) showItemDetail(kind, idx);
      });
      // Item detail actions (delegation)
      elItemBody.addEventListener('click', function(ev){
        var b = ev.target.closest('button'); if (!b) return;
        var id = b.getAttribute('data-id');
        if (b.classList.contains('lm-act-focus'))    focusInGraph(id);
        else if (b.classList.contains('lm-act-mentions')) loadEntityMentions(id);
        else if (b.classList.contains('lm-act-related'))  loadEntityRelated(id);
      });
      $('.lm-itemclose').onclick = function(){ elItemDet.style.display = 'none'; };

      $('.lm-cfgsave').onclick = saveCfg;
      $('.lm-cfgload').onclick = loadCfg;
      $('.lm-runpipe').onclick = runPipeline;
      $('.lm-logclear').onclick = function(){ if (elLogContent) elLogContent.innerHTML = ''; };

      // ── NER backend wiring ────────────────────────────────────────────────
      async function loomNerStatus(silent) {
        var r = await api('/fabric/entity_graph/ner', 'POST', {}, 15000);
        if (!r) { if (!silent) setStatus('.lm-ner-st', 'Failed', 'err'); return; }
        var ab = r.active_backend || r.backend || '?';
        var actEl = $('.lm-ner-active'); if (actEl) actEl.textContent = ab;
        var sel = $('.lm-ner-backend');
        if (sel) { for (var i = 0; i < sel.options.length; i++) { if (sel.options[i].value === ab) { sel.selectedIndex = i; break; } } }
        if (!silent) setStatus('.lm-ner-st', 'Active: ' + ab + (r.available ? ' | gliner:' + (r.available.gliner ? '\u2713' : '\u2717') + ' spacy:' + (r.available.spacy ? '\u2713' : '\u2717') : ''), 'ok');
      }
      if ($('.lm-ner-apply')) {
        $('.lm-ner-apply').onclick = async function() {
          var be = ($('.lm-ner-backend') && $('.lm-ner-backend').value) || 'auto';
          var model = ($('.lm-ner-model') && $('.lm-ner-model').value.trim()) || '';
          var body = { backend: be };
          if (model) { if (be === 'gliner') body.gliner_model = model; else body.spacy_model = model; }
          setStatus('.lm-ner-st', 'Applying\u2026', '');
          var r = await api('/fabric/entity_graph/ner', 'POST', body, 30000);
          if (r && !r.error) { setStatus('.lm-ner-st', 'Active: ' + (r.active_backend || be), 'ok'); await loomNerStatus(true); }
          else setStatus('.lm-ner-st', (r && r.error) || 'Failed', 'err');
        };
      }
      if ($('.lm-ner-status')) { $('.lm-ner-status').onclick = function(){ loomNerStatus(false); }; }
      if ($('.lm-ner-install')) {
        $('.lm-ner-install').onclick = async function() {
          var sel = $('.lm-ner-install-pkg'); var pkg = (sel && sel.value) || '';
          var spmodel = ($('.lm-ner-spmodel') && $('.lm-ner-spmodel').value.trim()) || '';
          if (!pkg && !spmodel) { setStatus('.lm-ner-st', 'Specify package or model', 'warn'); return; }
          setStatus('.lm-ner-st', 'Installing\u2026 (may take a minute)', '');
          $('.lm-ner-install').disabled = true;
          var r = await api('/fabric/entity_graph/ner_install', 'POST', { package: pkg, model_name: spmodel }, 300000);
          $('.lm-ner-install').disabled = false;
          if (r && r.ok) { setStatus('.lm-ner-st', 'Installed OK', 'ok'); await loomNerStatus(false); }
          else setStatus('.lm-ner-st', (r && r.error) || 'Failed (exit ' + (r && r.returncode) + ')', 'err');
        };
      }
      loomNerStatus(true);

      // ── Initial population ────────────────────────────────────────────────
      populateDatasets().then(function(){ /* ready */ });

      // Expose a tiny handle for host code / other panels if needed
      this._loomRefresh = refreshView;
    },
  });
})();