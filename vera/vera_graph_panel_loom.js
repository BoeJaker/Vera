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
      '.lmp-itemdetail{position:absolute;top:50px;left:14px;width:300px;max-height:calc(100% - 70px);background:var(--bg1,#1f1d1a);border:1px solid var(--acc,#5a9e8f);border-radius:4px;box-shadow:0 4px 16px rgba(0,0,0,.5);z-index:9999;overflow-y:auto;display:none}',
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
                '<option value="discovery">Discovery graph</option>' +
                '<option value="visible">Visible graph (current)</option>' +
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
            '<label class="loom-check"><input type="checkbox" class="lm-incsubent"><span>Include sub-entities (pages, records)</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-incpageents"><span>Include page-level entities from discovery</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-mergeActive" checked><span>Merge into active graph (overlay, don\'t replace)</span></label>' +
            '<div style="display:flex;gap:4px;margin-top:5px">' +
              '<button class="lbtn teal lm-refresh" style="flex:1">\u21bb Refresh</button>' +
              '<button class="lbtn lm-runvis" style="flex:1" title="Run pipeline stages on the visible graph dataset">\u25b6︎ Run on visible</button>' +
            '</div>' +
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
        // DATASET LINKS — dynamic cross-dataset connection queries
        '<details class="loom-section" open>' +
          '<summary class="loom-section-head"><span class="loom-section-title">Dataset Links</span><span class="loom-section-sub">shared entities</span></summary>' +
          '<div class="loom-section-body">' +
            '<div class="loom-hint">Build a dataset-to-dataset graph from shared entities, precomputed loom edges, and live matching — a combined view of every way datasets connect.</div>' +
            '<div class="row"><label>Connections</label>' +
              '<select class="lm-dl-mode">' +
                '<option value="entities" selected>Shared entities (extracted)</option>' +
                '<option value="stitched">Precomputed (loom edges)</option>' +
                '<option value="both">Both</option>' +
              '</select></div>' +
            '<div class="row"><label>Entity type</label>' +
              '<select class="lm-dl-type">' +
                '<option value="">All types</option>' +
                '<option value="person">Person</option>' +
                '<option value="organisation">Organisation</option>' +
                '<option value="technology">Technology</option>' +
                '<option value="domain">Domain</option>' +
                '<option value="named_entity">Named entity</option>' +
              '</select></div>' +
            '<div class="row"><label>Min shared</label><input class="lm-dl-min" type="number" value="2" min="1" max="100"><span class="loom-unit">entities</span></div>' +
            '<div class="row"><label>Name filter</label><input class="lm-dl-filter" placeholder="only entities matching…"></div>' +
            '<label class="loom-check"><input type="checkbox" class="lm-dl-showent" checked><span>Show linking entities as nodes (between datasets)</span></label>' +
            '<label class="loom-check"><input type="checkbox" class="lm-dl-merge"><span>Merge into active graph (overlay)</span></label>' +
            '<div style="display:flex;gap:4px;margin-top:5px">' +
              '<button class="lbtn primary lm-dl-run" style="flex:1">Build link graph</button>' +
              '<button class="lbtn lm-dl-live" style="flex:1" title="Also run loom record-matching live between the linked datasets (not persisted)">+ live match</button>' +
            '</div>' +
            '<div class="status-bar lm-dl-stat" style="font-size:8.5px"></div>' +
          '</div>' +
        '</details>' +
        // DATASET CONFIG TARGET
        '<details class="loom-section" open>' +
          '<summary class="loom-section-head"><span class="loom-section-title">Dataset Config</span><span class="loom-section-sub">target &amp; actions</span></summary>' +
          '<div class="loom-section-body">' +
            '<div class="row"><label>Dataset</label>' +
              '<select class="lm-cfgds"><option value="">Select dataset...</option></select></div>' +
            '<div class="row" title="0 = unlimited. Shared with Discover panel.">' +
              '<label>Text cap</label>' +
              '<input type="number" class="lm-textcap" value="0" min="0" max="200000" style="width:70px" title="Max chars per page/record (0=unlimited)">' +
              '<span class="loom-unit">chars</span>' +
              '<label style="margin-left:6px">Rec cap</label>' +
              '<input type="number" class="lm-reccap" value="0" min="0" max="200000" style="width:70px" title="Max chars stored per record">' +
            '</div>' +
            '<div style="display:flex;gap:4px;margin-top:5px">' +
              '<button class="lbtn primary lm-cfgsave" style="flex:1">Save</button>' +
              '<button class="lbtn lm-cfgload" style="flex:1">Load</button>' +
              '<button class="lbtn teal lm-runpipe" style="flex:1">Run</button>' +
            '</div>' +
            '<div style="display:flex;gap:4px;margin-top:4px">' +
              '<button class="lbtn lm-synth" style="flex:1" title="Build a 3rd-order topic model from this dataset">◈ 3rd-order synthesis</button>' +
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
            '<div style="display:flex;gap:4px;margin-top:4px">' +
              '<button class="lbtn lm-ner-sync-disc" style="flex:1;font-size:8px">⧗ Sync from Discover</button>' +
            '</div>' +
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
        var includeSubEnt   = !!($('.lm-incsubent') && $('.lm-incsubent').checked);
        var includePageEnts = !!($('.lm-incpageents') && $('.lm-incpageents').checked);
        if (!dsId) includeDatasets = true;

        st.data = { entities: [], relations: [], edges: [], _stitchedNodes: null };

        // ── Visible graph mode: read nodes/edges directly from the host graph ──
        if (src === 'visible') {
          if (graph && graph.state && graph.state.nodeIndex) {
            var vnodes = Object.values(graph.state.nodeIndex);
            vnodes.forEach(function(n) {
              st.data.entities.push({
                id: n.id, name: n.label || n.id,
                type: n.type || 'Node',
                mention_count: (n.props && n.props.mention_count) || 1,
                props: n.props || {}
              });
            });
            // edges from graph state
            if (graph.state.edges) {
              graph.state.edges.forEach(function(e) {
                st.data.relations.push({
                  from: e.from, to: e.to, rel: e.rel || 'EDGE',
                  from_name: e.from, to_name: e.to, props: {}
                });
              });
            }
          }
          if (elViewStat) elViewStat.textContent = st.data.entities.length + ' nodes from visible graph';
          renderList();
          await loadGraph(src, dsId);
          return;
        }

        // ── Discovery graph mode: pull from discover/graph endpoint ──
        if (src === 'discovery') {
          var discDs = dsId;
          if (!discDs) {
            // Find most recent discovery dataset from history
            var hist = await api('/fabric/discover/history');
            var crawls = (hist && hist.crawls) || [];
            if (crawls.length) discDs = crawls[0].dataset_id || '';
          }
          if (discDs) {
            var dg = await api('/fabric/discover/graph?dataset_id=' + encodeURIComponent(discDs) + '&include_entities=true');
            if (dg && !dg.error) {
              (dg.nodes || []).forEach(function(n) {
                st.data.entities.push({
                  id: n.id, name: n.label || n.id,
                  type: n.type || 'Node',
                  mention_count: (n.props && n.props.mention_count) || 1,
                  props: n.props || {}
                });
              });
              (dg.edges || []).forEach(function(e) {
                st.data.relations.push({
                  from: e.from, to: e.to, rel: e.rel || 'LINKS_TO',
                  from_name: e.from, to_name: e.to, props: {}
                });
              });
            }
          }
          if (elViewStat) elViewStat.textContent = st.data.entities.length + ' nodes from discovery';
          renderList();
          await loadGraph(src, dsId || discDs);
          return;
        }

        // ── Standard entity/stitched/combined modes ──
        if (src === 'entities' || src === 'combined') {
          // Scoped to one dataset: 500 is plenty. "All datasets" needs a much
          // larger pull so entities from every dataset show, not just the global
          // top-500 by mention_count (the backend ceiling is 20000).
          var qs = '?limit=' + (dsId ? '500' : '20000');
          if (dsId) qs += '&dataset_id=' + encodeURIComponent(dsId);
          if (typeFilter) qs += '&entity_type=' + encodeURIComponent(typeFilter);
          if (includeRecords) qs += '&include_records=1';
          if (includeDatasets) qs += '&include_datasets=1';
          var entRes = await api('/fabric/entity_graph/snapshot' + qs);
          // Auto-fallback: if the snapshot returned nothing and we have no dataset
          // context (e.g. fabric structure graph or discovery graph), read from the
          // visible graph nodes so the panel is not empty.
          if ((!entRes || !(entRes.nodes && entRes.nodes.length)) && !dsId) {
            if (graph && graph.state && graph.state.nodeIndex) {
              var _vn = Object.values(graph.state.nodeIndex);
              _vn.forEach(function(n) {
                st.data.entities.push({
                  id: n.id, name: n.label || n.id,
                  type: n.type || 'Node',
                  mention_count: (n.props && n.props.mention_count) || 1,
                  props: n.props || {}
                });
              });
              if (graph.state.edges) {
                graph.state.edges.forEach(function(e) {
                  st.data.relations.push({
                    from: e.from, to: e.to, rel: e.rel || 'EDGE',
                    from_name: e.from, to_name: e.to, props: {}
                  });
                });
              }
              if (elViewStat) elViewStat.textContent = st.data.entities.length + ' nodes from visible graph';
              renderList();
              await loadGraph('visible', dsId);
              return;
            }
          }
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
          // Sub-entities: also pull records and page entities from this dataset
          if (includeSubEnt && dsId) {
            var brRes = await api('/fabric/browse', 'POST',
              { dataset_id: dsId, limit: 200, offset: 0, search: '', lite: true }, 15000);
            (brRes && brRes.records || []).forEach(function(r) {
              var rid = r.id || r.record_id;
              if (rid && !st.data.entities.some(function(e){ return e.id === rid; })) {
                st.data.entities.push({
                  id: rid,
                  name: r.title || r.name || (r.data && r.data.title) || (r.text || '').slice(0,60) || rid,
                  type: 'FabricRecord',
                  mention_count: 1,
                  props: r.data || r
                });
              }
            });
          }
          // Page-level entities from discovery
          if (includePageEnts && dsId) {
            var pgRes = await api('/fabric/discover/graph?dataset_id=' + encodeURIComponent(dsId) + '&include_entities=true');
            (pgRes && pgRes.nodes || []).filter(function(n){ return n.type === 'Page'; })
              .forEach(function(n) {
                if (!st.data.entities.some(function(e){ return e.id === n.id; })) {
                  st.data.entities.push({
                    id: n.id, name: n.label || n.id, type: 'Page',
                    mention_count: (n.props && n.props.word_count) || 1,
                    props: n.props || {}
                  });
                }
              });
            (pgRes && pgRes.edges || []).forEach(function(e) {
              st.data.relations.push({
                from: e.from, to: e.to, rel: e.rel || 'LINKS_TO',
                from_name: e.from, to_name: e.to, props: {}
              });
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
        // 'visible' and 'discovery' populate st.data.entities/relations the same
        // way as 'entities', so they must drive the graph the same way — otherwise
        // selecting them would build an empty node set and clear the host graph.
        if (src === 'entities' || src === 'combined' || src === 'visible' || src === 'discovery') {
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

        // ── Combine duplicate Dataset nodes across graph types ────────────────
        // The discovery graph and the fabric-structure graph both emit a node
        // for the same dataset, but their ids can differ by sanitisation (the
        // discovery pipeline runs dataset_id through re.sub(r"[^a-zA-Z0-9_.]","_")
        // — discovery.py). graph.addNode only dedupes on an EXACT id match, so the
        // two survive as twin "same name" nodes. Canonicalise dataset identity and
        // remap each incoming Dataset id onto an equivalent node already on the
        // active graph (or an earlier one in this batch), rewriting its edges, so
        // the two graphs link through ONE shared dataset node. Scoped to
        // Dataset-type nodes only — it never merges other node types.
        function _dsKey(idOrName){
          return String(idOrName == null ? '' : idOrName)
            .trim().toLowerCase().replace(/[^a-z0-9._]/g, '_')
            .replace(/_+/g, '_').replace(/^_+|_+$/g, '');
        }
        function _isDs(n){ return n && (n.type === 'Dataset' || (n.props && n.props.root)); }
        var _dsCanon = {};   // canonical key -> winning node id
        // Seed with Dataset nodes already on the active graph — they win, so the
        // overlay attaches to whatever the user is currently looking at.
        if (graph && graph.state && graph.state.nodeIndex) {
          Object.keys(graph.state.nodeIndex).forEach(function(nid){
            var en = graph.state.nodeIndex[nid];
            if (!_isDs(en)) return;
            var k = _dsKey((en.props && en.props.id) || en.id);
            if (k && !_dsCanon[k]) _dsCanon[k] = en.id;
          });
        }
        var _dsRemap = {};   // incoming id -> canonical id (only when different)
        nodes.forEach(function(n){
          if (!_isDs(n)) return;
          var k = _dsKey((n.props && n.props.id) || n.id);
          if (!k) return;
          if (_dsCanon[k] && _dsCanon[k] !== n.id) _dsRemap[n.id] = _dsCanon[k];
          else if (!_dsCanon[k]) _dsCanon[k] = n.id;   // first sighting wins
        });
        if (Object.keys(_dsRemap).length) {
          nodes = nodes.filter(function(n){ return !_dsRemap[n.id]; });
          edges.forEach(function(e){
            if (_dsRemap[e.from]) e.from = _dsRemap[e.from];
            if (_dsRemap[e.to])   e.to   = _dsRemap[e.to];
          });
          if (elViewStat) {
            elViewStat.textContent = (elViewStat.textContent || '') +
              ' · merged ' + Object.keys(_dsRemap).length + ' dup dataset node(s)';
          }
        }

        // Drive the host graph. When "Merge into active graph" is checked we
        // OVERLAY onto whatever is currently on screen (addNode/addEdge both
        // dedupe), so the loom builds on the active graph instead of replacing
        // it with a loom-only view. Otherwise replace via load().
        var mergeActive = !!($('.lm-mergeActive') && $('.lm-mergeActive').checked);
        if (graph && mergeActive && graph.addNode && graph.addEdge) {
          nodes.forEach(function(n){ graph.addNode(n, true); });
          edges.forEach(function(e){ graph.addEdge(e, true); });
          if (graph.wake) graph.wake();
          else if (nodes.length && graph.focusNode) graph.focusNode(nodes[0].id);
        } else if (graph && graph.load) {
          graph.load({ nodes: nodes, edges: edges });
        }
        if (graph) {
          // Open the panel so the user sees its controls alongside the graph.
          if (papi && papi.isActive && !papi.isActive() && papi.activate) papi.activate();
        }
      }

      // ── Mirror the active graph's contents into the Items list ─────────────
      // The Items section reflects whatever is currently ON the host graph:
      //   nodes → Entities tab, edges → Relations tab.
      // This is READ-ONLY — it never loads anything into the graph (unlike the
      // View → Refresh action), so it doesn't fight the "no auto-load on open"
      // rule. Kept live while the panel is open; only re-renders when the node/
      // edge counts actually change, so it won't clobber the user's scroll while
      // they browse. The Edges tab (loom stitched edges) is left untouched.
      var _activeSig = '';
      function syncItemsFromActiveGraph(force){
        if (!(graph && graph.state && graph.state.nodeIndex)) return;
        var ns = graph.state.nodes || Object.values(graph.state.nodeIndex);
        var es = graph.state.edges || [];
        var sig = ns.length + ':' + es.length;
        if (!force && sig === _activeSig) return;
        _activeSig = sig;
        st.data.entities = ns.map(function(n){
          return { id: n.id, name: n.label || n.id, type: n.type || 'Node',
                   mention_count: (n.props && (n.props.mention_count || n.props.count)) || 1,
                   props: n.props || {} };
        });
        st.data.relations = es.map(function(e){
          var fn = graph.state.nodeIndex[e.from], tn = graph.state.nodeIndex[e.to];
          return { from: e.from, to: e.to, rel: e.rel || 'EDGE',
                   from_name: (fn && fn.label) || e.from,
                   to_name:   (tn && tn.label) || e.to,
                   props: e.props || {} };
        });
        renderList();
        if (elViewStat) {
          elViewStat.textContent = st.data.entities.length + ' nodes · ' +
            st.data.relations.length + ' edges (active graph)';
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
      // Mirrored into the graph's shared bottom Terminal so loom activity is
      // visible alongside discovery/worldview output without opening the panel.
      function pipeLog(msg, type){
        try {
          if (graph && graph.bottomDrawer && graph.bottomDrawer.log) {
            graph.bottomDrawer.log('[loom] ' + msg, type);
          }
        } catch(_) {}
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

      // ── Run the entity-extraction pipeline over the ACTIVE graph ──────────
      // Unlike runPipeline (which needs a stored dataset), this reads the text
      // off whatever nodes are currently on the host graph — labels, titles,
      // text previews, URLs — and runs them through the same NER engine. The
      // extracted entities are merged back IN PLACE: each entity is linked to
      // the source node(s) it was found in (MENTIONED_IN), and inferred
      // entity↔entity relations are added as edges. Nothing is replaced, so the
      // graph grows rather than reloads.
      async function runOnActiveGraph(){
        if (!(graph && graph.state && graph.state.nodeIndex)) {
          setStatus(elViewStat, 'No active graph to parse', 'err'); return;
        }
        var vnodes = Object.values(graph.state.nodeIndex);
        var items = [];
        vnodes.forEach(function(n){
          var p = n.props || {};
          var txt = [n.label, p.title, p.name, p.description, p.text, p.text_preview, p.url]
            .filter(function(x){ return x && typeof x === 'string'; }).join('. ');
          if (txt && txt.trim().length > 2) items.push({ id: n.id, text: txt.slice(0, 6000) });
        });
        if (!items.length) {
          setStatus(elViewStat, 'No text found on active-graph nodes', 'warn');
          pipeLog('Active-graph extraction: no node text', 'err'); return;
        }
        setStatus(elViewStat, 'Extracting entities from ' + items.length + ' nodes…', '');
        pipeLog('Active-graph extraction: parsing ' + items.length + ' nodes');
        var ct = ($('.lm-contentType') && $('.lm-contentType').value) || 'text';
        var res = await api('/fabric/entity_graph/extract_text', 'POST',
          { items: items, content_type: ct }, 180000);
        if (!res || res.error) {
          setStatus(elViewStat, (res && res.error) || 'Extraction failed', 'err');
          pipeLog('Active-graph extraction failed: ' + ((res && res.error) || '?'), 'err'); return;
        }
        var nAdded = 0, eAdded = 0;
        (res.nodes || []).forEach(function(nd){
          if (graph.addNode) {
            graph.addNode({ id: nd.id, label: nd.name || nd.id, type: nd.type || 'Entity', props: nd.props || {} }, true);
            nAdded++;
          }
          // Link each extracted entity back to the source node(s) it came from.
          ((nd.props && nd.props.record_ids) || []).forEach(function(rid){
            if (graph.state.nodeIndex[rid] && graph.addEdge) {
              graph.addEdge({ from: nd.id, to: rid, rel: 'MENTIONED_IN', props: {} }, true);
              eAdded++;
            }
          });
        });
        (res.edges || []).forEach(function(e){
          if (graph.addEdge) { graph.addEdge({ from: e.from, to: e.to, rel: e.rel || 'RELATED_TO', props: e.props || {} }, true); eAdded++; }
        });
        if (graph.wake) graph.wake();
        // Reflect the new entities in the Items list too.
        st.data.entities = (res.nodes || []).map(function(nd){
          return { id: nd.id, name: nd.name || nd.id, type: nd.type || 'entity',
                   mention_count: (nd.props && nd.props.mention_count) || 1, props: nd.props || {} };
        });
        st.data.relations = (res.edges || []).map(function(e){
          return { from: e.from, to: e.to, from_name: e.from, to_name: e.to, rel: e.rel || 'RELATED_TO', props: e.props || {} };
        });
        renderList();
        setStatus(elViewStat, (res.entities || nAdded) + ' entities, ' + (res.relations || 0) + ' relations from active graph', 'ok');
        pipeLog('Active-graph extraction: +' + nAdded + ' entities, +' + eAdded + ' edges', 'ok');
      }

      // ── Dataset Links: dynamic cross-dataset connection graphs ────────────
      // Builds a dataset↔dataset graph from up to three connection sources:
      //   1. shared entities — the entity-extraction system: an entity that is
      //      mentioned in records of two datasets links those datasets
      //   2. precomputed     — loom-stitched record edges aggregated per pair
      //   3. live            — loom matching run on the fly between the linked
      //      pairs (persist:false) — "found there and then"
      // The result is loaded into the host graph (replace or overlay), with the
      // linking entities optionally shown as nodes between the datasets.
      async function buildDatasetLinks(includeLive){
        var stat = $('.lm-dl-stat');
        var mode       = ($('.lm-dl-mode')  || {}).value || 'entities';
        var etype      = ($('.lm-dl-type')  || {}).value || '';
        var minShared  = parseInt(($('.lm-dl-min') || {}).value || '2', 10) || 1;
        var nameFilter = (($('.lm-dl-filter') || {}).value || '').trim().toLowerCase();
        var showEnts   = !!($('.lm-dl-showent') && $('.lm-dl-showent').checked);
        var merge      = !!($('.lm-dl-merge')   && $('.lm-dl-merge').checked);
        setStatus(stat, 'Querying connections…', '');
        pipeLog('Dataset links: querying (' + mode + (etype ? ', type=' + etype : '') +
                ', min shared ' + minShared + ')…');

        var nodes = [], edges = [], nodeMap = {};
        function addN(spec){ if (!spec.id || nodeMap[spec.id]) return; nodeMap[spec.id] = 1; nodes.push(spec); }
        function dsNode(did){
          addN({ id: did, label: String(did).split('.').pop() || did, type: 'Dataset',
                 layer: 'structure', props: { id: did } });
        }
        function pairKey(a, b){ return a < b ? a + ' ' + b : b + ' ' + a; }

        var entPairs = {};   // pairKey -> { count, sample: [entity names] }
        var entRows  = [];   // entities spanning >= 2 datasets (for showEnts)

        function _tallyEntity(row){
          var ds = row.ds || [];
          if (!Array.isArray(ds) || ds.length < 2) return;
          entRows.push(row);
          for (var i = 0; i < ds.length; i++) {
            for (var j = i + 1; j < ds.length; j++) {
              var k = pairKey(String(ds[i]), String(ds[j]));
              var rec = entPairs[k] || (entPairs[k] = { count: 0, sample: [] });
              rec.count++;
              if (rec.sample.length < 6) rec.sample.push(row.name || row.id);
            }
          }
        }

        if (mode === 'entities' || mode === 'both') {
          // PRIMARY source: the entity store the discover/extraction system
          // actually writes to (SQLite fabric_entities, served by the
          // entity_graph snapshot API). Each entity carries props.datasets —
          // every dataset it was seen in. Neo4j only holds the small subset
          // explicitly persisted to the graph, which is why querying it
          // directly surfaced almost none of the discover-extracted entities.
          var qsE = '?limit=20000';
          if (etype) qsE += '&entity_type=' + encodeURIComponent(etype);
          var snap = await api('/fabric/entity_graph/snapshot' + qsE, 'GET', undefined, 90000);
          ((snap && snap.nodes) || []).forEach(function(n){
            var nm = String(n.name || (n.props && n.props.name) || n.id || '');
            if (nameFilter && nm.toLowerCase().indexOf(nameFilter) < 0) return;
            var ds = (n.props && n.props.datasets) || [];
            _tallyEntity({ id: n.id, name: nm,
                           type: (n.props && n.props.type) || n.type || 'entity',
                           ds: ds });
          });
          // FALLBACK: entities that only exist as Neo4j graph nodes.
          if (!entRows.length) {
            var cy = 'MATCH (e:Entity)-[:MENTIONED_IN|HAS_ENTITY]-(r:FabricRecord) ' +
                     'WHERE r.dataset_id IS NOT NULL ' +
                     (etype ? 'AND e.type = $etype ' : '') +
                     (nameFilter ? 'AND toLower(coalesce(e.name, e.id)) CONTAINS $nf ' : '') +
                     'WITH e, collect(DISTINCT r.dataset_id) AS ds ' +
                     'WHERE size(ds) >= 2 ' +
                     'RETURN e.id AS id, coalesce(e.name, e.id) AS name, e.type AS type, ds ' +
                     'LIMIT 4000';
            var eres = await api('/fabric/graph/query', 'POST',
              { cypher: cy, params: { etype: etype, nf: nameFilter } }, 60000);
            ((eres && eres.rows) || []).forEach(_tallyEntity);
          }
        }

        var stitchPairs = {};   // pairKey -> edge count
        if (mode === 'stitched' || mode === 'both') {
          var cy2 = 'MATCH (a:FabricRecord)-[r:RELATED_TO|SIMILAR_TO|REFERENCES|DEPENDS_ON|DERIVED_FROM|SHARES_TOPIC]-(b:FabricRecord) ' +
                    'WHERE a.dataset_id IS NOT NULL AND b.dataset_id IS NOT NULL ' +
                    'AND a.dataset_id < b.dataset_id ' +
                    'RETURN a.dataset_id AS da, b.dataset_id AS db, count(r) AS n LIMIT 2000';
          var sres = await api('/fabric/graph/query', 'POST', { cypher: cy2 }, 60000);
          ((sres && sres.rows) || []).forEach(function(row){
            if (row.da && row.db) stitchPairs[pairKey(String(row.da), String(row.db))] = row.n || 1;
          });
        }

        // ── Assemble the dataset-level graph ────────────────────────────────
        var pairList = [];
        Object.keys(entPairs).forEach(function(k){
          if (entPairs[k].count < minShared) return;
          var ab = k.split(' ');
          pairList.push(ab);
          dsNode(ab[0]); dsNode(ab[1]);
          edges.push({ from: ab[0], to: ab[1], rel: 'SHARES_ENTITIES', layer: 'loom',
                       props: { shared: entPairs[k].count,
                                sample: entPairs[k].sample.join(', ') } });
        });
        Object.keys(stitchPairs).forEach(function(k){
          var ab = k.split(' ');
          pairList.push(ab);
          dsNode(ab[0]); dsNode(ab[1]);
          edges.push({ from: ab[0], to: ab[1], rel: 'LOOM_LINKED', layer: 'loom',
                       props: { stitched_edges: stitchPairs[k] } });
        });

        // Linking entities as intermediate nodes (only ones whose pairs passed)
        if (showEnts && entRows.length) {
          var keptDs = {};
          Object.keys(nodeMap).forEach(function(id){ keptDs[id] = 1; });
          entRows.slice(0, 150).forEach(function(row){
            var ds = (row.ds || []).filter(function(d){ return keptDs[d]; });
            if (ds.length < 2) return;
            addN({ id: row.id, label: String(row.name || row.id).slice(0, 40),
                   type: 'Entity', layer: 'entities',
                   props: { type: row.type || 'entity', datasets: row.ds } });
            ds.forEach(function(d){
              edges.push({ from: row.id, to: d, rel: 'SHARED_IN', layer: 'entities' });
            });
          });
        }

        // ── Optional live loom pass over the top pairs ──────────────────────
        if (includeLive && pairList.length) {
          setStatus(stat, 'Live matching ' + Math.min(5, pairList.length) + ' pair(s)…', '');
          var seen = {}, done = 0;
          for (var pi = 0; pi < pairList.length && done < 5; pi++) {
            var pk2 = pairList[pi].join(' ');
            if (seen[pk2]) continue;
            seen[pk2] = 1; done++;
            var lr = await api('/fabric/loom/run', 'POST', {
              dataset_a: pairList[pi][0], dataset_b: pairList[pi][1],
              mode: 'hybrid', min_score: 0.4, max_matches: 20, persist: false,
            }, 120000);
            var nMatch = (lr && (lr.total || (lr.matches && lr.matches.length))) || 0;
            if (nMatch) {
              edges.push({ from: pairList[pi][0], to: pairList[pi][1],
                           rel: 'LIVE_MATCH', layer: 'loom', _dashed: true,
                           props: { matches: nMatch, computed: 'live' } });
            }
          }
        }

        if (!nodes.length) {
          setStatus(stat, 'No cross-dataset connections found (try lowering Min shared, or run entity extraction first)', 'warn');
          pipeLog('Dataset links: ' + entRows.length + ' multi-dataset entities seen, ' +
                  'but no pair met the min-shared threshold', 'err');
          return;
        }

        // ── Drive the host graph ────────────────────────────────────────────
        if (graph && merge && graph.addNode && graph.addEdge) {
          nodes.forEach(function(n){ graph.addNode(n, true); });
          edges.forEach(function(e){ graph.addEdge(e, true); });
          if (graph.wake) graph.wake();
        } else if (graph && graph.load) {
          graph.load({ nodes: nodes, edges: edges });
        }
        syncItemsFromActiveGraph(true);
        var dsCount = nodes.filter(function(n){ return n.type === 'Dataset'; }).length;
        var summary = dsCount + ' datasets · ' + edges.length + ' connection(s)' +
          (showEnts ? ' · ' + (nodes.length - dsCount) + ' linking entities' : '');
        setStatus(stat, summary, 'ok');
        pipeLog('Dataset links: ' + summary + ' (from ' + entRows.length + ' shared entities)', 'ok');
      }

      // ── Wire events ───────────────────────────────────────────────────────
      if ($('.lm-dl-run'))  $('.lm-dl-run').onclick  = function(){ buildDatasetLinks(false); };
      if ($('.lm-dl-live')) $('.lm-dl-live').onclick = function(){ buildDatasetLinks(true); };
      if (elViewSrc)  elViewSrc.onchange  = refreshView;
      if (elViewDs)   elViewDs.onchange   = refreshView;
      if (elTypeFilt) elTypeFilt.onchange = refreshView;
      if (elIncRec)   elIncRec.onchange   = refreshView;
      if (elIncDs)    elIncDs.onchange    = refreshView;
      if ($('.lm-incsubent'))   $('.lm-incsubent').onchange   = refreshView;
      if ($('.lm-incpageents')) $('.lm-incpageents').onchange = refreshView;

      // "Run on visible" button — parses the active graph's node text through the
      // entity-extraction pipeline (see runOnActiveGraph). This works even when
      // the visible graph isn't backed by a stored dataset (e.g. the fabric
      // structure graph, an ad-hoc query result, or a hand-built graph).
      if ($('.lm-runvis')) {
        $('.lm-runvis').onclick = function() { runOnActiveGraph(); };
      }
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
      // Trigger 3rd-order synthesis on the selected dataset. Progress streams to
      // the graph terminal via fabric.synthesize.progress (handled by Discover+).
      if ($('.lm-synth')) $('.lm-synth').onclick = async function(){
        var dsId = elCfgDs ? elCfgDs.value : '';
        if (!dsId) { setStatus(elCfgStat, 'Select a dataset', 'err'); return; }
        setStatus(elCfgStat, 'Synthesising 3rd-order…', '');
        pipeLog('3rd-order synthesis: ' + dsId);
        var r = await api('/fabric/synthesize/topic', 'POST',
          { dataset_id: dsId, max_records: 120, neighbor_depth: 1, use_llm: true }, 600000);
        if (r && !r.error) {
          var n = r.entries || r.entry_count || 0;
          setStatus(elCfgStat, '3rd-order: ' + n + ' entries', 'ok');
          pipeLog('3rd-order model: ' + n + ' entries', 'ok');
        } else {
          setStatus(elCfgStat, (r && r.error) || 'Synthesis failed', 'err');
          pipeLog('Synthesis failed: ' + ((r && r.error) || '?'), 'err');
        }
      };
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

      // Listen for NER settings broadcast from the Discover panel
      window.addEventListener('vera:ner:changed', function(ev) {
        var d = ev && ev.detail; if (!d) return;
        if (d.backend) {
          var bsel = $('.lm-ner-backend');
          if (bsel) { for (var i = 0; i < bsel.options.length; i++) { if (bsel.options[i].value === d.backend) { bsel.selectedIndex = i; break; } } }
        }
        if (d.model) { var mf = $('.lm-ner-model'); if (mf) mf.value = d.model; }
        // Apply immediately
        var body = { backend: d.backend || 'auto' };
        if (d.model) body[d.backend === 'gliner' ? 'gliner_model' : 'spacy_model'] = d.model;
        api('/fabric/entity_graph/ner', 'POST', body, 15000).then(function(r) {
          if (r && !r.error) {
            setStatus('.lm-ner-st', 'Synced from Discover: ' + (r.active_backend || d.backend), 'ok');
            loomNerStatus(true);
          }
        });
        if (d.labels) {
          api('/fabric/entity_graph/ner_labels', 'POST',
              { labels: d.labels, threshold: d.threshold || 0.4 }, 10000);
        }
        pipeLog('NER synced from Discover: ' + (d.backend || 'auto'));
      });

      // "Sync from Discover" button in loom NER section broadcasts request to discover
      if ($('.lm-ner-sync-disc')) {
        $('.lm-ner-sync-disc').onclick = function() {
          window.dispatchEvent(new CustomEvent('vera:ner:request_sync'));
        };
      }
      // Listen for discover panel's sync-on-request
      window.addEventListener('vera:ner:request_sync', function() {
        // Discover panel handles this and broadcasts vera:ner:changed
      });

      // ── Sync shared text caps from Discover panel (sessionStorage) ──────────
      (function() {
        try {
          var caps = JSON.parse(sessionStorage.getItem('vera_crawl_caps') || '{}');
          if (caps.page_text_cap != null && $('.lm-textcap')) $('.lm-textcap').value = caps.page_text_cap;
          if (caps.max_record_chars != null && $('.lm-reccap')) $('.lm-reccap').value = caps.max_record_chars;
        } catch(_) {}
        // Save on change
        function saveCaps() {
          try {
            var t = parseInt(($('.lm-textcap') && $('.lm-textcap').value) || '0', 10);
            var r = parseInt(($('.lm-reccap') && $('.lm-reccap').value) || '0', 10);
            sessionStorage.setItem('vera_crawl_caps', JSON.stringify({page_text_cap: t, max_record_chars: r}));
          } catch(_) {}
        }
        if ($('.lm-textcap')) $('.lm-textcap').onchange = saveCaps;
        if ($('.lm-reccap')) $('.lm-reccap').onchange = saveCaps;
      })();

      // ── Initial population ────────────────────────────────────────────────
      // Populate the dataset selectors ONLY. Do NOT auto-run refreshView() —
      // loading entities INTO the host graph on open is surprising and clobbers
      // whatever the user already has on screen. Loading into the graph happens
      // only when the user presses "Refresh" / "Run on visible".
      populateDatasets();

      // The Items section instead mirrors the active graph (read-only) so it is
      // populated the moment the panel opens, and stays live as the graph grows.
      syncItemsFromActiveGraph(true);
      var _itemsTimer = setInterval(function(){
        // Only work while this panel is the visible one — cheap no-op otherwise.
        if (papi && papi.isActive && !papi.isActive()) return;
        syncItemsFromActiveGraph(false);
      }, 2000);
      // Clean up the poll if the panel's body is ever torn out of the DOM.
      if (bodyEl && window.MutationObserver) {
        try {
          var _mo = new MutationObserver(function(){
            if (!document.body.contains(bodyEl)) { clearInterval(_itemsTimer); _mo.disconnect(); }
          });
          _mo.observe(document.body, { childList: true, subtree: true });
        } catch(_) {}
      }

      // Expose tiny handles for host code / other panels if needed
      this._loomRefresh = refreshView;
      this._loomSyncItems = syncItemsFromActiveGraph;
    },
  });
})();